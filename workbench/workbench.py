#!/usr/bin/env python3
"""Machine-facing controller for the brettinternet.workbench Herdr plugin."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "brettinternet.workbench")
POLL_INTERVAL_SECONDS = 0.05
START_TIMEOUT_SECONDS = 10.0
MAX_LOG_BYTES = 5 * 1024 * 1024
READ_CHUNK_BYTES = 8192
MAX_DIRTY_BUFFERS = 64
MAX_BUFFER_NAME_LENGTH = 4096
UNNAMED_BUFFER_NAME = "[No Name]"
JOB_CLOSE_TIMEOUT_SECONDS = 2.0
JOB_KILL_TIMEOUT_SECONDS = 0.5
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_JOB_STATUSES = {"starting", "running", "cancelling"}

# Keep this expression fixed: only the socket is supplied by the caller.  It
# asks for one extra entry so Python can report truncation while keeping the
# returned dirty-buffer list bounded.  The result is JSON containing the stable
# Neovim buffer number and its name, not a stringified Vimscript value that
# callers would need to interpret.
NVIM_MODIFIED_BUFFERS_EXPR = (
    'json_encode(map(getbufinfo({"bufmodified": 1})[:64], '
    '"{\\"id\\": v:val.bufnr, \\"name\\": strcharpart(v:val.name, 0, 4096)}"))'
)
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()
_CURRENT_JOB_STATE_PATH: Path | None = None


class WorkbenchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "workbench_error",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_root() -> Path:
    configured = os.environ.get("WORKBENCH_STATE_DIR")
    root = (
        Path(configured).expanduser()
        if configured
        else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / "herdr"
        / "plugins"
        / PLUGIN_ID
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def socket_root() -> Path:
    root = Path(tempfile.gettempdir()) / f"herdr-workbench-{os.getuid()}"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def emit(result: dict[str, Any]) -> None:
    print(json.dumps({"ok": True, **result}, sort_keys=True))


def fail(
    message: str,
    *,
    code: str = "workbench_error",
    details: dict[str, Any] | None = None,
) -> None:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error.update(details)
    print(json.dumps({"ok": False, "error": error}, sort_keys=True), file=sys.stderr)
    raise SystemExit(1)


def herdr_binary() -> str:
    binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
    if not binary:
        raise WorkbenchError("herdr is not available")
    return binary


def run(
    argv: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=check,
        cwd=cwd,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def herdr(*args: str, check: bool = True) -> dict[str, Any]:
    try:
        completed = run([herdr_binary(), *args], check=check)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise WorkbenchError(f"herdr {' '.join(args)} failed: {detail}") from error
    if not completed.stdout.strip():
        return {"result": {}}
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise WorkbenchError(f"herdr returned invalid JSON for {' '.join(args)}") from error


def herdr_text(*args: str) -> str:
    try:
        return run([herdr_binary(), *args]).stdout
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise WorkbenchError(f"herdr {' '.join(args)} failed: {detail}") from error


def result_of(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result")
    if not isinstance(result, dict):
        raise WorkbenchError("Herdr response did not contain a result object")
    return result


def find_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = find_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_value(child, key)
            if found is not None:
                return found
    return None


def require_herdr_context() -> dict[str, Any]:
    if os.environ.get("HERDR_ENV") != "1":
        raise WorkbenchError("workbench must run from a Herdr-managed pane")
    response = herdr("pane", "current", "--current")
    pane = result_of(response).get("pane")
    if not isinstance(pane, dict) or not isinstance(pane.get("pane_id"), str):
        raise WorkbenchError("Herdr did not return the current pane")
    return pane


def resolve_path(raw: str, *, must_exist: bool = True) -> Path:
    path = Path(raw).expanduser().resolve()
    if must_exist and not path.exists():
        raise WorkbenchError(f"path does not exist: {path}")
    return path


def default_direction(pane_id: str) -> str:
    try:
        layout = result_of(herdr("pane", "layout", "--pane", pane_id)).get("layout", {})
        panes = layout.get("panes", []) if isinstance(layout, dict) else []
        rect = next(
            (
                pane.get("rect")
                for pane in panes
                if isinstance(pane, dict) and pane.get("pane_id") == pane_id
            ),
            None,
        )
        if isinstance(rect, dict):
            width, height = rect.get("width"), rect.get("height")
            if isinstance(width, int) and isinstance(height, int):
                return "down" if height * 2 > width else "right"
    except WorkbenchError:
        pass
    return "right"


def placement_args(
    placement: str, context: dict[str, Any], cwd: Path, focus: bool
) -> list[str]:
    pane_id = str(context["pane_id"])
    workspace_id = context.get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")
    if placement == "auto":
        placement = default_direction(pane_id)
    args = ["--cwd", str(cwd), "--focus" if focus else "--no-focus"]
    if placement in {"right", "down"}:
        return [
            "--placement",
            "split",
            "--target-pane",
            pane_id,
            "--direction",
            placement,
            *args,
        ]
    if placement == "tab":
        if not workspace_id:
            raise WorkbenchError("current Herdr workspace is unavailable")
        return ["--placement", "tab", "--workspace", str(workspace_id), *args]
    if placement == "zoomed":
        return ["--placement", "zoomed", "--target-pane", pane_id, *args]
    raise WorkbenchError(f"unsupported placement: {placement}")


def plugin_pane_open(
    entrypoint: str,
    placement: str,
    context: dict[str, Any],
    cwd: Path,
    focus: bool,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    args = [
        "plugin",
        "pane",
        "open",
        "--plugin",
        PLUGIN_ID,
        "--entrypoint",
        entrypoint,
        *placement_args(placement, context, cwd, focus),
    ]
    for key, value in (environment or {}).items():
        args.extend(["--env", f"{key}={value}"])
    response = herdr(*args)
    pane_id = find_value(result_of(response), "pane_id")
    if not isinstance(pane_id, str):
        raise WorkbenchError("Herdr did not return the opened plugin pane ID")
    return {"paneId": pane_id, "response": response}


def pane_presence(pane_id: str) -> bool | None:
    try:
        herdr("pane", "get", pane_id)
        return True
    except WorkbenchError as error:
        if "pane_not_found" in str(error):
            return False
        return None


def pane_exists(pane_id: str) -> bool:
    presence = pane_presence(pane_id)
    if presence is None:
        raise WorkbenchError(f"could not determine whether pane exists: {pane_id}")
    return presence


def checked_pane_presence(pane_id: str) -> bool | None:
    try:
        return pane_exists(pane_id)
    except WorkbenchError:
        return None


def focus_plugin_pane(pane_id: str) -> None:
    herdr("plugin", "pane", "focus", pane_id)


def close_plugin_pane(pane_id: str) -> None:
    herdr("plugin", "pane", "close", pane_id)


@contextmanager
def locked_json(path: Path) -> Iterator[dict[str, Any]]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        yield data
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(data, sort_keys=True) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(key, threading.Lock())
    with process_lock:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def resource_lock(name: str) -> Iterator[None]:
    with file_lock(state_root() / "locks" / f"{name}.lock"):
        yield


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise WorkbenchError(f"unknown workbench resource: {path.stem}") from error
    except json.JSONDecodeError as error:
        raise WorkbenchError(f"invalid workbench state: {path}") from error
    if not isinstance(value, dict):
        raise WorkbenchError(f"invalid workbench state: {path}")
    return value


def write_request(kind: str, data: dict[str, Any]) -> Path:
    request_id = uuid.uuid4().hex
    path = state_root() / "requests" / f"{kind}-{request_id}.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(data, sort_keys=True) + "\n")
    path.chmod(0o600)
    return path


def nvim_binary() -> str:
    binary = os.environ.get("WORKBENCH_NVIM") or shutil.which("nvim")
    if not binary:
        raise WorkbenchError("nvim is not available")
    return binary


def lazygit_binary() -> str:
    binary = os.environ.get("WORKBENCH_LAZYGIT") or shutil.which("lazygit")
    if not binary:
        raise WorkbenchError("lazygit is not available")
    return binary


def editor_state_path(workspace_id: str) -> Path:
    return state_root() / "editors" / f"{workspace_id}.json"


def _normalize_modified_buffer(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkbenchError(
            "Neovim returned an invalid modified-buffer record",
            code="editor_dirty_unknown",
        )
    buffer_id = value.get("id", value.get("bufnr"))
    if isinstance(buffer_id, bool) or not isinstance(buffer_id, int) or buffer_id < 1:
        raise WorkbenchError(
            "Neovim returned an invalid modified-buffer ID",
            code="editor_dirty_unknown",
        )
    name = value.get("name")
    if not isinstance(name, str):
        raise WorkbenchError(
            "Neovim returned an invalid modified-buffer name",
            code="editor_dirty_unknown",
        )
    return {
        "id": buffer_id,
        "name": (name or UNNAMED_BUFFER_NAME)[:MAX_BUFFER_NAME_LENGTH],
    }


def _query_modified_buffers(socket_path: str) -> tuple[list[dict[str, Any]], bool]:
    try:
        completed = run(
            [
                nvim_binary(),
                "--server",
                socket_path,
                "--remote-expr",
                NVIM_MODIFIED_BUFFERS_EXPR,
            ]
        )
    except (subprocess.CalledProcessError, OSError) as error:
        detail = (
            getattr(error, "stderr", None)
            or getattr(error, "stdout", None)
            or str(error)
        ).strip()
        raise WorkbenchError(
            f"could not inspect modified Neovim buffers: {detail}",
            code="editor_dirty_unknown",
        ) from error
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise WorkbenchError(
            "Neovim returned invalid modified-buffer JSON",
            code="editor_dirty_unknown",
        ) from error
    if not isinstance(payload, list):
        raise WorkbenchError(
            "Neovim returned invalid modified-buffer data",
            code="editor_dirty_unknown",
        )
    truncated = len(payload) > MAX_DIRTY_BUFFERS
    buffers = [
        _normalize_modified_buffer(value)
        for value in payload[:MAX_DIRTY_BUFFERS]
    ]
    return buffers, truncated


def modified_buffers(socket_path: str) -> list[dict[str, Any]]:
    return _query_modified_buffers(socket_path)[0]


# Keep the descriptive alias available to callers that use the controller as
# a small Python module rather than through its CLI.
def nvim_modified_buffers(socket_path: str) -> list[dict[str, Any]]:
    return modified_buffers(socket_path)


def open_in_nvim(socket_path: str, path: Path, line: int, column: int) -> None:
    command = [nvim_binary(), "--server", socket_path, "--remote-silent"]
    if line > 0:
        command.append(f"+{line}")
    command.append(str(path))
    try:
        run(command)
        if column > 1:
            run(
                [
                    nvim_binary(),
                    "--server",
                    socket_path,
                    "--remote-expr",
                    f"cursor({max(line, 1)},{column})",
                ]
            )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise WorkbenchError(f"could not control Neovim: {detail}") from error


def wait_for_nvim(socket_path: str) -> None:
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            run([nvim_binary(), "--server", socket_path, "--remote-expr", "1"])
            return
        except subprocess.CalledProcessError:
            time.sleep(POLL_INTERVAL_SECONDS)
    raise WorkbenchError("Neovim did not expose its RPC socket in time")


def editor_open(args: argparse.Namespace) -> None:
    context = require_herdr_context()
    workspace_id = str(context.get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID") or "")
    with resource_lock(f"editor-{workspace_id}"):
        _editor_open_locked(args, context, workspace_id)


def _editor_open_locked(
    args: argparse.Namespace, context: dict[str, Any], workspace_id: str
) -> None:
    if not workspace_id:
        raise WorkbenchError("current Herdr workspace is unavailable")
    path = resolve_path(args.path)
    cwd = resolve_path(args.cwd) if args.cwd else (path if path.is_dir() else path.parent)
    record_path = editor_state_path(workspace_id)
    if record_path.exists():
        record = read_json(record_path)
        try:
            pane_id, server = _owned_editor_record(record, workspace_id)
        except WorkbenchError:
            pane_id, server = None, None
        if isinstance(server, str) and pane_id is not None and pane_exists(pane_id):
            try:
                open_in_nvim(server, path, args.line, args.column)
                if args.focus:
                    focus_plugin_pane(pane_id)
                emit({"action": "editor.open", "created": False, "editor": record})
                return
            except WorkbenchError:
                pass

    server = str(socket_root() / f"{workspace_id}.nvim.sock")
    request = write_request(
        "editor",
        {
            "cwd": str(cwd),
            "path": str(path),
            "line": args.line,
            "column": args.column,
            "server": server,
        },
    )
    opened = plugin_pane_open(
        "editor",
        args.placement,
        context,
        cwd,
        args.focus,
        {"WORKBENCH_REQUEST": str(request)},
    )
    pane_id = opened["paneId"]
    record = {
        "kind": "editor",
        "pluginId": PLUGIN_ID,
        "workspaceId": workspace_id,
        "paneId": pane_id,
        "server": server,
        "cwd": str(cwd),
        "createdAt": now(),
    }
    with locked_json(record_path) as state:
        state.clear()
        state.update(record)
    try:
        wait_for_nvim(server)
    except WorkbenchError:
        close_plugin_pane(pane_id)
        raise
    emit({"action": "editor.open", "created": True, "editor": record})


def _owned_editor_record(
    record: dict[str, Any], workspace_id: str
) -> tuple[str, str | None]:
    if record.get("pluginId") not in {None, PLUGIN_ID}:
        raise WorkbenchError(
            "editor state belongs to another plugin",
            code="editor_not_owned",
        )
    kind = record.get("kind")
    if kind is not None and kind != "editor":
        raise WorkbenchError(
            "editor state does not belong to a managed editor",
            code="editor_not_owned",
        )
    recorded_workspace_id = record.get("workspaceId")
    if recorded_workspace_id is not None and recorded_workspace_id != workspace_id:
        raise WorkbenchError(
            "editor state belongs to another Herdr workspace",
            code="editor_not_owned",
        )
    pane_id = record.get("paneId")
    if not isinstance(pane_id, str) or not pane_id:
        raise WorkbenchError("editor state has no pane ID", code="editor_state_invalid")
    if ":" in pane_id and not pane_id.startswith(f"{workspace_id}:"):
        raise WorkbenchError(
            "editor pane belongs to another Herdr workspace",
            code="editor_not_owned",
        )
    server = record.get("server")
    if server is not None and not isinstance(server, str):
        raise WorkbenchError(
            "editor state has an invalid Neovim server",
            code="editor_state_invalid",
        )
    return pane_id, server


def _editor_dirty_fields(
    record: dict[str, Any],
    pane_is_live: bool | None,
) -> None:
    if pane_is_live is not True:
        # A stale or unavailable pane cannot be treated as clean.  Drop old
        # observations rather than exposing them as the current editor state.
        record["dirty"] = None
        record["dirtyBuffers"] = None
        record.pop("dirtyBufferCount", None)
        record.pop("dirtyBuffersTruncated", None)
        return
    server = record.get("server")
    if not isinstance(server, str):
        record["dirty"] = None
        record["dirtyBuffers"] = None
        return
    try:
        buffers, truncated = _query_modified_buffers(server)
    except WorkbenchError:
        record["dirty"] = None
        record["dirtyBuffers"] = None
        record.pop("dirtyBufferCount", None)
        record.pop("dirtyBuffersTruncated", None)
        return
    record["dirty"] = bool(buffers)
    record["dirtyBuffers"] = buffers
    record["dirtyBufferCount"] = len(buffers)
    record["dirtyBuffersTruncated"] = truncated


def editor_status(_args: argparse.Namespace) -> None:
    context = require_herdr_context()
    workspace_id = str(context.get("workspace_id") or "")
    if not workspace_id:
        raise WorkbenchError("current Herdr workspace is unavailable")
    with resource_lock(f"editor-{workspace_id}"):
        path = editor_state_path(workspace_id)
        if not path.exists():
            emit({"action": "editor.status", "editor": None})
            return
        record = read_json(path)
        pane_id, _server = _owned_editor_record(record, workspace_id)
        if record.get("closedAt") and record.get("running") is False:
            record["dirty"] = None
            record["dirtyBuffers"] = None
            record.pop("dirtyBufferCount", None)
            record.pop("dirtyBuffersTruncated", None)
            emit({"action": "editor.status", "editor": record})
            return
        pane_is_live = checked_pane_presence(pane_id)
        record["running"] = pane_is_live
        if pane_is_live is False:
            record["stale"] = True
        elif pane_is_live is True:
            record.pop("stale", None)
        _editor_dirty_fields(record, pane_is_live)
        emit({"action": "editor.status", "editor": record})


def _mark_editor_closed(path: Path, pane_id: str, *, stale: bool = False) -> None:
    with locked_json(path) as state:
        state["closedAt"] = now()
        state["running"] = False
        if stale:
            state["stale"] = True
            state["dirty"] = None
            state["dirtyBuffers"] = None
            state.pop("dirtyBufferCount", None)
            state.pop("dirtyBuffersTruncated", None)
        else:
            state.pop("stale", None)
            state.pop("dirty", None)
            state.pop("dirtyBuffers", None)
            state.pop("dirtyBufferCount", None)
            state.pop("dirtyBuffersTruncated", None)


def editor_close(args: argparse.Namespace) -> None:
    context = require_herdr_context()
    workspace_id = str(context.get("workspace_id") or "")
    if not workspace_id:
        raise WorkbenchError("current Herdr workspace is unavailable")
    force = bool(getattr(args, "force", False))
    expected_pane_id = getattr(args, "expected_pane_id", None)
    with resource_lock(f"editor-{workspace_id}"):
        path = editor_state_path(workspace_id)
        if not path.exists():
            if expected_pane_id is not None:
                raise WorkbenchError(
                    "managed editor pane changed before close",
                    code="editor_pane_changed",
                    details={"expectedPaneId": expected_pane_id, "currentPaneId": None},
                )
            # Closing an already absent editor is a safe no-op.  In particular,
            # it must not claim that an unknown editor was clean.
            emit({"action": "editor.close", "paneId": None, "closed": False})
            return
        record = read_json(path)
        pane_id, server = _owned_editor_record(record, workspace_id)
        if expected_pane_id is not None and expected_pane_id != pane_id:
            raise WorkbenchError(
                "managed editor pane changed before close",
                code="editor_pane_changed",
                details={"expectedPaneId": expected_pane_id, "currentPaneId": pane_id},
            )
        if record.get("closedAt") and record.get("running") is False:
            emit(
                {
                    "action": "editor.close",
                    "paneId": pane_id,
                    "closed": False,
                    "alreadyClosed": True,
                    "forced": force,
                }
            )
            return
        pane_is_live = checked_pane_presence(pane_id)
        if pane_is_live is None:
            raise WorkbenchError(
                "could not determine whether the managed editor pane exists",
                code="editor_pane_unknown",
            )
        if pane_is_live is False:
            _mark_editor_closed(path, pane_id, stale=True)
            emit(
                {
                    "action": "editor.close",
                    "paneId": pane_id,
                    "closed": False,
                    "stale": True,
                    "forced": force,
                }
            )
            return

        dirty: list[dict[str, Any]] = []
        truncated = False
        if not force:
            if not isinstance(server, str):
                raise WorkbenchError(
                    "could not inspect modified Neovim buffers; refusing to close editor",
                    code="editor_dirty_unknown",
                    details={"dirtyBuffers": None},
                )
            try:
                dirty, truncated = _query_modified_buffers(server)
            except WorkbenchError as error:
                raise WorkbenchError(
                    "could not inspect modified Neovim buffers; refusing to close editor",
                    code="editor_dirty_unknown",
                    details={"dirtyBuffers": None},
                ) from error
            if dirty:
                raise WorkbenchError(
                    "refusing to close editor with modified buffers; use --force",
                    code="editor_dirty",
                    details={
                        "dirtyBuffers": dirty,
                        "dirtyBufferCount": len(dirty),
                        "dirtyBuffersTruncated": truncated,
                    },
                )
        try:
            # The plugin-scoped Herdr operation is intentional: even a stale
            # or forged pane ID cannot make this controller close an arbitrary
            # non-plugin pane.
            close_plugin_pane(pane_id)
        except WorkbenchError as error:
            if "pane_not_found" in str(error) or "plugin_pane_not_found" in str(error):
                _mark_editor_closed(path, pane_id, stale=True)
                emit(
                    {
                        "action": "editor.close",
                        "paneId": pane_id,
                        "closed": False,
                        "stale": True,
                        "forced": force,
                    }
                )
                return
            raise
        _mark_editor_closed(path, pane_id)
        emit(
            {
                "action": "editor.close",
                "paneId": pane_id,
                "closed": True,
                "forced": force,
                "dirtyBuffers": dirty if force is False else None,
            }
        )


def internal_editor() -> None:
    request_path = os.environ.get("WORKBENCH_REQUEST")
    if not request_path:
        raise WorkbenchError("WORKBENCH_REQUEST is required")
    request = read_json(Path(request_path))
    server = str(request["server"])
    server_path = Path(server)
    if server_path.exists():
        server_path.unlink()
    os.chdir(str(request["cwd"]))
    argv = [nvim_binary(), "--listen", server]
    line = int(request.get("line", 1))
    column = int(request.get("column", 1))
    if column > 1:
        argv.append(f"+call cursor({max(line, 1)},{column})")
    elif line > 0:
        argv.append(f"+{line}")
    argv.append(str(request["path"]))
    os.execvp(argv[0], argv)


def job_state_path(job_id: str) -> Path:
    return state_root() / "jobs" / f"{job_id}.json"


def job_start(args: argparse.Namespace) -> None:
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise WorkbenchError("job start requires a command after --")
    context = require_herdr_context()
    cwd = resolve_path(args.cwd or os.getcwd())
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    path = job_state_path(job_id)
    record = {
        "kind": "job",
        "pluginId": PLUGIN_ID,
        "jobId": job_id,
        "workspaceId": context.get("workspace_id"),
        "paneId": None,
        "cwd": str(cwd),
        "command": command,
        "interactive": args.interactive,
        "logFile": str(state_root() / "jobs" / f"{job_id}.log"),
        "status": "starting",
        "createdAt": now(),
    }
    with locked_json(path) as state:
        state.update(record)
    request = write_request("job", {"stateFile": str(path)})
    try:
        opened = plugin_pane_open(
            "job",
            args.placement,
            context,
            cwd,
            args.focus,
            {"WORKBENCH_REQUEST": str(request)},
        )
    except Exception:
        with locked_json(path) as state:
            state["status"] = "failed"
            state["finishedAt"] = now()
        raise
    pane_id = opened["paneId"]
    with locked_json(path) as state:
        state["paneId"] = pane_id
    emit({"action": "job.start", "job": read_json(path)})


def _record_job_process(
    path: Path, process: subprocess.Popen[Any], *, process_group: bool = True
) -> None:
    process_group_id: int | None = None
    if process_group:
        try:
            process_group_id = os.getpgid(process.pid)
        except (OSError, ProcessLookupError):
            process_group_id = None
    with locked_json(path) as state:
        state["processId"] = process.pid
        state["processMode"] = "group" if process_group else "direct"
        if process_group_id is not None:
            state["processGroupId"] = process_group_id


def _mark_job_cancelled(path: Path, *, reason: str | None = None) -> dict[str, Any]:
    with locked_json(path) as state:
        if state.get("status") in ACTIVE_JOB_STATUSES:
            state["status"] = "cancelled"
            state["completionRecorded"] = True
            state["exitCode"] = 130
            state["finishedAt"] = now()
            if reason:
                state["error"] = reason
    return read_json(path)


def _recorded_process_is_owned(record: dict[str, Any]) -> bool:
    process_id = record.get("processId")
    if (
        not isinstance(process_id, int)
        or isinstance(process_id, bool)
        or process_id <= 1
    ):
        return False
    process_mode = record.get("processMode")
    if process_mode == "direct":
        return True
    process_group_id = record.get("processGroupId")
    return (
        isinstance(process_group_id, int)
        and not isinstance(process_group_id, bool)
        and process_group_id == process_id
    )


def stop_recorded_job_process(record: dict[str, Any]) -> bool:
    """Stop only the process group created and recorded by this job."""
    if not _recorded_process_is_owned(record):
        return False
    process_id = int(record["processId"])
    process_mode = record.get("processMode")
    process_group_id = (
        int(record["processGroupId"])
        if process_mode != "direct"
        else None
    )
    try:
        if process_group_id is None:
            os.kill(process_id, signal.SIGTERM)
        else:
            os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError as error:
        raise WorkbenchError(
            "could not terminate the recorded job process",
            code="job_process_not_owned",
        ) from error
    deadline = time.monotonic() + JOB_KILL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    try:
        if process_group_id is None:
            os.kill(process_id, signal.SIGKILL)
        else:
            os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError as error:
        raise WorkbenchError(
            "could not terminate the recorded job process",
            code="job_process_not_owned",
        ) from error
    return True


def internal_job() -> None:
    request_path = os.environ.get("WORKBENCH_REQUEST")
    if not request_path:
        raise WorkbenchError("WORKBENCH_REQUEST is required")
    request = read_json(Path(request_path))
    path = Path(str(request["stateFile"]))
    record = read_json(path)
    command = record.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise WorkbenchError("job command is invalid")
    with locked_json(path) as state:
        state["status"] = "running"
        state["startedAt"] = now()
        if os.environ.get("HERDR_PANE_ID"):
            state["paneId"] = os.environ["HERDR_PANE_ID"]
    return_code = 1
    failure: str | None = None
    global _CURRENT_JOB_STATE_PATH
    previous_job_state_path = _CURRENT_JOB_STATE_PATH
    _CURRENT_JOB_STATE_PATH = path
    try:
        if record.get("interactive"):
            return_code = run_interactive_job(command, str(record["cwd"]))
        else:
            return_code = run_captured_job(
                command, str(record["cwd"]), Path(str(record["logFile"]))
            )
    except KeyboardInterrupt:
        return_code = 130
    except Exception as error:
        failure = str(error)
    finally:
        _CURRENT_JOB_STATE_PATH = previous_job_state_path
    with locked_json(path) as state:
        cancellation_requested = state.get("status") == "cancelling" or state.get(
            "forceCloseRequested"
        )
        completion_recorded = bool(state.get("completionRecorded"))
        if not completion_recorded:
            state["exitCode"] = return_code
            state["status"] = (
                "failed"
                if failure and not cancellation_requested
                else "cancelled"
                if cancellation_requested or return_code in {130, -2, -9, -15}
                else "completed"
            )
        if failure:
            state["error"] = failure
        state["finishedAt"] = now()
    if failure:
        print(f"workbench job failed: {failure}", file=sys.stderr, flush=True)
    print(f"\n[workbench {record.get('jobId', path.stem)} exited {return_code}]", flush=True)
    hold_job_pane()


def mirror_output(chunk: bytes) -> None:
    binary_output = getattr(sys.stdout, "buffer", None)
    if binary_output is not None:
        binary_output.write(chunk)
        binary_output.flush()
    else:
        sys.stdout.write(chunk.decode(errors="replace"))
        sys.stdout.flush()


def run_captured_job(
    command: list[str], cwd: str, log_path: Path, state_path: Path | None = None
) -> int:
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    rotated_path = log_path.with_suffix(log_path.suffix + ".1")
    log = log_path.open("wb")
    try:
        log_path.chmod(0o600)
        written = 0
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
        )
        process_state_path = state_path or _CURRENT_JOB_STATE_PATH
        if process_state_path is not None:
            try:
                _record_job_process(process_state_path, process, process_group=True)
            except (OSError, WorkbenchError):
                stop_process(process, process_group=True)
                raise
        assert process.stdout is not None
        try:
            with process.stdout:
                while chunk := os.read(process.stdout.fileno(), READ_CHUNK_BYTES):
                    mirror_output(chunk)
                    with file_lock(log_path.with_suffix(log_path.suffix + ".lock")):
                        if written and written + len(chunk) > MAX_LOG_BYTES:
                            log.close()
                            os.replace(log_path, rotated_path)
                            log = log_path.open("wb")
                            log_path.chmod(0o600)
                            written = 0
                        log.write(chunk)
                        log.flush()
                        written += len(chunk)
            return process.wait()
        except KeyboardInterrupt:
            stop_process(process, process_group=True)
            raise
    finally:
        log.close()


def run_interactive_job(
    command: list[str], cwd: str, state_path: Path | None = None
) -> int:
    process = subprocess.Popen(command, cwd=cwd)
    process_state_path = state_path or _CURRENT_JOB_STATE_PATH
    if process_state_path is not None:
        try:
            _record_job_process(process_state_path, process, process_group=False)
        except (OSError, WorkbenchError):
            stop_process(process)
            raise
    try:
        return process.wait()
    except KeyboardInterrupt:
        stop_process(process)
        return 130


def stop_process(
    process: subprocess.Popen[Any], *, process_group: bool = False
) -> None:
    if process.poll() is not None:
        return
    if process_group:
        os.killpg(process.pid, 15)
    else:
        process.terminate()
    try:
        process.wait(timeout=0.5)
        return
    except subprocess.TimeoutExpired:
        pass
    if process_group:
        os.killpg(process.pid, 9)
    else:
        process.kill()
    process.wait()


def tail_log(path: Path, line_count: int) -> str:
    if line_count <= 0:
        return ""
    chunks: list[bytes] = []
    newlines = 0
    rotated = path.with_suffix(path.suffix + ".1")
    with file_lock(path.with_suffix(path.suffix + ".lock")):
        for candidate in (path, rotated):
            if not candidate.exists():
                continue
            with candidate.open("rb") as source:
                source.seek(0, os.SEEK_END)
                position = source.tell()
                candidate_chunks: list[bytes] = []
                while position > 0 and newlines <= line_count:
                    size = min(READ_CHUNK_BYTES, position)
                    position -= size
                    source.seek(position)
                    chunk = source.read(size)
                    candidate_chunks.append(chunk)
                    newlines += chunk.count(b"\n")
                chunks = list(reversed(candidate_chunks)) + chunks
                if newlines > line_count:
                    break
    content = b"".join(chunks)
    return b"".join(content.splitlines(keepends=True)[-line_count:]).decode(errors="replace")


def hold_job_pane() -> None:
    shell = os.environ.get("SHELL") or shutil.which("sh")
    if not shell:
        raise WorkbenchError("no shell is available to hold the completed job pane")
    os.execvp(shell, [shell, "-l"])


def find_job(job_id: str) -> tuple[Path, dict[str, Any]]:
    if not job_id.startswith("job-") or "/" in job_id:
        raise WorkbenchError(f"invalid job ID: {job_id}")
    path = job_state_path(job_id)
    return path, read_json(path)


def _owned_job_record(job_id: str, record: dict[str, Any]) -> str:
    if record.get("pluginId") not in {None, PLUGIN_ID}:
        raise WorkbenchError(
            "job state belongs to another plugin",
            code="job_not_owned",
        )
    recorded_job_id = record.get("jobId")
    if recorded_job_id is not None and recorded_job_id != job_id:
        raise WorkbenchError(
            "job state does not match the requested job",
            code="job_not_owned",
        )
    kind = record.get("kind")
    if kind is not None and kind != "job":
        raise WorkbenchError(
            "job state does not belong to a managed job",
            code="job_not_owned",
        )
    pane_id = record.get("paneId")
    if not isinstance(pane_id, str) or not pane_id:
        raise WorkbenchError("job state has no pane ID", code="job_state_invalid")
    workspace_id = record.get("workspaceId")
    if (
        isinstance(workspace_id, str)
        and ":" in pane_id
        and not pane_id.startswith(f"{workspace_id}:")
    ):
        raise WorkbenchError(
            "job pane belongs to another Herdr workspace",
            code="job_not_owned",
        )
    return pane_id


def _request_job_cancellation(path: Path) -> bool:
    with locked_json(path) as state:
        if state.get("status") not in ACTIVE_JOB_STATUSES:
            return False
        state["status"] = "cancelling"
        state.setdefault("cancelRequestedAt", now())
        state["forceCloseRequested"] = True
        state["forceCloseRequestedAt"] = now()
        return True


def _wait_for_job_terminal(path: Path, pane_id: str) -> dict[str, Any] | None:
    deadline = time.monotonic() + JOB_CLOSE_TIMEOUT_SECONDS
    while True:
        record = read_json(path)
        if record.get("status") in TERMINAL_JOB_STATUSES:
            return record
        pane_is_live = checked_pane_presence(pane_id)
        if pane_is_live is False:
            stop_recorded_job_process(record)
            return _mark_job_cancelled(
                path, reason="job pane exited while cancellation was requested"
            )
        if pane_is_live is None:
            return None
        if time.monotonic() >= deadline:
            return record
        time.sleep(POLL_INTERVAL_SECONDS)


def job_status(args: argparse.Namespace) -> None:
    with resource_lock(f"job-{args.job_id}"):
        path, record = find_job(args.job_id)
        pane_id = _owned_job_record(args.job_id, record)
        pane_is_live = checked_pane_presence(pane_id)
        if pane_is_live is False and record.get("status") in ACTIVE_JOB_STATUSES:
            # A controller can disappear with its child still alive.  The
            # process identity was recorded by the controller, so clean up
            # only that group.
            stop_recorded_job_process(record)
            with locked_json(path) as state:
                if state.get("status") == "cancelling" or state.get("forceCloseRequested"):
                    state["status"] = "cancelled"
                    state["completionRecorded"] = True
                    state["exitCode"] = 130
                elif state.get("status") in {"starting", "running"}:
                    state["status"] = "failed"
                    state["completionRecorded"] = True
                    state["exitCode"] = 1
                    state["error"] = "job pane exited before reporting completion"
                state["finishedAt"] = now()
            record = read_json(path)
        record["paneExists"] = pane_is_live
        emit({"action": "job.status", "job": record})


def job_list(_args: argparse.Namespace) -> None:
    jobs = []
    directory = state_root() / "jobs"
    if directory.exists():
        for path in sorted(directory.glob("job-*.json"), reverse=True):
            try:
                jobs.append(read_json(path))
            except WorkbenchError:
                continue
    emit({"action": "job.list", "jobs": jobs})


def job_read(args: argparse.Namespace) -> None:
    _path, record = find_job(args.job_id)
    pane_id = _owned_job_record(args.job_id, record)
    log_file = record.get("logFile")
    if isinstance(log_file, str) and Path(log_file).exists():
        output = tail_log(Path(log_file), args.lines)
        source = "log"
    else:
        if not isinstance(pane_id, str) or not pane_exists(pane_id):
            raise WorkbenchError(f"job pane is not available: {args.job_id}")
        output = herdr_text(
            "pane",
            "read",
            pane_id,
            "--source",
            "recent-unwrapped",
            "--lines",
            str(args.lines),
            "--format",
            "text",
        )
        source = "pane"
    emit({"action": "job.read", "jobId": args.job_id, "paneId": pane_id, "source": source, "output": output})


def job_cancel(args: argparse.Namespace) -> None:
    with resource_lock(f"job-{args.job_id}"):
        path, record = find_job(args.job_id)
        pane_id = _owned_job_record(args.job_id, record)
        if record.get("status") not in {"starting", "running"}:
            raise WorkbenchError(f"job is not running: {args.job_id}")
        if not pane_exists(pane_id):
            raise WorkbenchError(f"job pane is not available: {args.job_id}")
        with locked_json(path) as state:
            if state.get("status") not in {"starting", "running"}:
                raise WorkbenchError(f"job is not running: {args.job_id}")
            previous_status = str(state["status"])
            state["status"] = "cancelling"
            state["cancelRequestedAt"] = now()
        try:
            herdr("pane", "send-keys", pane_id, "ctrl+c")
        except WorkbenchError:
            with locked_json(path) as state:
                if state.get("status") == "cancelling":
                    state["status"] = previous_status
                    state.pop("cancelRequestedAt", None)
            raise
        emit({"action": "job.cancel", "job": read_json(path)})


def job_close(args: argparse.Namespace) -> None:
    force = bool(getattr(args, "force", False))
    with resource_lock(f"job-{args.job_id}"):
        path, record = find_job(args.job_id)
        pane_id = _owned_job_record(args.job_id, record)
        status = record.get("status")
        if record.get("paneClosedAt") and status not in ACTIVE_JOB_STATUSES:
            emit(
                {
                    "action": "job.close",
                    "jobId": args.job_id,
                    "paneId": pane_id,
                    "closed": False,
                    "alreadyClosed": True,
                    "forced": force,
                }
            )
            return
        if status in ACTIVE_JOB_STATUSES and not force:
            raise WorkbenchError("refusing to close a running job without --force")

        pane_is_live = checked_pane_presence(pane_id)
        if pane_is_live is None:
            raise WorkbenchError(
                f"could not determine whether job pane exists: {args.job_id}",
                code="job_pane_unknown",
            )

        if status in ACTIVE_JOB_STATUSES and force:
            if pane_is_live:
                _request_job_cancellation(path)
                cancellation_error: WorkbenchError | None = None
                try:
                    herdr("pane", "send-keys", pane_id, "ctrl+c")
                except WorkbenchError as error:
                    cancellation_error = error
                _wait_for_job_terminal(path, pane_id)
                current = read_json(path)
                if current.get("status") not in TERMINAL_JOB_STATUSES:
                    # Ctrl-C normally reaches the controller.  If it did not,
                    # terminate only the process group this job recorded.
                    stopped = stop_recorded_job_process(current)
                    if stopped:
                        _mark_job_cancelled(
                            path, reason="job was force-closed before it reported completion"
                        )
                    else:
                        if cancellation_error is not None:
                            raise cancellation_error
                        # Older records may not have process identity fields.
                        # The bounded wait plus an explicit --force request is
                        # the safest available cleanup for those records.
                        _mark_job_cancelled(
                            path, reason="job was force-closed before it reported completion"
                        )
            else:
                # The pane is already gone.  Reconcile the active record, but
                # never issue a close or key event for an unknown pane.
                stop_recorded_job_process(record)
                _mark_job_cancelled(
                    path, reason="job pane was already gone during force close"
                )

        record = read_json(path)
        if record.get("status") in ACTIVE_JOB_STATUSES:
            raise WorkbenchError(
                "refusing to close job before cancellation completed",
                code="job_cancellation_timeout",
            )
        pane_is_live = checked_pane_presence(pane_id)
        if pane_is_live is None:
            raise WorkbenchError(
                f"could not determine whether job pane exists: {args.job_id}",
                code="job_pane_unknown",
            )
        if pane_is_live:
            try:
                # As with editor close, this is deliberately plugin-scoped and
                # operates only on the pane recorded for this job.
                close_plugin_pane(pane_id)
            except WorkbenchError as error:
                if "pane_not_found" not in str(error) and "plugin_pane_not_found" not in str(error):
                    raise
        with locked_json(path) as state:
            state["paneClosedAt"] = now()
            if force:
                state["forceClosed"] = True
        emit({
            "action": "job.close",
            "jobId": args.job_id,
            "paneId": pane_id,
            "forced": force,
            "status": record.get("status"),
        })


def lazygit_state_path(workspace_id: str) -> Path:
    return state_root() / "lazygit" / f"{workspace_id}.json"


def lazygit_open(args: argparse.Namespace) -> None:
    context = require_herdr_context()
    workspace_id = str(context.get("workspace_id") or "")
    with resource_lock(f"lazygit-{workspace_id}"):
        _lazygit_open_locked(args, context, workspace_id)


def _lazygit_open_locked(
    args: argparse.Namespace, context: dict[str, Any], workspace_id: str
) -> None:
    cwd = resolve_path(args.cwd or os.getcwd())
    record_path = lazygit_state_path(workspace_id)
    if record_path.exists():
        record = read_json(record_path)
        pane_id = record.get("paneId")
        if isinstance(pane_id, str) and pane_exists(pane_id):
            if args.focus:
                focus_plugin_pane(pane_id)
            emit({"action": "lazygit.open", "created": False, "lazygit": record})
            return
    opened = plugin_pane_open("lazygit", args.placement, context, cwd, args.focus)
    record = {
        "kind": "lazygit",
        "workspaceId": workspace_id,
        "paneId": opened["paneId"],
        "cwd": str(cwd),
        "createdAt": now(),
    }
    with locked_json(record_path) as state:
        state.clear()
        state.update(record)
    emit({"action": "lazygit.open", "created": True, "lazygit": record})


def lazygit_close(_args: argparse.Namespace) -> None:
    context = require_herdr_context()
    path = lazygit_state_path(str(context.get("workspace_id") or ""))
    record = read_json(path)
    pane_id = record.get("paneId")
    if not isinstance(pane_id, str):
        raise WorkbenchError("lazygit state has no pane ID")
    if pane_exists(pane_id):
        close_plugin_pane(pane_id)
    with locked_json(path) as state:
        state["closedAt"] = now()
    emit({"action": "lazygit.close", "paneId": pane_id})


def internal_lazygit() -> None:
    binary = lazygit_binary()
    os.execvp(binary, [binary])


def action_editor() -> None:
    context = require_herdr_context()
    cwd = str(context.get("foreground_cwd") or context.get("cwd") or os.getcwd())
    editor_open(
        argparse.Namespace(
            path=cwd,
            line=1,
            column=1,
            cwd=cwd,
            placement="auto",
            focus=True,
        )
    )


def action_lazygit() -> None:
    context = require_herdr_context()
    cwd = str(context.get("foreground_cwd") or context.get("cwd") or os.getcwd())
    lazygit_open(
        argparse.Namespace(cwd=cwd, placement="auto", focus=True)
    )


def layout_show(_args: argparse.Namespace) -> None:
    context = require_herdr_context()
    workspace_id = str(context.get("workspace_id") or "")
    layout = result_of(herdr("pane", "layout", "--pane", str(context["pane_id"]))).get("layout")
    panes = result_of(herdr("pane", "list", "--workspace", workspace_id)).get("panes")
    emit({"action": "layout.show", "currentPane": context, "layout": layout, "panes": panes})


def pane_focus(args: argparse.Namespace) -> None:
    if not pane_exists(args.pane_id):
        raise WorkbenchError(f"pane does not exist: {args.pane_id}")
    focus_plugin_pane(args.pane_id)
    emit({"action": "pane.focus", "paneId": args.pane_id})


def add_placement(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--placement",
        choices=["auto", "right", "down", "tab", "zoomed"],
        default="auto",
    )
    focus = parser.add_mutually_exclusive_group()
    focus.add_argument("--focus", action="store_true")
    focus.add_argument("--no-focus", action="store_false", dest="focus")
    parser.set_defaults(focus=False)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise WorkbenchError(message)


def parser() -> argparse.ArgumentParser:
    root = JsonArgumentParser(
        prog="workbench",
        description="Control plugin-owned Herdr workbench panes",
    )
    commands = root.add_subparsers(dest="resource", required=True)

    layout = commands.add_parser("layout")
    layout.set_defaults(handler=layout_show)

    pane = commands.add_parser("pane")
    pane_commands = pane.add_subparsers(dest="pane_action", required=True)
    focus = pane_commands.add_parser("focus")
    focus.add_argument("pane_id")
    focus.set_defaults(handler=pane_focus)

    editor = commands.add_parser("editor")
    editor_commands = editor.add_subparsers(dest="editor_action", required=True)
    editor_open_parser = editor_commands.add_parser("open")
    editor_open_parser.add_argument("path")
    editor_open_parser.add_argument("--line", type=int, default=1)
    editor_open_parser.add_argument("--column", type=int, default=1)
    editor_open_parser.add_argument("--cwd")
    add_placement(editor_open_parser)
    editor_open_parser.set_defaults(handler=editor_open)
    editor_commands.add_parser("status").set_defaults(handler=editor_status)
    editor_close_parser = editor_commands.add_parser("close")
    editor_close_parser.add_argument("--expected-pane-id")
    editor_close_parser.add_argument("--force", action="store_true")
    editor_close_parser.set_defaults(handler=editor_close)

    job = commands.add_parser("job")
    job_commands = job.add_subparsers(dest="job_action", required=True)
    job_start_parser = job_commands.add_parser("start")
    job_start_parser.add_argument("--cwd")
    job_start_parser.add_argument(
        "--interactive",
        action="store_true",
        help="keep the command attached directly to the pane PTY instead of capturing a log",
    )
    add_placement(job_start_parser)
    job_start_parser.add_argument("command", nargs=argparse.REMAINDER)
    job_start_parser.set_defaults(handler=job_start)
    job_status_parser = job_commands.add_parser("status")
    job_status_parser.add_argument("job_id")
    job_status_parser.set_defaults(handler=job_status)
    job_commands.add_parser("list").set_defaults(handler=job_list)
    job_read_parser = job_commands.add_parser("read")
    job_read_parser.add_argument("job_id")
    job_read_parser.add_argument("--lines", type=int, default=120)
    job_read_parser.set_defaults(handler=job_read)
    job_cancel_parser = job_commands.add_parser("cancel")
    job_cancel_parser.add_argument("job_id")
    job_cancel_parser.set_defaults(handler=job_cancel)
    job_close_parser = job_commands.add_parser("close")
    job_close_parser.add_argument("job_id")
    job_close_parser.add_argument("--force", action="store_true")
    job_close_parser.set_defaults(handler=job_close)

    lazygit = commands.add_parser("lazygit")
    lazygit_commands = lazygit.add_subparsers(dest="lazygit_action", required=True)
    lazygit_open_parser = lazygit_commands.add_parser("open")
    lazygit_open_parser.add_argument("--cwd")
    add_placement(lazygit_open_parser)
    lazygit_open_parser.set_defaults(handler=lazygit_open)
    lazygit_commands.add_parser("close").set_defaults(handler=lazygit_close)

    action_editor_parser = commands.add_parser("_open_editor_action", help=argparse.SUPPRESS)
    action_editor_parser.set_defaults(handler=lambda _args: action_editor())
    action_lazygit_parser = commands.add_parser("_open_lazygit_action", help=argparse.SUPPRESS)
    action_lazygit_parser.set_defaults(handler=lambda _args: action_lazygit())
    internal_editor_parser = commands.add_parser("_editor", help=argparse.SUPPRESS)
    internal_editor_parser.set_defaults(handler=lambda _args: internal_editor())
    internal_job_parser = commands.add_parser("_job", help=argparse.SUPPRESS)
    internal_job_parser.set_defaults(handler=lambda _args: internal_job())
    internal_lazygit_parser = commands.add_parser("_lazygit", help=argparse.SUPPRESS)
    internal_lazygit_parser.set_defaults(handler=lambda _args: internal_lazygit())
    return root


def main() -> None:
    try:
        args = parser().parse_args()
        args.handler(args)
    except WorkbenchError as error:
        fail(str(error), code=error.code, details=error.details)


if __name__ == "__main__":
    main()

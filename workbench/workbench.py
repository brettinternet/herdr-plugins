#!/usr/bin/env python3
"""Machine-facing controller for the brettinternet.workbench Herdr plugin."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
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
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class WorkbenchError(RuntimeError):
    pass


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


def fail(message: str, *, code: str = "workbench_error") -> None:
    print(json.dumps({"ok": False, "error": {"code": code, "message": message}}), file=sys.stderr)
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
        pane_id, server = record.get("paneId"), record.get("server")
        if isinstance(pane_id, str) and isinstance(server, str) and pane_exists(pane_id):
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


def editor_status(_args: argparse.Namespace) -> None:
    context = require_herdr_context()
    workspace_id = str(context.get("workspace_id") or "")
    path = editor_state_path(workspace_id)
    if not path.exists():
        emit({"action": "editor.status", "editor": None})
        return
    record = read_json(path)
    pane_id = record.get("paneId")
    record["running"] = isinstance(pane_id, str) and pane_exists(pane_id)
    emit({"action": "editor.status", "editor": record})


def editor_close(_args: argparse.Namespace) -> None:
    context = require_herdr_context()
    path = editor_state_path(str(context.get("workspace_id") or ""))
    record = read_json(path)
    pane_id = record.get("paneId")
    if not isinstance(pane_id, str):
        raise WorkbenchError("editor state has no pane ID")
    if pane_exists(pane_id):
        close_plugin_pane(pane_id)
    with locked_json(path) as state:
        state["closedAt"] = now()
        state["running"] = False
    emit({"action": "editor.close", "paneId": pane_id})


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
    try:
        if record.get("interactive"):
            return_code = run_interactive_job(command, str(record["cwd"]))
        else:
            return_code = run_captured_job(command, str(record["cwd"]), Path(str(record["logFile"])))
    except KeyboardInterrupt:
        return_code = 130
    except Exception as error:
        failure = str(error)
    with locked_json(path) as state:
        state["exitCode"] = return_code
        state["status"] = (
            "failed"
            if failure
            else "cancelled"
            if return_code in {130, -2}
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


def run_captured_job(command: list[str], cwd: str, log_path: Path) -> int:
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


def run_interactive_job(command: list[str], cwd: str) -> int:
    process = subprocess.Popen(command, cwd=cwd)
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


def job_status(args: argparse.Namespace) -> None:
    path, record = find_job(args.job_id)
    pane_id = record.get("paneId")
    pane_is_live = pane_presence(pane_id) if isinstance(pane_id, str) else False
    if pane_is_live is False and record.get("status") in {"starting", "running", "cancelling"}:
        with locked_json(path) as state:
            if state.get("status") == "cancelling":
                state["status"] = "cancelled"
                state["exitCode"] = 130
            elif state.get("status") in {"starting", "running"}:
                state["status"] = "failed"
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
    pane_id = record.get("paneId")
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
    path, record = find_job(args.job_id)
    if record.get("status") not in {"starting", "running"}:
        raise WorkbenchError(f"job is not running: {args.job_id}")
    pane_id = record.get("paneId")
    if not isinstance(pane_id, str) or not pane_exists(pane_id):
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
    path, record = find_job(args.job_id)
    pane_id = record.get("paneId")
    if not isinstance(pane_id, str):
        raise WorkbenchError("job state has no pane ID")
    if record.get("status") in {"starting", "running", "cancelling"} and not args.force:
        raise WorkbenchError("refusing to close a running job without --force")
    if pane_exists(pane_id):
        if args.force and record.get("status") in {"starting", "running", "cancelling"}:
            herdr("pane", "send-keys", pane_id, "ctrl+c")
        close_plugin_pane(pane_id)
    with locked_json(path) as state:
        state["paneClosedAt"] = now()
    emit({"action": "job.close", "jobId": args.job_id, "paneId": pane_id})


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
    editor_commands.add_parser("close").set_defaults(handler=editor_close)

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
        fail(str(error))


if __name__ == "__main__":
    main()

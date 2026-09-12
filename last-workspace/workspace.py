#!/usr/bin/env python3

import fcntl
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

POLL_INTERVAL_SECONDS = 0.1


def workspace_state() -> tuple[str, set[str]]:
    socket_path = os.environ.get("HERDR_SOCKET_PATH")
    if socket_path:
        payload = json.dumps(
            {"id": "last-workspace", "method": "workspace.list", "params": {}}
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(socket_path)
            client.sendall(payload.encode() + b"\n")
            response = b""
            while b"\n" not in response:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response += chunk
        if not response:
            raise RuntimeError("Herdr returned no workspace list")
        decoded = json.loads(response.splitlines()[0])
        if "error" in decoded:
            raise RuntimeError(decoded["error"])
        workspaces = decoded["result"]["workspaces"]
    else:
        result = subprocess.run(
            [os.environ.get("HERDR_BIN_PATH", "herdr"), "workspace", "list"],
            check=True,
            capture_output=True,
            text=True,
        )
        workspaces = json.loads(result.stdout)["result"]["workspaces"]

    workspace_ids = {item["workspace_id"] for item in workspaces}
    focused = next(
        (item["workspace_id"] for item in workspaces if item.get("focused")), ""
    )
    return focused, workspace_ids


def record(state_dir: Path, workspace_id: str) -> str:
    if not workspace_id:
        return ""

    state_dir.mkdir(parents=True, exist_ok=True)
    current_path = state_dir / "current"
    previous_path = state_dir / "previous"
    current = current_path.read_text().strip() if current_path.exists() else ""
    if current == workspace_id:
        return previous_path.read_text().strip() if previous_path.exists() else ""

    if current:
        previous_path.write_text(f"{current}\n")
    current_path.write_text(f"{workspace_id}\n")
    return current


def refresh(state_dir: Path, focused: str, workspace_ids: set[str]) -> str:
    previous = record(state_dir, focused)
    if previous and previous not in workspace_ids:
        (state_dir / "previous").write_text("")
        return ""
    return previous


def watch(state_dir: Path) -> None:
    token = uuid.uuid4().hex
    watcher_path = state_dir / "watcher"
    lock_path = state_dir / ".lock"
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        watcher_path.write_text(f"{token}\n")

    while True:
        try:
            with lock_path.open("w") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                if (
                    not watcher_path.exists()
                    or watcher_path.read_text().strip() != token
                ):
                    return
                focused, workspace_ids = workspace_state()
                refresh(state_dir, focused, workspace_ids)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return
        time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"record", "toggle", "watch"}:
        raise SystemExit("usage: workspace.py record|toggle|watch")

    state_dir = Path(os.environ["HERDR_PLUGIN_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    if sys.argv[1] == "watch":
        watch(state_dir)
        return

    with (state_dir / ".lock").open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        focused, workspace_ids = workspace_state()
        previous = refresh(state_dir, focused, workspace_ids)
    if sys.argv[1] == "toggle" and previous and previous != focused:
        subprocess.run(
            [os.environ.get("HERDR_BIN_PATH", "herdr"), "workspace", "focus", previous],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if __name__ == "__main__":
    main()

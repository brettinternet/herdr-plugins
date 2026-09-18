#!/usr/bin/env python3
"""Open Worktrunk's picker and focus the selected checkout in Herdr."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def source_checkout(environment: dict[str, str]) -> str | None:
    try:
        context = json.loads(environment.get("HERDR_PLUGIN_CONTEXT_JSON", "{}"))
    except json.JSONDecodeError:
        return None

    worktree = context.get("worktree") or {}
    return worktree.get("checkout_path") or context.get("workspace_cwd")


def main() -> int:
    checkout = source_checkout(dict(os.environ))
    if not checkout:
        print("The selected Herdr workspace is not backed by a Git checkout.")
        input("Press Enter to close…")
        return 1

    try:
        result = subprocess.run(
            [
                os.environ.get("WORKTRUNK_BIN_PATH", "wt"),
                "-C",
                checkout,
                "switch",
                "--no-cd",
                "--execute",
                os.environ.get("HERDR_BIN_PATH", "herdr"),
                "--",
                "worktree",
                "open",
                "--cwd",
                "{{ repo_path }}",
                "--path",
                "{{ worktree_path }}",
                "--label",
                "{{ worktree_name }}",
                "--focus",
            ],
            check=False,
        )
    except FileNotFoundError:
        print("Worktrunk is not installed or is not on PATH.", file=sys.stderr)
        input("Press Enter to close…")
        return 127

    if result.returncode:
        input("Worktree selection failed. Press Enter to close…")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())

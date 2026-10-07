from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


picker = load("worktrunk_picker", "picker.py")
create = load("worktrunk_create", "create.py")


class WorktrunkTest(unittest.TestCase):
    def test_picker_opens_selected_worktree_in_herdr(self) -> None:
        context = '{"worktree":{"checkout_path":"/repo/project"}}'
        environment = {
            "HERDR_PLUGIN_CONTEXT_JSON": context,
            "HERDR_BIN_PATH": "/bin/herdr",
            "WORKTRUNK_BIN_PATH": "/bin/wt",
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(picker.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            self.assertEqual(picker.main(), 0)

        run.assert_called_once_with(
            [
                "/bin/wt",
                "-C",
                "/repo/project",
                "switch",
                "--no-cd",
                "--execute",
                "/bin/herdr",
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

    def test_picker_requires_a_checkout_context(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("builtins.input"),
            mock.patch.object(picker.subprocess, "run") as run,
        ):
            self.assertEqual(picker.main(), 1)

        run.assert_not_called()

    def test_create_uses_selected_checkout_and_requests_hook_focus(self) -> None:
        context = '{"worktree":{"checkout_path":"/repo/project"}}'
        with (
            mock.patch.dict(
                os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": context}, clear=True
            ),
            mock.patch("builtins.input", side_effect=["feature/test", ""]),
            mock.patch.object(create.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            self.assertEqual(create.main(), 0)

        self.assertEqual(
            run.call_args.args[0],
            [
                "wt",
                "-C",
                "/repo/project",
                "switch",
                "--create",
                "feature/test",
                "--base",
                "@",
                "--no-cd",
                "--format=json",
            ],
        )
        self.assertEqual(run.call_args.kwargs["env"]["WORKTRUNK_HERDR_FOCUS"], "1")

    def test_blank_branch_cancels_without_creating(self) -> None:
        context = '{"workspace_cwd":"/repo/project"}'
        with (
            mock.patch.dict(
                os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": context}, clear=True
            ),
            mock.patch("builtins.input", return_value=""),
            mock.patch.object(create.subprocess, "run") as run,
        ):
            self.assertEqual(create.main(), 0)

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

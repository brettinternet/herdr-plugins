import argparse
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).with_name("workbench.py")
spec = importlib.util.spec_from_file_location("workbench", SCRIPT)
workbench = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(workbench)


class WorkbenchTest(unittest.TestCase):
    def context(self):
        return {
            "pane_id": "w1:p1",
            "workspace_id": "w1",
            "cwd": "/repo",
            "foreground_cwd": "/repo",
        }

    def test_state_root_is_shared_between_plugin_and_direct_invocations(self):
        with tempfile.TemporaryDirectory() as temporary:
            xdg = Path(temporary) / "state"
            with mock.patch.dict(
                os.environ,
                {
                    "XDG_STATE_HOME": str(xdg),
                    "HERDR_PLUGIN_STATE_DIR": str(Path(temporary) / "injected"),
                },
                clear=True,
            ):
                self.assertEqual(
                    workbench.state_root(), xdg / "herdr/plugins/brettinternet.workbench"
                )

    def test_default_direction_splits_tall_pane_down(self):
        response = {
            "result": {
                "layout": {
                    "panes": [
                        {"pane_id": "w1:p1", "rect": {"width": 80, "height": 50}}
                    ]
                }
            }
        }
        with mock.patch.object(workbench, "herdr", return_value=response):
            self.assertEqual(workbench.default_direction("w1:p1"), "down")

    def test_placement_args_preserve_focus_by_default(self):
        with mock.patch.object(workbench, "default_direction", return_value="right"):
            result = workbench.placement_args(
                "auto", self.context(), Path("/repo"), False
            )
        self.assertEqual(
            result,
            [
                "--placement",
                "split",
                "--target-pane",
                "w1:p1",
                "--direction",
                "right",
                "--cwd",
                "/repo",
                "--no-focus",
            ],
        )

    def test_plugin_pane_open_passes_request_as_environment(self):
        response = {
            "result": {
                "plugin_pane": {"pane": {"pane_id": "w1:p2"}}
            }
        }
        with mock.patch.object(workbench, "herdr", return_value=response) as herdr:
            result = workbench.plugin_pane_open(
                "job",
                "down",
                self.context(),
                Path("/repo"),
                False,
                {"WORKBENCH_REQUEST": "/state/request.json"},
            )
        self.assertEqual(result["paneId"], "w1:p2")
        self.assertEqual(
            herdr.call_args.args,
            (
                "plugin",
                "pane",
                "open",
                "--plugin",
                "brettinternet.workbench",
                "--entrypoint",
                "job",
                "--placement",
                "split",
                "--target-pane",
                "w1:p1",
                "--direction",
                "down",
                "--cwd",
                "/repo",
                "--no-focus",
                "--env",
                "WORKBENCH_REQUEST=/state/request.json",
            ),
        )

    def test_editor_reuses_live_workspace_editor(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "file.py"
            target.write_text("print('ok')\n")
            state = Path(temporary) / "state"
            editor_state = state / "editors/w1.json"
            editor_state.parent.mkdir(parents=True)
            editor_state.write_text(
                json.dumps(
                    {
                        "kind": "editor",
                        "workspaceId": "w1",
                        "paneId": "w1:p2",
                        "server": "/tmp/nvim.sock",
                    }
                )
            )
            args = argparse.Namespace(
                path=str(target),
                cwd=None,
                line=8,
                column=3,
                placement="right",
                focus=True,
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                mock.patch.object(workbench, "pane_exists", return_value=True),
                mock.patch.object(workbench, "open_in_nvim") as open_file,
                mock.patch.object(workbench, "focus_plugin_pane") as focus,
                mock.patch.object(workbench, "plugin_pane_open") as pane_open,
                mock.patch.object(workbench, "emit"),
            ):
                workbench.editor_open(args)
            open_file.assert_called_once_with("/tmp/nvim.sock", target.resolve(), 8, 3)
            focus.assert_called_once_with("w1:p2")
            pane_open.assert_not_called()

    def test_modified_buffer_query_is_fixed_typed_json_and_normalized(self):
        output = json.dumps(
            [
                {"id": 7, "name": ""},
                {"id": 8, "name": "x" * (workbench.MAX_BUFFER_NAME_LENGTH + 20)},
            ]
        )
        completed = subprocess.CompletedProcess(
            ["nvim"], 0, stdout=output, stderr=""
        )
        with (
            mock.patch.object(workbench, "nvim_binary", return_value="nvim"),
            mock.patch.object(workbench, "run", return_value=completed) as run,
        ):
            buffers = workbench.modified_buffers("/tmp/editor.sock")
        self.assertEqual(
            buffers,
            [
                {"id": 7, "name": workbench.UNNAMED_BUFFER_NAME},
                {"id": 8, "name": "x" * workbench.MAX_BUFFER_NAME_LENGTH},
            ],
        )
        self.assertEqual(
            run.call_args.args[0],
            [
                "nvim",
                "--server",
                "/tmp/editor.sock",
                "--remote-expr",
                workbench.NVIM_MODIFIED_BUFFERS_EXPR,
            ],
        )
        self.assertIn("getbufinfo", workbench.NVIM_MODIFIED_BUFFERS_EXPR)
        self.assertIn("json_encode", workbench.NVIM_MODIFIED_BUFFERS_EXPR)

    def test_modified_buffer_query_bounds_count(self):
        output = json.dumps(
            [{"id": index, "name": str(index)} for index in range(1, 5)]
        )
        completed = subprocess.CompletedProcess(["nvim"], 0, stdout=output, stderr="")
        with (
            mock.patch.object(workbench, "MAX_DIRTY_BUFFERS", 2),
            mock.patch.object(workbench, "run", return_value=completed),
            mock.patch.object(workbench, "nvim_binary", return_value="nvim"),
        ):
            buffers, truncated = workbench._query_modified_buffers("/tmp/editor.sock")
        self.assertEqual(buffers, [{"id": 1, "name": "1"}, {"id": 2, "name": "2"}])
        self.assertTrue(truncated)

    def test_editor_status_reports_bounded_dirty_buffers(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            editor_state = state / "editors/w1.json"
            editor_state.parent.mkdir(parents=True)
            editor_state.write_text(
                json.dumps(
                    {
                        "kind": "editor",
                        "workspaceId": "w1",
                        "paneId": "w1:p2",
                        "server": "/tmp/nvim.sock",
                    }
                )
            )
            dirty = [{"id": 3, "name": workbench.UNNAMED_BUFFER_NAME}]
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                mock.patch.object(workbench, "pane_presence", return_value=True),
                mock.patch.object(workbench, "_query_modified_buffers", return_value=(dirty, False)),
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.editor_status(argparse.Namespace())
            record = emit.call_args.args[0]["editor"]
            self.assertTrue(record["running"])
            self.assertTrue(record["dirty"])
            self.assertEqual(record["dirtyBuffers"], dirty)
            self.assertEqual(record["dirtyBufferCount"], 1)
            self.assertFalse(record["dirtyBuffersTruncated"])

    def test_editor_status_stale_pane_does_not_assert_clean(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            editor_state = state / "editors/w1.json"
            editor_state.parent.mkdir(parents=True)
            editor_state.write_text(
                json.dumps(
                    {
                        "kind": "editor",
                        "workspaceId": "w1",
                        "paneId": "w1:p2",
                        "server": "/tmp/nvim.sock",
                        "dirty": True,
                        "dirtyBuffers": [{"id": 1, "name": "old.py"}],
                    }
                )
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                mock.patch.object(workbench, "pane_presence", return_value=False),
                mock.patch.object(workbench, "_query_modified_buffers") as query,
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.editor_status(argparse.Namespace())
            record = emit.call_args.args[0]["editor"]
            self.assertFalse(record["running"])
            self.assertTrue(record["stale"])
            self.assertIsNone(record["dirty"])
            self.assertIsNone(record["dirtyBuffers"])
            query.assert_not_called()

    def test_editor_close_refuses_named_unnamed_and_multiple_dirty_buffers(self):
        cases = [
            ([{"id": 4, "name": "changed.py"}], False),
            ([{"id": 5, "name": workbench.UNNAMED_BUFFER_NAME}], False),
            (
                [{"id": index, "name": f"changed-{index}.py"} for index in range(1, 4)],
                True,
            ),
        ]
        for dirty, truncated in cases:
            with self.subTest(dirty=dirty, truncated=truncated), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary) / "state"
                editor_state = state / "editors/w1.json"
                editor_state.parent.mkdir(parents=True)
                editor_state.write_text(
                    json.dumps(
                        {
                            "kind": "editor",
                            "workspaceId": "w1",
                            "paneId": "w1:p2",
                            "server": "/tmp/nvim.sock",
                        }
                    )
                )
                with (
                    mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                    mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                    mock.patch.object(workbench, "pane_presence", return_value=True),
                    mock.patch.object(
                        workbench,
                        "_query_modified_buffers",
                        return_value=(dirty, truncated),
                    ),
                    mock.patch.object(workbench, "close_plugin_pane") as close,
                    self.assertRaises(workbench.WorkbenchError) as raised,
                ):
                    workbench.editor_close(argparse.Namespace(force=False))
                self.assertEqual(raised.exception.code, "editor_dirty")
                self.assertEqual(raised.exception.details["dirtyBuffers"], dirty)
                self.assertEqual(
                    raised.exception.details["dirtyBuffersTruncated"], truncated
                )
                close.assert_not_called()

    def test_clean_editor_closes_normally(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            editor_state = state / "editors/w1.json"
            editor_state.parent.mkdir(parents=True)
            editor_state.write_text(json.dumps({
                "kind": "editor",
                "workspaceId": "w1",
                "paneId": "w1:p2",
                "server": "/tmp/nvim.sock",
            }))
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                mock.patch.object(workbench, "pane_presence", return_value=True),
                mock.patch.object(workbench, "_query_modified_buffers", return_value=([], False)),
                mock.patch.object(workbench, "close_plugin_pane") as close,
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.editor_close(argparse.Namespace(force=False))
            close.assert_called_once_with("w1:p2")
            self.assertTrue(emit.call_args.args[0]["closed"])
            self.assertFalse(emit.call_args.args[0]["forced"])

    def test_editor_close_refuses_when_dirty_state_cannot_be_inspected(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            editor_state = state / "editors/w1.json"
            editor_state.parent.mkdir(parents=True)
            editor_state.write_text(json.dumps({
                "kind": "editor",
                "workspaceId": "w1",
                "paneId": "w1:p2",
                "server": "/tmp/stale.sock",
            }))
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                mock.patch.object(workbench, "pane_presence", return_value=True),
                mock.patch.object(
                    workbench,
                    "_query_modified_buffers",
                    side_effect=workbench.WorkbenchError(
                        "server unavailable", code="editor_dirty_unknown"
                    ),
                ),
                mock.patch.object(workbench, "close_plugin_pane") as close,
                self.assertRaises(workbench.WorkbenchError) as raised,
            ):
                workbench.editor_close(argparse.Namespace(force=False))
            self.assertEqual(raised.exception.code, "editor_dirty_unknown")
            self.assertIsNone(raised.exception.details["dirtyBuffers"])
            close.assert_not_called()

    def test_editor_dirty_error_is_machine_readable_from_main(self):
        dirty = [{"id": 4, "name": "changed.py"}]
        with (
            mock.patch.object(
                workbench,
                "editor_close",
                side_effect=workbench.WorkbenchError(
                    "refusing to close editor with modified buffers",
                    code="editor_dirty",
                    details={"dirtyBuffers": dirty},
                ),
            ),
            mock.patch.object(sys, "argv", ["workbench", "editor", "close"]),
            redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(SystemExit) as exited,
        ):
            workbench.main()
        self.assertEqual(exited.exception.code, 1)
        payload = json.loads(errors.getvalue())
        self.assertEqual(payload["error"]["code"], "editor_dirty")
        self.assertEqual(payload["error"]["dirtyBuffers"], dirty)

    def test_editor_force_close_is_plugin_scoped_and_idempotent_for_stale_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            editor_state = state / "editors/w1.json"
            editor_state.parent.mkdir(parents=True)
            editor_state.write_text(
                json.dumps(
                    {
                        "kind": "editor",
                        "workspaceId": "w1",
                        "paneId": "w1:p2",
                        "server": "/tmp/nvim.sock",
                    }
                )
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                mock.patch.object(workbench, "pane_presence", return_value=True),
                mock.patch.object(workbench, "close_plugin_pane") as close,
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.editor_close(argparse.Namespace(force=True))
            close.assert_called_once_with("w1:p2")
            self.assertTrue(emit.call_args.args[0]["closed"])

            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                mock.patch.object(workbench, "pane_presence", return_value=False),
                mock.patch.object(workbench, "close_plugin_pane") as close_again,
                mock.patch.object(workbench, "emit") as emit_again,
            ):
                workbench.editor_close(argparse.Namespace(force=True))
            close_again.assert_not_called()
            self.assertTrue(emit_again.call_args.args[0]["alreadyClosed"])

    def test_editor_close_missing_state_is_idempotent_noop(self):
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(Path(temporary) / "state")}),
                mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.editor_close(argparse.Namespace(force=False))
            self.assertEqual(emit.call_args.args[0], {
                "action": "editor.close",
                "paneId": None,
                "closed": False,
            })

    def test_concurrent_editor_opens_create_one_workspace_pane(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "file.py"
            target.touch()
            state = Path(temporary) / "state"
            barrier = threading.Barrier(2)
            args = argparse.Namespace(
                path=str(target), cwd=None, line=1, column=1, placement="right", focus=False
            )

            def calling_context():
                barrier.wait()
                return self.context()

            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "require_herdr_context", side_effect=calling_context),
                mock.patch.object(workbench, "plugin_pane_open", return_value={"paneId": "w1:p2"}) as pane_open,
                mock.patch.object(workbench, "wait_for_nvim", side_effect=lambda _server: time.sleep(0.05)),
                mock.patch.object(workbench, "pane_exists", return_value=True),
                mock.patch.object(workbench, "open_in_nvim"),
                mock.patch.object(workbench, "emit"),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                list(executor.map(workbench.editor_open, [args, args]))
            pane_open.assert_called_once()

    def test_editor_closes_new_pane_when_nvim_does_not_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "file.py"
            target.touch()
            state = Path(temporary) / "state"
            args = argparse.Namespace(
                path=str(target),
                cwd=None,
                line=1,
                column=1,
                placement="right",
                focus=False,
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                mock.patch.object(workbench, "plugin_pane_open", return_value={"paneId": "w1:p2"}),
                mock.patch.object(workbench, "wait_for_nvim", side_effect=workbench.WorkbenchError("timeout")),
                mock.patch.object(workbench, "close_plugin_pane") as close,
                self.assertRaises(workbench.WorkbenchError),
            ):
                workbench.editor_open(args)
            close.assert_called_once_with("w1:p2")

    def test_job_start_strips_argument_separator_and_records_owned_pane(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            args = argparse.Namespace(
                command=["--", "python3", "-c", "print('ok')"],
                cwd=temporary,
                placement="down",
                focus=False,
                interactive=False,
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
                mock.patch.object(workbench, "plugin_pane_open", return_value={"paneId": "w1:p3"}),
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.job_start(args)
            record = emit.call_args.args[0]["job"]
            self.assertEqual(record["command"], ["python3", "-c", "print('ok')"])
            self.assertEqual(record["paneId"], "w1:p3")
            self.assertEqual(record["status"], "starting")

    def test_new_editor_applies_initial_column(self):
        with tempfile.TemporaryDirectory() as temporary:
            request = Path(temporary) / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "server": str(Path(temporary) / "nvim.sock"),
                        "cwd": temporary,
                        "path": str(Path(temporary) / "file.py"),
                        "line": 7,
                        "column": 4,
                    }
                )
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_REQUEST": str(request)}, clear=True),
                mock.patch.object(workbench, "nvim_binary", return_value="nvim"),
                mock.patch.object(workbench.os, "execvp") as execute,
            ):
                workbench.internal_editor()
            execute.assert_called_once_with(
                "nvim",
                [
                    "nvim",
                    "--listen",
                    str(Path(temporary) / "nvim.sock"),
                    "+call cursor(7,4)",
                    str(Path(temporary) / "file.py"),
                ],
            )

    def test_stop_process_kills_a_sigterm_ignoring_process_group(self):
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)",
            ],
            start_new_session=True,
        )
        time.sleep(0.1)
        workbench.stop_process(process, process_group=True)
        self.assertIsNotNone(process.poll())

    def test_internal_job_records_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "job-1.json"
            state.write_text(
                json.dumps(
                    {
                        "command": ["test-command", "arg"],
                        "cwd": temporary,
                        "status": "starting",
                        "interactive": True,
                    }
                )
            )
            request = Path(temporary) / "request.json"
            request.write_text(json.dumps({"stateFile": str(state)}))
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "WORKBENCH_REQUEST": str(request),
                        "HERDR_PANE_ID": "w1:p4",
                    },
                    clear=True,
                ),
                mock.patch.object(workbench, "run_interactive_job", return_value=7) as call,
                mock.patch.object(workbench, "hold_job_pane") as hold,
            ):
                workbench.internal_job()
            hold.assert_called_once_with()
            record = json.loads(state.read_text())
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["exitCode"], 7)
            self.assertEqual(record["paneId"], "w1:p4")
            call.assert_called_once_with(["test-command", "arg"], temporary)

    def test_captured_job_mirrors_output_to_a_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "job.log"
            with mock.patch.object(workbench, "mirror_output") as output:
                status = workbench.run_captured_job(
                    [sys.executable, "-c", "print('captured output')"], temporary, log
                )
            self.assertEqual(status, 0)
            self.assertEqual(log.read_text(), "captured output\n")
            output.assert_called_once_with(b"captured output\n")

    def test_captured_job_rotates_bounded_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "job.log"
            with (
                mock.patch.object(workbench, "MAX_LOG_BYTES", 10),
                mock.patch.object(workbench, "READ_CHUNK_BYTES", 8),
                mock.patch.object(workbench, "mirror_output"),
            ):
                status = workbench.run_captured_job(
                    [sys.executable, "-c", "import sys; sys.stdout.write('x' * 30)"],
                    temporary,
                    log,
                )
            self.assertEqual(status, 0)
            self.assertLessEqual(log.stat().st_size, 10)
            self.assertLessEqual(log.with_suffix(".log.1").stat().st_size, 10)

    def test_captured_job_exposes_flushed_output_without_a_newline(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "job.log"
            thread = threading.Thread(
                target=workbench.run_captured_job,
                args=(
                    [
                        sys.executable,
                        "-c",
                        "import sys,time; sys.stdout.write('ready'); sys.stdout.flush(); time.sleep(.4)",
                    ],
                    temporary,
                    log,
                ),
            )
            with mock.patch.object(workbench, "mirror_output"):
                thread.start()
                deadline = time.monotonic() + 1
                while (not log.exists() or log.read_bytes() != b"ready") and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(log.read_bytes(), b"ready")
                self.assertTrue(thread.is_alive())
                thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_tail_log_joins_a_line_across_rotation_and_waits_for_rotation(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "job.log"
            rotated = log.with_suffix(".log.1")
            lock = log.with_suffix(".log.lock")
            log.write_bytes(b"line-prefix-")
            result = []
            with workbench.file_lock(lock):
                reader = threading.Thread(target=lambda: result.append(workbench.tail_log(log, 1)))
                reader.start()
                time.sleep(0.05)
                self.assertTrue(reader.is_alive())
                os.replace(log, rotated)
                log.write_bytes(b"line-suffix\n")
            reader.join(timeout=1)
            self.assertEqual(result, ["line-prefix-line-suffix\n"])

    def test_tail_log_reads_only_the_requested_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "job.log"
            log.write_bytes((b"old line\n" * 10000) + b"last line\n")
            with mock.patch.object(Path, "read_text", side_effect=AssertionError("unbounded read")):
                self.assertEqual(workbench.tail_log(log, 1), "last line\n")

    def test_job_read_uses_log_after_pane_is_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            jobs = state / "jobs"
            jobs.mkdir(parents=True)
            log = jobs / "job-abc.log"
            log.write_text("first\nsecond\nthird\n")
            (jobs / "job-abc.json").write_text(
                json.dumps(
                    {
                        "jobId": "job-abc",
                        "paneId": "w1:p5",
                        "status": "completed",
                        "logFile": str(log),
                    }
                )
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "pane_exists", return_value=False),
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.job_read(argparse.Namespace(job_id="job-abc", lines=2))
            result = emit.call_args.args[0]
            self.assertEqual(result["source"], "log")
            self.assertEqual(result["output"], "second\nthird\n")

    def test_job_read_returns_plain_pane_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            jobs = state / "jobs"
            jobs.mkdir(parents=True)
            (jobs / "job-abc.json").write_text(
                json.dumps({"jobId": "job-abc", "paneId": "w1:p5", "status": "completed"})
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "pane_exists", return_value=True),
                mock.patch.object(workbench, "herdr_text", return_value="tests passed\n"),
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.job_read(argparse.Namespace(job_id="job-abc", lines=40))
            self.assertEqual(emit.call_args.args[0]["output"], "tests passed\n")

    def test_job_cancel_only_targets_a_recorded_running_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            jobs = state / "jobs"
            jobs.mkdir(parents=True)
            path = jobs / "job-abc.json"
            path.write_text(
                json.dumps(
                    {"jobId": "job-abc", "paneId": "w1:p5", "status": "running"}
                )
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "pane_exists", return_value=True),
                mock.patch.object(workbench, "herdr") as herdr,
                mock.patch.object(workbench, "emit"),
            ):
                workbench.job_cancel(argparse.Namespace(job_id="job-abc"))
            herdr.assert_called_once_with("pane", "send-keys", "w1:p5", "ctrl+c")
            self.assertEqual(json.loads(path.read_text())["status"], "cancelling")

    def test_job_status_reconciles_cancelled_pane_that_exited_during_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            jobs = state / "jobs"
            jobs.mkdir(parents=True)
            path = jobs / "job-abc.json"
            path.write_text(
                json.dumps({"jobId": "job-abc", "paneId": "w1:p5", "status": "cancelling"})
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "pane_presence", return_value=False),
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.job_status(argparse.Namespace(job_id="job-abc"))
            record = emit.call_args.args[0]["job"]
            self.assertEqual(record["status"], "cancelled")
            self.assertEqual(record["exitCode"], 130)

    def test_job_status_does_not_fail_live_job_when_pane_presence_is_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            jobs = state / "jobs"
            jobs.mkdir(parents=True)
            path = jobs / "job-abc.json"
            path.write_text(
                json.dumps({"jobId": "job-abc", "paneId": "w1:p5", "status": "running"})
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "pane_presence", return_value=None),
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.job_status(argparse.Namespace(job_id="job-abc"))
                self.assertEqual(emit.call_args.args[0]["job"]["status"], "running")
                self.assertIsNone(emit.call_args.args[0]["job"]["paneExists"])
                with self.assertRaisesRegex(workbench.WorkbenchError, "refusing to close"):
                    workbench.job_close(argparse.Namespace(job_id="job-abc", force=False))

    def test_job_cancel_does_not_overwrite_terminal_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            jobs = state / "jobs"
            jobs.mkdir(parents=True)
            path = jobs / "job-abc.json"
            path.write_text(
                json.dumps({"jobId": "job-abc", "paneId": "w1:p5", "status": "running"})
            )

            def finish_before_send_returns(*_args):
                with workbench.locked_json(path) as record:
                    record["status"] = "cancelled"
                    record["exitCode"] = 130
                return {"result": {}}

            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "pane_exists", return_value=True),
                mock.patch.object(workbench, "herdr", side_effect=finish_before_send_returns),
                mock.patch.object(workbench, "emit"),
            ):
                workbench.job_cancel(argparse.Namespace(job_id="job-abc"))
            self.assertEqual(json.loads(path.read_text())["status"], "cancelled")

    def test_job_close_force_waits_for_terminal_state_before_closing(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            jobs = state / "jobs"
            jobs.mkdir(parents=True)
            path = jobs / "job-abc.json"
            path.write_text(
                json.dumps(
                    {
                        "kind": "job",
                        "jobId": "job-abc",
                        "workspaceId": "w1",
                        "paneId": "w1:p5",
                        "status": "running",
                    }
                )
            )

            def finish_cancellation(job_path, _pane_id):
                with workbench.locked_json(job_path) as record:
                    record["status"] = "cancelled"
                    record["exitCode"] = 130
                    record["finishedAt"] = workbench.now()
                return json.loads(job_path.read_text())

            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "pane_presence", side_effect=[True, True]),
                mock.patch.object(workbench, "herdr") as herdr,
                mock.patch.object(workbench, "_wait_for_job_terminal", side_effect=finish_cancellation),
                mock.patch.object(workbench, "close_plugin_pane") as close,
                mock.patch.object(workbench, "emit") as emit,
            ):
                workbench.job_close(argparse.Namespace(job_id="job-abc", force=True))
            herdr.assert_called_once_with("pane", "send-keys", "w1:p5", "ctrl+c")
            close.assert_called_once_with("w1:p5")
            self.assertEqual(json.loads(path.read_text())["status"], "cancelled")
            self.assertTrue(emit.call_args.args[0]["forced"])

    def test_job_close_force_reconciles_gone_pane_without_closing_another_pane(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            jobs = state / "jobs"
            jobs.mkdir(parents=True)
            path = jobs / "job-abc.json"
            path.write_text(
                json.dumps(
                    {
                        "kind": "job",
                        "jobId": "job-abc",
                        "workspaceId": "w1",
                        "paneId": "w1:p5",
                        "status": "cancelling",
                    }
                )
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "pane_presence", return_value=False),
                mock.patch.object(workbench, "herdr") as herdr,
                mock.patch.object(workbench, "close_plugin_pane") as close,
                mock.patch.object(workbench, "emit"),
            ):
                workbench.job_close(argparse.Namespace(job_id="job-abc", force=True))
            herdr.assert_not_called()
            close.assert_not_called()
            self.assertEqual(json.loads(path.read_text())["status"], "cancelled")

    def test_job_close_rejects_foreign_recorded_workspace_pane(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            jobs = state / "jobs"
            jobs.mkdir(parents=True)
            (jobs / "job-abc.json").write_text(
                json.dumps(
                    {
                        "kind": "job",
                        "jobId": "job-abc",
                        "workspaceId": "w1",
                        "paneId": "w2:p5",
                        "status": "completed",
                    }
                )
            )
            with (
                mock.patch.dict(os.environ, {"WORKBENCH_STATE_DIR": str(state)}),
                mock.patch.object(workbench, "pane_presence") as presence,
                mock.patch.object(workbench, "close_plugin_pane") as close,
                self.assertRaisesRegex(workbench.WorkbenchError, "another Herdr workspace"),
            ):
                workbench.job_close(argparse.Namespace(job_id="job-abc", force=False))
            presence.assert_not_called()
            close.assert_not_called()

    def test_parser_errors_are_machine_readable(self):
        errors = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["workbench", "job", "read", "job-x", "--lines", "nope"]),
            redirect_stderr(errors),
            self.assertRaises(SystemExit) as exited,
        ):
            workbench.main()
        self.assertEqual(exited.exception.code, 1)
        payload = json.loads(errors.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "workbench_error")

    def test_action_editor_uses_calling_pane_cwd(self):
        with (
            mock.patch.object(workbench, "require_herdr_context", return_value=self.context()),
            mock.patch.object(workbench, "editor_open") as editor_open,
        ):
            workbench.action_editor()
        args = editor_open.call_args.args[0]
        self.assertEqual(args.path, "/repo")
        self.assertEqual(args.cwd, "/repo")
        self.assertTrue(args.focus)

    def test_wait_for_nvim_retries_until_server_responds(self):
        failed = subprocess.CalledProcessError(1, ["nvim"])
        with (
            mock.patch.object(workbench, "nvim_binary", return_value="nvim"),
            mock.patch.object(workbench, "run", side_effect=[failed, mock.Mock()]) as run,
            mock.patch.object(workbench.time, "sleep"),
        ):
            workbench.wait_for_nvim("/tmp/test.sock")
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()

"""A failed viewport transport must not permanently disable its target."""
import copy
import errno
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import ccc_observation as health
import cmux_codex_watch as watch
from tests.test_watch import FakeClient, armed_daemon, grid_payload


TRANSPORT_ERRORS = (
    "cmux read-screen --workspace failed: Error: Failed to write to socket (Broken pipe, errno 32)",
    "cmux --json rpc failed: Error: Failed to write to socket (Broken pipe, errno 32)",
    "cmux --json rpc failed: Error: No live cmux socket found. Tried:\n  /tmp/cmux.sock",
    "cmux read-screen --workspace failed: Error: Connection reset by peer",
    "cmux --json rpc failed: Error: Socket closed before reply",
    "cmux read-screen --workspace failed: Error: Connection closed before reply",
)


class ObservationTransportTests(unittest.TestCase):
    def test_broken_pipe_does_not_persist_a_pause(self):
        self.check_retry(TRANSPORT_ERRORS[0], "read_screen", False)

    def check_retry(self, detail, operation, after_refresh):
        client = FakeClient(grid_payload(["previous output"], error="http_405"),
                            "■ unexpected status 405 Method Not Allowed")
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, client)
            self.addCleanup(daemon._process_snapshots.close)
            config_path = Path(directory) / "config.json"
            before = config_path.read_bytes()
            runtime = watch.TargetRuntime(
                episode_id="keep-episode", send_count=8, last_send_at=123,
                awaiting=True, delivery_status="unknown", send_started_at=123,
                claude_completed_latched=True, claude_session_id="keep-session",
            )
            daemon.runtime["surface-uuid"] = runtime
            reads = 0

            def fail(*args):
                nonlocal reads
                reads += 1
                if after_refresh and reads == 1:
                    raise watch.CmuxError("workspace moved")
                raise watch.CmuxError(detail)

            with mock.patch.object(client, operation, side_effect=fail), \
                    mock.patch.object(daemon, "_refresh_workspace", return_value=True):
                daemon.process_once(client)

            self.assertEqual(config_path.read_bytes(), before)
            self.assertFalse(daemon.config["targets"][0]["paused"])
            self.assertEqual(runtime.state, "cmux_unavailable")
            self.assertEqual((runtime.episode_id, runtime.send_count, runtime.last_send_at),
                             ("keep-episode", 8, 123))
            self.assertEqual(runtime.delivery_status, "unknown")
            self.assertTrue(runtime.awaiting)
            self.assertTrue(runtime.claude_completed_latched)
            self.assertEqual(runtime.claude_session_id, "keep-session")
            self.assertEqual(client.sent, [])
            saved = json.loads((Path(directory) / "state.json").read_text())["surface-uuid"]
            self.assertEqual(saved["delivery_status"], "unknown")

            client.text = "Working (0s • esc to interrupt)"
            with mock.patch.object(client, "read_screen", return_value=client.text) as read:
                daemon.process_once(client)
                read.assert_called_once()
            self.assertEqual(client.sent, [])

    def test_transport_failures_keep_initial_and_refreshed_reads_monitored(self):
        for detail in TRANSPORT_ERRORS:
            for operation in ("read_screen", "replay"):
                for after_refresh in (False, True):
                    with self.subTest(detail=detail, operation=operation, after_refresh=after_refresh):
                        self.check_retry(detail, operation, after_refresh)

    def test_transport_or_timeout_does_not_start_a_second_read(self):
        for failure in (
            subprocess.CompletedProcess([], 1, "", TRANSPORT_ERRORS[0]),
            subprocess.CompletedProcess([], 1, "", TRANSPORT_ERRORS[2]),
            subprocess.TimeoutExpired(["cmux", "read-screen"], 8),
            BrokenPipeError(errno.EPIPE, "Broken pipe"),
        ):
            with self.subTest(failure=failure):
                runner = mock.Mock()
                if isinstance(failure, BaseException):
                    runner.side_effect = failure
                else:
                    runner.return_value = failure
                client = watch.CmuxClient(runner=runner)
                with mock.patch.object(client, "replay") as replay:
                    with self.assertRaises(watch.CmuxError):
                        client.read_screen("workspace-uuid", "surface-uuid")
                runner.assert_called_once()
                replay.assert_not_called()

    def test_wrapped_connection_errors_are_retryable_but_identity_errors_are_not(self):
        for cause in (BrokenPipeError(errno.EPIPE, "pipe closed"),
                      ConnectionResetError(errno.ECONNRESET, "peer reset"),
                      ConnectionRefusedError(errno.ECONNREFUSED, "connection refused"),
                      ConnectionAbortedError(errno.ECONNABORTED, "connection aborted"),
                      OSError(errno.ENOTCONN, "socket disconnected"),
                      TimeoutError("deadline exceeded")):
            with self.subTest(cause=cause):
                exc = watch.CmuxError("cmux transport failed")
                exc.__cause__ = cause
                self.assertTrue(watch.WatchDaemon._is_transient_observation_error(exc))
                for identity in ("surface not found", "surface_not_found", "not a terminal",
                                 "invalid_params", "workspace identity mismatch"):
                    blocked = watch.CmuxError(identity + ": Broken pipe")
                    blocked.__cause__ = cause
                    self.assertFalse(watch.WatchDaemon._is_transient_observation_error(blocked))
        missing_binary = watch.CmuxError("cmux executable missing")
        missing_binary.__cause__ = FileNotFoundError(errno.ENOENT, "No such file")
        self.assertFalse(watch.WatchDaemon._is_transient_observation_error(missing_binary))

    def test_transport_classification_never_retries_terminal_input(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 1, "", TRANSPORT_ERRORS[0]))
        client = watch.CmuxClient(runner=runner)
        with self.assertRaises(watch.CmuxError):
            client.send("workspace-uuid", "surface-uuid", "continue")
        runner.assert_called_once()


class PausedTransportHealthTests(unittest.TestCase):
    def test_automatic_transport_pause_cannot_make_health_green(self):
        for detail in (*TRANSPORT_ERRORS, "Command terminal.replay timed out after 12 seconds"):
            with self.subTest(detail=detail):
                target = dict(surface_id="s", workspace_id="w", enabled=True,
                              paused=True, paused_reason=detail, pause_origin="automatic_observation_error")
                before = copy.deepcopy(target)
                report = health.continuation_report([target], {}, now=100)
                self.assertEqual(report["status"], "degraded")
                self.assertEqual(report["counts"]["unavailable"], 1)
                self.assertEqual(report["targets"][0]["reason_code"], "paused_after_observation_error")
                self.assertEqual(target, before)  # Projection cannot resume the target.

    def test_manual_disabled_and_identity_pauses_keep_their_protection(self):
        for detail, enabled in (("manual pause", True), ("", True),
                                ("invalid_params: Surface is not a terminal", True),
                                ("surface not found", True), (TRANSPORT_ERRORS[0], False)):
            with self.subTest(detail=detail, enabled=enabled):
                target = dict(surface_id="s", workspace_id="w", enabled=enabled,
                              paused=True, paused_reason=detail)
                row = health.continuation_row(target, {}, now=100)
                self.assertEqual(row["status"], "paused")
                self.assertEqual(row["reason_code"], "explicitly_paused_or_disabled")


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from tests.test_codex_status_chrome import status_payload, visible_text, ERRORS
from tests.test_watch import FakeClient, armed_daemon


class CodexTurnGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        payload = status_payload("high_demand")
        self.client = FakeClient(payload, visible_text(payload))
        self.daemon = armed_daemon(self.tmp.name, self.client)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.finished = {"kind": "task_complete", "session_id": "original",
                         "turn_id": "one", "at": 100,
                         "error": {"message": ERRORS["high_demand"]}}
        self.turn = Mock(return_value=self.finished)
        self.daemon.codex_queue_recovery.current_turn = self.turn

    def test_error_display_before_native_task_end_never_submits(self):
        for kind in ("task_started", "user_message", "unknown", "turn_aborted"):
            with self.subTest(kind=kind):
                self.turn.return_value = {"kind": kind}
                self.daemon.process_once(self.client)
                self.assertEqual(self.client.sent, [])
        self.turn.return_value = self.finished
        self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)

    def test_new_user_turn_between_persistence_and_io_cancels_send(self):
        self.turn.side_effect = [self.finished, {"kind": "task_started"}]
        self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.daemon.runtime["surface-uuid"].delivery_status, "cancelled")

    def test_binding_disappearing_before_io_cancels_send(self):
        self.turn.side_effect = [self.finished, None]
        self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.daemon.runtime["surface-uuid"].delivery_status, "cancelled")

    def test_missing_hook_and_pending_process_snapshot_do_not_bypass_native_gate(self):
        # Exercise the real QueueRecovery wiring, including the advisory
        # nonblocking process lookup used by the production daemon.
        recovery = self.daemon.codex_queue_recovery
        del recovery.current_turn
        recovery.sessions_root.mkdir(parents=True)
        with patch.object(self.daemon, "_candidate_process_label", return_value={
                "agent_kind": "unknown", "summary": "process refresh pending"}):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.daemon.runtime["surface-uuid"].state, "awaiting_transition")

    def test_success_and_different_native_failure_do_not_revive_old_banner(self):
        for error in (None, {"message": "Permission denied"}, {"message": ERRORS["rate_limit"]}):
            with self.subTest(error=error):
                self.turn.return_value = {**self.finished, "error": error}
                self.daemon.process_once(self.client)
                self.assertEqual(self.client.sent, [])

    def test_one_submission_per_failed_turn_even_if_banner_changes(self):
        self.daemon.process_once(self.client)
        runtime = self.daemon.runtime["surface-uuid"]
        for _ in range(3):
            runtime.awaiting = False
            runtime.last_send_at = 0
            self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)
        self.turn.return_value = {**self.finished, "turn_id": "two", "at": 200}
        self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 2)

    def test_native_turn_deduplication_survives_restart(self):
        self.daemon.process_once(self.client)
        restarted = armed_daemon(self.tmp.name, self.client)
        self.addCleanup(restarted._process_snapshots.close)
        restarted.codex_queue_recovery.current_turn = self.turn
        restarted.runtime["surface-uuid"].awaiting = False
        restarted.runtime["surface-uuid"].last_send_at = 0
        restarted.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)

    def test_timed_out_unsent_prompt_recovers_only_after_stable_native_evidence(self):
        self.daemon.process_once(self.client)
        runtime = self.daemon.runtime["surface-uuid"]
        runtime.delivery_status = "unknown"
        runtime.send_started_at = 150
        runtime.send_completed_at = 158
        runtime.last_send_at = 150
        self.turn.return_value = {**self.finished, "signature": [1, 50, 100]}
        with patch("cmux_codex_watch.time.time", return_value=161):
            self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)
        with patch("cmux_codex_watch.time.time", return_value=162.1):
            self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 2)

    def test_unknown_send_is_not_retried_after_native_progress(self):
        self.daemon.process_once(self.client)
        runtime = self.daemon.runtime["surface-uuid"]
        runtime.delivery_status = "unknown"
        runtime.send_started_at, runtime.send_completed_at = 150, 158
        runtime.last_send_at = 150
        self.turn.return_value = {"kind": "user_message", "at": 159, "signature": [1, 55, 159]}
        for now in (161, 165, 170):
            with patch("cmux_codex_watch.time.time", return_value=now):
                self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)


if __name__ == "__main__":
    unittest.main()

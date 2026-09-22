import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ccc_codex_queue import QueueRecovery, completed_error


class QueueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ledger = self.root / "queue.json"
        self.recovery = QueueRecovery(self.ledger, self.root / "bindings", self.root, "任务请继续")
        self.evidence = {"session_id": "original", "pid": 123, "process_start": 1,
                         "completed_at": 100, "signature": [1, 2, 3]}
        self.recovery.evidence = Mock(return_value=self.evidence)
        self.target = {"surface_id": "s", "workspace_id": "w", "enabled": True}
        self.runtime = SimpleNamespace(delivery_status="confirmed", send_count=1)
        self.empty = {"empty": True, "busy": False, "editable": True, "queued": ["任务请继续"]}
        self.draft = {"empty": False, "busy": False, "queued": [], "draft": "任务请继续"}
        self.edit, self.enter = Mock(), Mock()

    def run_recovery(self, views=None, authorized=None):
        with patch("ccc_codex_queue.time.sleep"):
            return self.recovery.recover(self.target, self.runtime,
                read_view=Mock(side_effect=views or [self.empty, self.draft]),
                edit_queued=self.edit, enter=self.enter,
                authorized=authorized or Mock(return_value=True))

    def test_reuses_one_message_and_persists_before_each_key(self):
        phases = []
        self.edit.side_effect = lambda: phases.append(next(iter(json.loads(self.ledger.read_text()).values()))["phase"])
        self.enter.side_effect = lambda: phases.append(next(iter(json.loads(self.ledger.read_text()).values()))["phase"])
        self.assertEqual(self.run_recovery(), "queue_recovery_submitted")
        self.assertEqual(phases, ["editing", "submitting"])
        restarted = QueueRecovery(self.ledger, self.root / "bindings", self.root, "任务请继续")
        restarted.evidence = Mock(return_value=self.evidence)
        self.recovery = restarted
        self.assertEqual(self.run_recovery(), "")
        self.assertEqual(self.edit.call_count, 1)
        self.assertEqual(self.enter.call_count, 1)

    def test_busy_draft_foreign_and_multiple_queued_messages_are_protected(self):
        for view in [{**self.empty, "busy": True}, {**self.empty, "empty": False},
                     {**self.empty, "editable": False}, {**self.empty, "queued": ["user instruction"]},
                     {**self.empty, "queued": ["任务请继续", "任务请继续"]}]:
            with self.subTest(view=view):
                self.recovery.next_probe.clear()
                self.assertEqual(self.run_recovery([view]), "")
                self.edit.assert_not_called()

    def test_unknown_delivery_cannot_be_overridden(self):
        self.runtime.delivery_status = "unknown"
        self.assertEqual(self.run_recovery(), "")
        self.edit.assert_not_called()

    def test_unproven_original_session_cannot_be_recovered(self):
        self.recovery.evidence.return_value = None
        self.assertEqual(self.run_recovery(), "")
        self.edit.assert_not_called()

    def test_revoked_authorization_before_edit(self):
        self.assertEqual(self.run_recovery(authorized=Mock(return_value=False)), "")
        self.edit.assert_not_called()

    def test_revoked_authorization_after_edit_preserves_the_draft(self):
        self.assertEqual(self.run_recovery(authorized=Mock(side_effect=[True, False])), "queue_recovery_unconfirmed")
        self.edit.assert_called_once()
        self.enter.assert_not_called()

    def test_changed_process_or_turn_before_edit(self):
        self.recovery.evidence.side_effect = [self.evidence, {**self.evidence, "pid": 456}]
        self.assertEqual(self.run_recovery(), "")
        self.edit.assert_not_called()

    def test_changed_draft_blocks_enter(self):
        self.assertEqual(self.run_recovery([self.empty, {**self.draft, "draft": "user draft"}]), "queue_recovery_unconfirmed")
        self.enter.assert_not_called()

    def test_lost_edit_acknowledgement_is_never_retried(self):
        self.edit.side_effect = TimeoutError()
        self.assertEqual(self.run_recovery(), "queue_recovery_unconfirmed")
        self.recovery.next_probe.clear()
        self.assertEqual(self.run_recovery(), "queue_recovery_unconfirmed")
        self.assertEqual(self.edit.call_count, 1)
        self.enter.assert_not_called()

    def test_lost_enter_acknowledgement_is_never_retried(self):
        self.enter.side_effect = TimeoutError()
        self.assertEqual(self.run_recovery(), "queue_recovery_unconfirmed")
        self.recovery.next_probe.clear()
        self.run_recovery()
        self.assertEqual(self.enter.call_count, 1)

    def test_corrupt_ledger_cannot_be_reset(self):
        self.ledger.write_text("{broken")
        self.recovery = QueueRecovery(self.ledger, self.root / "bindings", self.root, "任务请继续")
        self.assertEqual(self.run_recovery(), "")
        self.edit.assert_not_called()

    def test_transcript_requires_latest_turn_completed_with_retryable_error(self):
        now = time.time()
        path = self.root / "session.jsonl"
        meta = {"type": "session_meta", "payload": {"id": "original"}}
        finished = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10)),
                    "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "old",
                    "error": {"message": "We're currently experiencing high demand, which may cause temporary errors."}}}
        path.write_text("\n".join(json.dumps(x) for x in [meta, finished]) + "\n")
        self.assertIsNotNone(completed_error(path, "original", now))
        self.assertIsNone(completed_error(path, "replacement", now))
        with path.open("a") as h:
            h.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        self.assertIsNone(completed_error(path, "original", now))

    def test_transport_failure_eligibility_is_shared_by_queue_and_transcript_checks(self):
        now = time.time()
        path = self.root / "session.jsonl"
        recovery = QueueRecovery(self.ledger, self.root / "bindings", self.root, "任务请继续")
        for message, allowed in (("Connection failed: error sending request", True),
                                 ("stream disconnected before completion", True),
                                 ("Connection failed: permission denied", False),
                                 ("Example: Connection failed: error sending request", False)):
            with self.subTest(message=message):
                turn = {"kind": "task_complete", "at": now - 10, "turn_id": "original-turn",
                        "error": {"message": message}, "session_id": "original", "pid": 123,
                        "process_start": 1, "signature": [1, 2, 3]}
                recovery.current_turn = Mock(return_value=turn)
                finished = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10)),
                            "type": "event_msg", "payload": {"type": "task_complete", "error": turn["error"]}}
                path.write_text("\n".join(json.dumps(x) for x in [
                    {"type": "session_meta", "payload": {"id": "original"}}, finished]) + "\n")
                self.assertEqual(recovery.evidence(self.target) is not None, allowed)
                self.assertEqual(completed_error(path, "original", now) is not None, allowed)


if __name__ == "__main__":
    unittest.main()

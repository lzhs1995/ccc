"""Per-attempt queue text cannot leak between ordinary and b continuations."""
from concurrent.futures import ThreadPoolExecutor
import copy
import unittest
from unittest.mock import Mock, patch

import ccc_workspace_batch as batch
from ccc_codex_queue import QueueRecovery
from tests import test_codex_queue_recovery as fixtures


class PrivateCheckQueueTests(unittest.TestCase):
    setUp = fixtures.QueueRecoveryTests.setUp

    def recover(self, message, *, target=None, views=None, edit=None, enter=None):
        return self.recovery.recover(target or self.target, self.runtime,
            read_view=Mock(side_effect=views or [
                {**self.empty, "queued": [message]}, {**self.draft, "draft": message}]),
            edit_queued=edit or self.edit, enter=enter or self.enter,
            authorized=lambda: True, message=message)

    def test_short_check_and_default_use_their_own_text_concurrently(self):
        with patch("ccc_codex_queue.time.sleep"), ThreadPoolExecutor(max_workers=2) as pool:
            short = pool.submit(self.recover, batch.PROMPT)
            normal = pool.submit(self.recover, "任务请继续", target={**self.target, "surface_id": "other"},
                                 edit=Mock(), enter=Mock())
            self.assertEqual(short.result(), "queue_recovery_submitted")
            self.assertEqual(normal.result(), "queue_recovery_submitted")
        records = {r["surface_id"]: r for r in self.recovery.attempts.values()}
        self.assertEqual(records["s"]["message"], batch.PROMPT)
        self.assertEqual(records["other"]["message"], "任务请继续")
        self.assertEqual(self.recovery.message, "任务请继续")

    def test_existing_generic_queue_is_never_rewritten_as_short_check(self):
        with patch("ccc_codex_queue.time.sleep"):
            self.assertEqual(self.recover(batch.PROMPT, views=[self.empty]), "")
        self.edit.assert_not_called()
        self.enter.assert_not_called()

    def test_existing_generic_draft_ledger_keeps_original_message(self):
        with patch("ccc_codex_queue.time.sleep"):
            self.recover("任务请继续", views=[self.empty, {**self.draft, "busy": True}])
        self.assertEqual(self.edit.call_count, 1)
        self.recovery.next_probe.clear()
        before = copy.deepcopy(self.recovery.attempts)
        with patch("ccc_codex_queue.time.sleep"):
            self.assertEqual(self.recover(batch.PROMPT), "queue_recovery_unconfirmed")
        self.assertEqual(self.recovery.attempts, before)
        self.assertEqual(self.edit.call_count, 1)
        self.enter.assert_not_called()

    def test_short_queue_lost_enter_acknowledgement_is_not_replayed_after_restart(self):
        self.enter.side_effect = TimeoutError("lost acknowledgement")
        with patch("ccc_codex_queue.time.sleep"):
            self.assertEqual(self.recover(batch.PROMPT), "queue_recovery_unconfirmed")
        self.recovery = QueueRecovery(self.ledger, self.root / "bindings", self.root, "任务请继续")
        self.recovery.evidence = Mock(return_value=self.evidence)
        with patch("ccc_codex_queue.time.sleep"):
            self.recover(batch.PROMPT)
        self.assertEqual(self.enter.call_count, 1)
        self.assertEqual(self.edit.call_count, 1)


if __name__ == "__main__":
    unittest.main()

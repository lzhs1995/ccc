"""Historical jobs must no longer gate a new batch's startup dispatch."""
import json
import unittest
import uuid
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures


class StartupReservationTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def test_fifty_reservations_are_allowed_at_the_same_timestamp(self):
        for slot in self.worker.job["slots"]:
            self.assertTrue(self.worker._reserve_start(slot))
        self.assertEqual({s["launched_at"] for s in self.worker.job["slots"]}, {self.now})
        self.assertEqual(len({s["launch_id"] for s in self.worker.job["slots"]}), 50)
        self.assertFalse((self.root / "batch-capacity.json").exists())

    def test_other_history_is_not_read_or_validated_during_reservation(self):
        foreign = []
        for _ in range(100):
            jid = str(uuid.uuid4())
            path = batch.job_path(self.config, jid)
            path.parent.mkdir(parents=True)
            path.write_text("old damaged history must not block a different original pool")
            foreign.append(path)
        self.store.mutate(lambda c: c["workspace_rules"].extend(
            {"workspace_id": str(uuid.uuid4()), "active_batch_id": p.parent.name, "enabled": True}
            for p in foreign))
        original = core.load_json
        def load(path, default):
            self.assertNotIn(path, foreign)
            return original(path, default)
        with patch.object(core, "load_json", side_effect=load):
            self.assertTrue(self.worker._reserve_start(self.worker.job["slots"][0]))

    def test_existing_initializations_and_ambiguous_creates_do_not_consume_permits(self):
        for slot in self.worker.job["slots"][:49]:
            slot.update(phase="create_unknown", created_at=self.now, launched_at=self.now)
        self.assertTrue(self.worker._reserve_start(self.worker.job["slots"][-1]))
        self.assertTrue(all(s["phase"] == "create_unknown" for s in self.worker.job["slots"][:-1]))

    def test_pause_invalidates_previously_loaded_configuration(self):
        self.assertTrue(self.worker._reserve_start(self.worker.job["slots"][0]))
        self.store.mutate(lambda c: c["workspace_rules"][0].update(paused=True))
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][1]))
        self.assertEqual(self.worker.job["slots"][1]["phase"], "pending")

    def test_durable_reservation_cannot_be_reallocated(self):
        slot = self.worker.job["slots"][0]
        self.assertTrue(self.worker._reserve_start(slot))
        before = dict(slot)
        self.assertFalse(self.worker._reserve_start(slot))
        self.assertEqual(slot, before)
        slot["phase"] = "create_unknown"
        self.assertFalse(self.worker._reserve_start(slot))
        self.assertEqual(slot["launch_id"], before["launch_id"])

    def test_old_budget_file_is_preserved_and_ignored(self):
        path = self.root / "batch-capacity.json"
        original = json.dumps({"last_start": self.now + 100000, "last_job": "another"}).encode()
        path.write_bytes(original)
        with core.FileLock(self.root / "batch-capacity.lock"):
            self.assertTrue(self.worker._reserve_start(self.worker.job["slots"][0]))
        self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()

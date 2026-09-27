"""Real reservation logic against temporary jobs; no native or model traffic."""
import os
import unittest
import uuid
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures


class CapacityHistoryTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def other(self, phases, *, status="complete"):
        jid, wid = str(uuid.uuid4()), str(uuid.uuid4())
        job = {"id": jid, "workspace_id": wid, "config_path": str(self.config),
               "status": status, "slots": [
                   {"index": i, "phase": phase, "launched_at": self.now}
                   for i, phase in enumerate(phases)]}
        path = batch.job_path(self.config, jid)
        core.atomic_write_json(path, job)
        self.store.mutate(lambda c: c["workspace_rules"].append({
            "workspace_id": wid, "enabled": True, "active_batch_id": jid}))
        return path, job

    def occupy_self(self):
        for slot in self.worker.job["slots"][1:5]:
            slot.update(phase="created", launched_at=self.now)

    def test_unchanged_neutral_history_is_parsed_once_not_once_per_wait(self):
        self.occupy_self()
        paths = {self.other(["confirmed"])[0] for _ in range(20)}
        original = core.load_json
        reads = []
        def load(path, default):
            if path in paths:
                reads.append(path)
            return original(path, default)
        with patch.object(core, "load_json", side_effect=load):
            for _ in range(3):
                self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))
        self.assertEqual(len(reads), 20)
        self.assertFalse((self.root / "batch-capacity.json").exists())

    def test_complete_status_alone_does_not_hide_initializing_slots(self):
        self.other(["created"] * 4)
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))
        self.assertEqual(len(self.worker._capacity_history.neutral), 0)

    def test_complete_with_pending_is_not_cached_out_of_rotation(self):
        path, _ = self.other(["pending"])
        self.worker._capacity_history.read(path)
        self.assertNotIn(path, self.worker._capacity_history.neutral)

    def test_closed_job_is_neutral_but_reopening_invalidates_its_generation(self):
        path, job = self.other(["created"] * 4, status="workspace_closed")
        self.assertEqual(self.worker._capacity_history.read(path), {})
        self.assertIn(path, self.worker._capacity_history.neutral)
        job["status"] = "running"
        core.atomic_write_json(path, job)
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))
        self.assertNotIn(path, self.worker._capacity_history.neutral)

    def test_current_in_memory_initialization_is_never_cached(self):
        self.occupy_self()
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))
        self.assertNotIn(self.worker.path, self.worker._capacity_history.neutral)

    def test_replaced_history_immediately_counts_new_initialization(self):
        path, job = self.other(["confirmed"] * 4)
        self.worker._capacity_history.read(path)
        for slot in job["slots"]:
            slot["phase"] = "submitted"
        core.atomic_write_json(path, job)
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))
        self.assertNotIn(path, self.worker._capacity_history.neutral)

    def test_same_size_in_place_edit_with_restored_mtime_invalidates(self):
        path, _ = self.other(["confirmed"] * 4)
        self.worker._capacity_history.read(path)
        before = path.stat()
        data = path.read_bytes().replace(b'"confirmed"', b'"submitted"')
        self.assertEqual(len(data), before.st_size)
        path.write_bytes(data)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertNotEqual(path.stat().st_ctime_ns, before.st_ctime_ns)
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))

    def test_change_between_json_read_and_stat_defers_and_does_not_cache(self):
        path, job = self.other(["confirmed"] * 4)
        original = core.load_json
        def load(candidate, default):
            value = original(candidate, default)
            if candidate == path:
                for slot in job["slots"]:
                    slot["phase"] = "created"
                core.atomic_write_json(path, job)
            return value
        with patch.object(core, "load_json", side_effect=load):
            with self.assertRaisesRegex(RuntimeError, "批次状态正在更新"):
                self.worker._reserve_start(self.worker.job["slots"][0])
        self.assertNotIn(path, self.worker._capacity_history.neutral)
        self.assertEqual(self.worker.job["slots"][0]["phase"], "pending")
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))

    def test_deleted_then_recreated_file_does_not_reuse_old_neutral_entry(self):
        path, job = self.other(["confirmed"] * 4)
        self.worker._capacity_history.read(path)
        path.unlink()
        self.assertEqual(self.worker._capacity_history.read(path), {})
        for slot in job["slots"]:
            slot["phase"] = "created"
        core.atomic_write_json(path, job)
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))

    def test_pause_during_history_scan_cannot_reserve_caller(self):
        self.other(["confirmed"])
        read = self.worker._capacity_history.read
        def pausing(path):
            value = read(path)
            self.store.mutate(lambda c: c["workspace_rules"][0].update(paused=True))
            return value
        with patch.object(self.worker._capacity_history, "read", side_effect=pausing):
            self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))
        self.assertEqual(self.worker.job["slots"][0]["phase"], "pending")

    def test_external_authorization_is_fresh_even_after_prior_wait(self):
        path, job = self.other(["created"] * 4)
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))
        self.store.mutate(lambda c: next(r for r in c["workspace_rules"]
                                        if r.get("active_batch_id") == job["id"]).update(paused=True))
        self.assertTrue(self.worker._reserve_start(self.worker.job["slots"][0]))
        self.assertNotIn(path, self.worker._capacity_history.neutral)

    def test_rate_limit_and_initialization_lease_still_apply(self):
        self.other(["confirmed"])
        slot = self.worker.job["slots"][0]
        self.assertTrue(self.worker._reserve_start(slot))
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][1]))
        self.now += 0.5
        self.assertTrue(self.worker._reserve_start(self.worker.job["slots"][1]))
        for slot in self.worker.job["slots"][:4]:
            slot.update(phase="create_unknown", launched_at=self.now)
        self.now += 1
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][4]))
        self.now += batch.STARTUP_LEASE_SEC
        self.assertTrue(self.worker._reserve_start(self.worker.job["slots"][4]))

    def test_cache_is_bounded_and_contains_only_file_generations(self):
        cache = batch.CapacityHistory(limit=2)
        paths = [self.other(["confirmed"])[0] for _ in range(3)]
        for path in paths:
            self.assertEqual(cache.read(path), {})
        self.assertEqual(list(cache.neutral), paths[-2:])
        self.assertTrue(all(len(stamp) == 5 and all(type(v) is int for v in stamp)
                            for stamp in cache.neutral.values()))


if __name__ == "__main__":
    unittest.main()

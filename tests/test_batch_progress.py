"""Explain local preparation waits without promoting them to model failures."""
import unittest
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
import cmux_supervisor_tui as tui
from tests import test_workspace_batch as fixtures


class BatchProgressTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def snapshot(self):
        self.worker.save()
        return batch.snapshots(self.config, self.store.load())[self.wid]

    def test_capacity_wait_is_visible_even_when_failed_count_is_zero(self):
        for slot in self.worker.job["slots"][1:5]:
            slot.update(phase="created", launched_at=self.now)
        self.assertFalse(self.worker._reserve_start(self.worker.job["slots"][0]))
        value = self.snapshot()
        self.assertEqual(value["failed"], 0)
        self.assertEqual(value["wait"]["reason"], "capacity")
        self.assertIn("共享启动名额", tui.batch_preparation_progress(value, now=self.now))

    def test_lock_timeout_has_a_specific_recoverable_wait(self):
        enter = core.FileLock.__enter__
        def acquire(lock):
            if lock.path.name == "batch-capacity.lock":
                raise RuntimeError(f"timed out waiting for lock: {lock.path}")
            return enter(lock)
        with patch.object(core.FileLock, "__enter__", acquire):
            self.worker.step()
        self.assertEqual(self.snapshot()["wait"]["reason"], "capacity_lock")
        self.assertEqual(self.client.calls, [])
        self.now += 3
        self.worker.step()
        self.assertEqual(len(self.client.calls), 1)
        self.assertNotIn("preparation_wait", self.worker.job)
        self.assertNotIn("error", self.worker.job)

    def test_unchanged_wait_keeps_since_and_does_not_rewrite_each_tick(self):
        self.worker.pty_probe = lambda: False
        self.worker.step()
        before = self.worker.path.read_bytes()
        with patch.object(core, "atomic_write_json", wraps=core.atomic_write_json) as write:
            for _ in range(5):
                self.now += 1
                self.worker.step()
        write.assert_not_called()
        self.assertEqual(before, self.worker.path.read_bytes())

    def test_stale_error_is_not_presented_as_a_current_wait(self):
        self.worker.job.update(status="running", error="old capacity.lock timeout",
                               preparation_wait={"reason": "capacity_lock", "message": "old timeout"})
        value = self.snapshot()
        self.assertNotIn("old", tui.batch_preparation_progress(value, now=self.now))
        self.assertEqual(value["wait"]["reason"], "scheduled")

    def test_successful_preflight_removes_previous_wait(self):
        self.worker.pty_probe = lambda: False
        self.worker.step()
        self.worker.pty_probe = lambda: True
        self.now += 1
        self.worker.step()
        self.assertEqual(self.worker.job["status"], "running")
        self.assertNotIn("preparation_wait", self.worker.job)
        self.assertNotIn("error", self.worker.job)

    def test_pause_clears_current_wait_without_creating_a_slot(self):
        self.worker.pty_probe = lambda: False
        self.worker.step()
        self.store.mutate(lambda c: c["workspace_rules"][0].update(paused=True))
        self.assertFalse(self.worker.step())
        self.assertEqual(self.worker.job["status"], "cancelled")
        self.assertEqual(self.snapshot()["wait"], {})
        self.assertEqual(self.client.calls, [])

    def test_topology_wait_clears_when_fresh_membership_returns(self):
        with patch.object(self.client, "tree", side_effect=core.CmuxError("tree refresh pending")):
            self.worker.step()
        self.assertEqual(self.snapshot()["wait"]["reason"], "topology")
        self.now += 1
        self.worker.step()
        self.assertNotIn("preparation_wait", self.worker.job)
        self.assertEqual(len(self.client.calls), 1)

    def test_last_progress_ignores_retry_heartbeat_and_old_error(self):
        self.worker.job["created_at"] = 1
        self.worker.job["updated_at"] = 999
        self.worker.job["slots"][0].update(
            phase="created", created_at=2, retry_at=998,
            naming={"confirmed_at": 20}, confirmation={"confirmed_at": 30})
        value = batch.preparation_progress(self.worker.job)
        self.assertEqual(value["named"], 1)
        self.assertEqual(value["last_progress_at"], 30)
        line = tui.batch_preparation_progress({**self.snapshot(), **value}, now=100)
        self.assertIn("最近进展 70秒前", line)

    def test_naming_and_first_task_wait_are_distinct(self):
        slot = self.worker.job["slots"][0]
        slot.update(phase="created", naming={"submitted_at": 1})
        self.assertEqual(self.snapshot()["wait"]["reason"], "naming")
        slot.update(phase="submitted", naming={"confirmed_at": 2}, submit_at=3)
        self.assertEqual(self.snapshot()["wait"]["reason"], "first_task")

    def test_all_first_tasks_started_does_not_claim_model_success(self):
        value = {"status": "complete", "startup_mode": "private_check", "created": 50,
                 "named": 50, "started": 50, "total": 50, "failed": 0, "wait": {}}
        line = tui.batch_preparation_progress(value)
        self.assertIn("首任务已全数启动", line)
        self.assertNotIn("模型成功", line)
        self.assertNotIn("异常 0", line)


if __name__ == "__main__":
    unittest.main()

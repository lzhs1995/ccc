"""Permanent slot claims: real files/threads, mocked exec only."""
from concurrent.futures import ThreadPoolExecutor
import copy
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

import ccc_workspace_batch as batch


class ArgvClaimTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name) / "config.json"
        self.job = str(uuid.uuid4())
        batch.job_path(self.config, self.job).parent.mkdir(parents=True)
        self.argv = ["/native/codex", batch.PROMPT]
        self.record = dict(job_id=self.job, index=0, policy=batch.ARGV_INITIAL_POLICY,
                           launch_id="first", argv=self.argv)

    def execute(self, record=None, guard=lambda: True):
        return batch._exec_claimed_argv(self.config, self.job, 0,
                                       record or self.record, self.argv, guard)

    def test_concurrent_bootstraps_exec_at_most_once(self):
        barrier = threading.Barrier(20)
        def attempt(index):
            barrier.wait()
            record = {**self.record, "launch_id": str(index)}
            try:
                self.execute(record)
                return True
            except FileExistsError:
                return False
        with patch.object(os, "execv") as execute, ThreadPoolExecutor(20) as pool:
            results = list(pool.map(attempt, range(20)))
        self.assertEqual(sum(results), 1)
        execute.assert_called_once_with(self.argv[0], self.argv)

    def test_pause_after_durable_claim_never_executes_or_retries(self):
        def paused():
            self.assertTrue((batch.job_path(self.config, self.job).parent /
                             "initial-argv-0.json").read_bytes())
            return False
        with patch.object(os, "execv") as execute:
            with self.assertRaisesRegex(RuntimeError, "authorization changed"):
                self.execute(guard=paused)
            with self.assertRaises(FileExistsError):
                self.execute({**self.record, "launch_id": "replacement"})
        execute.assert_not_called()

    def test_partial_claim_remains_consumed_after_restart(self):
        path = batch.job_path(self.config, self.job).parent / "initial-argv-0.json"
        path.write_bytes(b'{"partial":')
        with patch.object(os, "execv") as execute:
            with self.assertRaises(FileExistsError):
                self.execute()
        execute.assert_not_called()
        self.assertEqual(path.read_bytes(), b'{"partial":')

    def test_fsync_failure_cannot_be_replayed(self):
        with patch.object(os, "fsync", side_effect=OSError("disk")), \
             patch.object(os, "execv") as execute:
            with self.assertRaises(OSError):
                self.execute()
        with self.assertRaises(FileExistsError):
            self.execute()
        execute.assert_not_called()

    def test_exec_error_keeps_claim(self):
        with patch.object(os, "execv", side_effect=OSError("exec failed")) as execute:
            with self.assertRaises(OSError):
                self.execute()
            with self.assertRaises(FileExistsError):
                self.execute()
        self.assertEqual(execute.call_count, 1)

    def test_guard_replacement_of_claim_vetoes_exec(self):
        def replace():
            path = batch.job_path(self.config, self.job).parent / "initial-argv-0.json"
            path.write_text("{}")
            return True
        with patch.object(os, "execv") as execute:
            with self.assertRaisesRegex(RuntimeError, "claim changed"):
                self.execute(guard=replace)
        execute.assert_not_called()

    def test_guard_exception_keeps_claim(self):
        def fail():
            raise OSError("topology unavailable")
        with patch.object(os, "execv") as execute:
            with self.assertRaises(OSError):
                self.execute(guard=fail)
            with self.assertRaises(FileExistsError):
                self.execute()
        execute.assert_not_called()

    def test_wrong_argv_does_not_consume_slot(self):
        record = copy.deepcopy(self.record)
        record["argv"].append("extra")
        with self.assertRaisesRegex(RuntimeError, "argv identity"):
            self.execute(record)
        with patch.object(os, "execv") as execute:
            self.execute()
        execute.assert_called_once()

    def test_guard_cannot_mutate_consumed_argv(self):
        expected = list(self.argv)
        def mutate():
            self.argv.append("different prompt")
            return True
        with patch.object(os, "execv") as execute:
            self.execute(guard=mutate)
        execute.assert_called_once_with(expected[0], expected)

    def test_symlink_claim_is_not_followed_or_replaced(self):
        target = self.config.parent / "preserved"
        target.write_bytes(b"original")
        path = batch.job_path(self.config, self.job).parent / "initial-argv-0.json"
        path.symlink_to(target)
        with patch.object(os, "execv") as execute:
            with self.assertRaises(FileExistsError):
                self.execute()
        execute.assert_not_called()
        self.assertEqual(target.read_bytes(), b"original")

    def test_separate_slots_have_independent_claims(self):
        with patch.object(os, "execv") as execute:
            for index in range(50):
                record = {**self.record, "index": index}
                batch._exec_claimed_argv(self.config, self.job, index, record,
                                         self.argv, lambda: True)
        self.assertEqual(execute.call_count, 50)


if __name__ == "__main__":
    unittest.main()

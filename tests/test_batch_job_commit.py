"""Actual job-file commits and failure/authorization boundaries; no native I/O."""
from concurrent.futures import Future, ThreadPoolExecutor
import copy
import tempfile
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures


class JobCommitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'job.json'
        self.writer = batch._JobCommitter(self.path)
        self.addCleanup(self.writer.close)

    def test_fifty_waiters_share_a_real_commit_containing_all_intents(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original, written = core.atomic_write_json, []

        def held(path, value):
            written.append(copy.deepcopy(value))
            entered.set()
            if not release.wait(5):
                raise AssertionError('commit not released')
            original(path, value)

        with patch.object(core, 'atomic_write_json', side_effect=held):
            with self.writer.condition:
                futures = [self.writer.submit({'slots': [{'index': k, 'phase': 'creating'}
                             for k in range(n + 1)]}) for n in range(50)]
            self.assertTrue(entered.wait(1))
            self.assertFalse(self.path.exists())
            self.assertTrue(all(not future.done() for future in futures))
            release.set()
            for future in futures:
                future.result(2)
        self.assertEqual(len(written), 1)
        self.assertEqual([slot['index'] for slot in core.load_json(self.path, {})['slots']], list(range(50)))

    def test_write_failure_releases_every_waiter_as_failed_and_rejects_late_work(self):
        with patch.object(core, 'atomic_write_json', side_effect=OSError('disk rejected commit')):
            with self.writer.condition:
                futures = [self.writer.submit({'slot': n}) for n in range(50)]
            for future in futures:
                with self.assertRaisesRegex(OSError, 'disk rejected'):
                    future.result(2)
        self.assertFalse(self.path.exists())
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            self.writer.submit({'slot': 'late'})

    def test_close_drains_pending_before_return_and_rejects_new_submissions(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = core.atomic_write_json

        def held(path, value):
            entered.set()
            if not release.wait(5):
                raise AssertionError('commit not released')
            original(path, value)

        with patch.object(core, 'atomic_write_json', side_effect=held), ThreadPoolExecutor(1) as pool:
            saved = self.writer.submit({'slots': [1]})
            self.assertTrue(entered.wait(1))
            with self.writer.condition:
                closing = pool.submit(self.writer.close)
            # Wait for close's state without depending on thread scheduling.
            with self.writer.condition:
                self.writer.condition.wait_for(lambda: self.writer.closed, timeout=1)
                self.assertTrue(self.writer.closed)
            self.assertFalse(closing.done())
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                self.writer.submit({'slots': [2]})
            release.set()
            saved.result(2)
            closing.result(2)
        self.assertEqual(core.load_json(self.path, {}), {'slots': [1]})


class BatchCommitBoundaryTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def slot_view(self):
        with self.worker._state_lock:
            commit = self.worker._queue_job_commit()
        self.worker._wait_job_commit(commit)
        view = copy.copy(self.worker)
        view.job = copy.deepcopy(self.worker.job)
        view._slot_parent, view._slot_index = self.worker, 0
        view._wait_observed = False
        return view

    def test_unchanged_poll_skips_serialization_but_waits_for_pending_commit(self):
        view = self.slot_view()
        pending, entered = Future(), threading.Event()
        self.worker._queued_commit = pending
        original = self.worker._wait_job_commit
        def wait(commit):
            entered.set()
            return original(commit)
        with patch.object(self.worker, '_wait_job_commit', side_effect=wait), \
             patch.object(self.worker, '_serialized_job', side_effect=AssertionError('unchanged job serialized')), \
             ThreadPoolExecutor(1) as pool:
            result = pool.submit(view.save)
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(result.done())
            finally:
                pending.set_result(None)
            result.result(1)

    def test_unchanged_poll_propagates_original_commit_failure(self):
        view = self.slot_view()
        pending = Future()
        pending.set_exception(OSError('original commit failed'))
        self.worker._queued_commit = pending
        with self.assertRaisesRegex(OSError, 'original commit failed'):
            view.save()

    def test_same_memory_slot_without_matching_queued_snapshot_is_persisted(self):
        view = self.slot_view()
        self.worker.job['slots'][0]['error'] = 'not yet committed'
        view.job['slots'][0]['error'] = 'not yet committed'
        view.save()
        self.assertEqual(core.load_json(self.worker.path, {})['slots'][0]['error'], 'not yet committed')

    def test_pause_during_group_commit_prevents_all_original_creations(self):
        self.worker._slot_pool = ThreadPoolExecutor(50)
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = core.atomic_write_json

        def held(path, value):
            if Path(path) == self.worker.path and any(s.get('phase') == 'creating' for s in value['slots']):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('commit not released')
            original(path, value)

        with patch.object(core, 'atomic_write_json', side_effect=held), ThreadPoolExecutor(1) as pool:
            stepping = pool.submit(self.worker.step)
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.client.calls, [])
            self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
            release.set()
            stepping.result(5)
            for future in list(self.worker._inflight.values()):
                future.result(5)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.sent, [])
        self.assertTrue(all(s['phase'] == 'pending' for s in self.worker.job['slots']))

    def test_slot_merge_snapshot_does_not_change_while_writer_is_waiting(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = core.atomic_write_json

        def held(path, value):
            if Path(path) == self.worker.path:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('commit not released')
            original(path, value)

        with patch.object(core, 'atomic_write_json', side_effect=held):
            with self.worker._state_lock:
                self.worker.job['slots'][0].update(phase='creating', launch_id='original')
                commit = self.worker._queue_job_commit()
            self.assertTrue(entered.wait(1))
            self.worker.job['slots'][0]['launch_id'] = 'in-memory-later'
            release.set()
            self.worker._wait_job_commit(commit)
        saved = core.load_json(self.worker.path, {})
        self.assertEqual(saved['slots'][0]['launch_id'], 'original')
        self.assertEqual(self.worker.job['slots'][0]['launch_id'], 'in-memory-later')


if __name__ == '__main__':
    unittest.main()

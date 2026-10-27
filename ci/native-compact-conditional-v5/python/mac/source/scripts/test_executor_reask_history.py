"""Bounded scheduler evidence cannot block or change authorized file work."""
import fcntl
import os
import time
import unittest
from unittest.mock import patch

import executor_ready as ready
import executor_reask as reask
import executor_reask_scheduler as scheduler
from cmux_evidence_io import open_regular_lock
import test_executor_reask_scheduler as fixtures
from test_executor_reask_scheduler import CALLER


class HistoryTests(unittest.TestCase):
    # Reuse the isolated fixture, without rerunning the scheduler's test class.
    setUp = fixtures.SchedulerTests.setUp
    begin = fixtures.SchedulerTests.begin
    live = fixtures.SchedulerTests.live
    enroll = fixtures.SchedulerTests.enroll

    def history(self):
        return ready.read_json(scheduler.root() / 'SWEEP-HISTORY.json')

    def test_success_keeps_full_status_and_compact_history_separate(self):
        self.enroll()
        result = scheduler.sweep(now=1001.)
        self.assertEqual(result, ready.read_json(scheduler.root() / 'STATUS.json'))
        self.assertEqual(result['history']['status'], 'HISTORY_RECORDED')
        entry = self.history()['entries'][-1]
        self.assertEqual(entry['file_posts'], 1)
        self.assertTrue(entry['file_posts_complete'])
        self.assertEqual(entry['enrollment_order'], [scheduler.enrollment_path(CALLER).name])
        self.assertEqual(entry['episodes'][0]['caller_surface_uuid'], CALLER)
        self.assertNotIn('history', entry)
        self.assertEqual(result['history']['bytes'],
                         (scheduler.root() / 'SWEEP-HISTORY.json').stat().st_size)
        self.transport.assert_not_called()

    def test_busy_sweep_is_retained_without_overwriting_worker_cursor(self):
        self.enroll()
        scheduler.sweep(now=1001.)
        path = scheduler.root() / 'STATUS.json'
        before = path.read_bytes()
        with open_regular_lock(scheduler.root() / 'sweep.lock') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = scheduler.sweep(now=1062.)
        self.assertEqual(result['status'], 'SCHEDULER_BUSY')
        self.assertEqual(result['history']['status'], 'HISTORY_RECORDED')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.history()['entries'][-1]['status'], 'SCHEDULER_BUSY')
        self.assertEqual(reask.load(CALLER)['file_posts'], 1)

    def test_registry_contention_retains_enrollment_without_inventing_caller(self):
        self.enroll()
        with scheduler.registry_lock():
            result = scheduler.sweep(now=1001.)
        row = self.history()['entries'][-1]['episodes'][0]
        self.assertEqual(row['enrollment'], scheduler.enrollment_path(CALLER).name)
        self.assertEqual(row['status'], 'REGISTRY_BUSY')
        self.assertNotIn('caller_surface_uuid', row)
        self.assertEqual(result['file_posts'], 0)

    def test_episode_contention_has_authenticated_caller_but_no_work(self):
        self.enroll()
        with reask.episode_lock(CALLER):
            result = scheduler.sweep(now=1001.)
        row = self.history()['entries'][-1]['episodes'][0]
        self.assertEqual((row['status'], row['caller_surface_uuid']), ('EPISODE_BUSY', CALLER))
        self.assertEqual(result['file_posts'], 0)

    def test_interrupted_commit_keeps_partial_counter_and_pending_enrollment(self):
        self.enroll()
        tick = reask.tick_locked
        def interrupted(*args, **kwargs):
            tick(*args, **kwargs)
            raise ready.WaitBudgetExpired()
        with patch.object(reask, 'tick_locked', side_effect=interrupted):
            result = scheduler.sweep(now=1001.)
        entry = self.history()['entries'][-1]
        self.assertEqual(entry['status'], 'SWEEP_BUDGET_EXHAUSTED')
        self.assertFalse(entry['file_posts_complete'])
        self.assertEqual(entry['pending_enrollment'], entry['enrollment_order'][0])
        self.assertEqual(entry['episodes'], [])
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(reask.load(CALLER)['file_posts'], 1)

    def test_history_lock_skips_only_diagnostics_and_file_post_still_commits(self):
        self.enroll()
        scheduler.sweep(now=1001.)
        path = scheduler.root() / 'SWEEP-HISTORY.json'
        before = path.read_bytes()
        with open_regular_lock(scheduler.root() / 'history.lock') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = scheduler.sweep(now=1062.)
        self.assertEqual((result['status'], result['file_posts']), ('SWEPT', 1))
        self.assertEqual(result['history']['status'], 'HISTORY_BUSY')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(reask.load(CALLER)['file_posts'], 2)

    def test_history_read_timeout_is_bounded_and_preserves_existing_evidence(self):
        self.enroll()
        scheduler.sweep(now=1001.)
        path = scheduler.root() / 'SWEEP-HISTORY.json'
        before = path.read_bytes()
        read = scheduler.read_bytes
        def slow(candidate, **kwargs):
            if candidate == path:
                time.sleep(.2)
            return read(candidate, **kwargs)
        started = time.monotonic()
        with patch.object(scheduler, 'read_bytes', side_effect=slow), \
                patch.object(scheduler, 'MAX_HISTORY_SECONDS', .02):
            result = scheduler.sweep(now=1062.)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual((result['status'], result['file_posts']), ('SWEPT', 1))
        self.assertEqual(result['history']['status'], 'HISTORY_BUDGET_EXHAUSTED')
        self.assertEqual(result['history']['phase'], 'READ')
        self.assertEqual(path.read_bytes(), before)

    def test_invalid_existing_history_is_preserved_without_blocking_sweep(self):
        self.enroll()
        path = scheduler.root() / 'SWEEP-HISTORY.json'
        for raw in (b'invalid json', b'[]', b'{"schema":"future","entries":[]}',
                    b'x' * (scheduler.MAX_HISTORY_BYTES + 1)):
            with self.subTest(prefix=raw[:40]):
                path.write_bytes(raw)
                result = scheduler.sweep(now=1001.)
                self.assertEqual(result['status'], 'SWEPT')
                self.assertEqual(result['history']['status'], 'HISTORY_UNCONFIRMED')
                self.assertEqual(result['history']['phase'], 'READ')
                self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(reask.load(CALLER)['file_posts'], 1)

    def test_history_retains_latest_entries_with_count_and_byte_limits(self):
        scheduler.secure_directory(scheduler.root())
        with patch.object(scheduler, 'MAX_HISTORY_ENTRIES', 3):
            for index in range(8):
                self.assertEqual(scheduler.record_history(dict(at_epoch=index))['status'],
                                 'HISTORY_RECORDED')
        self.assertEqual([e['at_epoch'] for e in self.history()['entries']], [5, 6, 7])
        path = scheduler.root() / 'SWEEP-HISTORY.json'
        path.unlink()  # isolated diagnostic fixture only
        with patch.object(scheduler, 'MAX_HISTORY_BYTES', 1000):
            for index in range(8):
                receipt = scheduler.record_history(dict(at_epoch=index, episodes=[dict(
                    enrollment='e' * 68, status='WAITING_REPLY', file_posts=0)]))
                self.assertEqual(receipt['status'], 'HISTORY_RECORDED')
                self.assertLessEqual(path.stat().st_size, 1000)
                self.assertEqual(path.stat().st_size, receipt['bytes'])
        self.assertEqual(self.history()['entries'][-1]['at_epoch'], 7)
        self.assertLess(len(self.history()['entries']), 8)

    def test_history_omits_large_reservation_payloads_and_keeps_authority_fact(self):
        scheduler.secure_directory(scheduler.root())
        scheduler.record_history(dict(episodes=[dict(enrollment='original.json',
            status='WAITING_REPLY', paused_tasks={'task': {'prompt': 'x' * 1000000}},
            reservation_inquiries_authorized=False)]))
        row = self.history()['entries'][0]['episodes'][0]
        self.assertEqual(row['paused_task_count'], 1)
        self.assertFalse(row['reservation_inquiries_authorized'])
        self.assertNotIn('paused_tasks', row)
        self.assertLess((scheduler.root() / 'SWEEP-HISTORY.json').stat().st_size, 1000)

    def test_history_post_replace_failure_does_not_claim_record_absent(self):
        scheduler.secure_directory(scheduler.root())
        with patch.object(ready, 'sync_directory', side_effect=OSError('fsync failed')):
            receipt = scheduler.record_history(dict(status='SWEPT', at_epoch=1001.))
        self.assertEqual(receipt['status'], 'HISTORY_UNCONFIRMED')
        self.assertEqual(receipt['phase'], 'WRITE')
        self.assertEqual(self.history()['entries'][-1]['at_epoch'], 1001.)
        self.assertEqual(list(scheduler.root().glob('.*.tmp')), [])

    def test_history_pre_replace_deadline_preserves_bytes_and_releases_lock(self):
        scheduler.secure_directory(scheduler.root())
        scheduler.record_history(dict(status='SWEPT', at_epoch=1001.))
        path = scheduler.root() / 'SWEEP-HISTORY.json'
        before = path.read_bytes()
        with patch.object(ready.os, 'fsync', side_effect=ready.WaitBudgetExpired):
            receipt = scheduler.record_history(dict(status='SWEPT', at_epoch=1061.))
        self.assertEqual(receipt['status'], 'HISTORY_BUDGET_EXHAUSTED')
        self.assertEqual(receipt['phase'], 'WRITE')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(scheduler.root().glob('.*.tmp')), [])
        self.assertEqual(scheduler.record_history(dict(at_epoch=1121.))['status'],
                         'HISTORY_RECORDED')

    def test_history_special_files_do_not_block_or_follow_foreign_evidence(self):
        scheduler.secure_directory(scheduler.root())
        path = scheduler.root() / 'SWEEP-HISTORY.json'
        foreign = self.home / 'foreign.json'
        foreign.write_bytes(b'original evidence')
        path.symlink_to(foreign)
        receipt = scheduler.record_history(dict(at_epoch=1001.))
        self.assertEqual(receipt['status'], 'HISTORY_UNCONFIRMED')
        self.assertEqual(foreign.read_bytes(), b'original evidence')
        path.unlink()
        os.mkfifo(path)
        receipt = scheduler.record_history(dict(at_epoch=1061.))
        self.assertEqual(receipt['status'], 'HISTORY_UNCONFIRMED')
        self.assertEqual(receipt['phase'], 'READ')
        self.assertLess(receipt['duration_seconds'], 1.0)

    def test_history_fifo_lock_is_rejected_without_waiting(self):
        scheduler.secure_directory(scheduler.root())
        os.mkfifo(scheduler.root() / 'history.lock')
        receipt = scheduler.record_history(dict(at_epoch=1001.))
        self.assertEqual(receipt['status'], 'HISTORY_UNCONFIRMED')
        self.assertEqual(receipt['phase'], 'LOCK')
        self.assertLess(receipt['duration_seconds'], 1.0)


if __name__ == '__main__':
    unittest.main()

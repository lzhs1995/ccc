import copy
import threading
import time
import unittest

from test_standby_manager import ManagerTests
from ccc_native_standby import ObservationExpired


class CompletionAlignmentTests(unittest.TestCase):
    setUp = ManagerTests.setUp

    def test_staggered_fifty_reads_finish_fresh_without_leaving_workers(self):
        self.ledger.clock = time.monotonic
        active = set()
        lock = threading.Lock()
        def observe(index):
            with lock: active.add(index)
            try:
                time.sleep(.1 + 2.9 * index / 49)
                return {**copy.deepcopy(self.rows[index]),
                        'observed_monotonic': time.monotonic()}
            finally:
                with lock: active.remove(index)
        def authorized(index):
            time.sleep(.3)
            return True
        self.manager.observer = observe
        self.manager.authorized = authorized
        result = self.manager._refresh_locked(deadline=time.monotonic() + 9)
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(active, set())
        self.assertEqual(self.sent, [])
        self.assertFalse((self.directory / 'activation.json').exists())

    def test_invalidation_wakes_delayed_readers_without_new_observation(self):
        self.manager._observation_costs = {0: 3.0, **{i: 0.0 for i in range(1, 50)}}
        calls = []
        started = threading.Event()
        def observe(index):
            calls.append(index)
            started.set()
            return copy.deepcopy(self.rows[index])
        self.manager.observer = observe
        def cancel():
            started.wait(2)
            self.manager.invalidate('cancel during scheduling')
        thread = threading.Thread(target=cancel)
        thread.start()
        try:
            with self.assertRaises(ValueError):
                self.manager._gather_aligned_observations(time.monotonic() + 5)
        finally:
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertLessEqual(set(calls), {0})
        self.assertEqual(self.sent, [])

    def test_deadline_does_not_allow_delayed_observations_to_start(self):
        self.manager._observation_costs = {0: 1.0, **{i: 0.0 for i in range(1, 50)}}
        seen = []
        self.manager.observer = lambda i: (seen.append(i) or copy.deepcopy(self.rows[i]))
        with self.assertRaises(TimeoutError):
            self.manager._gather_aligned_observations(time.monotonic() + .05)
        self.assertLessEqual(set(seen), {0})
        self.assertFalse(self.sent)

    def test_alignment_preserves_returned_timestamps(self):
        rows = self.manager._gather_aligned_observations(time.monotonic() + 1)
        self.assertEqual([r['observed_monotonic'] for r in rows], [self.now] * 50)

    def test_worker_error_is_propagated_after_all_workers_finish(self):
        active = set()
        lock = threading.Lock()
        def observe(index):
            with lock: active.add(index)
            try:
                if index == 0: raise OSError('original reader failed')
                time.sleep(.01)
                return copy.deepcopy(self.rows[index])
            finally:
                with lock: active.remove(index)
        self.manager.observer = observe
        with self.assertRaisesRegex(OSError, 'original reader failed'):
            self.manager._gather_aligned_observations(time.monotonic() + 1)
        self.assertEqual(active, set())
        self.assertFalse(self.sent)


    def _expire_first_pass(self, mutation):
        original = self.ledger.observe_ready
        calls = []
        def expire(rows, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                self.now += .5
                for row in self.rows:
                    row['observed_monotonic'] = self.now
                mutation()
                raise ObservationExpired('injected aged slot', [0])
            return original(rows, **kwargs)
        self.ledger.observe_ready = expire

    def test_original_change_in_previously_fresh_slot_rejected(self):
        self._expire_first_pass(lambda: self.rows[49].update(pid=99999))
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            self.manager.refresh()
        self.assertFalse(self.sent)
        self.assertFalse((self.directory / 'activation.json').exists())

    def test_authorization_revoked_during_retry_rejected(self):
        def revoke(): self.allowed = False
        self._expire_first_pass(revoke)
        with self.assertRaisesRegex(ValueError, 'authorization changed'):
            self.manager.refresh()
        self.assertFalse(self.sent)
        self.assertFalse((self.directory / 'activation.json').exists())


if __name__ == '__main__':
    unittest.main()

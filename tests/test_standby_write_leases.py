"""Concurrent input writes and cohort revocation; no native processes."""
import copy
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from tests import test_native_standby as ledger_fixture


class WriteLeaseTests(unittest.TestCase):
    def setUp(self):
        self.state = ledger_fixture.StandbyLedgerTests()
        self.state.setUp()
        self.addCleanup(self.state.doCleanups)
        self.state.ready()
        self.state.activate()

    def test_slow_socket_write_does_not_force_other_original_to_reobserve(self):
        entered, release, following = (threading.Event() for _ in range(3))
        observed = [0, 0]

        def observe(index):
            observed[index] += 1
            return copy.deepcopy(self.state.rows[index])

        def send(row, prompt, input_id, *, write_guard):
            with write_guard():
                if row['index'] == 0:
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError('fixture write not released')
                else:
                    following.set()

        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.state.deliver, 0, observe=observe, send=send)
            try:
                self.assertTrue(entered.wait(1))
                second = pool.submit(self.state.deliver, 1, observe=observe, send=send)
                self.assertTrue(following.wait(1), 'other socket blocked by cohort write lock')
                self.assertTrue(second.result(1))
                self.assertEqual(observed[1], 3)
                self.assertFalse(first.done())
            finally:
                release.set()
            self.assertTrue(first.result(1))

    def test_revocation_closes_admission_and_drains_existing_write(self):
        entered, release, revoked = (threading.Event() for _ in range(3))
        def send(row, prompt, input_id, *, write_guard):
            with write_guard():
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('fixture write not released')

        ledger = self.state.ledger
        def invalidate():
            ledger._invalidate('other original changed')
            revoked.set()

        with ThreadPoolExecutor(3) as pool:
            first = pool.submit(self.state.deliver, 0, send=send)
            try:
                self.assertTrue(entered.wait(1))
                revocation = pool.submit(invalidate)
                # The condition wait releases the state lock, letting us
                # inspect closure while the admitted socket is still blocked.
                with ledger._writes_drained:
                    self.assertTrue(ledger._writes_drained.wait_for(lambda: ledger._invalid, 1))
                self.assertFalse(revoked.is_set())
                other = pool.submit(self.state.deliver, 1)
                self.assertFalse(revoked.is_set())
            finally:
                release.set()
            self.assertTrue(first.result(2))
            revocation.result(2)
            with self.assertRaises(ValueError):
                other.result(2)
        self.assertTrue(revoked.is_set())
        self.assertFalse(self.state.sent)
        self.assertFalse(self.state.deliver(0))

    def test_simultaneous_write_errors_drain_without_deadlock_or_replay(self):
        barrier = threading.Barrier(2)
        def send(row, prompt, input_id, *, write_guard):
            with write_guard():
                barrier.wait(timeout=2)
                raise OSError('partial socket write; outcome unknown')
        with ThreadPoolExecutor(2) as pool:
            attempts = [pool.submit(self.state.deliver, i, send=send) for i in range(2)]
            for attempt in attempts:
                with self.assertRaises(OSError):
                    attempt.result(3)
        self.assertTrue((self.state.path/'invalidated.json').exists())
        self.assertEqual(self.state.ledger._active_writes, 0)
        self.assertFalse(self.state.deliver(0))
        self.assertFalse(self.state.deliver(1))


if __name__ == '__main__':
    unittest.main()

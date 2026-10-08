"""FD admission fairness; no real surfaces, inputs, or model requests."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading
import time
import unittest

import ccc_standby_prepare as prep


class InventoryPriorityTests(unittest.TestCase):
    def queued(self, reader, count):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with reader.condition:
                if len(reader.waiters) == count:
                    return
            time.sleep(.001)
        self.fail('read did not join admission queue')

    def test_connected_checks_advance_and_background_cannot_starve(self):
        local = threading.local()
        seen = []
        reader = prep.InventoryReader(lambda i: seen.append(i) or i,
            lambda: True, limit=1, priority=lambda: getattr(local, 'connected', False))
        reader.capacity = 0
        def call(i, connected):
            local.connected = connected
            return reader(i)
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = []
            for i, priority in [(0, False), (1, False), *[(i, True) for i in range(2, 12)]]:
                futures.append(pool.submit(call, i, priority))
                self.queued(reader, len(futures))
            with reader.condition:
                reader.capacity = 1
                reader._wake_head()
            self.assertEqual([f.result(timeout=3) for f in futures], list(range(12)))
        self.assertEqual(seen, [2, 3, 4, 5, 0, 6, 7, 8, 9, 1, 10, 11])
        self.assertEqual(reader.capacity, 1)
        self.assertFalse(reader.waiters)

    def test_cancel_both_classes_without_read_and_release_tickets(self):
        local = threading.local()
        live = threading.Event()
        live.set()
        seen = []
        reader = prep.InventoryReader(lambda: seen.append(True), live.is_set,
            limit=1, priority=lambda: getattr(local, 'connected', False))
        reader.capacity = 0
        def call(connected):
            local.connected = connected
            return reader()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, p) for p in (False, True)]
            self.queued(reader, 2)
            live.clear()
            for f in futures:
                with self.assertRaisesRegex(ValueError, 'cancelled'):
                    f.result(timeout=1)
        self.assertEqual(seen, [])
        self.assertFalse(reader.waiters)

    def test_failed_priority_read_releases_capacity_and_does_not_cache(self):
        seen = []
        def read(i):
            seen.append(i)
            if i == 0:
                raise OSError('vnode changed')
            return object()
        reader = prep.InventoryReader(read, lambda: True, limit=1, priority=lambda: True)
        with self.assertRaises(OSError):
            reader(0)
        a, b = reader(1), reader(1)
        self.assertIsNot(a, b)
        self.assertEqual(seen, [0, 1, 1])
        self.assertEqual(reader.capacity, 1)

    def test_priority_is_bound_to_current_thread_admitted_connection(self):
        local = threading.local()
        owner = object.__new__(prep.PreparationOwner)
        owner.client = SimpleNamespace(viewport_socket=SimpleNamespace(_connection_local=local))
        self.assertFalse(owner._inventory_priority())
        local.read_rpc = lambda: None
        self.assertTrue(owner._inventory_priority())
        with ThreadPoolExecutor(max_workers=1) as pool:
            self.assertFalse(pool.submit(owner._inventory_priority).result(timeout=1))
        local.read_rpc = None
        self.assertFalse(owner._inventory_priority())
        owner.client.viewport_socket._connection_local = SimpleNamespace(read_rpc=lambda: None)
        self.assertFalse(owner._inventory_priority())


if __name__ == '__main__':
    unittest.main()

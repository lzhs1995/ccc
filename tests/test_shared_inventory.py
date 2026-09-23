"""Cross-process inventory bounds under large/slow/failing fleet discovery."""
import json
import fcntl
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from ccc_inventory import InventoryUnavailable, SharedInventory


def collect_once(directory):
    inventory = SharedInventory(directory)
    def loader():
        with (Path(directory) / 'calls').open('a') as out:
            out.write('top\n')
        time.sleep(.1)
        return {'windows': []}
    try:
        inventory.get('top', loader, ttl=5)
    except InventoryUnavailable:
        pass


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1000.0

    def test_500_and_1000_surface_consumers_never_multiply_global_scans(self):
        for size in [500, 1000]:
            with self.subTest(size=size):
                clients = [SharedInventory(self.root / str(size), clock=lambda: self.now) for _ in range(6)]
                payload = {'windows': [{'workspaces': [{'id': str(i // 50), 'surfaces': [
                    {'id': str(i), 'processes': [{'pid': i, 'command': 'codex'}]}]} for i in range(size)]}]}
                loader = Mock(return_value=payload)
                for tick in range(300):
                    self.now = 1000 + tick * .05
                    for client in clients:
                        self.assertEqual(client.get('top', loader, ttl=5), payload)
                self.assertEqual(loader.call_count, 3)

    def test_failing_requests_are_coalesced_and_recover_after_retry_period(self):
        clients = [SharedInventory(self.root, clock=lambda: self.now) for _ in range(6)]
        loader = Mock(side_effect=TimeoutError('system.top control request failed: timed out'))
        for _ in range(20):
            for client in clients:
                with self.assertRaises(InventoryUnavailable):
                    client.get('top', loader, ttl=5)
        self.assertEqual(loader.call_count, 1)
        self.now += 5
        loader.side_effect = None
        loader.return_value = {'windows': []}
        self.assertEqual(clients[-1].get('top', loader, ttl=5), {'windows': []})
        self.assertEqual(loader.call_count, 2)

    def test_watcher_owns_normal_collection_and_display_cannot_make_stale_data_fresh(self):
        owner = SharedInventory(self.root, owner=True, clock=lambda: self.now)
        reader = SharedInventory(self.root, clock=lambda: self.now)
        loader = Mock(return_value={'windows': []})
        owner.get('top', loader, ttl=5)
        self.now += 6
        owner.heartbeat()
        with self.assertRaises(InventoryUnavailable):
            reader.get('top', loader, ttl=5)
        self.assertIsNone(reader.peek('top', max_age=5))
        self.assertEqual(reader.peek('top', max_age=30), {'windows': []})
        self.assertEqual(loader.call_count, 1)
        owner.get('top', loader, ttl=5)
        self.assertEqual(reader.get('top', loader, ttl=5), {'windows': []})
        self.assertEqual(loader.call_count, 2)

    def test_multiple_os_processes_share_one_slow_scan(self):
        context = multiprocessing.get_context('spawn')
        children = [context.Process(target=collect_once, args=(str(self.root),)) for _ in range(4)]
        for child in children:
            child.start()
        for child in children:
            child.join(timeout=10)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['top'])

    def test_reader_can_fill_topology_gap_without_starting_a_process_scan(self):
        owner = SharedInventory(self.root, owner=True, clock=lambda: self.now)
        reader = SharedInventory(self.root, clock=lambda: self.now)
        tree = Mock(return_value={'windows': []})
        owner.get('tree', tree, ttl=1)
        self.now += 2
        owner.heartbeat()
        self.assertEqual(reader.get('tree', tree, ttl=1), {'windows': []})
        self.assertEqual(tree.call_count, 2)

    def test_send_reader_joins_running_tree_refresh_without_second_collection(self):
        owner = SharedInventory(self.root)
        reader = SharedInventory(self.root)
        entered, finish = threading.Event(), threading.Event()
        def collect():
            entered.set()
            self.assertTrue(finish.wait(2))
            return {'windows': [{'id': 'current'}]}
        thread = threading.Thread(target=lambda: owner.get('tree', collect, ttl=1))
        thread.start()
        self.assertTrue(entered.wait(2))
        release = threading.Timer(.05, finish.set)
        release.start()
        duplicate = Mock(side_effect=AssertionError('duplicate collection'))
        try:
            self.assertEqual(reader.get('tree', duplicate, ttl=1, wait_timeout=1),
                             {'windows': [{'id': 'current'}]})
            duplicate.assert_not_called()
        finally:
            finish.set()
            thread.join(2)
            release.join(2)

    def test_bounded_tree_wait_never_uses_expired_snapshot(self):
        inventory = SharedInventory(self.root)
        inventory._write('tree', {'value': {'old': True}, 'collected_at':time.time()-10})
        loader = Mock()
        with (inventory.root / 'tree.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with self.assertRaisesRegex(InventoryUnavailable, 'tree refresh pending'):
                inventory.get('tree', loader, ttl=1, wait_timeout=.05)
            self.assertLess(time.monotonic()-started, .5)
        loader.assert_not_called()

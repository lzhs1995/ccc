"""A finished FD read must not retain capacity for a blocked final guard."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from ccc_standby_prepare import InventoryReader


class InventoryCompletionTests(unittest.TestCase):
    def run_final_guard(self, *, fail):
        local = threading.local()
        in_final, release_final, second_read = (threading.Event() for _ in range(3))
        reads = []

        def allowed():
            if getattr(local, 'read_done', False) and local.index == 0:
                in_final.set()
                if not release_final.wait(3):
                    raise TimeoutError('test final guard release missing')
                if fail:
                    raise PermissionError('owner changed after read')
            return True

        def read(index):
            reads.append(index)
            local.read_done = True
            if index == 1:
                second_read.set()
            return {'index': index}

        reader = InventoryReader(read, allowed, limit=1)

        def call(index):
            local.index, local.read_done = index, False
            return reader(index)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(call, 0)
            try:
                self.assertTrue(in_final.wait(2))
                self.assertFalse(first.done(), 'result escaped before final lifetime check')
                second = pool.submit(call, 1)
                progressed = second_read.wait(.5)
                first_still_blocked = not first.done()
            finally:
                release_final.set()
            if fail:
                with self.assertRaisesRegex(PermissionError, 'owner changed'):
                    first.result(timeout=2)
            else:
                self.assertEqual(first.result(timeout=2), {'index': 0})
            self.assertEqual(second.result(timeout=2), {'index': 1})
        self.assertEqual(reads, [0, 1])
        self.assertEqual(reader.capacity, 1)
        self.assertFalse(reader.waiters)
        self.assertTrue(first_still_blocked)
        self.assertTrue(progressed, 'finished FD read held capacity during final lifetime check')

    def test_next_fresh_read_progresses_while_first_result_waits_for_final_guard(self):
        self.run_final_guard(fail=False)

    def test_failed_final_guard_discards_first_result_without_stalling_next_read(self):
        self.run_final_guard(fail=True)


if __name__ == '__main__':
    unittest.main()

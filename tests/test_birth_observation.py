"""Preserve the exact first libproc read without changing identity decisions."""
import ctypes
import unittest
from unittest.mock import patch
import ccc_guard_scope as scope


class BirthObservationTests(unittest.TestCase):
    def test_raw_result_and_identity_decision(self):
        for returned, status, pid, expected in (
                (0, 0, 0, None), (1, 2, 123, None),
                (ctypes.sizeof(scope.native._BsdInfo()), 5, 123, None),
                (ctypes.sizeof(scope.native._BsdInfo()), 2, 124, None),
                (ctypes.sizeof(scope.native._BsdInfo()), 2, 123, [4, 5])):
            with self.subTest(returned=returned, status=status, pid=pid):
                def read(_, flavor, arg, pointer, size):
                    info = ctypes.cast(pointer, ctypes.POINTER(scope.native._BsdInfo)).contents
                    info.pid, info.status = pid, status
                    info.start_sec, info.start_usec = 4, 5
                    return returned
                record = {}
                with patch.object(scope.native, '_proc_pidinfo', side_effect=read) as call:
                    self.assertEqual(scope.birth(123, observation=record), expected)
                self.assertEqual(call.call_count, 1)
                self.assertEqual(record['returned_bytes'], returned)
                self.assertEqual(record['status'], status)
                self.assertEqual(record['pid'], pid)


if __name__ == '__main__':
    unittest.main()

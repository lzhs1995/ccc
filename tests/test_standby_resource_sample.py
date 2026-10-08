import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import standby_resource_sample as sample


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.rows = [dict(pid=123, birth=[100, 2])]

    def capture(self, **kw):
        return sample.capture(self.rows, self.root, expected_count=1, **kw)

    def test_regular_files_hardlinks_and_nested_directories(self):
        (self.root/'a').write_bytes(b'abc')
        (self.root/'nested').mkdir()
        os.link(self.root/'a', self.root/'nested'/'b')
        result = sample.allocated_tree(self.root, lambda: None)
        self.assertEqual(result['regular_files'], 1)
        self.assertEqual(result['logical_bytes'], 3)
        self.assertFalse(result['unique_physical_bytes_proven'])

    def test_symlink_rejected(self):
        (self.root/'link').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'link'):
            sample.allocated_tree(self.root, lambda: None)

    def test_directory_replaced_before_open_is_not_followed(self):
        child = self.root/'child'
        child.mkdir()
        real_open = os.open
        def changed(path, flags, **kw):
            if path == 'child':
                child.rmdir()
                child.symlink_to(self.root, target_is_directory=True)
            return real_open(path, flags, **kw)
        with patch.object(sample.os, 'open', side_effect=changed):
            with self.assertRaises(OSError):
                sample.allocated_tree(self.root, lambda: None)

    def test_invalid_budgets_rejected_without_process_query(self):
        with patch.object(sample.subprocess, 'run') as run:
            for seconds in (True, float('nan'), float('inf'), 0, -1, 121, '30'):
                with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                    self.capture(seconds=seconds)
            run.assert_not_called()

    def test_exact_count_required(self):
        with self.assertRaises(ValueError):
            sample.capture(self.rows, self.root)

    def test_pid_reuse_before_or_after_read_rejected(self):
        for births in ([[101, 2]], [[100, 2], [101, 2]]):
            with self.subTest(births=births), patch('ccc_guard_scope.birth', side_effect=births), \
                 patch.object(sample, 'descriptors', return_value=4), \
                 patch.object(sample.subprocess, 'run', return_value=SimpleNamespace(
                     returncode=0, stderr='', stdout='123 10\n')):
                with self.assertRaisesRegex(ValueError, 'process'):
                    self.capture()

    def test_incomplete_or_duplicate_ps_rejected(self):
        for stdout in ('', '123 10\n123 10\n', '124 10\n'):
            with self.subTest(stdout=stdout), patch('ccc_guard_scope.birth', return_value=[100, 2]), \
                 patch.object(sample.subprocess, 'run', return_value=SimpleNamespace(
                     returncode=0, stderr='', stdout=stdout)):
                with self.assertRaises(ValueError):
                    self.capture()

    def test_deadline_after_birth_prevents_ps(self):
        with patch('ccc_guard_scope.birth', return_value=[100, 2]), \
             patch.object(sample.time, 'monotonic', side_effect=[0, 0, 31]), \
             patch.object(sample.subprocess, 'run') as run:
            with self.assertRaises(TimeoutError):
                self.capture()
            run.assert_not_called()

    def test_success_keeps_measurement_limits(self):
        with patch('ccc_guard_scope.birth', return_value=[100, 2]), \
             patch.object(sample, 'descriptors', return_value=4), \
             patch.object(sample.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stderr='', stdout='123 10\n')):
            result = self.capture()
        self.assertEqual(result['rss_sum_bytes'], 10240)
        self.assertEqual(result['fd_sum'], 4)
        self.assertFalse(result['full_500_acceptance'])

    def test_descriptor_truncation_rejected(self):
        import ccc_codex_queue as native
        import ctypes
        size = ctypes.sizeof(native._FdInfo)
        with patch.object(native, '_proc_pidinfo', side_effect=[size, size * 65]):
            with self.assertRaisesRegex(OSError, 'incomplete'):
                sample.descriptors(123)

    def test_auxiliary_shared_pid_is_rejected(self):
        with patch('ccc_guard_scope.birth', return_value=[100, 2]):
            with self.assertRaisesRegex(ValueError, 'unique'):
                self.capture(auxiliaries=self.rows)

    def test_auxiliaries_counted_once_with_native_roles(self):
        with patch('ccc_guard_scope.birth', return_value=[100, 2]), \
             patch.object(sample, 'descriptors', return_value=4), \
             patch.object(sample.subprocess, 'run', return_value=SimpleNamespace(
                 returncode=0, stderr='', stdout='123 10\n456 20\n')):
            result = self.capture(auxiliaries=[dict(pid=456, birth=[100, 2])])
        self.assertEqual(result['native_process_count'], 1)
        self.assertEqual(result['auxiliary_process_count'], 1)
        self.assertEqual(result['rss_sum_bytes'], 30720)
        self.assertEqual([r['role'] for r in result['processes']], ['native', 'auxiliary'])


if __name__ == '__main__':
    unittest.main()

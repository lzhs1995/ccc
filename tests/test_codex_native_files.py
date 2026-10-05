"""Native file discovery must preserve original-writer and race guards."""
import ctypes
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import ccc_codex_queue as native


class NativeFileTests(unittest.TestCase):
    def setUp(self):
        self.entries = [(3, 1), (4, 1), (5, 2)]
        self.files = {3: (3, b'/tmp/current.jsonl'), 4: (1, b'/tmp/history.jsonl')}

    def descriptors(self, pid, flavor, arg, buffer, size):
        self.assertEqual((pid, flavor, arg), (123, 1, 0))
        if buffer is not None:
            rows = ctypes.cast(buffer, ctypes.POINTER(native._FdInfo))
            for index, (fd, kind) in enumerate(self.entries):
                rows[index].fd, rows[index].kind = fd, kind
        return len(self.entries) * ctypes.sizeof(native._FdInfo)

    def vnode(self, pid, fd, flavor, buffer, size):
        self.assertEqual((pid, flavor, size), (123, 2, 1200))
        info = ctypes.cast(buffer, ctypes.POINTER(native._VnodeFdInfo)).contents
        info.openflags, info.path = self.files[fd]
        return size

    def test_only_writable_vnodes_supply_paths_without_spawning_lsof(self):
        with patch.object(native, '_proc_pidinfo', side_effect=self.descriptors), \
                patch.object(native, '_proc_pidfdinfo', side_effect=self.vnode), \
                patch.object(native.subprocess, 'run', side_effect=AssertionError('lsof spawned')):
            self.assertEqual(native.process_writable_files(123), {Path('/tmp/current.jsonl').resolve()})

    def test_unreadable_or_truncated_native_inventory_does_not_fall_back(self):
        with patch.object(native, '_proc_pidinfo', side_effect=self.descriptors) as descriptors, \
                patch.object(native, '_proc_pidfdinfo', side_effect=self.vnode) as vnode, \
                patch.object(native.subprocess, 'run', side_effect=AssertionError('unsafe fallback')):
            for count in (0, -1, 1, 8 * 20000):
                descriptors.side_effect = None
                descriptors.return_value = count
                with self.assertRaises(OSError):
                    native.process_writable_files(123)
            descriptors.side_effect = self.descriptors
            vnode.side_effect = None
            vnode.return_value = 1199
            with self.assertRaises(OSError):
                native.process_writable_files(123)

    def test_descriptor_change_or_missing_path_cannot_supply_evidence(self):
        def changing(*args):
            result = self.vnode(*args)
            self.entries.append((6, 1))
            return result
        with patch.object(native, '_proc_pidinfo', side_effect=self.descriptors), \
                patch.object(native, '_proc_pidfdinfo', side_effect=changing):
            with self.assertRaises(native.VnodeInventoryChanged):
                native.process_writable_files(123)
        self.entries = [(3, 1)]
        self.files[3] = (3, b'')
        with patch.object(native, '_proc_pidinfo', side_effect=self.descriptors), \
                patch.object(native, '_proc_pidfdinfo', side_effect=self.vnode):
            with self.assertRaises(OSError):
                native.process_writable_files(123)

    def test_portable_fallback_keeps_access_filter_and_timeout(self):
        reply = subprocess.CompletedProcess([], 0, 'f3\nau\nn/tmp/current.jsonl\nf4\nar\nn/tmp/history.jsonl\n', '')
        with patch.object(native, '_proc_pidfdinfo', None), \
                patch.object(native.subprocess, 'run', return_value=reply) as run:
            self.assertEqual(native.process_writable_files(123), {Path('/tmp/current.jsonl').resolve()})
            self.assertEqual(run.call_args.kwargs['timeout'], 2)
            run.return_value = subprocess.CompletedProcess([], 1, '', '')
            with self.assertRaises(OSError):
                native.process_writable_files(123)

    def test_short_vnode_records_fd_phase_count_and_current_errno(self):
        self.entries = [(3, 1)]
        for phase, fail_at in [('initial', 1), ('verification', 2)]:
            calls = 0
            def short(*args):
                nonlocal calls
                calls += 1
                if calls == fail_at:
                    ctypes.set_errno(9)
                    return 0
                return self.vnode(*args)
            with self.subTest(phase=phase), \
                    patch.object(native, '_proc_pidinfo', side_effect=self.descriptors), \
                    patch.object(native, '_proc_pidfdinfo', side_effect=short):
                with self.assertRaises(OSError) as caught:
                    native.process_writable_files(123, identities=True)
                self.assertEqual(caught.exception.errno, 9)
                self.assertIn(f'pid=123 fd=3 phase={phase} returned=0 expected=1200', str(caught.exception))
                self.assertEqual(calls, fail_at)

    def test_short_vnode_without_errno_does_not_report_stale_errno(self):
        ctypes.set_errno(13)
        with patch.object(native, '_proc_pidinfo', side_effect=self.descriptors), \
                patch.object(native, '_proc_pidfdinfo', return_value=1199):
            with self.assertRaises(OSError) as caught:
                native.process_writable_files(123)
            self.assertEqual(caught.exception.errno, 0)
            self.assertIn('returned=1199 expected=1200', str(caught.exception))

    def test_reused_descriptor_number_cannot_supply_stale_writer(self):
        self.entries = [(3, 1)]
        def reused(*args):
            result = self.vnode(*args)
            self.files[3] = (3, b'/tmp/different.jsonl')
            return result
        with patch.object(native, '_proc_pidinfo', side_effect=self.descriptors), \
                patch.object(native, '_proc_pidfdinfo', side_effect=reused):
            with self.assertRaises(native.VnodeInventoryChanged):
                native.process_writable_files(123)

    def test_duplicate_descriptors_resolve_twice_per_observation_without_cache_reuse(self):
        self.entries = [(fd, 1) for fd in range(3, 67)]
        self.files = {fd: (3, b'/tmp/current.jsonl') for fd, _ in self.entries}
        with patch.object(native, '_proc_pidinfo', side_effect=self.descriptors), \
                patch.object(native, '_proc_pidfdinfo', side_effect=self.vnode) as vnode, \
                patch.object(Path, 'resolve', return_value=Path('/private/tmp/current.jsonl')) as resolve:
            for _ in range(2):
                self.assertEqual(native.process_writable_files(123, identities=True),
                                 {Path('/private/tmp/current.jsonl'): {'device': 0, 'inode': 0}})
            self.assertEqual(resolve.call_count, 4)
            self.assertEqual(vnode.call_count, 256)

    def test_canonical_path_drift_is_rejected(self):
        with patch.object(native, '_proc_pidinfo', side_effect=self.descriptors), \
                patch.object(native, '_proc_pidfdinfo', side_effect=self.vnode), \
                patch.object(Path, 'resolve', side_effect=[Path('/a'), Path('/b')]):
            with self.assertRaises(native.VnodeInventoryChanged):
                native.process_writable_files(123)

    def test_canonical_collision_with_distinct_inode_is_rejected(self):
        self.files[4] = (3, b'/tmp/alias.jsonl')
        def distinct(*args):
            size = self.vnode(*args)
            info = ctypes.cast(args[3], ctypes.POINTER(native._VnodeFdInfo)).contents
            info.vnode[8] = args[1]
            return size
        with patch.object(native, '_proc_pidinfo', side_effect=self.descriptors), \
                patch.object(native, '_proc_pidfdinfo', side_effect=distinct), \
                patch.object(Path, 'resolve', return_value=Path('/same')):
            with self.assertRaises(native.VnodeInventoryChanged):
                native.process_writable_files(123, identities=True)


if __name__ == '__main__':
    unittest.main()

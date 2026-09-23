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
            with self.assertRaises(OSError):
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

    def test_reused_descriptor_number_cannot_supply_stale_writer(self):
        self.entries = [(3, 1)]
        def reused(*args):
            result = self.vnode(*args)
            self.files[3] = (3, b'/tmp/different.jsonl')
            return result
        with patch.object(native, '_proc_pidinfo', side_effect=self.descriptors), \
                patch.object(native, '_proc_pidfdinfo', side_effect=reused):
            with self.assertRaises(OSError):
                native.process_writable_files(123)


if __name__ == '__main__':
    unittest.main()

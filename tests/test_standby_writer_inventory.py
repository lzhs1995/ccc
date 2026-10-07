"""Two complete inventories tolerate readers, never unknown/new writers."""
import ctypes
from pathlib import Path
import unittest
from unittest.mock import patch
import ccc_codex_queue as native


class WriterInventoryTests(unittest.TestCase):
    def run_pair(self, initial, final, *, short=None, writer_only=True):
        calls = 0
        current = initial
        observed = []
        def descriptors(pid, flavor, arg, buffer, size):
            nonlocal calls, current
            calls += 1
            current = initial if calls <= 2 else final
            if buffer is not None:
                entries = ctypes.cast(buffer, ctypes.POINTER(native._FdInfo))
                for index, fd in enumerate(current):
                    entries[index].fd, entries[index].kind = fd, 1
            return len(current) * ctypes.sizeof(native._FdInfo)
        def vnode(pid, fd, flavor, buffer, size):
            phase = 'initial' if calls <= 2 else 'verification'
            observed.append((phase, fd))
            if short == (phase, fd):
                return size - 1
            info = ctypes.cast(buffer, ctypes.POINTER(native._VnodeFdInfo)).contents
            access, path, inode = current[fd]
            info.openflags, info.path = access, path
            info.vnode[8] = inode
            return size
        with patch.object(native, '_proc_pidinfo', side_effect=descriptors), \
                patch.object(native, '_proc_pidfdinfo', side_effect=vnode):
            result = native.process_writable_files(123, identities=True,
                                                   writer_identity_only=writer_only)
        self.assertCountEqual(observed, [('initial', fd) for fd in initial] +
                                  [('verification', fd) for fd in final])
        return result

    def setUp(self):
        self.writer = (3, b'/tmp/standby-original.jsonl', 12)
        self.reader = (1, b'/tmp/reader-a', 13)
        self.other = (1, b'/tmp/reader-b', 14)

    def test_readonly_reuse_keeps_original_writer(self):
        result = self.run_pair({3:self.writer,35:self.reader}, {3:self.writer,35:self.other})
        self.assertEqual(result, {Path('/tmp/standby-original.jsonl').resolve():
                                 {'device':0,'inode':12}})

    def test_added_reader_is_actually_read(self):
        self.run_pair({3:self.writer}, {3:self.writer,35:self.reader})

    def test_removed_reader_keeps_writer(self):
        self.run_pair({3:self.writer,35:self.reader}, {3:self.writer})

    def test_added_writer_including_duplicate_path_rejected(self):
        for new in (self.writer, (2,b'/tmp/other-writer',22)):
            with self.subTest(new=new), self.assertRaises(native.VnodeInventoryChanged):
                self.run_pair({3:self.writer}, {3:self.writer,35:new})

    def test_removed_writer_rejected(self):
        with self.assertRaises(native.VnodeInventoryChanged):
            self.run_pair({3:self.writer,35:self.reader}, {35:self.reader})

    def test_readonly_to_writer_rejected(self):
        with self.assertRaises(native.VnodeInventoryChanged):
            self.run_pair({3:self.writer,35:self.reader}, {3:self.writer,35:self.writer})

    def test_writer_to_readonly_rejected(self):
        with self.assertRaises(native.VnodeInventoryChanged):
            self.run_pair({3:self.writer}, {3:self.reader})

    def test_writer_inode_path_or_access_drift_rejected(self):
        for changed in ((3,self.writer[1],99),(3,b'/tmp/other',12),(2,self.writer[1],12)):
            with self.subTest(changed=changed), self.assertRaises(native.VnodeInventoryChanged):
                self.run_pair({3:self.writer}, {3:changed})

    def test_writer_fd_relocation_rejected(self):
        with self.assertRaises(native.VnodeInventoryChanged):
            self.run_pair({3:self.writer}, {35:self.writer})

    def test_incomplete_new_readonly_fd_rejected(self):
        with self.assertRaises(native.IncompleteVnodeRead):
            self.run_pair({3:self.writer}, {3:self.writer,35:self.reader},
                          short=('verification',35))

    def test_incomplete_old_reader_rejected(self):
        with self.assertRaises(native.IncompleteVnodeRead):
            self.run_pair({3:self.writer,35:self.reader}, {3:self.writer},
                          short=('initial',35))

    def test_default_still_rejects_reader_identity_drift(self):
        with self.assertRaises(native.VnodeInventoryChanged):
            self.run_pair({3:self.writer,35:self.reader}, {3:self.writer,35:self.other},
                          writer_only=False)

    def test_writer_mode_requires_native_inventory(self):
        with patch.object(native,'_proc_pidfdinfo',None), \
                patch.object(native.subprocess,'run',side_effect=AssertionError('fallback')):
            with self.assertRaises(OSError):
                native.process_writable_files(123,writer_identity_only=True)

"""Capacity changes retain content, alias and registration failure checks."""
import errno
import os
from pathlib import Path
import resource
import select
import tempfile
import unittest
from unittest.mock import patch

from ccc_standby_generation import PRODUCTION_BOUNDS, SCOPES, StandbyGeneration, _VnodeWatch


class CapacityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.file = self.root / 'a'
        self.file.write_bytes(b'original')
        self.alias = self.root / 'b'
        os.link(self.file, self.alias)
        self.roots = {scope: [self.root] for scope in SCOPES}

    def pin(self, **kwargs):
        p = StandbyGeneration(self.roots, lambda: {'profile': 'original'}, **kwargs)
        self.addCleanup(p.close)
        return p

    def test_measure_does_not_read_contents_or_create_watcher(self):
        with patch.object(Path, 'open', side_effect=AssertionError('content read')), \
                patch('ccc_standby_generation._VnodeWatch', side_effect=AssertionError('watch')):
            report = StandbyGeneration.measure(self.roots)
        self.assertEqual(report['unique_file_bytes'], 8)
        self.assertEqual(report['file_inodes'], 1)
        self.assertEqual(report['read_bytes_total'], 0)
        self.assertEqual(report['watch_paths'] - report['watch_inodes'], 1)

    def test_hardlink_reads_once_and_retains_both_paths(self):
        pin = self.pin(max_read_bytes=8)
        self.assertEqual(pin.capacity['read_bytes_total'], 8)
        self.assertIn(str(self.file), pin._inventory_paths)
        self.assertIn(str(self.alias), pin._inventory_paths)
        self.assertEqual(pin.current(), pin.value)
        self.assertEqual(pin.read_bytes_total, 8)

    def test_alias_replacement_refuses_even_equal_bytes(self):
        pin = self.pin()
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'original')
        os.replace(replacement, self.alias)
        with self.assertRaises(ValueError): pin.current()
        with self.assertRaises(ValueError): pin.current()

    def test_alias_content_roundtrip_refuses(self):
        pin = self.pin()
        self.alias.write_bytes(b'mutation')
        self.alias.write_bytes(b'original')
        with self.assertRaises(ValueError): pin.current()

    def test_change_between_alias_reads_is_not_hidden_by_cache(self):
        original = Path.open
        changed = False
        def opening(path, *args, **kwargs):
            nonlocal changed
            if path == self.file and args == ('rb',) and not changed:
                changed = True
                with original(self.alias, 'wb') as stream: stream.write(b'mutation')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', opening):
            with self.assertRaises(ValueError): self.pin()

    def test_read_budget_reports_actual_root_file_and_bytes(self):
        with self.assertRaises(ValueError) as caught: self.pin(max_read_bytes=7)
        detail = caught.exception.dependency_budget
        self.assertEqual(detail['root'], str(self.root))
        self.assertEqual(detail['path'], str(self.file))
        self.assertEqual(detail['file_bytes'], 8)
        self.assertEqual(detail['read_bytes'], 8)
        self.assertEqual(detail['max_read_bytes'], 7)

    def test_measure_keeps_entry_limit(self):
        with self.assertRaises(ValueError) as caught:
            StandbyGeneration.measure(self.roots, max_entries=1)
        self.assertEqual(caught.exception.dependency_budget['max_entries'], 1)


@unittest.skipUnless(hasattr(select, 'kqueue') and hasattr(os, 'O_SYMLINK'), 'Darwin events')
class WatchCapacityTests(unittest.TestCase):
    setUp = CapacityTests.setUp
    pin = CapacityTests.pin
    def watch(self, paths, limit=40000, missing=None):
        w = _VnodeWatch(paths, limit, missing)
        self.addCleanup(w.close)
        return w

    def test_watch_one_inode_one_fd_and_strongest_alias_mask(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                paths = [(str(self.file), 'identity'), (str(self.alias), 'content')]
                w = self.watch(dict(reversed(paths) if reverse else paths), limit=1)
                self.assertEqual(len(w._fds), 1)
                self.assertEqual(len(w._entries[w._fds[0]]), 2)
                w.check()
                self.alias.write_bytes(b'changed!')
                with self.assertRaises(ValueError): w.check()
                w.close()

    def test_watch_alias_replace_latches_without_rescan(self):
        pin = self.pin(use_events=True, max_watch_files=40000)
        with patch.object(pin, '_snapshot', side_effect=AssertionError('hot scan')):
            self.alias.unlink()
            os.link(self.file, self.alias)
            with self.assertRaises(ValueError): pin.current()

    def test_missing_children_of_all_directory_aliases_checked(self):
        directory = self.root / 'dir'; directory.mkdir()
        link = self.root / 'link'; link.symlink_to(directory, target_is_directory=True)
        child = directory / 'child'; child.mkdir()
        alias = link / 'child'
        w = self.watch({str(child): 'identity', str(alias): 'missing'}, limit=1,
                       missing={str(alias): {str(alias / 'optional')}})
        (child / 'unrelated').write_text('unrelated')
        w.check()
        (child / 'optional').write_text('new')
        with self.assertRaisesRegex(ValueError, 'optional'): w.check()
        (child / 'optional').unlink()
        with self.assertRaises(ValueError): w.check()

    def test_soft_descriptor_bound_refuses_before_queue(self):
        with patch.object(resource, 'getrlimit', return_value=(1024, 65536)), \
                patch.object(select, 'kqueue', side_effect=AssertionError('queue opened')):
            with self.assertRaises(ValueError) as caught:
                self.watch({str(self.file): 'content'})
        self.assertEqual(caught.exception.dependency_budget['watch_inodes'], 1)

    def test_explicit_inode_ceiling_refuses_before_queue(self):
        distinct = self.root / 'c'; distinct.write_text('distinct')
        with patch.object(select, 'kqueue', side_effect=AssertionError('queue opened')):
            with self.assertRaises(ValueError) as caught:
                self.watch({str(self.file): 'content', str(self.alias): 'content',
                            str(distinct): 'content'}, limit=1)
        self.assertEqual(caught.exception.dependency_budget['watch_paths'], 3)
        self.assertEqual(caught.exception.dependency_budget['watch_inodes'], 2)

    def test_open_failure_closes_acquired_fds_and_queue(self):
        distinct = self.root / 'c'; distinct.write_text('distinct')
        real_open, real_queue = os.open, select.kqueue
        opened, queues = [], []
        def opening(*args):
            if opened: raise OSError(errno.EMFILE, 'fixture capacity exhausted')
            fd = real_open(*args); opened.append(fd); return fd
        def queue():
            q = real_queue(); queues.append(q); return q
        with patch.object(os, 'open', opening), patch.object(select, 'kqueue', queue):
            with self.assertRaises(OSError):
                self.watch({str(self.file): 'content', str(distinct): 'content'})
        self.assertTrue(queues[0].closed)
        with self.assertRaises(OSError): os.fstat(opened[0])

    def test_replaced_alias_during_registration_closes_fd(self):
        real_open = os.open
        opened = []
        def opening(*args):
            fd = real_open(*args); opened.append(fd)
            self.alias.unlink(); self.alias.write_bytes(b'original')
            return fd
        with patch.object(os, 'open', opening):
            with self.assertRaises(ValueError):
                self.watch({str(self.file): 'content', str(self.alias): 'content'})
        with self.assertRaises(OSError): os.fstat(opened[0])

    def test_production_bounds_keep_full_graph_and_hot_path(self):
        pin = self.pin(**PRODUCTION_BOUNDS)
        self.assertEqual(len(pin._watch._fds), pin.capacity['watch_inodes'])
        with patch.object(pin, '_snapshot', side_effect=AssertionError('hot scan')):
            self.assertEqual(pin.current(), pin.value)
        fds = list(pin._watch._fds)
        pin.close()
        for fd in fds:
            with self.assertRaises(OSError): os.fstat(fd)


if __name__ == '__main__':
    unittest.main()

"""Real Darwin vnode events: access alone versus dependency mutations."""
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ccc_standby_generation import _VnodeWatch, _access_stable_stamp


@unittest.skipUnless(hasattr(select, 'kqueue'), 'Darwin vnode events')
class AccessEventTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.file = self.root / 'program'
        shutil.copyfile('/usr/bin/true', self.file)
        self.file.chmod(0o700)
        self.alias = self.root / 'alias'
        os.link(self.file, self.alias)
        info = self.file.stat()
        os.utime(self.file, ns=(1_000_000_000, info.st_mtime_ns))
        self.before = self.file.stat()
        self.watch = _VnodeWatch({str(self.file): 'content', str(self.alias): 'content'}, 2)
        self.addCleanup(self.watch.close)

    def reject(self):
        with self.assertRaises(ValueError):
            self.watch.check()
        with self.assertRaises(ValueError):
            self.watch.check()

    def test_read_only_access(self):
        self.file.read_bytes()
        after = self.file.stat()
        self.assertNotEqual(after.st_atime_ns, self.before.st_atime_ns)
        self.assertEqual(_access_stable_stamp(after), _access_stable_stamp(self.before))
        self.watch.check()
        self.watch.check()

    def test_execute_only_access(self):
        subprocess.run([str(self.file)], check=True, timeout=5)
        after = self.file.stat()
        self.assertNotEqual(after.st_atime_ns, self.before.st_atime_ns)
        self.assertEqual(_access_stable_stamp(after), _access_stable_stamp(self.before))
        self.watch.check()

    def test_repeated_execute_only_access(self):
        for _ in range(20):
            subprocess.run([str(self.file)], check=True, timeout=5)
            self.assertEqual(_access_stable_stamp(self.file.stat()),
                             _access_stable_stamp(self.before))
            self.watch.check()

    def test_delayed_attrib_with_identical_access_time(self):
        queue = Mock()
        queue.control.return_value = [select.kevent(self.watch._fds[0],
            filter=select.KQ_FILTER_VNODE, fflags=select.KQ_NOTE_ATTRIB)]
        with patch.object(self.watch, '_queue', queue):
            self.watch.check()
            self.watch.check()

    def test_same_mode_chmod_with_access_refuses(self):
        self.file.read_bytes()
        self.file.chmod(0o700)
        self.reject()

    def test_read_between_fd_and_alias_observations(self):
        queue = Mock()
        queue.control.return_value = [select.kevent(self.watch._fds[0],
            filter=select.KQ_FILTER_VNODE, fflags=select.KQ_NOTE_ATTRIB)]
        original = Path.lstat
        def read_then_stat(path, *args, **kwargs):
            self.file.read_bytes()
            return original(path, *args, **kwargs)
        with patch.object(self.watch, '_queue', queue), patch.object(Path, 'lstat', read_then_stat):
            self.watch.check()
        self.assertNotEqual(self.file.stat().st_atime_ns, self.before.st_atime_ns)

    def test_mutation_after_last_alias_observation_refuses(self):
        queue = Mock()
        queue.control.return_value = [select.kevent(self.watch._fds[0],
            filter=select.KQ_FILTER_VNODE, fflags=select.KQ_NOTE_ATTRIB)]
        original = Path.lstat
        calls = []
        def stat_then_mutate(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            calls.append(path)
            if len(calls) == 2:
                self.file.chmod(0o600)
            return info
        with patch.object(self.watch, '_queue', queue), patch.object(Path, 'lstat', stat_then_mutate):
            self.reject()

    def test_mode_roundtrip_with_access_refuses(self):
        self.file.read_bytes()
        self.file.chmod(0o600)
        self.file.chmod(0o700)
        self.reject()

    def test_content_roundtrip_with_access_refuses(self):
        original = self.file.read_bytes()
        self.file.write_bytes(b'changed')
        self.file.write_bytes(original)
        self.reject()

    def test_alias_replaced_after_access_refuses(self):
        original = self.file.read_bytes()
        self.alias.unlink()
        self.alias.write_bytes(original)
        self.reject()

    def test_xattr_roundtrip_with_access_refuses(self):
        self.file.read_bytes()
        subprocess.run(['/usr/bin/xattr', '-w', 'user.ccc-test', 'x', str(self.file)], check=True, timeout=5)
        subprocess.run(['/usr/bin/xattr', '-d', 'user.ccc-test', str(self.file)], check=True, timeout=5)
        self.reject()

    def test_atime_set_explicitly_refuses(self):
        os.utime(self.file, ns=(2_000_000_000, self.before.st_mtime_ns))
        self.reject()


if __name__ == '__main__':
    unittest.main()

"""Permission-limited root login is a boundary only with a stable kernel child."""
import copy
import ctypes
import errno
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import cmux_daemon_identity as d

READ_PROCESS = d.process

class PermissionLoginTests(unittest.TestCase):
    def run_boundary(self, *, error=errno.EPERM, allowed=True, child=True,
                     first=None, second=None, path='/usr/bin/login',
                     path_final=None, stat_uid=0, mode=0o100755,
                     before=None, after=None, short_count=None):
        original = dict(pid=20, ppid=9, birth=[100, 99], argv=['/bin/zsh'],
                        env={'CMUX_SURFACE_ID': 'real'}, executable='/bin/zsh',
                        file_identity=[1, 2, 3, 4])
        pinned = original if child is True else child
        child_reads = iter([dict(original, **(before or {})), dict(original, **(after or {}))])
        self.short_reads = 0
        self.path_reads = 0
        def info(pid, flavor, arg, ptr, size):
            self.assertEqual(pid, 9)
            if flavor == 3:
                ctypes.set_errno(error)
                return 0
            self.assertEqual(flavor, 13)
            self.short_reads += 1
            b = ptr._obj
            b.pid, b.ppid, b.pgid, b.uid, b.ruid, b.svuid, b.status = pid, 8, 9, 0, 501, 0, 2
            for key, value in ((first if self.short_reads == 1 else second) or {}).items():
                setattr(b, key, value)
            return size if short_count is None else short_count
        def pidpath(pid, buffer, size):
            self.path_reads += 1
            value = path if self.path_reads == 1 or path_final is None else path_final
            buffer.value = value.encode()
            return len(buffer.value)
        lib = SimpleNamespace(proc_pidinfo=Mock(side_effect=info), proc_pidpath=Mock(side_effect=pidpath))
        def process(pid, **kw):
            if pid == 20:
                return next(child_reads)
            return READ_PROCESS(pid, allow_system_login=allowed)
        with patch.object(d.sys, 'platform', 'darwin'), patch.object(d.os, 'getuid', return_value=501), \
             patch.object(d.ctypes, 'CDLL', return_value=lib), patch.object(d, 'process', side_effect=process), \
             patch.object(d.Path, 'stat', return_value=SimpleNamespace(st_uid=stat_uid, st_mode=mode)):
            return d._ancestor_process(9, pinned)

    def test_eperm_and_eacces_accept_only_pinned_system_boundary(self):
        for error in (errno.EPERM, errno.EACCES):
            with self.subTest(error=error):
                result = self.run_boundary(error=error)
                self.assertEqual(result['child_birth'], [100, 99])
                self.assertEqual(result['identity_source'], 'short_bsd_stable_child')
                self.assertEqual(self.short_reads, 2)
                self.assertEqual(self.path_reads, 2)
                self.assertNotIn('birth', result)  # Never fabricate login birth.
                self.assertNotIn('env', result)

    def test_missing_or_unbound_child_denies_before_short_read(self):
        for child in (None, {}, {'pid': 20, 'ppid': 8, 'birth': [100, 99], 'argv': ['zsh']}):
            with self.subTest(child=child), self.assertRaises(d.IdentityError):
                self.run_boundary(child=child)
            self.assertEqual(self.short_reads, 0)

    def test_same_user_full_reader_does_not_gain_root_fallback(self):
        with self.assertRaises(d.IdentityError):
            self.run_boundary(allowed=False)
        self.assertEqual(self.short_reads, 0)

    def test_unknown_or_absent_full_identity_never_falls_back(self):
        for error in (0, errno.ESRCH, errno.EIO):
            with self.subTest(error=error), self.assertRaises(d.IdentityError):
                self.run_boundary(error=error)
            self.assertEqual(self.short_reads, 0)

    def test_foreign_uid_or_real_uid_denies(self):
        for fields in ({'uid': 501}, {'uid': 502}, {'ruid': 502}):
            with self.subTest(fields=fields), self.assertRaises(d.IdentityError):
                self.run_boundary(first=fields)

    def test_wrong_pid_zombie_or_no_parent_denies(self):
        for fields in ({'pid': 10}, {'status': 5}, {'ppid': 0}):
            with self.subTest(fields=fields), self.assertRaises(d.IdentityError):
                self.run_boundary(first=fields)

    def test_short_read_unknown_or_truncated_denies(self):
        for count in (0, -1, 63):
            with self.subTest(count=count), self.assertRaises(d.IdentityError):
                self.run_boundary(short_count=count)

    def test_child_already_changed_denies_before_short_read(self):
        for changed in ({'ppid': 1}, {'birth': [101, 1]}, {'env': {'CMUX_SURFACE_ID': 'forged'}}):
            with self.subTest(changed=changed), self.assertRaises(d.IdentityError):
                self.run_boundary(before=changed)
            self.assertEqual(self.short_reads, 0)

    def test_pid_reuse_or_reparent_during_read_denies(self):
        for changed in ({'ppid': 1}, {'birth': [101, 1]}, {'argv': ['/tmp/fake']}, {'env': {}}):
            with self.subTest(changed=changed), self.assertRaises(d.IdentityError):
                self.run_boundary(after=changed)

    def test_short_identity_drift_denies(self):
        for changed in ({'ppid': 7}, {'pgid': 10}, {'uid': 502}, {'ruid': 502}, {'svuid': 501}):
            with self.subTest(changed=changed), self.assertRaises(d.IdentityError):
                self.run_boundary(second=changed)

    def test_arbitrary_root_paths_and_path_drift_denied(self):
        for kwargs in ({'path': '/tmp/login'}, {'path': '/usr/bin/other'},
                       {'path_final': '/tmp/login'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(d.IdentityError):
                self.run_boundary(**kwargs)

    def test_unprotected_binary_denied(self):
        for kwargs in ({'stat_uid': 501}, {'mode': 0o100777}):
            with self.subTest(kwargs=kwargs), self.assertRaises(d.IdentityError):
                self.run_boundary(**kwargs)

    def test_ordinary_chain_rechecks_same_child_binding(self):
        child = dict(pid=20, ppid=9, birth=[100, 99], argv=['/bin/zsh'], env={}, executable='/bin/zsh')
        boundary = dict(pid=9, ppid=8, system_login=True)
        def read(pid, expected_child):
            if pid == 20:
                self.assertIsNone(expected_child)
                return copy.deepcopy(child)
            self.assertEqual(pid, 9)
            self.assertEqual(expected_child, child)
            return copy.deepcopy(boundary)
        with patch.object(d.os, 'getppid', return_value=20), patch.object(d, '_ancestor_process', side_effect=read) as reader:
            chain, daemon = d._ancestry()
            self.assertIsNone(daemon)
            self.assertEqual([r['pid'] for r in chain], [20, 9])
            self.assertEqual(reader.call_count, 4)

    def test_later_full_read_permission_refusal_never_becomes_boundary(self):
        observations = iter([True, False])
        def info(pid, flavor, arg, ptr, size):
            if not next(observations):
                ctypes.set_errno(errno.EPERM)
                return 0
            b = ptr._obj
            b.pid, b.ppid, b.uid, b.start_sec, b.start_usec, b.status = pid, 8, 501, 100, 2, 2
            return size
        lib = SimpleNamespace(proc_pidinfo=Mock(side_effect=info))
        with patch.object(d.sys, 'platform', 'darwin'), patch.object(d.os, 'getuid', return_value=501), \
             patch.object(d.ctypes, 'CDLL', return_value=lib):
            with self.assertRaises(d.IdentityError) as caught:
                READ_PROCESS(9, arguments=False, allow_system_login=True)
            self.assertNotIsInstance(caught.exception, d.LoginInfoDenied)

if __name__ == '__main__':
    unittest.main()

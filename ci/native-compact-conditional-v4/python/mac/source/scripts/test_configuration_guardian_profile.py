"""Explicit profile repair preserves user configuration and paused guardian state."""
from contextlib import redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import configuration_guardian as g
import manage_install as manager


class ExplicitProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.profile = self.home / 'profiles' / 'explicit.json'
        self.profile.parent.mkdir()
        self.doc = {
            'env': {'API_KEY': 'fixture-secret', 'BASE_URL': 'fixture-url'},
            'model': 'user-model', 'permissions': {'allow': ['Read']},
            'hooks': {'Stop': [{'matcher': 'keep', 'hooks': [
                {'type': 'command', 'command': 'foreign-fixture-hook'}]}]},
        }
        self.raw = (json.dumps(self.doc, ensure_ascii=False) + '  \n').encode()
        self.profile.write_bytes(self.raw)
        self.root = g.state_root(self.home) / 'configuration-guardian-v1'
        self.root.mkdir(parents=True)
        self.status = self.root / 'STATUS.json'
        self.status.write_bytes(b'{"status":"PAUSED_BY_COORDINATOR"}\n')
        self.status_raw = self.status.read_bytes()
        self.backups = self.root / 'profile-backups'
        self.manifest = g.state_root(self.home) / 'CURRENT.json'
        self.current = {'source': '/fixture/complete/source'}

    def reconcile(self, **kwargs):
        with patch.object(g, 'load_release', return_value=(self.current, manager)):
            return g.reconcile_profile(
                self.home, self.manifest, self.profile, **kwargs)

    def cli(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = g.main(['reconcile-profile', '--home', str(self.home),
                           '--settings', str(self.profile), *extra])
        return code, json.loads(out.getvalue()), out.getvalue()

    def assert_guardian_unchanged(self):
        self.assertEqual(self.status.read_bytes(), self.status_raw)
        self.assertFalse((self.home / 'Library/LaunchAgents').exists())
        self.assertFalse((self.home / '.claude').exists())
        self.assertFalse((self.home / '.codex').exists())
        self.assertFalse((self.home / '.cc-switch').exists())

    def test_preview_does_not_write_profile_backup_or_guardian(self):
        before = self.profile.stat()
        with patch.object(g.subprocess, 'run', side_effect=AssertionError('no job action')):
            result = self.reconcile()
        self.assertEqual(result['status'], 'PROFILE_PLANNED')
        self.assertEqual(result['before_sha256'], g.digest(self.raw))
        self.assertTrue(result['change_needed'])
        self.assertFalse(result['changed'])
        self.assertFalse(result['runtime_reload_proven'])
        self.assertEqual(self.profile.read_bytes(), self.raw)
        self.assertEqual(self.profile.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(self.backups.exists())
        self.assert_guardian_unchanged()

    def test_apply_keeps_exact_private_backup_foreign_hooks_and_other_fields(self):
        result = self.reconcile(apply=True, expected_sha256=g.digest(self.raw))
        self.assertEqual(result['status'], 'PROFILE_RECONCILED')
        self.assertTrue(result['replacement_performed'])
        self.assertEqual(result['after_sha256'], result['planned_sha256'])
        self.assertEqual(result['after_sha256'], g.digest(self.profile.read_bytes()))
        actual = json.loads(self.profile.read_bytes())
        self.assertEqual({k: v for k, v in actual.items() if k != 'hooks'},
                         {k: v for k, v in self.doc.items() if k != 'hooks'})
        self.assertEqual(g.foreign_hooks(actual, manager, self.home), self.doc['hooks'])
        backup = Path(result['backup'])
        self.assertEqual(backup.read_bytes(), self.raw)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertEqual(g.profile_backup(self.home, self.profile, self.raw), backup)
        before = self.profile.stat()
        again = self.reconcile(apply=True, expected_sha256=result['after_sha256'])
        self.assertEqual(again['status'], 'PROFILE_UNCHANGED')
        self.assertFalse(again['changed'])
        self.assertEqual(self.profile.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(list(self.backups.iterdir()), [backup])
        self.assert_guardian_unchanged()

    def test_noop_keeps_original_format_without_creating_backup(self):
        doc = g.normalized(self.doc, manager, self.home, True)
        raw = (json.dumps(doc, separators=(',', ':')) + '\n\n').encode()
        self.profile.write_bytes(raw)
        result = self.reconcile(apply=True, expected_sha256=g.digest(raw))
        self.assertEqual(result['status'], 'PROFILE_UNCHANGED')
        self.assertEqual(self.profile.read_bytes(), raw)
        self.assertFalse(self.backups.exists())
        self.assert_guardian_unchanged()

    def test_expected_original_hash_required_and_mismatch_is_zero_write(self):
        for value in (None, 'short', 'A' * 64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.reconcile(apply=True, expected_sha256=value)
        result = self.reconcile(apply=True, expected_sha256='0' * 64)
        self.assertEqual(result['status'], 'CONCURRENT_EDIT')
        self.assertFalse(result['changed'])
        self.assertEqual(self.profile.read_bytes(), self.raw)
        self.assertFalse(self.backups.exists())
        self.assert_guardian_unchanged()

    def test_concurrent_edit_during_planning_is_preserved(self):
        normalize = g.normalized
        external = b'{"model":"concurrent-user-model"}\n'
        def edit(*args):
            result = normalize(*args)
            self.profile.write_bytes(external)
            return result
        with patch.object(g, 'normalized', side_effect=edit):
            result = self.reconcile(apply=True, expected_sha256=g.digest(self.raw))
        self.assertEqual(result['status'], 'CONCURRENT_EDIT')
        self.assertFalse(result['changed'])
        self.assertEqual(self.profile.read_bytes(), external)
        self.assertEqual(Path(result['backup']).read_bytes(), self.raw)
        self.assert_guardian_unchanged()

    def test_later_writer_after_replacement_is_not_reported_as_success(self):
        atomic = g.atomic_json
        external = b'{"model":"later-writer"}\n'
        def edit(*args, **kwargs):
            atomic(*args, **kwargs)
            self.profile.write_bytes(external)
        with patch.object(g, 'atomic_json', side_effect=edit):
            result = self.reconcile(apply=True, expected_sha256=g.digest(self.raw))
        self.assertEqual(result['status'], 'CONCURRENT_EDIT')
        self.assertTrue(result['replacement_performed'])
        self.assertEqual(result['after_sha256'], g.digest(external))
        self.assertNotEqual(result['after_sha256'], result['planned_sha256'])
        self.assertEqual(self.profile.read_bytes(), external)
        self.assert_guardian_unchanged()

    def test_unreadable_post_write_content_keeps_failed_verification_visible(self):
        atomic = g.atomic_json
        def edit(*args, **kwargs):
            atomic(*args, **kwargs)
            self.profile.write_bytes(b'not-json fixture-secret')
        with patch.object(g, 'atomic_json', side_effect=edit):
            result = self.reconcile(apply=True, expected_sha256=g.digest(self.raw))
        self.assertEqual(result['status'], 'POST_WRITE_VERIFICATION_FAILED')
        self.assertTrue(result['replacement_performed'])
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertEqual(self.profile.read_bytes(), b'not-json fixture-secret')
        self.assert_guardian_unchanged()

    def test_aliases_relative_missing_directory_and_unowned_profiles_refused(self):
        alias = self.home / 'alias.json'
        alias.symlink_to(self.profile)
        parent = self.home / 'alias-directory'
        parent.symlink_to(self.profile.parent, target_is_directory=True)
        for path in (alias, parent / self.profile.name, Path('relative.json'),
                     self.home / 'missing.json', self.profile.parent):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                g.reconcile_profile(self.home, self.manifest, path)
        uid = os.getuid()
        with patch.object(g.os, 'getuid', return_value=uid + 1), self.assertRaises(ValueError):
            self.reconcile()
        self.assertEqual(self.profile.read_bytes(), self.raw)
        self.assertFalse(self.backups.exists())

    def test_installer_lock_is_nonblocking_and_explicit_cli_returns_failure(self):
        lock = g.state_root(self.home) / 'install.lock'
        with lock.open('w') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(g, 'load_release', side_effect=AssertionError('lock first')):
                code, result, _ = self.cli('--apply', '--expected-sha256', g.digest(self.raw))
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'INSTALLER_BUSY')
        self.assertEqual(result['input_operations'], 0)
        self.assertEqual(self.profile.read_bytes(), self.raw)
        self.assertFalse(self.backups.exists())
        self.assert_guardian_unchanged()

    def test_disabled_hooks_remain_disabled_and_no_job_is_started(self):
        self.doc['disableAllHooks'] = True
        raw = json.dumps(self.doc).encode()
        self.profile.write_bytes(raw)
        result = self.reconcile(apply=True, expected_sha256=g.digest(raw))
        self.assertEqual(result['status'], 'HOOKS_DISABLED_EXTERNALLY')
        self.assertTrue(result['hooks_disabled'])
        self.assertTrue(json.loads(self.profile.read_bytes())['disableAllHooks'])
        self.assert_guardian_unchanged()

    def test_cli_errors_do_not_expose_profile_or_change_guardian_status(self):
        with patch.object(g, 'load_release', return_value=(self.current, manager)):
            self.profile.write_bytes(b'fixture-secret invalid json')
            code, result, output = self.cli('--apply', '--expected-sha256', '0' * 64)
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'RECONCILE_FAILED')
        self.assertEqual(result['error_type'], 'JSONDecodeError')
        self.assertNotIn('fixture-secret', output)
        self.assertEqual(self.profile.read_bytes(), b'fixture-secret invalid json')
        self.assertFalse(self.backups.exists())
        self.assert_guardian_unchanged()

    def test_unverified_current_cannot_repair_profile(self):
        code, result, _ = self.cli('--apply', '--expected-sha256', g.digest(self.raw))
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'RECONCILE_FAILED')
        self.assertEqual(self.profile.read_bytes(), self.raw)
        self.assertFalse(self.backups.exists())
        self.assert_guardian_unchanged()

    def test_existing_inexact_or_public_backup_is_not_overwritten(self):
        backup = g.profile_backup(self.home, self.profile, self.raw)
        backup.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private and exact'):
            self.reconcile(apply=True, expected_sha256=g.digest(self.raw))
        backup.chmod(0o600)
        backup.write_bytes(b'{"external":"preserve"}')
        with self.assertRaisesRegex(ValueError, 'private and exact'):
            self.reconcile(apply=True, expected_sha256=g.digest(self.raw))
        self.assertEqual(backup.read_bytes(), b'{"external":"preserve"}')
        self.assertEqual(self.profile.read_bytes(), self.raw)
        self.assert_guardian_unchanged()


if __name__ == '__main__':
    unittest.main()


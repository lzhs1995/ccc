import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import install_claude_observation_entries as installer
import ccc_claude_launcher as launcher
import cmux_supervisor_tui as tui


class ClaudeObservationEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ccc-observation-entry-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.package = self.root / 'package'
        self.package.mkdir()
        for name in ('ccc_claude_launcher.py', 'ccc_claude_request_observer.cjs'):
            shutil.copy2(Path(launcher.__file__).with_name(name), self.package / name)
        self.manifest = {n.name: installer.digest(n) for n in self.package.iterdir()}
        (self.package / 'RELEASE-MANIFEST.json').write_text(json.dumps({'files': self.manifest}))
        self.binary = self.root / 'original selected executable'
        self.binary.write_text('#!' + sys.executable + '\nimport os,sys,json\n'
            'print(json.dumps(dict(args=sys.argv[1:], key=os.getenv("ANTHROPIC_AUTH_TOKEN"), '
            'observer=os.getenv("CCC_CLAUDE_REQUEST_OBSERVER"))))\n')
        self.binary.chmod(0o700)
        self.entry = self.root / 'claude'
        self.entry.symlink_to(self.binary.name)
        self.journal = self.root / 'install.json'

    def apply(self, **kwargs):
        return installer.install(self.package, [self.entry], self.journal, apply=True, **kwargs)

    def test_direct_launch_and_rollback_preserve_selected_binary_profile_and_key(self):
        original = installer.digest(self.binary)
        self.assertEqual(self.apply()['state'], 'committed')
        args = ['--settings', '/profiles/one $(false).json', '--resume', 'original-session']
        proc = subprocess.run([str(self.entry), *args], env=dict(os.environ,
            ANTHROPIC_AUTH_TOKEN='fake-private', CCC_CLAUDE_REQUEST_OBSERVATIONS_DIR=str(self.root / 'observations')),
            capture_output=True, text=True, timeout=5)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)
        self.assertEqual(value['args'], args)
        self.assertEqual(value['key'], 'fake-private')
        self.assertEqual(value['observer'], str(self.package / 'ccc_claude_request_observer.cjs'))
        self.assertEqual(installer.restore(self.journal)['state'], 'rolled_back')
        self.assertEqual(os.readlink(self.entry), self.binary.name)
        self.assertEqual(installer.digest(self.binary), original)

    def test_drift_during_preparation_does_not_overwrite_user_entry(self):
        other = self.root / 'other'
        other.write_text(self.binary.read_text()); other.chmod(0o700)
        def drift():
            self.entry.unlink()
            self.entry.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'changed during preparation'):
            self.apply(prepared=drift)
        self.assertEqual(self.entry.resolve(), other)
        self.assertEqual(json.loads(self.journal.read_bytes())['state'], 'rolled_back')

    def test_rollback_refuses_external_change(self):
        self.apply()
        self.entry.unlink()
        self.entry.symlink_to(self.binary)
        with self.assertRaisesRegex(ValueError, 'changed after installation'):
            installer.restore(self.journal)
        self.assertEqual(self.entry.resolve(), self.binary)

    def test_package_drift_after_preparation_refused_before_link_change(self):
        def drift():
            (self.package / 'ccc_claude_launcher.py').write_text('raise RuntimeError("drift")')
        with self.assertRaisesRegex(ValueError, 'differs from manifest'):
            self.apply(prepared=drift)
        self.assertEqual(os.readlink(self.entry), self.binary.name)
        self.assertEqual(json.loads(self.journal.read_bytes())['state'], 'rolled_back')

    def test_partial_failure_restores_original_relative_links(self):
        second = self.root / 'second'
        second.symlink_to(self.binary.name)
        replace = installer.replace_link
        def failing(path, target):
            if path == second:
                raise OSError('injected replacement failure')
            replace(path, target)
        with patch.object(installer, 'replace_link', side_effect=failing), \
                self.assertRaisesRegex(OSError, 'replacement failure'):
            installer.install(self.package, [self.entry, second], self.journal, apply=True)
        self.assertEqual(os.readlink(self.entry), self.binary.name)
        self.assertEqual(os.readlink(second), self.binary.name)
        self.assertEqual(json.loads(self.journal.read_bytes())['state'], 'rolled_back')

    def test_concrete_binary_and_changed_package_refused(self):
        with self.assertRaisesRegex(ValueError, 'owned symlink'):
            installer.install(self.package, [self.binary], self.journal, apply=True)
        (self.package / 'ccc_claude_launcher.py').write_text('raise RuntimeError("must not import")')
        with self.assertRaisesRegex(ValueError, 'differs from manifest'):
            self.apply()
        self.assertEqual(os.readlink(self.entry), self.binary.name)
        self.assertFalse(self.journal.exists())

    def test_panel_distinguishes_not_loaded_from_waiting_for_request(self):
        from types import SimpleNamespace
        session = SimpleNamespace(api_key_observation_historical=False, api_key_observed='',
                                  api_key_observation_status='not_instrumented')
        candidate = SimpleNamespace(session=session)
        self.assertEqual(tui.Candidate.api_key_text.fget(candidate), '未接入采集（K查看）')
        session.api_key_observation_status = 'absent'
        self.assertEqual(tui.Candidate.api_key_text.fget(candidate), '暂无请求记录')


if __name__ == '__main__':
    unittest.main()

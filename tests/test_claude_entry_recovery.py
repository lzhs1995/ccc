"""Recover interrupted entry installs using only private, fake executables."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from tools import install_claude_observation_entries as installer
from tests import test_claude_observation_entries as fixture


class EntryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.state = fixture.ClaudeObservationEntryTests()
        self.state.setUp()
        self.addCleanup(self.state.doCleanups)
        self.second = self.state.root / 'second'
        self.second.symlink_to(self.state.binary.name)

    def install_two(self):
        return installer.install(self.state.package, [self.state.entry, self.second],
                                 self.state.journal, apply=True)

    def crash_after_replace(self, action):
        script = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tools import install_claude_observation_entries as m
original = m.replace_link
def cut(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(73)
m.replace_link = cut
if sys.argv[5] == 'install':
    m.install(Path(sys.argv[2]), [Path(sys.argv[3])], Path(sys.argv[4]), apply=True)
else:
    m.restore(Path(sys.argv[4]))
'''
        result = subprocess.run([sys.executable, '-B', '-c', script,
            str(Path(installer.__file__).resolve().parent.parent), str(self.state.package),
            str(self.state.entry), str(self.state.journal), action],
            capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 73, result.stderr)

    def assert_original(self):
        self.assertEqual(os.readlink(self.state.entry), self.state.binary.name)
        self.assertEqual(self.state.entry.resolve(), self.state.binary)

    def test_install_exit_after_replace_before_after_snapshot(self):
        self.crash_after_replace('install')
        row = json.loads(self.state.journal.read_bytes())['entries'][0]
        self.assertNotIn('after', row)
        self.assertIn('install_intent', row)
        self.assertEqual(self.state.entry.resolve(), Path(row['wrapper']))
        self.assertEqual(installer.restore(self.state.journal)['state'], 'rolled_back')
        self.assert_original()

    def test_partial_restore_retries_without_rejecting_own_restored_inode(self):
        self.install_two()
        original = installer.replace_link
        def fail(path, target, **kwargs):
            if path == self.state.entry:
                raise OSError('remaining rollback unavailable')
            return original(path, target, **kwargs)
        with patch.object(installer, 'replace_link', side_effect=fail), self.assertRaises(OSError):
            installer.restore(self.state.journal)
        self.assertEqual(self.second.resolve(), self.state.binary)
        self.assertNotEqual(self.state.entry.resolve(), self.state.binary)
        self.assertEqual(installer.restore(self.state.journal)['state'], 'rolled_back')
        self.assert_original()
        self.assertEqual(self.second.resolve(), self.state.binary)

    def test_restore_exit_after_replace_before_restored_snapshot(self):
        self.state.apply()
        self.crash_after_replace('restore')
        row = json.loads(self.state.journal.read_bytes())['entries'][0]
        self.assertNotIn('restored', row)
        self.assertIn('restore_intent', row)
        self.assert_original()
        self.assertEqual(installer.restore(self.state.journal)['state'], 'rolled_back')
        self.assert_original()

    def test_restore_record_write_failure_can_retry(self):
        self.install_two()
        original = installer.save
        def fail(journal, data):
            if any(row.get('restored') for row in data['entries']):
                raise OSError('record unavailable')
            return original(journal, data)
        with patch.object(installer, 'save', side_effect=fail), self.assertRaises(OSError):
            installer.restore(self.state.journal)
        self.assertEqual(installer.restore(self.state.journal)['state'], 'rolled_back')
        self.assert_original()
        self.assertEqual(self.second.resolve(), self.state.binary)

    def test_crash_recovery_rejects_external_same_target_symlink(self):
        self.crash_after_replace('install')
        target = os.readlink(self.state.entry)
        # Preserve the inode so the fixture cannot accidentally reuse it.
        self.state.entry.rename(self.state.root / 'previous-entry')
        self.state.entry.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'changed after installation'):
            installer.restore(self.state.journal)
        self.assertEqual(os.readlink(self.state.entry), target)

    def test_legacy_committed_journal_without_intents_can_restore(self):
        self.state.apply()
        data = json.loads(self.state.journal.read_bytes())
        for row in data['entries']:
            del row['install_intent']
        installer.save(self.state.journal, data)
        self.assertEqual(installer.restore(self.state.journal)['state'], 'rolled_back')
        self.assert_original()

    def test_successful_multi_entry_restore_is_idempotent(self):
        self.install_two()
        installer.restore(self.state.journal)
        first = [installer.snapshot(p) for p in (self.state.entry, self.second)]
        self.assertEqual(installer.restore(self.state.journal)['state'], 'rolled_back')
        self.assertEqual(first, [installer.snapshot(p) for p in (self.state.entry, self.second)])

    def test_install_intent_record_failure_never_replaces_entry(self):
        original = installer.save
        def fail(journal, data):
            if data['state'] == 'prepared' and any(r.get('install_intent') for r in data['entries']):
                raise OSError('intent record unavailable')
            return original(journal, data)
        before = installer.snapshot(self.state.entry)
        with patch.object(installer, 'save', side_effect=fail), self.assertRaises(OSError):
            self.state.apply()
        self.assertEqual(installer.snapshot(self.state.entry), before)

    def test_staged_link_drift_refused_before_replacement(self):
        original = installer.save
        def drift(journal, data):
            original(journal, data)
            if data['state'] == 'prepared' and data['entries'][0].get('install_intent'):
                staged = Path(data['entries'][0]['install_intent']['staged'])
                staged.rename(staged.with_name(staged.name + '-external'))
                staged.symlink_to(self.state.binary)
        before = installer.snapshot(self.state.entry)
        with patch.object(installer, 'save', side_effect=drift), self.assertRaises(ValueError):
            self.state.apply()
        self.assertEqual(installer.snapshot(self.state.entry), before)


if __name__ == '__main__':
    unittest.main()

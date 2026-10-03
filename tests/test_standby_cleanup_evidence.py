import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import standby_cleanup_evidence as subject


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.ids = [f'00000000-0000-0000-0000-{i:012d}' for i in range(1, 5)]
        self.tree = {'windows': [{'id': self.ids[0], 'workspaces': [
            {'id': self.ids[1], 'panes': [{'id': self.ids[2], 'surfaces': [
                {'id': self.ids[3], 'type': 'terminal'}]}]}]}]}
        self.client = Mock()
        self.client.tree.return_value = self.tree
        self.row = {'pid': 123, 'birth': [10, 20], 'surface_id': self.ids[3],
                    'environment_workspace_id': self.ids[1]}
        self.clock = lambda: {'boot_id': self.ids[0], 'wall': 1., 'monotonic': 1.}

    def capture(self, matches=True):
        with patch.object(subject.scope, 'scan', return_value=[self.row]), \
                patch.object(subject.scope, 'matches', side_effect=matches if isinstance(matches, list) else None,
                             return_value=matches):
            return subject.capture_baseline(self.root, client=self.client, clock=self.clock)

    def test_originals_saved_without_terminal_claim(self):
        result = self.capture()
        self.assertEqual(result['processes'], [self.row])
        self.assertEqual(json.loads((self.root / 'cleanup-baseline.json').read_text()), result)
        self.assertFalse(result['job_terminal'])
        self.assertFalse(result['run_terminal'])
        self.assertEqual(self.client.method_calls, [unittest.mock.call.tree(), unittest.mock.call.tree()])

    def test_partial_tree_is_not_empty_cleanup(self):
        for tree in ({}, {'windows': [{}]}, {'windows': [{'id': self.ids[0]}]}):
            with self.subTest(tree=tree), self.assertRaises((ValueError, KeyError)):
                subject.tree_records(tree)

    def test_final_identity_drift_rejected(self):
        with self.assertRaisesRegex(ValueError, 'final observation'):
            self.capture([True, False])
        self.assertFalse((self.root / 'cleanup-baseline.json').exists())

    def test_membership_drift_rejected(self):
        self.client.tree.side_effect = [self.tree, {'windows': []}]
        with self.assertRaisesRegex(ValueError, 'membership changed'):
            self.capture()

    def test_no_overwrite(self):
        self.capture()
        original = (self.root / 'cleanup-baseline.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.capture()
        self.assertEqual((self.root / 'cleanup-baseline.json').read_bytes(), original)

    def test_no_missing_boot(self):
        self.clock = lambda: {'boot_id': None}
        with self.assertRaisesRegex(ValueError, 'boot identity'):
            self.capture()

    def test_late_scan_rejected(self):
        with patch.object(subject.scope, 'scan', return_value=[self.row]), \
                self.assertRaisesRegex(ValueError, 'deadline'):
            subject.capture_baseline(self.root, client=self.client, clock=self.clock,
                                     monotonic=Mock(side_effect=[0, 0, 31]))

    def cleanup(self, *, pids=(), birth=None, matches=True):
        self.capture()
        original = {'pid': 456, 'birth': [10, 20], 'surface_id':
                    '00000000-0000-0000-0000-000000000099', 'session_id': 'session'}
        ui = {'originals': [original]}
        folder = self.root / 'standby'
        folder.mkdir()
        ui_path = folder / 'activation-ui.json'
        ui_path.write_text(json.dumps(ui))
        ui_path.chmod(0o600)
        saved = {'activation_ui_sha256': subject._sha(ui_path.read_bytes()),
                 'boot_id': self.ids[0], 'action_id': 'action'}
        clock = lambda: {'boot_id': self.ids[0], 'wall': 20., 'monotonic': 20.}
        with patch.object(subject, 'COUNT', 1), \
                patch.object(subject, 'read_settlement', return_value=(saved, 'sha')), \
                patch.object(subject.batch, 'job_path', return_value=self.root / 'job.json'), \
                patch.object(subject.scope, 'scan', return_value=[]), \
                patch.object(subject.scope, 'matches', return_value=matches), \
                patch.object(subject.scope, 'birth', return_value=birth), \
                patch.object(subject, 'pid_inventory', return_value=list(pids)):
            return subject.capture_cleanup(self.root / 'config.json', 'job',
                self.root / 'cleanup-baseline.json', self.root, client=self.client, clock=clock)

    def verify(self, mutate=None, **kwargs):
        record = self.cleanup(**kwargs)
        if mutate:
            mutate(record)
            (self.root / 'cleanup-observation.json').write_text(json.dumps(record))
        saved = {'activation_ui_sha256': subject._sha(
                    (self.root / 'standby/activation-ui.json').read_bytes()),
                 'boot_id': self.ids[0], 'action_id': 'action'}
        with patch.object(subject, 'COUNT', 1), \
                patch.object(subject, 'read_settlement', return_value=(saved, 'sha')), \
                patch.object(subject.batch, 'job_path', return_value=self.root / 'job.json'):
            return subject.verify_cleanup(self.root / 'config.json', 'job',
                                          self.root / 'cleanup-observation.json')

    def test_reverify_complete_originals(self):
        self.assertIn('observation_sha256', self.verify())

    def test_reverify_reused_pid(self):
        self.assertIn('observation_sha256', self.verify(pids=[456], birth=[19, 1]))

    def test_reverify_cannot_trust_passed_over_live_pid(self):
        with self.assertRaisesRegex(ValueError, 'cleanup not established'):
            self.verify(lambda r: r.update(passed=True), pids=[456], birth=[10, 20])

    def test_reverify_requires_both_passes(self):
        with self.assertRaisesRegex(ValueError, 'two complete'):
            self.verify(lambda r: r['passes'].pop())

    def test_reverify_missing_experiment_rejected(self):
        with self.assertRaisesRegex(ValueError, 'coverage incomplete'):
            self.verify(lambda r: r['passes'][0]['experiment'].clear())

    def test_reverify_missing_preserved_rejected(self):
        with self.assertRaisesRegex(ValueError, 'coverage incomplete'):
            self.verify(lambda r: r['passes'][0]['preserved'].clear())

    def test_cleanup_absent_pid_preserved_baseline(self):
        result = self.cleanup()
        self.assertTrue(result['passed'])
        self.assertFalse(result['job_terminal'])

    def test_scan_omission_cannot_hide_live_pid(self):
        result = self.cleanup(pids=[456], birth=[10, 20])
        self.assertFalse(result['passed'])
        self.assertEqual(result['passes'][0]['experiment'][0]['state'], 'original_alive')
        self.assertTrue((self.root / 'cleanup-observation.json').exists())

    def test_pid_present_birth_unknown_not_cleanup(self):
        result = self.cleanup(pids=[456])
        self.assertFalse(result['passed'])

    def test_reused_pid_does_not_mean_original_alive(self):
        self.assertTrue(self.cleanup(pids=[456], birth=[19, 1])['passed'])

    def test_original_session_loss_is_failure(self):
        self.assertFalse(self.cleanup(matches=False)['passed'])


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import standby_preparation_cleanup as subject


class PreparationCleanupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.root.chmod(0o700)
        self.uid = '00000000-0000-0000-0000-000000000001'
        self.spec = self.root/'owner.json'
        self.write(self.spec, {})
        self.write(self.root/'job.json', {})
        self.write(self.root/'runner-open.json', {'owner_spec_path': str(self.spec),
            'owner_spec_sha256': subject._sha(self.spec.read_bytes())})
        self.write(self.root/'runner-intent.json', {'started_at': 3, 'started_monotonic': 3})
        self.baseline = self.root/'baseline.json'
        self.write(self.baseline, {'version': 1, 'kind': 'standby_cleanup_baseline',
            'started': self.clock(1), 'finished': self.clock(2),
            'tree_before': {'windows': []}, 'tree_after': {'windows': []}, 'processes': []})
        self.rows = [{'index': 0, 'state': 'identified', 'surface_id': self.uid,
                      'process_identity': [123, 4, 0]}]
        self.client = Mock()
        self.client.tree.return_value = {'windows': []}

    def clock(self, value):
        return {'boot_id': self.uid, 'wall': value, 'monotonic': value}

    def write(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def capture(self, pids=(), birth=None, changed=False):
        original = {'rows': self.rows}
        ticks = iter([self.clock(6), self.clock(7)])
        with patch.object(subject.batch, 'job_path', return_value=self.root/'job.json'), \
             patch.object(subject.launch, 'policy', return_value={'boot_id': self.uid}), \
             patch.object(subject, 'runner_evidence', return_value=(self.clock(5), [])), \
             patch.object(subject, 'inventory', side_effect=[original, {} if changed else original]), \
             patch.object(subject.cleanup.scope, 'scan', return_value=[]), \
             patch.object(subject.cleanup, 'pid_inventory', return_value=list(pids)), \
             patch.object(subject.cleanup.scope, 'birth', return_value=birth):
            return subject.capture(self.root/'config.json', self.uid, self.root,
                runner_directory=self.root, preparation_directory=self.root,
                baseline_path=self.baseline, client=self.client, clock=lambda: next(ticks))

    def test_absent_identified_process_observed_without_terminal(self):
        result = self.capture()
        self.assertTrue(result['passed'])
        self.assertFalse(result['job_terminal'])
        self.assertEqual(len(result['passes']), 2)

    def test_live_process_preserves_failed_observation(self):
        self.assertFalse(self.capture(pids=[123], birth=[4, 0])['passed'])
        self.assertTrue((self.root/'preparation-cleanup.json').exists())

    def test_unknown_birth_not_absent(self):
        self.assertFalse(self.capture(pids=[123])['passed'])

    def test_reused_pid_can_be_distinguished(self):
        self.assertTrue(self.capture(pids=[123], birth=[5, 0])['passed'])

    def test_unknown_ack_not_resolved_by_empty_scan(self):
        self.rows[0]['state'] = 'unknown_ack'
        result = self.capture()
        self.assertFalse(result['passed'])
        self.assertEqual(result['unresolved_slots'], [0])

    def test_unknown_process_not_resolved_by_empty_scan(self):
        self.rows[0]['state'] = 'unknown_process'
        self.assertFalse(self.capture()['passed'])

    def test_inventory_change_rejected(self):
        with self.assertRaisesRegex(ValueError, 'originals changed'):
            self.capture(changed=True)
        self.assertFalse((self.root/'preparation-cleanup.json').exists())

    def test_activation_record_requires_other_path(self):
        (self.root/'standby').mkdir()
        self.write(self.root/'standby'/'activation-attempt.json', {})
        with self.assertRaisesRegex(ValueError, 'activated preparation'):
            self.capture()

    def test_late_baseline_rejected(self):
        value = json.loads(self.baseline.read_text())
        value['finished'] = self.clock(4)
        self.write(self.baseline, value)
        with self.assertRaisesRegex(ValueError, 'clock order'):
            self.capture()

    def reverify(self, mutate=None):
        record = self.capture()
        if mutate:
            mutate(record)
        path = self.root/'preparation-cleanup.json'
        self.write(path, record)
        with patch.object(subject.batch, 'job_path', return_value=self.root/'job.json'), \
             patch.object(subject.launch, 'policy', return_value={'boot_id': self.uid}), \
             patch.object(subject, 'runner_evidence', return_value=(self.clock(5), [])), \
             patch.object(subject, 'inventory', return_value={'rows': self.rows}):
            return subject.verify(self.root/'config.json', self.uid, path)

    def test_original_samples_reverified(self):
        result = self.reverify()
        self.assertEqual(result['job_id'], self.uid)

    def test_cannot_omit_process(self):
        with self.assertRaisesRegex(ValueError, 'coverage incomplete'):
            self.reverify(lambda r: r['passes'][0]['experiment'].clear())

    def test_cannot_hide_live_pid_using_saved_absent_state(self):
        def change(record):
            record['passes'][0]['pids'] = [123]
            record['passes'][0]['experiment'][0]['current_birth'] = [4, 0]
        with self.assertRaisesRegex(ValueError, 'cleanup not established'):
            self.reverify(change)

    def test_cannot_hide_recognized_process(self):
        def change(record):
            record['passes'][0]['recognized_processes'] = [{'surface_id': self.uid}]
        with self.assertRaisesRegex(ValueError, 'remains recognized'):
            self.reverify(change)

    def test_cannot_replace_original_hashes(self):
        with self.assertRaisesRegex(ValueError, 'hashes changed'):
            self.reverify(lambda r: r['originals'].clear())

    def test_requires_both_samples(self):
        with self.assertRaisesRegex(ValueError, 'two complete'):
            self.reverify(lambda r: r['passes'].pop())


if __name__ == '__main__':
    unittest.main()

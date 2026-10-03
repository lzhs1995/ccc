import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import standby_preparation_inventory as subject
from tools import standby_preparation_cleanup as cleanup


class InventoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.root.chmod(0o700)
        self.uid = '00000000-0000-0000-0000-000000000001'
        self.selected = dict(job_id=self.uid, workspace_id=self.uid,
            cohort_id=self.uid, generation='generation', boot_id=self.uid,
            mode='b', policy='policy')
        self.job = self.root / 'job.json'
        self.write(self.job, {'slots': [{'index': 0, 'launch_id': self.uid}]})
        self.intent = dict(self.selected, index=0, launch_id=self.uid)
        self.ack = dict(index=0, launch_id=self.uid, surface_id=self.uid,
                        workspace_id=self.uid)
        self.claim = dict(self.selected, index=0, launch_id=self.uid,
            surface_id=self.uid, argv=['codex'], bootstrap_pid=123,
            bootstrap_birth=[100, 1])

    def write(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def read(self):
        with patch.object(subject.batch, 'job_path', return_value=self.job), \
             patch.object(subject.launch, 'policy', return_value=self.selected), \
             patch.object(subject.launch, 'claim_path', return_value=self.root/'claim.json'), \
             patch.object(subject.launch, 'launch_argv', return_value=['codex']):
            return subject.inventory(self.root/'config.json', self.uid, self.root)

    def seed(self, ack=True, claim=True):
        self.write(self.root/'create-intent-0.json', self.intent)
        if ack:
            self.write(self.root/'create-ack-0.json', self.ack)
        if claim:
            self.write(self.root/'claim.json', self.claim)

    def test_missing_records_never_prove_cleanup(self):
        result = self.read()
        self.assertEqual(result['rows'][0]['state'], 'unrecorded')
        self.assertEqual(result['unresolved_slots'], [0])
        self.assertFalse(result['cleanup_proven'])

    def test_unknown_ack_retains_claim_without_promoting(self):
        self.seed(ack=False)
        row = self.read()['rows'][0]
        self.assertEqual(row['state'], 'unknown_ack')
        self.assertEqual(row['claim_surface_id'], self.uid)
        self.assertEqual(row['process_identity'], [123, 100, 1])

    def test_ack_without_claim_is_unknown_process(self):
        self.seed(claim=False)
        self.assertEqual(self.read()['rows'][0]['state'], 'unknown_process')

    def test_identified_is_not_cleanup(self):
        self.seed()
        result = self.read()
        self.assertEqual(result['rows'][0]['state'], 'identified')
        self.assertFalse(result['cleanup_proven'])

    def test_ack_without_intent_rejected(self):
        self.write(self.root/'create-ack-0.json', self.ack)
        with self.assertRaisesRegex(ValueError, 'intent missing'):
            self.read()

    def test_foreign_claim_rejected(self):
        self.claim['argv'] = ['other']
        self.seed()
        with self.assertRaisesRegex(ValueError, 'claim binding'):
            self.read()

    def test_extra_slot_record_rejected(self):
        self.write(self.root/'create-intent-1.json', self.intent)
        with self.assertRaisesRegex(ValueError, 'unexpected'):
            self.read()

    def test_record_appearing_during_read_rejected(self):
        self.seed(ack=False, claim=False)
        original = subject._read
        def read(path):
            result = original(path)
            if Path(path).name == 'create-intent-0.json':
                self.write(self.root/'create-ack-0.json', self.ack)
            return result
        with patch.object(subject, '_read', side_effect=read):
            with self.assertRaisesRegex(ValueError, 'changed during inventory'):
                self.read()

    def failure(self):
        spec = dict(self.selected, schema=1, kind='standby_live_bootstrap',
                    config_path=str(self.root/'config.json'), launch_ids=[self.uid])
        self.write(self.root/'bootstrap.json', spec)
        return dict(self.selected, kind='bootstrap_launch_failure', index=0,
            launch_id=self.uid, pid=123,
            spec_sha256=subject._sha((self.root/'bootstrap.json').read_bytes()),
            bootstrap_identity=dict(pid=123, birth_before=[100, 1], birth_after=[100, 1],
                surface_id=self.uid, surface_id_after=self.uid,
                workspace_id=self.uid, workspace_id_after=self.uid))

    def test_bound_failure_supplies_identity_but_never_cleanup(self):
        self.seed(claim=False)
        self.write(self.root/'bootstrap-failure-0.json', self.failure())
        result = self.read()
        self.assertEqual(result['rows'][0]['process_identity'], [123, 100, 1])
        self.assertEqual(result['rows'][0]['process_identity_source'], 'bootstrap_failure')
        self.assertEqual(result['rows'][0]['state'], 'identified')
        self.assertFalse(result['cleanup_proven'])

    def test_failure_unknown_or_drift_does_not_establish_identity(self):
        self.seed(claim=False)
        for field, value in [('birth_before', None), ('birth_after', [101, 1]),
                             ('surface_id_after', 'foreign'), ('workspace_id', 'foreign'),
                             ('pid', True)]:
            with self.subTest(field=field):
                record = self.failure()
                record['bootstrap_identity'][field] = value
                self.write(self.root/'bootstrap-failure-0.json', record)
                result = self.read()
                self.assertEqual(result['rows'][0]['state'], 'unknown_process')
                self.assertEqual(result['unresolved_slots'], [0])

    def test_failure_spec_or_claim_conflict_rejected(self):
        self.seed()
        record = self.failure()
        record['bootstrap_identity']['pid'] = record['pid'] = 456
        self.write(self.root/'bootstrap-failure-0.json', record)
        with self.assertRaisesRegex(ValueError, 'process differ'):
            self.read()
        record = self.failure()
        record['spec_sha256'] = '0'*64
        self.write(self.root/'bootstrap-failure-0.json', record)
        with self.assertRaisesRegex(ValueError, 'original binding'):
            self.read()

    def test_old_failure_without_birth_stays_unknown(self):
        self.seed(claim=False)
        record = self.failure()
        del record['bootstrap_identity']
        self.write(self.root/'bootstrap-failure-0.json', record)
        self.assertEqual(self.read()['rows'][0]['state'], 'unknown_process')

    def test_failure_identity_full_cleanup_capture_and_reverify(self):
        self.seed(claim=False)
        failure_path = self.root/'bootstrap-failure-0.json'
        self.write(failure_path, self.failure())
        def clock(n):
            return dict(boot_id=self.uid, wall=n, monotonic=n)
        self.write(self.root/'runner-open.json', {
            'owner_spec_path': str(self.root/'bootstrap.json'),
            'owner_spec_sha256': subject._sha((self.root/'bootstrap.json').read_bytes())})
        self.write(self.root/'runner-intent.json', dict(started_at=3, started_monotonic=3))
        baseline = self.root/'baseline.json'
        self.write(baseline, dict(kind='standby_cleanup_baseline', version=1,
            started=clock(1), finished=clock(2), tree_before={'windows': []},
            tree_after={'windows': []}, processes=[]))
        client = Mock()
        client.tree.return_value = {'windows': []}
        ticks = iter([clock(102), clock(103)])
        with patch.object(subject.batch, 'job_path', return_value=self.job), \
             patch.object(subject.launch, 'policy', return_value=self.selected), \
             patch.object(subject.launch, 'claim_path', return_value=self.root/'claim.json'), \
             patch.object(cleanup, 'runner_evidence', return_value=(clock(101), [])), \
             patch.object(cleanup.cleanup.scope, 'scan', return_value=[]), \
             patch.object(cleanup.cleanup, 'pid_inventory', return_value=[]):
            result = cleanup.capture(self.root/'config.json', self.uid, self.root,
                runner_directory=self.root, preparation_directory=self.root,
                baseline_path=baseline, client=client, clock=lambda: next(ticks))
            self.assertTrue(result['passed'])
            self.assertEqual(result['passes'][0]['experiment'][0]['pid'], 123)
            path = self.root/'preparation-cleanup.json'
            self.assertEqual(cleanup.verify(self.root/'config.json', self.uid, path)['job_id'], self.uid)
            changed = json.loads(failure_path.read_text())
            changed['bootstrap_identity']['birth_after'] = [101, 0]
            self.write(failure_path, changed)
            with self.assertRaisesRegex(ValueError, 'inventory unresolved or changed'):
                cleanup.verify(self.root/'config.json', self.uid, path)


if __name__ == '__main__':
    unittest.main()

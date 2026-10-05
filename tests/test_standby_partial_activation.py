"""Original private activation files; no native sessions or network traffic."""
import json
import unittest
import uuid
from unittest.mock import patch

from tests import test_standby_timing as fixtures
from tools import standby_partial_activation as subject
from tools import standby_preparation_cleanup as cleanup


class PartialActivationTests(unittest.TestCase):
    start_native = fixtures.TimingTests.start_native
    hook_fixture = fixtures.TimingTests.hook_fixture
    bind = fixtures.TimingTests.bind
    task = fixtures.TimingTests.task
    stamp = fixtures.TimingTests.stamp

    def setUp(self):
        fixtures.TimingTests.setUp(self)
        self.receipt = self.bridge.committed()
        self.directory = self.bridge.directory
        for path in self.directory.glob('input-*.json'):
            path.unlink()  # The shared synthetic timing fixture consumed slot 0.
        self.delivery = {k: self.receipt[k] for k in
                         ('action_id', 'cohort_id', 'workspace_id', 'boot_id')}
        self.delivery.update(native_task_acceptance_evaluated=False, acknowledged_inputs=0,
            outcomes=[dict(index=i, acknowledged=False, error='CmuxError') for i in range(50)])
        self.put('delivery-results.json', self.delivery)

    def put(self, name, value):
        path = self.directory / name
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def read(self):
        return subject.evidence(self.config.resolve(), self.worker.job['id'])

    def input_claim(self, i=0):
        claim = dict(action_id=self.action, original=self.receipt['originals'][i],
            activation_sha256=self.receipt['evidence_sha256']['activation'],
            input_id=str(uuid.uuid4()), prompt=json.loads((self.directory/'activation.json').read_bytes())['prompt'])
        self.put(f'input-{i}.json', claim)
        return claim

    def test_closed_input_failure_retains_activation_without_success(self):
        record = self.read()
        self.assertTrue(record['activated'])
        self.assertFalse(record['submission_settled'])
        self.assertFalse(record['native_task_acceptance_evaluated'])
        self.assertFalse((self.directory/'submission-settled.json').exists())

    def test_missing_ui_not_downgraded(self):
        (self.directory/'activation-ui.json').unlink()
        with self.assertRaises(OSError): self.read()

    def test_existing_settlement_even_corrupt_rejected(self):
        self.put('submission-settled.json', {})
        with self.assertRaisesRegex(ValueError, 'job terminal'): self.read()

    def test_ack_requires_original_consumption(self):
        self.delivery['outcomes'][0]['acknowledged'] = True
        self.delivery['acknowledged_inputs'] = 1
        self.put('delivery-results.json', self.delivery)
        with self.assertRaisesRegex(ValueError, 'consumption'): self.read()
        self.input_claim()
        self.assertEqual(self.read()['acknowledged_inputs'], 1)

    def test_unknown_ack_consumed_but_not_promoted(self):
        self.input_claim()
        record = self.read()
        self.assertEqual(record['consumed_inputs'], [0])
        self.assertEqual(record['acknowledged_inputs'], 0)

    def test_claim_from_other_action_rejected(self):
        claim = self.input_claim(); claim['action_id'] = str(uuid.uuid4())
        self.put('input-0.json', claim)
        with self.assertRaisesRegex(ValueError, 'consumption'): self.read()

    def test_extra_input_file_rejected(self):
        self.put('input-50.json', {})
        with self.assertRaisesRegex(ValueError, 'unexpected'): self.read()

    def test_wrong_ui_action_rejected(self):
        self.receipt['action_id'] = str(uuid.uuid4())
        self.put('activation-ui.json', self.receipt)
        with self.assertRaisesRegex(ValueError, 'UI differs'): self.read()

    def test_delivery_coverage_cannot_hide_one_slot(self):
        self.delivery['outcomes'].pop(); self.put('delivery-results.json', self.delivery)
        with self.assertRaisesRegex(ValueError, 'delivery binding'): self.read()

    def test_late_settlement_rejected(self):
        original = subject._read
        def read(path):
            result = original(path)
            if path.name == 'delivery-results.json': self.put('submission-settled.json', {})
            return result
        with patch.object(subject, '_read', side_effect=read):
            with self.assertRaisesRegex(ValueError, 'changed during read'): self.read()

    def test_cleanup_requires_original_native_pid_and_birth(self):
        record = self.read()
        rows = [dict(index=r['index'], launch_id=r['launch_id'], surface_id=r['surface_id'],
                     state='identified', process_identity=[r['pid'], *r['birth']])
                for r in record['originals']]
        cleanup.bind_activation(record, {'rows': rows}, self.stamp(11), self.boot)
        rows[-1]['process_identity'][0] += 1
        with self.assertRaisesRegex(ValueError, 'process differs'):
            cleanup.bind_activation(record, {'rows': rows}, self.stamp(11), self.boot)


if __name__ == '__main__':
    unittest.main()

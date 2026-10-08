"""Real plan/invocation files; cleanup verifier isolated, no native actions."""
import json
import unittest
from unittest.mock import patch

from tests import test_standby_run_attempts as fixtures
from tools import standby_attempt_terminal as subject


class TerminalTests(unittest.TestCase):
    clock = fixtures.AttemptTests.clock
    declare = fixtures.AttemptTests.declare
    register = fixtures.AttemptTests.register

    def setUp(self):
        fixtures.AttemptTests.setUp(self)
        self.manifest = self.declare()
        self.register(self.manifest)
        self.cleanup = self.root / 'cleanup.json'
        self.put(self.cleanup, {'runner_directory': str(self.output)})
        self.intent = dict(invocation_id=self.value['invocation_id'],
            invocation_sha256=self.sha, workspace_id=self.value['workspace_id'],
            mode='b', boot_id=self.boot, started_at=3, started_monotonic=3)
        self.put(self.output/'runner-intent.json', self.intent)
        self.put(self.output/'runner-closed.json', {'reason': 'service_closed'})
        self.verifier = patch.object(subject, 'verify_cleanup', side_effect=lambda *a: {
            'observation_sha256': subject._sha(self.cleanup.read_bytes()),
            'finished': self.clock(5)})
        self.mock_verify = self.verifier.start()
        self.addCleanup(self.verifier.stop)

    def put(self, path, value):
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def capture(self):
        return subject.capture(self.plan_directory, self.batch_id, self.root/'config.json',
                               self.batch_id, self.cleanup, clock=lambda: self.clock(6))

    def verify(self):
        return subject.verify(self.plan_directory, self.batch_id, clock=lambda: self.clock(7))

    def test_incomplete_not_success_and_does_not_pollute_attempt_set(self):
        self.assertEqual(self.capture()['outcome'], 'incomplete')
        self.assertEqual(self.verify()['outcome'], 'incomplete')
        self.assertEqual(len(self.manifest.attempts()), 1)

    def test_failed_original_not_promoted(self):
        self.put(self.output/'runner-closed.json', {'reason': 'service_closed', 'error_type': 'OSError'})
        self.assertEqual(self.capture()['outcome'], 'failed')

    def test_other_invocation_rejected(self):
        self.intent['invocation_sha256'] = 'a'*64
        self.put(self.output/'runner-intent.json', self.intent)
        with self.assertRaisesRegex(ValueError, 'registered invocation'): self.capture()

    def test_runner_started_before_registration_rejected(self):
        self.intent.update(started_at=1, started_monotonic=1)
        self.put(self.output/'runner-intent.json', self.intent)
        with self.assertRaisesRegex(ValueError, 'clock order'): self.capture()

    def test_other_cleanup_runner_rejected(self):
        self.put(self.cleanup, {'runner_directory': str(self.root)})
        with self.assertRaisesRegex(ValueError, 'another runner'): self.capture()

    def test_missing_runner_not_zero_resource_proof(self):
        (self.output/'runner-closed.json').unlink()
        with self.assertRaises(OSError): self.capture()

    def test_saved_success_forgery_rejected(self):
        record = self.capture(); record['outcome'] = 'succeeded'
        self.put(self.plan_directory/f'terminal-attempt-{self.batch_id}.json', record)
        with self.assertRaisesRegex(ValueError, 'differs'): self.verify()

    def test_activation_during_verification_rejected(self):
        def verify(*args):
            self.put(self.plan_directory/f'batch-{self.batch_id}.json', {})
            return {'observation_sha256': subject._sha(self.cleanup.read_bytes()),
                    'finished': self.clock(5)}
        self.mock_verify.side_effect = verify
        with self.assertRaisesRegex(ValueError, 'originals changed'): self.capture()

    def test_cleanup_failure_propagates_without_terminal(self):
        self.mock_verify.side_effect = ValueError('unresolved cleanup')
        with self.assertRaisesRegex(ValueError, 'unresolved cleanup'): self.capture()
        self.assertFalse(list(self.plan_directory.glob('terminal-attempt-*')))


if __name__ == '__main__':
    unittest.main()

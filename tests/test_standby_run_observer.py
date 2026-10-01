"""Lifecycle dispatch with original registration files and isolated collectors."""
import unittest
from unittest.mock import Mock, patch

from tests import test_standby_attempt_terminal as fixtures
from tools import standby_run_observer as subject


class ObserverTests(unittest.TestCase):
    clock = fixtures.TerminalTests.clock
    declare = fixtures.TerminalTests.declare
    register = fixtures.TerminalTests.register
    put = fixtures.TerminalTests.put

    def setUp(self):
        fixtures.TerminalTests.setUp(self)
        self.opened = dict(invocation_id=self.value['invocation_id'], invocation_sha256=self.sha,
            config_path=self.value['config_path'], workspace_id=self.value['workspace_id'],
            mode='b', job_id=self.batch_id, job_path=str(self.root/'job.json'),
            owner_spec_path=str(self.root/'owner.json'))
        self.put(self.output/'runner-open.json', self.opened)

    def settle(self, **kwargs):
        return subject.settle(self.plan_directory, self.batch_id, self.root,
                              self.root/'baseline.json', client=Mock(), **kwargs)

    def test_missing_close_does_not_begin_cleanup(self):
        (self.output/'runner-closed.json').unlink()
        with patch.object(subject.preparation, 'capture') as capture:
            with self.assertRaises(OSError): self.settle()
            capture.assert_not_called()

    def test_unknown_open_binding_rejected(self):
        self.opened['invocation_sha256'] = '0'*64
        self.put(self.output/'runner-open.json', self.opened)
        with self.assertRaisesRegex(ValueError, 'registered invocation'): self.settle()

    def test_preparation_cleanup_failure_cannot_create_terminal(self):
        with patch.object(subject.preparation, 'capture', side_effect=ValueError('unknown ACK')), \
             patch.object(subject.attempt_terminal, 'capture') as terminal:
            with self.assertRaisesRegex(ValueError, 'unknown ACK'): self.settle()
            terminal.assert_not_called()

    def test_unactivated_cannot_accept_completion(self):
        with self.assertRaisesRegex(ValueError, 'unactivated'):
            self.settle(completion_path=self.root/'completion.json')

    def test_closed_owner_rejects_late_completion(self):
        with patch.object(subject.completion, 'capture') as capture:
            with self.assertRaisesRegex(ValueError, 'owner is alive'):
                subject.observe_completion(self.plan_directory, self.batch_id, self.root, failed_rounds=1)
            capture.assert_not_called()

    def test_activated_missing_settlement_cannot_fall_back_to_attempt(self):
        standby = self.root/'standby'; standby.mkdir()
        self.put(standby/'activation-attempt.json', {})
        with patch.object(subject.RunManifest, 'bind', side_effect=ValueError('settlement missing')), \
             patch.object(subject.preparation, 'capture') as capture:
            with self.assertRaisesRegex(ValueError, 'settlement missing'): self.settle()
            capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""Real attempt/plan files and terminal verifiers; isolated cleanup evidence."""
import json
import unittest
import uuid
from unittest.mock import patch

from tests import test_standby_attempt_terminal as fixtures
from tools import standby_run_terminal as subject


class RunTerminalTests(unittest.TestCase):
    clock = fixtures.TerminalTests.clock
    declare = fixtures.TerminalTests.declare
    register = fixtures.TerminalTests.register
    put = fixtures.TerminalTests.put

    def setUp(self):
        fixtures.TerminalTests.setUp(self)
        fixtures.TerminalTests.capture(self)
        self.paths = {self.batch_id: str(self.plan_directory / f'terminal-attempt-{self.batch_id}.json')}

    def capture(self):
        return subject.capture(self.plan_directory, self.paths, clock=lambda: self.clock(8))

    def verify(self):
        return subject.verify(self.plan_directory, clock=lambda: self.clock(9))

    def test_complete_incomplete_attempt_is_not_success(self):
        result = self.capture()
        self.assertTrue(result['run_terminal'])
        self.assertFalse(result['succeeded'])
        self.assertEqual(result['outcomes']['incomplete'], 1)
        self.assertEqual(result['planned_sessions'], 50)
        self.assertFalse(self.verify()['succeeded'])

    def test_missing_batch_rejected(self):
        self.paths.clear()
        with self.assertRaisesRegex(ValueError, 'every declared'): self.capture()

    def test_extra_batch_rejected(self):
        self.paths[str(uuid.uuid4())] = next(iter(self.paths.values()))
        with self.assertRaisesRegex(ValueError, 'every declared'): self.capture()

    def test_foreign_terminal_path_rejected(self):
        path = self.root / 'copy.json'
        path.write_bytes(next(self.plan_directory.glob('terminal-attempt-*')).read_bytes())
        path.chmod(0o600)
        self.paths[self.batch_id] = str(path)
        with self.assertRaisesRegex(ValueError, 'original attempt terminal path'): self.capture()

    def test_saved_summary_cannot_claim_success(self):
        record = self.capture(); record['succeeded'] = True
        self.put(self.plan_directory/'run-terminal.json', record)
        with self.assertRaisesRegex(ValueError, 'differs'): self.verify()

    def test_original_cleanup_failure_after_capture_invalidates_summary(self):
        self.capture()
        self.mock_verify.side_effect = ValueError('original cleanup changed')
        with self.assertRaisesRegex(ValueError, 'cleanup changed'): self.verify()

    def test_second_pass_dependency_failure_prevents_write(self):
        original = subject.collect
        calls = []
        def collect(*args, **kwargs):
            calls.append(True)
            if len(calls) == 2:
                self.mock_verify.side_effect = ValueError('cleanup changed')
            return original(*args, **kwargs)
        with patch.object(subject, 'collect', side_effect=collect):
            with self.assertRaisesRegex(ValueError, 'cleanup changed'): self.capture()
        self.assertFalse((self.plan_directory/'run-terminal.json').exists())

    def test_extra_activation_binding_rejected(self):
        self.put(self.plan_directory/f'batch-{uuid.uuid4()}.json', {})
        with self.assertRaisesRegex(ValueError, 'binding set'): self.capture()

    def test_terminal_timestamp_cannot_precede_child(self):
        record = self.capture(); record['observed'] = self.clock(4)
        self.put(self.plan_directory/'run-terminal.json', record)
        with self.assertRaisesRegex(ValueError, 'clock order'): self.verify()

    def test_no_overwrite(self):
        self.capture()
        with self.assertRaisesRegex(ValueError, 'already exists'): self.capture()

    def activated(self, wrong_runner=False, wrong_invocation=False):
        binding = dict(job_id=str(uuid.uuid4()), action_id=str(uuid.uuid4()),
            cohort_id=str(uuid.uuid4()), workspace_id=self.value['workspace_id'],
            mode='b', boot_id=self.boot, config_path=str(self.root/'config.json'))
        path = self.root/'job-terminal.json'
        record = dict(binding, kind='standby_job_terminal', outcome='succeeded',
            observed=self.clock(7), runner_directory=str(self.root if wrong_runner else self.output))
        self.put(path, record)
        self.paths = {self.batch_id: str(path)}
        bp = self.plan_directory/f'batch-{self.batch_id}.json'
        self.put(bp, binding)
        if wrong_invocation:
            self.intent['invocation_sha256'] = '0'*64
            self.put(self.output/'runner-intent.json', self.intent)
        # Isolate settlement/UI and completion validation here; original
        # attempt registration, invocation and runner-intent checks are real.
        with patch.object(subject.RunManifest, '_binding_record',
                          return_value=(binding, bp.read_bytes())), \
             patch.object(subject.RunManifest, '_original_binding'), \
             patch.object(subject.jobs, 'verify', return_value={
                 'terminal_sha256': subject._sha(path.read_bytes()), 'outcome': 'succeeded'}):
            return self.capture()

    def test_activated_original_runner_can_complete(self):
        self.assertTrue(self.activated()['succeeded'])

    def test_activated_foreign_runner_rejected(self):
        with self.assertRaisesRegex(ValueError, 'registered batch'):
            self.activated(wrong_runner=True)

    def test_activated_foreign_invocation_rejected(self):
        with self.assertRaisesRegex(ValueError, 'registered invocation'):
            self.activated(wrong_invocation=True)


if __name__ == '__main__':
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import standby_job_terminal as terminal


class TerminalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.root.chmod(0o700)
        self.boot = '00000000-0000-0000-0000-000000000001'
        self.saved = dict(job_id='job', action_id='action', cohort_id='cohort',
                          workspace_id='workspace', boot_id=self.boot, mode='b')
        self.config = self.root / 'config.json'
        self.completion = self.root / 'completion.json'
        self.write(self.completion, {'finished': self.clock(2)})
        self.write(self.root / 'baseline.json', {'fixture': 'baseline'})
        self.intent = dict(version=1, kind='standby_runner_intent', invocation_id='invocation',
            invocation_sha256='sha', workspace_id='workspace', mode='b', boot_id=self.boot,
            started_at=1, started_monotonic=1)
        self.opened = dict(self.saved, version=1, kind='standby_runner_open',
            invocation_id='invocation', invocation_sha256='sha', config_path=str(self.config),
            job_path=str(self.root / 'job.json'))
        self.closed = dict(version=1, kind='standby_runner_closed', invocation_id='invocation',
            closed_at=3, closed_monotonic=3, handles_closed=True, close_error_type=None,
            error_type=None, reason='cancelled', all_resources_released=True,
            communication_resources={'routes': {'resources_released': True},
                                     'owner_endpoint': {'resources_released': True}},
            source_resources={'resources_released': True})

    def clock(self, n):
        return {'wall': n, 'monotonic': n, 'boot_id': self.boot}

    def write(self, path, value):
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def run_capture(self, cleanup_passed=True, mutate=None, completed=True):
        for name, value in [('intent', self.intent), ('open', self.opened), ('closed', self.closed)]:
            self.write(self.root / f'runner-{name}.json', value)
        verified = {'observation_sha256': terminal._sha(self.completion.read_bytes()),
                    'observation_path': str(self.completion), 'verified': self.clock(3)}
        def cleanup(*args, **kwargs):
            value = {'passed': cleanup_passed, 'started': self.clock(4), 'finished': self.clock(5)}
            self.write(self.root / 'cleanup-observation.json', value)
            if mutate:
                mutate()
            return value
        with patch.object(terminal, 'read_settlement', return_value=(self.saved, 'settlement')), \
                patch.object(terminal.batch, 'job_path', return_value=self.root / 'job.json'), \
                patch.object(terminal, 'verify_capture', return_value=verified), \
                patch.object(terminal, 'capture_cleanup', side_effect=cleanup):
            return terminal.capture(self.config, 'job', self.root, runner_directory=self.root,
                completion_path=self.completion if completed else None, baseline_path=self.root / 'baseline.json',
                client=object(), clock=lambda: self.clock(6))

    def test_joined_success_not_run_terminal(self):
        result = self.run_capture()
        self.assertTrue(result['job_terminal'])
        self.assertFalse(result['run_terminal'])
        self.assertEqual(result['outcome'], 'succeeded')

    def test_cancelled_without_completion_is_not_success(self):
        result = self.run_capture(completed=False)
        self.assertEqual(result['outcome'], 'cancelled')
        self.assertIsNone(result['completion'])

    def test_timeout_without_completion(self):
        self.closed['reason'] = 'lifetime_expired'
        self.assertEqual(self.run_capture(completed=False)['outcome'], 'timed_out')

    def test_failed_runner_can_settle_but_not_succeed(self):
        self.closed.update(reason='failed', error_type='ValueError')
        self.assertEqual(self.run_capture(completed=False)['outcome'], 'failed')

    def test_closed_without_completion_is_incomplete(self):
        self.closed['reason'] = 'service_closed'
        self.assertEqual(self.run_capture(completed=False)['outcome'], 'incomplete')

    def test_failed_outcome_still_requires_cleanup(self):
        self.closed.update(reason='failed', error_type='ValueError')
        with self.assertRaisesRegex(ValueError, 'cleanup failed'):
            self.run_capture(completed=False, cleanup_passed=False)
        self.assertFalse((self.root / 'job-terminal.json').exists())

    def reverify(self, *, mutate=None, completed=True, during_cleanup=None, completion_check=None):
        result = self.run_capture(completed=completed)
        if mutate:
            mutate(result)
            self.write(self.root / 'job-terminal.json', result)
        cleanup = {'observation_sha256': result['cleanup_sha256'],
                   'started': self.clock(4), 'finished': self.clock(5)}
        # Existing capture fixture mocks cleanup; give its saved record the
        # baseline reference required by the terminal join's own checks.
        path = self.root / 'cleanup-observation.json'
        value = json.loads(path.read_text())
        value['baseline_path'] = str(self.root / 'baseline.json')
        self.write(path, value)
        result['cleanup_sha256'] = terminal._sha(path.read_bytes())
        cleanup['observation_sha256'] = result['cleanup_sha256']
        self.write(self.root / 'job-terminal.json', result)
        fresh = dict(result['completion'] or {}, verified=self.clock(8))
        def check_cleanup(*args, **kwargs):
            if during_cleanup:
                during_cleanup()
            return cleanup
        def check_completion(*args, **kwargs):
            if completion_check:
                return completion_check(fresh)
            return fresh
        with patch.object(terminal, 'read_settlement', return_value=(self.saved, 'settlement')), \
                patch.object(terminal.batch, 'job_path', return_value=self.root / 'job.json'), \
                patch.object(terminal, 'verify_capture', side_effect=check_completion), \
                patch.object(terminal, 'verify_cleanup', side_effect=check_cleanup):
            return terminal.verify(self.config, 'job', self.root / 'job-terminal.json',
                                   clock=lambda: self.clock(9))

    def test_terminal_reverify_later_clock(self):
        self.assertEqual(self.reverify()['outcome'], 'succeeded')

    def test_transcript_invalidated_during_cleanup_rejected(self):
        state = {'changed': False}
        def check(fresh):
            if state['changed']:
                raise ValueError('transcript original changed')
            return fresh
        with self.assertRaisesRegex(ValueError, 'transcript original changed'):
            self.reverify(during_cleanup=lambda: state.update(changed=True), completion_check=check)

    def test_completion_binding_changed_during_cleanup_rejected(self):
        state = {'changed': False}
        def check(fresh):
            return dict(fresh, observation_sha256='other') if state['changed'] else fresh
        with self.assertRaisesRegex(ValueError, 'completion changed during cleanup'):
            self.reverify(during_cleanup=lambda: state.update(changed=True), completion_check=check)

    def test_terminal_reverify_cancelled(self):
        self.assertEqual(self.reverify(completed=False)['outcome'], 'cancelled')

    def test_terminal_reverify_cannot_relabel_failure(self):
        with self.assertRaisesRegex(ValueError, 'outcome differs'):
            self.reverify(completed=False, mutate=lambda r: r.update(outcome='timed_out'))

    def test_terminal_reverify_rejects_foreign_job(self):
        with self.assertRaisesRegex(ValueError, 'job binding'):
            self.reverify(mutate=lambda r: r.update(job_id='other'))

    def test_terminal_reverify_rejects_missing_closure(self):
        with self.assertRaisesRegex(ValueError, 'closure originals'):
            self.reverify(mutate=lambda r: r['closure_originals'].clear())

    def test_foreign_runner_rejected(self):
        self.opened['job_id'] = 'other'
        with self.assertRaisesRegex(ValueError, 'different original job'):
            self.run_capture()

    def test_resource_summary_cannot_override_source_failure(self):
        self.closed['source_resources']['resources_released'] = False
        with self.assertRaisesRegex(ValueError, 'resource closure'):
            self.run_capture()

    def test_runner_error_not_success(self):
        self.closed['error_type'] = 'ValueError'
        with self.assertRaisesRegex(ValueError, 'resource closure'):
            self.run_capture()

    def test_failed_cleanup_kept_without_terminal(self):
        with self.assertRaisesRegex(ValueError, 'cleanup failed'):
            self.run_capture(cleanup_passed=False)
        self.assertTrue((self.root / 'cleanup-observation.json').exists())
        self.assertFalse((self.root / 'job-terminal.json').exists())

    def test_runner_drift_during_cleanup_rejected(self):
        with self.assertRaisesRegex(ValueError, 'originals changed'):
            self.run_capture(mutate=lambda: self.write(self.root / 'runner-closed.json', {}))

    def test_completion_after_closure_rejected(self):
        self.write(self.completion, {'finished': self.clock(4)})
        with self.assertRaisesRegex(ValueError, 'clock order'):
            self.run_capture()


if __name__ == '__main__':
    unittest.main()

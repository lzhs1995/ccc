"""Synthetic clocks/transcripts only; no native launch or network request."""
import copy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
import uuid

import cmux_codex_watch as core
import ccc_batch_timing as ui
import ccc_standby_acceptance as acceptance
import ccc_standby_timing as timing
from tests import test_standby_acceptance as acceptance_tests
from tests import test_standby_manager as manager_tests


class ObservationTests(unittest.TestCase):
    setUp = acceptance_tests.FirstTaskTests.setUp
    start_native = acceptance_tests.FirstTaskTests.start_native
    hook_fixture = acceptance_tests.FirstTaskTests.hook_fixture
    bind = acceptance_tests.FirstTaskTests.bind
    task = acceptance_tests.FirstTaskTests.task

    def test_started_before_prompt_preserves_clock_across_restart(self):
        self.task(turn=str(uuid.uuid4()))
        lines = self.transcript.read_bytes().splitlines(keepends=True)
        self.transcript.write_bytes(b''.join(lines[:-1]))
        observed = dict(wall=self.submitted + .1, monotonic=10.1, boot_id=self.boot)
        self.observer.observation_clock = lambda: copy.deepcopy(observed)
        self.assertIsNone(self.observer.poll(0, release=False))
        path = self.worker.path.parent / 'standby-first-observation-0.json'
        self.assertEqual(json.loads(path.read_bytes())['observed'], observed)
        with self.transcript.open('ab') as stream:
            stream.write(lines[-1])
        restarted = acceptance.FirstTaskObserver(self.config, self.worker.job['id'], self.action,
            client=self.client, observation_clock=lambda: self.fail('restart replaced first clock'))
        result = restarted.poll(0, release=False)
        self.assertEqual(result['first_task_observed'], observed)
        self.assertEqual(self.worker.path.read_bytes(), self.job_before)

    def test_observation_wrong_boot_never_certifies(self):
        self.task()
        self.observer.observation_clock = lambda: dict(wall=self.submitted, monotonic=10, boot_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(ValueError, 'clock'):
            self.observer.poll(0)
        self.assertFalse(self.result_path.exists())

    def test_pending_first_observation_rewrite_cannot_revive_on_restart(self):
        self.task(turn='original')
        lines = self.transcript.read_bytes().splitlines(keepends=True)
        self.transcript.write_bytes(b''.join(lines[:-1]))
        self.assertIsNone(self.observer.poll(0))
        self.transcript.write_bytes(b''.join(lines).replace(b'original', b'replaced'))
        restarted = acceptance.FirstTaskObserver(self.config, self.worker.job['id'], self.action, client=self.client)
        with self.assertRaises(ValueError):
            restarted.poll(0)


class CommitTests(unittest.TestCase):
    setUp = manager_tests.ManagerTests.setUp

    def test_committed_observer_precedes_all_sends(self):
        self.manager.refresh()
        called = []
        def committed():
            self.assertTrue((self.directory / 'activation.json').exists())
            self.assertTrue((self.directory / 'activation-attempt.json').exists())
            self.assertEqual(self.sent, [])
            called.append(True)
        result = self.manager.activate(action_id=self.action, mode='b', prompt='fixed prompt', committed=committed)
        self.assertTrue(result['new_activation'])
        self.assertEqual(called, [True])
        self.assertEqual(len(self.sent), 50)
        self.assertFalse(self.manager.activate(action_id=self.action, mode='b', prompt='fixed prompt',
            committed=lambda: self.fail('replayed commit'))['new_activation'])

    def test_commit_persistence_error_is_consumed_without_sends(self):
        self.manager.refresh()
        with self.assertRaises(OSError):
            self.manager.activate(action_id=self.action, mode='b', prompt='fixed prompt',
                committed=lambda: (_ for _ in ()).throw(OSError('disk full')))
        self.assertEqual(self.sent, [])
        self.assertTrue((self.directory / 'activation-attempt.json').exists())
        self.assertEqual(self.manager.state, 'invalidated')

    def test_cancel_in_commit_observer_prevents_sends(self):
        self.manager.refresh()
        with self.assertRaises(ValueError):
            self.manager.activate(action_id=self.action, mode='b', prompt='fixed prompt',
                committed=lambda: self.manager.invalidate('cancel'))
        self.assertEqual(self.sent, [])


class TimingTests(unittest.TestCase):
    start_native = acceptance_tests.FirstTaskTests.start_native
    hook_fixture = acceptance_tests.FirstTaskTests.hook_fixture
    bind = acceptance_tests.FirstTaskTests.bind
    task = acceptance_tests.FirstTaskTests.task

    def setUp(self):
        acceptance_tests.FirstTaskTests.setUp(self)
        self.hashes = {'runtime.py': 'a' * 64}
        # The Hook fixture binds one slot; make all synthetic job launch IDs
        # match its synthetic roster before capturing this test's immutable job.
        for slot, original in zip(self.worker.job['slots'], self.observer.originals):
            slot['launch_id'] = original['launch_id']
        self.worker.save()
        self.observer = acceptance.FirstTaskObserver(self.config, self.worker.job['id'], self.action,
            client=self.client, observation_clock=lambda: self.stamp(10.1))
        self.origin = {'version': 1, 'action_id': self.action, 'workspace_id': self.wid,
            'mode': 'b', 'input_kind': 'keyboard', 'row_kind': 'group', 'source_hashes': self.hashes,
            'events': [{'phase': phase, **self.stamp(9.5 + i * .1)}
                       for i, phase in enumerate(timing.ORIGIN_PHASES)]}
        self.bridge = timing.ActivationTiming(self.config, self.worker.job['id'], self.origin,
            clock=lambda: self.stamp(10), current_hashes=lambda: self.hashes)
        self.before = self.worker.path.read_bytes()

    def stamp(self, monotonic):
        return dict(wall=self.submitted + monotonic - 10, monotonic=monotonic, boot_id=self.boot)

    def terminal(self, receipt, tasks=None):
        if tasks is None:
            tasks = [{'index': i, 'original': r, 'turn_id': str(uuid.uuid4()),
                'task_at': datetime.fromtimestamp(self.submitted + .01, timezone.utc).isoformat(),
                'observed': self.stamp(10.2), 'task_receipt_sha256': 'a'*64,
                'observation_receipt_sha256': 'b'*64} for i,r in enumerate(receipt['originals'])]
        return {'version': 2, 'kind': 'standby_activation_terminal',
            **{k: receipt[k] for k in ('job_id', 'job_sha256', 'action_id', 'cohort_id',
                'workspace_id', 'boot_id', 'mode', 'evidence_sha256')},
            'activation_ui_sha256': timing._sha(timing._serialized(receipt)),
            'outcome': 'complete', 'tasks': tasks, 'event': {'phase': 'activation_terminal', **self.stamp(11)}}

    def test_independent_activation_bridge_preserves_job_and_two_origins(self):
        receipt = self.bridge.committed()
        terminal = self.terminal(receipt)
        result = timing.evaluate(receipt, terminal, current_hashes=self.hashes)
        self.assertEqual(result['problems'], [])
        self.assertTrue(result['startup_passed'])
        self.assertAlmostEqual(result['original_tasks'][0]['input_upper_bound_seconds'], .7)
        self.assertAlmostEqual(result['original_tasks'][0]['confirmation_upper_bound_seconds'], .6)
        self.assertEqual(self.worker.path.read_bytes(), self.before)
        self.assertNotIn('job_created', json.dumps(receipt))
        self.assertNotIn('ui_timing_origin', json.loads(self.before))

    def test_missing_task_never_complete_even_with_ack_or_action_finished(self):
        receipt = self.bridge.committed()
        terminal = self.terminal(receipt)
        terminal['tasks'].pop()
        terminal['acknowledged_inputs'] = 50
        self.assertFalse(timing.evaluate(receipt, terminal, current_hashes=self.hashes)['startup_passed'])
        terminal['event']['phase'] = 'action_finished'
        self.assertFalse(timing.evaluate(receipt, terminal, current_hashes=self.hashes)['evidence_complete'])

    def test_wrong_identity_boot_reuse_source_and_clock_fail(self):
        receipt = self.bridge.committed()
        good = self.terminal(receipt)
        changes = [lambda t: t.update(action_id=str(uuid.uuid4())),
            lambda t: t['tasks'][0]['observed'].update(boot_id=str(uuid.uuid4())),
            lambda t: t['tasks'][0].update(turn_id=t['tasks'][1]['turn_id']),
            lambda t: t['tasks'][0]['original'].update(pid=999),
            lambda t: t['tasks'][0]['observed'].update(wall=self.submitted + 8),
            lambda t: t['tasks'][0]['observed'].update(monotonic=9)]
        for change in changes:
            with self.subTest(change=change):
                terminal = copy.deepcopy(good); change(terminal)
                self.assertFalse(timing.evaluate(receipt, terminal, current_hashes=self.hashes)['evidence_complete'])
        self.assertFalse(timing.evaluate(receipt, good, current_hashes={})['startup_passed'])

    def test_slow_observation_is_not_proven_not_native_late(self):
        receipt = self.bridge.committed(); terminal = self.terminal(receipt)
        terminal['tasks'][0]['observed'] = self.stamp(10.8)
        result = timing.evaluate(receipt, terminal, current_hashes=self.hashes)
        self.assertTrue(result['evidence_complete'])
        self.assertFalse(result['startup_passed'])
        self.assertEqual(result['verdict'], 'not_proven')

    def test_task_receipt_hashes_required_for_evidence_complete(self):
        receipt = self.bridge.committed()
        for key in ('task_receipt_sha256', 'observation_receipt_sha256'):
            for value in (None, '', 'a' * 63, 'g' * 64, 123):
                with self.subTest(key=key, value=value):
                    terminal = self.terminal(receipt)
                    if value is None:
                        terminal['tasks'][0].pop(key)
                    else:
                        terminal['tasks'][0][key] = value
                    result = timing.evaluate(receipt, terminal, current_hashes=self.hashes)
                    self.assertFalse(result['evidence_complete'])
                    self.assertFalse(result['startup_passed'])

    def test_old_job_created_origin_and_wrong_source_reject(self):
        for phase in ('job_created', 'job_reused', 'action_finished'):
            origin = copy.deepcopy(self.origin)
            origin['events'].append({'phase': phase, **self.stamp(10)})
            with self.assertRaises(ValueError):
                timing.ActivationTiming(self.config, self.worker.job['id'], origin, current_hashes=lambda: self.hashes)
        with self.assertRaises(ValueError):
            timing.ActivationTiming(self.config, self.worker.job['id'], self.origin, current_hashes=lambda: {})

    def test_timeout_terminal_reads_real_task_receipts_and_persists_separately(self):
        self.bridge.committed()
        self.task(turn=str(uuid.uuid4()))
        self.observer.poll(0, release=False)
        self.bridge.clock = lambda: self.stamp(11)
        with self.assertRaisesRegex(ValueError, '50'):
            self.bridge.finish(outcome='complete')
        terminal = self.bridge.finish(outcome='timeout', reason='49 originals pending')
        self.assertEqual(len(terminal['tasks']), 1)
        self.assertEqual(json.loads(self.bridge.terminal.read_bytes()), terminal)
        self.assertEqual(self.worker.path.read_bytes(), self.before)

    def test_receipt_edit_and_transcript_edit_reject_terminal(self):
        self.bridge.committed(); self.task(turn=str(uuid.uuid4()))
        self.observer.poll(0, release=False)
        self.transcript.write_bytes(self.transcript.read_bytes().replace(b'session_meta', b'changed_meta'))
        with self.assertRaisesRegex(ValueError, 'prefix'):
            self.bridge.finish(outcome='timeout', reason='test')
        self.assertFalse(self.bridge.terminal.exists())

    def test_canonical_commit_clock_edit_cannot_replace_original_receipt(self):
        receipt = self.bridge.committed()
        receipt['event']['wall'] += .005
        receipt['event']['monotonic'] += .005
        self.bridge.receipt.write_bytes(timing._serialized(receipt))
        with self.assertRaisesRegex(ValueError, 'receipt changed'):
            self.bridge.finish(outcome='timeout', reason='49 pending')
        self.assertFalse(self.bridge.terminal.exists())

    def test_commit_cannot_repeat_or_use_modified_job(self):
        self.bridge.committed()
        with self.assertRaises(FileExistsError):
            self.bridge.committed()
        job = json.loads(self.before); job['extra'] = 'changed'
        core.atomic_write_json(self.worker.path, job)
        with self.assertRaisesRegex(ValueError, 'job changed'):
            self.bridge.finish(outcome='timeout', reason='test')


if __name__ == '__main__':
    unittest.main()

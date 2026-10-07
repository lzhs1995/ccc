"""Startup read suppression must expire and resume on changed disk evidence."""
import copy
import time
import unittest
from unittest.mock import Mock, patch

import cmux_codex_watch as core
from tests import test_workspace_batch, test_batch_argv_initial


class BatchObservationHoldTests(unittest.TestCase):
    def setUp(self):
        test_workspace_batch.WorkspaceBatchTests.setUp(self)
        test_batch_argv_initial.ArgvInitialTests.prepare(self)
        self.daemon = core.WatchDaemon(self.config, self.root / 'state.json', client=self.client)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.target = {'workspace_id': self.wid, 'surface_id': self.slot['surface_id']}

    def held(self):
        return self.daemon._argv_startup_observation_held(self.target)

    def test_standby_hint_skips_only_periodic_read_without_fabricating_observation(self):
        hint = self.daemon._standby_periodic_hint = Mock()
        hint.covered.return_value = True
        runtime = self.daemon.runtime[self.target['surface_id']] = core.TargetRuntime()
        previous = runtime.observation_completed_at
        with patch.object(self.daemon, '_process_one_target') as process:
            self.daemon._scheduled_observe(self.target, lambda: True)
            process.assert_not_called()
            self.assertEqual(runtime.observation_completed_at, previous)
            hint.covered.return_value = False
            self.daemon._scheduled_observe(self.target, lambda: True)
            process.assert_called_once()

    def test_native_failure_bypasses_even_a_positive_standby_hint(self):
        hint = self.daemon._standby_periodic_hint = Mock()
        hint.covered.return_value = True
        runtime = self.daemon.runtime[self.target['surface_id']] = core.TargetRuntime()
        runtime.native_failure_at = time.time()
        with patch.object(self.daemon, '_process_one_target') as process:
            self.daemon._scheduled_observe(self.target, lambda: True)
            process.assert_called_once()
            hint.covered.assert_not_called()
        runtime.native_failure_at = 0
        with patch.object(self.daemon, '_active_send_target', return_value=self.target), \
                patch.object(self.daemon, '_process_one_target') as process:
            self.daemon._scheduled_native(self.target, lambda: True)
            process.assert_called_once()
            hint.covered.assert_not_called()

    def test_common_observation_entry_skips_then_resumes_after_hold_release(self):
        with patch.object(self.daemon, '_observe_target_viewport', side_effect=AssertionError('read resumed')) as read:
            self.assertIsNone(self.daemon._process_one_target(self.target, self.client, None, ''))
            read.assert_not_called()
            self.store.mutate(lambda config: config['workspace_rules'][0]['batch_start_holds'].clear())
            with self.assertRaisesRegex(AssertionError, 'read resumed'):
                self.daemon._process_one_target(self.target, self.client, None, '')

    def test_expired_or_future_hold_restores_observation(self):
        for age in (31, -2):
            with self.subTest(age=age):
                self.store.mutate(lambda c: c['workspace_rules'][0]['batch_start_holds'][self.target['surface_id']].update(
                    created_at=time.time() - age))
                self.assertFalse(self.held())

    def test_job_cache_invalidates_policy_slot_and_phase_changes(self):
        original = copy.deepcopy(self.worker.job)
        changes = [lambda j:j.pop('initial_prompt_policy'),
                   lambda j:j['slots'][0].update(launch_id='replacement'),
                   lambda j:j['slots'][0].update(surface_id='replacement'),
                   lambda j:j['slots'][0].update(phase='confirmed'),
                   lambda j:j.update(workspace_id='replacement'),
                   lambda j:j.update(status='cancelled')]
        for change in changes:
            core.atomic_write_json(self.worker.path, original)
            self.assertTrue(self.held())
            changed = copy.deepcopy(original)
            change(changed)
            core.atomic_write_json(self.worker.path, changed)
            self.assertFalse(self.held())

    def test_missing_or_changed_registration_restores_observation(self):
        self.assertTrue(self.held())
        raw = self.receipt.read_bytes()
        self.receipt.unlink()
        self.assertFalse(self.held())
        self.receipt.write_bytes(raw)
        self.assertTrue(self.held())
        receipt = core.load_json(self.receipt, {})
        receipt['launch_id'] = 'replacement'
        core.atomic_write_json(self.receipt, receipt)
        self.assertFalse(self.held())

    def test_missing_job_and_legacy_reason_do_not_suppress_reads(self):
        self.worker.path.unlink()
        self.assertFalse(self.held())
        def legacy(c):
            rule = c['workspace_rules'][0]
            rule['batch_start_holds'].clear()
            rule.setdefault('excluded_surface_reasons', {})[self.target['surface_id']] = (
                'batch:' + self.worker.job['id'] + ':initial')
        self.store.mutate(legacy)
        self.assertFalse(self.held())

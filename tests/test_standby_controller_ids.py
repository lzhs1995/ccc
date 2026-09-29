"""cmux's uppercase IDs remain intact across the actual standby adapters.

Native processes are synthetic; real ledger files, holds, locks and local
socket transport are used. This is not native readiness or timing acceptance.
"""
import copy
import unittest
import uuid
from unittest.mock import patch

import ccc_native_standby as standby
import ccc_standby_factory as factory
from tests import test_standby_factory as factories
from tests import test_standby_prepare as preparations
from tests import test_standby_launch as launches
from tests import test_standby_acceptance as acceptances
from tests import test_standby_timing as timings
from tests import test_standby_activation as activations


class FactoryIds(unittest.TestCase):
    controller_id = staticmethod(str.upper)
    setUp = factories.FactoryTests.setUp
    admit = factories.FactoryTests.admit

    def test_original_rule_job_ledger_endpoint_and_lock_spelling(self):
        real_lock = factory.core.workspace_input_lock
        with patch.object(factory.core, 'workspace_input_lock', wraps=real_lock) as lock:
            owner = self.admit()
        prep = owner.service.preparation
        self.assertEqual(prep.job['workspace_id'], self.wid)
        self.assertEqual(prep.selected['workspace_id'], self.wid)
        self.assertEqual(owner.service.activation.ledger.manifest['workspace_id'], self.wid)
        self.assertTrue(any(call.args[1] == self.wid for call in lock.call_args_list))
        self.assertFalse(any(call.args[1] == self.wid.lower() for call in lock.call_args_list))
        self.assertEqual(owner.status()['workspace_id'], self.wid)
        self.client.new_codex_surface.assert_not_called()

    def test_foreign_workspace_does_not_gain_permission(self):
        self.wid = str(uuid.uuid4()).upper()
        with self.assertRaises(RuntimeError):
            self.admit()
        self.capture.assert_not_called()


class PrepareIds(unittest.TestCase):
    controller_id = staticmethod(str.upper)
    setUp = preparations.PreparationTests.setUp
    revoke = preparations.PreparationTests.revoke
    test_original_create_once = preparations.PreparationTests.test_original_creation_intent_precedes_write_and_is_never_replayed
    test_revoke_before_create = preparations.PreparationTests.test_pause_during_connection_refuses_create_and_does_not_revive
    test_missing_original_hold = preparations.PreparationTests.test_missing_hold_is_denied


class LaunchIds(unittest.TestCase):
    controller_id = staticmethod(str.upper)
    setUp = launches.StandbyLaunchTests.setUp
    start_native = launches.StandbyLaunchTests.start_native
    hook_fixture = launches.StandbyLaunchTests.hook_fixture
    bind = launches.StandbyLaunchTests.bind
    test_exec_claim_and_original_hold = launches.StandbyLaunchTests.test_exact_no_prompt_argv_is_consumed_before_guarded_exec
    test_hook_to_original_session = launches.StandbyLaunchTests.test_postactivation_hook_matches_original_session_and_consumes_callback
    test_foreign_hook_refused = launches.StandbyLaunchTests.test_wrong_session_hook_cannot_be_repaired_by_later_startup


class FirstTaskIds(unittest.TestCase):
    controller_id = staticmethod(str.upper)
    setUp = acceptances.FirstTaskTests.setUp
    start_native = acceptances.FirstTaskTests.start_native
    hook_fixture = acceptances.FirstTaskTests.hook_fixture
    bind = acceptances.FirstTaskTests.bind
    task = acceptances.FirstTaskTests.task
    hold = acceptances.FirstTaskTests.hold
    test_original_hold_released_once = acceptances.FirstTaskTests.test_original_first_task_releases_only_after_receipt_without_mutating_job
    test_foreign_hold_preserved = acceptances.FirstTaskTests.test_manual_exclusion_and_foreign_hold_are_preserved


class TimingIds(unittest.TestCase):
    controller_id = staticmethod(str.upper)
    setUp = timings.TimingTests.setUp
    start_native = timings.TimingTests.start_native
    hook_fixture = timings.TimingTests.hook_fixture
    bind = timings.TimingTests.bind
    task = timings.TimingTests.task
    stamp = timings.TimingTests.stamp
    terminal = timings.TimingTests.terminal
    test_original_job_and_double_ui_origins = timings.TimingTests.test_independent_activation_bridge_preserves_job_and_two_origins


class ActivationIds(unittest.TestCase):
    guard = activations.ActivationTests.guard
    proof = activations.ActivationTests.proof
    observe = activations.ActivationTests.observe
    owner = activations.ActivationTests.owner
    start = activations.ActivationTests.start

    def setUp(self):
        activations.ActivationTests.setUp(self)
        fixture = self.fixture
        fixture.workspace = fixture.workspace.upper()
        for row in self.rows:
            row.update(workspace_id=fixture.workspace, surface_id=row['surface_id'].upper())
        # Recreate only the untouched synthetic ledger, before observing it.
        (fixture.path / 'cohort.json').unlink()
        fixture.path.rmdir()
        self.ledger = fixture.ledger = standby.StandbyLedger.create(fixture.path,
            cohort_id=fixture.cohort, workspace_id=fixture.workspace, boot_id=fixture.boot,
            mode='b', prompt='fixed prompt', config_generation=fixture.gen, clock=lambda: fixture.now)

    test_fifty_original_uppercase_socket_destinations = activations.ActivationTests.test_fifty_real_pastes_use_worker_caller_guard_and_never_replay
    test_last_revoke_still_refuses = activations.ActivationTests.test_caller_revoked_after_connect_has_zero_socket_writes

    def test_duplicate_surface_with_different_case_rejected(self):
        rows = copy.deepcopy(self.rows)
        rows[1]['surface_id'] = rows[0]['surface_id'].lower()
        with self.assertRaisesRegex(ValueError, 'not unique'):
            self.ledger.observe_ready(rows, config_generation=self.fixture.gen,
                boot_id=self.fixture.boot, authorized=True)
        self.assertFalse((self.ledger.directory / 'originals.json').exists())

    def test_controller_identity_validation_rejects_non_uuid(self):
        for value in (None, 'surface:4', 'prefix-' + str(uuid.uuid4()), 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                standby.controller_identifier(value)


if __name__ == '__main__':
    unittest.main()

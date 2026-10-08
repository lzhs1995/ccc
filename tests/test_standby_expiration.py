"""Real manager/ledger, simulated clock and originals; no native inputs."""
import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import test_standby_manager as fixture


class ExpirationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.ManagerTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.reads = [0] * 50
        self.change = lambda index: None
        self.f.manager.observer = self.observe

    def observe(self, index):
        self.reads[index] += 1
        self.change(index)
        return {**copy.deepcopy(self.f.rows[index]), 'observed_monotonic': self.f.now}

    def expire_at_permission(self, *, always=False):
        fired = []
        def allowed(index):
            if index == 49 and (always or not fired):
                fired.append(True)
                self.f.now += 2.1
            return self.f.allowed
        self.f.manager.authorized = allowed
        return fired

    def test_expired_gather_reobserves_fifty_without_input(self):
        self.expire_at_permission()
        self.assertEqual(self.f.manager.refresh()['state'], 'ready')
        self.assertEqual(self.reads, [2] * 50)
        self.assertEqual(self.f.ledger._ready['observed_at'], self.f.now)
        self.assertFalse(self.f.sent)
        self.assertFalse(self.f.ledger._consumed())

    def test_expired_subset_reobserves_complete_cohort(self):
        def observe(index):
            row = self.observe(index)
            if index < 3 and self.reads[index] == 1:
                row['observed_monotonic'] -= 2.1
            return row
        self.f.manager.observer = observe
        advanced = []
        def allowed(index):
            if index == 49 and not advanced:
                advanced.append(True)
                self.f.now += .1
            return self.f.allowed
        self.f.manager.authorized = allowed
        self.assertEqual(self.f.manager.refresh()['state'], 'ready')
        self.assertEqual(self.reads, [2] * 50)
        self.assertFalse(self.f.sent)
        self.assertFalse(self.f.ledger._consumed())

    def test_permission_withdrawal_during_reobservation_refuses(self):
        self.expire_at_permission()
        def change(index):
            if index == 0 and self.reads[index] == 2:
                self.f.allowed = False
        self.change = change
        with self.assertRaisesRegex(ValueError, 'authorization'):
            self.f.manager.refresh()
        self.assertFalse(self.f.sent)
        self.assertEqual(self.f.manager.status()['state'], 'invalidated')

    def test_writer_change_during_initial_expiration_is_permanent(self):
        self.expire_at_permission()
        def change(index):
            if index == 0 and self.reads[index] == 2:
                self.f.rows[index]['writer_identity'][1] += 1
        self.change = change
        with self.assertRaisesRegex(ValueError, 'original.*changed'):
            self.f.manager.refresh()
        self.assertFalse(self.f.sent)
        self.assertEqual(self.f.manager.status()['state'], 'invalidated')

    def test_stale_busy_row_does_not_mask_real_failure(self):
        self.expire_at_permission()
        self.f.rows[49]['task_count'] = 1
        with self.assertRaisesRegex(ValueError, 'untouched idle'):
            self.f.manager.refresh()
        self.assertEqual(self.reads, [1] * 50)
        self.assertFalse(self.f.sent)

    def test_stale_foreign_boot_does_not_retry(self):
        self.expire_at_permission()
        self.f.rows[49]['boot_id'] = 'foreign'
        with self.assertRaisesRegex(ValueError, 'foreign'):
            self.f.manager.refresh()
        self.assertEqual(self.reads, [1] * 50)

    def test_cached_observer_cannot_renew_expiration(self):
        self.f.manager.observer = lambda i: copy.deepcopy(self.f.rows[i])
        self.expire_at_permission()
        with self.assertRaisesRegex(ValueError, 'did not advance'):
            self.f.manager.refresh()
        self.assertFalse(self.f.ledger._consumed())
        self.assertFalse(self.f.sent)

    def test_repeated_expiration_has_fixed_deadline(self):
        fired = self.expire_at_permission(always=True)
        start = self.f.now
        # Instrumentation may read the clock without advancing time. Advance
        # the clock at the simulated slow permission operation instead.
        clock = lambda: self.f.now - start
        with mock.patch('ccc_standby_manager.time', SimpleNamespace(monotonic=clock)):
            with self.assertRaises(TimeoutError):
                self.f.manager.refresh()
        self.assertGreater(len(fired), 1)
        self.assertGreaterEqual(self.f.now - start, 30)
        self.assertLess(self.f.now - start, 32.1)
        self.assertFalse(self.f.ledger._consumed())
        self.assertFalse(self.f.sent)

    def test_expiration_at_consumption_rechecks_before_single_activation(self):
        self.f.manager.refresh()
        consume = self.f.ledger.consume_activation
        calls = []
        def delayed(**kwargs):
            calls.append(True)
            if len(calls) == 1:
                self.f.now += 2.1
            return consume(**kwargs)
        with mock.patch.object(self.f.ledger, 'consume_activation', side_effect=delayed):
            self.assertEqual(self.f.activate()['delivery']['acknowledged_inputs'], 50)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.f.sent), 50)
        self.assertFalse(self.f.activate()['new_activation'])

    def test_wrong_prompt_with_expiration_never_retries(self):
        self.f.manager.refresh()
        self.f.now += 2.1
        with self.assertRaisesRegex(ValueError, 'activation rejected'):
            self.f.ledger.consume_activation(action_id=self.f.action, mode='b',
                prompt='wrong', config_generation=self.f.gen, boot_id=self.f.boot, authorized=True)
        self.assertTrue(self.f.ledger._invalid)
        self.assertFalse(self.f.sent)

    def test_expiration_cannot_mask_another_slots_configuration_change(self):
        self.expire_at_permission()
        self.f.rows[49]['generation'] = 'f' * 64
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            self.f.manager.refresh()
        self.assertEqual(self.reads, [1] * 50)
        self.assertFalse(self.f.ledger._consumed())

    def test_expired_post_consumption_check_never_retries_input(self):
        self.f.manager.refresh()
        self.assertTrue(self.f.ledger.consume_activation(action_id=self.f.action, mode='b',
            prompt='fixed prompt', config_generation=self.f.gen, boot_id=self.f.boot, authorized=True))
        self.f.now += 2.1
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.f.ledger.deliver(0, action_id=self.f.action,
                    observe=lambda i: self.f.rows[i], authorized=lambda i: True,
                    send=lambda *a, **k: self.fail('expired input was sent'))
        self.assertTrue(self.f.ledger._invalid)
        self.assertFalse((self.f.directory / 'input-0.json').exists())

    def test_expiration_while_waiting_for_ready_lock_reobserves(self):
        import ccc_native_standby as native
        enter = native.core.FileLock.__enter__
        locks = []
        def delayed(lock):
            value = enter(lock)
            locks.append(True)
            if len(locks) == 1:
                self.f.now += 2.1
            return value
        with mock.patch.object(native.core.FileLock, '__enter__', delayed):
            self.assertEqual(self.f.manager.refresh()['state'], 'ready')
        self.assertEqual(self.reads, [2] * 50)
        self.assertFalse(self.f.sent)

    def test_ready_cohort_expiration_does_not_allow_lost_slot(self):
        self.f.manager.refresh()
        self.expire_at_permission()
        self.f.manager.observer = lambda i: None if i == 49 else self.observe(i)
        with self.assertRaisesRegex(ValueError, 'readiness was lost'):
            self.f.manager.refresh()
        self.assertEqual(self.f.manager.status()['state'], 'invalidated')

    def test_refresh_and_consume_expiration_share_one_deadline(self):
        self.f.manager.refresh()
        wall = [0.0]
        self.expire_at_permission()
        consume = self.f.ledger.consume_activation
        calls = []
        def delayed(**kwargs):
            calls.append(True)
            wall[0] = 31.0
            self.f.now += 2.1
            return consume(**kwargs)
        with mock.patch('ccc_standby_manager.time', SimpleNamespace(monotonic=lambda: wall[0])):
            with mock.patch.object(self.f.ledger, 'consume_activation', side_effect=delayed):
                with self.assertRaises(TimeoutError):
                    self.f.activate()
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.f.ledger._consumed())
        self.assertFalse(self.f.sent)

    def test_lost_slot_after_consume_expiration_is_permanent(self):
        self.f.manager.refresh()
        consume = self.f.ledger.consume_activation
        def delayed(**kwargs):
            self.f.now += 2.1
            self.f.manager.observer = lambda i: None if i == 49 else self.observe(i)
            return consume(**kwargs)
        with mock.patch.object(self.f.ledger, 'consume_activation', side_effect=delayed):
            with self.assertRaisesRegex(ValueError, 'readiness was lost'):
                self.f.activate()
        self.assertTrue(self.f.ledger._invalid)
        self.assertFalse(self.f.ledger._consumed())
        self.assertFalse(self.f.sent)

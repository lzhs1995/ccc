import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from ccc_native_standby import StandbyLedger
from ccc_standby_manager import StandbyManager


class ManagerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / 'standby'
        self.boot, self.workspace, self.action = [str(uuid.uuid4()) for _ in range(3)]
        self.now = 100.0; self.gen = 'a' * 64; self.allowed = True
        self.ledger = StandbyLedger.create(self.directory, cohort_id=str(uuid.uuid4()),
            workspace_id=self.workspace, boot_id=self.boot, mode='b', prompt='fixed prompt',
            config_generation=self.gen, clock=lambda: self.now)
        self.rows = []
        for index in range(50):
            session = str(uuid.uuid4())
            self.rows.append(dict(index=index, launch_id=str(uuid.uuid4()), surface_id=str(uuid.uuid4()),
                session_id=session, workspace_id=self.workspace, pid=1000+index,
                birth=[12345, index], claim_sha256='b'*64, argv_sha256='c'*64,
                writer_lock='/fake/' + session + '.lock', writer_identity=[1, 10+index],
                initialized=True, idle=True, composer_empty=True, pending_approval=False,
                task_count=0, user_input_count=0, model_request_count=0, readiness_proven=True,
                observed_monotonic=self.now, boot_id=self.boot, generation=self.gen))
        self.sent = []
        def send(row, prompt, input_id, *, write_guard):
            with write_guard(): self.sent.append((row['index'], input_id))
        self.manager = StandbyManager(self.ledger, generation_current=lambda: self.gen,
            boot_current=lambda: self.boot, authorized=lambda i: self.allowed,
            observe=lambda i: copy.deepcopy(self.rows[i]), send=send)
        self.addCleanup(self.manager.close)

    def activate(self):
        return self.manager.activate(action_id=self.action, mode='b', prompt='fixed prompt')

    def test_identity_only_is_preparing_never_ready_or_sent(self):
        self.rows[0]['readiness_proven'] = False
        self.assertEqual(self.manager.refresh()['ready_originals'], 49)
        with self.assertRaises(ValueError): self.activate()
        self.assertEqual(self.sent, [])
        self.assertFalse((self.directory / 'activation.json').exists())

    def test_refresh_deadline_reports_observer_and_authorization_cost(self):
        now = [100.0]
        gather = self.manager._gather

        def timed(callback, *args):
            result = gather(callback, *args)
            now[0] += 12.0 if callback == self.manager._observe else 19.0
            return result

        with patch('ccc_standby_manager.time.monotonic', side_effect=lambda: now[0]), \
                patch.object(self.manager, '_gather', side_effect=timed):
            with self.assertRaisesRegex(TimeoutError,
                    'observation_seconds=12.000; authorization_seconds=19.000; '
                    'observation_rounds=1; expired_slots=0'):
                self.manager.refresh()
        self.assertEqual(self.manager.status()['state'], 'invalidated')
        self.assertFalse((self.directory / 'activation.json').exists())
        self.assertEqual(self.sent, [])

    def test_fifty_inputs_once_not_task_acceptance(self):
        self.assertEqual(self.manager.refresh()['state'], 'ready')
        result = self.activate()
        self.assertEqual(result['delivery']['acknowledged_inputs'], 50)
        self.assertIs(result['delivery']['native_task_acceptance_evaluated'], False)
        self.assertFalse(self.activate()['new_activation'])
        self.assertEqual(len({item[1] for item in self.sent}), 50)

    def test_delivery_error_preserves_bounded_cause_without_retry(self):
        self.manager.refresh()
        def failed(*args, **kwargs):
            try:
                raise OSError('socket observation expired')
            except OSError as cause:
                raise ValueError('guard: ' + 'x' * 2048) from cause
        self.manager.sender = failed
        result = self.activate()['delivery']
        row = next(r for r in result['outcomes'] if r['error'] == 'ValueError'
                   and r['error_chain'][0]['message'].startswith('guard:'))
        self.assertEqual(len(row['error_chain'][0]['message']), 1024)
        self.assertEqual(row['error_chain'][1]['message'], 'socket observation expired')
        self.assertFalse(row['acknowledged'])
        self.assertFalse(self.activate()['new_activation'])
        self.assertFalse(self.sent)

    def test_confirmation_after_idle_reobserves_originals_once(self):
        self.manager.refresh()
        self.now += 30
        for row in self.rows:
            row['observed_monotonic'] = self.now
        self.assertEqual(self.activate()['delivery']['acknowledged_inputs'], 50)
        self.assertFalse(self.activate()['new_activation'])
        self.assertEqual(len(self.sent), 50)

    def test_confirmation_rejects_stale_observer_without_consuming(self):
        self.manager.refresh()
        self.now += 30
        with self.assertRaises(ValueError): self.activate()
        self.assertEqual(self.sent, [])
        self.assertFalse((self.directory / 'activation.json').exists())

    def test_confirmation_rejects_lost_readiness_before_any_send(self):
        self.manager.refresh()
        self.rows[49]['readiness_proven'] = False
        with self.assertRaises(ValueError): self.activate()
        self.assertEqual(self.sent, [])
        self.assertFalse((self.directory / 'activation.json').exists())

    def test_slow_independent_authorization_does_not_age_out_whole_cohort(self):
        self.ledger.clock = time.monotonic
        def observe(index):
            return {**copy.deepcopy(self.rows[index]), 'observed_monotonic': time.monotonic()}
        self.manager.observer = observe
        self.manager.refresh()
        def authorized(index):
            time.sleep(.05)
            return True
        self.manager.authorized = authorized
        result = self.activate()
        self.assertEqual(result['delivery']['acknowledged_inputs'], 50, result['delivery'])
        self.assertEqual(len(self.sent), 50)

    def test_lost_readiness_does_not_revive(self):
        self.manager.refresh(); self.rows[0]['readiness_proven'] = False
        with self.assertRaises(ValueError): self.manager.refresh()
        self.rows[0]['readiness_proven'] = True
        self.assertEqual(self.manager.refresh()['state'], 'invalidated')

    def test_generation_or_authorization_change_prevents_activation(self):
        self.manager.refresh(); self.allowed = False
        with self.assertRaises(ValueError): self.activate()
        self.allowed = True
        with self.assertRaises(ValueError): self.activate()
        self.assertFalse(self.sent)

    def test_ack_unknown_is_consumed(self):
        original = self.manager.sender
        def send(row, *args, **kwargs):
            original(row, *args, **kwargs)
            if row['index'] == 0: raise TimeoutError('unknown ACK')
        self.manager.sender = send; self.manager.refresh()
        result = self.activate()
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(result['delivery']['acknowledged_inputs'], 49)
        self.assertFalse(self.activate()['new_activation'])
        self.assertEqual(len(self.sent), 50)

    def test_final_authorization_callback_cannot_change_generation_or_boot(self):
        self.manager.refresh()
        inside_send = [False]
        def authorized(index):
            if inside_send[0]:
                self.gen = 'd' * 64
            return True
        def send(row, prompt, input_id, *, write_guard):
            inside_send[0] = True
            with write_guard():
                self.sent.append((row['index'], input_id))
        self.manager.authorized = authorized
        self.manager.sender = send
        self.assertEqual(self.activate()['state'], 'invalidated')
        self.assertEqual(self.sent, [])

    def test_writer_identity_cannot_change_after_ready(self):
        self.manager.refresh(); self.rows[0]['writer_identity'][1] += 1
        with self.assertRaises(ValueError): self.activate()
        self.assertEqual(self.manager.status()['state'], 'invalidated')
        self.assertEqual(self.sent, [])

    def test_consumed_restart_is_observation_only(self):
        self.manager.refresh(); self.activate()
        restarted = StandbyManager(StandbyLedger(self.directory, clock=lambda: self.now),
            generation_current=lambda: self.gen, boot_current=lambda: self.boot,
            authorized=lambda i: True, observe=lambda i: self.rows[i],
            send=lambda *a, **k: self.fail('restart sent input'))
        try:
            self.assertEqual(restarted.refresh()['state'], 'observation_only')
            self.assertFalse(restarted.activate(action_id=self.action, mode='b', prompt='fixed prompt')['new_activation'])
        finally:
            restarted.close()

    def test_late_ack_does_not_revive_invalidation(self):
        gate = threading.Barrier(51); release = threading.Event(); original = self.manager.sender
        def send(*args, **kwargs):
            original(*args, **kwargs)
            gate.wait(timeout=5); release.wait(timeout=5)
        self.manager.sender = send; self.manager.refresh(); results = []
        thread = threading.Thread(target=lambda: results.append(self.activate()))
        thread.start()
        try:
            gate.wait(timeout=5)
            self.manager.invalidate('cancel during ACK wait')
        finally:
            release.set(); thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]['state'], 'invalidated')
        self.assertEqual(results[0]['delivery']['acknowledged_inputs'], 50)
        self.assertEqual(results[0]['ready_originals'], 0)

    def test_invalidation_write_failure_still_clears_ready_and_closes_executor(self):
        self.manager.refresh()
        with patch('ccc_native_standby.write_once', side_effect=OSError('full disk')):
            with self.assertRaises(OSError): self.manager.close()
        self.assertEqual(self.manager.status()['state'], 'closed')
        self.assertEqual(self.manager.status()['ready_originals'], 0)
        with self.assertRaises(RuntimeError): self.manager._executor.submit(lambda: None)

    def test_last_permission_callback_cannot_revive_preparing(self):
        self.rows[0]['readiness_proven'] = False
        def authorized(index):
            if index == 49: self.manager.invalidate('late cancellation')
            return True
        self.manager.authorized = authorized
        with self.assertRaises(ValueError): self.manager.refresh()
        self.assertEqual(self.manager.status()['state'], 'invalidated')


if __name__ == '__main__':
    unittest.main()

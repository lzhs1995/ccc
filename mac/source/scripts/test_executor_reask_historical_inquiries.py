"""Exercise historical reservation authority through real, isolated sweeps."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import executor_ready as ready
import executor_reask as reask
import executor_reask_lifecycle as lifecycle
import executor_reask_protocol as protocol
import executor_reask_scheduler as scheduler
import test_executor_reask_protocol as protocol_fixtures
import test_executor_reask_scheduler as scheduler_fixtures
from test_executor_reask_scheduler import CALLER, SUP, WORKSPACE


class HistoricalInquiryTests(unittest.TestCase):
    # Reuse only fixtures, without inheriting or rerunning their test suites.
    setUp = scheduler_fixtures.SchedulerTests.setUp
    live = scheduler_fixtures.SchedulerTests.live
    make_release = protocol_fixtures.ProtocolTests.make_release
    write_pin = protocol_fixtures.ProtocolTests.write_pin

    def historical_wait(self, prefix='0.4.36-', *, capability=True, until_epoch=None):
        self.auth = self.home / 'authorization.txt'
        self.auth.write_text('Continue this task with file status inquiries during reservations.\n')
        continuation = dict(enabled=True, owner=SUP, overall_task='original task',
                            authorization_file=str(self.auth), until_epoch=until_epoch)
        if capability is not None:
            continuation['reservation_inquiries'] = capability
        # Enrollment is atomic with the original authorization. Only the live
        # identity dependency is stubbed; exercise the real registration path.
        with patch.object(scheduler, '_live_pins', return_value=self.live()):
            scheduler.start(self.live(), task_id='original-task', now=1000.,
                            authenticate_executor=True, continuing=True,
                            continuation=continuation)
        state = reask.load(CALLER)
        name = next(name for name in protocol.LEGACY if name.startswith(prefix))
        self.base = self.home / '.local/share/multi-agent-collaboration/releases'
        source, pins, raw = self.make_release(name)
        self.stack.enter_context(patch.dict(protocol.LEGACY, {
            name: {key: pins[key]['sha256']
                   for key in (*protocol.historical_files(name), protocol.SCHEDULER)}}))
        self.stack.enter_context(patch.dict(protocol.LEGACY_MANIFESTS,
                                            {name: protocol_fixtures.digest(raw)}))
        original = protocol_fixtures.identity(source, pins)
        state['controller'] = copy.deepcopy(original)
        state['continuation']['controller'] = copy.deepcopy(original)
        # Preserve an explicitly false historical field, as well as absence.
        if capability is not None:
            state['continuation']['reservation_inquiries'] = capability
        state['terminal_pending'] = dict(marker='ORIGINAL_UNKNOWN', outcome='UNKNOWN',
                                         controller=copy.deepcopy(original), terminal=True)
        path = scheduler.enrollment_path(CALLER)
        record = ready.read_json(path)
        record.update(binding=scheduler.binding(state), controller=copy.deepcopy(original),
                      scheduler=protocol_fixtures.issuer(source, pins))
        reask.save(state)
        ready.write_json(path, record)
        self.marker = dict(collaboration_id='reserved-child', task_id='child-task',
                           workspace_uuid=WORKSPACE,
                           participants=[dict(role='supervisor', surface_uuid=SUP),
                                         dict(role='executor', surface_uuid=CALLER)])
        lifecycle.task_boundary(self.marker, 'RESERVED', now=1001.)
        return reask.load(CALLER), record, source

    def assert_original(self, original, record):
        after = reask.load(CALLER)
        for key in ('controller', 'continuation', 'episode_id', 'registration',
                    'supervisor', 'task_id', 'terminal_pending', 'paused_tasks'):
            self.assertEqual(after[key], original[key], key)
        current = ready.read_json(scheduler.enrollment_path(CALLER))
        for key in ('controller', 'scheduler', 'binding', 'registration', 'authorization'):
            self.assertEqual(current[key], record[key], key)
        self.assertEqual((after['terminal_sends'], after['native_receipts']), (0, 0))
        self.transport.assert_not_called()

    def check_authorized(self, prefix):
        original, record, _source = self.historical_wait(prefix)
        with patch.object(ready, 'Tail', side_effect=AssertionError('transcript read')), \
                patch.object(reask, 'idle_binding', side_effect=AssertionError('idle binding')), \
                patch('importlib.util.spec_from_file_location', side_effect=AssertionError('old import')), \
                patch('subprocess.run', side_effect=AssertionError('old process')):
            first = scheduler.sweep(now=1002.)
            self.assertEqual(first['file_posts'], 1, first)
            self.assertTrue(first['episodes'][0]['reservation_inquiries_authorized'])
            self.assertEqual(scheduler.sweep(now=1061.)['file_posts'], 0)
            self.assertEqual(scheduler.sweep(now=1062.)['file_posts'], 1)
            self.assertEqual(scheduler.sweep(now=2000.)['file_posts'], 1)
        state = reask.load(CALLER)
        self.assertEqual(state['next_due_epoch'], 2060.)
        self.assertTrue(all(a['inquiry_kind'] == 'RESERVATION_STATUS' for a in state['asks']))
        self.assertTrue(reask.summary(state)['reservation_inquiries_authorized'])
        self.assert_original(original, record)

    def test_reviewed_035_reservation_inquiries_survive_current_scheduler(self):
        self.check_authorized('0.4.35-')

    def test_reviewed_036_reservation_inquiries_survive_current_scheduler(self):
        self.check_authorized('0.4.36-')

    def test_reviewed_037_reservation_inquiries_survive_current_scheduler(self):
        self.check_authorized('0.4.37-')

    def test_absent_authorization_remains_paused(self):
        original, record, _source = self.historical_wait(capability=None)
        result = scheduler.sweep(now=1002.)
        self.assertEqual(result['file_posts'], 0)
        self.assertFalse(result['episodes'][0]['reservation_inquiries_authorized'])
        self.assert_original(original, record)

    def test_false_authorization_remains_paused(self):
        original, record, _source = self.historical_wait(capability=False)
        result = scheduler.sweep(now=1002.)
        self.assertEqual(result['file_posts'], 0)
        self.assertFalse(result['episodes'][0]['reservation_inquiries_authorized'])
        self.assert_original(original, record)

    def test_older_producer_cannot_gain_unsupported_capability(self):
        self.historical_wait('0.4.34-')
        result = scheduler.sweep(now=1002.)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(result['episodes'][0]['status'], 'BINDING_OR_CONTROLLER_MISMATCH')

    def test_editing_state_cannot_upgrade_original_enrollment(self):
        state, _record, _source = self.historical_wait(capability=None)
        state['continuation']['reservation_inquiries'] = True
        reask.save(state)
        result = scheduler.sweep(now=1002.)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(result['episodes'][0]['status'], 'BINDING_OR_CONTROLLER_MISMATCH')

    def test_changed_authorization_stops_historical_inquiries(self):
        self.historical_wait()
        self.auth.write_text('Authorization revoked.\n')
        result = scheduler.sweep(now=1002.)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(reask.load(CALLER)['exit_reason'], 'AUTHORIZATION_CHANGED')

    def test_expired_authorization_stops_historical_inquiries(self):
        self.historical_wait(until_epoch=1003.)
        self.assertEqual(scheduler.sweep(now=1002.)['file_posts'], 1)
        self.assertEqual(scheduler.sweep(now=1003.)['file_posts'], 0)
        self.assertEqual(reask.load(CALLER)['exit_reason'], 'AUTHORIZATION_EXPIRED')

    def test_one_full_historical_verification_per_sweep(self):
        _state, _record, source = self.historical_wait()
        original_load = protocol._load_trusted_release
        for now in (1002., 1062.):
            with patch.object(protocol, '_load_trusted_release', wraps=original_load) as loads:
                self.assertEqual(scheduler.sweep(now=now)['file_posts'], 1)
            self.assertEqual(sum(call.args[0] == source for call in loads.call_args_list), 1)

    def test_changed_manifest_member_refuses_historical_inquiries(self):
        _state, _record, source = self.historical_wait()
        self.write_pin(source / 'README.md', b'changed after historical enrollment')
        result = scheduler.sweep(now=1002.)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(result['episodes'][0]['status'], 'BINDING_OR_CONTROLLER_MISMATCH')

    def test_operator_stop_still_wins_over_historical_authority(self):
        self.historical_wait()
        ready.write_json(reask.stop_file(CALLER), dict(reason='USER_STOP'))
        result = scheduler.sweep(now=1002.)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(reask.load(CALLER)['exit_reason'], 'OPERATOR_STOP')

    def test_reply_is_detected_but_current_cannot_consume_original(self):
        original, record, _source = self.historical_wait()
        self.assertEqual(scheduler.sweep(now=1002.)['file_posts'], 1)
        request = ready.read_json(reask.inbox_path(reask.load(CALLER)))
        reply = dict(request['reply_template'], status='TASK', trigger='Review next increment.')
        ready.write_json(Path(request['reply_mailbox']), reply)
        result = scheduler.sweep(now=1003.)
        self.assertEqual(result['file_posts'], 0)
        state = reask.load(CALLER)
        self.assertEqual(state['state'], reask.ANSWERED)
        self.assertIsNone(state['reply_consumed_epoch'])
        before = reask.state_path(CALLER).read_bytes()
        rejected = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1004.)
        self.assertEqual(rejected['outcome'], 'ORIGINAL_CONTROLLER_REQUIRED')
        self.assertEqual(reask.state_path(CALLER).read_bytes(), before)
        self.assert_original(original, record)


if __name__ == '__main__':
    unittest.main()

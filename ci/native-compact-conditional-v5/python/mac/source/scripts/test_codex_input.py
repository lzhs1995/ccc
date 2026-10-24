"""Direct Codex input: real journals and JSONL receipts, fake external RPC only."""
import copy
from datetime import datetime, timezone, timedelta
import itertools
import json
import os
from pathlib import Path
import time
import unittest
import uuid
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_codex_input as direct
import cmux_native_delivery as native
from native_test_support import NativeFixture, LegacyTerminalFixture


class FakeRPC:
    def __init__(self, fixture, *, active=True, receive=True):
        self.fixture = fixture
        self.active = active
        self.receive = receive
        self.turn = str(uuid.uuid4())
        self.calls = []
        self.before_mutation = None
        self.mutation_error = None
        self.thread_changes = {}
        self.turn_changes = {}
        self.socket_identity = dict(path=str(fixture.home / 'offline.sock'),
            device=1, inode=2, uid=os.geteuid(), mode=0o600, peer_uid=os.geteuid())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    @property
    def mutations(self):
        return [row for row in self.calls if row[0] in ('turn/steer', 'turn/start')]

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'thread/read':
            value = dict(id=self.fixture.session_id, path=str(self.fixture.transcript),
                         canAcceptDirectInput=True,
                         status={'type': 'active' if self.active else 'idle'})
            value.update(self.thread_changes)
            return {'thread': value}
        if method == 'thread/turns/list':
            value = dict(id=self.turn, status='inProgress' if self.active else 'completed')
            value.update(self.turn_changes)
            return {'data': [value]}
        if method not in ('turn/steer', 'turn/start'):
            raise AssertionError('unexpected RPC method: ' + method)
        if self.before_mutation:
            self.before_mutation(method, params)
        if self.receive:
            self.fixture.append_user(params['input'][0]['text'])
        if self.mutation_error:
            raise self.mutation_error
        return {'turnId': self.turn} if method == 'turn/steer' else {'turn': {'id': self.turn}}


class CodexInputTests(unittest.TestCase):
    def setUp(self):
        self.fixture = NativeFixture.attach(self)
        self.rpc = FakeRPC(self.fixture)
        self.fixture.rpc.side_effect = lambda **kwargs: self.rpc
        self.text = 'STATUS: NATIVE_DIRECT_TEST 完整callback正文  spaces  preserved'
        self.marker = 'NATIVE_DIRECT_TEST'
        self.fixture.stack.enter_context(patch.object(bridge, 'read_screen',
            side_effect=AssertionError('new Codex input must not inspect a composer')))
        self.fixture.stack.enter_context(patch.object(direct.time, 'monotonic',
            side_effect=itertools.count().__next__))

    def send(self, **kwargs):
        return bridge.submit_text('target', self.text, marker=self.marker, **kwargs)

    def attempts(self):
        return list((self.fixture.home / '.local/state/multi-agent-collaboration').rglob('attempt-*.json'))

    def assert_no_terminal(self):
        self.fixture.send.assert_not_called()
        self.fixture.key.assert_not_called()
        bridge.read_screen.assert_not_called()

    def test_active_turn_is_steered_with_exact_original_payload_and_fresh_fence(self):
        def at_input(method, params):
            original = json.loads(self.attempts()[0].read_text())
            event = original['events'][-1]
            self.assertEqual(event['phase'], 'NATIVE_INPUT_INTENT')
            self.assertEqual(event['native_input_fence']['transcript']['offset'],
                             self.fixture.transcript.stat().st_size)
            self.assertEqual(event['native_request']['params'], params)
        self.rpc.before_mutation = at_input
        result = self.send()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['transport'], direct.TRANSPORT)
        self.assertEqual(self.rpc.mutations, [('turn/steer', {
            'threadId': self.fixture.session_id, 'expectedTurnId': self.rpc.turn,
            'input': [{'type': 'text', 'text': self.text}]} )])
        self.assertEqual(result['terminal_input_operations'], 0)
        self.assert_no_terminal()

    def test_idle_turn_starts_in_same_thread_without_setting_overrides(self):
        self.rpc.active = False
        self.assertTrue(self.send()['confirmed'])
        self.assertEqual(self.rpc.mutations, [('turn/start', {
            'threadId': self.fixture.session_id,
            'input': [{'type': 'text', 'text': self.text}]} )])
        self.assert_no_terminal()

    def test_rpc_acceptance_without_native_record_requires_readonly_reconcile(self):
        self.rpc.receive = False
        with self.assertRaises(bridge.DispatchUnconfirmed):
            self.send()
        with self.assertRaisesRegex(bridge.TaskPackContractError, 'ATTEMPT_EXISTS'):
            self.send()
        self.fixture.append_user(self.text)
        result = self.send(reconcile_only=True)
        self.assertTrue(result['confirmed'])
        self.assertTrue(result['reconciled_read_only'])
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_no_terminal()

    def test_timeout_after_receipt_is_confirmed_without_retry(self):
        self.rpc.mutation_error = TimeoutError('accepted before timeout')
        result = self.send()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['native_rpc_outcome'], 'RPC_ERROR_OR_UNKNOWN')
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_no_terminal()

    def test_default_window_confirms_eight_second_native_append_with_one_input(self):
        self.rpc.receive = False
        elapsed = [0.0]
        def sleep(seconds):
            elapsed[0] += seconds
            if elapsed[0] == 8:
                self.fixture.append_user(self.text)
        with patch.object(direct.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(
                direct.time, 'sleep', side_effect=sleep):
            result = self.send()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['confirmation_observation']['requested_seconds'], 30)
        self.assertEqual(result['confirmation_observation']['elapsed_seconds'], 8)
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_no_terminal()

    def test_pending_diagnostic_and_delayed_reconcile_preserve_attempt_bytes(self):
        self.rpc.receive = False
        self.rpc.mutation_error = TimeoutError('original RPC result unknown')
        with self.assertRaises(bridge.DispatchUnconfirmed) as caught:
            self.send(wait_seconds=0)
        details = caught.exception.details
        self.assertEqual(details['native_rpc_outcome'], 'RPC_ERROR_OR_UNKNOWN')
        self.assertEqual(details['input_operations'], 1)
        self.assertEqual(details['confirmation_observation']['requested_seconds'], 0)
        self.assertFalse(details['business_accepted'])
        attempt = Path(details['attempt'])
        original = attempt.read_bytes()
        with patch.object(direct.time, 'sleep', side_effect=lambda _: self.fixture.append_user(self.text)):
            result = self.send(reconcile_only=True, wait_seconds=30)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['input_operations'], 0)
        self.assertEqual(result['native_rpc_outcome'], 'RPC_ERROR_OR_UNKNOWN')
        self.assertEqual(attempt.read_bytes(), original)
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_no_terminal()

    def test_invalid_windows_leave_all_dispatch_slots_and_rpc_untouched(self):
        path, pack = self.make_pack()
        for value in (-1, 61, True, '30', float('nan'), float('inf')):
            for route in ('message', 'task', 'callback'):
                with self.subTest(value=value, route=route):
                    with self.assertRaisesRegex(ValueError, 'CONFIRMATION_WINDOW_INVALID'):
                        if route == 'message':
                            self.send(wait_seconds=value)
                        elif route == 'task':
                            bridge.submit_task_pack('target', self.text, path,
                                                    marker=self.marker, wait_seconds=value)
                        else:
                            bridge.submit_completion_callback(path, wait_seconds=value)
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.rpc.calls, [])
        self.assert_no_terminal()

    def test_unavailable_native_goal_can_receive_terminal_message_without_resuming_goal(self):
        self.rpc.thread_changes['status'] = {'type': 'notLoaded'}
        self.rpc.turn_changes.update(status='interrupted', error={'code': 429, 'message': 'busy'})
        self.fixture.key.side_effect = self.fixture.receipt_on_key(self.text)
        result = self.send()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['transport'], 'terminal-direct-marker-v1')
        self.assertEqual(self.rpc.mutations, [])
        self.fixture.send.assert_called_once_with('target', self.text)
        self.fixture.key.assert_called_once_with('target', 'enter')
        bridge.read_screen.assert_not_called()

    def test_probe_reads_only_metadata_and_creates_no_attempt(self):
        result = direct.probe_target(bridge, 'target')
        self.assertEqual(result['input_operations'], 0)
        self.assertFalse(result['journal_created'])
        self.assertFalse(result['native_receipt_established'])
        self.assertEqual(result['native_metadata']['available_input_method'], 'turn/steer')
        self.assertEqual(self.attempts(), [])
        self.assertEqual([r[0] for r in self.rpc.calls], ['thread/read', 'thread/turns/list'])
        self.assert_no_terminal()

    def test_metadata_connection_failure_is_zero_input(self):
        self.fixture.rpc.side_effect = ConnectionError('daemon unavailable')
        with self.assertRaises(ConnectionError) as caught:
            self.send()
        self.assertEqual(caught.exception.details['input_operations'], 0)
        self.assertEqual(caught.exception.details['native_metadata']['metadata_rpc_status'], 'UNAVAILABLE')
        original = json.loads(self.attempts()[0].read_text())
        self.assertEqual(original['phase'], 'NO_INPUT')
        self.assertEqual(original['events'], [])
        self.assert_no_terminal()

    def test_timeout_without_receipt_never_uses_terminal_fallback(self):
        self.rpc.receive = False
        self.rpc.mutation_error = TimeoutError('ambiguous')
        with self.assertRaises(bridge.DispatchUnconfirmed):
            self.send()
        with self.assertRaises(bridge.TaskPackContractError):
            self.send()
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_no_terminal()

    def test_unavailable_native_turn_falls_back_before_any_rpc_input(self):
        self.rpc.turn_changes['status'] = 'completed'
        self.fixture.key.side_effect = self.fixture.receipt_on_key(self.text)
        self.assertTrue(self.send()['confirmed'])
        self.assertEqual(self.rpc.mutations, [])
        self.fixture.send.assert_called_once_with('target', self.text)
        bridge.read_screen.assert_not_called()

    def test_wrong_thread_path_or_nonaccepting_thread_refuses_input(self):
        for delta in ({'path': '/different/session.jsonl'}, {'id': str(uuid.uuid4())}):
            with self.subTest(delta=delta):
                self.rpc.thread_changes = delta
                with self.assertRaisesRegex(native.NativeDeliveryError, 'NOT_ACCEPTING'):
                    direct.prepare(self.rpc, self.fixture_bind(), self.text)
        self.assertEqual(self.rpc.mutations, [])

    def fixture_bind(self):
        return native.bind_target(bridge, 'target', self.text)

    def test_binding_drift_after_metadata_has_no_input(self):
        original = self.rpc.request
        def request(method, params):
            result = original(method, params)
            if method == 'thread/turns/list':
                self.fixture.process_state['birth'] = 'a different process'
            return result
        self.rpc.request = request
        with self.assertRaises(native.NativeDeliveryError):
            self.send()
        self.assertEqual(self.rpc.mutations, [])
        self.assert_no_terminal()

    def test_failed_result_journal_still_observes_receipt_and_reports_write_error(self):
        binding = self.fixture_bind()
        observed = []
        phases = []
        def emit(phase, **kwargs):
            phases.append(phase)
            if phase == 'NATIVE_INPUT_RESULT':
                raise OSError('disk write unavailable')
        def observe():
            observed.append(True)
            return {'confirmed': True}
        result = direct.send_once(binding, self.text, recheck=lambda: None,
                                  emit=emit, observe=observe, wait_seconds=0)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['native_rpc_outcome'], 'RPC_ACCEPTED')
        self.assertEqual(result['native_result_journal_error']['error_type'], 'OSError')
        self.assertEqual(phases, ['NATIVE_INPUT_INTENT', 'NATIVE_INPUT_RESULT'])
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assertEqual(observed, [True])

    def test_failed_intent_write_has_zero_mutation(self):
        with self.assertRaises(OSError):
            direct.send_once(self.fixture_bind(), self.text, recheck=lambda: None,
                emit=lambda *a, **k: (_ for _ in ()).throw(OSError('no durable intent')),
                observe=lambda: self.fail('no mutation to observe'))
        self.assertEqual(self.rpc.mutations, [])

    def test_changed_request_and_mixed_transports_cannot_prove_reception(self):
        result = self.send()
        attempt = json.loads(Path(result['attempt']).read_text())
        altered = copy.deepcopy(attempt)
        altered['events'][0]['native_request']['params']['input'][0]['text'] += 'x'
        with self.assertRaises(native.NativeDeliveryError):
            native._original_intent(altered, altered['native_binding'], self.text)
        altered = copy.deepcopy(attempt)
        altered['events'].append({'phase': 'ENTER_INTENT'})
        with self.assertRaises(native.NativeDeliveryError):
            native._original_intent(altered, altered['native_binding'], self.text)

    def test_native_request_rejects_overrides_changed_socket_and_invalid_turn(self):
        binding = self.fixture_bind()
        request = direct.prepare(self.rpc, binding, self.text)
        for category in ('override', 'peer', 'mode', 'turn'):
            bad = copy.deepcopy(request)
            if category == 'override':
                bad['params']['cwd'] = '/other'
            elif category == 'peer':
                bad['socket']['peer_uid'] += 1
            elif category == 'mode':
                bad['socket']['mode'] = 0o666
            else:
                bad['params']['expectedTurnId'] = 'invalid'
            with self.subTest(category=category), self.assertRaises(native.NativeDeliveryError):
                direct.validate_request(bad, binding, self.text)

    def make_pack(self):
        report = self.fixture.home / 'executor-report.md'
        report.write_text('Read-only verification result.\n')
        gate = self.fixture.home / 'identity-gate.json'
        gate.write_text(json.dumps(dict(status='PASS', task_id='native-callback-test',
            supervisor_surface_uuid='target', workspace_uuid='workspace')))
        pack = dict(task_id='native-callback-test', completion_nonce='fresh-native-nonce',
            report=str(report), callback_target='target', executor_uuid='caller',
            completion_receipt=str(self.fixture.home / 'callback-receipt.json'),
            identity_gate=str(gate), finalized_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
        pack['completion_callback'] = f"DONE|{pack['task_id']}|{pack['completion_nonce']}|REPORT={report}"
        path = self.fixture.home / 'task-pack.json'
        path.write_text(json.dumps(pack))
        self.fixture.stack.enter_context(patch.object(bridge, 'validate_task_pack_contract', return_value=pack))
        self.fixture.stack.enter_context(patch('availability_contract.require_action'))
        return path, pack

    def test_real_callback_journal_and_independent_receiver_validation(self):
        import callback_native_evidence
        path, pack = self.make_pack()
        result = bridge.submit_completion_callback(path)
        self.assertTrue(result['confirmed'])
        receipt = json.loads(Path(pack['completion_receipt']).read_text())
        self.assertEqual(receipt['transport'], direct.TRANSPORT)
        original = json.loads(Path(receipt['attempt']).read_text())
        self.assertEqual([e['phase'] for e in original['events']],
                         ['NATIVE_INPUT_INTENT', 'NATIVE_INPUT_RESULT'])
        evidence = callback_native_evidence.validate(path, receipt['attempt'],
            self.fixture.transcript, 2, self.fixture.session_id)
        self.assertEqual(evidence['status'], 'EXACT_NATIVE_RECEPTION')
        with self.assertRaisesRegex(bridge.TaskPackContractError, 'RECEIPT_EXISTS'):
            bridge.submit_completion_callback(path)
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_no_terminal()

    def test_callback_receipt_preserves_abort_diagnostic_without_replay(self):
        path, pack = self.make_pack()
        self.rpc.receive = False
        self.rpc.before_mutation = lambda *_: self.fixture.append_record(dict(
            timestamp=datetime.now(timezone.utc).isoformat(), type='event_msg',
            payload=dict(type='turn_aborted', turn_id=self.rpc.turn, reason='interrupted')))
        with self.assertRaises(bridge.DispatchUnconfirmed) as caught:
            bridge.submit_completion_callback(path, wait_seconds=0)
        self.assertEqual(caught.exception.details['native_turn_diagnostic']['status'],
                         'ORIGINAL_TURN_ABORTED')
        attempt = Path(caught.exception.details['attempt'])
        original = attempt.read_bytes()
        self.fixture.append_user(pack['completion_callback'])
        result = bridge.submit_completion_callback(path, reconcile_only=True)
        self.assertTrue(result['confirmed'])
        receipt = json.loads(Path(pack['completion_receipt']).read_text())
        self.assertEqual(receipt['native_turn_diagnostic']['status'], 'ORIGINAL_TURN_ABORTED')
        self.assertFalse(receipt['native_turn_diagnostic']['replay_authorized'])
        self.assertEqual(receipt['input_operations'], 0)
        self.assertEqual(attempt.read_bytes(), original)
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_no_terminal()

    def test_task_dispatch_uses_same_direct_route(self):
        path, pack = self.make_pack()
        pack['executor_uuid'] = 'target'
        path.write_text(json.dumps(pack))
        result = bridge.submit_task_pack('target', self.text, path, marker=self.marker)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['transport'], direct.TRANSPORT)
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_no_terminal()


class HistoricalNoReplayTests(unittest.TestCase):
    def test_old_terminal_attempt_only_reconciles_and_never_uses_native_fallback(self):
        text, marker = 'STATUS: HISTORICAL_QUEUED retained', 'HISTORICAL_QUEUED'
        with LegacyTerminalFixture() as fixture:
            historical = fixture
            idle = '› Ask Codex to do anything\nGPT-6 high'
            with patch.object(bridge, 'read_screen', side_effect=fixture.ready_screens(
                    idle, fixture.draft(text), idle)):
                with self.assertRaises(bridge.DispatchUnconfirmed):
                    bridge.submit_text('target', text, marker=marker)
            # Restore the production dispatcher while retaining original files.
            original_dispatch = bridge._submit_text_once
            with patch.object(bridge, '_submit_text_once', wraps=original_dispatch) as dispatch:
                with self.assertRaisesRegex(bridge.TaskPackContractError, 'ATTEMPT_EXISTS'):
                    bridge.submit_text('target', text, marker=marker)
                fixture.append_user(text)
                self.assertTrue(bridge.submit_text('target', text, marker=marker,
                                                  reconcile_only=True)['confirmed'])
                dispatch.assert_not_called()
            fixture.rpc.assert_not_called()
            self.assertEqual(historical.send.call_count, 1)
            self.assertEqual(historical.key.call_count, 1)


if __name__ == '__main__':
    unittest.main()

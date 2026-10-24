"""Current queued input and historical partial-dual ACK recovery regressions."""
import copy
from datetime import datetime, timezone
import io
import contextlib
import json
import time
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import cmux_bridge as bridge
import cmux_native_delivery as native
import continue_handshake as continuation
import handshake_panel as panel
import mac_harness as harness
import reconcile_handshake_ack as recovery
from native_test_support import NativeFixture
from test_handshake_reconciliation import HandshakeReconciliationTests as Helpers, digest


class CurrentMessageTests(unittest.TestCase):
    def test_preflight_cannot_rewrite_existing_gate_before_refusing_replay(self):
        for started in ('handshake-receipt.json', 'task-pack.json'):
            with self.subTest(started=started), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                root = Path(tmp)
                (root / started).write_text('original')
                (root / 'identity-gate.json').write_text('original gate')
                stack.enter_context(mock.patch.object(harness, '_artifact_root', return_value=root))
                calls = [stack.enter_context(mock.patch.object(harness, name)) for name in (
                    'cmd_setup_check', 'cmd_helper_parity', 'cmd_identity_gate', 'cmd_name_surfaces',
                    'cmd_handshake', 'cmd_validate')]
                with self.assertRaises(SystemExit):
                    harness.cmd_preflight(SimpleNamespace())
                for call in calls:
                    call.assert_not_called()
                self.assertEqual((root / 'identity-gate.json').read_text(), 'original gate')

    def test_real_direct_queue_exception_has_wait_state_and_never_claims_receipt(self):
        fixture = NativeFixture.attach(self, provider='claude')
        text = 'STATUS: queued-handshake-marker'
        fixture.key.side_effect = lambda *_: fixture.append_queued(text)
        with self.assertRaises(bridge.DispatchUnconfirmed) as raised:
            bridge.submit_text('target', text, marker='queued-handshake-marker', wait_seconds=0)
        error = raised.exception
        self.assertEqual(error.state, bridge.DELIVERY_QUEUED_AT_RECEIVER)
        self.assertFalse(error.details['confirmed'])
        self.assertEqual(error.details['input_operations'], 2)
        fixture.send.assert_called_once()
        fixture.key.assert_called_once()
        with self.assertRaises(bridge.DispatchUnconfirmed) as observed:
            bridge.submit_text('target', text, marker='queued-handshake-marker',
                               wait_seconds=0, reconcile_only=True)
        self.assertEqual(observed.exception.state, bridge.DELIVERY_QUEUED_AT_RECEIVER)
        fixture.send.assert_called_once()
        fixture.key.assert_called_once()


class PartialHandshakeTests(unittest.TestCase):
    patch = Helpers.patch
    setUp = Helpers.setUp
    _prepare_attempt = Helpers._prepare_attempt
    save = Helpers.save
    append_ack = Helpers.append_ack
    run_reconcile = Helpers.run_reconcile
    files = Helpers.files

    def partial(self):
        self.gate['executors'][0]['surface_ref'] = 'surface:21'
        self.rows[0]['executor'] = 'surface:21'
        self.gate['executors'].append(dict(surface_ref='surface:22', surface_uuid='target2',
            provider='claude', workspace_ref='workspace:1', pane_ref='pane:3'))
        self.gate['native_participants'].append(dict(role='executor', provider='claude',
            surface_uuid='target2', native_session_id='second-original-session'))
        self.binding = native.marker_binding(self.binding, self.text, self.nonce)
        self.attempt['native_binding'] = self.binding
        self.attempt['events'][0]['native_paste_fence'] = native.capture_paste_fence(self.binding)
        self.attempt['events'][0]['at_epoch'] = time.time()
        self.attempt['events'] += [dict(phase=p, at_epoch=self.attempt['events'][0]['at_epoch'])
                                  for p in ('PASTED', 'ENTER_INTENT', 'ENTER_SENT')]
        details = dict(confirmed=False, state='NATIVE_QUEUED', transport='terminal-direct-marker-v1',
                       submission_key='enter', extra_enter=0, input_operations=2)
        self.attempt.update(phase='ENTER_SENT', error='MESSAGE_NOT_CONFIRMED', error_details=details)
        self.attempt['controller_provenance'] = dict(source=self.args.controller)
        self.rows[0].update(lifecycle='REJECTED', submission_state='SUPERVISOR_DID_NOT_SUBMIT',
            error='MESSAGE_NOT_CONFIRMED', dispatch_error='MESSAGE_NOT_CONFIRMED',
            dispatch_error_details=details, dispatch_payload=self.text)
        self.save()
        self.rows[0]['dispatch_attempt'] = dict(path=str(self.attempt_path), sha256=self.args.attempt_sha256)
        self.save()
        # The actual 0.4.54 writer lacked the panel-size fields. Preserve that input shape.
        self.receipt_path.write_bytes(recovery.json_bytes(panel.aggregate(self.rows)))
        self.args.receipt_sha256 = digest(self.receipt_path.read_bytes())
        self.fixture.append_queued(self.text)
        self.append_ack()

    def test_reconcile_ref_prefix_keeps_whole_panel_unready_and_original_bytes(self):
        self.partial()
        original = self.attempt_path.read_bytes()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILED', result)
        self.assertEqual(result['validation']['status'], 'FAIL')
        receipt = json.loads(self.receipt_path.read_bytes())
        self.assertEqual(receipt['status'], 'AWAITING_EXECUTOR_ACK')
        self.assertFalse(receipt['executor_ack'])
        self.assertFalse(receipt['all_executors_acked'])
        self.assertEqual(receipt['unstarted_executors'], ['surface:22'])
        self.assertTrue(receipt['executors'][0]['executor_ack'])
        self.assertEqual(receipt['executors'][0]['ack_nonce'], self.nonce)
        self.assertEqual(self.attempt_path.read_bytes(), original)
        self.assertFalse(receipt['executors'][0]['late_ack_reconciliation']['native_notice']['confirmed'])

    def test_direct_rejection_cannot_reconcile_without_original_enter(self):
        self.partial()
        self.attempt['events'].pop()
        self.save()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILIATION_UNAVAILABLE', result)
        self.assertIn('DIRECT_INPUT_HISTORY_REQUIRED', result['error'])

    def test_partial_row_cannot_claim_a_different_member_or_skip_first(self):
        self.partial()
        self.rows[0]['executor'] = 'surface:22'
        self.save()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILIATION_UNAVAILABLE')
        self.assertIn('PANEL_ORDER_OR_BINDING_CHANGED', result['error'])

    def continued_args(self):
        self.partial()
        self.assertEqual(self.run_reconcile(apply=True)['status'], 'ACK_RECONCILED')
        # Earlier reconciliation helper carries generic bridge fixture bytes.
        # A continuation needs the actual original peer postconditions.
        (self.root / 'bridge-test-evidence.json').write_text(json.dumps(dict(
            task_id=self.task, executors=[dict(executor='surface:21', clear_confirmed=True),
                                         dict(executor='surface:22', clear_confirmed=True)])))
        reservation = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        marker_path = harness._ACTIVE_DIR / self.gate['workspace_uuid'] / (reservation + '.json')
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker = dict(marker_version=2, collaboration_id=reservation, task_id=self.task,
            artifact_root=str(self.root), workspace_uuid='workspace', workspace_id='workspace',
            participants=[dict(p, ordinal=i) for i, p in enumerate(self.gate['native_participants'])],
            armed_at=datetime.now(timezone.utc).isoformat(), ttl_seconds=3600)
        marker_path.write_bytes(recovery.json_bytes(marker))
        return SimpleNamespace(controller=self.args.controller, artifact_root=str(self.root),
            identity_sha256=digest((self.root / 'identity-gate.json').read_bytes()),
            receipt_sha256=digest(self.receipt_path.read_bytes()), reservation_id=reservation,
            apply=False, original_harness_exited=True, lines=200, handshake_timeout=600)

    def test_continuation_preview_does_not_invoke_sender(self):
        args = self.continued_args()
        before = self.files()
        with mock.patch.object(harness, '_handshake_one') as send:
            result = continuation.continue_handshake(args, self.modules)
        self.assertEqual(result['status'], 'READY_ORIGINAL_HANDSHAKE_CONTINUATION', result)
        send.assert_not_called()
        self.assertEqual(before, self.files())

    def test_continuation_only_second_peer_and_durable_slot_before_input(self):
        args = self.continued_args()
        args.apply = True
        original = self.attempt_path.read_bytes()
        before_row = json.loads(self.receipt_path.read_bytes())['executors'][0]
        next_binding = copy.deepcopy(self.binding)
        next_binding['identity']['target_surface_uuid'] = 'target2'
        next_binding['session_id'] = 'second-original-session'
        actual_require = native.require_bound
        def require(bridge, target, binding, text, **kw):
            if target != 'target2':
                return actual_require(bridge, target, binding, text, **kw)
        def send(_args, root, gate, peer, rows):
            self.assertEqual(peer['surface_uuid'], 'target2')
            self.assertTrue((root / 'handshake-continuation-2/INTENT.json').is_file())
            self.assertEqual(rows, [before_row])
            second = dict(before_row, executor='surface:22', ordinal=2, ack_nonce='87654321',
                          executor_ack=False, status='FAIL', lifecycle='EXPIRED')
            rows.append(second)
            return False
        with mock.patch.object(native, 'bind_target', return_value=next_binding), \
                mock.patch.object(native, 'require_bound', side_effect=require), \
                mock.patch.object(harness, '_handshake_one', side_effect=send) as sender:
            result = continuation.continue_handshake(args, self.modules)
            self.assertEqual(result['status'], 'HANDSHAKE_CONTINUED_TERMINAL', result)
            self.assertEqual(result['executor_lifecycle'], 'EXPIRED')
            sender.assert_called_once()
            again = continuation.continue_handshake(args, self.modules)
            self.assertEqual(again['status'], 'HANDSHAKE_CONTINUATION_UNAVAILABLE')
            sender.assert_called_once()
        self.assertEqual(original, self.attempt_path.read_bytes())
        self.assertEqual(json.loads(self.receipt_path.read_bytes())['executors'][0], before_row)

    def test_real_original_bridge_check_refuses_without_consuming_slot(self):
        args = self.continued_args()
        evidence_path = self.root / 'bridge-test-evidence.json'
        cases = [[], [dict(executor='surface:22', clear_confirmed=False)],
                 [dict(executor='surface:22', clear_confirmed=True)] * 2]
        for rows in cases:
            evidence_path.write_text(json.dumps(dict(task_id=self.task, executors=rows)))
            for apply in (False, True):
                with self.subTest(rows=rows, apply=apply):
                    args.apply = apply
                    before = self.files()
                    result = continuation.continue_handshake(args, self.modules)
                    self.assertEqual(result['status'], 'HANDSHAKE_CONTINUATION_UNAVAILABLE', result)
                    self.assertEqual(result['handshake_invocations'], 0)
                    self.assertEqual(result['input_operations'], 0)
                    self.assertFalse((self.root / 'handshake-continuation-2').exists())
                    self.assertEqual(self.files(), before)
        evidence_path.write_text(json.dumps(dict(task_id=self.task,
            executors=[dict(executor='surface:22', clear_confirmed=True)])))
        args.apply = False
        self.assertEqual(continuation.continue_handshake(args, self.modules)['status'],
                         'READY_ORIGINAL_HANDSHAKE_CONTINUATION')

    def test_changed_prefix_is_not_published_by_continuation(self):
        args = self.continued_args()
        args.apply = True
        before = self.receipt_path.read_bytes()
        next_binding = copy.deepcopy(self.binding)
        next_binding['identity']['target_surface_uuid'] = 'target2'
        next_binding['session_id'] = 'second-original-session'
        original_require = native.require_bound
        def require(bridge, target, binding, text, **kw):
            if target != 'target2':
                return original_require(bridge, target, binding, text, **kw)
        def changed(_args, root, gate, peer, rows):
            rows[0]['ack_nonce'] = 'changed'
            rows.append(dict(rows[0], executor='surface:22', ordinal=2))
            return True
        with mock.patch.object(native, 'bind_target', return_value=next_binding), \
                mock.patch.object(native, 'require_bound', side_effect=require), \
                mock.patch.object(harness, '_handshake_one', side_effect=changed), \
                mock.patch.object(harness, '_write_handshake_receipt') as write:
            result = continuation.continue_handshake(args, self.modules)
        self.assertIn('ORIGINAL_PREFIX_CHANGED', result['error'])
        write.assert_not_called()
        self.assertEqual(self.receipt_path.read_bytes(), before)

    def test_wrong_supervisor_cannot_continue_or_create_slot(self):
        args = self.continued_args()
        args.apply = True
        self.caller.return_value[-1]['foreground']['thread_id'] = 'another-thread'
        before = self.files()
        with mock.patch.object(harness, '_handshake_one') as send:
            result = continuation.continue_handshake(args, self.modules)
        self.assertEqual(result['status'], 'HANDSHAKE_CONTINUATION_UNAVAILABLE', result)
        send.assert_not_called()
        self.assertEqual(before, self.files())


del Helpers

if __name__ == '__main__':
    unittest.main()

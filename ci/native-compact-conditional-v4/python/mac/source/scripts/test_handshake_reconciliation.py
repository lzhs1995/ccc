"""Late ACK recovery against real journals and the original writer/validator.

Only process, terminal and caller observations are replaced. Run this file with
--original-controller /immutable/release/source to exercise that whole release.
"""
import contextlib
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import reconcile_handshake_ack as reconcile

if __name__ == '__main__' and '--original-controller' in sys.argv:
    _index = sys.argv.index('--original-controller')
    reconcile.original_modules(sys.argv[_index + 1])
    del sys.argv[_index:_index + 2]

import cmux_bridge as bridge
import cmux_evidence_io as evidence
import cmux_message_journal as journal
import cmux_native_delivery as native
import cmux_workspace_guard as guard
import mac_harness as harness
from native_test_support import NativeFixture


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class HandshakeReconciliationTests(unittest.TestCase):
    def patch(self, obj, name, **kwargs):
        patcher = mock.patch.object(obj, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        self.fixture = NativeFixture.attach(self, provider='claude')
        self.fixture.pin.side_effect = lambda *args, **kwargs: copy.deepcopy(self.fixture.identity)
        self.home = self.fixture.home
        self.root = self.home / 'original-task'
        self.root.mkdir()
        self.task = 'late-ack-original-task'
        self.nonce = 'b90a4192'
        self.target = self.fixture.identity['target_surface_uuid']
        self.thread = 'original-supervisor-thread'
        self.expected = f'PREFLIGHT_ACK|{self.task}|claude:identity|READY|INLINE|{self.nonce}'
        self.screen = self.patch(bridge, 'read_screen', return_value='⏺ ' + self.expected)
        self.patch(harness, '_ACTIVE_DIR', new=self.home / 'active')
        self.patch(harness, '_workspace_key', return_value='workspace')
        self.caller = self.patch(guard, 'caller_snapshot', return_value=(
            dict(caller=dict(surface_id='caller', workspace_id='workspace')),
            None, dict(CMUX_WORKSPACE_ID='workspace'),
            dict(foreground=dict(thread_id=self.thread))))
        self.modules = (bridge, native, evidence, journal, harness)
        self.text = 'STATUS: Original handshake DELIVERY_NONCE=' + self.nonce
        self.body_reference = None
        self.body_pin = None
        self._prepare_attempt()
        self.gate = dict(status='PASS', task_id=self.task, supervisor='caller',
            supervisor_provider='codex', supervisor_surface_uuid='caller',
            workspace_uuid='workspace', workspace_ref='workspace:1',
            supervisor_pane_ref='pane:1', side_panel=True,
            executors=[dict(surface_ref=self.target, surface_uuid=self.target,
                provider='claude', workspace_ref='workspace:1', pane_ref='pane:2')],
            native_participants=[dict(role='supervisor', provider='codex',
                surface_uuid='caller', native_session_id=self.thread),
                dict(role='executor', provider='claude', surface_uuid=self.target,
                     native_session_id=self.fixture.session_id)])
        self.rows = [dict(task_id=self.task, supervisor='caller', executor=self.target,
            ordinal=1, workspace_ref='workspace:1', executor_provider='claude',
            status='FAIL', lifecycle='EXPIRED', executor_ack=False,
            ack_nonce=self.nonce, ack_line_expected=self.expected,
            terminal_error_at=datetime.now(timezone.utc).isoformat(),
            error='original timeout', bridge_clear_confirmed=True,
            submission_state='NATIVE_QUEUED', dispatch_error='original notice unconfirmed',
            dispatch_payload_sha256=digest(self.text.encode()), dispatch_body_reference=None)]
        self.receipt_path = self.root / 'handshake-receipt.json'
        self.args = SimpleNamespace(controller=str(Path(harness.__file__).resolve().parent.parent),
            receipt=str(self.receipt_path), attempt=str(self.attempt_path),
            text_file=str(self.text_path), timeout=1, lines=200, apply=False,
            original_harness_exited=True)
        self.save()

    def _prepare_attempt(self):
        self.binding = native.bind_target(bridge, self.target, self.text)
        self.fence = native._capture_fence(self.binding, 'native-paste-fence-v1')
        screen = NativeFixture.draft(self.text, 'claude')
        self.attempt = dict(binding=dict(identity=self.fixture.identity,
            marker=self.nonce, payload_sha256=digest(self.text.encode())),
            native_binding=self.binding, events=[dict(phase='PASTE_INTENT',
                at_epoch=time.time(), screen=screen, screen_sha256=digest(screen.encode())[:16],
                native_paste_fence=self.fence)])
        if self.body_reference is not None:
            self.attempt.update(body_reference=self.body_reference, body_pin=self.body_pin)
        key = digest(json.dumps(['caller', self.nonce], sort_keys=True).encode())
        directory = self.home / '.local/state/multi-agent-collaboration/message-dispatch-v1' / key
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'delivery.lock').touch(exist_ok=True)
        self.attempt_path = directory / 'attempt-0001.json'
        self.text_path = self.root / 'original-wire.txt'

    def save(self):
        self.attempt_path.write_bytes(reconcile.json_bytes(self.attempt))
        self.text_path.write_bytes(self.text.encode())
        (self.root / 'identity-gate.json').write_bytes(reconcile.json_bytes(self.gate))
        (self.root / 'naming-proof.json').write_bytes(reconcile.json_bytes(dict(status='PASS')))
        (self.root / 'bridge-test-evidence.json').write_bytes(reconcile.json_bytes(dict(observed_in_screen=True)))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            harness._write_handshake_receipt(self.root, self.rows)
            try:
                harness.cmd_validate(SimpleNamespace(artifact_root=str(self.root), task_id=self.task, json=False))
            except SystemExit as exc:
                self.assertEqual(exc.code, 1)
        self.args.receipt_sha256 = digest(self.receipt_path.read_bytes())
        self.args.attempt_sha256 = digest(self.attempt_path.read_bytes())

    def append_ack(self, **changes):
        record = dict(type='assistant', sessionId=self.fixture.session_id,
            uuid='native-assistant-record', timestamp=datetime.now(timezone.utc).isoformat(),
            message=dict(role='assistant', content=[dict(type='text', text=self.expected)]))
        record.update(changes)
        self.fixture.append_record(record)

    def files(self):
        return {str(path): evidence.snapshot(path) for path in self.home.rglob('*') if path.is_file()}

    def run_reconcile(self, apply=False):
        self.args.apply = apply
        result = reconcile.reconcile(self.args, self.modules)
        self.fixture.send.assert_not_called()
        self.fixture.key.assert_not_called()
        self.fixture.rpc.assert_not_called()
        self.assertEqual(result['input_operations'], 0)
        self.assertEqual(result['journal_writes'], 0)
        return result

    def assert_unavailable(self, error=None):
        before = self.files()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILIATION_UNAVAILABLE', result)
        if error:
            self.assertIn(error, result['error'])
        self.assertEqual(result['receipt_write_attempts'], 0)
        self.assertEqual(before, self.files())
        return result

    def test_preview_real_queued_ack_is_read_only(self):
        self.fixture.append_queued(self.text)
        self.append_ack()
        before = self.files()
        result = self.run_reconcile()
        self.assertEqual(result['status'], 'READY_ZERO_INPUT_ACK_RECONCILIATION', result)
        self.assertEqual(result['observation']['native_message']['state'], 'NATIVE_QUEUED')
        self.assertFalse(result['observation']['native_message']['confirmed'])
        self.assertEqual(before, self.files())
        self.assertFalse((self.root / 'handshake-reconciliations').exists())

    def prepare_no_probe(self, **changes):
        self.rows[0].update(bridge_probe_policy='optional-probe-v1',
            bridge_probe_status='NOT_RUN', bridge_clear_confirmed=None,
            bridge_clear_recovery=None)
        self.rows[0].update(changes)
        self.save()
        (self.root / 'bridge-test-evidence.json').unlink()
        self.fixture.append_queued(self.text)
        self.append_ack()

    def test_original_no_probe_ack_preview_and_apply_keep_clear_unclaimed(self):
        if not callable(getattr(harness, 'handshake_without_probe', None)):
            self.skipTest('original controller predates optional probes')
        self.prepare_no_probe()
        original_attempt = self.attempt_path.read_bytes()
        before = self.files()
        result = self.run_reconcile()
        self.assertEqual(result['status'], 'READY_ZERO_INPUT_ACK_RECONCILIATION', result)
        self.assertEqual(before, self.files())
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILED', result)
        self.assertEqual(result['validation']['status'], 'PASS')
        receipt = json.loads(self.receipt_path.read_bytes())
        self.assertEqual(receipt['bridge_probe_status'], 'NOT_RUN')
        self.assertIsNone(receipt['bridge_clear_confirmed'])
        self.assertIsNone(receipt['bridge_clear_recovery'])
        validation = json.loads((self.root / 'validation.json').read_bytes())
        self.assertEqual(validation['checks']['bridge_test']['status'], 'NOT_RUN')
        self.assertFalse((self.root / 'bridge-test-evidence.json').exists())
        self.assertEqual(self.attempt_path.read_bytes(), original_attempt)

    def test_no_probe_label_cannot_bypass_existing_probe_evidence(self):
        self.prepare_no_probe()
        (self.root / 'bridge-test-evidence.json').write_text(json.dumps({
            'task_id': self.task, 'executor': self.target, 'clear_confirmed': False}))
        self.assert_unavailable('ACK_ORIGINAL_BRIDGE_CLEAR_REQUIRED')

    def test_malformed_no_probe_state_cannot_waive_original_clear(self):
        variants = [dict(bridge_probe_policy=None), dict(bridge_probe_status='CLEARED'),
                    dict(bridge_clear_confirmed=False), dict(bridge_clear_recovery={})]
        for changes in variants:
            with self.subTest(changes=changes):
                self.prepare_no_probe(**changes)
                self.assert_unavailable('ACK_ORIGINAL_BRIDGE_CLEAR_REQUIRED')

    def test_old_controller_does_not_gain_no_probe_capability_from_receipt(self):
        self.prepare_no_probe()
        with mock.patch.object(harness, 'handshake_without_probe', None, create=True):
            self.assert_unavailable('ACK_ORIGINAL_BRIDGE_CLEAR_REQUIRED')

    def test_apply_real_ack_archives_and_validates_without_native_receipt(self):
        self.fixture.append_queued(self.text)
        self.append_ack()
        original = self.receipt_path.read_bytes()
        attempt = self.attempt_path.read_bytes()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILED', result)
        self.assertEqual(result['validation']['status'], 'PASS')
        receipt = json.loads(self.receipt_path.read_bytes())
        self.assertTrue(receipt['executor_ack'])
        self.assertEqual(receipt['submission_state'], 'NATIVE_QUEUED')
        self.assertEqual(receipt['dispatch_error'], 'original notice unconfirmed')
        self.assertFalse(receipt['late_ack_reconciliation']['native_notice']['confirmed'])
        self.assertEqual(receipt['late_ack_reconciliation']['business_acceptance'], 'NOT_EVALUATED')
        self.assertEqual((Path(result['archive']) / 'handshake-receipt.json.original').read_bytes(), original)
        self.assertEqual(self.attempt_path.read_bytes(), attempt)
        committed = json.loads((Path(result['archive']) / 'RESULT.json').read_bytes())
        self.assertEqual(committed, result)
        self.assertEqual(result['receipt_writes'], 1)
        self.assertEqual(result['validation_writes'], 1)

    def test_real_native_user_and_ack_are_reported_separately(self):
        self.fixture.append_user(self.text)
        self.append_ack()
        result = self.run_reconcile()
        self.assertEqual(result['status'], 'READY_ZERO_INPUT_ACK_RECONCILIATION', result)
        self.assertTrue(result['observation']['native_message']['confirmed'])
        self.assertTrue(result['ack']['observed'])

    def reasoned_content(self):
        return [dict(type='thinking', thinking='private synthetic reasoning',
                     signature='synthetic-signature'),
                dict(type='text', text=self.expected)]

    def test_reasoned_ack_preview_is_read_only_and_keeps_reasoning_private(self):
        self.fixture.append_queued(self.text)
        self.append_ack(message=dict(role='assistant', content=self.reasoned_content()))
        before = self.files()
        result = self.run_reconcile()
        self.assertEqual(result['status'], 'READY_ZERO_INPUT_ACK_RECONCILIATION', result)
        self.assertFalse(result['observation']['native_message']['confirmed'])
        self.assertEqual(result['ack']['proof']['ack_line'], self.expected)
        self.assertNotIn('private synthetic reasoning', json.dumps(result))
        self.assertNotIn('synthetic-signature', json.dumps(result))
        self.assertEqual(before, self.files())

    def test_reasoned_stranded_ack_apply_uses_original_writer_without_notice_promotion(self):
        self.prepare_stranded_draft()
        self.append_ack(message=dict(role='assistant', content=self.reasoned_content()))
        original = self.receipt_path.read_bytes()
        attempt = self.attempt_path.read_bytes()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILED', result)
        self.assertEqual(result['validation']['status'], 'PASS')
        receipt = json.loads(self.receipt_path.read_bytes())
        self.assertTrue(receipt['executor_ack'])
        self.assertEqual(receipt['submission_state'], 'STRANDED_IN_COMPOSE')
        self.assertFalse(receipt['late_ack_reconciliation']['native_notice']['confirmed'])
        self.assertEqual(receipt['late_ack_reconciliation']['business_acceptance'], 'NOT_EVALUATED')
        self.assertEqual((Path(result['archive']) / 'handshake-receipt.json.original').read_bytes(), original)
        self.assertEqual(self.attempt_path.read_bytes(), attempt)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'private synthetic reasoning', path.read_bytes())
                self.assertNotIn(b'synthetic-signature', path.read_bytes())

    def test_redacted_reasoning_with_one_exact_text_is_observed(self):
        content = [dict(type='redacted_thinking', data='opaque-synthetic-data'),
                   dict(type='text', text=self.expected)]
        self.append_ack(message=dict(role='assistant', content=content))
        result = self.run_reconcile()
        self.assertEqual(result['status'], 'READY_ZERO_INPUT_ACK_RECONCILIATION', result)
        self.assertNotIn('opaque-synthetic-data', json.dumps(result))

    def test_reasoning_cannot_hide_extra_text_tools_or_malformed_blocks(self):
        valid = self.reasoned_content()
        variants = [
            valid + [dict(type='text', text='')],
            valid + [dict(type='text', text=self.expected)],
            valid + [dict(type='text', text='additional response')],
            valid + [dict(type='tool_use', id='tool1', name='tool', input={})],
            valid + [dict(type='tool_result', content='result')],
            valid + [dict(type='image', source={})],
            [valid[0]],
            [dict(type='thinking', thinking=self.expected, signature='sig')],
            [dict(type='thinking', thinking='private'), valid[1]],
            [dict(type='thinking', thinking=[], signature='sig'), valid[1]],
            [dict(type='thinking', thinking='private', signature=None), valid[1]],
            [dict(valid[0], text='hidden text'), valid[1]],
            [dict(type='redacted_thinking', data=None), valid[1]],
            [dict(type='redacted_thinking', data='opaque', text='hidden'), valid[1]],
            [valid[0], dict(valid[1], extra='hidden')],
            [valid[0], dict(type='text', text=self.expected + '\n')],
            [valid[0], dict(type='text', text=' ' + self.expected)],
            [valid[0], dict(type='text', text=self.expected[:-1] + '0')],
            [valid[0], None, valid[1]],
        ]
        for index, content in enumerate(variants):
            with self.subTest(case=index):
                self.append_ack(message=dict(role='assistant', content=content))
                before = self.files()
                result = self.run_reconcile(apply=True)
                self.assertEqual(result['status'], 'ACK_NOT_VISIBLE', result)
                self.assertEqual(result['receipt_write_attempts'], 0)
                self.assertEqual(before, self.files())

    def test_reasoned_ack_retains_session_role_metadata_and_time_gates(self):
        variants = [dict(sessionId='other-session'), dict(isSidechain=True),
                    dict(isSidechain='false'), dict(isSidechain=0),
                    dict(isMeta=True), dict(isMeta=None), dict(isMeta='false'),
                    dict(type='user'), dict(uuid=''),
                    dict(timestamp='2000-01-01T00:00:00Z'),
                    dict(timestamp='2999-01-01T00:00:00Z'),
                    dict(message=dict(role='user', content=self.reasoned_content()))]
        for index, changes in enumerate(variants):
            with self.subTest(case=index):
                record = dict(message=dict(role='assistant', content=self.reasoned_content()))
                record.update(changes)
                self.append_ack(**record)
                before = self.files()
                result = self.run_reconcile(apply=True)
                self.assertEqual(result['status'], 'ACK_NOT_VISIBLE', result)
                self.assertEqual(result['receipt_write_attempts'], 0)
                self.assertEqual(before, self.files())

    def prepare_stranded_draft(self):
        error = 'DRAFT_NOT_READY: original paste preserved; no submission key sent'
        self.rows[0].update(lifecycle='REJECTED', submission_state='STRANDED_IN_COMPOSE',
                            dispatch_submitted_at=None, error=error, dispatch_error=error)
        self.attempt.update(phase='DRAFT_OBSERVATION', error=error)
        self.attempt['events'].extend([
            dict(phase='PASTED', at_epoch=time.time()),
            dict(phase='DRAFT_OBSERVATION', at_epoch=time.time())])
        self.save()

    def test_stranded_draft_preview_with_late_ack_is_read_only(self):
        self.prepare_stranded_draft()
        self.fixture.append_queued(self.text)
        self.append_ack()
        before = self.files()
        result = self.run_reconcile()
        self.assertEqual(result['status'], 'READY_ZERO_INPUT_ACK_RECONCILIATION', result)
        self.assertFalse(result['observation']['native_message']['confirmed'])
        self.assertEqual(result['observation']['original_receipt_lifecycle'], 'REJECTED')
        self.assertEqual(before, self.files())

    def test_stranded_draft_apply_preserves_submission_and_original_attempt(self):
        self.prepare_stranded_draft()
        self.append_ack()
        original = self.receipt_path.read_bytes()
        attempt = self.attempt_path.read_bytes()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILED', result)
        self.assertEqual(result['validation']['status'], 'PASS')
        receipt = json.loads(self.receipt_path.read_bytes())
        self.assertTrue(receipt['executor_ack'])
        self.assertEqual(receipt['submission_state'], 'STRANDED_IN_COMPOSE')
        self.assertIsNone(receipt['dispatch_submitted_at'])
        self.assertEqual(receipt['dispatch_error'], self.rows[0]['dispatch_error'])
        self.assertEqual(receipt['late_ack_reconciliation']['previous']['lifecycle'], 'REJECTED')
        self.assertFalse(receipt['late_ack_reconciliation']['native_notice']['confirmed'])
        self.assertEqual(receipt['late_ack_reconciliation']['business_acceptance'], 'NOT_EVALUATED')
        self.assertEqual((Path(result['archive']) / 'handshake-receipt.json.original').read_bytes(), original)
        self.assertEqual(self.attempt_path.read_bytes(), attempt)
        self.assertEqual(result['receipt_writes'], 1)

    def test_stranded_draft_screen_ack_alone_does_not_write(self):
        self.prepare_stranded_draft()
        before = self.files()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_NOT_VISIBLE', result)
        self.assertEqual(result['receipt_write_attempts'], 0)
        self.assertEqual(before, self.files())

    def test_other_rejected_states_cannot_use_stranded_draft_reconciliation(self):
        self.prepare_stranded_draft()
        self.append_ack()
        original = copy.deepcopy(self.rows[0])
        for change in [dict(error='another rejection'), dict(dispatch_error='changed'),
                       dict(submission_state='UNKNOWN'), dict(lifecycle='PENDING'),
                       dict(dispatch_submitted_at=datetime.now(timezone.utc).isoformat())]:
            with self.subTest(change=change):
                self.rows[0] = dict(original, **change)
                self.save()
                self.assert_unavailable('COMPLETE_TERMINAL_PANEL')

    def test_stranded_draft_requires_original_paste_history_without_submit(self):
        self.prepare_stranded_draft()
        self.append_ack()
        original = copy.deepcopy(self.attempt)
        for phase in ['SUBMIT_INTENT', 'SUBMITTED', 'RECOVERY_SUBMIT_INTENT',
                      'PASTE_INTENT', 'PASTED', None]:
            with self.subTest(phase=phase):
                self.attempt = copy.deepcopy(original)
                self.attempt['events'].append(dict(phase=phase, at_epoch=time.time()))
                self.save()
                self.assert_unavailable()
        for change in [dict(phase='PASTED'), dict(error='another error'),
                       dict(events=original['events'][:1])]:
            with self.subTest(change=change):
                self.attempt = dict(copy.deepcopy(original), **change)
                self.save()
                self.assert_unavailable('STRANDED_DRAFT_HISTORY_REQUIRED')

    def test_stranded_draft_still_requires_original_supervisor(self):
        self.prepare_stranded_draft()
        self.append_ack()
        snapshot = copy.deepcopy(self.caller.return_value)
        snapshot[3]['foreground']['thread_id'] = 'maintenance-successor'
        self.caller.return_value = snapshot
        self.assert_unavailable('SUPERVISOR_THREAD_CHANGED')

    def test_other_rejected_panel_member_remains_failed_after_selected_ack(self):
        self.prepare_stranded_draft()
        self.append_ack()
        other = dict(self.rows[0], ordinal=2, executor='other-target', ack_nonce='12345678',
            ack_line_expected=f'PREFLIGHT_ACK|{self.task}|claude:identity|READY|INLINE|12345678')
        self.rows.append(other)
        self.gate['executors'].append(dict(surface_uuid='other-target', surface_ref='other-target',
            provider='claude', pane_ref='pane:3'))
        self.save()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILED', result)
        self.assertEqual(result['validation']['status'], 'FAIL')
        receipt = json.loads(self.receipt_path.read_bytes())
        self.assertEqual(receipt['executors'][1], other)
        self.assertFalse(receipt['all_executors_acked'])

    def test_screen_ack_without_bound_assistant_record_cannot_apply(self):
        before = self.files()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_NOT_VISIBLE', result)
        self.assertTrue(result['observation']['ack']['observed'])
        self.assertEqual(before, self.files())

    def test_wrong_nonce_session_role_sidechain_and_echo_do_not_authorize(self):
        for changes in [dict(sessionId='other-session'), dict(isSidechain=True),
                dict(isMeta=True), dict(type='user'), dict(uuid=''),
                dict(message=dict(role='assistant', content=self.expected + '0')),
                dict(message=dict(role='assistant', content='echo ' + self.expected))]:
            with self.subTest(changes=changes):
                self.append_ack(**changes)
                result = self.run_reconcile(apply=True)
                self.assertEqual(result['status'], 'ACK_NOT_VISIBLE', result)
                self.assertEqual(result['receipt_write_attempts'], 0)

    def test_ack_before_original_fence_does_not_authorize(self):
        self.append_ack()
        self._prepare_attempt()
        self.save()
        self.assertEqual(self.run_reconcile(apply=True)['status'], 'ACK_NOT_VISIBLE')

    def test_changed_original_fence_is_rejected(self):
        self.append_ack()
        self.attempt['events'][0]['native_paste_fence']['binding_sha256'] = '0' * 64
        self.save()
        self.assert_unavailable()

    def test_original_supervisor_thread_cannot_be_inherited_by_successor(self):
        self.append_ack()
        snapshot = copy.deepcopy(self.caller.return_value)
        snapshot[3]['foreground']['thread_id'] = 'new-thread-same-surface'
        self.caller.return_value = snapshot
        self.assert_unavailable('SUPERVISOR_THREAD_CHANGED')

    def test_apply_requires_original_harness_exit_attestation(self):
        self.append_ack()
        self.args.original_harness_exited = False
        self.assert_unavailable('EXIT_ATTESTATION_REQUIRED')

    def test_pending_panel_and_business_pack_are_rejected(self):
        self.append_ack()
        self.rows[0].update(status='AWAITING_EXECUTOR_ACK', lifecycle='PENDING')
        self.save()
        self.assert_unavailable('COMPLETE_TERMINAL_PANEL')

    def test_existing_business_pack_is_not_reconciled(self):
        self.append_ack()
        (self.root / 'task-pack.json').write_text('{}')
        self.assert_unavailable('BUSINESS_PACK_ALREADY_EXISTS')

    def test_other_unacked_executor_keeps_original_validation_failed(self):
        self.append_ack()
        other = dict(self.rows[0], ordinal=2, executor='other-target', ack_nonce='12345678',
            ack_line_expected=f'PREFLIGHT_ACK|{self.task}|claude:identity|READY|INLINE|12345678')
        self.rows.append(other)
        self.gate['executors'].append(dict(surface_uuid='other-target', surface_ref='other-target',
            provider='claude', pane_ref='pane:3'))
        self.save()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILED', result)
        self.assertEqual(result['validation']['status'], 'FAIL')
        receipt = json.loads(self.receipt_path.read_bytes())
        self.assertEqual(receipt['executors'][1], other)
        self.assertEqual(receipt['unproven_executors'], ['other-target'])
        self.assertFalse(receipt['all_executors_acked'])

    def test_delivery_lock_competition_does_not_write(self):
        self.append_ack()
        with (self.attempt_path.parent / 'delivery.lock').open('r+') as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_unavailable()

    def test_directory_lock_competition_does_not_write(self):
        self.append_ack()
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_unavailable()
        finally:
            os.close(fd)

    def test_archive_symlink_is_never_followed(self):
        self.append_ack()
        outside = self.home / 'outside'
        outside.mkdir()
        (self.root / 'handshake-reconciliations').symlink_to(outside, target_is_directory=True)
        self.assert_unavailable()
        self.assertEqual(list(outside.iterdir()), [])

    def mutate_after_ack_scan(self, change):
        original = reconcile.scan_ack
        def scan(*args, **kwargs):
            result = original(*args, **kwargs)
            change()
            return result
        return self.patch(reconcile, 'scan_ack', side_effect=scan)

    def test_concurrent_original_receipt_edit_is_not_overwritten(self):
        self.append_ack()
        self.mutate_after_ack_scan(lambda: self.receipt_path.write_text('new owner state'))
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILIATION_UNAVAILABLE', result)
        self.assertIn('EVIDENCE_CHANGED', result['error'])
        self.assertEqual(self.receipt_path.read_text(), 'new owner state')
        self.assertEqual(result['receipt_write_attempts'], 0)

    def test_replaced_root_is_rejected_before_archive_or_receipt_write(self):
        self.append_ack()
        def replace():
            self.root.rename(self.home / 'original-root-preserved')
            self.root.mkdir()
        self.mutate_after_ack_scan(replace)
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILIATION_UNAVAILABLE', result)
        self.assertIn('LOCK_CHANGED', result['error'])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_body_reference_content_change_is_rejected(self):
        body = 'STATUS: ' + self.nonce + '\n' + 'original body\n' * 80
        prepared = journal.prompt_reference.plan(body, self.nonce)
        self.body_reference = prepared['reference']
        self.body_pin = journal.prompt_reference.persist(self.body_reference, body)
        self.text = prepared['text']
        self._prepare_attempt()
        self.rows[0].update(dispatch_payload_sha256=digest(self.text.encode()),
                            dispatch_body_reference=self.body_reference)
        self.save()
        self.append_ack()
        body_path = Path(self.body_reference['path'])
        self.mutate_after_ack_scan(lambda: body_path.write_bytes(body.encode() + b' '))
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILIATION_UNAVAILABLE', result)
        self.assertEqual(result['receipt_write_attempts'], 0)

    def test_partial_archive_stays_incomplete_and_is_never_replayed(self):
        self.append_ack()
        original = reconcile.write_exclusive
        def fail(fd, name, raw):
            if name == 'handshake-receipt.json.original':
                original(fd, name, raw[:10])
                raise OSError('partial archive write')
            return original(fd, name, raw)
        with mock.patch.object(reconcile, 'write_exclusive', side_effect=fail):
            result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILIATION_INCOMPLETE', result)
        self.assertEqual(result['receipt_write_attempts'], 0)
        before = self.files()
        again = self.run_reconcile(apply=True)
        self.assertEqual(again['status'], 'ACK_RECONCILIATION_INCOMPLETE', again)
        self.assertEqual(again['receipt_write_attempts'], 0)
        self.assertEqual(before, self.files())

    def test_exception_after_original_writer_preserves_published_receipt(self):
        self.append_ack()
        original = harness._write_handshake_receipt
        def write_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('failure after publishing receipt')
        with mock.patch.object(harness, '_write_handshake_receipt', side_effect=write_then_fail):
            result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILIATION_INCOMPLETE', result)
        self.assertEqual(result['receipt_writes'], 1)
        self.assertTrue(json.loads(self.receipt_path.read_bytes())['executor_ack'])
        before = self.files()
        again = self.run_reconcile(apply=True)
        self.assertEqual(again['status'], 'ACK_RECONCILIATION_INCOMPLETE', again)
        self.assertEqual(before, self.files())

    def test_missing_result_commit_stays_incomplete(self):
        self.append_ack()
        original = reconcile.write_exclusive
        def fail_commit(fd, name, raw):
            if name == 'RESULT.json':
                raise OSError('commit unavailable')
            return original(fd, name, raw)
        with mock.patch.object(reconcile, 'write_exclusive', side_effect=fail_commit):
            result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILIATION_INCOMPLETE', result)
        self.assertEqual(result['archive_commit_record']['state'], 'UNCONFIRMED')
        before = self.files()
        again = self.run_reconcile(apply=True)
        self.assertEqual(again['status'], 'ACK_RECONCILIATION_INCOMPLETE', again)
        self.assertEqual(before, self.files())

    def test_repeated_apply_and_preview_are_read_only(self):
        self.append_ack()
        self.assertEqual(self.run_reconcile(apply=True)['status'], 'ACK_RECONCILED')
        before = self.files()
        for apply in (False, True):
            result = self.run_reconcile(apply=apply)
            self.assertEqual(result['status'], 'ALREADY_RECONCILED', result)
            self.assertEqual(result['receipt_write_attempts'], 0)
            self.assertEqual(result['archive_write_attempts'], 0)
            self.assertEqual(before, self.files())

    def test_reentry_detects_published_validation_change(self):
        self.append_ack()
        self.assertEqual(self.run_reconcile(apply=True)['status'], 'ACK_RECONCILED')
        (self.root / 'validation.json').write_text('{}')
        before = self.files()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_PUBLISHED_STATE_CHANGED', result)
        self.assertEqual(before, self.files())

    def test_unrelated_invalid_marker_does_not_block_original_writer(self):
        directory = harness._ACTIVE_DIR / 'workspace'
        directory.mkdir(parents=True)
        marker = directory / 'unrelated.json'
        marker.write_text('{broken unrelated task')
        self.append_ack()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILED', result)
        self.assertEqual(marker.read_text(), '{broken unrelated task')

    def test_original_writer_heartbeat_is_measured_separately(self):
        directory = harness._ACTIVE_DIR / 'workspace'
        directory.mkdir(parents=True)
        marker = directory / 'original.json'
        original = dict(task_id=self.task, artifact_root=str(self.root),
                        last_activity_at='2026-01-01T00:00:00+00:00')
        marker.write_bytes(reconcile.json_bytes(original))
        self.append_ack()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILED', result)
        current = json.loads(marker.read_bytes())
        self.assertNotEqual(current['last_activity_at'], original['last_activity_at'])
        current['last_activity_at'] = original['last_activity_at']
        self.assertEqual(current, original)
        self.assertEqual(len(result['heartbeat_changes']), 1)
        self.assertEqual(result['heartbeat_changes'][0]['attribution'],
                         'OBSERVED_DURING_ORIGINAL_WRITER_CALLS_NOT_EXCLUSIVE')

    def test_archive_replacement_after_intent_is_not_written_through(self):
        self.append_ack()
        original = reconcile.write_exclusive
        def replace(fd, name, raw):
            original(fd, name, raw)
            if name == 'INTENT.json':
                path = self.root / 'handshake-reconciliations' / self.args.receipt_sha256
                path.rename(path.with_name('preserved-operation'))
                path.mkdir()
        with mock.patch.object(reconcile, 'write_exclusive', side_effect=replace):
            result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILIATION_INCOMPLETE', result)
        self.assertIn('ARCHIVE_DIRECTORY_CHANGED', result['error'])
        self.assertEqual(result['receipt_write_attempts'], 0)
        self.assertEqual(list(Path(result['archive']).iterdir()), [])

    def test_second_executor_ack_preserves_first_executor_and_aggregate(self):
        self.append_ack()
        first = dict(self.rows[0], ordinal=1, executor='first-target', ack_nonce='12345678',
            status='PASS', lifecycle='ACKED', executor_ack=True,
            ack_line_expected=f'PREFLIGHT_ACK|{self.task}|claude:identity|READY|INLINE|12345678',
            ack_line=f'PREFLIGHT_ACK|{self.task}|claude:identity|READY|INLINE|12345678',
            ack_source='executor_response_nonce', ack_response_marker=True)
        self.rows[0]['ordinal'] = 2
        self.rows.insert(0, first)
        self.gate['executors'].insert(0, dict(surface_uuid='first-target', surface_ref='first-target',
            provider='claude', pane_ref='pane:3'))
        self.save()
        result = self.run_reconcile(apply=True)
        self.assertEqual(result['status'], 'ACK_RECONCILED', result)
        receipt = json.loads(self.receipt_path.read_bytes())
        self.assertEqual(receipt['executors'][0], first)
        self.assertTrue(receipt['all_executors_acked'])
        self.assertEqual(result['validation']['status'], 'PASS')

    def test_reentry_rechecks_original_supervisor_thread(self):
        self.append_ack()
        self.assertEqual(self.run_reconcile(apply=True)['status'], 'ACK_RECONCILED')
        snapshot = copy.deepcopy(self.caller.return_value)
        snapshot[3]['foreground']['thread_id'] = 'successor-on-original-surface'
        self.caller.return_value = snapshot
        self.assert_unavailable('SUPERVISOR_THREAD_CHANGED')


if __name__ == '__main__':
    unittest.main()

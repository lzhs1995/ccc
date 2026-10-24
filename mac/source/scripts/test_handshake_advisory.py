"""Original handshake reminders: real manifests, journals and native records.

Only external process/TTY/tree/terminal I/O is replaced. No validator is mocked
to grant identity, controller trust, receipt proof or task acceptance.
"""
import copy
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stdout
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_hook_runtime_audit as audit
import cmux_prompt_reference as reference
import cmux_workflow_advisory as workflow
import executor_ready as ready
import handshake_advisory as advisory
import handshake_evidence as evidence
import mac_harness as harness
from delivery_receipts import snapshot
from native_test_support import NativeFixture
from offline_hook_thread_fixture import native_processes, native_thread
import test_cmux_daemon_identity as daemon_inputs
from test_cmux_daemon_identity import S, C, W, P, Q, T
from test_codex_input import FakeRPC

IDLE = '❯ \n[claude-opus-5]'


class HandshakeFixture(unittest.TestCase):
    PROVIDER = 'claude'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.native = NativeFixture.attach(self, home=self.home, provider=self.PROVIDER, identity=dict(
            caller_surface_uuid=C, target_surface_uuid=T, workspace_uuid=W,
            caller_pane_uuid=P, target_pane_uuid=Q))
        self.supervisor_session = S
        if self.PROVIDER == 'codex':
            self.supervisor_session = C
            self.daemon_inputs = daemon_inputs.DaemonTests()
            self.daemon_inputs.setUp()
            self.native.session_id = S
            self.native.process_state = self.daemon_inputs.processes[30]
            self.native.process_state['env']['CMUX_SURFACE_ID'] = T
            self.native.tty = self.native.process_tty = 'ttys2'
            self.native.tree['windows'][0]['workspaces'][0]['panes'][0]['surfaces'][0]['tty'] = 'ttys2'
            payload, _ = native_thread(self.home, actual=S)
            self.native.transcript = Path(payload['transcript_path'])
            self.native.native_root = self.home / '.codex/sessions'
            self.fake_rpc = FakeRPC(self.native, receive=False)
            self.native.rpc.side_effect = lambda **_: self.fake_rpc
        self.source = self.home / '.local/share/multi-agent-collaboration/releases/test/source'
        self.source.mkdir(parents=True)
        original = Path(__file__).resolve().parents[1]
        members = [original / 'VERSION', original / 'SKILL.md', *sorted((original / 'scripts').glob('*.py'))]
        files = {}
        for path in members:
            name = str(path.relative_to(original))
            raw = path.read_bytes()
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            target.chmod(0o444)
            files[name] = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), executable=False)
        manifest = self.source.parent / 'MANIFEST.json'
        self.write(manifest, dict(files=files))
        manifest.chmod(0o444)
        spec = importlib.util.spec_from_file_location('original_handshake_sender',
            self.source / 'scripts/cmux_message_journal.py')
        self.sender = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sender)
        self.active = self.home / 'active'
        (self.active / W).mkdir(parents=True)
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.contexts.enter_context(patch.object(advisory.markers.stop_guard, 'ACTIVE_DIR', self.active))
        self.contexts.enter_context(patch.object(ready, 'state_root', return_value=self.home / 'state'))
        self.contexts.enter_context(patch.object(bridge, 'COLLABORATION_SKILL_PATH', self.source / 'SKILL.md'))
        self.supervisor_payload, _ = native_thread(self.home, actual=self.supervisor_session)
        self.payload = dict(hook_event_name='PostToolUse', session_id=self.native.session_id,
            transcript_path=str(self.native.transcript), tool_name='Bash')
        self.cursor = self.home / 'cursors'
        self.emissions = []
        self.case = self.make_case('task')
        self.screen = self.contexts.enter_context(patch.object(bridge, 'read_screen',
            side_effect=AssertionError('an observer cannot inspect compose')))
        self.reset_input_mocks()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def reset_input_mocks(self):
        self.native.send.reset_mock()
        self.native.key.reset_mock()
        self.native.rpc.reset_mock()

    def make_case(self, task):
        root = self.home / task
        root.mkdir()
        row = dict(task_id=task, supervisor='surface:1', executor='surface:2',
            executor_provider=self.PROVIDER, ordinal=1, ack_nonce='nonce-' + uuid.uuid4().hex,
            executor_ack=False)
        body = evidence.challenge(row, root, self.source / 'SKILL.md')
        prepared = reference.plan(body, row['ack_nonce'])
        reference.persist(prepared['reference'], body)
        row.update(dispatch_payload=prepared['text'],
            dispatch_payload_sha256=reference.digest(prepared['text']),
            dispatch_body_reference=prepared['reference'])
        evidence.record_controller(row, self.sender.__file__, self.source / 'SKILL.md',
            self.source / 'scripts/mac_harness.py', self.source / 'scripts/cmux_bridge.py')
        self.assertEqual(row['dispatch_controller_status'], 'PINNED_BEFORE_INPUT')
        with (patch.object(bridge, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(prepared['text'], 'claude'), IDLE))
                if self.PROVIDER == 'claude' else nullcontext()):
            with self.assertRaises(bridge.DispatchUnconfirmed) as raised:
                self.sender.deliver(bridge, T, prepared['text'], row['ack_nonce'], wait_seconds=0)
        evidence.record_dispatch(row, raised.exception.details, failed=True)
        self.assertEqual(row['dispatch_controller_evidence_status'], 'VERIFIED')
        self.assertEqual(row['dispatch_evidence_status'], 'ORIGINAL_ATTEMPT_PINNED')
        receipt = root / 'handshake-receipt.json'
        self.write(receipt, dict(executors=[row]))
        with patch.object(harness, '_ACTIVE_DIR', self.active), \
                patch.object(harness, '_workspace_key', return_value=W):
            marker = harness.arm_task(task, root, 'surface:1', 'surface:2',
                supervisor_provider='codex', executor_provider=self.PROVIDER, workspace_uuid=W,
                supervisor_surface_uuid=C, executor_surface_uuid=T,
                native_participants=[dict(role='supervisor', provider='codex', surface_uuid=C,
                    native_session_id=self.supervisor_session),
                    dict(role='executor', provider=self.PROVIDER, surface_uuid=T,
                    native_session_id=self.native.session_id)])
        return dict(root=root, row=row, receipt=receipt, wire=prepared['text'], marker=marker,
            markerfile=self.active / W / (marker['collaboration_id'] + '.json'),
            attempt=Path(row['dispatch_attempt']['path']))

    def originals(self):
        paths = [self.native.transcript, Path(self.supervisor_payload['transcript_path'])]
        for base in (self.active, self.home / '.local/state/multi-agent-collaboration'):
            paths.extend(p for p in base.rglob('*') if p.is_file())
        paths.extend(p for p in self.home.glob('task*/*') if p.is_file())
        return {str(p): (p.read_bytes(), p.stat().st_ino) for p in paths}

    @contextmanager
    def caller(self, role, foreground=None):
        if self.PROVIDER == 'codex' and role == 'executor':
            with native_processes(self.daemon_inputs, caller=T), (
                    patch('cmux_daemon_identity.foreground.read',
                          return_value={'thread_id': foreground}) if foreground else nullcontext()):
                yield
        elif role == 'supervisor':
            with native_processes(), (patch('cmux_daemon_identity.foreground.read',
                    return_value={'thread_id': foreground}) if foreground else nullcontext()):
                yield
        else:
            with patch.object(advisory.os, 'getppid', return_value=self.native.process_state['pid']), \
                    patch.object(advisory.executors.maintenance.identity, 'process',
                                 side_effect=self.native._process), \
                    patch.object(advisory.executors, 'subprocess', SimpleNamespace(run=self.native._ps)):
                yield

    def discover(self, now=100., *, role='executor', payload=None, emit=None, foreground=None):
        before = self.originals()
        with self.caller(role, foreground):
            result = advisory.discover(payload or (self.payload if role == 'executor'
                else self.supervisor_payload), now=now, state_root=self.cursor,
                emit=self.emissions.append if emit is None else emit)
        self.assertEqual(self.originals(), before)
        self.native.send.assert_not_called()
        self.native.key.assert_not_called()
        self.native.rpc.assert_not_called()
        self.screen.assert_not_called()
        return result

    def save_row(self, case=None):
        case = case or self.case
        self.write(case['receipt'], dict(executors=[case['row']]))


class HandshakeTests(HandshakeFixture):
    def test_pending_queued_native_and_ack_share_original_sixty_second_budget(self):
        first = self.discover()[0]
        self.assertEqual(first['notice_native_state'], 'NATIVE_PENDING')
        self.assertEqual(first['original_attempt'], snapshot(self.case['attempt']))
        self.assertFalse(first['body_read_confirmed'])
        self.assertFalse(first['business_accepted'])
        self.native.append_queued(self.case['wire'])
        self.assertEqual(self.discover(130), [])
        queued = self.discover(160)[0]
        self.assertEqual(queued['notice_native_state'], 'NATIVE_QUEUED')
        self.assertFalse(queued['notice_native_received'])
        self.native.append_user(self.case['wire'])
        self.case['row']['executor_ack'] = True
        self.save_row()
        self.assertEqual(self.discover(190), [])
        notice = self.discover(220)[0]
        self.assertEqual(notice['kind'], 'ACKED_RESERVATION')
        self.assertTrue(notice['notice_native_received'])
        self.assertFalse(notice['ack_independently_verified'])
        self.assertEqual(notice['task_pack_state'], 'ABSENT_DISPATCH_UNKNOWN')
        self.assertEqual(notice['input_operations'], 0)

    def test_reconcile_preserves_original_attempt_and_does_not_settle_reservation(self):
        before = self.case['attempt'].read_bytes()
        self.native.append_user(self.case['wire'])
        result = self.sender.deliver(bridge, T, self.case['wire'], self.case['row']['ack_nonce'],
            wait_seconds=0, reconcile_only=True)
        self.assertTrue(result['confirmed'])
        self.assertEqual(before, self.case['attempt'].read_bytes())
        notice = self.discover()[0]
        self.assertTrue(notice['notice_native_received'])
        self.assertFalse(notice['release_verified'])
        self.assertEqual(len(self.discover(160)), 1)

    def test_supervisor_authenticates_root_and_rejects_child_foreground_and_transcript(self):
        for change in (dict(session_id=str(uuid.uuid4())), dict(subagent={'parent_thread_id': S}),
                       dict(transcript_path=str(self.native.transcript))):
            self.assertEqual(self.discover(role='supervisor', payload=dict(self.supervisor_payload, **change)), [])
        self.assertEqual(self.discover(role='supervisor', foreground=T), [])
        self.assertFalse(self.cursor.exists())
        self.assertEqual(len(self.discover(role='supervisor')), 1)

    def test_executor_original_session_tty_birth_and_transcript_are_required(self):
        self.assertEqual(self.discover(payload=dict(self.payload, session_id=S)), [])
        self.assertEqual(self.discover(payload=dict(self.payload,
            transcript_path=self.supervisor_payload['transcript_path'])), [])
        with patch.object(self.native, 'process_tty', 'different-tty'):
            self.assertEqual(self.discover(), [])
        with patch.dict(self.native.process_state, birth='reused-pid'):
            self.assertEqual(self.discover(), [])
        original = self.native.transcript.with_suffix('.original')
        self.native.transcript.rename(original)
        self.native.transcript.write_bytes(original.read_bytes())
        self.assertEqual(self.discover(), [])

    def test_complete_manifest_change_and_untrusted_runtime_preserve_attempt_without_authority(self):
        path = self.source / 'scripts/executor_ready.py'
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b'\n# changed\n')
        path.chmod(0o444)
        self.assertEqual(self.discover(), [])
        result = self.discover(role='supervisor')[0]
        self.assertEqual(result['controller_state'], 'ORIGINAL_EVIDENCE_UNAVAILABLE')
        self.assertFalse(result['notice_native_received'])
        evidence.record_dispatch(self.case['row'], self.case['row']['dispatch_error_details'], failed=True)
        self.assertEqual(self.case['row']['dispatch_attempt'], snapshot(self.case['attempt']))
        self.assertEqual(self.case['row']['dispatch_controller_evidence_status'], 'UNVERIFIED')

    def test_missing_sender_return_never_reconstructs_by_nonce(self):
        row = dict(ack_nonce=self.case['row']['ack_nonce'])
        evidence.record_dispatch(row, {'state': 'unknown'}, failed=True)
        self.assertNotIn('dispatch_attempt', row)
        self.assertEqual(row['dispatch_evidence_status'], 'UNAVAILABLE')

    def test_body_and_original_attempt_pins_cannot_be_repaired_from_live_state(self):
        path = self.case['attempt']
        attempt = json.loads(path.read_bytes())
        attempt['events'][0].pop('native_paste_fence', None)
        self.write(path, attempt)
        self.assertEqual(self.discover(), [])
        diagnostic = self.discover(role='supervisor')[0]
        self.assertEqual(diagnostic['controller_state'], 'ORIGINAL_EVIDENCE_UNAVAILABLE')
        self.assertIsNone(diagnostic['body_reference'])

    def test_top_level_or_duplicate_executor_fields_never_substitute_for_own_receipt(self):
        row = copy.deepcopy(self.case['row'])
        self.write(self.case['receipt'], dict(row, executors=[dict(row, ordinal=2)]))
        self.assertEqual(self.discover(), [])
        self.write(self.case['receipt'], dict(row, executors=[row, row]))
        self.assertEqual(self.discover(), [])
        self.save_row()
        self.assertEqual(len(self.discover()), 1)

    def test_more_than_thirty_two_unrelated_markers_rotate(self):
        for i in range(advisory.MAX_MARKERS + 7):
            self.write(self.active / W / ('0000-%03d.json' % i), {'unrelated': True})
        self.assertEqual(self.discover(), [])
        self.assertEqual(len(self.discover()), 1)

    def test_more_than_eight_pending_handshakes_rotate_without_duplicates(self):
        cases = [self.case] + [self.make_case('task-%02d' % i) for i in range(10)]
        self.reset_input_mocks()
        first = self.discover()
        second = self.discover()
        ids = [n['task_id'] for n in first + second]
        self.assertEqual(len(first), 8)
        self.assertEqual(len(second), 3)
        self.assertEqual(set(ids), {c['row']['task_id'] for c in cases})
        self.assertEqual(len(ids), len(set(ids)))

    def test_one_marker_more_than_eight_workers_resumes_before_other_markers(self):
        case = self.case
        rows = [case['row']]
        participants = case['marker']['participants']
        # Missing native evidence is an honest supervisor diagnostic. These
        # workers are distinct original marker/receipt identities, never a
        # substituted receiver or a mocked successful identity check.
        for ordinal in range(2, 12):
            worker = dict(participants[1], ordinal=ordinal,
                          surface_uuid=str(uuid.uuid4()), native_session_id=str(uuid.uuid4()),
                          surface_ref='surface:' + str(ordinal + 1))
            participants.append(worker)
            row = dict(case['row'], ordinal=ordinal, executor=worker['surface_ref'],
                       ack_nonce='missing-original-' + str(ordinal))
            row.pop('dispatch_attempt')
            rows.append(row)
        self.write(case['markerfile'], case['marker'])
        self.write(case['receipt'], dict(executors=rows))
        first = self.discover(role='supervisor')
        self.assertEqual(len(first), 8)
        cursor = next(self.cursor.glob('*.json'))
        before = cursor.read_bytes()

        def failed_flush(_):
            raise BrokenPipeError('closed output')

        self.assertEqual(self.discover(role='supervisor', emit=failed_flush), [])
        self.assertEqual(cursor.read_bytes(), before)
        # More than a scan budget of later markers prevents cursor wraparound
        # from accidentally satisfying the unfinished-marker-first contract.
        for index in range(advisory.MAX_MARKERS + 1):
            self.write(self.active / W / ('zzzz-unrelated-%03d.json' % index),
                       {'unrelated': True})
        second = self.discover(role='supervisor')
        self.assertEqual([n['ordinal'] for n in first + second], list(range(1, 12)))
        self.assertEqual(len(second), 3)
        self.assertTrue(all(n['input_operations'] == 0 for n in first + second))
        self.assertTrue(all(n['controller_state'] == 'ORIGINAL_EVIDENCE_UNAVAILABLE'
                            for n in first[1:] + second))
        self.assertEqual(self.discover(role='supervisor'), [])

    def test_deadline_does_not_skip_marker_or_publish_a_native_confirmation(self):
        for i in range(advisory.MAX_MARKERS):
            self.write(self.active / W / ('0000-%03d.json' % i), {'unrelated': True})
        self.assertEqual(self.discover(), [])
        cursor = next(self.cursor.glob('*.json'))
        before = cursor.read_bytes()
        self.native.append_user(self.case['wire'])
        with patch.object(advisory.native, 'probe', side_effect=ready.WaitBudgetExpired):
            self.assertEqual(self.discover(), [])
        self.assertEqual(cursor.read_bytes(), before)
        notice = self.discover()[0]
        self.assertTrue(notice['notice_native_received'])

    def test_native_scan_resumes_across_budget_without_skipping_or_false_positive(self):
        for _ in range(5):
            self.native.append_record(dict(type='assistant', sessionId=self.native.session_id,
                message=dict(role='assistant', content='x' * 220)))
        self.native.append_user(self.case['wire'])
        with patch.object(advisory, 'MAX_NATIVE_BYTES', 350):
            notice = self.discover()[0]
            self.assertFalse(notice['notice_native_received'])
            for _ in range(8):
                self.discover(101)
            final = self.discover(160)[0]
        self.assertTrue(final['notice_native_received'])

    def test_failed_flush_preserves_cursors_and_nonblocking_lock_contention(self):
        def fail(_):
            raise BrokenPipeError('closed output')
        self.assertEqual(self.discover(emit=fail), [])
        self.assertEqual(list(self.cursor.glob('*.json')), [])
        lockfile = next(self.cursor.glob('*.lock'))
        with lockfile.open('r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.discover(), [])
        self.assertEqual(len(self.discover()), 1)

    def test_stop_is_silent_and_real_wrapper_survives_audit_failure(self):
        output = io.StringIO()
        for event in ('Stop', 'PostToolUse'):
            output.seek(0)
            output.truncate()
            with self.caller('executor'), patch.object(audit, 'record', side_effect=OSError('disk full')), \
                    patch('sys.stdin', io.StringIO(json.dumps(dict(self.payload, hook_event_name=event)))), \
                    patch('sys.argv', ['observer', '--hook', 'handshake_advisory.py']), redirect_stdout(output):
                self.assertEqual(workflow.main(), 0)
            if event == 'Stop':
                self.assertEqual(output.getvalue(), '')
            else:
                value = json.loads(output.getvalue())
                self.assertEqual(set(value), {'hookSpecificOutput'})
                self.assertEqual(value['hookSpecificOutput']['hookEventName'], 'PostToolUse')
                self.assertIn('PENDING_HANDSHAKE', value['hookSpecificOutput']['additionalContext'])
        self.native.send.assert_not_called()
        self.native.key.assert_not_called()
        self.native.rpc.assert_not_called()
        self.screen.assert_not_called()

    def test_formal_native_receipt_plus_reported_ack_ends_reminder_only(self):
        case = self.case
        report = case['root'] / 'report.md'
        nonce = 'completion-nonce'
        packfile = case['root'] / 'task-pack.json'
        pack = dict(draft=False, task_id='task', executor='surface:2', executor_uuid=T,
            completion_nonce=nonce, report=str(report), callback_target='surface:1',
            required_skill=str(self.source / 'SKILL.md'),
            completion_callback='DONE|task|' + nonce + '|REPORT=' + str(report),
            completion_receipt=str(case['root'] / 'completion-receipt.json'),
            completion_delivery=dict(transport='cmux_bridge.submit_completion_callback', require_confirmed=True))
        self.write(packfile, pack)
        self.assertEqual(self.discover()[0]['task_pack_state'], 'ON_DISK_DISPATCH_UNVERIFIED')
        wire = bridge.task_pack_notice(packfile)
        with patch.object(bridge, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(wire, 'claude'), IDLE)):
            with self.assertRaises(bridge.DispatchUnconfirmed):
                bridge.submit_task_pack(T, wire, packfile, marker='task', wait_seconds=0)
        self.native.append_queued(wire)
        self.reset_input_mocks()
        self.assertEqual(self.discover(160)[0]['task_pack_state'], 'ON_DISK_DISPATCH_UNVERIFIED')
        self.native.append_user(wire)
        result = bridge.submit_task_pack(T, wire, packfile, marker='task', reconcile_only=True, wait_seconds=0)
        self.assertTrue(result['confirmed'])
        self.assertEqual(self.discover(220)[0]['task_pack_state'], 'NATIVE_RECEIVED')
        case['row']['executor_ack'] = True
        self.save_row()
        self.assertEqual(self.discover(280), [])
        self.assertTrue(case['markerfile'].exists())
        self.assertFalse(Path(pack['completion_receipt']).exists())


class CodexExecutorTests(HandshakeFixture):
    PROVIDER = 'codex'

    def test_original_codex_executor_receives_own_handshake_without_input(self):
        notice = self.discover()[0]
        self.assertEqual(notice['controller_state'], 'VERIFIED_ORIGINAL_CONTROLLER')
        self.assertEqual(notice['executor_uuid'], T)
        self.assertEqual(notice['executor_session'], S)
        self.assertEqual(notice['notice_native_state'], 'NATIVE_PENDING')
        self.native.append_user(self.case['wire'])
        self.assertTrue(self.discover(160)[0]['notice_native_received'])

    def test_codex_executor_rejects_child_wrong_session_and_foreground(self):
        for payload in (dict(self.payload, session_id=C),
                        dict(self.payload, subagent={'parent_thread_id': C}),
                        dict(self.payload, transcript_path=self.supervisor_payload['transcript_path'])):
            with self.subTest(payload=payload):
                self.assertEqual(self.discover(payload=payload), [])
        self.assertEqual(self.discover(foreground=C), [])
        self.assertEqual(len(self.discover()), 1)

    def test_codex_hook_process_must_be_the_original_receiver(self):
        original = self.daemon_inputs.processes[30]
        for change in (dict(birth=[200, 30]), dict(executable='/changed/codex'),
                       dict(file_identity=[1, 30, 999, 1])):
            with self.subTest(change=change), patch.dict(original, change):
                self.assertEqual(self.discover(), [])
        with patch.object(self.native, 'process_tty', 'wrong-tty'):
            self.assertEqual(self.discover(), [])
        saved = self.native.transcript.with_suffix('.original')
        self.native.transcript.rename(saved)
        self.native.transcript.write_bytes(saved.read_bytes())
        self.assertEqual(self.discover(), [])


if __name__ == '__main__':
    unittest.main()

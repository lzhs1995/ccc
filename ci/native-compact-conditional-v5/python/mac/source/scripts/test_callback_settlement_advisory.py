"""Real callback journals and native proofs; only external client I/O is replaced."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from contextlib import ExitStack, redirect_stdout, nullcontext

import callback_settlement_advisory as advisory
from callback_native_evidence import reconcile_received
import cmux_bridge as bridge
import cmux_workflow_advisory as workflow
import cmux_hook_runtime_audit as audit
import executor_ready as ready
import mac_harness as harness
from native_test_support import LegacyTerminalFixture
from offline_hook_thread_fixture import native_processes
from test_cmux_daemon_identity import S, C, W, P, Q, T

IDLE = '› Ask Codex to do anything\nGPT-6-Astra high\n? for shortcuts'


class SettlementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve()
        self.root = self.home / 'task'
        self.root.mkdir()
        # Real executors keep frozen reports in a sibling output directory.
        # Exercise that contract through the original journal/native validator.
        output = self.home / 'executor-output'
        output.mkdir()
        self.report = output / 'report.md'
        self.report.write_text('Frozen executor result; supervisor review remains pending.')
        self.gate = self.root / 'identity-gate.json'
        self.write(self.gate, dict(status='PASS', task_id='task', supervisor='surface:1',
                                  supervisor_surface_uuid=C, workspace_uuid=W,
                                  executor='surface:2', executor_surface_uuid=T))
        self.pack = dict(draft=False, task_id='task', completion_nonce='nonce12345',
                         callback_target='surface:1', executor='surface:2', executor_uuid=T,
                         completion_callback='DONE|task|nonce12345|REPORT=' + str(self.report),
                         completion_receipt=str(self.root / 'receipt.json'), report=str(self.report),
                         identity_gate=str(self.gate), finalized_at='2026-01-01T00:00:00+00:00',
                         required_skill=str(bridge.COLLABORATION_SKILL_PATH),
                         completion_delivery=dict(transport='cmux_bridge.submit_completion_callback',
                                                  require_confirmed=True),
                         availability_state=str(self.root / 'availability.json'))
        self.packfile = self.root / 'task-pack.json'
        self.write(self.packfile, self.pack)
        self.receipt = Path(self.pack['completion_receipt'])
        self.native = LegacyTerminalFixture.attach(self, home=self.home, identity=dict(
            caller_surface_uuid=T, target_surface_uuid=C, workspace_uuid=W, target_pane_uuid=P))
        self.native.pin.side_effect = lambda *args, **kwargs: copy.deepcopy(self.native.identity)
        self.native.session_id = S
        self.native.process_state['argv'][-1] = S
        self.native.native_root = self.home / '.codex/sessions'
        self.native.native_root.mkdir(parents=True)
        self.native.transcript.unlink()
        self.native.transcript = self.native.native_root / ('rollout-test-' + S + '.jsonl')
        self.native.append_record(dict(type='session_meta', payload=dict(id=S, source='cli')))
        self.payload = dict(hook_event_name='PostToolUse', session_id=S,
                            transcript_path=str(self.native.transcript), tool_name='exec_command')
        with patch.object(bridge, 'read_screen', side_effect=self.native.ready_screens(
                IDLE, self.native.draft(self.pack['completion_callback']), IDLE)):
            with self.assertRaises(bridge.DispatchUnconfirmed):
                bridge.submit_completion_callback(self.packfile)
        self.native.append_user(self.pack['completion_callback'])
        self.native.identity = dict(caller_surface_uuid=C, target_surface_uuid=T,
                                    workspace_uuid=W, caller_pane_uuid=P, target_pane_uuid=Q)
        with patch.dict(os.environ, {'CODEX_THREAD_ID': S}):
            reconcile_received(self.packfile, bridge, transcript=self.native.transcript, line=2)
        self.native.send.reset_mock()
        self.native.key.reset_mock()
        self.contexts = ExitStack()
        self.addCleanup(self.contexts.close)
        self.screen = self.contexts.enter_context(patch.object(bridge, 'read_screen',
            side_effect=AssertionError('observer must never inspect a composer')))
        self.active = self.home / 'active'
        (self.active / W).mkdir(parents=True)
        self.contexts.enter_context(patch.object(advisory.markers.stop_guard, 'ACTIVE_DIR', self.active))
        # Exercise the actual marker writer, including its version and native
        # participant shape. A handwritten v1 fixture hid the v2 regression.
        with patch.object(harness, '_ACTIVE_DIR', self.active), \
                patch.object(harness, '_workspace_key', return_value=W), \
                patch.object(harness, '_ARM_TTL_SECONDS', 1), \
                patch.object(harness, '_now', return_value='2026-01-01T00:00:00Z'), \
                patch.object(ready, 'state_root', return_value=self.home / 'state'):
            self.marker = harness.arm_task('task', self.root, 'surface:1', 'surface:2',
                supervisor_provider='codex', executor_provider='claude',
                supervisor_surface_uuid=C, executor_surface_uuid=T, workspace_uuid=W,
                native_participants=[dict(role='supervisor', provider='codex',
                    surface_uuid=C, native_session_id=S), dict(role='executor',
                    provider='claude', surface_uuid=T, native_session_id=str(uuid.uuid4()))])
        collaboration = self.marker['collaboration_id']
        self.markerfile = self.active / W / (collaboration + '.json')
        self.cursor = self.home / 'reminders'
        self.emissions = []
        self.inquiry = self.home / 'original-inquiry.json'
        self.write(self.inquiry, dict(state='WAITING_REPLY', ask_count=49,
                                     paused_tasks=[collaboration], unknown='preserve original'))

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value))

    def originals(self):
        return {str(p): (p.read_bytes(), p.stat().st_ino)
                for base in (self.root, self.active, self.report.parent)
                for p in base.rglob('*') if p.is_file()} | {
                    str(self.inquiry): (self.inquiry.read_bytes(), self.inquiry.stat().st_ino),
                    str(self.native.transcript): (self.native.transcript.read_bytes(),
                                                  self.native.transcript.stat().st_ino)}

    def discover(self, now=100., emit=None, payload=None, foreground=None):
        before = self.originals()
        with native_processes(), (patch('cmux_daemon_identity.foreground.read',
                return_value={'thread_id': foreground}) if foreground else nullcontext()):
            result = advisory.discover(payload or self.payload, now=now, state_root=self.cursor,
                                       emit=self.emissions.append if emit is None else emit)
        self.assertEqual(self.originals(), before)
        self.native.send.assert_not_called()
        self.native.key.assert_not_called()
        self.native.rpc.assert_not_called()
        self.screen.assert_not_called()
        return result

    def test_real_callback_reminds_original_root_after_ttl_without_mutating_inquiry(self):
        self.assertEqual(self.marker['marker_version'], 2)
        notices = self.discover()
        self.assertEqual(len(notices), 1)
        notice = notices[0]
        self.assertEqual(notice['kind'], 'CALLBACK_SETTLEMENT')
        self.assertEqual(notice['thread_id'], S)
        self.assertEqual(notice['receipt_sha256'], hashlib.sha256(self.receipt.read_bytes()).hexdigest())
        self.assertTrue(notice['callback_native_confirmed'])
        self.assertEqual(notice['ask_count_increment'], 0)
        self.assertFalse(notice['accepted'])
        self.assertFalse(any(k.startswith('_') for k in notice))
        self.assertEqual(self.discover(now=159.9), [])
        self.assertEqual(len(self.discover(now=160.)), 1)

    def test_explicit_legacy_version_is_observed_without_migration(self):
        self.markerfile.unlink()
        self.markerfile = self.active / (W + '.json')
        self.write(self.markerfile, dict(self.marker, marker_version=1))
        self.assertEqual(len(self.discover()), 1)

    def test_unknown_or_noninteger_marker_version_cannot_offer(self):
        for version in (0, 3, True, '2', None):
            with self.subTest(version=version):
                self.write(self.markerfile, dict(self.marker, marker_version=version))
                self.assertEqual(self.discover(), [])

    def test_missing_marker_never_manufactures_release_or_changes_inquiry(self):
        self.markerfile.unlink()
        self.assertEqual(self.discover(), [])

    def test_changed_pack_cannot_redirect_the_original_report_or_receipt(self):
        self.assertEqual(len(self.discover()), 1)
        original = self.packfile.read_bytes()
        other_report = self.home / 'other-report.md'
        other_report.write_bytes(self.report.read_bytes())
        other_receipt = self.home / 'other-receipt.json'
        other_receipt.write_bytes(self.receipt.read_bytes())
        for field, path in [('report', other_report), ('completion_receipt', other_receipt)]:
            with self.subTest(field=field):
                self.write(self.packfile, dict(self.pack, **{field: str(path)}))
                # Past the reminder interval, rejection must come from evidence
                # validation, not from an already-offered cursor.
                self.assertEqual(self.discover(now=160.), [])
                self.packfile.write_bytes(original)
        # Restoring bytes does not restore the frozen file identity/mtime.
        # A previously published native proof must remain invalid after edits.
        self.assertEqual(self.discover(now=160.), [])

    def test_child_inherited_root_and_changed_foreground_cannot_consume_cursor(self):
        for payload in (dict(self.payload, session_id=str(uuid.uuid4())),
                        dict(self.payload, subagent={'parent_thread_id': S})):
            self.assertEqual(self.discover(payload=payload), [])
        self.assertEqual(self.discover(foreground=T), [])
        wrong = dict(self.payload, transcript_path=str(self.home / 'not-native.jsonl'))
        self.assertEqual(self.discover(payload=wrong), [])
        self.assertFalse(self.cursor.exists())
        self.assertEqual(len(self.discover()), 1)

    def test_wrong_owner_workspace_session_and_duplicate_owner_are_refused(self):
        variations = [dict(workspace_uuid=str(uuid.uuid4())), dict(task_id='other'),
                      dict(collaboration_id=str(uuid.uuid4()))]
        for key, value in [('surface_uuid', T), ('native_session_id', str(uuid.uuid4()))]:
            peers = copy.deepcopy(self.marker['participants'])
            peers[0][key] = value
            variations.append(dict(participants=peers))
        variations.append(dict(participants=self.marker['participants'] + [self.marker['participants'][0]]))
        for change in variations:
            with self.subTest(change=change):
                self.write(self.markerfile, dict(self.marker, **change))
                self.assertEqual(self.discover(), [])

    def test_receipt_report_and_original_fence_tampering_cannot_offer(self):
        receipt = json.loads(self.receipt.read_bytes())
        attempt_path = Path(receipt['attempt'])
        original = {p: p.read_bytes() for p in (self.receipt, attempt_path, self.report,
                                               self.native.transcript)}
        changes = [(self.receipt, json.dumps(dict(receipt, confirmed=False)).encode()),
                   (self.report, b'changed report'),
                   (self.native.transcript, original[self.native.transcript].replace(b'"user"', b'"assistant"'))]
        attempt = json.loads(original[attempt_path])
        for e in attempt['events']:
            if e['phase'] == 'PASTE_INTENT':
                e.pop('native_paste_fence')
        changes.append((attempt_path, json.dumps(attempt).encode()))
        for path, raw in changes:
            with self.subTest(path=path):
                path.write_bytes(raw)
                self.assertEqual(self.discover(), [])
                path.write_bytes(original[path])

    def test_output_failure_does_not_advance_reminder_or_scan_cursor(self):
        def fail(_notices):
            raise BrokenPipeError('closed output')
        self.assertEqual(self.discover(emit=fail), [])
        self.assertEqual(list(self.cursor.glob('*.json')), [])
        self.assertEqual(len(self.discover()), 1)

    def test_unrelated_prefix_does_not_starve_later_markers(self):
        for index in range(advisory.MAX_MARKERS):
            self.write(self.active / W / (f'0000-{index:03}.json'), {'unrelated': True})
        self.assertEqual(self.discover(), [])
        self.assertEqual(len(self.discover()), 1)

    def test_missing_receipt_and_symlink_marker_are_not_delivery(self):
        raw = self.receipt.read_bytes()
        self.receipt.unlink()
        self.assertEqual(self.discover(), [])
        self.receipt.write_bytes(raw)
        original = self.home / 'original-marker.json'
        self.markerfile.rename(original)
        self.markerfile.symlink_to(original)
        self.assertEqual(self.discover(), [])

    def test_real_wrapper_emits_valid_context_despite_telemetry_failure(self):
        out = io.StringIO()
        with native_processes(), patch.object(audit, 'record', side_effect=OSError('audit full')), \
                patch('sys.stdin', io.StringIO(json.dumps(self.payload))), \
                patch('sys.argv', ['advisory', '--hook', 'callback_settlement_advisory.py']), \
                redirect_stdout(out):
            self.assertEqual(workflow.main(), 0)
        value = json.loads(out.getvalue())
        self.assertEqual(set(value), {'hookSpecificOutput'})
        self.assertEqual(value['hookSpecificOutput']['hookEventName'], 'PostToolUse')
        self.assertIn('CALLBACK_SETTLEMENT', value['hookSpecificOutput']['additionalContext'])
        self.native.send.assert_not_called()
        self.native.key.assert_not_called()

    def test_stop_and_runtime_deadline_never_block_or_consume(self):
        self.assertEqual(self.discover(payload=dict(self.payload, hook_event_name='Stop')), [])
        # Only the deadline I/O boundary is interrupted; no validator is replaced
        # by a successful mock or by synthetic native evidence.
        with patch.object(advisory.markers, '_workspace_paths', side_effect=ready.WaitBudgetExpired):
            self.assertEqual(self.discover(), [])
        self.assertEqual(len(self.discover()), 1)


if __name__ == '__main__':
    unittest.main()

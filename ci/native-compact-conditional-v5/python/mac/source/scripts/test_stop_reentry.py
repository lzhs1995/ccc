"""Real stdin/exit-code tests; no native session or transport is invoked."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock
import offline_test_hook
import cmux_bridge as BRIDGE
from native_test_support import NativeFixture, native_hook_command

HOOK = Path(os.environ.get('STOP_GUARD_UNDER_TEST', Path(__file__).with_name('cmux_consensus_stop_guard.py')))


class StopReentryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='stop-reentry-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.native_state = None
        self.workspace = 'stop-reentry-test-' + uuid.uuid4().hex
        self.surface = uuid.uuid4().hex
        self.active = self.root / 'active'
        self.marker = self.active / (self.workspace + '.json')
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.marker.unlink, missing_ok=True)
        self.marker.write_text(json.dumps(dict(task_id='offline-test', artifact_root=str(self.root), participants=[dict(role='executor', surface_uuid=self.surface)])))
        self.report = self.root / 'report.md'
        self.report.write_text('Offline report.\n')
        self.pack = dict(draft=False, task_id='offline-test', completion_nonce='test-nonce', completion_callback='DONE|offline-test', callback_target='supervisor', report=str(self.report), completion_receipt=str(self.root / 'receipt.json'))
        (self.root / 'task-pack.json').write_text(json.dumps(self.pack))
        self.env = dict(os.environ, CMUX_WORKSPACE_ID=self.workspace, CMUX_SURFACE_ID=self.surface, PYTHONDONTWRITEBYTECODE='1')

    def call(self, payload, check_file=False):
        data = dict(hook_event_name='Stop', last_assistant_message='Report written; callback unconfirmed.')
        data.update(payload)
        command = (native_hook_command(HOOK, self.active, self.native_state)
                   if self.native_state else offline_test_hook.command(HOOK, self.active))
        if check_file:
            path = self.root / 'input.json'
            path.write_text(json.dumps(data))
            command += ['--check-file', str(path)]
        return subprocess.run(command, input=json.dumps(data), text=True, capture_output=True, env=self.env, timeout=5)

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()} | {str(self.marker): self.marker.read_bytes()}

    def test_first_stop_missing_callback_still_blocks(self):
        r = self.call(dict(stop_hook_active=False))
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('completion callback receipt missing', r.stderr)

    def test_legacy_first_stop_missing_flag_still_blocks(self):
        self.assertEqual(self.call({}).returncode, 2)

    def test_stop_reentry_returns_success_without_mutating_evidence(self):
        before = self.snapshot()
        r = self.call(dict(stop_hook_active=True))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((r.stdout, r.stderr), ('', ''))
        self.assertEqual(self.snapshot(), before)

    def test_subagent_stop_reentry(self):
        r = self.call(dict(stop_hook_active=True, hook_event_name='SubagentStop'))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_wrong_types_cannot_claim_reentry(self):
        for flag in ('true', 'false', 1, 0, None, [], {}):
            with self.subTest(flag=flag):
                self.assertEqual(self.call(dict(stop_hook_active=flag)).returncode, 2)

    def test_other_events_do_not_get_reentry_exemption(self):
        for event in ('PreToolUse', '', None):
            with self.subTest(event=event):
                self.assertEqual(self.call(dict(stop_hook_active=True, hook_event_name=event)).returncode, 2)

    def test_next_normal_turn_is_checked_again(self):
        self.assertEqual(self.call(dict(stop_hook_active=True)).returncode, 0)
        self.assertEqual(self.call(dict(stop_hook_active=False)).returncode, 2)

    def test_valid_callback_passes_but_report_drift_blocks(self):
        # 成功回执来自真实 public callback、原 journal 与同一 JSONL user 记录。
        self.pack.update(executor_uuid=self.surface, callback_target='surface:1',
                         required_skill=str(BRIDGE.COLLABORATION_SKILL_PATH),
                         completion_callback=f'DONE|offline-test|test-nonce|REPORT={self.report}',
                         completion_delivery=dict(transport='cmux_bridge.submit_completion_callback',
                                                  require_confirmed=True))
        pack_path = self.root / 'task-pack.json'
        pack_path.write_text(json.dumps(self.pack))
        native = NativeFixture.attach(self, home=self.root / 'home', provider='claude',
            identity=dict(workspace_uuid=self.workspace, caller_surface_uuid=self.surface,
                          target_surface_uuid='supervisor-uuid', target_pane_uuid='pane-uuid'))
        callback = self.pack['completion_callback']
        idle = native.draft('', provider='claude')
        with mock.patch.object(BRIDGE, 'read_screen', side_effect=native.ready_screens(
                idle, native.draft(callback, provider='claude'), idle)):
            native.key.side_effect = native.receipt_on_key(callback)
            receipt = BRIDGE.submit_completion_callback(pack_path)
        self.assertTrue(receipt['confirmed'])
        native.send.assert_called_once()
        native.key.assert_called_once()
        self.native_state = native.export_state(self.root / 'native-state.json')
        before = self.snapshot()
        result = self.call(dict(stop_hook_active=False))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.snapshot())
        # 子进程重新验证原 proof；user 记录被截断时不能沿用 confirmed 字段。
        transcript = native.transcript.read_bytes()
        native.transcript.write_bytes(transcript.splitlines(keepends=True)[0])
        self.assertEqual(self.call(dict(stop_hook_active=False)).returncode, 2)
        native.transcript.write_bytes(transcript)
        self.report.write_text('Changed report\n')
        self.assertEqual(self.call(dict(stop_hook_active=False)).returncode, 2)

    def test_false_consensus_still_blocks_first_stop(self):
        (self.root / 'task-pack.json').unlink()
        self.assertEqual(self.call(dict(stop_hook_active=False, last_assistant_message='consensus-validation PASS')).returncode, 2)

    def test_unarmed_passes(self):
        self.marker.unlink()
        self.assertEqual(self.call(dict(stop_hook_active=False)).returncode, 0)

    def test_check_file_uses_same_reentry_rule(self):
        r = self.call(dict(stop_hook_active=True), check_file=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('ALLOW:', r.stdout)

    def earlier_other_task(self):
        """A different executor's v2 marker precedes this task's v1 marker."""
        directory = self.marker.parent / self.workspace
        directory.mkdir()
        self.addCleanup(directory.rmdir)
        path = directory / 'first.json'
        self.addCleanup(path.unlink)
        other_root = self.root / 'other-task'
        other_root.mkdir()
        path.write_text(json.dumps(dict(
            task_id='other-task', artifact_root=str(other_root),
            participants=[dict(role='executor', surface_uuid='other-executor')],
        )))
        for filename in ('validation.json', 'consensus-validation.json'):
            (other_root / filename).write_text(json.dumps(dict(status='PASS', task_id='other-task')))
        return path

    def test_callback_failure_names_its_task_not_first_marker(self):
        other = self.earlier_other_task()
        before = self.snapshot() | {str(other): other.read_bytes()}
        result = self.call({})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('completion callback receipt missing', result.stderr)
        self.assertIn(f'(armed marker: task=offline-test root={self.root})', result.stderr)
        self.assertNotIn('(armed marker: task=other-task', result.stderr)
        self.assertEqual(before, self.snapshot() | {str(other): other.read_bytes()})

    def test_contradiction_names_the_failing_task(self):
        self.earlier_other_task()
        (self.root / 'task-pack.json').unlink()
        result = self.call(dict(last_assistant_message='consensus-validation PASS'))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f'(armed marker: task=offline-test root={self.root})', result.stderr)

    def test_missing_summary_names_the_failing_task(self):
        self.earlier_other_task()
        (self.root / 'task-pack.json').unlink()
        result = self.call(dict(last_assistant_message='all rounds are recorded'))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('validation.json missing/not PASS', result.stderr)
        self.assertIn(f'(armed marker: task=offline-test root={self.root})', result.stderr)

    def test_diagnostic_does_not_rescan_markers_after_verdict(self):
        spec = importlib.util.spec_from_file_location('stop_guard_marker_test', HOOK)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        marker = json.loads(self.marker.read_text())
        payload = dict(surface_id=self.surface, final_message='Report pending')
        with offline_test_hook.hook_environment(self.active), \
                mock.patch.object(guard, 'ACTIVE_DIR', self.active), \
                mock.patch.dict(os.environ, {'CMUX_SURFACE_ID': self.surface,
                                            'CMUX_WORKSPACE_ID': self.workspace}), \
                mock.patch.object(guard, '_active_markers', side_effect=[[marker], []]) as read, \
                mock.patch.object(sys, 'argv', [str(HOOK)]), \
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, 'stderr', io.StringIO()) as stderr:
            self.assertEqual(guard.main(), 2)
            self.assertEqual(read.call_count, 1)
            self.assertIn(f'(armed marker: task=offline-test root={self.root})', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()

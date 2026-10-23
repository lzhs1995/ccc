"""Real hook entrypoint tests using temporary markers and the original journal."""
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

import executor_closeout as closeout
import offline_test_hook
import cmux_bridge as BRIDGE
from native_test_support import NativeFixture, native_hook_command

# The user's exact sentence, spelled independently of the module under test.
COMPLETION = '完成，建议检查 usage: /context'
WAITING_STOP = {
    'continue': False,
    'suppressOutput': True,
    'stopReason': (
        'WAITING_SUPERVISOR: report frozen; original callback returned. '
        'Await supervisor receipt reconciliation and acceptance. '
        'Delivery and task completion remain unconfirmed.'
    ),
}


class CloseoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='closeout-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.native_state = None
        self.workspace, self.surface, self.supervisor = [str(uuid.uuid4()) for _ in range(3)]
        self.marker = dict(task_id='test-task', workspace_uuid=self.workspace,
                           artifact_root=str(self.root), participants=[
                               dict(role='supervisor', surface_ref='surface:1',
                                    surface_uuid=self.supervisor),
                               dict(role='executor', surface_uuid=self.surface)])
        self.active = self.root / 'active'
        self.marker_path = self.active / (self.workspace + '.json')
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.write(self.marker_path, self.marker)
        self.addCleanup(self.marker_path.unlink, missing_ok=True)
        self.report = self.root / 'report.md'
        self.report.write_text('Bounded findings; known gaps remain.\n')
        self.pack_path = self.root / 'task-pack.json'
        self.receipt = self.root / 'receipt.json'
        self.pack = dict(draft=False, task_id='test-task', executor_uuid=self.surface,
                         completion_nonce='nonce', callback_target='surface:1',
                         completion_callback='DONE|test-task|nonce', report=str(self.report),
                         completion_receipt=str(self.receipt))
        self.write(self.pack_path, self.pack)
        self.journal = self.root / 'receipt-attempts'
        self.journal.mkdir()
        self.lock = self.journal / 'delivery.lock'
        self.lock.touch()
        self.attempt_path = self.journal / 'attempt-0001.json'
        self.attempt = dict(
            binding={**{k: self.pack[k] for k in ('task_id', 'completion_nonce',
                     'completion_callback', 'callback_target', 'report')},
                     'task_pack_sha256': self.sha(self.pack_path),
                     'report_sha256': self.sha(self.report), 'report_bytes': self.report.stat().st_size,
                     'identity': dict(workspace_uuid=self.workspace, caller_surface_uuid=self.surface,
                                      target_surface_uuid=self.supervisor, target_pane_uuid='pane-uuid')},
            phase='POST_ENTER_OBSERVATION', started_at_epoch=1.0, ended_at_epoch=3.0,
            events=[dict(phase='PASTE_INTENT', at_epoch=1.5),
                    dict(phase='POST_ENTER_OBSERVATION', at_epoch=2.0)],
            error='delivery detector unconfirmed', delivery_state='QUEUED')
        self.write(self.attempt_path, self.attempt)
        self.env = dict(os.environ, CMUX_WORKSPACE_ID=self.workspace, CMUX_SURFACE_ID=self.surface)
        self.line = closeout.handoff_line(dict(task_id='test-task', report=str(self.report)))

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def sha(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def evidence(self):
        return closeout.terminal_report(self.marker, self.workspace, self.surface)

    def hook(self, name, event, final=None, env=None, reentry=False, tool_input=None):
        data = dict(hook_event_name=event, tool_name='Bash',
                    tool_input=tool_input if tool_input is not None else dict(command='touch MUST_NOT_RUN'),
                    last_assistant_message=final or self.line, stop_hook_active=reentry)
        script = Path(__file__).with_name(name)
        command = (native_hook_command(script, self.active, self.native_state)
                   if self.native_state else offline_test_hook.command(script, self.active))
        return subprocess.run(command,
                              input=json.dumps(data), text=True, capture_output=True,
                              env=env or self.env, timeout=5)

    def pre(self, **kwargs):
        return self.hook('cmux_executor_closeout_guard.py', 'PreToolUse', **kwargs)

    def stop(self, **kwargs):
        return self.hook('cmux_consensus_stop_guard.py', 'Stop', **kwargs)

    def assert_waiting_stop(self, result):
        # 经真实 stdin/main 出口验证协议，不把 exit 0 当作暂停原生 Goal 的证明。
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertEqual(json.loads(result.stdout), WAITING_STOP)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertNotIn(COMPLETION, result.stdout)

    def test_terminal_unknown_seals_tools_allows_honest_stop_without_writing(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertIsNotNone(self.evidence())
        self.assertEqual(self.pre().returncode, 2)
        self.assertIn('EXECUTOR_CLOSEOUT', self.pre().stderr)
        self.assert_waiting_stop(self.stop())
        self.assertFalse(self.receipt.exists())
        self.assertTrue(self.marker_path.exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_unknown_cannot_claim_delivery_or_consensus(self):
        for text in ('DONE|test-task|nonce', 'callback confirmed', self.line + ' consensus-validation PASS'):
            with self.subTest(text=text):
                self.assertEqual(self.stop(final=text).returncode, 2)

    def test_no_input_return_can_handoff_without_mandatory_retry(self):
        self.attempt.update(phase='NO_INPUT', events=[])
        self.write(self.attempt_path, self.attempt)
        self.assert_waiting_stop(self.stop())
        self.assertEqual(self.pre().returncode, 2)

    def no_input_command(self, release_name='original-release'):
        # A different complete controller remains bound by the frozen pack.
        original = self.root / release_name
        (original / 'scripts').mkdir(parents=True)
        (original / 'SKILL.md').write_text('Original task-bound skill.\n')
        controller = original / 'scripts/cmux_bridge.py'
        controller.write_text('raise RuntimeError("HOOK MUST NOT EXECUTE TOOL")\n')
        argv = [sys.executable, '-B', str(controller),
                'submit-completion-callback', '--task-pack', str(self.pack_path)]
        self.pack.update(required_skill=str(original / 'SKILL.md'),
                         callback_command=shlex.join(argv))
        self.write(self.pack_path, self.pack)
        self.attempt.update(phase='NO_INPUT', events=[])
        self.attempt['binding']['task_pack_sha256'] = self.sha(self.pack_path)
        self.write(self.attempt_path, self.attempt)
        return shlex.join(['rtk', 'proxy', *argv])

    def test_no_input_successor_exact_original_cli_allowed_without_mutating_evidence(self):
        command = self.no_input_command()
        before = self.snapshot()
        result = self.pre(tool_input={'command': command})
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertEqual(before, self.snapshot())
        self.assert_waiting_stop(self.stop())
        self.assertEqual(self.pre().returncode, 2)

    def test_no_input_successor_accepts_equivalent_literal_unicode_quoting(self):
        quoted = self.no_input_command(release_name='原始版本')
        argv = shlex.split(quoted)
        variants = [' '.join(argv), ' '.join("'" + arg + "'" for arg in argv),
                    ' '.join('"' + arg + '"' for arg in argv),
                    quoted.replace('rtk proxy ', "'rt'k  \"proxy\" ", 1)]
        before = self.snapshot()
        for command in variants:
            with self.subTest(command=command):
                result = self.pre(tool_input={'command': command, 'timeout': 600000})
                self.assertEqual((result.returncode, result.stderr), (0, ''))
                self.assertEqual(before, self.snapshot())

    def test_no_input_successor_missing_or_equivalent_callback_metadata(self):
        command = self.no_input_command(release_name='原始版本')
        original_argv = shlex.split(command)[2:]
        for supplied in (False, True):
            with self.subTest(metadata_present=supplied):
                self.pack.pop('callback_command', None)
                if supplied:
                    self.pack['callback_command'] = ' '.join(original_argv)
                self.write(self.pack_path, self.pack)
                self.attempt['binding']['task_pack_sha256'] = self.sha(self.pack_path)
                self.write(self.attempt_path, self.attempt)
                result = self.pre(tool_input={'command': ' '.join(shlex.split(command))})
                self.assertEqual((result.returncode, result.stderr), (0, ''))

    def test_no_input_successor_shell_expansion_is_not_literal_argv(self):
        for release_name in ('原始$HOME', '原始`true`', '原始*', '原始[abc]', '原始{a,b}'):
            with self.subTest(release_name=release_name):
                command = self.no_input_command(release_name=release_name)
                self.assertEqual(self.pre(tool_input={'command': command}).returncode, 0)
                raw = ' '.join(shlex.split(command))
                # shlex alone sees identical words; the shell would expand them.
                self.assertEqual(shlex.split(raw), shlex.split(command))
                self.assertEqual(self.pre(tool_input={'command': raw}).returncode, 2)
                if '$' in release_name or '`' in release_name:
                    double = ' '.join('"' + arg + '"' for arg in shlex.split(command))
                    self.assertEqual(self.pre(tool_input={'command': double}).returncode, 2)

    def test_no_input_successor_rejects_comment_and_empty_control_tail(self):
        command = self.no_input_command(release_name='原始版本')
        for suffix in (';', '&', '\n', '\r', ' # comment', ' || true', ' > receipt.json',
                       ' --task-pack ' + shlex.quote(str(self.pack_path))):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.pre(tool_input={'command': command + suffix}).returncode, 2)
        for bad in (None, False, 0, [], {}, '"' + command,
                    command.replace(sys.executable, '/usr/bin/python3', 1)):
            with self.subTest(command=bad):
                self.assertEqual(self.pre(tool_input={'command': bad}).returncode, 2)

    def test_no_input_successor_rejects_invalid_callback_metadata(self):
        command = self.no_input_command()
        original = self.pack['callback_command']
        for bad in (None, False, [], original + ';', original + ' # note',
                    original.replace('original-release', 'other-release')):
            with self.subTest(metadata=bad):
                self.pack['callback_command'] = bad
                self.write(self.pack_path, self.pack)
                self.attempt['binding']['task_pack_sha256'] = self.sha(self.pack_path)
                self.write(self.attempt_path, self.attempt)
                self.assertEqual(self.pre(tool_input={'command': command}).returncode, 2)

    def test_no_input_successor_rejects_shell_wrappers_and_other_targets(self):
        command = self.no_input_command()
        for bad in (command + ' &', command + '; true', 'env ' + command,
                    'sh -c ' + shlex.quote(command), command.replace(str(self.pack_path), '$PACK'),
                    command.replace(str(self.pack_path), str(self.root / 'other.json')),
                    command.replace('original-release', 'current-release'),
                    command + ' --reconcile-only'):
            with self.subTest(command=bad):
                self.assertEqual(self.pre(tool_input={'command': bad}).returncode, 2)
        self.assertEqual(self.pre(tool_input={'command': command, 'run_in_background': True}).returncode, 2)

    def test_no_input_successor_rejects_receipt_and_legacy_pending_even_broken_symlinks(self):
        command = self.no_input_command()
        for target in (self.receipt, Path(str(self.receipt) + '.pending.json')):
            for broken in (False, True):
                with self.subTest(path=target.name, broken=broken):
                    if broken:
                        target.symlink_to(self.root / 'missing')
                    else:
                        target.write_text('{}')
                    try:
                        self.assertIsNotNone(self.evidence())
                        self.assertEqual(self.pre(tool_input={'command': command}).returncode, 2)
                    finally:
                        target.unlink()

    def test_no_input_successor_second_attempt_exhausts_budget(self):
        command = self.no_input_command()
        self.write(self.journal / 'attempt-0002.json', self.attempt)
        self.assertIsNotNone(self.evidence())
        self.assertEqual(self.pre(tool_input={'command': command}).returncode, 2)

    def test_no_input_successor_entered_queued_unknown_never_repastes(self):
        command = self.no_input_command()
        for phase in ('PASTE_INTENT', 'POST_ENTER_OBSERVATION', 'POST_QUEUE_TAB_OBSERVATION'):
            with self.subTest(phase=phase):
                self.attempt.update(phase=phase, events=[{'phase': phase, 'at_epoch': 2.0}])
                self.write(self.attempt_path, self.attempt)
                self.assertIsNotNone(self.evidence())
                self.assertEqual(self.pre(tool_input={'command': command}).returncode, 2)

    def test_no_input_successor_rechecks_original_evidence_pins(self):
        from cmux_callback_no_input_successor import allowed
        command = self.no_input_command()
        evidence = self.evidence()
        payload = {'tool_name': 'Bash', 'tool_input': {'command': command}}
        for target in (self.report, self.pack_path, self.attempt_path):
            raw = target.read_bytes()
            try:
                target.write_bytes(raw + b' ')
                self.assertFalse(allowed(payload, self.marker, evidence))
            finally:
                target.write_bytes(raw)
        for bad in ({**payload, 'tool_name': 'Write'},
                    {**payload, 'tool_input': None}):
            self.assertFalse(allowed(bad, self.marker, evidence))

    def test_no_input_successor_missing_original_controller_does_not_choose_new_release(self):
        command = self.no_input_command()
        (self.root / 'original-release/scripts/cmux_bridge.py').unlink()
        self.assertIsNotNone(self.evidence())
        self.assertEqual(self.pre(tool_input={'command': command}).returncode, 2)

    def test_confirmed_return_still_prevents_new_work(self):
        self.confirmed_callback()
        before = self.snapshot()
        self.assertEqual(self.pre().returncode, 2)
        result = self.stop(final=self.pack['completion_callback'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_inflight_lock_never_settled_by_old_end_timestamp(self):
        with self.lock.open('rb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIsNone(self.evidence())
            self.assertEqual(self.pre().returncode, 0)
            self.assertEqual(self.stop().returncode, 2)
            reentry = self.stop(reentry=True)
            self.assertEqual((reentry.returncode, reentry.stdout, reentry.stderr), (0, '', ''))
        self.assertIsNotNone(self.evidence())
        self.assert_waiting_stop(self.stop(reentry=True))

    def test_confirmed_receipt_does_not_accept_changed_or_unpinned_pack(self):
        receipt = self.confirmed_callback()
        original_pack = self.pack_path.read_bytes()
        callback = self.pack['completion_callback']
        result = self.stop(final=callback)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.pack['scope'] = 'changed without a new callback'
        self.write(self.pack_path, self.pack)
        self.assertEqual(self.pre().returncode, 0)
        self.assertEqual(self.stop(final=callback).returncode, 2)
        # 恢复原 pack 后单独验证缺 pin，避免被上一项 pack 漂移掩盖。
        self.pack_path.write_bytes(original_pack)
        self.assertEqual(self.stop(final=callback).returncode, 0)
        del receipt['task_pack_sha256']
        self.write(self.receipt, receipt)
        self.assertEqual(self.stop(final=callback).returncode, 2)

    def confirmed_callback(self):
        # 仅成功用例用真实发送替换 setUp 的未确认形状夹具；不改写真实 proof。
        self.attempt_path.unlink()
        nonce = 'closeout-native-001'
        self.pack.update(completion_nonce=nonce,
                         completion_callback=f'DONE|test-task|{nonce}|REPORT={self.report}',
                         required_skill=str(BRIDGE.COLLABORATION_SKILL_PATH),
                         completion_delivery=dict(transport='cmux_bridge.submit_completion_callback',
                                                  require_confirmed=True))
        self.write(self.pack_path, self.pack)
        native = NativeFixture.attach(self, home=self.root / 'home', provider='claude',
            identity=dict(workspace_uuid=self.workspace, caller_surface_uuid=self.surface,
                          target_surface_uuid=self.supervisor, target_pane_uuid='pane-uuid'))
        callback = self.pack['completion_callback']
        idle = native.draft('', provider='claude')
        with mock.patch.object(BRIDGE, 'read_screen', side_effect=native.ready_screens(
                idle, native.draft(callback, provider='claude'), idle)):
            native.key.side_effect = native.receipt_on_key(callback)
            receipt = BRIDGE.submit_completion_callback(self.pack_path)
        self.assertTrue(receipt['confirmed'])
        native.send.assert_called_once()
        native.key.assert_called_once()
        self.native_state = native.export_state(self.root / 'native-state.json')
        return receipt

    def test_missing_and_nonterminal_evidence(self):
        original = copy.deepcopy(self.attempt)
        cases = [dict(phase='PREPARED'), dict(ended_at_epoch=None), dict(ended_at_epoch=True),
                 dict(ended_at_epoch=float('nan')), dict(ended_at_epoch=1.2),
                 dict(phase='ENTER_INTENT'), dict(error=''), dict(events=None)]
        for delta in cases:
            with self.subTest(delta=delta):
                self.write(self.attempt_path, dict(original, **delta))
                self.assertIsNone(self.evidence())
                self.assertEqual(self.pre().returncode, 0)
                self.assertEqual(self.stop().returncode, 2)
        self.attempt_path.unlink()
        self.assertIsNone(self.evidence())

    def test_changed_report_and_pack_do_not_close(self):
        self.report.write_text('Changed after callback')
        self.assertIsNone(self.evidence())
        self.assertEqual(self.stop().returncode, 2)
        self.report.write_text('Bounded findings; known gaps remain.\n')
        self.pack['new_scope'] = 'unbound'
        self.write(self.pack_path, self.pack)
        self.assertIsNone(self.evidence())

    def test_wrong_binding_identity_and_task(self):
        for key in ('task_id', 'completion_nonce', 'completion_callback', 'callback_target',
                    'report', 'report_sha256', 'report_bytes', 'task_pack_sha256'):
            with self.subTest(key=key):
                bad = copy.deepcopy(self.attempt)
                bad['binding'][key] = 'wrong'
                self.write(self.attempt_path, bad)
                self.assertIsNone(self.evidence())
        for key in ('workspace_uuid', 'caller_surface_uuid', 'target_surface_uuid'):
            bad = copy.deepcopy(self.attempt)
            bad['binding']['identity'][key] = 'wrong'
            self.write(self.attempt_path, bad)
            self.assertIsNone(self.evidence())

    def test_supervisor_and_other_executor_are_not_sealed(self):
        for surface in (self.supervisor, 'other'):
            env = dict(self.env, CMUX_SURFACE_ID=surface)
            self.assertEqual(self.pre(env=env).returncode, 0)
            result = self.stop(env=env)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_stale_and_disarmed_markers_do_not_seal_tools(self):
        self.marker.update(armed_at='2000-01-01T00:00:00+00:00', ttl_seconds=1)
        self.write(self.marker_path, self.marker)
        self.assertEqual(self.pre().returncode, 0)
        stale = self.stop()
        self.assertEqual((stale.returncode, stale.stdout, stale.stderr), (0, '', ''))
        self.marker_path.unlink()
        self.assertEqual(self.pre().returncode, 0)
        disarmed = self.stop()
        self.assertEqual((disarmed.returncode, disarmed.stdout, disarmed.stderr), (0, '', ''))

    def test_latest_attempt_must_be_terminal(self):
        newer = dict(self.attempt, phase='PREPARED', ended_at_epoch=None)
        self.write(self.journal / 'attempt-0002.json', newer)
        self.assertIsNone(self.evidence())

    def test_screen_hash_and_resume_timestamp_validated(self):
        bad = copy.deepcopy(self.attempt)
        bad['events'][-1].update(screen='queued', screen_sha256='bad')
        self.write(self.attempt_path, bad)
        self.assertIsNone(self.evidence())
        bad['events'][-1]['screen_sha256'] = hashlib.sha256(b'queued').hexdigest()[:16]
        self.write(self.attempt_path, bad)
        self.assertIsNotNone(self.evidence())
        bad['events'][-1]['at_epoch'] = 4.0  # resumed after old ended_at
        self.write(self.attempt_path, bad)
        self.assertIsNone(self.evidence())

    def test_missing_lock_does_not_create_it(self):
        self.lock.unlink()
        self.assertIsNone(self.evidence())
        self.assertFalse(self.lock.exists())

    def test_wrong_hook_event_does_not_deny(self):
        result = self.hook('cmux_executor_closeout_guard.py', 'Stop')
        self.assertEqual(result.returncode, 0)

    def snapshot(self):
        return {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def later_task(self, **changes):
        # A fresh v2 marker the same supervisor armed after the original attempt.
        armed = datetime.now(timezone.utc) - timedelta(seconds=5)
        marker = dict(task_id='next-task', workspace_uuid=self.workspace,
                      artifact_root=str(self.root / 'next'), ttl_seconds=21600,
                      armed_at=armed.isoformat(),
                      participants=copy.deepcopy(self.marker['participants']))
        marker.update(changes)
        # None drops the field: a missing field, not a JSON null.
        marker = {k: v for k, v in marker.items() if v is not None}
        path = self.active / self.workspace / 'later.json'
        path.parent.mkdir(exist_ok=True)
        self.write(path, marker)
        return path

    def test_completion_sentence_is_users_exact_text(self):
        self.assertEqual(closeout.COMPLETION_SENTENCE, COMPLETION)

    def test_first_stop_accepts_ordinary_honest_explanation_without_exact_template(self):
        before = self.snapshot()
        for final in (self.line, self.line + '\n' + COMPLETION, '\n' + self.line + '\n',
                      '报告已经写完，回调仍未确认送达，等待主管核收。',
                      'Enter only added a newline. Delivery is unconfirmed; the report is frozen.',
                      'The callback is not confirmed. Please review the saved report.',
                      'Summary\n' + self.line + '\n\n' + COMPLETION,
                      'Work incomplete; BLOCKED on supervisor review'):
            with self.subTest(final=final):
                result = self.stop(final=final)
                self.assert_waiting_stop(result)
        confirmed = self.line.replace('CALLBACK_UNCONFIRMED', 'CALLBACK_CONFIRMED')
        rejected = [confirmed, confirmed + '\n' + COMPLETION,
                    'DONE|test-task|nonce\n' + COMPLETION, 'NATIVE_RECEIVED',
                    '回调已送达', '尚未验收。CALLBACK_CONFIRMED',
                    'Earlier work is blocked. The callback is delivered.']
        for final in rejected:
            with self.subTest(final=final):
                result = self.stop(final=final)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('EXECUTOR_CLOSEOUT', result.stderr)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(sorted(self.journal.glob('attempt-*.json')), [self.attempt_path])
        self.assertFalse(self.receipt.exists())

    def test_hint_is_optional_and_does_not_block_ordinary_corrections(self):
        pre = self.pre()
        self.assertEqual(pre.returncode, 2)
        status = [line for line in pre.stderr.splitlines() if line.startswith('STATUS:')]
        self.assertEqual(status, [self.line])
        quoted = re.findall(r'may you append "([^"]+)"', pre.stderr)
        self.assertEqual(quoted, [COMPLETION])
        for final in (status[0], status[0] + '\n' + quoted[0]):
            with self.subTest(final=final):
                self.assertEqual(self.stop(final=final).returncode, 0)
        self.assert_waiting_stop(self.stop(final='Still waiting; correcting my earlier explanation.'))
        blocked = self.stop(final='callback confirmed')
        self.assertEqual(blocked.returncode, 2)
        instructions = closeout.closeout_instructions(self.evidence())
        self.assertIn(instructions, pre.stderr)
        self.assertIn(instructions, blocked.stderr)
        self.assertNotIn('Inspect its submission', blocked.stderr)
        self.assertNotIn('Produce real evidence', blocked.stderr)

    def test_hook_never_supplies_completion_for_the_executor(self):
        # hook 仅返回控制协议或拒绝提示，不代写 executor 的完成句。
        outputs = [self.pre().stderr]
        for final in ('Work incomplete; BLOCKED on review', self.line + '\nBLOCKED',
                      'BLOCKED\n' + COMPLETION):
            with self.subTest(final=final):
                result = self.stop(final=final)
                self.assert_waiting_stop(result)
                outputs.append(result.stdout)
        for text in outputs:
            self.assertNotIn(COMPLETION, [line.strip() for line in text.splitlines()])
            self.assertFalse(text.rstrip().endswith('/context'))
        allowed = self.stop(final=self.line)
        self.assert_waiting_stop(allowed)

    def test_first_stop_passes_without_reentry_exception_and_reentry_kept(self):
        complete = self.line + '\n' + COMPLETION
        self.assert_waiting_stop(self.stop(final=complete, reentry=False))
        self.assert_waiting_stop(self.stop(final='still waiting', reentry=False))
        for final in ('still working', complete):
            with self.subTest(final=final):
                result = self.stop(final=final, reentry=True)
                self.assert_waiting_stop(result)
        self.assertFalse(self.receipt.exists())

    def test_subagent_stop_does_not_emit_native_goal_waiting_protocol(self):
        before = self.snapshot()
        for reentry in (False, True):
            with self.subTest(reentry=reentry):
                result = self.hook('cmux_consensus_stop_guard.py', 'SubagentStop',
                                   reentry=reentry)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertEqual(before, self.snapshot())

    def test_reentry_does_not_emit_waiting_for_wrong_binding(self):
        for key in ('task_id', 'completion_nonce', 'completion_callback', 'callback_target',
                    'report', 'report_sha256', 'report_bytes', 'task_pack_sha256'):
            with self.subTest(key=key):
                bad = copy.deepcopy(self.attempt)
                bad['binding'][key] = 'wrong'
                self.write(self.attempt_path, bad)
                before = self.snapshot()
                result = self.stop(reentry=True)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
                self.assertEqual(before, self.snapshot())
        for key in ('workspace_uuid', 'caller_surface_uuid', 'target_surface_uuid'):
            with self.subTest(identity=key):
                bad = copy.deepcopy(self.attempt)
                bad['binding']['identity'][key] = 'wrong'
                self.write(self.attempt_path, bad)
                result = self.stop(reentry=True)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.write(self.attempt_path, self.attempt)
        self.assert_waiting_stop(self.stop(reentry=True))

    def test_reentry_requires_terminal_journal_and_unchanged_report(self):
        cases = [dict(phase='PREPARED'), dict(phase='ENTER_INTENT'),
                 dict(ended_at_epoch=None), dict(error=''), dict(events=None)]
        for delta in cases:
            with self.subTest(delta=delta):
                self.write(self.attempt_path, dict(self.attempt, **delta))
                result = self.stop(reentry=True)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.write(self.attempt_path, self.attempt)
        self.report.write_text('Changed after callback')
        result = self.stop(reentry=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.report.write_text('Bounded findings; known gaps remain.\n')
        self.assert_waiting_stop(self.stop(reentry=True))
        self.attempt_path.unlink()
        result = self.stop(reentry=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_completion_line_needs_the_same_frozen_evidence(self):
        complete = self.line + '\n' + COMPLETION
        with self.lock.open('rb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.stop(final=complete).returncode, 2)
        original = copy.deepcopy(self.attempt)
        for key in ('completion_nonce', 'report_sha256', 'task_pack_sha256'):
            with self.subTest(key=key):
                bad = copy.deepcopy(original)
                bad['binding'][key] = 'wrong'
                self.write(self.attempt_path, bad)
                self.assertEqual(self.stop(final=complete).returncode, 2)
        self.write(self.attempt_path, original)
        self.assertEqual(self.stop(final=complete).returncode, 0)
        self.report.write_text('Changed after callback')
        self.assertEqual(self.stop(final=complete).returncode, 2)
        self.report.write_text('Bounded findings; known gaps remain.\n')
        self.attempt_path.unlink()
        self.assertEqual(self.stop(final=complete).returncode, 2)

    def test_later_task_from_same_supervisor_unseals_without_settling(self):
        before = self.snapshot()
        self.assertEqual(self.pre().returncode, 2)
        later = self.later_task()
        self.assertEqual(self.pre().returncode, 0)
        for reentry in (False, True):
            result = self.stop(final='Starting the new bounded task.', reentry=reentry)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertIsNotNone(self.evidence())  # original evidence is untouched
        self.assertFalse(self.receipt.exists())
        self.assertTrue(self.marker_path.exists())
        after = self.snapshot()
        self.assertEqual(after.pop(later), later.read_bytes())
        self.assertEqual(before, after)

    def test_later_task_keeps_its_own_callback_obligation(self):
        self.later_task()
        next_root = self.root / 'next'
        next_root.mkdir()
        self.write(next_root / 'task-pack.json', dict(
            self.pack, task_id='next-task', report=str(next_root / 'report.md'),
            completion_receipt=str(next_root / 'receipt.json')))
        result = self.stop(final='Starting the new bounded task.')
        self.assertEqual(result.returncode, 2)
        self.assertIn('completion callback receipt missing', result.stderr)
        self.assertNotIn('EXECUTOR_CLOSEOUT', result.stderr)

    def test_supersession_requires_same_identity_and_later_arming(self):
        now = time.time()
        self.attempt.update(started_at_epoch=now - 60, ended_at_epoch=now - 20, events=[
            dict(phase='PASTE_INTENT', at_epoch=now - 50),
            dict(phase='POST_ENTER_OBSERVATION', at_epoch=now - 25)])
        self.write(self.attempt_path, self.attempt)
        self.assertIsNotNone(self.evidence())

        def stamp(offset, naive=False):
            # Naive means local wall time: the only reading a lenient parser has.
            zone = None if naive else timezone.utc
            return datetime.fromtimestamp(now + offset, zone).isoformat()

        supervisor, executor = copy.deepcopy(self.marker['participants'])
        cases = dict(
            other_executor=dict(participants=[supervisor, dict(executor, surface_uuid='other')]),
            other_supervisor=dict(participants=[dict(supervisor, surface_uuid='other'), executor]),
            no_supervisor=dict(participants=[executor]),
            two_supervisors=dict(participants=[supervisor, supervisor, executor]),
            two_executor_rows=dict(participants=[supervisor, executor, executor]),
            other_workspace=dict(workspace_uuid='other'),
            missing_workspace=dict(workspace_uuid=None),
            same_task=dict(task_id='test-task'),
            same_root=dict(artifact_root=str(self.root)),
            relative_root=dict(artifact_root='next'),
            armed_before_attempt_end=dict(armed_at=stamp(-30)),
            naive_armed_at=dict(armed_at=stamp(-5, naive=True)),
            missing_armed_at=dict(armed_at=None),
            future_armed_at=dict(armed_at=stamp(3600)),
            stale=dict(armed_at=stamp(-10), ttl_seconds=1),
            missing_ttl=dict(ttl_seconds=None),
            zero_ttl=dict(ttl_seconds=0),
            invalid_ttl=dict(ttl_seconds='unknown'),
            boolean_ttl=dict(ttl_seconds=True),
            nonfinite_ttl=dict(ttl_seconds=float('nan')),
            root_alias=dict(artifact_root=str(self.root / 'child' / '..')),
        )
        for name, change in cases.items():
            with self.subTest(case=name):
                self.later_task(**change)
                self.assertEqual(self.pre().returncode, 2)
                result = self.stop(final='The original report is frozen; waiting for the supervisor.')
                if name in ('same_root', 'relative_root'):
                    # A second pack rooted here also has its own unresolved callback.
                    self.assertEqual(result.returncode, 2)
                else:
                    # An invalid successor never releases tools, but the old
                    # returned callback may still end in honest, passive waiting.
                    self.assert_waiting_stop(result)
        self.later_task(armed_at=stamp(-5))  # positive control on the same journal
        self.assertEqual(self.pre().returncode, 0)
        self.assertEqual(self.stop(final='Starting the new bounded task.').returncode, 0)

    def test_queue_resume_effective_end_prevents_premature_new_task_scope(self):
        now = time.time()
        self.attempt.update(
            started_at_epoch=now - 70, ended_at_epoch=now - 40,
            phase='POST_QUEUE_TAB_OBSERVATION', events=[
                dict(phase='PASTE_INTENT', at_epoch=now - 60),
                dict(phase='ENTER_INTENT', at_epoch=now - 55),
                dict(phase='POST_ENTER_OBSERVATION', at_epoch=now - 50),
                dict(phase='QUEUE_TAB_INTENT', at_epoch=now - 20),
                dict(phase='POST_QUEUE_TAB_OBSERVATION', at_epoch=now - 15)])
        self.write(self.attempt_path, self.attempt)
        self.assertEqual(self.evidence()['ended_at_epoch'], now - 15)
        self.later_task(armed_at=datetime.fromtimestamp(now - 30, timezone.utc).isoformat())
        self.assertEqual(self.pre().returncode, 2)
        self.assert_waiting_stop(self.stop(final='Waiting for supervisor reconciliation.'))
        before = self.snapshot()
        self.assertEqual(self.stop(final=self.line + '\n' + COMPLETION).returncode, 0)
        self.assertEqual(before, self.snapshot())
        self.later_task(armed_at=datetime.fromtimestamp(now - 5, timezone.utc).isoformat())
        self.assertEqual(self.pre().returncode, 0)
        self.assertEqual(self.stop(final='Starting the new bounded task.').returncode, 0)
        self.assertFalse(self.receipt.exists())

    def test_old_consensus_evidence_does_not_override_later_task_evidence(self):
        self.later_task()
        root = self.root / 'next'
        root.mkdir()
        for name in ('validation.json', 'consensus-validation.json'):
            self.write(root / name, dict(task_id='next-task', status='PASS'))
        before = self.snapshot()
        self.assertEqual(self.stop(final='validation PASS').returncode, 0)
        self.assertEqual(before, self.snapshot())
        self.write(root / 'validation.json', dict(task_id='next-task', status='FAIL'))
        result = self.stop(final='validation PASS')
        self.assertEqual(result.returncode, 2)
        self.assertIn('next-task', result.stderr)
        self.assertFalse(self.receipt.exists())


if __name__ == '__main__':
    unittest.main()

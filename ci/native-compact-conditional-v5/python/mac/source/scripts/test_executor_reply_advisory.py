"""Exact reply context reaches only the original automatic Claude hook."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import cmux_native_delivery as native
import cmux_workflow_advisory as wrapper
import executor_ready as ready
import executor_reask as reask
import executor_reask_scheduler as scheduler
import executor_reply_advisory as advisory
import test_executor_reask_scheduler as fixtures
from test_executor_reask_scheduler import CALLER, SUP, WORKSPACE, PANE


class ReplyAdvisoryTests(unittest.TestCase):
    begin = fixtures.SchedulerTests.begin
    live = fixtures.SchedulerTests.live
    enroll = fixtures.SchedulerTests.enroll
    SESSION = '55555555-5555-4555-8555-555555555555'

    def setUp(self, continuing=False):
        fixtures.SchedulerTests.setUp(self)
        self.stack.enter_context(patch.dict(os.environ, CMUX_SURFACE_ID=CALLER))
        self.log = self.home / '.claude/projects/test' / (self.SESSION + '.jsonl')
        self.log.parent.mkdir(parents=True)
        self.log.write_text(json.dumps(dict(type='user', sessionId=self.SESSION)) + '\n')
        self.process = dict(pid=501, birth=[1, 2], executable='/test/claude',
                            file_identity=[1, 2, 3, 4])
        self.binding = dict(schema=advisory.IDENTITY_SCHEMA, provider='claude',
                            caller_surface_uuid=CALLER, workspace_uuid=WORKSPACE,
                            pane_uuid=PANE, tty='ttys001', session_id=self.SESSION,
                            process=self.process,
                            transcript=native._pin_file(self.log, 'claude', self.SESSION))
        self.row = dict(self.process, ppid=500,
                        env=dict(CMUX_SURFACE_ID=CALLER, CMUX_WORKSPACE_ID=WORKSPACE))
        self.process_mock = self.stack.enter_context(patch.object(
            native.identity, 'process', return_value=self.row))
        self.stack.enter_context(patch.object(native, '_provider', return_value='claude'))
        self.session_mock = self.stack.enter_context(patch.object(
            native, '_live_session', return_value=self.SESSION))
        self.tty_mock = self.stack.enter_context(patch.object(
            advisory.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'ttys001\n')))
        self.tree = self.stack.enter_context(patch.object(native, '_tree_row', return_value=dict(
            workspace_uuid=WORKSPACE, target_surface_uuid=CALLER,
            target_pane_uuid=PANE, tty='ttys001')))
        # Identity and cmux inventory are the only external dependencies mocked.
        # Keep the actual hook ancestry, native file pins, state and reply checks.
        self.transport.side_effect = None
        if continuing:
            self.authorization = self.home / 'original-authorization.txt'
            self.authorization.write_text('Continue the original task with the existing executor.')
            state = self.begin(continuing=True, continuation=dict(
                enabled=True, overall_task='original overall task', owner=SUP,
                authorization_file=str(self.authorization)))
            scheduler.enroll(state, self.live(), now=1000.)
        else:
            self.enroll()
        scheduler.sweep(now=1001.)
        state = reask.load(CALLER)
        current = ready.read_json(reask.inbox_path(state))
        self.body = dict(current['reply_template'], trigger='Read the complete reply. 中文正文。')
        ready.write_json(Path(current['reply_mailbox']), self.body)
        scheduler.sweep(now=1002.)
        state = reask.load(CALLER)
        self.assertEqual(state['state'], reask.ANSWERED)
        if not continuing:
            state['executor_identity'] = self.binding
            reask.save(state)
        self.payload = dict(hook_event_name='PostToolUse', session_id=self.SESSION,
                            transcript_path=str(self.log))

    def invoke(self, now=1003., payload=None):
        notices = []
        advisory.discover(self.payload if payload is None else payload,
                          emit=notices.append, now=now)
        return notices

    def test_exact_reply_path_and_original_consume_command_without_state_write(self):
        path = reask.state_path(CALLER)
        before = path.read_bytes()
        notice, = self.invoke()
        self.assertEqual(notice['status'], 'ANSWERED_UNCONSUMED')
        self.assertEqual(notice['reply_file'], str(path))
        self.assertEqual(notice['json_pointer'], '/reply/body')
        self.assertEqual(notice['body_sha256'], advisory.sha(advisory.canonical(self.body).encode()))
        self.assertIn(self.current['source'] + '/scripts/executor_reask.py', notice['consume_command'])
        self.assertIn('--reply-sha256 ' + notice['body_sha256'], notice['consume_command'])
        self.assertEqual((notice['input_operations'], notice['consumed']), (0, False))
        self.assertEqual(path.read_bytes(), before)
        self.transport.return_value.submit_text.assert_not_called()

    def test_reply_during_reservation_is_visible_without_release(self):
        state = reask.load(CALLER)
        state['paused_tasks'] = [dict(task_id='business', collaboration_id='reservation')]
        reask.save(state)
        before = reask.state_path(CALLER).read_bytes()
        self.assertTrue(self.invoke()[0]['reservation_paused'])
        self.assertEqual(reask.state_path(CALLER).read_bytes(), before)

    def test_repeat_after_sixty_seconds_and_failed_flush_does_not_advance(self):
        def fail(_):
            raise BrokenPipeError('closed output')
        self.assertIsNone(advisory.discover(self.payload, emit=fail, now=1003.))
        cursor = advisory.root() / (ready.digest(CALLER) + '.cursor.json')
        self.assertFalse(cursor.exists())
        self.assertEqual(len(self.invoke(1004.)), 1)
        self.assertEqual(self.invoke(1063.), [])
        self.assertEqual(len(self.invoke(1064.)), 1)

    def test_wrong_event_session_and_transcript_are_silent(self):
        for change in (dict(hook_event_name='Stop'), dict(hook_event_name='PreToolUse'),
                       dict(session_id=SUP), dict(transcript_path='/other'),
                       dict(session_id=None)):
            with self.subTest(change=change):
                self.assertEqual(self.invoke(payload=dict(self.payload, **change)), [])
        self.assertIsNone(advisory.discover(None))

    def test_manual_python_parent_is_not_an_automatic_hook(self):
        self.process_mock.return_value = dict(pid=700, ppid=501, executable='/test/python')
        self.assertEqual(self.invoke(), [])

    def test_shell_parent_chain_of_original_claude_is_supported(self):
        self.process_mock.side_effect = lambda pid, **_: (
            dict(pid=700, ppid=501, executable='/bin/sh') if pid == 700 else self.row)
        with patch.object(advisory.os, 'getppid', return_value=700):
            self.assertEqual(len(self.invoke()), 1)

    def test_changed_birth_session_tty_or_workspace_is_silent(self):
        for change in (dict(birth=[9, 9]), dict(file_identity=[9] * 4),
                       dict(env=dict(CMUX_SURFACE_ID=SUP, CMUX_WORKSPACE_ID=WORKSPACE))):
            with self.subTest(change=change):
                self.process_mock.return_value = dict(self.row, **change)
                self.assertEqual(self.invoke(), [])
        self.process_mock.return_value = self.row
        self.session_mock.return_value = SUP
        self.assertEqual(self.invoke(), [])
        self.session_mock.return_value = self.SESSION
        self.tty_mock.return_value.stdout = 'ttys999\n'
        self.assertEqual(self.invoke(), [])
        self.tty_mock.return_value.stdout = 'ttys001\n'
        self.tree.return_value = dict(self.tree.return_value, workspace_uuid=SUP)
        self.assertEqual(self.invoke(), [])

    def test_native_log_growth_allowed_but_replacement_or_truncation_refused(self):
        original = self.log.read_bytes()
        with self.log.open('ab') as stream:
            stream.write(b'{"type":"progress"}\n')
        self.assertEqual(len(self.invoke()), 1)
        self.log.write_bytes(b'')
        self.assertEqual(self.invoke(2000.), [])
        replacement = self.log.with_suffix('.replacement')
        replacement.write_bytes(original)
        os.replace(replacement, self.log)
        self.assertEqual(self.invoke(2000.), [])

    def test_wrong_reply_hash_marker_and_binding_are_refused(self):
        original = reask.load(CALLER)
        variants = []
        state = copy.deepcopy(original)
        state['reply']['body_sha256'] = '0' * 64
        variants.append(state)
        state = copy.deepcopy(original)
        state['reply']['marker'] = 'UNISSUED'
        variants.append(state)
        state = copy.deepcopy(original)
        state['reply']['body']['task_id'] = 'foreign'
        state['reply']['body_sha256'] = advisory.sha(advisory.canonical(state['reply']['body']).encode())
        variants.append(state)
        for state in variants:
            with self.subTest(reply=state['reply']):
                reask.save(state)
                self.assertEqual(self.invoke(), [])

    def test_consumed_stopped_and_operator_stop_never_offer(self):
        original = reask.load(CALLER)
        for status in (reask.WAITING, reask.CONSUMED, reask.STOPPED):
            reask.save(dict(original, state=status))
            self.assertEqual(self.invoke(), [])
        reask.save(original)
        ready.write_json(reask.stop_file(CALLER), dict(reason='stop'))
        self.assertEqual(self.invoke(), [])

    def test_legacy_episode_without_explicit_enrollment_is_untouched(self):
        state = reask.load(CALLER)
        state.pop('executor_identity')
        reask.save(state)
        before = reask.state_path(CALLER).read_bytes()
        self.assertEqual(self.invoke(), [])
        self.assertEqual(reask.state_path(CALLER).read_bytes(), before)
        self.assertFalse(advisory.root().exists())

    def test_identity_change_between_checks_leaves_cursor_uncommitted(self):
        original = advisory.check_executor
        calls = []
        def check(*args):
            calls.append(None)
            if len(calls) == 2:
                raise ValueError('foreground changed')
            return original(*args)
        with patch.object(advisory, 'check_executor', side_effect=check):
            self.assertEqual(self.invoke(), [])
        self.assertFalse((advisory.root() / (ready.digest(CALLER) + '.cursor.json')).exists())

    def test_wrapper_emits_client_context_and_does_not_run_old_gates(self):
        with patch.object(wrapper.sys, 'stdin', io.StringIO(json.dumps(self.payload))), \
                patch.object(wrapper.sys, 'argv', ['wrapper', '--hook', 'executor_reply_advisory.py']), \
                patch.object(wrapper.sys, 'stdout', io.StringIO()) as output, \
                patch('cmux_hook_runtime_audit.record', side_effect=OSError('telemetry unavailable')):
            self.assertEqual(wrapper.main(), 0)
        value = json.loads(output.getvalue())
        self.assertEqual(set(value), {'hookSpecificOutput'})
        self.assertIn('ANSWERED_UNCONSUMED', value['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('decision', value)

    def creation_proof(self):
        state = reask.load(CALLER)
        marker = reask.issue_marker(state, 1)
        tool_id = 'original-start'
        summary = dict(outcome=reask.WAITING, ask_count=1, supervisor=SUP,
                       state_file=str(reask.state_path(CALLER)), last_ask=dict(marker=marker),
                       file_posts=1, terminal_sends=0, terminal_attempts=0)
        call = dict(type='assistant', sessionId=self.SESSION, timestamp='1970-01-01T00:16:39Z',
                    message=dict(content=[dict(type='tool_use', id=tool_id, name='Bash',
                                              input=dict(command='original start --file-only'))]))
        result = dict(type='user', sessionId=self.SESSION, timestamp='1970-01-01T00:16:41Z',
                      message=dict(content=[dict(type='tool_result', tool_use_id=tool_id,
                                                is_error=False, content=json.dumps(summary))]))
        use, response = ((json.dumps(value) + '\n').encode() for value in (call, result))
        self.log.write_bytes(use + response)
        binding = dict(self.binding, transcript=native._pin_file(self.log, 'claude', self.SESSION))
        proof = dict(session_id=self.SESSION, episode_id=state['episode_id'],
                     controller=advisory.protocol.state_controller(state), first_marker=marker,
                     tool_use_id=tool_id,
                     tool_use=dict(offset=0, bytes=len(use), sha256=advisory.sha(use)),
                     tool_result=dict(offset=len(use), bytes=len(response), sha256=advisory.sha(response)))
        return state, binding, proof

    def test_original_creation_requires_exact_native_call_result_and_first_marker(self):
        state, binding, proof = self.creation_proof()
        self.assertTrue(advisory.verify_creation(state, binding, proof))
        for field in ('session_id', 'episode_id', 'controller', 'first_marker', 'tool_use_id'):
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError)):
                advisory.verify_creation(state, binding, dict(proof, **{field: 'wrong'}))

    def test_creation_rejects_nonboundary_offsets_changed_bytes_and_wrong_time(self):
        state, binding, proof = self.creation_proof()
        for changed in (dict(offset=1), dict(bytes=advisory.MAX_CREATION_BYTES + 1),
                        dict(sha256='0' * 64)):
            modified = copy.deepcopy(proof)
            modified['tool_use'].update(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                advisory.verify_creation(state, binding, modified)
        with self.assertRaises(ValueError):
            advisory.verify_creation(dict(state, started_epoch=2000.), binding, proof)


class ExistingEpisodeEnrollmentTests(unittest.TestCase):
    begin = fixtures.SchedulerTests.begin
    live = fixtures.SchedulerTests.live
    creation_proof = ReplyAdvisoryTests.creation_proof
    invoke = ReplyAdvisoryTests.invoke
    SESSION = ReplyAdvisoryTests.SESSION

    def setUp(self):
        ReplyAdvisoryTests.setUp(self, continuing=True)
        state, _binding, proof = self.creation_proof()
        self.proof_path = self.home / 'original-creation-proof.json'
        ready.write_json(self.proof_path, proof)
        self.pending = self.home / 'original-attempt.json'
        self.pending.write_text('{"outcome":"UNKNOWN","nonce":"original","events":[]}\n')
        state['terminal_pending'] = dict(attempt=str(self.pending), outcome='UNKNOWN')
        reask.save(state)
        self.state = state
        self.originals = {p: p.read_bytes() for p in (
            reask.state_path(CALLER), scheduler.enrollment_path(CALLER), self.pending)}
        self.transport.return_value.pin_workspace.return_value = self.live()
        self.stack.enter_context(patch.object(native, '_target_process', return_value=self.row))
        self.stack.enter_context(patch.object(native, '_transcript_path', return_value=self.log))

    def register(self):
        return advisory.enroll_existing(
            self.live(), self.state['episode_id'],
            advisory.sha(self.originals[reask.state_path(CALLER)]), str(self.authorization),
            str(self.proof_path), advisory.sha(self.proof_path.read_bytes()))

    def assert_originals_unchanged(self):
        for path, raw in self.originals.items():
            self.assertEqual(path.read_bytes(), raw, str(path))
        self.transport.return_value.submit_text.assert_not_called()

    def test_real_sidecar_registration_and_hook_preserve_original_episode_and_unknown(self):
        self.assertEqual(self.invoke(), [])
        registered = self.register()
        self.assertEqual(registered['status'], 'ENROLLED_ADVISORY_ONLY')
        sidecar = ready.read_json(Path(registered['path']))
        self.assertEqual(sidecar['controller'], advisory.protocol.state_controller(self.state))
        self.assertEqual(sidecar['enrollment_mode'], 'executor_self')
        self.assertFalse(sidecar['historical_wrapper_source_verified'])
        notice, = self.invoke()
        self.assertEqual(notice['status'], 'ANSWERED_UNCONSUMED')
        self.assertEqual(notice['body_sha256'], self.state['reply']['body_sha256'])
        self.assertEqual((registered['input_operations'], registered['consumed'],
                          registered['migrated']), (0, False, False))
        self.assert_originals_unchanged()

    def test_revoked_authorization_suppresses_hook_without_mutating_old_state(self):
        self.register()
        self.authorization.write_text('Revoked; preserve the original inquiry.')
        self.assertEqual(self.invoke(), [])
        self.assert_originals_unchanged()

    def test_wrong_native_creation_proof_cannot_register(self):
        proof = ready.read_json(self.proof_path)
        proof['tool_use']['sha256'] = '0' * 64
        ready.write_json(self.proof_path, proof)
        with self.assertRaisesRegex(ValueError, 'creation record changed'):
            self.register()
        self.assertFalse((advisory.root() / (ready.digest(CALLER) + '.enrollment.json')).exists())
        self.assert_originals_unchanged()

    def test_duplicate_registration_cannot_replace_sidecar(self):
        registered = self.register()
        path = Path(registered['path'])
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.register()
        self.assertEqual(path.read_bytes(), original)
        self.assert_originals_unchanged()


if __name__ == '__main__':
    unittest.main()

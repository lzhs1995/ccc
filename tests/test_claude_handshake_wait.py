"""Offline boundary tests: a harness ACK waits for its native task dispatch.

All terminal/process interactions use FakeClient. No Claude process is started,
no live terminal is read, and no real text or Enter is sent by this module.
"""
import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import claude_ccc_protocol as protocol
import cmux_codex_watch as core
from tests.test_watch import (
    FakeClient, armed_daemon, bind_fake_claude_process_identity,
    claude_grid_payload, claude_idle_screen, claude_wrapped_composer_payload,
    process_fixture,
)


TASK = 'ccc-handshake-offline-737'
NONCE = 'a1737b42'
MARKER = 'dispatch737-a1737b42'
ENV = {'CLAUDE_PID': '1234', 'CMUX_SURFACE_ID': 'surface-uuid',
       'CMUX_WORKSPACE_ID': 'workspace-uuid'}


def challenge_text(directory, *, task=TASK, nonce=NONCE, executor='surface:1'):
    # Preserve the measured native harness grammar, including receipt-record
    # assertions, rather than inventing a convenient shorter prompt protocol.
    return (
        f'DELIVERY_NONCE={nonce}. This is a legitimate cmux multi-agent harness '
        f'handshake from supervisor surface:9 for task {task}. '
        'Use the /multi-agent-collaboration skill for this session. '
        'Read the canonical skill file at /tmp/ccc-offline/SKILL.md. '
        f'Verify the pending receipt at the absolute path {directory}/handshake-receipt.json; '
        f'do not search the disk. Select exactly one executors[] record with executor == {executor} '
        f'and ordinal == 1. In that record confirm task_id == {task}, '
        f'executor_provider == claude, and ack_nonce == {nonce}. '
        f'Then construct the ACK from these fields: ACK_TASK_ID={task} '
        f'ACK_AGENT=claude:identity ACK_STATUS=READY ACK_REPORT=INLINE ACK_NONCE={nonce}. '
        'Reply with exactly the ACK and wait for the task pack.'
    )


def ack_text(*, task=TASK, nonce=NONCE, agent='claude:identity'):
    return f'PREFLIGHT_ACK|{task}|{agent}|READY|INLINE|{nonce}'


def dispatch_text(directory, *, task=TASK, marker=MARKER):
    return (
        f'TASK_DISPATCH {marker}\n'
        'READ_AND_OBEY_REQUIRED_SKILL_FIRST\n'
        f'TASK_PACK={directory}/task-pack.json\n'
        'REQUIRED_SKILL=/tmp/ccc-offline/SKILL.md\n'
        'CALLBACK_TARGET=surface:9\n'
        f'COMPLETION_CALLBACK=DONE|{task}|{marker}|REPORT={directory}/executor-report.md\n'
        'Proceed with the bounded task pack.\n'
    )


def native_event(event_id, event_name, text='', *, created_at=None, **changes):
    payload = {'hook_event_name': event_name, 'session_id': 'session-uuid'}
    payload['prompt' if event_name == 'UserPromptSubmit' else 'last_assistant_message'] = text
    with mock.patch.object(protocol, 'configured_claude_message', return_value=core.CLAUDE_MESSAGE):
        event = protocol.build_event(payload, ENV)
    event.update(event_id=event_id, created_at=created_at if created_at is not None else time.time(),
                 process_generation='fixture-birth-1234')
    event.update(changes)
    return event


class HandshakeProtocolTests(unittest.TestCase):
    def test_native_challenge_ack_and_dispatch_extract_bounded_metadata(self):
        event = native_event('challenge', 'UserPromptSubmit', challenge_text('/tmp/ccc737'))
        self.assertEqual(event.get('handshake_challenge'), {
            'task_id': TASK, 'ack_nonce': NONCE, 'agent': 'claude:identity',
            'executor': 'surface:1', 'receipt_path': '/tmp/ccc737/handshake-receipt.json',
        })
        event = native_event('ack', 'Stop', ack_text())
        self.assertEqual(event.get('handshake_ack'), {
            'task_id': TASK, 'ack_nonce': NONCE, 'agent': 'claude:identity',
        })
        self.assertFalse(event['completed'])
        self.assertFalse(event.get('report_ready_task_id'))
        event = native_event('dispatch', 'UserPromptSubmit', dispatch_text('/tmp/ccc737'))
        self.assertEqual(event.get('task_dispatch'), {
            'task_id': TASK, 'marker': MARKER, 'task_pack': '/tmp/ccc737/task-pack.json',
        })

    def test_ack_requires_entire_unquoted_exact_response(self):
        ack = ack_text()
        bad = ['> ' + ack, '```\n' + ack + '\n```', 'Example: ' + ack,
               ack + '\nStill working', '    ' + ack, ack + ' ',
               ack + '\n' + ack, ack.replace('|READY|', '|PENDING|'),
               ack.replace('|INLINE|', '|/tmp/report.md|')]
        for text in bad:
            with self.subTest(text=text):
                self.assertFalse(native_event('bad', 'Stop', text).get('handshake_ack'))
        self.assertFalse(native_event('failure', 'StopFailure', ack).get('handshake_ack'))

    def test_challenge_rejects_quoted_duplicate_and_inconsistent_fields(self):
        value = challenge_text('/tmp/ccc737')
        bad = [
            '> ' + value, '```\n' + value + '\n```', 'Example: ' + value,
            value + f' ACK_NONCE={NONCE}', value + f' ACK_TASK_ID={TASK}',
            value + ' ACK_NONCE=bad', value + ' ACK_AGENT=codex:identity',
            value.replace(f'DELIVERY_NONCE={NONCE}', 'DELIVERY_NONCE=other737'),
            value.replace(f'for task {TASK}.', 'for task other-task.'),
            value.replace(f'task_id == {TASK},', 'task_id == other-task,'),
            value.replace(f'ack_nonce == {NONCE}.', 'ack_nonce == other737.'),
            value.replace('executor_provider == claude', 'executor_provider == codex'),
            value.replace('/tmp/ccc737/handshake-receipt.json', 'relative/handshake-receipt.json'),
        ]
        for text in bad:
            with self.subTest(text=text):
                self.assertFalse(native_event('bad', 'UserPromptSubmit', text).get('handshake_challenge'))

    def test_dispatch_rejects_quoted_partial_ambiguous_and_marker_mismatch(self):
        value = dispatch_text('/tmp/ccc737')
        bad = [
            '> ' + value, '```\n' + value + '\n```', 'Example:\n' + value,
            value.replace('TASK_DISPATCH ', 'TASK_DISPATCH'),
            value.replace(f'DONE|{TASK}|{MARKER}|', f'DONE|{TASK}|different737|'),
            value.replace('TASK_PACK=/tmp', 'TASK_PACK=relative'),
            '\n'.join(line for line in value.splitlines() if not line.startswith('COMPLETION_CALLBACK=')),
            value + 'TASK_PACK=/tmp/second/task-pack.json\n',
            value + f'COMPLETION_CALLBACK=DONE|{TASK}|{MARKER}|REPORT=/tmp/other.md\n',
        ]
        for text in bad:
            with self.subTest(text=text):
                self.assertFalse(native_event('bad', 'UserPromptSubmit', text).get('task_dispatch'))

    def test_dispatch_parser_does_not_open_prompt_selected_files(self):
        with mock.patch.object(Path, 'read_text', side_effect=AssertionError('unexpected disk read')):
            event = native_event('dispatch', 'UserPromptSubmit', dispatch_text('/untrusted/not-present'))
        self.assertEqual(event['task_dispatch']['task_pack'], '/untrusted/not-present/task-pack.json')


class HandshakeWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = FakeClient(
            claude_grid_payload(lines=['Unfinished task'], completed=True),
            text='Unfinished task\n' + claude_idle_screen(),
            top=process_fixture(('surface-uuid', 'claude')))
        self.daemon = armed_daemon(self.tmp.name, self.client)
        self.daemon.config['claude_enabled'] = True
        self.runtime = self.daemon.runtime.setdefault('surface-uuid', core.TargetRuntime())
        self.runtime.claude_process_pid = 1234
        self.runtime.claude_process_generation = 'fixture-birth-1234'
        self.runtime.claude_session_id = 'session-uuid'
        self.runtime.claude_hook_health = 'healthy'
        self.target = self.daemon.config['targets'][0]
        self.target['ref'] = 'surface:1'
        self.at = time.time() - 30
        self._counter = 0
        self.write_pack()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0).start()
        self.process_inspection = mock.patch.object(core, 'inspect_claude_process', return_value={
            'pid': 1234, 'started_at': '1970-01-01T00:00:01', 'started_epoch': 1.0,
            'generation': 'fixture-birth-1234', 'legacy_override': False,
        }).start()

    def event(self, kind, text='', *, offset=0, **changes):
        self._counter += 1
        created_at = changes.pop('created_at', self.at + offset)
        return native_event(f'offline737-{self._counter}', kind, text,
                            created_at=created_at, **changes)

    def write_pack(self, **changes):
        pack = {'task_id': TASK, 'callback': ack_text(), 'completion_nonce': MARKER,
                'executor': 'surface:1', 'executor_uuid': 'surface-uuid',
                'draft': False, 'role': 'executor',
                'executors': [{'surface_ref': 'surface:1', 'provider': 'claude', 'ordinal': 1}]}
        pack.update(changes)
        path = Path(self.tmp.name) / 'task-pack.json'
        path.write_text(json.dumps(pack), encoding='utf-8')
        return path

    def receipt(self, **changes):
        # Native harness receipts bind one executors[] record; top-level
        # success alone is insufficient to prove this executor's ACK.
        row = {'task_id': TASK, 'executor': 'surface:1', 'ordinal': 1,
               'executor_provider': 'claude', 'status': 'PASS', 'lifecycle': 'ACKED',
               'executor_ack': True, 'ack_nonce': NONCE,
               'ack_line': ack_text(), 'ack_line_expected': ack_text()}
        row.update(changes)
        return {'task_id': TASK, 'executors': [row]}

    def write_receipt(self, receipt):
        path = Path(self.tmp.name) / 'handshake-receipt.json'
        path.write_text(json.dumps(receipt), encoding='utf-8')
        return path

    def handle(self, event):
        self.daemon._handle_claude_event(event, self.client)
        return event

    def challenge(self, *, offset=0, **kwargs):
        return self.handle(self.event('UserPromptSubmit', challenge_text(self.tmp.name, **kwargs), offset=offset))

    def ack(self, *, offset=1, **kwargs):
        return self.handle(self.event('Stop', ack_text(**kwargs), offset=offset))

    def wait(self):
        self.challenge()
        self.ack()
        self.assert_waiting()

    def dispatch(self, *, offset=2, **kwargs):
        return self.handle(self.event('UserPromptSubmit', dispatch_text(self.tmp.name, **kwargs), offset=offset))

    def assert_waiting(self):
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.assertEqual(self.runtime.state, 'claude_handshake_wait')
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertFalse(self.runtime.claude_report_ready_task_id)

    def assert_no_input(self):
        self.assertEqual(self.client.sent_text, [])
        self.assertEqual(self.client.sent_keys, [])

    def screen(self):
        return core.classify_claude_grid(core.Grid.from_rpc(self.client.payload, 'surface-uuid'))

    def composer(self):
        self.client.payload = claude_wrapped_composer_payload()
        self.client.text = '❯ ' + core.CLAUDE_MESSAGE
        return self.screen()

    def test_challenge_binds_native_identity_without_business_completion(self):
        event = self.challenge()
        challenge = self.runtime.claude_handshake_challenge
        self.assertEqual(challenge['task_id'], TASK)
        self.assertEqual(challenge['session_id'], 'session-uuid')
        self.assertEqual(challenge['process_generation'], 'fixture-birth-1234')
        self.assertEqual(challenge['agent_pid'], 1234)
        self.assertEqual(challenge['event_id'], event['event_id'])
        self.assertEqual(challenge['message_hash'], event['message_hash'])
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertEqual(self.daemon.claude_event_ledger.status_of(event['event_id']), 'handshake_challenge')
        self.assert_no_input()

    def test_matching_ack_latches_wait_and_recursive_stop_cannot_continue(self):
        self.challenge()
        event = self.ack()
        self.assert_waiting()
        self.assertEqual(self.runtime.claude_handshake_ack_at, event['created_at'])
        self.assertEqual(self.runtime.claude_handshake_ack_event_id, event['event_id'])
        self.assertEqual(self.daemon.claude_event_ledger.status_of(event['event_id']), 'handshake_wait')
        stop = self.handle(self.event('Stop', 'Still waiting for task pack', offset=2, stop_hook_active=True))
        self.assert_waiting()
        self.assertEqual(self.daemon.claude_event_ledger.status_of(stop['event_id']), 'suppressed_handshake_wait')
        self.assert_no_input()

    def test_actual_native_events_without_generation_bind_verified_process_birth(self):
        # build_event emits PID/session, not the daemon's process generation.
        # The real ingestion boundary must work without fixture-only metadata.
        for kind, text, offset in [
            ('UserPromptSubmit', challenge_text(self.tmp.name), 0),
            ('Stop', ack_text(), 1),
        ]:
            event = self.event(kind, text, offset=offset)
            event.pop('process_generation')
            self.handle(event)
        self.assert_waiting()
        self.assertEqual(self.runtime.claude_handshake_challenge['process_generation'], 'fixture-birth-1234')
        event = self.event('UserPromptSubmit', dispatch_text(self.tmp.name), offset=2)
        event.pop('process_generation')
        self.handle(event)
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.process_inspection.assert_called_with(1234)
        self.assert_no_input()

    def test_untagged_native_event_rejects_exited_reused_or_younger_process(self):
        for inspection in [
            {'pid': 1234, 'started_epoch': 0.0, 'generation': 'fixture-birth-1234'},
            {'pid': 1234, 'started_epoch': 1.0, 'generation': 'reused-pid-birth'},
            {'pid': 1234, 'started_epoch': self.at + 5, 'generation': 'fixture-birth-1234'},
            {'pid': 1234, 'started_epoch': float('nan'), 'generation': 'fixture-birth-1234'},
            {'pid': 1234, 'started_epoch': 1.0, 'generation': ''},
        ]:
            with self.subTest(inspection=inspection):
                self.process_inspection.return_value = inspection
                event = self.event('UserPromptSubmit', challenge_text(self.tmp.name))
                event.pop('process_generation')
                self.handle(event)
                self.assertFalse(self.runtime.claude_handshake_challenge)
                self.assertFalse(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_unbound_ack_cannot_create_wait(self):
        self.ack()
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assertFalse(self.runtime.claude_completed_latched)

    def test_wrong_task_nonce_agent_and_quoted_ack_cannot_create_wait(self):
        self.challenge()
        values = [ack_text(task='other-task'), ack_text(nonce='other737'),
                  ack_text(agent='claude:reviewer'), '> ' + ack_text(),
                  '```\n' + ack_text() + '\n```', ack_text() + '\nMore prose']
        # Protected viewport prevents unrelated ordinary Stop continuation from
        # obscuring the parser/ACK-binding assertion under test.
        self.client.payload = claude_grid_payload(spinner='✶ Thinking… (3s · ↓ 15 tokens)')
        for index, value in enumerate(values, 1):
            with self.subTest(value=value):
                self.handle(self.event('Stop', value, offset=index))
                self.assertFalse(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_wrong_native_identity_cannot_establish_challenge(self):
        changes = [{'agent_pid': 999}, {'session_id': 'other-session'},
                   {'surface_id': 'other-surface'}, {'workspace_id': 'other-workspace'},
                   {'process_generation': 'other-birth'}, {'synthetic_fallback': True}]
        for change in changes:
            with self.subTest(change=change):
                self.handle(self.event('UserPromptSubmit', challenge_text(self.tmp.name), **change))
                self.assertFalse(self.runtime.claude_handshake_challenge)
                self.assertFalse(self.runtime.claude_handshake_wait)
        self.challenge(executor='surface:2')
        self.assertFalse(self.runtime.claude_handshake_challenge)
        self.assert_no_input()

    def test_wrong_native_identity_cannot_ack_or_release_existing_wait(self):
        self.challenge()
        changes = [{'agent_pid': 999}, {'session_id': 'other-session'},
                   {'surface_id': 'other-surface'}, {'workspace_id': 'other-workspace'},
                   {'process_generation': 'other-birth'}, {'synthetic_fallback': True}]
        for change in changes:
            with self.subTest(stage='ack', change=change):
                self.handle(self.event('Stop', ack_text(), offset=1, **change))
                self.assertFalse(self.runtime.claude_handshake_wait)
        self.ack(offset=2)
        self.assert_waiting()
        for change in changes:
            with self.subTest(stage='dispatch', change=change):
                self.handle(self.event('UserPromptSubmit', dispatch_text(self.tmp.name), offset=3, **change))
                self.assertTrue(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_stale_and_nonfinite_events_cannot_ack_or_release_wait(self):
        self.challenge()
        stamps = [self.at - 1, self.at, float('nan'), float('inf'), time.time() + 120]
        for stamp in stamps:
            with self.subTest(stage='ack', stamp=stamp):
                self.handle(self.event('Stop', ack_text(), created_at=stamp))
                self.assertFalse(self.runtime.claude_handshake_wait)
        self.ack()
        for stamp in [self.at, self.at + 1, float('nan'), float('inf'), time.time() + 120]:
            with self.subTest(stage='dispatch', stamp=stamp):
                self.handle(self.event('UserPromptSubmit', dispatch_text(self.tmp.name), created_at=stamp))
                self.assertTrue(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_challenge_future_and_nonfinite_times_do_not_establish_binding(self):
        for stamp in [float('nan'), float('inf'), time.time() + 120]:
            with self.subTest(stamp=stamp):
                self.handle(self.event('UserPromptSubmit', challenge_text(self.tmp.name), created_at=stamp))
                self.assertFalse(self.runtime.claude_handshake_challenge)
        self.assert_no_input()

    def test_wait_survives_daemon_save_reload(self):
        self.wait()
        expected = dict(self.runtime.claude_handshake_challenge)
        self.daemon.save()
        self.daemon = core.WatchDaemon(Path(self.tmp.name) / 'config.json',
                                      Path(self.tmp.name) / 'state.json', client=self.client)
        bind_fake_claude_process_identity(self.daemon, self.client)
        self.runtime = self.daemon.runtime['surface-uuid']
        self.target = self.daemon.config['targets'][0]
        state = self.daemon._apply_claude_runtime_guards('surface-uuid', self.runtime, self.screen())
        self.assertEqual(state.kind, 'claude_handshake_wait')
        self.assertEqual(self.runtime.claude_handshake_challenge, expected)
        self.assert_waiting()
        self.dispatch()
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_same_session_start_preserves_wait_and_verified_new_session_clears_it(self):
        self.wait()
        event = self.event('SessionStart', offset=2)
        event.pop('process_generation')
        self.handle(event)
        self.assert_waiting()
        event = self.event('SessionStart', offset=3, session_id='new-session')
        event.pop('process_generation')
        self.handle(event)
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assertFalse(self.runtime.claude_handshake_challenge)
        self.assertEqual(self.runtime.claude_session_id, 'new-session')
        self.process_inspection.assert_called_with(1234)
        self.assert_no_input()

    def test_stale_future_or_wrong_workspace_session_start_cannot_clear_wait(self):
        self.wait()
        for changes in [
            {'created_at': self.at - 1}, {'created_at': self.at + 1},
            {'created_at': time.time() + 120}, {'created_at': float('nan')},
            {'workspace_id': 'other-workspace'}, {'synthetic_fallback': True},
            {'agent_pid': 999}, {'process_generation': 'other-birth'},
        ]:
            with self.subTest(changes=changes):
                self.handle(self.event('SessionStart', offset=2, session_id='old-or-wrong-session', **changes))
                self.assertTrue(self.runtime.claude_handshake_wait)
                self.assertEqual(self.runtime.claude_session_id, 'session-uuid')
        self.assert_no_input()

    def test_matching_native_dispatch_releases_and_business_stop_continues_once(self):
        self.wait()
        event = self.dispatch()
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assertEqual(self.daemon.claude_event_ledger.status_of(event['event_id']), 'task_dispatch')
        stop = self.event('Stop', 'The assigned business task is unfinished', offset=3)
        self.handle(stop)
        self.handle(stop)
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(len(self.client.sent_keys), 1)

    def test_malformed_or_wrong_task_dispatch_is_not_human_override(self):
        self.wait()
        value = dispatch_text(self.tmp.name)
        bad = [
            dispatch_text(self.tmp.name, task='other-task'),
            dispatch_text('/tmp/wrong-task-pack737'),
            value.replace(f'DONE|{TASK}|{MARKER}|', f'DONE|{TASK}|different737|'),
            '> ' + value, '```\n' + value + '\n```', 'Example:\n' + value,
            'TASK_DISPATCH ' + MARKER,
            value + 'TASK_PACK=/tmp/second/task-pack.json\n',
            value.replace('TASK_PACK=/', 'TASK_PACK=relative/'),
            value.replace('/task-pack.json', '/../different/task-pack.json'),
            '> ' + challenge_text(self.tmp.name),
            challenge_text(self.tmp.name) + ' ACK_NONCE=duplicate737',
        ]
        for index, text in enumerate(bad, 2):
            with self.subTest(text=text):
                self.handle(self.event('UserPromptSubmit', text, offset=index))
                self.assertTrue(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_missing_corrupt_and_mismatched_task_pack_do_not_release_wait(self):
        self.wait()
        path = Path(self.tmp.name) / 'task-pack.json'
        path.unlink()
        self.dispatch()
        self.assertTrue(self.runtime.claude_handshake_wait)
        path.write_text('{broken json', encoding='utf-8')
        self.dispatch(offset=3)
        self.assertTrue(self.runtime.claude_handshake_wait)
        for offset, changes in enumerate([
            {'task_id': 'other-task'}, {'callback': ack_text(nonce='other737')},
            {'callback': ack_text(task='other-task')}, {'completion_nonce': 'other737'},
            {'executor': 'surface:2'}, {'executor_uuid': 'other-surface'},
        ], 4):
            with self.subTest(changes=changes):
                self.write_pack(**changes)
                self.dispatch(offset=offset)
                self.assertTrue(self.runtime.claude_handshake_wait)
        self.write_pack()
        self.dispatch(offset=12)
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_template_callback_and_acked_receipt_wait_for_native_dispatch(self):
        self.wait()
        self.write_pack(callback=f'PREFLIGHT_ACK|{TASK}|<provider>:identity|READY|INLINE|<nonce>')
        self.write_receipt(self.receipt())
        state = self.daemon._apply_claude_runtime_guards('surface-uuid', self.runtime, self.screen())
        self.assertEqual(state.kind, 'claude_handshake_wait')
        self.handle(self.event('Stop', 'Waiting for the task pack', offset=2))
        self.assert_waiting()
        self.handle(self.event('UserPromptSubmit', dispatch_text(self.tmp.name), offset=0.5))
        self.assert_waiting()
        self.handle(self.event('UserPromptSubmit', dispatch_text(self.tmp.name),
                               offset=3, synthetic_fallback=True))
        self.assert_waiting()
        event = self.event('UserPromptSubmit', dispatch_text(self.tmp.name), offset=4)
        event.pop('process_generation')
        self.handle(event)
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assertEqual(self.daemon.claude_event_ledger.status_of(event['event_id']), 'task_dispatch')
        self.assert_no_input()

    def test_template_callback_rejects_missing_corrupt_wrong_and_duplicate_receipts(self):
        self.wait()
        self.write_pack(callback=f'PREFLIGHT_ACK|{TASK}|<provider>:identity|READY|INLINE|<nonce>')
        self.dispatch()
        self.assert_waiting()
        path = Path(self.tmp.name) / 'handshake-receipt.json'
        path.write_text('{broken json', encoding='utf-8')
        self.dispatch()
        self.assert_waiting()
        invalid = [
            [], {'task_id': TASK, 'executors': []},
            {**self.receipt(), 'task_id': 'other-task'},
            {'task_id': TASK, 'executors': self.receipt()['executors'] * 2},
        ]
        invalid.extend(self.receipt(**changes) for changes in [
            {'task_id': 'other-task'}, {'executor': 'surface:2'},
            {'executor_provider': 'codex'}, {'ack_nonce': 'wrong737'},
            {'status': 'PENDING'}, {'lifecycle': 'CREATED'}, {'executor_ack': False},
            {'executor_ack': 1}, {'ack_line': ack_text(nonce='wrong737')},
            {'ack_line_expected': ack_text(nonce='wrong737')},
        ])
        for receipt in invalid:
            with self.subTest(receipt=receipt):
                self.write_receipt(receipt)
                self.dispatch()
                self.assert_waiting()
        self.write_receipt(self.receipt())
        self.dispatch(offset=3)
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_receipt_cannot_authorize_arbitrary_callback_or_draft_pack(self):
        self.wait()
        self.write_receipt(self.receipt())
        for changes in [
            {'callback': ack_text(nonce='wrong737')}, {'callback': ''},
            {'callback': f'PREFLIGHT_ACK|{TASK}|claude:identity|READY|INLINE|<nonce>'},
            {'callback': f'PREFLIGHT_ACK|other-task|<provider>:identity|READY|INLINE|<nonce>'},
            {'draft': True}, {'draft': 'false'},
        ]:
            with self.subTest(changes=changes):
                self.write_pack(**changes)
                self.dispatch()
                self.assert_waiting()
        self.assert_no_input()

    def test_new_human_override_releases_but_old_human_prompt_cannot(self):
        self.wait()
        self.handle(self.event('UserPromptSubmit', 'Please take up a different task', offset=0.5))
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.handle(self.event('UserPromptSubmit', 'Please take up a different task', offset=2))
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.ack(offset=1.5)
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_non_native_or_wrong_identity_human_cannot_release_wait(self):
        self.wait()
        for changes in [
            {'agent_pid': 999}, {'session_id': 'other-session'},
            {'workspace_id': 'other-workspace'}, {'process_generation': 'other-birth'},
            {'synthetic_fallback': True}, {'created_at': time.time() + 120},
        ]:
            with self.subTest(changes=changes):
                self.handle(self.event('UserPromptSubmit', 'Please continue with a new task',
                                       offset=2, **changes))
                self.assertTrue(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_correlated_and_uncorrelated_watchdog_never_release_wait(self):
        self.wait()
        self.handle(self.event('UserPromptSubmit', core.CLAUDE_MESSAGE, offset=2))
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.runtime.claude_last_submit_event_id = 'old-watchdog-submit'
        self.runtime.claude_last_submit_message_hash = protocol._digest(core.CLAUDE_MESSAGE)
        self.runtime.claude_last_submit_session_id = 'session-uuid'
        self.runtime.claude_last_submit_generation = 'fixture-birth-1234'
        self.runtime.claude_last_submit_at = self.at + 2
        self.handle(self.event('UserPromptSubmit', core.CLAUDE_MESSAGE, offset=3))
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_unknown_submission_is_preserved_when_matching_ack_arrives(self):
        self.challenge()
        self.runtime.claude_submit_phase = 'text_written'
        self.runtime.claude_submit_event_id = 'pending-unknown'
        self.runtime.claude_submit_write_unknown = True
        self.ack()
        self.assert_waiting()
        self.daemon._reconcile_claude_submit(self.target, self.runtime, self.composer(), self.client)
        self.assertEqual(self.runtime.claude_submit_phase, 'text_written')
        self.assertEqual(self.runtime.claude_submit_event_id, 'pending-unknown')
        self.assertTrue(self.runtime.claude_submit_write_unknown)
        self.assertEqual(self.runtime.claude_submit_confirmed_at, 0)
        self.assert_no_input()

    def test_invalid_dispatch_cannot_cancel_unknown_submission_during_wait(self):
        self.wait()
        self.runtime.claude_submit_phase = 'text_written'
        self.runtime.claude_submit_event_id = 'pending-unknown'
        self.runtime.claude_submit_write_unknown = True
        self.dispatch(task='wrong-task')
        self.assert_waiting()
        self.assertEqual(self.runtime.claude_submit_phase, 'text_written')
        self.assertEqual(self.runtime.claude_submit_event_id, 'pending-unknown')
        self.assertTrue(self.runtime.claude_submit_write_unknown)
        self.assertEqual(self.runtime.claude_submit_confirmed_at, 0)
        self.assert_no_input()

    def test_same_challenge_replay_keeps_wait_new_challenge_replaces_it(self):
        self.wait()
        self.challenge(offset=2)
        self.assert_waiting()
        self.challenge(offset=3, task='new-task737', nonce='newnonce737')
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assertEqual(self.runtime.claude_handshake_challenge['task_id'], 'new-task737')
        self.client.payload = claude_grid_payload(spinner='✶ Thinking… (3s · ↓ 15 tokens)')
        self.ack(offset=4)
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.client.payload = claude_grid_payload(lines=['Waiting for task pack'], completed=True)
        self.ack(offset=5, task='new-task737', nonce='newnonce737')
        self.assert_waiting()
        self.assert_no_input()

    def test_late_ack_cannot_relock_business_after_dispatch(self):
        self.wait()
        self.dispatch()
        self.ack(offset=1.5)
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_direct_text_and_enter_including_external_check_are_blocked(self):
        self.wait()
        result = self.daemon._send_claude_event(
            self.event('Stop', 'unfinished', offset=2), self.target, self.runtime, self.client)
        self.assertFalse(result[0])
        self.composer()
        for check in [None, lambda: True]:
            with self.subTest(external_check=check is not None):
                self.assertFalse(self.daemon._send_claude_enter(
                    self.target, self.runtime, self.client, reason='offline-guard', input_check=check))
        self.assert_no_input()

    def test_reconcile_orphan_deferred_fallback_and_completion_reopen_cannot_send(self):
        self.wait()
        state = self.composer()
        self.daemon._reconcile_claude_submit(self.target, self.runtime, state, self.client)
        self.daemon._recover_orphan_watchdog_submit(self.target, self.runtime, state, self.client)
        self.assert_waiting()
        self.assertEqual(self.runtime.claude_orphan_enter_count, 0)
        event = self.event('Stop', 'unfinished', offset=2)
        self.runtime.claude_deferred_event = event
        self.runtime.claude_deferred_since = self.at + 2
        self.daemon._maybe_send_deferred_claude_stop(self.target, self.runtime, state, self.client)
        self.assertFalse(self.daemon._reopen_claude_completion_from_hook(self.runtime, event, self.target))
        self.daemon._claude_hook_config_health = {'healthy': True}
        self.client.payload = claude_grid_payload(lines=['Unfinished task'], completed=True)
        state = self.screen()
        observation = {'agent_kind': 'claude', 'pid': 1234, 'generation': 'fixture-birth-1234'}
        for _ in range(3):
            self.daemon._maybe_send_claude_hook_gap_fallback(
                self.target, self.runtime, state, observation, self.client)
        self.assert_waiting()
        self.assert_no_input()

    def test_wait_established_after_preflight_still_blocks_text(self):
        self.challenge()
        snapshot = self.daemon._claude_event_snapshot
        count = 0

        def raced_snapshot(*args, **kwargs):
            nonlocal count
            result = snapshot(*args, **kwargs)
            count += 1
            if count == 2:
                self.ack()
            return result

        with mock.patch.object(self.daemon, '_claude_event_snapshot', side_effect=raced_snapshot):
            sent, _ = self.daemon._send_claude_event(
                self.event('Stop', 'unfinished', offset=2), self.target, self.runtime, self.client)
        self.assertEqual(count, 2)
        self.assertFalse(sent)
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.assert_no_input()

    def test_enter_transport_guard_rechecks_wait_with_external_input_check(self):
        self.challenge()
        self.composer()

        @contextlib.contextmanager
        def input_guard(check):
            self.ack()
            if not check():
                raise core.InputNotSentError('offline race: handshake ACK arrived')
            yield

        self.client.input_guard = input_guard
        sent = self.daemon._send_claude_enter(
            self.target, self.runtime, self.client, reason='offline-race', input_check=lambda: True)
        self.assertFalse(sent)
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.assert_no_input()


if __name__ == '__main__':
    unittest.main()

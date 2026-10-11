"""Legacy dispatch recovery against private, original Hook/native/ledger files.

Only FakeClient sees Text/Enter. These tests do not start or read live agents.
"""
import copy
import dataclasses
import datetime as dt
import json
import unittest
from pathlib import Path
from unittest import mock

import claude_dispatch_recovery as recovery
import cmux_codex_watch as core
import tests.test_claude_handshake_wait as helpers


class DispatchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = helpers.HandshakeWaitTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = Path(self.f.tmp.name)
        self.daemon, self.runtime = self.f.daemon, self.f.runtime
        self.target, self.client = self.f.target, self.f.client
        self.journal = self.daemon.claude_event_inbox.journal_path
        self.assertTrue(self.journal.is_relative_to(self.root), self.journal)
        self.projects = self.root / 'projects'
        self.native = self.projects / 'fixture-project' / 'session-uuid.jsonl'
        self.native.parent.mkdir(parents=True)
        self.native.touch()
        patcher = mock.patch.object(recovery, 'PROJECTS_DIR', self.projects)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.daemon._claude_hook_config_health = {'healthy': True}
        self.observation = {'agent_kind': 'claude', 'pid': 1234,
                            'generation': 'fixture-birth-1234'}
        self.events = []

    def append(self, kind, content, offset=4, **extra):
        row = {'type': kind, 'sessionId': 'session-uuid', 'isSidechain': False,
               'uuid': f'native-{self.native.stat().st_size}',
               'timestamp': dt.datetime.fromtimestamp(self.f.at + offset, dt.timezone.utc).isoformat(),
               'message': {'role': kind, 'content': content}, **extra}
        with self.native.open('a') as stream:
            stream.write(json.dumps(row) + '\n')
        return row

    def record(self, event):
        self.f.handle(event)
        self.events.append(copy.deepcopy(event))
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open('a') as stream:
            stream.write(json.dumps(event) + '\n')
        return event

    def history(self, build=helpers.business_dispatch, end='Stop'):
        self.f.write_receipt(self.f.receipt())
        challenge = self.record(self.f.event('UserPromptSubmit', helpers.challenge_text(str(self.root))))
        self.append('user', helpers.challenge_text(str(self.root)), 0)
        self.record(self.f.event('Stop', helpers.ack_text(), offset=1))
        self.append('assistant', helpers.ack_text(), 1)
        text = build(str(self.root))
        event = self.f.event('UserPromptSubmit', text, offset=2,
                             transcript_id=recovery.digest(str(self.native)))
        self.assertIsNotNone(event['task_dispatch'])
        event['task_dispatch'] = None  # Original parser's persisted rejection.
        self.dispatch = self.record(event)
        self.append('user', text, 2)
        if end == 'Stop':
            self.append('assistant', 'Unfinished task', 3)
            self.record(self.f.event('Stop', 'Unfinished task', offset=3))
        elif end == 'StopFailure':
            self.record(self.f.event('StopFailure', offset=3, error_kind='claude_429'))
        elif end == 'compact':
            self.append('user', 'Native compact summary', 3,
                        isCompactSummary=True, isVisibleInTranscriptOnly=True)
            self.record(self.f.event('SessionStart', offset=3, source='compact'))
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.assertEqual(self.daemon.claude_event_ledger.status_of(event['event_id']),
                         'suppressed_handshake_wait')
        self.assertEqual(self.runtime.claude_turn_started_at, challenge['created_at'])
        self.original_journal = self.journal.read_bytes()
        self.original_rows = {e['event_id']: copy.deepcopy(self.daemon.claude_event_ledger.events[e['event_id']])
                              for e in self.events}
        self.original_binding = self.daemon._claude_dispatch_binding(self.runtime)

    def recover(self):
        state = self.f.screen()
        return self.daemon._apply_claude_runtime_guards('surface-uuid', self.runtime, state)

    def deferred(self, state=None):
        return self.daemon._maybe_send_deferred_claude_stop(
            self.target, self.runtime, state or self.f.screen(), self.client)

    def gap(self, state=None):
        return self.daemon._maybe_send_claude_hook_gap_fallback(
            self.target, self.runtime, state or self.f.screen(), self.observation, self.client)

    def no_input(self):
        self.assertEqual(self.client.sent_text, [])
        self.assertEqual(self.client.sent_keys, [])

    def originals_unchanged(self):
        self.assertEqual(self.journal.read_bytes(), self.original_journal)
        for key, value in self.original_rows.items():
            self.assertEqual(self.daemon.claude_event_ledger.events[key], value)

    def assert_one_resume(self):
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(len(self.client.sent_keys), 1)
        self.assertEqual(self.client.sent_keys[0][-1], 'enter')

    def test_business_dispatch_recovers_original_stop_once_without_rewriting_history(self):
        self.history()
        state = self.recover()
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.no_input()
        self.assertEqual(self.daemon._claude_dispatch_binding(self.runtime), self.original_binding)
        self.assertTrue(self.deferred(state))
        self.deferred(state)
        self.gap(state)
        self.assert_one_resume()
        self.originals_unchanged()

    def test_skill_first_dispatch_recovers_original_stopfailure_once(self):
        self.history(helpers.skill_first_dispatch, 'StopFailure')
        self.recover()
        self.assertTrue(self.deferred())
        self.no_input()
        self.assertFalse(self.runtime.claude_deferred_event)
        with mock.patch.object(core.time, 'time', return_value=self.runtime.claude_provider_guard['retry_after'] + 1):
            self.recover()
            self.assertTrue(self.deferred())
            self.deferred()
        self.assert_one_resume()
        self.originals_unchanged()

    def test_compact_without_stop_uses_two_stable_frames_and_one_durable_id(self):
        self.history(end='compact')
        state = self.recover()
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assertFalse(self.gap(state))
        self.no_input()
        self.assertTrue(self.gap(state))
        self.gap(state)
        self.assert_one_resume()
        self.originals_unchanged()

    def test_only_dispatch_without_stop_recovers_when_stopped(self):
        self.history(end=None)
        self.recover()
        self.gap()
        self.gap()
        self.assert_one_resume()

    def test_work_retry_reconnect_compact_and_draft_never_release_wait(self):
        self.history(end='compact')
        raw = self.f.screen()
        for kind, activity in [('working', None), ('working', 'retry'),
                               ('working', 'reconnect'), ('working', 'compact'),
                               ('claude_stopped', 'retry'), ('menu', None),
                               ('compose_pending', None), ('incompatible', None)]:
            with self.subTest(kind=kind, activity=activity):
                state = dataclasses.replace(raw, kind=kind, claude_native_activity=activity)
                self.daemon._apply_claude_runtime_guards('surface-uuid', self.runtime, state)
                self.assertTrue(self.runtime.claude_handshake_wait)
                self.no_input()

    def test_pause_disable_unknown_and_identity_change_refuse_recovery(self):
        self.history()
        for owner, key, value in [(self.daemon.config, 'global_paused', True),
                                   (self.daemon.config, 'mode', 'observe'),
                                   (self.daemon.config, 'claude_enabled', False),
                                   (self.target, 'paused', True), (self.target, 'enabled', False)]:
            with self.subTest(key=key), mock.patch.dict(owner, {key: value}):
                self.recover()
                self.assertTrue(self.runtime.claude_handshake_wait)
        for key, value in [('claude_submit_write_unknown', True), ('claude_submit_phase', 'text_sent'),
                           ('claude_completed_latched', True), ('claude_process_pid', 9999),
                           ('claude_process_generation', 'other-birth'), ('claude_session_id', 'other-session')]:
            with self.subTest(key=key), mock.patch.object(self.runtime, key, value):
                self.recover()
                self.assertTrue(self.runtime.claude_handshake_wait)
        self.no_input()
        self.recover()
        self.assertFalse(self.runtime.claude_handshake_wait)

    def test_transient_identity_inspection_failure_can_recover_next_poll(self):
        self.history()
        with mock.patch.object(core, 'inspect_claude_process', return_value={}):
            self.recover()
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.recover()
        self.assertFalse(self.runtime.claude_handshake_wait)

    def test_structural_failure_cache_rechecks_changed_task_receipt_immediately(self):
        self.history()
        self.f.write_receipt(self.f.receipt(status='FAIL'))
        self.recover()
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.f.write_receipt(self.f.receipt())
        self.recover()
        self.assertFalse(self.runtime.claude_handshake_wait)

    def test_new_native_prompt_rejects_old_dispatch(self):
        self.history()
        self.append('user', 'A new task')
        self.recover()
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.no_input()

    def test_new_mixed_user_prompt_rejects_old_dispatch(self):
        self.history()
        self.append('user', [{'type': 'tool_result', 'tool_use_id': 't', 'content': 'old'},
                             {'type': 'text', 'text': 'A new task'}])
        self.recover()
        self.assertTrue(self.runtime.claude_handshake_wait)

    def test_new_completion_rejects_old_dispatch(self):
        self.history()
        self.append('assistant', '完成，建议检查 usage: /context')
        self.recover()
        self.assertTrue(self.runtime.claude_handshake_wait)

    def test_mixed_assistant_completion_rejects_old_dispatch(self):
        self.history()
        self.append('assistant', [{'type': 'thinking', 'thinking': 'Finished'},
                                  {'type': 'text', 'text': '完成，建议检查 usage: /context'}])
        self.recover()
        self.assertTrue(self.runtime.claude_handshake_wait)

    def test_compact_marker_alone_does_not_hide_new_user_text(self):
        self.history()
        self.append('user', 'New input with incomplete compact flags', isCompactSummary=True)
        self.recover()
        self.assertTrue(self.runtime.claude_handshake_wait)

    def test_legal_same_inode_append_is_reproved_before_sending(self):
        self.history()
        self.recover()
        before = copy.deepcopy(self.runtime.claude_dispatch_recovery['files'][0])
        self.append('assistant', 'Still unfinished')
        self.assertTrue(self.deferred())
        self.assert_one_resume()
        after = self.runtime.claude_dispatch_recovery['files'][0]
        self.assertEqual(before['stat'][:2], after['stat'][:2])
        self.assertGreater(after['stat'][2], before['stat'][2])
        self.originals_unchanged()

    def test_prefix_mutation_refuses_same_inode_refresh(self):
        self.history()
        self.recover()
        data = self.native.read_bytes().replace(b'Native', b'Changed', 1)
        # Ensure a prefix change even when this fixture has no compact summary.
        data = data.replace(b'legitimate', b'counterfeit', 1)
        self.native.write_bytes(data)
        self.assertFalse(self.deferred())
        self.no_input()

    def test_replaced_or_symlink_transcript_refuses_refresh(self):
        self.history()
        self.recover()
        other = self.native.with_suffix('.replacement')
        self.native.rename(other)
        self.native.write_bytes(other.read_bytes())
        self.assertFalse(self.deferred())
        self.native.unlink()
        self.native.symlink_to(other)
        self.assertFalse(self.deferred())
        self.no_input()

    def test_pack_or_receipt_rewrite_refuses_refresh(self):
        self.history()
        self.recover()
        self.f.write_receipt(self.f.receipt())
        self.assertFalse(self.deferred())
        self.no_input()

    def test_new_native_prompt_after_release_cannot_be_reproved(self):
        self.history()
        self.recover()
        self.append('user', 'A new owner task')
        self.assertFalse(self.deferred())
        self.no_input()

    def test_working_deferral_keeps_only_stop_for_later_safe_frame(self):
        self.history()
        self.recover()
        event = copy.deepcopy(self.runtime.claude_deferred_event)
        self.append('assistant', 'Additional progress')
        busy = dataclasses.replace(self.f.screen(), kind='working', claude_native_activity='retry')
        self.assertFalse(self.deferred(busy))
        self.assertEqual(self.runtime.claude_deferred_event, event)
        self.no_input()
        self.assertTrue(self.deferred())
        self.assert_one_resume()

    def test_no_original_dispatch_evidence_keeps_ack_waiting(self):
        self.f.write_receipt(self.f.receipt())
        self.record(self.f.event('UserPromptSubmit', helpers.challenge_text(str(self.root))))
        self.record(self.f.event('Stop', helpers.ack_text(), offset=1))
        self.recover()
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.no_input()

    def test_original_ledger_outcome_changed_refuses_recovery(self):
        self.history()
        self.daemon.claude_event_ledger.mark(self.dispatch, 'reserved')
        self.recover()
        self.assertTrue(self.runtime.claude_handshake_wait)
        self.no_input()

    def test_consumed_resume_rows_survive_stale_state_reload_without_replay(self):
        self.history()
        old_runtime = dataclasses.asdict(self.runtime)
        self.recover()
        proof = copy.deepcopy(self.runtime.claude_dispatch_recovery)
        event = self.daemon._claude_dispatch_resume_event(proof)
        for status in ('reserved', 'sent', 'unknown', 'submit_unknown'):
            with self.subTest(status=status):
                self.daemon.claude_event_ledger.mark(event, status)
                restored = core.TargetRuntime(**copy.deepcopy(old_runtime))
                self.daemon.runtime['surface-uuid'] = self.runtime = restored
                self.f.runtime = restored
                self.recover()
                self.deferred()
                self.gap()
                self.gap()
                self.assertFalse(self.runtime.claude_handshake_wait)
                self.assertFalse(self.runtime.claude_deferred_event)
                self.no_input()

    def test_gap_consumed_rows_cannot_get_new_fallback_id(self):
        self.history(end='compact')
        self.recover()
        event = self.daemon._claude_dispatch_resume_event(self.runtime.claude_dispatch_recovery)
        for status in ('reserved', 'sent', 'unknown'):
            with self.subTest(status=status):
                self.daemon.claude_event_ledger.mark(event, status)
                self.gap()
                self.gap()
                self.no_input()

    def test_new_hook_prompt_takes_ownership_and_clears_proof(self):
        self.history()
        self.recover()
        self.record(self.f.event('UserPromptSubmit', 'A new real task', offset=5))
        self.assertFalse(self.runtime.claude_dispatch_recovery)
        self.assertFalse(self.runtime.claude_deferred_event)
        self.no_input()

    def test_journal_append_before_text_blocks_without_refresh_in_write_guard(self):
        self.history()
        self.recover()
        original = self.client.read_screen
        def changed(*args):
            result = original(*args)
            self.append('user', 'User took control during preflight')
            return result
        with mock.patch.object(self.client, 'read_screen', side_effect=changed):
            self.deferred()
        self.no_input()

    def test_native_append_after_text_blocks_enter_without_retyping(self):
        self.history()
        self.recover()
        original = self.client.send_text
        def changed(*args):
            original(*args)
            self.append('assistant', 'Additional native progress during Text')
        with mock.patch.object(self.client, 'send_text', side_effect=changed):
            self.deferred()
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(self.client.sent_keys, [])
        self.deferred()
        self.gap()
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(self.client.sent_keys, [])

    def test_native_append_during_enter_composer_read_blocks_second_write(self):
        self.history()
        self.recover()
        original = self.client.read_screen
        def changed(*args):
            result = original(*args)
            if self.client.sent_text:
                self.append('user', 'New user task at Enter boundary')
            return result
        with mock.patch.object(self.client, 'read_screen', side_effect=changed):
            self.deferred()
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(self.client.sent_keys, [])


if __name__ == '__main__':
    unittest.main()

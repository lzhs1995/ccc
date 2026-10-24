"""Report diagnostics never replace the user's task-completion contract."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import claude_ccc_protocol as protocol
import cmux_codex_watch as core
from tests.test_watch import (FakeClient, armed_daemon, bind_fake_claude_process_identity, claude_grid_payload,
                              claude_hook_event, claude_idle_screen, process_fixture)

TASK = 'ccc-daemon-discovery-review-20261007-711'
REPORT = ('STATUS: REPORT_READY TASK_ID=' + TASK + ' CALLBACK_UNCONFIRMED '
          'REPORT=/tmp/review711/executor-report.md supervisor_reconciliation_required')


def wrapped(value, width=78):
    return [value[i:i + width] for i in range(0, len(value), width)]


class ReportReadyTests(unittest.TestCase):
    def setUp(self):
        settle = mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0)
        settle.start()
        self.addCleanup(settle.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = FakeClient(
            claude_grid_payload(lines=wrapped(REPORT), completed=True),
            text=REPORT + '\n' + claude_idle_screen(),
            top=process_fixture(('surface-uuid', 'claude')))
        self.daemon = armed_daemon(self.tmp.name, self.client)
        self.daemon.config['claude_enabled'] = True
        self.runtime = self.daemon.runtime.setdefault('surface-uuid', core.TargetRuntime())
        self.runtime.claude_process_pid = 1234
        self.runtime.claude_process_generation = 'fixture-birth-1234'
        self.runtime.claude_session_id = 'session-uuid'
        self.runtime.claude_hook_health = 'healthy'
        self.target = self.daemon.config['targets'][0]
        self.at = time.time() - 10

    def event(self, name='report', **changes):
        event = claude_hook_event(name, created_at=self.at)
        event.update(agent_pid=1234, process_generation='fixture-birth-1234',
                     report_ready_task_id=TASK)
        event.update(changes)
        return event

    def handle(self, event):
        self.daemon._handle_claude_event(event, self.client)

    def screen(self, **kwargs):
        # These chrome variants occupy the same grid row; do not overwrite
        # a live spinner/error with the stopped-turn timing footer.
        kwargs.setdefault('completed', not (kwargs.get('spinner') or kwargs.get('error')))
        payload = claude_grid_payload(lines=wrapped(REPORT), **kwargs)
        return core.classify_claude_grid(core.Grid.from_rpc(payload, 'surface-uuid'))

    def assert_no_input(self):
        self.assertEqual(self.client.sent_text, [])
        self.assertEqual(self.client.sent_keys, [])

    def test_protocol_extracts_exact_declaration_without_claiming_completion(self):
        for text in [REPORT, '\n'.join(wrapped(REPORT))]:
            with self.subTest(text=text):
                self.assertEqual(protocol.report_ready_task(text), TASK)
                event = protocol.build_event({'hook_event_name': 'Stop', 'last_assistant_message': text})
                self.assertEqual(event['report_ready_task_id'], TASK)
                self.assertFalse(event['completed'])

    def test_protocol_rejects_quotes_partial_and_additional_prose(self):
        for text in ['> ' + REPORT, '```\n' + REPORT + '\n```', 'Example: ' + REPORT,
                     REPORT + '\nStill working', REPORT.replace('CALLBACK_UNCONFIRMED', 'CALLBACK_CONFIRMED'),
                     REPORT.replace('TASK_ID=', 'TASK='), REPORT.split(' REPORT=')[0]]:
            with self.subTest(text=text):
                self.assertIsNone(protocol.report_ready_task(text))

    def test_stop_failure_does_not_inherit_report_from_old_assistant_output(self):
        event = protocol.build_event({'hook_event_name': 'StopFailure',
            'last_assistant_message': REPORT, 'error': '429 rate limit exceeded'})
        self.assertNotIn('report_ready_task_id', event)
        self.assertEqual(event['error_kind'], 'claude_429')

    def test_protocol_accepts_final_declaration_after_explanation(self):
        for separator in ['\n', '\n\n']:
            message = 'Report frozen; callback remains unconfirmed.' + separator + REPORT
            self.assertEqual(protocol.report_ready_task(message), TASK)
            event = protocol.build_event({'hook_event_name': 'Stop',
                                          'last_assistant_message': message})
            self.assertEqual(event['report_ready_task_id'], TASK)
            self.assertFalse(event['completed'])

    def test_final_declaration_must_be_outside_code_or_quote(self):
        for prefix in ['```text\n\n', '~~~~\n\n', '> ', '    ', '\t']:
            with self.subTest(prefix=prefix):
                self.assertIsNone(protocol.report_ready_task('Explanation\n' + prefix + REPORT))
        self.assertEqual(protocol.report_ready_task('```\nexample\n```\n' + REPORT), TASK)

    def test_unfinished_report_stop_continues_once_without_claiming_completion(self):
        self.handle(self.event())
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertNotEqual(self.runtime.state, 'claude_report_ready')
        self.assertEqual(self.runtime.claude_report_ready_task_id, TASK)
        self.assertEqual(self.daemon.claude_event_ledger.status_of('report'), 'sent')
        self.assertEqual(self.runtime.claude_submit_confirmed_at, 0)
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(len(self.client.sent_keys), 1)
        self.handle(self.event())
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(len(self.client.sent_keys), 1)

    def test_report_stop_hook_reentry_is_deferred_until_safe(self):
        self.handle(self.event('reentry', stop_hook_active=True))
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertEqual(self.runtime.claude_deferred_event['event_id'], 'reentry')
        self.assertEqual(self.runtime.claude_deferred_reason, 'active_stop_hook')
        self.assertEqual(self.daemon.claude_event_ledger.status_of('reentry'), 'deferred_active_stop_hook')
        self.assert_no_input()

    def test_report_diagnostics_and_send_deduplication_survive_daemon_restart(self):
        self.handle(self.event())
        self.daemon.save()
        restored = bind_fake_claude_process_identity(core.WatchDaemon(
            Path(self.tmp.name) / 'config.json', Path(self.tmp.name) / 'state.json',
            client=self.client), self.client)
        runtime = restored.runtime['surface-uuid']
        state = restored._apply_claude_runtime_guards('surface-uuid', runtime,
                   core.ScreenState('claude_stopped', message_kind='claude'))
        self.assertNotIn(state.kind, {'claude_report_ready', 'claude_completed'})
        self.assertEqual(runtime.claude_report_ready_task_id, TASK)
        self.assertFalse(runtime.claude_completed_latched)
        restored._handle_claude_event(self.event(), self.client)
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(len(self.client.sent_keys), 1)

    def test_new_human_prompt_reopens_and_late_old_report_cannot_close_it(self):
        self.handle(self.event())
        self.handle(self.event('human', event_name='UserPromptSubmit', prompt_kind='human',
                               created_at=self.at + 2))
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertIsNone(self.runtime.claude_report_ready_task_id)
        self.daemon.save()
        self.daemon = core.WatchDaemon(Path(self.tmp.name) / 'config.json',
                                      Path(self.tmp.name) / 'state.json', client=self.client)
        self.runtime = self.daemon.runtime['surface-uuid']
        self.handle(self.event('late', created_at=self.at + 1))
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertEqual(self.daemon.claude_event_ledger.status_of('late'), 'stale_native_event')
        self.assertEqual(self.runtime.claude_turn_started_at, self.at + 2)

    def test_new_task_stops_normally_and_can_continue_once(self):
        self.handle(self.event())
        self.assertEqual(len(self.client.sent_text), 1)
        self.handle(self.event('human', event_name='UserPromptSubmit', prompt_kind='human',
                               created_at=self.at + 1))
        self.client.payload = claude_grid_payload(lines=['New task unfinished'], completed=True)
        self.client.text = 'New task unfinished\n' + claude_idle_screen()
        with mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0):
            self.handle(self.event('new-stop', report_ready_task_id=None, created_at=self.at + 2))
        self.assertEqual(len(self.client.sent_text), 2)
        self.assertEqual(len(self.client.sent_keys), 2)

    def test_pending_unknown_submit_stays_unknown_without_text_or_enter_retries(self):
        self.runtime.claude_submit_phase = 'text_written'
        self.runtime.claude_submit_event_id = 'pending'
        self.runtime.claude_submit_write_unknown = True
        self.handle(self.event())
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertEqual(self.daemon.claude_event_ledger.status_of('report'), 'submit_duplicate_suppressed')
        self.assertTrue(self.daemon._reconcile_claude_submit(
            self.target, self.runtime, self.screen(), self.client))
        self.assertEqual(self.runtime.claude_submit_phase, 'text_written')
        self.assertEqual(self.runtime.claude_submit_event_id, 'pending')
        self.assertTrue(self.runtime.claude_submit_write_unknown)
        self.assertEqual(self.runtime.claude_submit_confirmed_at, 0)
        self.assert_no_input()

    def test_wrong_process_or_birth_cannot_latch_report(self):
        for name, changes in [('pid', {'agent_pid': 999}),
                              ('birth', {'process_generation': 'other'})]:
            with self.subTest(name=name):
                self.handle(self.event(name, **changes))
                self.assertFalse(self.runtime.claude_completed_latched)
        self.assert_no_input()

    def test_new_session_clears_report_and_same_session_start_preserves_it(self):
        self.daemon.config['claude_enabled'] = False
        self.handle(self.event())
        self.handle(self.event('same', event_name='SessionStart'))
        self.assertEqual(self.runtime.claude_report_ready_task_id, TASK)
        self.handle(self.event('new', event_name='SessionStart', session_id='new-session'))
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertIsNone(self.runtime.claude_report_ready_task_id)

    def test_verified_root_session_rebind_does_not_inherit_old_report(self):
        self.daemon.config['claude_enabled'] = False
        self.handle(self.event())
        self.assert_no_input()
        self.daemon.config['claude_enabled'] = True
        self.client.payload = claude_grid_payload(lines=['New session unfinished'], completed=True)
        self.client.text = 'New session unfinished\n' + claude_idle_screen()
        with mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0):
            self.handle(self.event('new-root-stop', session_id='new-session',
                                   report_ready_task_id=None, created_at=self.at + 2))
        self.assertIsNone(self.runtime.claude_report_ready_task_id)
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertEqual(len(self.client.sent_text), 1)

    def test_nonfinite_or_future_report_cannot_close_turn(self):
        for index, stamp in enumerate([float('nan'), float('inf'), time.time() + 120]):
            self.handle(self.event('bad-time-' + str(index), created_at=stamp))
            self.assertFalse(self.runtime.claude_completed_latched)
        self.assert_no_input()

    def test_visible_report_is_not_a_completion_or_a_barrier_to_verified_stop(self):
        for health in ['healthy', 'missing', 'historical', 'legacy_override']:
            with self.subTest(health=health):
                self.runtime.claude_hook_health = health
                state = self.daemon._apply_claude_runtime_guards('surface-uuid', self.runtime, self.screen())
                self.assertNotIn(state.kind, {'claude_report_ready', 'claude_completed'})
        self.assert_no_input()
        with mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0):
            self.handle(self.event('old-protocol-stop', report_ready_task_id=None))
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(len(self.client.sent_keys), 1)

    def test_new_content_or_quoted_report_is_not_a_closeout(self):
        for lines in [wrapped('> ' + REPORT), wrapped(REPORT) + ['', 'Still implementing'],
                      wrapped(REPORT) + ['', '❯ new human task', 'New result pending']]:
            with self.subTest(lines=lines):
                grid = core.Grid.from_rpc(claude_grid_payload(lines=lines, completed=True), 'surface-uuid')
                self.assertEqual(core.classify_claude_grid(grid).kind, 'claude_stopped')

    def test_report_with_rendered_recap_does_not_block_verified_stop(self):
        lines = ['⏺ Report frozen. Waiting for the original callback.', '']
        lines += wrapped(REPORT)
        lines += ['', '✻ Churned for 4m 53s', '',
                  '※ recap: Review complete; receipt still pending.',
                  '  Wait for reconciliation. (disable recaps in /config)']
        self.client.payload = claude_grid_payload(lines=lines, completed=False)
        self.client.text = '\n'.join(lines) + '\n' + claude_idle_screen()
        state = core.classify_claude_grid(core.Grid.from_rpc(self.client.payload, 'surface-uuid'))
        self.assertEqual(state.kind, 'claude_stopped')
        with mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0):
            self.handle(self.event('legacy-recap-stop', report_ready_task_id=None))
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(len(self.client.sent_keys), 1)

    def test_genuine_completion_outranks_legacy_report_and_keeps_original_record(self):
        old = self.event('old-report', created_at=self.at - 1)
        self.daemon.claude_event_ledger.mark(old, 'report_ready')
        self.runtime.claude_report_ready_task_id = TASK
        self.runtime.claude_completed_latched = True
        self.runtime.claude_completion_event_id = 'old-report'
        self.handle(self.event('actual-completion', completed=True))
        self.assertTrue(self.runtime.claude_completed_latched)
        self.assertEqual(self.runtime.state, 'claude_completed')
        self.assertEqual(self.runtime.claude_completion_event_id, 'actual-completion')
        self.assertEqual(self.daemon.claude_event_ledger.status_of('old-report'), 'report_ready')
        self.assertEqual(self.daemon.claude_event_ledger.status_of('actual-completion'), 'completed')
        self.assert_no_input()

    def test_complete_report_has_same_meaning_in_native_hook_and_terminal(self):
        completed = REPORT + '\n完成，建议检查 usage: /context'
        event = protocol.build_event({'hook_event_name': 'Stop', 'last_assistant_message': completed})
        self.assertTrue(event['completed'])
        grid = core.Grid.from_rpc(claude_grid_payload(
            lines=wrapped(REPORT) + ['完成，建议检查 usage: /context'], completed=True), 'surface-uuid')
        self.assertEqual(core.classify_claude_grid(grid).kind, 'claude_completed')

    def test_completion_sentence_cannot_be_negated_quoted_or_only_a_suffix(self):
        phrase = '完成，建议检查 usage: /context'
        for text in ['未' + phrase, '还未' + phrase, '建议检查 usage: /context',
                     '> ' + phrase, '    ' + phrase, '```\n' + phrase + '\n```',
                     '```\n\n' + phrase, '"' + phrase + '"', phrase + '\n还要继续修复']:
            with self.subTest(text=text):
                self.assertFalse(protocol.completion_reported(text))
                grid = core.Grid.from_rpc(claude_grid_payload(
                    lines=text.splitlines(), completed=True), 'surface-uuid')
                self.assertNotEqual(core.classify_claude_grid(grid).kind, 'claude_completed')

    def test_recap_cannot_supply_report_or_hide_new_body_or_prompt(self):
        recap = ['※ recap: Review complete. (disable recaps in /config)']
        for lines in [
            ['Unfinished', '✻ Worked for 1s'] + wrapped('※ recap: ' + REPORT + ' (disable recaps in /config)'),
            wrapped(REPORT) + ['', 'New work pending', '✻ Worked for 1s'] + recap,
            wrapped(REPORT) + ['', '❯ new human task', '✻ Worked for 1s'] + recap,
            wrapped(REPORT) + ['', '※ recap: Unrecognized body without timing or terminator'],
            ['```text', ''] + wrapped(REPORT),
            ['~~~~', ''] + wrapped(REPORT),
            ['Thought for 2s', '', '⏺ ```text', ''] + wrapped(REPORT),
            ['    ' + line for line in wrapped(REPORT)],
        ]:
            with self.subTest(lines=lines):
                grid = core.Grid.from_rpc(claude_grid_payload(lines=lines, completed=False), 'surface-uuid')
                self.assertNotEqual(core.classify_claude_grid(grid).kind, 'claude_report_ready')

    def test_spinner_draft_menu_and_retry_have_priority_over_report(self):
        for kwargs, expected in [({'spinner': '✶ Thinking… (3s · ↓ 15 tokens)'}, 'working'),
                                 ({'composer': 'busy'}, 'composer_busy'),
                                 ({'question': True}, 'menu'),
                                 ({'error': 'API Error: 429 rate limit exceeded'}, 'recoverable_error'),
                                 ({'error': 'API error: 429 · Retrying in 2s · attempt 2/10'}, 'working')]:
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.screen(**kwargs).kind, expected)


if __name__ == '__main__':
    unittest.main()

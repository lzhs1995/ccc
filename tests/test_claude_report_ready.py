"""Closed executor reports must not become endless CCC continuation prompts."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import claude_ccc_protocol as protocol
import cmux_codex_watch as core
from tests.test_watch import (FakeClient, armed_daemon, claude_grid_payload,
                              claude_hook_event, claude_idle_screen, process_fixture)

TASK = 'ccc-daemon-discovery-review-20261007-711'
REPORT = ('STATUS: REPORT_READY TASK_ID=' + TASK + ' CALLBACK_UNCONFIRMED '
          'REPORT=/tmp/review711/executor-report.md supervisor_reconciliation_required')


def wrapped(value, width=78):
    return [value[i:i + width] for i in range(0, len(value), width)]


class ReportReadyTests(unittest.TestCase):
    def setUp(self):
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

    def test_hook_latches_report_pending_not_delivered(self):
        self.handle(self.event())
        self.assertTrue(self.runtime.claude_completed_latched)
        self.assertEqual(self.runtime.state, 'claude_report_ready')
        self.assertEqual(self.runtime.claude_report_ready_task_id, TASK)
        self.assertEqual(self.daemon.claude_event_ledger.status_of('report'), 'report_ready')
        self.assertEqual(self.runtime.claude_submit_confirmed_at, 0)
        self.assert_no_input()

    def test_stop_hook_reentry_cannot_reopen_report(self):
        self.handle(self.event())
        self.handle(self.event('reentry', report_ready_task_id=None,
                               stop_hook_active=True, created_at=self.at + 1))
        self.assertEqual(self.runtime.state, 'claude_report_ready')
        self.assertEqual(self.daemon.claude_event_ledger.status_of('reentry'), 'suppressed_report_ready')
        self.assert_no_input()

    def test_report_survives_daemon_restart(self):
        self.handle(self.event())
        self.daemon.save()
        restored = core.WatchDaemon(Path(self.tmp.name) / 'config.json',
                                   Path(self.tmp.name) / 'state.json', client=self.client)
        runtime = restored.runtime['surface-uuid']
        state = restored._apply_claude_runtime_guards('surface-uuid', runtime,
                   core.ScreenState('claude_stopped', message_kind='claude'))
        self.assertEqual(state.kind, 'claude_report_ready')
        self.assertEqual(runtime.claude_report_ready_task_id, TASK)
        self.assert_no_input()

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
        self.assertEqual(self.daemon.claude_event_ledger.status_of('late'), 'stale_report_ready')

    def test_new_task_stops_normally_and_can_continue_once(self):
        self.handle(self.event())
        self.handle(self.event('human', event_name='UserPromptSubmit', prompt_kind='human',
                               created_at=self.at + 1))
        self.client.payload = claude_grid_payload(lines=['New task unfinished'], completed=True)
        self.client.text = 'New task unfinished\n' + claude_idle_screen()
        with mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0):
            self.handle(self.event('new-stop', report_ready_task_id=None, created_at=self.at + 2))
        self.assertEqual(len(self.client.sent_text), 1)

    def test_pending_unknown_submit_stays_unknown_without_text_or_enter_retries(self):
        self.runtime.claude_submit_phase = 'text_written'
        self.runtime.claude_submit_event_id = 'pending'
        self.runtime.claude_submit_write_unknown = True
        self.handle(self.event())
        self.assertEqual(self.runtime.state, 'claude_report_ready')
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
        self.handle(self.event())
        self.handle(self.event('same', event_name='SessionStart'))
        self.assertEqual(self.runtime.claude_report_ready_task_id, TASK)
        self.handle(self.event('new', event_name='SessionStart', session_id='new-session'))
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertIsNone(self.runtime.claude_report_ready_task_id)

    def test_verified_root_session_rebind_does_not_inherit_old_report(self):
        self.handle(self.event())
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

    def test_visible_wrapped_report_blocks_legacy_hook_and_fallback(self):
        for health in ['healthy', 'missing', 'historical', 'legacy_override']:
            with self.subTest(health=health):
                self.runtime.claude_hook_health = health
                state = self.daemon._apply_claude_runtime_guards('surface-uuid', self.runtime, self.screen())
                self.assertEqual(state.kind, 'claude_report_ready')
                observation = {'agent_kind': 'claude', 'pid': 1234,
                               'generation': 'fixture-birth-1234'}
                for _ in range(3):
                    self.daemon._maybe_send_claude_hook_gap_fallback(
                        self.target, self.runtime, state, observation, self.client)
        with mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0):
            self.handle(self.event('old-protocol-stop', report_ready_task_id=None))
        self.assert_no_input()

    def test_new_content_or_quoted_report_is_not_a_closeout(self):
        for lines in [wrapped('> ' + REPORT), wrapped(REPORT) + ['', 'Still implementing'],
                      wrapped(REPORT) + ['', '❯ new human task', 'New result pending']]:
            with self.subTest(lines=lines):
                grid = core.Grid.from_rpc(claude_grid_payload(lines=lines, completed=True), 'surface-uuid')
                self.assertEqual(core.classify_claude_grid(grid).kind, 'claude_stopped')

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

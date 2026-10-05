"""Real Stop-hook continuation must supersede a premature completion latch."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_watch import (core, FakeClient, armed_daemon, claude_grid_payload,
                              claude_idle_screen, claude_hook_event, process_fixture)


class CompletionReopenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = FakeClient(
            claude_grid_payload(lines=['Still validating the callback'], completed=True),
            text='Still validating the callback\n' + claude_idle_screen(),
            top=process_fixture(('surface-uuid', 'claude')),
        )
        self.daemon = armed_daemon(self.tmp.name, self.client)
        self.daemon.config['claude_enabled'] = True
        self.runtime = self.daemon.runtime.setdefault('surface-uuid', core.TargetRuntime())
        # armed_daemon binds a live fake PID and birth; deferred retries must
        # retain that same identity rather than inventing a second one.
        self.runtime.claude_process_pid = 1234
        self.runtime.claude_process_generation = 'fixture-birth-1234'
        self.runtime.claude_session_id = 'session-uuid'
        self.runtime.claude_hook_health = 'healthy'
        self.at = time.time() - 10
        self.handle(self.event('completion', completed=True, created_at=self.at))
        self.assertTrue(self.runtime.claude_completed_latched)

    def event(self, name='continued', **changes):
        event = claude_hook_event(name, created_at=self.at + 1, stop_hook_active=True)
        event.update(agent_pid=1234, process_generation='fixture-birth-1234')
        event.update(changes)
        return event

    def handle(self, event):
        self.daemon._handle_claude_event(event, self.client)

    def poll(self, kind='claude_stopped'):
        with mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0):
            return self.daemon._maybe_send_deferred_claude_stop(
                self.daemon.config['targets'][0], self.runtime,
                core.ScreenState(kind, message_kind='claude'), self.client)

    def test_native_hook_continuation_reopens_without_resetting_delivery(self):
        self.runtime.send_count = 3
        self.runtime.last_send_at = self.at - 3
        self.handle(self.event())
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertEqual(self.runtime.send_count, 3)
        self.assertEqual(self.runtime.last_send_at, self.at - 3)
        self.assertEqual(self.daemon.claude_event_ledger.status_of('completion'), 'completed')
        self.assertEqual(self.client.sent_text, [])
        self.assertEqual(self.runtime.claude_deferred_reason, 'active_stop_hook')

    def test_safe_frame_delivers_exactly_once(self):
        self.handle(self.event())
        self.poll()
        self.poll()
        self.assertEqual(len(self.client.sent_text), 1)
        self.assertEqual(self.daemon.claude_event_ledger.status_of('continued'), 'sent')

    def test_working_frame_keeps_unsent_stop(self):
        self.handle(self.event())
        self.poll('working')
        self.assertIsNotNone(self.runtime.claude_deferred_event)
        self.assertEqual(self.client.sent_text, [])

    def test_same_pid_new_birth_cannot_consume_deferred_stop(self):
        self.handle(self.event())
        with mock.patch.object(self.daemon, '_claude_send_process_identity',
                               return_value={'pid': 1234, 'started_epoch': 2.0,
                                             'generation': 'replacement-birth'}):
            self.poll()
        self.assertEqual(self.client.sent_text, [])
        self.assertNotEqual(self.daemon.claude_event_ledger.status_of('continued'), 'sent')

    def test_restart_compares_original_event_time_not_handling_time(self):
        self.daemon.save()
        self.daemon = core.WatchDaemon(Path(self.tmp.name)/'config.json',
                                       Path(self.tmp.name)/'state.json', client=self.client)
        self.daemon.config['claude_enabled'] = True
        self.runtime = self.daemon.runtime['surface-uuid']
        self.handle(self.event())
        self.assertFalse(self.runtime.claude_completed_latched)
        self.daemon.save()
        again = core.WatchDaemon(Path(self.tmp.name)/'config.json',
                                 Path(self.tmp.name)/'state.json', client=self.client)
        self.assertFalse(again.runtime['surface-uuid'].claude_completed_latched)

    def test_old_equal_future_and_invalid_timestamps_do_not_reopen(self):
        for index, at in enumerate([self.at-1, self.at, time.time()+3600, float('nan'), float('inf')]):
            with self.subTest(at=at):
                self.handle(self.event('old-'+str(index), created_at=at))
                self.assertTrue(self.runtime.claude_completed_latched)
        self.assertEqual(self.client.sent_text, [])

    def test_non_native_ambiguous_or_other_identity_does_not_reopen(self):
        for index, changes in enumerate([
            {'synthetic_fallback': True}, {'completed': None}, {'stop_hook_active': False},
            {'stop_hook_active': 'true'}, {'agent_pid': 0}, {'agent_pid': 999},
            {'session_id': 'other'}, {'process_generation': 'other'},
            {'workspace_id': 'other'}, {'surface_id': 'other'},
        ]):
            with self.subTest(changes=changes):
                self.handle(self.event('unsafe-'+str(index), **changes))
                self.assertTrue(self.runtime.claude_completed_latched)
        self.assertEqual(self.client.sent_text, [])

    def test_missing_completion_row_does_not_guess(self):
        self.daemon.claude_event_ledger.events.pop('completion')
        self.handle(self.event())
        self.assertTrue(self.runtime.claude_completed_latched)

    def test_original_native_events_without_generation_use_current_bound_process(self):
        self.daemon.claude_event_ledger.events['completion'].pop('process_generation')
        event = self.event()
        event.pop('process_generation')
        self.handle(event)
        self.assertFalse(self.runtime.claude_completed_latched)

    def test_global_pause_still_blocks_deferred_send(self):
        self.daemon.config['global_paused'] = True
        self.handle(self.event())
        self.poll()
        self.assertEqual(self.client.sent_text, [])

    def test_nonempty_composer_still_blocks_deferred_send(self):
        self.client.payload = claude_grid_payload(composer='busy')
        self.handle(self.event())
        self.poll()
        self.assertEqual(self.client.sent_text, [])

    def test_unresolved_submit_is_not_cleared_or_replayed(self):
        self.runtime.claude_submit_phase = 'awaiting_confirmation'
        self.handle(self.event())
        self.assertTrue(self.runtime.claude_completed_latched)
        self.assertEqual(self.runtime.claude_submit_phase, 'awaiting_confirmation')
        self.assertEqual(self.client.sent_text, [])

    def test_paused_target_cannot_send(self):
        self.daemon.config['targets'][0]['paused'] = True
        self.handle(self.event())
        self.poll()
        self.assertEqual(self.client.sent_text, [])
        self.assertTrue(self.daemon.config['targets'][0]['paused'])

    def test_subsequent_real_completion_stops_recovery(self):
        self.handle(self.event())
        self.handle(self.event('final-completion', completed=True, created_at=self.at+2))
        self.poll()
        self.assertTrue(self.runtime.claude_completed_latched)
        self.assertEqual(self.runtime.claude_completion_event_id, 'final-completion')
        self.assertEqual(self.client.sent_text, [])


if __name__ == '__main__':
    unittest.main()

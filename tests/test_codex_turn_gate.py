import tempfile
import contextlib
import dataclasses
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from tests import test_codex_status_chrome as status_fixture
from tests.test_codex_status_chrome import PROVIDER_TOKEN_RATE_LIMIT, status_payload, visible_text, ERRORS
from tests.test_watch import FakeClient, armed_daemon
import cmux_codex_watch as core


class CodexTurnGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        payload = status_payload("high_demand")
        self.client = FakeClient(payload, visible_text(payload))
        self.daemon = armed_daemon(self.tmp.name, self.client)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.finished = {"kind": "task_complete", "session_id": "original",
                         "turn_id": "one", "at": 100,
                         "error": {"message": ERRORS["high_demand"]}}
        self.turn = Mock(return_value=self.finished)
        self.daemon.codex_queue_recovery.current_turn = self.turn

    def test_error_display_before_native_task_end_never_submits(self):
        for kind in ("task_started", "user_message", "unknown", "turn_aborted"):
            with self.subTest(kind=kind):
                self.turn.return_value = {"kind": kind}
                self.daemon.process_once(self.client)
                self.assertEqual(self.client.sent, [])
        self.turn.return_value = self.finished
        self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)

    def test_native_retries_do_not_enter_the_send_queue(self):
        target = self.daemon.config["targets"][0]
        for kind in ("task_started", "user_message", "unknown", "turn_aborted"):
            with self.subTest(kind=kind):
                self.turn.return_value = {"kind": kind}
                candidate = self.daemon._scheduled_observe(target, lambda: True)
                self.assertIsNone(candidate)
        self.assertEqual(self.client.sent, [])
        self.turn.return_value = self.finished
        candidate = self.daemon._scheduled_observe(target, lambda: True)
        self.assertEqual(candidate.kind, "recoverable_error")
        self.daemon._scheduled_send(target, candidate, lambda: True)
        self.assertEqual(len(self.client.sent), 1)

    def test_user_turn_after_observation_still_cancels_scheduled_send(self):
        target = self.daemon.config["targets"][0]
        candidate = self.daemon._scheduled_observe(target, lambda: True)
        self.assertEqual(candidate.kind, "recoverable_error")
        self.turn.return_value = {"kind": "task_started"}
        self.daemon._scheduled_send(target, candidate, lambda: True)
        self.assertEqual(self.client.sent, [])

    def test_new_user_turn_between_persistence_and_io_cancels_send(self):
        persist = self.daemon._delivery_store.persist
        def changed(sid, runtime):
            persist(sid, runtime)
            self.turn.return_value = {"kind": "task_started"}
        with patch.object(self.daemon._delivery_store, 'persist', side_effect=changed):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.daemon.runtime["surface-uuid"].delivery_status, "cancelled")

    def test_binding_disappearing_before_io_cancels_send(self):
        persist = self.daemon._delivery_store.persist
        def disappeared(sid, runtime):
            persist(sid, runtime)
            self.turn.return_value = None
        with patch.object(self.daemon._delivery_store, 'persist', side_effect=disappeared):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.daemon.runtime["surface-uuid"].delivery_status, "cancelled")

    def test_new_turn_while_waiting_for_workspace_lock_cancels_io(self):
        import cmux_codex_watch as core
        lock = core.workspace_input_lock
        @contextlib.contextmanager
        def changed(*args, **kwargs):
            with lock(*args, **kwargs):
                self.turn.return_value = {"kind": "task_started"}
                yield
        with patch.object(core, 'workspace_input_lock', side_effect=changed):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.daemon.runtime['surface-uuid'].delivery_status, 'cancelled')

    def test_unknown_delivery_never_retries_without_unsent_confirmation(self):
        self.daemon.process_once(self.client)
        runtime = self.daemon.runtime['surface-uuid']
        runtime.delivery_status = 'unknown'
        runtime.awaiting = False
        runtime.last_send_at = 0
        # No stable native signature: the renderer alone cannot prove non-delivery.
        self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)

    def test_missing_hook_and_pending_process_snapshot_do_not_bypass_native_gate(self):
        # Exercise the real QueueRecovery wiring, including the advisory
        # nonblocking process lookup used by the production daemon.
        recovery = self.daemon.codex_queue_recovery
        del recovery.current_turn
        recovery.sessions_root.mkdir(parents=True)
        with patch.object(self.daemon, "_candidate_process_label", return_value={
                "agent_kind": "unknown", "summary": "process refresh pending"}):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.daemon.runtime["surface-uuid"].state, "awaiting_transition")

    def test_success_and_different_native_failure_do_not_revive_old_banner(self):
        for error in (None, {"message": "Permission denied"}, {"message": ERRORS["rate_limit"]}):
            with self.subTest(error=error):
                self.turn.return_value = {**self.finished, "error": error}
                self.daemon.process_once(self.client)
                self.assertEqual(self.client.sent, [])

    def test_scrolled_error_needs_native_eof_when_binding_is_unavailable(self):
        payload = status_fixture.CodexNewActivityChromeTests().payload()
        state = core.classify_grid(core.Grid.from_rpc(payload, 'surface-uuid'))
        self.assertEqual(state.kind, 'recoverable_error')
        self.turn.return_value = None
        target = self.daemon.config['targets'][0]
        with patch.object(self.daemon, '_current_blocked_goal', return_value=None):
            self.assertFalse(self.daemon._codex_turn_ready(
                target, core.TargetRuntime(), state))
        self.assertEqual(self.client.sent, [])

    def test_scrolled_error_uses_latest_native_failure_not_hidden_reply(self):
        payload = status_fixture.CodexNewActivityChromeTests().payload()
        state = core.classify_grid(core.Grid.from_rpc(payload, 'surface-uuid'))
        target = self.daemon.config['targets'][0]
        ended = {**self.finished, 'signature': (1, 100, 1),
                 'terminal_tail': {'offset': 50, 'end': 100, 'sha256': 'a' * 64}}
        for error, allowed in ((None, False),
                               ({'message': PROVIDER_TOKEN_RATE_LIMIT}, True)):
            with self.subTest(error=error):
                self.turn.return_value = {**ended, 'error': error}
                with patch.object(self.daemon, '_current_blocked_goal', return_value=None):
                    self.assertEqual(self.daemon._codex_turn_ready(
                        target, core.TargetRuntime(), state), allowed)
        self.assertEqual(self.client.sent, [])

    def test_one_submission_per_failed_turn_even_if_banner_changes(self):
        self.daemon.process_once(self.client)
        runtime = self.daemon.runtime["surface-uuid"]
        for _ in range(3):
            runtime.awaiting = False
            runtime.last_send_at = 0
            self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)
        self.turn.return_value = {**self.finished, "turn_id": "two", "at": 200}
        self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 2)

    def test_same_turn_status_and_key_format_are_not_non_delivery_proof(self):
        target = self.daemon.config['targets'][0]
        state = core.ScreenState('recoverable_error', message_kind='codex', error_type='high_demand')
        for status in ('accepted', 'confirmed', 'failed', 'cancelled', 'retryable', 'unknown'):
            for key in ('original:one:100', 'goal:original:g:one:99'):
                for at in (100, 101):
                    with self.subTest(status=status, key=key, at=at):
                        runtime = core.TargetRuntime(delivery_status=status, codex_sent_turn_key=key,
                            codex_input_phase='text_pending', send_attempt_id='unproved')
                        self.turn.return_value = {**self.finished, 'at': at}
                        self.assertFalse(self.daemon._codex_turn_ready(target, runtime, state))

    def test_goal_cannot_repeat_same_native_turn_after_plain_or_goal_send(self):
        target = self.daemon.config['targets'][0]
        state = core.ScreenState('recoverable_error', message_kind='codex',
                                 error_type='high_demand', native_goal_stalled=True)
        goal = {**self.finished, 'kind': 'goal_blocked', 'goal_id': 'g', 'at': 99}
        for status in ('accepted', 'confirmed', 'failed', 'cancelled', 'retryable', 'unknown'):
            for key in ('original:one:100', 'goal:original:g:one:99', 'goal:original:old:one:98'):
                with self.subTest(status=status, key=key):
                    runtime = core.TargetRuntime(delivery_status=status, codex_sent_turn_key=key,
                        codex_input_phase='text_pending', send_attempt_id='unproved')
                    with patch.object(self.daemon, '_current_blocked_goal', return_value=goal):
                        self.assertFalse(self.daemon._codex_turn_ready(target, runtime, state))

    def test_native_turn_deduplication_survives_restart(self):
        self.daemon.process_once(self.client)
        restarted = armed_daemon(self.tmp.name, self.client)
        self.addCleanup(restarted._process_snapshots.close)
        restarted.codex_queue_recovery.current_turn = self.turn
        restarted.runtime["surface-uuid"].awaiting = False
        restarted.runtime["surface-uuid"].last_send_at = 0
        restarted.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)

    def test_unknown_prompt_cannot_replay_from_unchanged_native_evidence(self):
        self.daemon.process_once(self.client)
        runtime = self.daemon.runtime["surface-uuid"]
        runtime.delivery_status = "unknown"
        runtime.send_started_at = 150
        runtime.send_completed_at = 158
        runtime.last_send_at = 150
        self.turn.return_value = {**self.finished, "signature": [1, 50, 100]}
        with patch("cmux_codex_watch.time.time", return_value=161):
            self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)
        with patch("cmux_codex_watch.time.time", return_value=162.1):
            self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)

    def test_unknown_send_is_not_retried_after_native_progress(self):
        self.daemon.process_once(self.client)
        runtime = self.daemon.runtime["surface-uuid"]
        runtime.delivery_status = "unknown"
        runtime.send_started_at, runtime.send_completed_at = 150, 158
        runtime.last_send_at = 150
        self.turn.return_value = {"kind": "user_message", "at": 159, "signature": [1, 55, 159]}
        for now in (161, 165, 170):
            with patch("cmux_codex_watch.time.time", return_value=now):
                self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)

    def uncertain_attempt(self, key='goal:original:goal:one:100'):
        runtime = self.daemon.runtime.setdefault('surface-uuid', core.TargetRuntime())
        runtime.delivery_status = 'unknown'
        runtime.codex_input_phase = 'text_pending'
        runtime.codex_sent_turn_key = key
        runtime.send_attempt_id = 'original-uncertain-attempt'
        runtime.send_started_at = 150
        runtime.last_send_error = 'original input outcome remains unknown'
        self.turn.return_value = {**self.finished, 'turn_id': 'two', 'at': 200,
                                  'signature': [1, 100, 200]}
        return runtime, core.ScreenState('recoverable_error', message_kind='codex', error_type='high_demand')

    def test_later_native_failure_is_not_blocked_by_unknown_goal_input(self):
        for key in ('goal:original:goal:one:100', 'original:one:100'):
            with self.subTest(key=key):
                runtime, state = self.uncertain_attempt(key)
                self.assertFalse(self.daemon._reconcile_codex_delivery('surface-uuid', runtime, state))
                self.assertEqual(runtime.delivery_status, 'unknown')
                self.assertEqual(runtime.send_attempt_id, 'original-uncertain-attempt')
                self.assertEqual(runtime.last_send_error, 'original input outcome remains unknown')
                self.assertEqual(self.client.sent, [])

    def test_unknown_goal_only_yields_to_a_distinct_authenticated_failure(self):
        cases = ({'turn_id': 'one'}, {'session_id': 'another'}, {'at': 149},
                 {'at': float('nan')}, {'at': float('inf')}, {'at': '200'},
                 {'kind': 'task_started'}, {'kind': 'turn_aborted'},
                 {'signature': None}, {'turn_id': ''}, {'error': None},
                 {'error': {'message': ERRORS['rate_limit']}})
        for key in ('goal:original:goal:one:100', 'original:one:100'):
            for change in cases:
                with self.subTest(key=key, change=change):
                    runtime, state = self.uncertain_attempt(key)
                    self.turn.return_value.update(change)
                    self.assertTrue(self.daemon._reconcile_codex_delivery('surface-uuid', runtime, state))
                    self.assertEqual(runtime.delivery_status, 'unknown')
                    self.assertEqual(self.client.sent, [])

    def test_retained_draft_and_queue_never_yield_to_a_new_failure(self):
        for key in ('goal:original:goal:one:100', 'original:one:100'):
            for phase, kind in (('text_retained', 'recoverable_error'),
                                ('text_pending', 'queued_followup'), ('text_pending', 'composer_busy')):
                with self.subTest(key=key, phase=phase, kind=kind):
                    runtime, state = self.uncertain_attempt(key)
                    runtime.codex_input_phase = phase
                    state = dataclasses.replace(state, kind=kind)
                    self.assertTrue(self.daemon._reconcile_codex_delivery('surface-uuid', runtime, state))
                    self.assertEqual(runtime.send_attempt_id, 'original-uncertain-attempt')

    def test_only_a_fresh_bound_goal_can_unblock_unknown_goal_input(self):
        proof = {'kind': 'goal_blocked', 'session_id': 'original', 'turn_id': 'two',
                 'goal_id': 'goal', 'at': 200, 'log_id': 2, 'submission_digest': 'digest',
                 'error': {'message': ERRORS['high_demand']}}
        for change, expected in (({}, False), ({'turn_id': 'one'}, True),
                                 ({'session_id': 'another'}, True), ({'at': 149}, True)):
            with self.subTest(change=change):
                runtime, state = self.uncertain_attempt()
                state = dataclasses.replace(state, native_goal_stalled=True)
                self.turn.return_value = {'kind': 'unknown'}
                with patch.object(self.daemon, '_current_blocked_goal', return_value={**proof, **change}):
                    self.assertEqual(self.daemon._reconcile_codex_delivery('surface-uuid', runtime, state), expected)
                self.assertEqual(runtime.delivery_status, 'unknown')

    def test_unbound_goal_or_malformed_original_identity_cannot_clear_unknown(self):
        for key in ('', 'goal:original', 'original', 'goal:original:goal:one:nan'):
            with self.subTest(key=key):
                runtime, state = self.uncertain_attempt(key)
                self.assertTrue(self.daemon._reconcile_codex_delivery('surface-uuid', runtime, state))
        runtime, state = self.uncertain_attempt()
        state = dataclasses.replace(state, native_goal_stalled=True)
        self.turn.return_value = {'kind': 'unknown'}
        with patch.object(self.daemon, '_current_blocked_goal', return_value=None):
            self.assertTrue(self.daemon._reconcile_codex_delivery('surface-uuid', runtime, state))


if __name__ == "__main__":
    unittest.main()

"""Cached failure hints authorize observation only, never replay old input."""
import threading
import unittest

import cmux_codex_watch as core


class NativeRetryHintTests(unittest.TestCase):
    def setUp(self):
        self.target = dict(surface_id='surface', workspace_id='workspace')
        self.daemon = object.__new__(core.WatchDaemon)
        self.daemon._native_dispatch = None
        self.daemon.config = dict(mode='armed', global_paused=False)
        self.daemon._runtime_lock = threading.Lock()
        self.daemon._event_target = lambda _: self.target
        self.runtime = core.TargetRuntime(
            delivery_status='unknown', codex_input_phase='text_pending',
            codex_sent_turn_key='original:old-turn:200.0', send_started_at=210,
            observed_at=301, observed_state='recoverable_error',
            native_failure_at=300, native_failure_turn_key='original:new-turn:300.0',
            send_attempt_id='uncertain-original', last_send_error='lost ACK')
        self.daemon.runtime = {'surface': self.runtime}

    def ready(self, failed_at=300):
        return self.daemon._native_retry_needed('surface', 'workspace', failed_at)

    def test_new_same_session_failed_turn_can_wake_after_old_unknown(self):
        for key in ('original:old-turn:200.0', 'goal:original:goal-id:old-turn:200.0'):
            with self.subTest(key=key):
                self.runtime.codex_sent_turn_key = key
                self.assertTrue(self.ready())
                self.assertEqual(self.runtime.delivery_status, 'unknown')
                self.assertEqual(self.runtime.send_attempt_id, 'uncertain-original')
                self.assertEqual(self.runtime.last_send_error, 'lost ACK')
                self.assertEqual(self.runtime.delivery_confirmed_at, 0)

    def test_retimestamped_same_turn_is_not_new_failure(self):
        for key in ('original:old-turn:300.0',):
            for observed_at in (201, 301):
                with self.subTest(key=key, observed_at=observed_at):
                    self.runtime.native_failure_turn_key = key
                    self.runtime.observed_at = observed_at
                    self.assertFalse(self.ready())

    def test_different_session_hint_wakes_observation_without_settling_old_input(self):
        self.runtime.native_failure_turn_key = 'replacement:new-turn:300.0'
        self.assertTrue(self.ready())
        self.assertEqual(self.runtime.delivery_status, 'unknown')
        self.assertEqual(self.runtime.send_attempt_id, 'uncertain-original')
        self.assertEqual(self.runtime.delivery_confirmed_at, 0)

    def test_hint_must_match_current_failure_and_follow_original_send(self):
        for key, at, requested in (
            ('original:new-turn:299.0', 300, 300),
            ('original:new-turn:300.0', 299, 300),
            ('original:new-turn:300.0', 300, 301),
            ('original:new-turn:205.0', 205, 205),
            ('original:new-turn:210.0', 210, 210),
            ('broken', 300, 300),
            ('original:new-turn:nan', float('nan'), float('nan')),
        ):
            with self.subTest(key=key, at=at, requested=requested):
                self.runtime.native_failure_turn_key = key
                self.runtime.native_failure_at = at
                self.assertFalse(self.ready(requested))

    def test_pauses_drafts_queue_and_retained_text_withhold_cached_retry(self):
        for state in ('composer_busy', 'queued_followup', 'menu'):
            with self.subTest(state=state):
                self.runtime.observed_state = state
                self.assertFalse(self.ready())
        self.runtime.observed_state = 'recoverable_error'
        self.runtime.codex_input_phase = 'text_retained'
        self.assertFalse(self.ready())
        self.runtime.codex_input_phase = 'text_pending'
        self.target['paused'] = True
        self.assertFalse(self.ready())
        self.target.pop('paused')
        self.daemon.config['global_paused'] = True
        self.assertFalse(self.ready())
        self.daemon.config.update(global_paused=False, mode='dry-run')
        self.assertFalse(self.ready())

    def test_ordinary_unobserved_failure_and_confirmed_input_keep_existing_policy(self):
        self.runtime.delivery_status = ''
        self.runtime.observed_at = 299
        self.assertTrue(self.ready())
        self.runtime.observed_at = 301
        for status in ('sending', 'accepted', 'confirmed'):
            with self.subTest(status=status):
                self.runtime.delivery_status = status
                self.runtime.send_io_started_at = 300
                self.assertFalse(self.ready())


if __name__ == '__main__':
    unittest.main()

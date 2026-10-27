"""Slow connected Goal checks retain one original, bounded input attempt."""
from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch

import cmux_codex_watch as core
from tests import test_goal_input_not_sent as goal_input


class GoalEnterBudgetTests(unittest.TestCase):
    def setUp(self):
        self.fixture = goal_input.GoalInputNotSentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.case = self.fixture.case
        self.case.deny = False

    @contextmanager
    def slow_connected_check(self, text_seconds=0, enter_seconds=0, change=None):
        """Charge only live checks on the admitted input connection, with no sleep."""
        c = self.case
        ready = c.f.daemon._goal_input_ready
        monotonic = core.time.monotonic
        elapsed = [0.0]
        checks = []

        def delayed(*args, **kwargs):
            if c.client.connected_input_remaining() is not None:
                draft = kwargs.get('draft', args[5] if len(args) > 5 else None)
                stage = 'enter' if draft == '/goal resume' else 'text'
                checks.append(stage)
                elapsed[0] += enter_seconds if stage == 'enter' else text_seconds
                if change is not None:
                    change(stage)
            return ready(*args, **kwargs)

        with patch.object(core.time, 'monotonic', side_effect=lambda: monotonic() + elapsed[0]), \
             patch.object(c.f.daemon, '_goal_input_ready', side_effect=delayed):
            yield checks

    def input_methods(self):
        return [r[0] for r in self.case.pending]

    def retain_unsubmitted_enter(self):
        c = self.case
        with self.slow_connected_check(enter_seconds=46):
            c.send()
        self.assertEqual(self.input_methods(), ['surface.send_text'])
        self.assertEqual(c.f.runtime.delivery_status, 'unknown')
        observation = c.f.runtime.codex_goal_diagnostics['observation']
        self.assertIs(observation['enter_not_sent_proof']['write_attempted'], False)
        self.assertNotIn('retained_submit', c.f.runtime.codex_goal_diagnostics)
        attempt = c.f.runtime.send_attempt_id
        c.deny = False
        c.after_input = lambda method: None
        c.f.state = core.classify_grid(core.Grid.from_rpc(c.f.client.payload, c.f.sid))
        return attempt

    def test_slow_text_and_enter_checks_send_once(self):
        with self.slow_connected_check(text_seconds=12, enter_seconds=12) as checks:
            self.case.send()
        self.assertIn('text', checks)
        self.assertIn('enter', checks)
        self.assertEqual(self.input_methods(), ['surface.send_text', 'surface.send_key'])
        self.assertEqual(self.case.f.runtime.delivery_status, 'accepted')
        self.case.send()
        self.assertEqual(len(self.case.pending), 2)

    def test_expired_initial_enter_is_proven_unwritten(self):
        with self.slow_connected_check(enter_seconds=46):
            self.case.send()
        self.assertEqual(self.input_methods(), ['surface.send_text'])
        self.assertEqual(self.case.f.runtime.delivery_status, 'unknown')
        observation = self.case.f.runtime.codex_goal_diagnostics['observation']
        self.assertIs(observation['enter_not_sent_proof']['write_attempted'], False)

    def test_retained_enter_slow_check_reuses_attempt_once(self):
        attempt = self.retain_unsubmitted_enter()
        with self.slow_connected_check(enter_seconds=12) as checks:
            self.case.send()
        self.assertIn('enter', checks)
        self.assertEqual(self.input_methods(), ['surface.send_text', 'surface.send_key'])
        self.assertEqual(self.case.f.runtime.send_attempt_id, attempt)
        self.assertEqual(self.case.f.runtime.codex_goal_diagnostics['retained_submit']['status'],
                         'enter_acknowledged')
        self.case.send()
        self.assertEqual(len(self.case.pending), 2)

    def test_retained_enter_expiry_does_not_reopen_budget_after_restart(self):
        attempt = self.retain_unsubmitted_enter()
        with self.slow_connected_check(enter_seconds=46):
            self.case.send()
        c = self.case
        self.assertEqual(self.input_methods(), ['surface.send_text'])
        retained = deepcopy(c.f.runtime.codex_goal_diagnostics['retained_submit'])
        self.assertEqual(retained['status'], 'outcome_unknown')
        self.fixture.restart()
        c.send()
        self.assertEqual(self.input_methods(), ['surface.send_text'])
        self.assertEqual(c.f.runtime.send_attempt_id, attempt)
        self.assertEqual(c.f.runtime.codex_goal_diagnostics['retained_submit'], retained)

    def test_lost_initial_enter_ack_never_replays_text_or_key(self):
        c = self.case
        c.after_input = lambda method: setattr(c, 'fault', 'eof') if method == 'surface.send_text' else None
        with self.slow_connected_check(enter_seconds=12):
            c.send()
        self.assertEqual(self.input_methods(), ['surface.send_text', 'surface.send_key'])
        self.assertEqual(c.f.runtime.delivery_status, 'unknown')
        self.assertNotIn('enter_not_sent_proof', c.f.runtime.codex_goal_diagnostics['observation'])
        c.fault = None
        c.after_input = lambda method: None
        self.fixture.restart()
        c.send()
        self.assertEqual(len(c.pending), 2)

    def test_changed_identity_during_slow_enter_withholds_key(self):
        def change(stage):
            if stage == 'enter':
                self.fixture.goal[0]['model_provider'] = 'replacement-provider'
        with self.slow_connected_check(enter_seconds=12, change=change):
            self.case.send()
        self.assertEqual(self.input_methods(), ['surface.send_text'])
        self.assertEqual(self.case.f.runtime.delivery_status, 'unknown')
        self.assertNotIn('retained_submit', self.case.f.runtime.codex_goal_diagnostics)


if __name__ == '__main__':
    unittest.main()

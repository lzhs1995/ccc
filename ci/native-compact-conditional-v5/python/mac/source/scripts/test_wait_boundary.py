"""Public wait and Stop entrypoints must not create a dispatch/wait deadlock."""
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch

import cmux_workflow_advisory as advisory
import cmux_hook_runtime_audit as audit
import executor_reask as reask


class WaitBoundaryTests(unittest.TestCase):
    def invoke(self, state):
        out = io.StringIO()
        with patch.object(reask, 'tick', return_value=state) as tick, \
                patch.object(reask.time, 'sleep', side_effect=AssertionError('must return')), \
                contextlib.redirect_stdout(out):
            self.assertEqual(reask.main(['run', '--caller-uuid', 'executor']), 0)
        tick.assert_called_once()
        return json.loads(out.getvalue())

    def test_reserved_wait_returns_without_stopping_or_rearming_episode(self):
        state = dict(caller_surface_uuid='executor', state=reask.WAITING,
                     episode_id='original', paused_tasks=[{'collaboration_id': 'reservation'}],
                     terminal_pending={'marker': 'original-uncertain'}, asks=[])
        original = json.dumps(state, sort_keys=True)
        result = self.invoke(state)
        self.assertEqual(result['run_return_reason'], 'FORMAL_RESERVATION_WAIT')
        self.assertTrue(result['runner_returned'])
        self.assertEqual(result['outcome'], reask.WAITING)
        self.assertTrue(result['reservation_paused'])
        self.assertFalse(result['wait_boundary']['idle_observed'])
        self.assertFalse(result['wait_boundary']['goal_state_changed'])
        self.assertEqual(json.dumps(state, sort_keys=True), original)

    def test_answered_wait_returns_unconsumed_even_when_reserved(self):
        state = dict(caller_surface_uuid='executor', state=reask.ANSWERED,
                     asks=[], reply={'body': 'read before consuming'},
                     paused_tasks=[{'collaboration_id': 'reservation'}])
        result = self.invoke(state)
        self.assertEqual(result['run_return_reason'], 'REPLY_REQUIRES_ORIGINAL_CONSUME')
        self.assertEqual(result['outcome'], reask.ANSWERED)
        self.assertIsNone(result['reply_consumed_epoch'])
        self.assertTrue(result['wait_boundary']['fresh_preflight_required'])

    def test_deadline_returns_without_extending_or_closing_waiting_episode(self):
        state = dict(state=reask.WAITING, episode_id='original')
        moments = iter([0, 0, 100])
        with patch.object(reask, 'tick', return_value=state):
            result = reask.run('executor', max_seconds=1, clock=lambda: next(moments),
                               sleep=lambda _: self.fail('deadline must return'))
        self.assertEqual(result['state'], reask.WAITING)
        self.assertEqual(result['run_return_reason'], 'OBSERVATION_DEADLINE')
        self.assertEqual(state, dict(state=reask.WAITING, episode_id='original'))

    def test_actual_stop_wrapper_ignores_legacy_blocker_and_goal_wait(self):
        for hook in ('cmux_executor_idle_guard.py', 'cmux_executor_reask_stop_guard.py',
                     'cmux_consensus_stop_guard.py', 'cmux_handshake_receipt_guard.py'):
            with self.subTest(hook=hook), \
                    patch.object(sys, 'argv', ['advisory', '--hook', hook]), \
                    patch.object(sys, 'stdin', io.StringIO(json.dumps(dict(
                        hook_event_name='Stop', stop_hook_active=False,
                        last_assistant_message='Waiting for supervisor; Goal remains active.')))), \
                    patch.object(audit, 'record'), \
                    patch.object(reask, 'run', side_effect=AssertionError('no forced wait')), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(advisory.main(), 0)
                self.assertEqual(out.getvalue(), '')


if __name__ == '__main__':
    unittest.main()

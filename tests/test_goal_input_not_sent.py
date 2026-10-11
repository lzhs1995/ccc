"""Goal zero-input recovery retains its original attempt across real socket I/O."""
from copy import deepcopy
import dataclasses
import unittest
from unittest.mock import patch

import cmux_codex_watch as core
from tests import test_input_not_sent as zero_input
from tests.test_watch import span


class GoalInputNotSentTests(unittest.TestCase):
    def setUp(self):
        self.case = c = zero_input.InputNotSentTests()
        c.setUp()
        self.addCleanup(c.doCleanups)
        f = c.f
        grid = f.client.payload['render_grid']
        grid['row_spans'].append(span(grid['cursor']['row'] + 1, 0, 'Goal stalled (/goal resume)'))
        self.empty_frame = deepcopy(f.client.payload)
        f.state = core.classify_grid(core.Grid.from_rpc(f.client.payload, f.sid))
        self.assertTrue(f.state.native_goal_stalled)
        turn = f.daemon.codex_queue_recovery.current_turn(f.target)
        self.process_pid = turn['pid']
        self.goal = [{**turn, 'kind': 'goal_blocked', 'goal_id': 'original-goal'}]
        goal_patch = patch('ccc_codex_goal.blocked_goal', side_effect=lambda *a, **kw: self.goal[0])
        goal_patch.start()
        self.addCleanup(goal_patch.stop)
        self.bind_process()

    def bind_process(self):
        self.case.f.daemon.codex_queue_recovery.process_lookup = lambda _: {
            'agent_kind': 'codex', 'agent_pids': [self.process_pid]}

    def restart(self):
        self.case.restart_records()
        self.bind_process()

    def initial_refusal(self):
        attempt = self.case.initial_refusal()
        runtime = self.case.f.runtime
        self.assertEqual(runtime.delivery_status, 'retryable')
        self.assertEqual(runtime.codex_input_phase, 'input_not_sent')
        self.assertEqual(runtime.codex_input_not_sent['identity']['goal_id'], 'original-goal')
        self.assertEqual(self.case.restored().codex_input_not_sent, runtime.codex_input_not_sent)
        return attempt

    def assert_recovered_once(self, attempt):
        c = self.case
        c.deny = False
        c.send()
        self.assertEqual(c.f.runtime.delivery_status, 'accepted')
        self.assertEqual(c.f.runtime.send_attempt_id, attempt)
        self.assertEqual(c.episode()['count'], 1)
        self.assertEqual([r[0] for r in c.pending], ['surface.send_text', 'surface.send_key'])
        self.assertTrue(all(r[1:] == ('sending', 'text_pending') for r in c.pending))
        c.send()
        self.assertEqual(len(c.pending), 2)

    def test_goal_refusal_reuses_original_attempt(self):
        self.assert_recovered_once(self.initial_refusal())

    def test_goal_refusal_survives_daemon_restart(self):
        attempt = self.initial_refusal()
        self.restart()
        self.assert_recovered_once(attempt)

    def test_real_goal_workspace_lock_timeout_recovers_same_attempt_after_restart(self):
        c = self.case
        c.deny = False
        with zero_input.held_workspace_input_lock(c):
            c.send()
        self.assertFalse(c.pending)
        attempt = c.f.runtime.send_attempt_id
        self.assertEqual(c.restored().delivery_status, 'retryable')
        self.assertEqual(c.restored().codex_input_phase, 'input_not_sent')
        self.assertEqual(c.restored().codex_input_not_sent['identity']['goal_id'], 'original-goal')
        self.assertEqual(c.episode()['count'], 0)
        self.restart()
        self.assert_recovered_once(attempt)

    def test_goal_workspace_lock_exit_failure_cannot_authorize_another_paste(self):
        c = self.case
        c.deny = False
        with zero_input.workspace_lock_exit_failure():
            c.send()
        self.assertEqual([r[0] for r in c.pending], ['surface.send_text', 'surface.send_key'])
        self.assertFalse(c.restored().codex_input_not_sent)
        self.restart()
        c.f.client.payload = deepcopy(self.empty_frame)
        c.send()
        self.assertEqual(len(c.pending), 2)

    def test_goal_unclassified_body_failure_cannot_mint_zero_input_proof(self):
        c = self.case
        c.deny = False
        with patch.object(c.client, 'resume_codex_goal', side_effect=RuntimeError('unclassified failure')):
            c.send()
        self.assertFalse(c.pending)
        self.assertFalse(c.restored().codex_input_not_sent)
        self.restart()
        c.send()
        self.assertFalse(c.pending)

    def assert_pending_write_recovers(self, mode):
        c = self.case
        c.deny = False
        with zero_input.pending_commit_fault(c, mode) as failures:
            c.send()
        self.assertEqual(failures, ['pending'])
        self.assertFalse(c.pending)
        attempt = c.f.runtime.send_attempt_id
        self.assertEqual(c.f.runtime.delivery_status, 'retryable')
        self.assertEqual(c.restored().codex_input_not_sent['identity']['goal_id'], 'original-goal')
        self.assertEqual(c.episode()['count'], 0)
        self.restart()
        self.assert_recovered_once(attempt)

    def test_real_goal_pending_rollback_preserves_original_goal_after_restart(self):
        self.assert_pending_write_recovers('rollback')

    def test_real_goal_pending_commit_with_lost_result_preserves_original_goal(self):
        self.assert_pending_write_recovers('lost_result')

    def test_real_goal_proof_failure_blocks_input_across_restart(self):
        c = self.case
        c.deny = False
        with zero_input.pending_commit_fault(c, 'proof_failure') as failures:
            c.send()
        self.assertEqual(failures, ['pending', 'proof'])
        self.assertFalse(c.pending)
        self.assertFalse(c.f.runtime.codex_input_not_sent)
        self.assertEqual(c.restored().codex_input_phase, 'text_pending')
        self.restart()
        c.send()
        self.assertFalse(c.pending)

    def test_goal_with_inflight_transcript_still_requires_and_reuses_exact_goal(self):
        self.case.f.state = dataclasses.replace(self.case.f.state, requires_goal_proof=True)
        self.assert_recovered_once(self.initial_refusal())

    def test_changed_goal_process_provider_and_target_cannot_borrow_proof(self):
        attempt = self.initial_refusal()
        c = self.case
        c.deny = False
        original = deepcopy(self.goal[0])
        for change in ({'goal_id': 'different-goal'}, {'pid': original['pid'] + 1},
                       {'process_start': original['process_start'] + 1},
                       {'model_provider': 'changed-provider'}, {'at': original['at'] + .001},
                       {'session_id': 'different-session'}, {'turn_id': 'different-turn'}):
            with self.subTest(change=change):
                self.goal[0] = {**original, **change}
                c.send()
                self.assertFalse(c.pending)
                self.assertEqual(c.f.runtime.send_attempt_id, attempt)
        self.goal[0] = original
        with patch.dict(c.f.target, workspace_id='different-workspace'):
            c.send()
        self.assertFalse(c.pending)

    def test_original_goal_proof_cannot_become_plain_continuation(self):
        self.initial_refusal()
        self.case.deny = False
        self.goal[0] = None
        self.case.send()
        self.assertFalse(self.case.pending)

    def test_goal_cancellation_keeps_zero_input_proof_and_reservation(self):
        attempt = self.initial_refusal()
        c = self.case
        c.deny = False
        f = c.f
        proof = deepcopy(f.runtime.codex_input_not_sent)
        save, active = f.daemon._save_delivery, f.daemon._active_send_target
        committed = []
        def persist(*args, **kwargs):
            result = save(*args, **kwargs)
            if f.runtime.delivery_status == 'sending':
                committed.append(True)
            return result
        with patch.object(f.daemon, '_save_delivery', side_effect=persist), \
             patch.object(f.daemon, '_active_send_target', side_effect=lambda *a, **kw:
                          None if committed else active(*a, **kw)):
            c.send()
        self.assertTrue(committed)
        self.assertFalse(c.pending)
        self.assertEqual(c.restored().codex_input_not_sent, proof)
        self.restart()
        self.assert_recovered_once(attempt)

    def test_goal_identity_changed_during_socket_admission_blocks_text(self):
        attempt = self.initial_refusal()
        c = self.case
        c.deny = False
        control = c.client._control_rpc
        def drift(method, params, **kwargs):
            if method == 'surface.send_text':
                self.goal[0]['model_provider'] = 'replacement-provider'
            return control(method, params, **kwargs)
        with patch.object(c.client, '_control_rpc', side_effect=drift):
            c.send()
        self.assertFalse(c.pending)
        self.assertEqual(c.f.runtime.send_attempt_id, attempt)
        self.assertEqual(c.episode()['count'], 1)

    def test_goal_identity_changed_after_text_blocks_enter_and_repaste(self):
        self.initial_refusal()
        c = self.case
        c.deny = False
        original = deepcopy(self.goal[0])
        def drift(method):
            if method == 'surface.send_text':
                self.goal[0]['model_provider'] = 'replacement-provider'
        c.after_input = drift
        c.send()
        self.assertEqual([r[0] for r in c.pending], ['surface.send_text'])
        self.assertEqual(c.f.runtime.delivery_status, 'unknown')
        self.assertFalse(c.f.runtime.codex_input_not_sent)
        self.goal[0] = original
        c.after_input = lambda method: None
        self.restart()
        c.f.client.payload = deepcopy(self.empty_frame)
        c.send()
        self.assertEqual(len(c.pending), 1)

    def test_lost_goal_text_ack_remains_unknown_after_restart(self):
        self.initial_refusal()
        c = self.case
        c.deny, c.fault = False, 'eof'
        c.send()
        self.assertEqual(c.f.runtime.delivery_status, 'unknown')
        self.restart()
        c.fault = None
        c.f.client.payload = deepcopy(self.empty_frame)
        c.send()
        self.assertEqual([r[0] for r in c.pending], ['surface.send_text'])

    def test_goal_proof_commit_failure_never_authorizes_input(self):
        c = self.case
        save = c.f.daemon._delivery_store.persist
        def refuse_proof(sid, runtime):
            if runtime.codex_input_phase == 'input_not_sent':
                raise RuntimeError('goal proof commit failed')
            return save(sid, runtime)
        with patch.object(c.f.daemon._delivery_store, 'persist', side_effect=refuse_proof):
            with self.assertRaisesRegex(RuntimeError, 'goal proof commit failed'):
                c.send()
        self.assertFalse(c.f.runtime.codex_input_not_sent)
        self.assertEqual(c.restored().codex_input_phase, 'text_pending')
        c.deny = False
        c.send()
        self.restart()
        c.send()
        self.assertFalse(c.pending)

    def test_goal_pending_intent_crash_never_becomes_zero_input_proof(self):
        self.initial_refusal()
        c = self.case
        c.deny = False
        with patch.object(c.client, '_control_rpc', side_effect=SystemExit('crash')):
            with self.assertRaises(SystemExit):
                c.send()
        self.restart()
        c.send()
        self.assertFalse(c.pending)

    def test_pause_and_existing_draft_preserve_original_goal_attempt(self):
        attempt = self.initial_refusal()
        c = self.case
        c.deny = False
        c.f.daemon.config_store.mutate(lambda config: config.update(global_paused=True))
        c.send()
        self.assertFalse(c.pending)
        c.f.daemon.config_store.mutate(lambda config: config.update(global_paused=False))
        c.deny = True
        c.send()
        self.assertFalse(c.pending)
        self.assert_recovered_once(attempt)


if __name__ == '__main__':
    unittest.main()

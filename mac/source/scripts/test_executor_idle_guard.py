"""Real Stop entrypoint and reentry tests: idle observation never blocks or sends."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import cmux_executor_idle_guard as guard
import executor_ready as ready
import offline_test_hook

GUARD = Path(__file__).with_name('cmux_executor_idle_guard.py')


class IdleGuardTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='finite-idle-hook-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / 'home'
        self.state = self.home / '.local/state/multi-agent-collaboration'
        self.active = self.root / 'active'
        self.workspace, self.surface, self.supervisor = [str(uuid.uuid4()) for _ in range(3)]
        self.marker_path = self.active / self.workspace / 'collab.json'
        self.env = dict(os.environ, HOME=str(self.home), CMUX_WORKSPACE_ID=self.workspace,
                        CMUX_SURFACE_ID=self.surface)
        patcher = patch.object(ready, 'state_root', return_value=self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def arm(self, executor=None, task='test-task'):
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps(dict(
            task_id=task, workspace_uuid=self.workspace, participants=[
                dict(role='supervisor', surface_ref='surface:1', surface_uuid=self.supervisor),
                dict(role='executor', surface_uuid=executor or self.surface)])))

    def stop(self, reentry=False, event='Stop'):
        data = dict(hook_event_name=event, stop_hook_active=reentry, last_assistant_message='idle')
        return subprocess.run(offline_test_hook.command(GUARD, self.active), input=json.dumps(data),
                              text=True, capture_output=True, env=self.env, timeout=10)

    def binding_path(self):
        return guard._binding_path(self.workspace, self.surface)

    def binding(self):
        return ready.read_json(self.binding_path())

    def disarmed(self):
        self.arm()
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.binding()['state'], 'BOUND')
        self.marker_path.unlink()

    def test_unbound_executor_and_other_events_pass_without_state(self):
        for event in ('Stop', 'PreToolUse', 'SubagentStop'):
            self.assertEqual(self.stop(event=event).returncode, 0)
        self.arm(executor=str(uuid.uuid4()))
        self.assertEqual(self.stop().returncode, 0)
        self.assertIsNone(self.binding())

    def test_idle_executor_without_ask_loop_always_passes_and_records_once(self):
        self.disarmed()
        run = self.stop()
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stderr, '')
        self.assertEqual(run.stdout, '')
        state = self.binding()
        self.assertEqual(state['state'], guard.IDLE)
        self.assertEqual(state['lifecycle_state'], ready.WAITING_DEPENDENCY)
        self.assertEqual(state['automatic_input_operations'], 0)
        self.assertTrue(state['stop_allowed'])
        self.assertEqual(state['supervisor_uuid'], self.supervisor)
        self.assertEqual(state['task_id'], 'test-task')
        self.assertFalse(ready.loop_path(self.surface).exists())
        before = self.binding_path().read_bytes()
        mtime = self.binding_path().stat().st_mtime_ns
        for _ in range(3):
            self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.binding_path().read_bytes(), before)
        self.assertEqual(self.binding_path().stat().st_mtime_ns, mtime)

    def test_reentrant_stop_passes_before_any_identity_or_filesystem_work(self):
        with patch.object(guard.hook_identity, 'evaluation', side_effect=AssertionError('identity read')), \
             patch.object(guard.stop_guard, '_has_active_markers', side_effect=AssertionError('state read')):
            self.assertEqual(guard.evaluate(dict(hook_event_name='Stop', stop_hook_active=True)), (True, ''))

    def test_reentry_does_not_erase_the_durable_idle_notice(self):
        self.disarmed()
        self.assertEqual(self.stop().returncode, 0)
        before = self.binding_path().read_bytes()
        for _ in range(3):
            self.assertEqual(self.stop(reentry=True).returncode, 0)
        self.assertEqual(self.binding_path().read_bytes(), before)

    def test_stop_reentry_string_is_not_treated_as_boolean_true(self):
        self.disarmed()
        self.assertEqual(self.stop(reentry='true').returncode, 0)
        self.assertEqual(self.binding()['state'], guard.IDLE)

    def test_old_infinite_loop_state_is_preserved_without_requiring_liveness(self):
        self.disarmed()
        ready.write_json(ready.loop_path(self.surface), dict(
            caller_surface_uuid=self.surface, state=ready.ASKING, pid=999999,
            heartbeat_epoch=time.time() - 600, interval_seconds=3600,
            asks=[dict(marker='original', outcome='UNCONFIRMED_DO_NOT_RESEND')]))
        before = ready.loop_path(self.surface).read_bytes()
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.binding()['state'], guard.IDLE)
        self.assertEqual(ready.loop_path(self.surface).read_bytes(), before)
        self.assertFalse(ready.stop_path(self.surface).exists())

    def test_legacy_idle_asking_binding_is_migrated_once_and_never_blocks(self):
        self.disarmed()
        original = self.binding()
        ready.write_json(self.binding_path(), dict(original, state='IDLE_ASKING', blocks=37))
        self.assertEqual(self.stop().returncode, 0)
        state = self.binding()
        self.assertEqual(state['state'], guard.IDLE)
        self.assertEqual(state['blocks'], 37)
        before = self.binding_path().read_bytes()
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.binding_path().read_bytes(), before)

    def test_timeout_does_not_clear_task_or_claim_reply_or_delivery(self):
        self.disarmed()
        ready.write_json(ready.loop_path(self.surface), dict(
            caller_surface_uuid=self.surface, state=ready.TIMED_OUT, task_id='test-task',
            asks=[dict(marker='original', outcome='UNCONFIRMED_DO_NOT_RESEND')], reply=None))
        before = ready.loop_path(self.surface).read_bytes()
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.binding()['task_id'], 'test-task')
        self.assertEqual(self.binding()['state'], guard.IDLE)
        self.assertEqual(ready.loop_path(self.surface).read_bytes(), before)

    def test_rearm_stops_existing_loop_through_legacy_stop_file_and_keeps_marker(self):
        self.disarmed()
        self.assertEqual(self.stop().returncode, 0)
        ready.write_json(ready.loop_path(self.surface), dict(
            caller_surface_uuid=self.surface, state=ready.WAITING_DEPENDENCY, task_id='test-task'))
        self.arm(task='next-task')
        before = self.marker_path.read_bytes()
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.binding()['state'], 'BOUND')
        self.assertEqual(self.binding()['task_id'], 'next-task')
        self.assertEqual(self.marker_path.read_bytes(), before)
        stop = ready.read_json(ready.stop_path(self.surface))
        self.assertEqual(stop['reason'], 'DISPATCHED')
        stop_before = ready.stop_path(self.surface).read_bytes()
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(ready.stop_path(self.surface).read_bytes(), stop_before)
        self.marker_path.unlink()
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.binding()['state'], guard.IDLE)

    def test_dispatch_does_not_stop_a_foreign_caller_loop(self):
        self.arm()
        ready.write_json(ready.loop_path(self.surface), dict(
            caller_surface_uuid='foreign', state=ready.ASKING))
        self.assertEqual(self.stop().returncode, 0)
        self.assertFalse(ready.stop_path(self.surface).exists())

    def test_old_answer_or_operator_stop_does_not_force_any_new_request(self):
        for state in ('IDLE_ANSWERED', 'IDLE_STOPPED_BY_OPERATOR'):
            with self.subTest(state=state):
                ready.write_json(self.binding_path(), dict(state=state, task_id='old',
                                                           surface_uuid=self.surface))
                before = self.binding_path().read_bytes()
                self.assertEqual(self.stop().returncode, 0)
                self.assertEqual(self.binding_path().read_bytes(), before)
        self.assertFalse(ready.requests_dir().exists())

    def test_idle_stop_cannot_invoke_a_sender_or_background_loop(self):
        self.disarmed()
        payload = dict(hook_event_name='Stop', surface_uuid=self.surface)
        with patch.object(guard.hook_identity, 'resolve', return_value=(self.workspace, self.surface)), \
             patch.object(guard.stop_guard, '_has_active_markers', return_value=False), \
             patch.object(guard.stop_guard, '_active_markers', return_value=[]), \
             patch.object(ready, '_bridge', side_effect=AssertionError('input bridge')), \
             patch.object(ready, 'persist', side_effect=AssertionError('background loop')), \
             patch.object(ready, 'request', side_effect=AssertionError('automatic ask')):
            self.assertEqual(guard.evaluate(payload), (True, ''))
        self.assertEqual(self.binding()['state'], guard.IDLE)

    def test_bad_state_and_identity_failure_fail_open_without_rewriting_evidence(self):
        self.disarmed()
        self.binding_path().write_text('broken binding')
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.binding_path().read_text(), 'broken binding')
        with patch.object(guard.stop_guard, '_has_active_markers', return_value=True), \
             patch.object(guard.hook_identity, 'resolve', side_effect=ValueError('unknown identity')):
            self.assertEqual(guard.evaluate(dict(hook_event_name='Stop')), (True, ''))

    def test_stable_bound_state_is_written_once_and_not_refreshed_each_stop(self):
        self.arm()
        self.assertEqual(self.stop().returncode, 0)
        before = self.binding_path().read_bytes()
        self.assertEqual(self.stop().returncode, 0)
        self.assertEqual(self.binding_path().read_bytes(), before)

    def test_main_ignores_malformed_or_oversized_payload(self):
        for raw in ('{', 'null', '[]', '{}', 'x' * (ready.MAX_JSON_BYTES + 1)):
            with self.subTest(size=len(raw)), patch.object(guard.sys, 'stdin', io.StringIO(raw)):
                self.assertEqual(guard.main(), 0)


if __name__ == '__main__':
    unittest.main()

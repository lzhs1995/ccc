"""Native goal recovery never replaces lost rollout evidence with a fake Stop."""
from contextlib import closing
from pathlib import Path
import sqlite3
import json
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import ccc_codex_goal as goal
import cmux_codex_watch as core
from ccc_provider_retry import ProviderRetryStore
from tests.test_codex_status_chrome import status_payload, visible_text, ERRORS
from tests.test_watch import FakeClient, armed_daemon, span


class NativeGoalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.sid, self.tid = str(uuid.uuid4()), str(uuid.uuid4())
        self.target = {"surface_id": "surface", "workspace_id": "workspace"}
        self.now = time.time() - 2
        self.process = {"pid": 12345, "birth": [int(self.now) - 100, 3], "surface_id": "surface",
                        "environment_workspace_id": "workspace"}
        lock = root / "thread-writer-locks" / (self.sid + ".lock")
        lock.parent.mkdir()
        lock.touch()
        self.goals, self.logs = root / "goals_1.sqlite", root / "logs_2.sqlite"
        with closing(sqlite3.connect(self.goals)) as db, db:
            db.execute("CREATE TABLE thread_goals(thread_id,goal_id,status,updated_at_ms)")
            db.execute("INSERT INTO thread_goals VALUES(?,?,?,?)", (self.sid, "goal", "blocked", int(self.now * 1000) + 5))
        with closing(sqlite3.connect(self.logs)) as db, db:
            db.execute("CREATE TABLE logs(id,ts,ts_nanos,thread_id,target,feedback_log_body,process_uuid)")
            db.execute("INSERT INTO logs VALUES(?,?,?,?,?,?,?)", (1, int(self.now), int(self.now % 1 * 1e9), self.sid,
                "codex_core::session::turn", f"turn{{turn.id={self.tid} model=model}}:session_task.run:run_turn: Turn error: " + ERRORS["rate_limit"],
                "pid:12345:process-generation"))
        self.files = {p: {"device": p.stat().st_dev, "inode": p.stat().st_ino} for p in (lock, self.goals, self.logs)}
        self.scope = patch.object(goal.scope, "process", return_value=self.process)
        self.inventory = patch.object(goal.native, "process_writable_files", return_value=self.files)
        self.scope.start()
        self.inventory.start()
        self.addCleanup(self.scope.stop)
        self.addCleanup(self.inventory.stop)

    def test_blocked_native_goal_and_matching_error_without_rollout(self):
        result = goal.blocked_goal(self.target, 12345)
        self.assertEqual(result["kind"], "goal_blocked")
        self.assertEqual(result["session_id"], self.sid)
        self.assertEqual(result["turn_id"], self.tid)

    def test_provider_comes_from_original_open_database_and_writer(self):
        state = self.goals.parent / 'state_5.sqlite'
        with closing(sqlite3.connect(state)) as db, db:
            db.execute('CREATE TABLE threads(id PRIMARY KEY, model_provider)')
            db.execute('INSERT INTO threads VALUES(?, ?)', (self.sid, 'original-provider'))
        self.files[state] = {'device': state.stat().st_dev, 'inode': state.stat().st_ino}
        turn = {'pid': 12345, 'session_id': self.sid, 'process_start': self.process['birth'][0]}
        self.assertEqual(goal.provider_for_turn(self.target, turn), 'original-provider')
        self.assertIsNone(goal.provider_for_turn(self.target, {**turn, 'session_id': str(uuid.uuid4())}))
        self.assertIsNone(goal.provider_for_turn({**self.target, 'workspace_id': 'other'}, turn))
        with patch.object(goal.scope, 'process', side_effect=[self.process, {**self.process, 'birth': [1, 2]}]):
            self.assertIsNone(goal.provider_for_turn(self.target, turn))
        self.files[state]['inode'] += 1
        self.assertIsNone(goal.provider_for_turn(self.target, turn))

    def test_provider_missing_database_never_guesses_global_config(self):
        turn = {'pid': 12345, 'session_id': self.sid, 'process_start': self.process['birth'][0]}
        self.assertIsNone(goal.provider_for_turn(self.target, turn))

    def test_later_submission_active_goal_and_replaced_database_each_veto(self):
        with closing(sqlite3.connect(self.logs)) as db, db:
            db.execute("INSERT INTO logs VALUES(?,?,?,?,?,?,?)", (2, int(self.now) + 1, 0, self.sid,
                "codex_core::session::handlers", "session_loop: Submission sub=new task", "pid:12345:process-generation"))
        self.assertIsNone(goal.blocked_goal(self.target, 12345))
        with closing(sqlite3.connect(self.logs)) as db, db:
            db.execute("DELETE FROM logs WHERE id=2")
        with closing(sqlite3.connect(self.goals)) as db, db:
            db.execute("UPDATE thread_goals SET status='active'")
        self.assertIsNone(goal.blocked_goal(self.target, 12345))
        with closing(sqlite3.connect(self.goals)) as db, db:
            db.execute("UPDATE thread_goals SET status='blocked'")
        self.files[self.goals]["inode"] += 1
        self.assertIsNone(goal.blocked_goal(self.target, 12345))

    def test_wrong_workspace_pid_generation_or_older_goal_cannot_authorize(self):
        self.assertIsNone(goal.blocked_goal({**self.target, "workspace_id": "other"}, 12345))
        with patch.object(goal.scope, "process", side_effect=[self.process, {**self.process, "birth": [999, 1]}]):
            self.assertIsNone(goal.blocked_goal(self.target, 12345))
        with closing(sqlite3.connect(self.goals)) as db, db:
            db.execute("UPDATE thread_goals SET updated_at_ms=updated_at_ms-60000")
        self.assertIsNone(goal.blocked_goal(self.target, 12345))

    def submission(self, *, turn=None, text='任务请继续', mode=None):
        mode = mode or ('Steer { expected_turn_id: ' + json.dumps(turn or self.tid) + ' }')
        return (f'session_loop{{thread_id={self.sid}}}: Submission sub=Submission {{ id: "{uuid.uuid4()}", '
                'op: TurnInput { request: TurnInputRequest { input: UserInput { content: [Text { text: '
                + json.dumps(text, ensure_ascii=False) + ', text_elements: [] }], client_id: Some("client") }, '
                'thread_settings: ThreadSettingsOverrides { model: None }, start: TurnStartOptions { turn_trigger: None }, '
                'additional_context: {}, responsesapi_client_metadata: None, trace: None }, mode: ' + mode + ', '
                'reply: Sender { inner: Some(Inner { state: State { is_complete: false } }) } }, '
                'trace: None, parent_turn_id: None, root_turn_id: None }')

    def append_submission(self, body, *, process='pid:12345:process-generation', row_id=2):
        with closing(sqlite3.connect(self.logs)) as db, db:
            db.execute('INSERT INTO logs VALUES(?,?,?,?,?,?,?)', (
                row_id, int(self.now) + row_id, 0, self.sid, 'codex_core::session::handlers', body, process))

    def test_matching_steer_after_failure_keeps_blocked_goal(self):
        original = goal.blocked_goal(self.target, 12345)
        self.append_submission(self.submission())
        proof = goal.blocked_goal(self.target, 12345)
        self.assertEqual(proof['turn_id'], self.tid)
        self.assertNotEqual(original['submission_digest'], proof['submission_digest'])

    def test_quoted_fields_never_override_outer_mode(self):
        spoof = '" }, mode: Steer { expected_turn_id: "' + self.tid + '" }, op: UserInput {'
        self.append_submission(self.submission(text=spoof, mode='Start'))
        self.assertIsNone(goal.blocked_goal(self.target, 12345))

    def test_balanced_quoted_user_content_is_not_a_new_turn(self):
        self.append_submission(self.submission(text='继续 "{mode: Start}" \\ [不要删除]'))
        self.assertIsNotNone(goal.blocked_goal(self.target, 12345))

    def test_wrong_turn_or_generation_steer_is_rejected(self):
        for body, process in ((self.submission(turn=str(uuid.uuid4())), 'pid:12345:process-generation'),
                              (self.submission(), 'pid:12345:replacement')):
            with self.subTest(body=body[:70], process=process):
                self.append_submission(body, process=process)
                self.assertIsNone(goal.blocked_goal(self.target, 12345))
                with closing(sqlite3.connect(self.logs)) as db, db:
                    db.execute('DELETE FROM logs WHERE id=2')

    def test_interrupt_unknown_and_truncated_submission_remain_vetoes(self):
        for body in (self.submission()[:-2], self.submission().replace('op: TurnInput', 'op: Unknown'),
                     f'session_loop{{thread_id={self.sid}}}: Submission sub=Submission {{ op: Interrupt }}'):
            self.append_submission(body)
            self.assertIsNone(goal.blocked_goal(self.target, 12345))
            with closing(sqlite3.connect(self.logs)) as db, db:
                db.execute('DELETE FROM logs WHERE id=2')

    def test_older_unknown_submission_is_not_hidden_by_newer_matching_steer(self):
        self.append_submission('Submission sub=unknown')
        self.append_submission(self.submission(), row_id=3)
        self.assertIsNone(goal.blocked_goal(self.target, 12345))

    def test_missing_error_beyond_bounded_history_rejects(self):
        for i in range(2, 68):
            self.append_submission(self.submission(), row_id=i)
        self.assertIsNone(goal.blocked_goal(self.target, 12345))

    def test_submission_changed_during_goal_reread_rejects(self):
        original = goal.sqlite3.connect
        calls = 0
        def connect(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                with closing(original(self.logs)) as db, db:
                    db.execute('INSERT INTO logs VALUES(?,?,?,?,?,?,?)', (
                        2, int(self.now) + 1, 0, self.sid, 'codex_core::session::handlers',
                        self.submission(), 'pid:12345:process-generation'))
            return original(*args, **kwargs)
        with patch.object(goal.sqlite3, 'connect', side_effect=connect):
            self.assertIsNone(goal.blocked_goal(self.target, 12345))


class GoalResumeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.payload = status_payload("rate_limit")
        row = self.payload["render_grid"]["cursor"]["row"]
        self.payload["render_grid"]["row_spans"].append(span(row + 2, 2, "GPT-6-Astra · Goal stalled (/goal resume)", 1))
        self.client = FakeClient(self.payload, visible_text(self.payload))
        self.client.resume_codex_goal = lambda wid, sid, **kwargs: self.client.send(wid, sid, "/goal resume")
        self.daemon = armed_daemon(self.temp.name, self.client)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.daemon.codex_queue_recovery.current_turn = lambda _: {"kind": "unknown"}
        self.daemon.codex_queue_recovery.process_lookup = lambda _: {"agent_kind": "codex", "agent_pids": [12345]}
        self.proof = {"session_id": "original", "goal_id": "goal", "turn_id": "failed", "at": 200,
                      "model_provider": "synthetic-provider", "error": {"message": ERRORS["rate_limit"]}}
        self.retry_now = 1000.0
        self.daemon._provider_retry = ProviderRetryStore(self.daemon._provider_retry.path,
            clock=lambda: self.retry_now, jitter=lambda: 0)
        self.daemon._provider_retry.observe('original', 'synthetic-provider', 'failed',
            'rate_limit', ERRORS['rate_limit'], 200)
        self.retry_now += 15

    def test_goal_waits_for_durable_error_delay_before_one_resume(self):
        self.retry_now = 1000.0
        with patch.object(goal, 'blocked_goal', return_value=self.proof):
            self.daemon.process_once(self.client)
            self.assertEqual(self.client.sent, [])
            self.retry_now = 1015.0
            self.daemon.process_once(self.client)
            self.assertEqual([row[-1] for row in self.client.sent], ['/goal resume'])

    def test_only_verified_stalled_goal_uses_native_resume_once(self):
        with patch.object(goal, "blocked_goal", return_value=self.proof):
            self.daemon.process_once(self.client)
            runtime = self.daemon.runtime["surface-uuid"]
            runtime.awaiting = False
            runtime.last_send_at = 0
            self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.client.sent[0][-1], "/goal resume")

    def test_goal_resumed_during_persistence_cancels_io(self):
        with patch.object(goal, "blocked_goal", side_effect=[self.proof, None]):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

    def test_missing_evidence_or_different_native_error_does_not_resume(self):
        for value in (None, {**self.proof, "error": {"message": "Permission denied"}}):
            with patch.object(goal, "blocked_goal", return_value=value):
                self.daemon.process_once(self.client)
                self.assertEqual(self.client.sent, [])

    def test_unverified_goal_continues_a_matching_native_failed_turn(self):
        self.daemon.codex_queue_recovery.current_turn = lambda _: {
            **self.proof, "kind": "task_complete"}
        with patch.object(goal, "blocked_goal", return_value=None):
            self.daemon.process_once(self.client)
        self.assertEqual([row[-1] for row in self.client.sent], ["任务请继续"])

    def test_unverified_goal_without_native_binding_never_uses_viewport_only(self):
        self.daemon.codex_queue_recovery.current_turn = lambda _: None
        with patch.object(goal, 'blocked_goal', return_value=None):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

    def test_high_demand_goal_footer_recovers_only_new_original_failed_turn(self):
        self.payload = status_payload('high_demand')
        row = self.payload['render_grid']['cursor']['row']
        self.payload['render_grid']['row_spans'].append(
            span(row + 2, 2, 'GPT-6-Astra · Goal stalled (/goal resume)', 1))
        self.client.payload, self.client.text = self.payload, visible_text(self.payload)
        turn = {**self.proof, 'kind': 'task_complete', 'error': {'message': ERRORS['high_demand']}}
        self.daemon.codex_queue_recovery.current_turn = lambda _: dict(turn)
        with patch.object(goal, 'blocked_goal', return_value=None):
            self.daemon.process_once(self.client)
            runtime = self.daemon.runtime['surface-uuid']
            for _ in range(3):
                runtime.awaiting = False
                runtime.last_send_at = 0
                self.daemon.process_once(self.client)
            self.assertEqual([r[-1] for r in self.client.sent], ['任务请继续'])
            turn.update(turn_id='new-failure', at=201)
            self.daemon.process_once(self.client)
        self.assertEqual([r[-1] for r in self.client.sent], ['任务请继续', '任务请继续'])

    def test_already_resumed_goal_cannot_receive_plain_continuation(self):
        self.daemon.codex_queue_recovery.current_turn = lambda _: {
            **self.proof, "kind": "task_complete"}
        with patch.object(goal, "blocked_goal", return_value=self.proof):
            self.daemon.process_once(self.client)
            runtime = self.daemon.runtime["surface-uuid"]
            runtime.awaiting = False
            runtime.last_send_at = 0
            self.daemon.process_once(self.client)
        self.assertEqual([row[-1] for row in self.client.sent], ["/goal resume"])

    def test_inflight_user_echo_keeps_verified_blocked_goal_recoverable(self):
        self.payload['render_grid']['row_spans'].append(span(49, 0, '› 请检查原任务，不要丢失消息', 0))
        self.client.text = visible_text(self.payload)
        with patch.object(goal, 'blocked_goal', return_value=self.proof):
            self.daemon.process_once(self.client)
            runtime = self.daemon.runtime['surface-uuid']
            runtime.awaiting, runtime.last_send_at = False, 0
            self.daemon.process_once(self.client)
        self.assertEqual([r[-1] for r in self.client.sent], ['/goal resume'])

    def test_inflight_status_without_goal_cannot_fall_back_to_old_failed_turn(self):
        self.payload['render_grid']['row_spans'].append(span(49, 0, '• 后续消息已到达', 0))
        self.client.text = visible_text(self.payload)
        self.daemon.codex_queue_recovery.current_turn = lambda _: {**self.proof, 'kind': 'task_complete'}
        with patch.object(goal, 'blocked_goal', return_value=None):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])


if __name__ == "__main__":
    unittest.main()

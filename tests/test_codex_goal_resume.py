"""Native goal recovery never replaces lost rollout evidence with a fake Stop."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import ccc_codex_goal as goal
import cmux_codex_watch as core
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


class GoalResumeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.payload = status_payload("rate_limit")
        row = self.payload["render_grid"]["cursor"]["row"]
        self.payload["render_grid"]["row_spans"].append(span(row + 2, 2, "GPT-6-Astra · Goal stalled (/goal resume)", 1))
        self.client = FakeClient(self.payload, visible_text(self.payload))
        self.client.resume_codex_goal = lambda wid, sid: self.client.send(wid, sid, "/goal resume")
        self.daemon = armed_daemon(self.temp.name, self.client)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.daemon.codex_queue_recovery.current_turn = lambda _: {"kind": "unknown"}
        self.daemon.codex_queue_recovery.process_lookup = lambda _: {"agent_kind": "codex", "agent_pids": [12345]}
        self.proof = {"session_id": "original", "goal_id": "goal", "turn_id": "failed", "at": 200,
                      "error": {"message": ERRORS["rate_limit"]}}

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

    def test_unverified_goal_never_falls_back_to_a_matching_native_failed_turn(self):
        self.daemon.codex_queue_recovery.current_turn = lambda _: {
            **self.proof, "kind": "task_complete"}
        with patch.object(goal, "blocked_goal", return_value=None):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

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


if __name__ == "__main__":
    unittest.main()

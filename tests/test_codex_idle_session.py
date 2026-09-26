"""Missing SessionStart Hooks must not strand an idle original native thread."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import ccc_codex_queue as native


class IdleSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.sid = str(uuid.uuid4())
        self.locks = self.root / "thread-writer-locks"
        self.locks.mkdir()
        self.lock = self.locks / f"{self.sid}.lock"
        self.lock.touch()
        self.path = self.sessions / f"rollout-{self.sid}.jsonl"
        self.write_turn()
        self.files = {self.lock: self.identity(self.lock)}
        self.target = {"surface_id": str(uuid.uuid4()).upper(),
                       "workspace_id": str(uuid.uuid4()).upper()}
        self.process = {"pid": 123, "birth": [100, 100], "process_start": 100,
                        "surface_id": self.target["surface_id"],
                        "environment_workspace_id": self.target["workspace_id"],
                        "remote": False, "backend": False}
        self.queue = native.QueueRecovery(self.root / "ledger", self.root / "missing-hooks",
                                          self.sessions, "continue")
        self.queue.process_lookup = lambda _: {"agent_kind": "codex", "agent_pids": [123]}
        for fixture in (
            patch("ccc_codex_queue.process_placement_start", return_value=100),
            patch("ccc_guard_scope.process", side_effect=lambda _: dict(self.process)),
            patch("ccc_guard_scope.arguments", return_value=(["/native/codex"], {})),
            patch("ccc_codex_queue.process_writable_files", side_effect=self.open_files),
        ):
            fixture.start()
            self.addCleanup(fixture.stop)

    @staticmethod
    def identity(path):
        info = path.stat()
        return {"device": info.st_dev, "inode": info.st_ino}

    def open_files(self, pid, *, identities=False):
        return dict(self.files) if identities else set(self.files)

    def write_turn(self, at=200):
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.sid}}) + "\n" +
            json.dumps({"type": "event_msg", "timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
                        "payload": {"type": "task_complete", "turn_id": "failed-original-turn",
                                    "error": {"message": "We’re currently experiencing high demand, which may cause temporary errors."}}}) + "\n")

    def test_closed_rollout_with_current_writer_lock_supplies_failed_turn(self):
        turn = self.queue.current_turn(self.target)
        self.assertEqual(turn.get("session_id"), self.sid)
        self.assertEqual(turn.get("kind"), "task_complete")
        self.assertEqual(turn.get("birth"), [100, 100])
        sources = self.queue.wakeup_sources([self.target])
        self.assertEqual([str(s["path"]) for s in sources], [str(self.path)])

    def test_different_surface_or_workspace_cannot_supply_a_turn(self):
        for field in ("surface_id", "environment_workspace_id"):
            with self.subTest(field=field):
                original = self.process[field]
                self.process[field] = str(uuid.uuid4()).upper()
                self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})
                self.process[field] = original

    def test_duplicate_or_missing_writer_locks_remain_unknown(self):
        second = self.locks / f"{uuid.uuid4()}.lock"
        second.touch()
        self.files[second] = self.identity(second)
        self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})
        self.files.clear()
        self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})

    def test_lock_path_replacement_and_foreign_native_home_are_rejected(self):
        self.files[self.lock]["inode"] += 1
        self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})
        self.files = {self.lock: self.identity(self.lock)}
        self.queue.sessions_root = self.root / "unrelated-sessions"
        self.queue.sessions_root.mkdir()
        self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})

    def test_ambiguous_rollouts_and_wrong_session_metadata_are_rejected(self):
        duplicate = self.sessions / f"second-{self.sid}.jsonl"
        duplicate.write_bytes(self.path.read_bytes())
        self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})
        duplicate.unlink()
        self.path.write_text(self.path.read_text().replace(self.sid, str(uuid.uuid4())))
        self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})

    def test_process_generation_change_or_dropped_lock_during_read_is_unknown(self):
        original = native.task_snapshot
        def changed(*args):
            result = original(*args)
            self.process["birth"] = [100, 101]
            return result
        with patch("ccc_codex_queue.task_snapshot", side_effect=changed):
            self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})
        self.process["birth"] = [100, 100]
        def dropped(*args):
            result = original(*args)
            self.files.clear()
            return result
        with patch("ccc_codex_queue.task_snapshot", side_effect=dropped):
            self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})

    def test_transcript_before_this_process_birth_is_not_a_current_failure(self):
        self.write_turn(at=100.00005)
        self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})

    def test_remote_client_cannot_use_a_local_writer_lock(self):
        for argv in (["/native/codex", "--remote", "ws://remote"],
                     ["/native/codex", "--remote=ws://remote"]):
            with patch("ccc_guard_scope.arguments", return_value=(argv, {})):
                self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})

    def test_replaced_cached_transcript_is_not_rebound(self):
        self.assertEqual(self.queue.current_turn(self.target).get("session_id"), self.sid)
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})


if __name__ == "__main__":
    unittest.main()

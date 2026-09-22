"""Native completions accelerate observation without granting input permission."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ccc_codex_queue import NativeCompletionWatcher, QueueRecovery


class NativeWakeupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "original.jsonl"
        self.path.write_text("")
        self.sources = [{"surface_id": "surface", "workspace_id": "workspace",
                         "session_id": "session", "path": self.path}]
        self.woken = []
        self.watcher = NativeCompletionWatcher(lambda: self.sources, self.wake)

    def wake(self, sid, wid):
        self.woken.append((sid, wid))
        return True

    def event(self, kind="task_complete", turn="turn", error=True):
        with self.path.open("a") as handle:
            handle.write(json.dumps({"type": "event_msg", "timestamp": "2026-09-22T10:00:00Z",
                "payload": {"type": kind, "turn_id": turn,
                            "error": {"message": "high demand"} if error else None}}) + "\n")

    def test_changed_file_wakes_failed_turn_once_and_unchanged_file_is_not_read(self):
        self.event()
        self.watcher.scan()
        self.assertEqual(self.woken, [("surface", "workspace")])
        with patch.object(Path, "open", side_effect=AssertionError("unchanged transcript reread")):
            self.watcher.scan()
        with self.path.open("a") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
        self.watcher.scan()
        self.assertEqual(len(self.woken), 1)
        self.event(turn="next-turn")
        self.watcher.scan()
        self.assertEqual(len(self.woken), 2)

    def test_started_aborted_successful_or_new_user_turn_never_wakes_old_failure(self):
        for kind in ("task_started", "turn_aborted", "user_message", "task_complete"):
            with self.subTest(kind=kind):
                self.path.write_text("")
                self.event()
                self.event(kind, turn="next", error=False)
                self.watcher.scan()
                self.assertEqual(self.woken, [])

    def test_hint_retries_when_surface_is_not_yet_scheduled(self):
        self.event()
        with patch.object(self.watcher, "wake", return_value=False):
            self.watcher.scan()
        self.watcher.scan()
        self.assertEqual(len(self.woken), 1)

    def test_bounded_tail_skips_large_or_incomplete_records(self):
        self.path.write_text(json.dumps({"type": "padding", "text": "x" * 200000}) + "\n")
        self.event()
        self.watcher.tail_bytes = 512
        self.watcher.scan()
        self.assertEqual(len(self.woken), 1)
        with self.path.open("a") as handle:
            handle.write('{"type":"event_msg","payload":')
        self.watcher.scan()
        self.assertEqual(len(self.woken), 1)

    def test_missing_source_does_not_block_other_surfaces(self):
        self.event()
        self.sources.insert(0, {**self.sources[0], "surface_id": "missing", "path": self.root / "gone"})
        self.watcher.scan()
        self.assertEqual(self.woken, [("surface", "workspace")])
        self.sources.clear()
        self.watcher.scan()
        self.assertEqual(self.watcher.signatures, {})

    def test_sources_use_enabled_workspace_uuid_and_latest_known_process(self):
        queue = QueueRecovery(self.root / "ledger", self.root / "bindings", self.root, "continue")
        queue.open_file_sources["surface"] = {**self.sources[0], "process_start": 20}
        records = {"old-session": {"surfaceId": "surface", "workspaceId": "workspace",
                                   "transcriptPath": str(self.root / "old.jsonl"), "pidStartSeconds": 10}}
        target = {"surface_id": "surface", "workspace_id": "workspace"}
        with patch.object(queue, "records", return_value=records):
            self.assertEqual(queue.wakeup_sources([target])[0]["session_id"], "session")
            for override in ({"paused": True}, {"enabled": False}, {"workspace_id": "other"}):
                self.assertEqual(queue.wakeup_sources([{**target, **override}]), [])
            queue.open_file_sources["surface"]["path"] = self.root.parent / "outside.jsonl"
            sources = queue.wakeup_sources([target])
            self.assertTrue(all(Path(s["path"]).is_relative_to(self.root) for s in sources))


if __name__ == "__main__":
    unittest.main()

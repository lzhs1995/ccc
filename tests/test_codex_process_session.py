import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ccc_codex_queue import QueueRecovery


class ProcessSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "rollout.jsonl"
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "original"}}) + "\n" +
            json.dumps({"type": "event_msg", "timestamp": "2026-09-22T08:00:00Z", "payload": {
                "type": "task_complete", "turn_id": "failed-turn", "error": {"message": "rate limit exceeded"}}}) + "\n")
        self.queue = QueueRecovery(self.root / "ledger", self.root / "missing-hooks", self.root, "continue")
        self.queue.process_lookup = lambda _: {"agent_kind": "codex", "agent_pids": [123]}
        self.target = {"surface_id": "s", "workspace_id": "w"}
        self.command = "Tue Sep 22 01:00:00 2026 /opt/homebrew/bin/codex resume original CMUX_SURFACE_ID=s CMUX_WORKSPACE_ID=w"
        self.paths = "n" + str(self.path) + "\n"

    def run_command(self, args, **kwargs):
        return subprocess.CompletedProcess(args, 0, self.paths if args[0].endswith("lsof") else self.command, "")

    def test_missing_hook_uses_current_process_original_open_transcript(self):
        with patch("ccc_codex_queue.subprocess.run", side_effect=self.run_command) as run:
            result = self.queue.current_turn(self.target)
        self.assertEqual((result["kind"], result["session_id"], result["pid"]), ("task_complete", "original", 123))
        self.assertTrue(all(call.args[0][0] in {"/bin/ps", "/usr/sbin/lsof"} for call in run.call_args_list))

    def test_foreign_surface_identity_cannot_supply_a_turn(self):
        self.command = self.command.replace("CMUX_SURFACE_ID=s", "CMUX_SURFACE_ID=foreign")
        with patch("ccc_codex_queue.subprocess.run", side_effect=self.run_command):
            self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})

    def test_multiple_open_sessions_cannot_supply_a_turn(self):
        second = self.root / "another.jsonl"
        second.write_text(self.path.read_text())
        self.paths += "n" + str(second) + "\n"
        with patch("ccc_codex_queue.subprocess.run", side_effect=self.run_command):
            self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})

    def test_process_replacement_during_read_blocks_evidence(self):
        calls = 0
        def run(args, **kwargs):
            nonlocal calls
            result = self.run_command(args, **kwargs)
            if args[0] == "/bin/ps":
                calls += 1
                if calls == 2:
                    result.stdout = result.stdout.replace("01:00:00", "01:01:00")
            return result
        with patch("ccc_codex_queue.subprocess.run", side_effect=run):
            self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})

    def test_unreadable_process_is_unknown(self):
        with patch("ccc_codex_queue.subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 2)):
            self.assertEqual(self.queue.current_turn(self.target), {"kind": "unknown"})


if __name__ == "__main__":
    unittest.main()

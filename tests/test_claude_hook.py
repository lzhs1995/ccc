import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_ccc_protocol import (  # noqa: E402
    DEFAULT_CLAUDE_MESSAGE,
    append_event,
    build_event,
    completion_reported,
    notify_daemon,
    prompt_kind,
)
from cmux_codex_watch import (  # noqa: E402
    CLAUDE_HOOK_COMMAND,
    CLAUDE_HOOK_EVENTS,
    ClaudeEventInbox,
    ClaudeEventLedger,
    ClaudeEventWorkerPool,
    ClaudeHookSettingsManager,
    hook_sla_missed,
)


class ClaudeJournalRecoveryTests(unittest.TestCase):
    def event(self, name, at, event_id, **extra):
        return {"version": 1, "event_id": event_id, "created_at": at,
                "event_name": name, "session_id": "session-a", "surface_id": "surface-a",
                "agent_pid": 100, **extra}

    def replay(self, events, known=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for event in events:
                append_event(event, root / "events.jsonl")
            inbox = ClaudeEventInbox(root / "events.jsonl", root / "events.sock")
            inbox.replay_journal(set(known))
            result = []
            while event := inbox.get_nowait():
                result.append(event["event_id"])
            return result

    def test_newer_prompt_supersedes_older_unfinished_stop_during_replay(self):
        now = time.time()
        events = [self.event("Stop", now - 30, "old-stop", completed=False),
                  self.event("UserPromptSubmit", now - 20, "new-prompt", prompt_kind="human"),
                  self.event("Stop", now - 10, "current-stop", completed=False)]
        self.assertEqual(self.replay(events), ["new-prompt", "current-stop"])
        self.assertEqual(self.replay(events, known={"new-prompt"}), ["current-stop"])

    def test_accepted_watchdog_prompt_also_supersedes_older_stop(self):
        now = time.time()
        events = [self.event("StopFailure", now - 2, "old-failure", completed=False),
                  self.event("UserPromptSubmit", now - 1, "accepted", prompt_kind="watchdog")]
        self.assertEqual(self.replay(events), ["accepted"])

    def test_replay_supersession_requires_same_surface_session_and_pid(self):
        now = time.time()
        for changed in ({"surface_id": "other"}, {"session_id": "other"}, {"agent_pid": 200}):
            with self.subTest(changed=changed):
                events = [self.event("Stop", now - 2, "stop", completed=False),
                          self.event("UserPromptSubmit", now - 1, "prompt", **changed)]
                self.assertEqual(self.replay(events), ["stop", "prompt"])

    def test_completed_stop_is_retained_for_the_completion_latch(self):
        now = time.time()
        events = [self.event("Stop", now - 2, "completed", completed=True),
                  self.event("UserPromptSubmit", now - 1, "echo", prompt_kind="watchdog")]
        self.assertEqual(self.replay(events), ["completed", "echo"])

    def test_older_prompt_and_unknown_pid_do_not_supersede_a_stop(self):
        now = time.time()
        events = [self.event("Stop", now - 1, "stop", completed=False),
                  self.event("UserPromptSubmit", now - 2, "older-prompt", prompt_kind="human")]
        self.assertEqual(self.replay(events), ["stop", "older-prompt"])
        events = [self.event("Stop", now - 2, "stop", agent_pid=0, completed=False),
                  self.event("UserPromptSubmit", now - 1, "unknown", agent_pid=0)]
        self.assertEqual(self.replay(events), ["stop", "unknown"])


class ClaudeHookProtocolTests(unittest.TestCase):
    def test_settings_repair_preserves_unrelated_values_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            original = {
                "env": {"TOKEN": "must-stay-byte-for-byte-semantically"},
                "model": "custom-model",
                "hooks": {
                    "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}],
                    "Stop": [{"matcher": "*", "hooks": [
                        {"type": "command", "command": CLAUDE_HOOK_COMMAND, "timeout": 5},
                        {"type": "command", "command": CLAUDE_HOOK_COMMAND, "timeout": 5},
                        {"type": "command", "command": "unrelated-stop-hook"},
                    ]}],
                },
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            manager = ClaudeHookSettingsManager(
                path,
                lock_path=root / "settings.lock",
                backup_dir=root / "backups",
            )
            report = manager.ensure(repair=True)
            repaired = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(report["changed"])
            self.assertEqual(repaired["env"], original["env"])
            self.assertEqual(repaired["model"], original["model"])
            self.assertEqual(repaired["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
            self.assertEqual(report["event_counts"], dict.fromkeys(CLAUDE_HOOK_EVENTS, 1))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(list((root / "backups").glob("claude-settings.*.json"))), 1)

    def test_settings_repair_rejects_malformed_json_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            raw = b'{"hooks": '
            path.write_bytes(raw)
            manager = ClaudeHookSettingsManager(
                path,
                lock_path=root / "settings.lock",
                backup_dir=root / "backups",
            )
            with self.assertRaisesRegex(RuntimeError, "cannot safely repair"):
                manager.ensure(repair=True)
            self.assertEqual(path.read_bytes(), raw)
            self.assertFalse((root / "backups").exists())

    def test_ledger_claim_is_atomic_under_concurrent_duplicate_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ClaudeEventLedger(Path(directory) / "ledger.json")
            base = {
                "version": 1,
                "event_id": "same-event",
                "created_at": time.time(),
                "event_name": "Stop",
                "session_id": "session-a",
            }
            barrier = threading.Barrier(100)
            outcomes = []
            outcome_lock = threading.Lock()

            def claim(index):
                barrier.wait()
                result = ledger.claim_if_absent({**base, "surface_id": f"surface-{index}"})
                with outcome_lock:
                    outcomes.append(result)

            workers = [threading.Thread(target=claim, args=(index,)) for index in range(100)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=2)
            self.assertEqual(outcomes.count(True), 1)
            self.assertEqual(list(ledger.events), ["same-event"])

    def test_live_hook_sla_boundary_is_strict(self):
        self.assertFalse(hook_sla_missed(1.0, 1.0))
        self.assertTrue(hook_sla_missed(1.000001, 1.0))

    def test_completion_requires_the_final_usage_suffix(self):
        self.assertTrue(completion_reported("完成，建议检查 usage: /context"))
        self.assertTrue(completion_reported("完成，建议检查\nusage： /context。 ”"))
        self.assertFalse(completion_reported("建议检查 usage: /context 后我又继续做了"))
        self.assertFalse(completion_reported("任务完成了"))

    def test_prompt_source_distinguishes_watchdog_from_human(self):
        self.assertEqual(prompt_kind(DEFAULT_CLAUDE_MESSAGE, DEFAULT_CLAUDE_MESSAGE), "watchdog")
        self.assertEqual(prompt_kind("请实现下一项任务", DEFAULT_CLAUDE_MESSAGE), "human")

    def test_stop_event_contains_hashes_not_raw_assistant_text(self):
        secret_text = "内部正文，完成，建议检查 usage: /context"
        event = build_event({
            "hook_event_name": "Stop",
            "session_id": "session-a",
            "transcript_path": "/private/transcript.jsonl",
            "cwd": "/private/project",
            "last_assistant_message": secret_text,
            "stop_hook_active": False,
        }, {"CMUX_SURFACE_ID": "surface-a", "CMUX_WORKSPACE_ID": "workspace-a"})
        self.assertIsNotNone(event)
        self.assertTrue(event["completed"])
        self.assertEqual(event["surface_id"], "surface-a")
        self.assertNotIn(secret_text, json.dumps(event, ensure_ascii=False))
        self.assertNotIn("/private/transcript.jsonl", json.dumps(event))

    def test_stop_failure_is_typed_without_persisting_error_text(self):
        raw_error = "503 private upstream details"
        event = build_event({
            "hook_event_name": "StopFailure",
            "session_id": "session-a",
            "error": raw_error,
        }, {"CMUX_SURFACE_ID": "surface-a"})
        self.assertEqual(event["error_kind"], "claude_503")
        self.assertNotIn(raw_error, json.dumps(event))

    def test_subagent_stop_is_not_a_ccc_lifecycle_event(self):
        self.assertIsNone(build_event({"hook_event_name": "SubagentStop"}, {}))

    def test_session_start_is_a_health_event_with_no_message_content(self):
        event = build_event({
            "hook_event_name": "SessionStart",
            "session_id": "session-a",
            "source": "startup",
        }, {
            "CMUX_SURFACE_ID": "surface-a",
            "CMUX_WORKSPACE_ID": "workspace-a",
            "CLAUDE_PID": "1234",
        })
        self.assertEqual(event["event_name"], "SessionStart")
        self.assertEqual(event["agent_pid"], 1234)
        self.assertEqual(event["source"], "startup")
        self.assertNotIn("prompt_kind", event)
        self.assertNotIn("completed", event)

    def test_journal_replay_and_ledger_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = root / "events.jsonl"
            sock = root / "events.sock"
            ledger_path = root / "ledger.json"
            event = {
                "version": 1,
                "event_id": "event-a",
                "created_at": time.time(),
                "event_name": "Stop",
                "session_id": "session-a",
                "surface_id": "surface-a",
            }
            append_event(event, journal)
            inbox = ClaudeEventInbox(journal, sock)
            inbox.replay_journal(set())
            self.assertEqual(inbox.get_nowait()["event_id"], "event-a")
            ledger = ClaudeEventLedger(ledger_path)
            ledger.mark(event, "sent")
            second = ClaudeEventInbox(journal, sock)
            second.replay_journal(ledger.known_ids())
            self.assertIsNone(second.get_nowait())

    def test_socket_and_journal_delivery_of_same_event_queue_once_with_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = ClaudeEventInbox(root / "events.jsonl", root / "events.sock")
            event = {
                "version": 1,
                "event_id": "event-source-race",
                "created_at": time.time(),
                "event_name": "Stop",
                "session_id": "session-a",
                "surface_id": "surface-a",
            }
            inbox._enqueue(event, source="journal_replay")
            inbox._enqueue(event, source="socket")
            queued = inbox.get_nowait()
            self.assertEqual(queued["event_id"], "event-source-race")
            self.assertEqual(queued["_inbox_source"], "journal_replay")
            self.assertIsNone(inbox.get_nowait())

    def test_unix_socket_wakes_the_daemon_inbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = ClaudeEventInbox(root / "events.jsonl", root / "events.sock")
            inbox.start(set())
            try:
                event = {
                    "version": 1,
                    "event_id": "live-event",
                    "created_at": time.time(),
                    "event_name": "Stop",
                    "session_id": "session-a",
                    "surface_id": "surface-a",
                }
                notify_daemon(event, root / "events.sock")
                inbox.wait(0.5)
                self.assertEqual(inbox.get_nowait()["event_id"], "live-event")
            finally:
                inbox.close()

    def test_worker_pool_preserves_surface_order_and_does_not_block_other_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = ClaudeEventInbox(root / "events.jsonl", root / "events.sock")
            blocked = threading.Event()
            release = threading.Event()
            fast = threading.Event()
            seen = []

            def handler(event, _client):
                if event["event_id"] == "slow-1":
                    blocked.set()
                    release.wait(1)
                seen.append(event["event_id"])
                if event["event_id"] == "fast-1":
                    fast.set()

            pool = ClaudeEventWorkerPool(inbox, handler, object, workers=2)
            slow_surface = "surface-slow"
            fast_surface = next(
                f"surface-fast-{index}"
                for index in range(100)
                if pool._shard({"surface_id": f"surface-fast-{index}"})
                != pool._shard({"surface_id": slow_surface})
            )
            pool.start()
            try:
                base = {
                    "version": 1,
                    "created_at": time.time(),
                    "event_name": "Stop",
                    "session_id": "session-a",
                }
                inbox._enqueue({**base, "event_id": "slow-1", "surface_id": slow_surface})
                inbox._enqueue({**base, "event_id": "slow-2", "surface_id": slow_surface})
                inbox._enqueue({**base, "event_id": "fast-1", "surface_id": fast_surface})
                self.assertTrue(blocked.wait(0.5))
                self.assertTrue(fast.wait(0.5), "another surface was blocked behind the slow shard")
                self.assertNotIn("slow-2", seen)
                release.set()
                deadline = time.time() + 1
                while "slow-2" not in seen and time.time() < deadline:
                    time.sleep(0.01)
                self.assertLess(seen.index("slow-1"), seen.index("slow-2"))
            finally:
                release.set()
                pool.close()


if __name__ == "__main__":
    unittest.main()

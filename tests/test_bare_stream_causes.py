"""Bare native transport failures use the existing stream recovery gates."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import ccc_codex_queue as queue
import cmux_codex_watch as watch
from tests.test_watch import FakeClient, armed_daemon, grid_payload, visible_lines


CAUSES = (
    "Encrypted function output content could not be decrypted or decoded.",
    "idle timeout waiting for SSE",
)


class BareStreamCausesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def daemon(self, message, **frame):
        payload = grid_payload([], error=message, **frame)
        client = FakeClient(payload, "\n".join(visible_lines(payload)))
        folder = self.root / str(len(list(self.root.iterdir())))
        folder.mkdir()
        daemon = armed_daemon(folder, client)
        self.addCleanup(daemon._process_snapshots.close)
        turn = {"kind": "task_complete", "session_id": "original", "turn_id": "failed",
                "at": 100, "error": {"message": message}}
        daemon.codex_queue_recovery.current_turn = Mock(return_value=turn)
        return daemon, client, turn, folder

    def test_complete_bare_causes_are_stream_errors(self):
        for message in CAUSES:
            with self.subTest(message=message):
                self.assertTrue(queue._retryable_completed_message(message))
                self.assertEqual(watch._match_error_block("■ " + message), "stream")
                payload = grid_payload([], error=message)
                self.assertEqual(watch.classify_grid(watch.Grid.from_rpc(payload, "surface-uuid")).kind,
                                 "recoverable_error")

    def test_full_stream_prefix_remains_supported(self):
        for cause in CAUSES:
            message = "stream disconnected before completion: " + cause
            self.assertTrue(queue._retryable_completed_message(message))
            self.assertEqual(watch._match_error_block("■ " + message), "stream")

    def test_only_complete_causes_accept_terminal_whitespace(self):
        for message in CAUSES:
            wrapped = message[:12] + "\n" + message[12:]
            self.assertTrue(queue._retryable_completed_message(wrapped))
            self.assertEqual(watch._match_error_block("■ " + wrapped), "stream")
            for invalid in (message[:-1], '"' + message + '"', "Example: " + message,
                            message + " retry now", "401 Unauthorized: " + message,
                            "403 Forbidden: " + message, "413 Payload Too Large: " + message,
                            "insufficient_quota " + message):
                with self.subTest(message=invalid):
                    self.assertFalse(queue._retryable_completed_message(invalid))
                    self.assertIsNone(watch._match_error_block("■ " + invalid))

    def test_unmarked_transcript_prose_is_not_a_failure_card(self):
        for message in CAUSES:
            payload = grid_payload([message])
            state = watch.classify_grid(watch.Grid.from_rpc(payload, "surface-uuid"))
            self.assertNotEqual(state.kind, "recoverable_error")

    def test_rollout_requires_original_session_latest_completed_turn_and_age(self):
        path, now = self.root / "session.jsonl", time.time()
        for message in CAUSES:
            meta = {"type": "session_meta", "payload": {"id": "original"}}
            complete = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10)),
                        "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "failed",
                        "error": {"message": message}}}
            path.write_text("\n".join(json.dumps(x) for x in (meta, complete)) + "\n")
            self.assertIsNotNone(queue.completed_error(path, "original", now))
            self.assertIsNone(queue.completed_error(path, "replacement", now))
            self.assertIsNone(queue.completed_error(path, "original", now - 9))
            self.assertIsNone(queue.completed_error(path, "original", now + 86400))
            for kind in ("task_started", "turn_aborted", "user_message"):
                event = {**complete, "payload": {"type": kind}}
                path.write_text("\n".join(json.dumps(x) for x in (meta, complete, event)) + "\n")
                self.assertIsNone(queue.completed_error(path, "original", now))

    def test_existing_native_gate_submits_once_and_persists_deduplication(self):
        for message in CAUSES:
            daemon, client, turn, folder = self.daemon(message)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(client.sent[0][-1], watch.MESSAGE)
            restarted = armed_daemon(folder, client)
            self.addCleanup(restarted._process_snapshots.close)
            restarted.codex_queue_recovery.current_turn = Mock(return_value=turn)
            restarted.runtime["surface-uuid"].awaiting = False
            restarted.runtime["surface-uuid"].last_send_at = 0
            restarted.process_once(client)
            self.assertEqual(len(client.sent), 1)

    def test_active_aborted_unknown_and_different_native_failure_block_input(self):
        for message in CAUSES:
            for native in ({"kind": "unknown"}, {"kind": "task_started"}, {"kind": "turn_aborted"},
                           {"kind": "user_message"}, {"kind": "task_complete", "error": None},
                           {"kind": "task_complete", "error": {"message": "Permission denied"}}):
                daemon, client, _, _ = self.daemon(message)
                daemon.codex_queue_recovery.current_turn.return_value = native
                daemon.process_once(client)
                self.assertEqual(client.sent, [])

    def test_legacy_none_binding_retains_existing_stream_compatibility(self):
        # None is the existing legacy-client contract; it is not native proof.
        # This change does not replace that contract with an unknown binding.
        for message in (*CAUSES, "stream disconnected before completion"):
            daemon, client, _, _ = self.daemon(message)
            daemon.codex_queue_recovery.current_turn.return_value = None
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)

    def test_binding_disappearing_after_observation_cancels_input(self):
        for message in CAUSES:
            daemon, client, turn, _ = self.daemon(message)
            daemon.codex_queue_recovery.current_turn.side_effect = [turn, None]
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].delivery_status, "cancelled")

    def test_native_turn_change_before_io_cancels_input(self):
        for message in CAUSES:
            daemon, client, turn, _ = self.daemon(message)
            daemon.codex_queue_recovery.current_turn.side_effect = [turn, {"kind": "task_started"}]
            daemon.process_once(client)
            self.assertEqual(client.sent, [])
            self.assertEqual(daemon.runtime["surface-uuid"].delivery_status, "cancelled")

    def test_draft_working_and_dialog_still_block_input(self):
        for message in CAUSES:
            for frame in ({"composer": "busy"}, {"working": True}, {"menu": True}):
                daemon, client, _, _ = self.daemon(message, **frame)
                daemon.process_once(client)
                self.assertEqual(client.sent, [])

    def test_unknown_without_unsent_proof_is_not_replayed(self):
        for message in CAUSES:
            daemon, client, _, _ = self.daemon(message)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            runtime = daemon.runtime["surface-uuid"]
            runtime.delivery_status, runtime.awaiting, runtime.last_send_at = "unknown", False, 0
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            self.assertEqual(runtime.delivery_status, "unknown")

    def test_stalled_goal_needs_native_goal_proof(self):
        from tests.test_watch import span
        import ccc_codex_goal as goal
        for message in CAUSES:
            daemon, client, turn, _ = self.daemon(message)
            row = client.payload["render_grid"]["cursor"]["row"]
            client.payload["render_grid"]["row_spans"].append(
                span(row + 2, 30, "Goal stalled (/goal resume)"))
            client.text = "\n".join(visible_lines(client.payload))
            client.resume_codex_goal = lambda wid, sid: client.send(wid, sid, "/goal resume")
            daemon.codex_queue_recovery.current_turn.return_value = {"kind": "unknown"}
            daemon.codex_queue_recovery.process_lookup = Mock(return_value={"agent_kind": "codex", "agent_pids": [12345]})
            with patch.object(goal, "blocked_goal", return_value=None):
                daemon.process_once(client)
                self.assertEqual(client.sent, [])
            with patch.object(goal, "blocked_goal", return_value={**turn, "goal_id": "goal"}):
                daemon.process_once(client)
                self.assertEqual([value[-1] for value in client.sent], ["/goal resume"])


if __name__ == "__main__":
    unittest.main()

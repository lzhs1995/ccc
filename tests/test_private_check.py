"""b text needs original task provenance; temporary files only, no native I/O."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

import ccc_codex_queue as native
import ccc_private_check as check
import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests.test_watch import FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload, span


class CheckFixture:
    def setup_check(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.sessions = self.root / ".codex/sessions"
        self.sessions.mkdir(parents=True)
        self.sid, self.wid, self.jid, self.session, self.launch = [str(uuid.uuid4()) for _ in range(5)]
        self.target = {"surface_id": self.sid, "workspace_id": self.wid, "enabled": True}
        self.path = self.sessions / (self.session + ".jsonl")
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.session}}) + "\n")
        self.first_start = time.time() - 20
        self.append_turn("first", self.first_start)
        stat = self.path.stat()
        self.slot = {"index": 0, "phase": "confirmed", "surface_id": self.sid, "session_id": self.session,
                     "pid": 123, "process_start": self.first_start - 1, "launch_id": self.launch,
                     "submit_at": self.first_start - .01, "transcript": str(self.path),
                     "confirmation": {"confirmed": True, "started": True, "prompt": True,
                                      "task_id": "first", "task_at": self.iso(self.first_start),
                                      "identity": [stat.st_dev, stat.st_ino], "session_id": self.session}}
        self.job = {"id": self.jid, "workspace_id": self.wid, "config_path": str(self.config), "status": "complete",
                    "cwd_policy": batch.EMPTY_CWD_POLICY, "initial_prompt": batch.PROMPT,
                    "check_retry_policy": check.POLICY, "slots": [self.slot]}
        self.job_path = batch.job_path(self.config, self.jid)
        self.write_job()
        core.atomic_write_json(self.job_path.parent / "surface-0.json", {
            "workspace_id": self.wid, "surface_id": self.sid, "launch_id": self.launch})
        self.assertTrue(check.record_origin(self.config, self.job, self.slot))
        self.checks = check.PrivateChecks(self.config, self.sessions)

    @staticmethod
    def iso(at):
        return datetime.fromtimestamp(at, timezone.utc).isoformat()

    def write_job(self):
        core.atomic_write_json(self.job_path, self.job)

    def append(self, events):
        with self.path.open("a") as stream:
            for event in events:
                stream.write(json.dumps(event) + "\n")

    def append_turn(self, tid, at, *, message=batch.PROMPT, success=False):
        self.append([
            {"type": "event_msg", "timestamp": self.iso(at), "payload": {"type": "task_started", "turn_id": tid}},
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": message}]}},
            {"type": "event_msg", "timestamp": self.iso(at), "payload": {"type": "user_message", "message": message}},
            {"type": "event_msg", "timestamp": self.iso(at + 1), "payload": {
                "type": "task_complete", "turn_id": tid, "error": None if success else {"message": HIGH_DEMAND_TEXT},
                "last_agent_message": "OK" if success else None}},
        ])

    def read_turn(self, target=None):
        return {"session_id": self.session, "pid": 123, "process_start": self.first_start - 1,
                **(native.task_snapshot(self.path, self.session) or {"kind": "unknown"})}

    def select(self):
        return self.checks.select(self.target, self.read_turn)

    def acknowledged(self):
        proof = self.select()
        self.assertIsNotNone(proof)
        self.checks.reserve(proof, "ours")
        self.checks.finish(proof, "ours", "accepted", io_started_at=self.first_start + 2)
        return proof


class PrivateCheckProofTests(CheckFixture, unittest.TestCase):
    setUp = CheckFixture.setup_check

    def test_original_first_failed_check_selects_exact_prompt(self):
        proof = self.select()
        self.assertEqual(proof["message"], batch.PROMPT)
        self.assertEqual(proof["origin"]["pid"], 123)
        self.assertEqual(proof["failed_turn_id"], "first")

    def test_native_user_message_item_without_legacy_event_is_supported(self):
        events = [json.loads(line) for line in self.path.read_bytes().splitlines()]
        events = [e for e in events if e.get("payload", {}).get("type") != "user_message"]
        events.insert(-1, {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": "first",
            "item": {"type": "UserMessage", "id": "item", "client_id": None,
                     "content": [{"type": "text", "text": batch.PROMPT, "text_elements": []}]}}})
        self.path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        self.assertIsNotNone(self.select())
        events[-2]["payload"]["item"]["content"][0]["text"] = "Actual user task"
        self.path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        self.assertIsNone(self.select())

    def test_known_own_short_retry_extends_the_same_check_chain(self):
        self.acknowledged()
        self.append_turn("second", self.first_start + 5)
        self.assertEqual(self.select()["failed_turn_id"], "second")

    def test_user_same_text_without_our_send_is_not_a_check_chain(self):
        self.append_turn("human", self.first_start + 5)
        self.assertIsNone(self.select())

    def test_legacy_generic_continuation_is_not_reinterpreted(self):
        self.append_turn("legacy", self.first_start + 5, message=core.MESSAGE)
        self.assertIsNone(self.select())

    def test_user_task_failing_in_original_session_uses_normal_semantics(self):
        self.acknowledged()
        self.append_turn("real-task", self.first_start + 5, message="Analyze my project")
        self.assertIsNone(self.select())

    def test_success_permanently_ends_the_original_check_chain(self):
        self.acknowledged()
        self.append_turn("success", self.first_start + 5, success=True)
        self.append_turn("later-failure", self.first_start + 10)
        self.assertIsNone(self.select())

    def test_output_or_goal_tool_also_ends_check_semantics(self):
        for payload in ({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "OK"}]},
                        {"type": "function_call", "name": "create_goal", "arguments": "{}"}):
            before = self.path.read_bytes()
            self.append([{"type": "response_item", "payload": payload}])
            self.assertIsNone(self.select())
            self.path.write_bytes(before)

    def test_missing_native_binding_never_borrows_legacy_client_allowance(self):
        for turn in (None, {"kind": "unknown"}, {**self.read_turn(), "pid": 456},
                     {**self.read_turn(), "session_id": str(uuid.uuid4())},
                     {**self.read_turn(), "process_start": 2}):
            self.assertIsNone(self.checks.select(self.target, lambda: turn))

    def test_n_markers_and_descriptor_never_downgrade_to_private_check(self):
        for field, value in (("access_mode", None), ("access_policy", {}), ("access_policy", None)):
            self.job[field] = value
            self.write_job()
            self.assertIsNone(self.select())
            del self.job[field]
        self.write_job()
        descriptor = self.job_path.parent / "access.json"
        descriptor.write_text("{}")
        self.assertIsNone(self.select())
        descriptor.unlink()
        self.slot["access_ready_at"] = self.first_start
        self.write_job()
        self.assertIsNone(self.select())

    def test_missing_explicit_policy_or_existing_directory_leaves_b_and_old_b_alone(self):
        for change in ({"check_retry_policy": None}, {"cwd_policy": None}, {"initial_prompt": batch.LEGACY_PROMPT}):
            before = copy.deepcopy(self.job)
            self.job.update(change)
            self.write_job()
            self.assertIsNone(self.select())
            self.job = before
        self.write_job()

    def test_newer_batch_in_workspace_does_not_lose_original_surface_lookup(self):
        self.config.write_text(json.dumps({"workspace_rules": [{"workspace_id": self.wid,
            "last_batch_id": str(uuid.uuid4()), "active_batch_id": str(uuid.uuid4())}]}))
        self.assertIsNotNone(self.select())

    def test_duplicate_surface_binding_and_changed_receipt_are_rejected(self):
        self.job["slots"].append(dict(self.slot))
        self.write_job()
        self.assertIsNone(self.select())
        self.job["slots"].pop()
        self.write_job()
        core.atomic_write_json(self.job_path.parent / "surface-0.json", {
            "workspace_id": self.wid, "surface_id": self.sid, "launch_id": str(uuid.uuid4())})
        self.assertIsNone(self.select())

    def test_truncation_replacement_and_partial_log_are_rejected(self):
        self.assertIsNotNone(self.select())
        data = self.path.read_bytes()
        replacement = self.path.with_suffix(".tmp")
        replacement.write_bytes(data)
        replacement.replace(self.path)
        self.assertIsNone(self.select())
        self.path.write_bytes(data[:-1])
        self.assertIsNone(self.select())

    def test_same_size_edit_restoring_mtime_does_not_reuse_cached_history(self):
        self.assertIsNotNone(self.select())
        stat = self.path.stat()
        data = self.path.read_bytes().replace(b"Reply only OK.", b"Reply only NO.")
        self.assertEqual(len(data), stat.st_size)
        self.path.write_bytes(data)
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertIsNone(self.select())

    def test_source_change_during_read_is_not_bound_to_old_bytes(self):
        origin = check._directory(self.config, self.sid) / "origin.json"
        read = Path.read_bytes
        def changing(path):
            data = read(path)
            if path == origin:
                value = json.loads(data)
                value["pid"] = 456
                path.write_text(json.dumps(value))
            return data
        with patch.object(Path, "read_bytes", changing):
            self.assertIsNone(self.select())

    def assert_source_change_during_ledger_read_rejected(self, change):
        original = self.checks._ledger
        def delayed(*args, **kwargs):
            value = original(*args, **kwargs)
            change()
            return value
        with patch.object(self.checks, "_ledger", side_effect=delayed):
            self.assertIsNone(self.select())

    def test_origin_change_during_later_ledger_read_invalidates_proof(self):
        path = check._directory(self.config, self.sid) / "origin.json"
        self.assert_source_change_during_ledger_read_rejected(
            lambda: path.write_text(json.dumps({**json.loads(path.read_text()), "pid": 456})))

    def test_job_change_during_later_ledger_read_invalidates_proof(self):
        self.assert_source_change_during_ledger_read_rejected(
            lambda: core.atomic_write_json(self.job_path, {**self.job, "access_mode": "access-check-v1"}))

    def test_receipt_change_during_later_ledger_read_invalidates_proof(self):
        self.assert_source_change_during_ledger_read_rejected(lambda: core.atomic_write_json(
            self.job_path.parent / "surface-0.json", {"workspace_id": self.wid,
                "surface_id": self.sid, "launch_id": str(uuid.uuid4())}))

    def test_n_descriptor_created_during_ledger_read_invalidates_proof(self):
        self.assert_source_change_during_ledger_read_rejected(
            lambda: (self.job_path.parent / "access.json").write_text("{}"))

    def test_missing_ledger_created_after_read_invalidates_proof(self):
        self.assert_source_change_during_ledger_read_rejected(
            lambda: self.checks._ledger_path(self.target).write_text("{}"))

    def test_user_response_before_next_task_started_invalidates_proof(self):
        self.assert_source_change_during_ledger_read_rejected(lambda: self.append([
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Work on my project"}]}}]))

    def test_goal_change_during_ledger_read_invalidates_proof(self):
        self.assert_source_change_during_ledger_read_rejected(lambda: self.append([
            {"type": "event_msg", "payload": {"type": "goal_updated", "goal_id": "user-goal"}}]))

    def test_final_match_rejects_new_log_bytes_even_if_turn_id_is_unchanged(self):
        proof = self.select()
        turn = self.read_turn()
        self.append([{"type": "event_msg", "payload": {"type": "goal_updated", "goal_id": "user-goal"}}])
        self.assertEqual(self.read_turn()["turn_id"], turn["turn_id"])
        self.assertFalse(check.PrivateChecks.matches_turn(proof, self.read_turn()))
        self.assertFalse(check.PrivateChecks.matches_turn(proof, turn))

    def test_final_match_rejects_same_size_edit_with_restored_mtime(self):
        proof = self.select()
        turn = self.read_turn()
        stat = self.path.stat()
        self.path.write_bytes(self.path.read_bytes().replace(b"Reply only OK.", b"Reply only NO."))
        os.utime(self.path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(self.read_turn()["signature"], turn["signature"])
        self.assertFalse(check.PrivateChecks.matches_turn(proof, turn))

    def test_origin_is_immutable_and_not_a_general_session_registry(self):
        original = (check._directory(self.config, self.sid) / "origin.json").read_bytes()
        self.slot["pid"] = 456
        self.assertFalse(check.record_origin(self.config, self.job, self.slot))
        self.assertEqual((check._directory(self.config, self.sid) / "origin.json").read_bytes(), original)

    def test_unacknowledged_short_message_never_proves_the_next_turn_source(self):
        proof = self.select()
        self.checks.reserve(proof, "unknown")
        self.checks.finish(proof, "unknown", "unknown", io_started_at=self.first_start + 2)
        with self.assertRaises(RuntimeError):
            self.checks.reserve(proof, "duplicate")
        self.append_turn("next", self.first_start + 5)
        self.assertIsNone(self.select())

    def test_proven_unsent_attempt_can_be_retried_but_both_receipts_remain(self):
        proof = self.select()
        self.checks.reserve(proof, "deferred")
        self.checks.finish(proof, "deferred", "not_sent")
        self.checks.reserve(proof, "retry")
        self.assertEqual(set(self.checks._ledger(self.target)), {"deferred", "retry"})


class PrivateCheckDeliveryTests(CheckFixture, unittest.TestCase):
    def setUp(self):
        self.setup_check()
        frame = grid_payload([], error=HIGH_DEMAND_TEXT)
        frame["render_grid"]["surface_id"] = self.sid
        tree = {"windows": [{"id": "window", "workspaces": [{"id": self.wid, "panes": [
            {"id": "pane", "surfaces": [{"id": self.sid, "ref": "surface:9", "type": "terminal"}]}]}]}]}
        self.client = FakeClient(frame, tree=tree)
        self.daemon = armed_daemon(self.root, self.client, extra_targets=[self.target])
        self.addCleanup(self.daemon._process_snapshots.close)
        self.daemon.codex_queue_recovery.current_turn = self.read_turn
        self.checks = self.daemon.private_checks
        self.runtime = core.TargetRuntime()
        self.daemon.runtime[self.sid] = self.runtime
        self.state = core.classify_grid(core.Grid.from_rpc(frame, self.sid))

    def send(self):
        self.daemon._handle_state(self.target, self.runtime, self.state, self.client,
                                  send_guard_tree=self.client.tree())

    def test_first_failure_sends_short_check_with_prior_durable_record(self):
        send = self.client.send
        def inspect(wid, sid, message):
            ledger = self.checks._ledger(self.target)
            self.assertEqual(ledger[self.runtime.send_attempt_id]["phase"], "intent")
            persisted = core.load_json(self.daemon.state_path, {})
            self.assertIn(batch.PROMPT, json.dumps(persisted))
            return send(wid, sid, message)
        with patch.object(self.client, "send", side_effect=inspect):
            self.send()
        self.assertEqual(self.client.sent, [(self.wid, self.sid, batch.PROMPT)])
        self.assertTrue(self.checks.accepted(self.runtime.codex_private_check, self.runtime.send_attempt_id))
        self.assertEqual(self.daemon.config["message"], core.MESSAGE)
        self.assertEqual(self.daemon.codex_queue_recovery.message, core.MESSAGE)

    def test_operator_task_in_same_session_still_receives_normal_continuation(self):
        self.append_turn("operator", time.time() - 5, message="Fix the project tests")
        self.send()
        self.assertEqual(self.client.sent, [(self.wid, self.sid, core.MESSAGE)])
        self.assertEqual(self.runtime.codex_private_check, {})

    def change_default_message_during_persistence(self):
        original = self.daemon.save
        changed = []
        def save(*args, **kwargs):
            result = original(*args, **kwargs)
            if not changed:
                self.daemon.config_store.mutate(lambda c: c.update(message="Continue the current original task."))
                changed.append(True)
            return result
        with patch.object(self.daemon, "save", side_effect=save):
            self.send()
        self.assertTrue(changed)

    def test_ordinary_continuation_keeps_original_fresh_message_timing(self):
        self.job.pop("check_retry_policy")
        self.write_job()
        self.change_default_message_during_persistence()
        self.assertEqual(self.runtime.codex_private_check, {})
        self.assertEqual(self.client.sent, [(self.wid, self.sid, "Continue the current original task.")])

    def test_private_check_uses_own_fixed_message_after_default_changes(self):
        self.change_default_message_during_persistence()
        self.assertTrue(self.runtime.codex_private_check)
        self.assertEqual(self.client.sent, [(self.wid, self.sid, batch.PROMPT)])

    def test_pause_during_check_ledger_fsync_prevents_actual_input(self):
        reserve = self.checks.reserve
        def pausing(*args):
            reserve(*args)
            self.daemon.config_store.mutate(lambda c: c["targets"][0].update(paused=True))
        with patch.object(self.checks, "reserve", side_effect=pausing):
            self.send()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.checks._ledger(self.target)[self.runtime.send_attempt_id]["phase"], "not_sent")

    def test_operator_input_after_check_ledger_fsync_prevents_short_prompt(self):
        reserve = self.checks.reserve
        def typing(*args):
            reserve(*args)
            frame = grid_payload([], error=HIGH_DEMAND_TEXT, composer="busy")
            frame["render_grid"]["surface_id"] = self.sid
            self.client.payload = frame
        with patch.object(self.checks, "reserve", side_effect=typing):
            self.send()
        self.assertEqual(self.client.sent, [])

    def test_changed_native_turn_after_persistence_prevents_input(self):
        reserve = self.checks.reserve
        def changed(*args):
            reserve(*args)
            self.append_turn("operator", time.time() - 5, message="User request")
        with patch.object(self.checks, "reserve", side_effect=changed):
            self.send()
        self.assertEqual(self.client.sent, [])

    def test_moved_surface_after_persistence_prevents_input(self):
        reserve = self.checks.reserve
        def moved(*args):
            reserve(*args)
            self.client.tree_data["windows"][0]["workspaces"][0]["id"] = str(uuid.uuid4())
        with patch.object(self.checks, "reserve", side_effect=moved):
            self.send()
        self.assertEqual(self.client.sent, [])

    def test_missing_source_after_selection_does_not_switch_message_mid_attempt(self):
        reserve = self.checks.reserve
        def removing(*args):
            reserve(*args)
            (check._directory(self.config, self.sid) / "origin.json").unlink()
        with patch.object(self.checks, "reserve", side_effect=removing):
            self.send()
        self.assertEqual(self.client.sent, [])

    def test_ledger_failure_before_io_never_sends(self):
        with patch.object(self.checks, "reserve", side_effect=OSError("disk full")):
            self.send()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.runtime.delivery_status, "failed")

    def test_unknown_send_and_restarted_daemon_do_not_duplicate(self):
        with patch.object(self.client, "send", side_effect=core.UncertainDeliveryError("timeout")) as send:
            self.send()
            restarted = core.WatchDaemon(self.config, self.daemon.state_path, client=self.client)
            self.addCleanup(restarted._process_snapshots.close)
            restarted.codex_queue_recovery.current_turn = self.read_turn
            runtime = restarted.runtime[self.sid]
            runtime.awaiting = False
            runtime.last_send_at = 0
            restarted._handle_state(self.target, runtime, self.state, self.client, send_guard_tree=self.client.tree())
        self.assertEqual(send.call_count, 1)

    def test_process_replaced_after_final_turn_gate_does_not_keep_short_prompt(self):
        original = self.daemon._codex_turn_ready
        def replaced(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("reserved") and self.checks._ledger(self.target):
                self.daemon.codex_queue_recovery.current_turn = lambda target: {**self.read_turn(), "pid": 456}
            return result
        with patch.object(self.daemon, "_codex_turn_ready", side_effect=replaced):
            self.send()
        self.assertEqual(self.client.sent, [])

    def assert_late_source_check_change_blocks_input(self, change):
        phase = {"ready": False, "view": False, "injected": False}
        original_ready = self.daemon._private_check_ready
        original_view = self.client.replay
        original_ledger = self.checks._ledger
        def ready(*args, **kwargs):
            phase["ready"] = True
            try:
                return original_ready(*args, **kwargs)
            finally:
                phase["ready"] = False
        def view(*args, **kwargs):
            value = original_view(*args, **kwargs)
            if phase["ready"]:
                phase["view"] = True
            return value
        def ledger(*args, **kwargs):
            value = original_ledger(*args, **kwargs)
            if phase["ready"] and not phase["injected"]:
                change()
                phase["injected"] = True
            return value
        with patch.object(self.daemon, "_private_check_ready", side_effect=ready), \
                patch.object(self.client, "replay", side_effect=view), \
                patch.object(self.checks, "_ledger", side_effect=ledger):
            self.send()
        self.assertTrue(phase["injected"])
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.checks._ledger(self.target)[self.runtime.send_attempt_id]["phase"], "not_sent")

    def test_user_response_during_final_history_check_prevents_short_input(self):
        self.assert_late_source_check_change_blocks_input(lambda: self.append([
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "My own task"}]}}]))

    def test_goal_change_during_final_history_check_prevents_short_input(self):
        self.assert_late_source_check_change_blocks_input(lambda: self.append([
            {"type": "event_msg", "payload": {"type": "goal_updated", "goal_id": "user-goal"}}]))

    def test_operator_draft_during_final_source_check_prevents_short_input(self):
        frame = grid_payload([], error=HIGH_DEMAND_TEXT, composer="busy")
        frame["render_grid"]["surface_id"] = self.sid
        self.assert_late_source_check_change_blocks_input(lambda: setattr(self.client, "payload", frame))

    def test_surface_moved_during_final_source_check_prevents_short_input(self):
        self.assert_late_source_check_change_blocks_input(
            lambda: self.client.tree_data["windows"][0]["workspaces"][0].update(id=str(uuid.uuid4())))

    def assert_source_change_during_last_view_blocks_input(self, change):
        phase = {"ready": False, "views": 0}
        original_ready = self.daemon._private_check_ready
        original_view = self.client.replay
        def ready(*args, **kwargs):
            phase["ready"] = True
            try:
                return original_ready(*args, **kwargs)
            finally:
                phase["ready"] = False
        def view(*args, **kwargs):
            value = original_view(*args, **kwargs)
            if phase["ready"]:
                phase["views"] += 1
                if phase["views"] == 1:
                    change()
            return value
        with patch.object(self.daemon, "_private_check_ready", side_effect=ready), \
                patch.object(self.client, "replay", side_effect=view):
            self.send()
        self.assertEqual(phase["views"], 1)
        self.assertEqual(self.client.sent, [])

    def test_source_change_during_last_composer_read_prevents_short_input(self):
        origin = check._directory(self.config, self.sid) / "origin.json"
        self.assert_source_change_during_last_view_blocks_input(lambda: origin.write_text("{}"))

    def test_n_descriptor_added_during_last_composer_read_prevents_short_input(self):
        self.assert_source_change_during_last_view_blocks_input(
            lambda: (self.job_path.parent / "access.json").write_text("{}"))

    def queue_frame(self, message, *, draft=False):
        lines = [] if draft else ["Messages to be submitted", "↳ " + message, "⌥↑ edit last queued message"]
        frame = grid_payload(lines)
        grid = frame["render_grid"]
        grid["surface_id"] = self.sid
        if draft:
            row = grid["cursor"]["row"]
            grid["row_spans"] = [s for s in grid["row_spans"] if s["row"] != row or s["column"] < 2]
            grid["row_spans"].append(span(row, 2, message))
            grid["cursor"]["column"] = 2 + len(message)
        return frame

    def recover_queue(self, *, message=batch.PROMPT, change_on_phase=None, synchronous=False):
        self.send()
        self.assertEqual(self.client.sent[-1][-1], batch.PROMPT)
        self.client.payload = self.queue_frame(message)
        def edit(wid, sid):
            self.client.payload = self.queue_frame(message, draft=True)
        self.client.edit_codex_queued_prompt = Mock(side_effect=edit)
        recovery = self.daemon.codex_queue_recovery
        original = recovery.write_attempt
        def record(key, value):
            original(key, value)
            if change_on_phase:
                change_on_phase(value["phase"])
        with patch.object(recovery, "write_attempt", side_effect=record), patch("ccc_codex_queue.time.sleep"):
            if synchronous:
                with patch.object(self.daemon, "_observe_target_viewport", return_value=core.ScreenState(
                        "queued_followup", message_kind="codex")), patch.object(
                        self.daemon, "_claude_process_observation", return_value=None):
                    # poll_once supplies no scheduler/is_current callback.
                    return self.daemon._process_one_target(
                        self.target, self.client, self.client.tree(), "")
            return self.daemon._recover_stranded_codex_queue(
                self.target, self.runtime, core.ScreenState("queued_followup", message_kind="codex"), self.client,
                lambda **kwargs: self.daemon._active_send_target(self.target) is not None)

    def test_real_watcher_queue_path_keeps_the_original_short_message(self):
        state = self.recover_queue()
        self.assertEqual(state.kind, "queue_recovery_submitted")
        self.client.edit_codex_queued_prompt.assert_called_once()
        self.assertEqual(self.client.sent_keys, [(self.wid, self.sid, "enter")])

    def test_old_generic_queue_is_not_rewritten_even_with_current_short_proof(self):
        self.recover_queue(message=core.MESSAGE)
        self.client.edit_codex_queued_prompt.assert_not_called()
        self.assertEqual(self.client.sent_keys, [])

    def test_queue_pause_after_edit_intent_prevents_the_key(self):
        def pause(phase):
            if phase == "editing":
                self.daemon.config_store.mutate(lambda c: c["targets"][0].update(paused=True))
        state = self.recover_queue(change_on_phase=pause)
        self.assertEqual(state.kind, "queue_recovery_unconfirmed")
        self.client.edit_codex_queued_prompt.assert_not_called()
        self.assertEqual(self.client.sent_keys, [])

    def assert_sync_queue_change_blocks(self, phase, change):
        def change_after_intent(written_phase):
            if written_phase == phase:
                self.daemon.config_store.mutate(change)
        self.recover_queue(change_on_phase=change_after_intent, synchronous=True)
        self.assertEqual(self.client.sent_keys, [])
        self.assertEqual(self.client.edit_codex_queued_prompt.call_count,
                         0 if phase == "editing" else 1)
        self.assertEqual([r["phase"] for r in self.daemon.codex_queue_recovery.attempts.values()], [phase])

    def test_sync_queue_pause_after_edit_intent_prevents_edit_and_enter(self):
        self.assert_sync_queue_change_blocks("editing", lambda c: c["targets"][0].update(paused=True))

    def test_sync_queue_pause_after_submit_intent_prevents_enter(self):
        self.assert_sync_queue_change_blocks("submitting", lambda c: c["targets"][0].update(paused=True))

    def test_sync_queue_global_pause_after_submit_intent_prevents_enter(self):
        self.assert_sync_queue_change_blocks("submitting", lambda c: c.update(global_paused=True))

    def test_sync_queue_disarm_after_submit_intent_prevents_enter(self):
        self.assert_sync_queue_change_blocks("submitting", lambda c: c.update(mode="observe"))

    def test_sync_queue_removed_target_after_submit_intent_prevents_enter(self):
        self.assert_sync_queue_change_blocks("submitting", lambda c: c.update(targets=[]))

    def test_sync_queue_keeps_authorized_original_short_message(self):
        self.recover_queue(synchronous=True)
        self.client.edit_codex_queued_prompt.assert_called_once()
        self.assertEqual(self.client.sent_keys, [(self.wid, self.sid, "enter")])
        self.assertEqual([r["phase"] for r in self.daemon.codex_queue_recovery.attempts.values()], ["submitted"])

    def assert_sync_queue_late_draft_blocks(self, after_phase):
        phase = {"active": False, "view": False, "injected": False}
        original_view = self.client.replay
        original_ledger = self.checks._ledger
        def persisted(name):
            if name == after_phase:
                phase.update(active=True, view=False)
        def view(*args, **kwargs):
            value = original_view(*args, **kwargs)
            if phase["active"]:
                phase["view"] = True
            return value
        def ledger(*args, **kwargs):
            value = original_ledger(*args, **kwargs)
            if phase["active"] and phase["view"] and not phase["injected"]:
                self.client.payload = self.queue_frame("Operator draft", draft=True)
                phase["injected"] = True
            return value
        with patch.object(self.client, "replay", side_effect=view), \
                patch.object(self.checks, "_ledger", side_effect=ledger):
            self.recover_queue(change_on_phase=persisted, synchronous=True)
        self.assertTrue(phase["injected"])
        self.assertEqual(self.client.sent_keys, [])
        self.assertEqual(self.client.edit_codex_queued_prompt.call_count, 0 if after_phase == "editing" else 1)

    def test_sync_queue_draft_during_final_source_read_prevents_edit(self):
        self.assert_sync_queue_late_draft_blocks("editing")

    def test_sync_queue_draft_during_final_source_read_prevents_enter(self):
        self.assert_sync_queue_late_draft_blocks("submitting")

    def test_sync_queue_source_changed_during_last_draft_read_prevents_enter(self):
        phase = {"active": False, "views": 0}
        original = self.client.replay
        def persisted(name):
            if name == "submitting":
                phase["active"] = True
        def view(*args, **kwargs):
            result = original(*args, **kwargs)
            if phase["active"]:
                phase["views"] += 1
                if phase["views"] == 2:
                    (self.job_path.parent / "access.json").write_text("{}")
            return result
        with patch.object(self.client, "replay", side_effect=view):
            self.recover_queue(change_on_phase=persisted, synchronous=True)
        self.assertEqual(phase["views"], 2)
        self.assertEqual(self.client.sent_keys, [])
        self.client.edit_codex_queued_prompt.assert_called_once()

    def test_queue_user_draft_after_enter_intent_is_preserved(self):
        def type_draft(phase):
            if phase == "submitting":
                self.client.payload = self.queue_frame("User draft", draft=True)
        state = self.recover_queue(change_on_phase=type_draft)
        self.assertEqual(state.kind, "queue_recovery_unconfirmed")
        self.client.edit_codex_queued_prompt.assert_called_once()
        self.assertEqual(self.client.sent_keys, [])


if __name__ == "__main__":
    unittest.main()

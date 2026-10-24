"""Finite offline regressions for the two native-input P2 review findings.

Keep this file private or move it into scripts. With the current repository's
scripts directory on PYTHONPATH, run this file alone with python3 -B -S.
NativeFixture and FakeRPC replace external I/O; journals, request validation,
native dispatch and reask persistence remain real and use owned temporary files.
No expectedFailure markers: an unfixed runtime must fail the positive regressions.
"""
import copy
import itertools
import json
from pathlib import Path
import socket
import subprocess
import unittest
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_codex_input as direct
import cmux_native_delivery as native
import executor_ready as ready
import executor_reask as reask
from native_test_support import NativeFixture
from test_codex_input import FakeRPC


WORKSPACE = "00000000-0000-4000-8000-000000000001"
CALLER = "00000000-0000-4000-8000-000000000002"
TARGET = "00000000-0000-4000-8000-000000000003"
PANE = "00000000-0000-4000-8000-000000000004"
FENCE_ERROR = "offline native fence capture failed"
CONNECT_ERROR = "offline connection unavailable before native intent"
POST_INTENT_ERROR = "offline binding lost after durable native intent"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.fixture = NativeFixture.attach(self, identity=dict(
            workspace_uuid=WORKSPACE, caller_surface_uuid=CALLER,
            target_surface_uuid=TARGET, target_pane_uuid=PANE))
        self.fixture.pin.side_effect = lambda *args, **kwargs: copy.deepcopy(self.fixture.identity)
        self.rpc = FakeRPC(self.fixture)
        self.fixture.rpc.side_effect = lambda **kwargs: self.rpc
        self.state_root = ready.state_root()
        self.fixture.stack.enter_context(patch.object(
            direct.time, "monotonic", side_effect=itertools.count().__next__))
        self.guards = {}
        for name in ("read_screen", "read_screen_successor", "_run_successor",
                     "require_agent_input", "compose_block_is_empty",
                     "receiver_cannot_submit_now", "pending_queue_holds",
                     "_exact_pending_text", "_draft_structure",
                     "_codex_tab_queue_allowed", "_queued_or_active_input",
                     "_submit_terminal_once"):
            self.guards[name] = self.fixture.stack.enter_context(patch.object(
                bridge, name, side_effect=AssertionError("forbidden terminal path: " + name)))
        for name, mock in (("send_text", self.fixture.send), ("send_key", self.fixture.key)):
            mock.side_effect = AssertionError("forbidden terminal path: " + name)
            self.guards[name] = mock
        self.guards["socket"] = self.fixture.stack.enter_context(patch.object(
            socket, "socket", side_effect=AssertionError("offline tests forbid real sockets")))
        self.guards["Popen"] = self.fixture.stack.enter_context(patch.object(
            subprocess, "Popen", side_effect=AssertionError("offline tests forbid external processes")))
        # Assert even if production caught an AssertionError from a forbidden call.
        self.addCleanup(self.assert_no_external_input)

    def assert_no_external_input(self):
        for name, mock in self.guards.items():
            with self.subTest(forbidden=name):
                mock.assert_not_called()
        for call in self.fixture.run.call_args_list:
            self.assertEqual(call.args, ("tree", "--all", "--json", "--id-format", "both"))


class FenceCaptureFailureTests(OfflineCase):
    def assert_fence_failure(self, submit, journal_root):
        # Fail the actual fence read, after native preparation but before the
        # observer can construct/persist an input event or invoke a mutation.
        with patch.object(native, "_capture_fence",
                          side_effect=OSError(FENCE_ERROR)) as capture:
            with self.assertRaisesRegex(OSError, FENCE_ERROR):
                submit()
        capture.assert_called_once()
        self.assertEqual(capture.call_args.args[1], "native-input-fence-v1")
        self.assertEqual(self.rpc.mutations, [])
        paths = list(journal_root.rglob("attempt-*.json"))
        self.assertEqual(len(paths), 1)
        attempt = read_json(paths[0])
        self.assertEqual(attempt["events"], [])
        self.assertIn(FENCE_ERROR, attempt["error"])
        self.assertEqual(attempt["phase"], "NO_INPUT")

    def test_task_fence_capture_failure_persists_no_input_with_empty_events(self):
        pack = dict(task_id="native-fence-task", executor_uuid=TARGET)
        pack_path = self.fixture.home / "task-pack.json"
        pack_path.write_text(json.dumps(pack), encoding="utf-8")
        with patch.object(bridge, "validate_task_pack_contract", return_value=pack), \
                patch("availability_contract.require_action"):
            self.assert_fence_failure(
                lambda: bridge.submit_task_pack(
                    TARGET, "STATUS: native-fence-task run original task", str(pack_path)),
                self.state_root / "task-dispatch-v1")

    def test_callback_fence_capture_failure_persists_no_input_with_empty_events(self):
        report = self.fixture.home / "REPORT.md"
        report.write_text("offline completed report", encoding="utf-8")
        receipt = self.fixture.home / "completion-callback-receipt.json"
        pack = dict(task_id="native-fence-callback", completion_nonce="nonce12345",
                    report=str(report), callback_target=TARGET,
                    completion_receipt=str(receipt), executor_uuid=CALLER)
        pack["completion_callback"] = (
            f"DONE|{pack['task_id']}|{pack['completion_nonce']}|REPORT={report}")
        pack_path = self.fixture.home / "callback-task-pack.json"
        pack_path.write_text(json.dumps(pack), encoding="utf-8")
        with patch.object(bridge, "validate_task_pack_contract", return_value=pack), \
                patch("availability_contract.require_action"):
            self.assert_fence_failure(
                lambda: bridge.submit_completion_callback(str(pack_path)),
                receipt.with_name(receipt.stem + "-attempts"))


class ReaskNoInputEvidenceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        reask.start(CALLER, TARGET, task_id="offline-p2-task", now=1000.0,
                    workspace=WORKSPACE,
                    channels=[str(self.fixture.home / "offline-channel")])

    def tick(self, now=1001.0):
        return reask.tick(CALLER, now)

    def attempt_paths(self, ask):
        journal = self.state_root / "message-dispatch-v1" / ready.request_key(CALLER, ask["marker"])
        return sorted(journal.glob("attempt-*.json"))

    def one_attempt(self, ask):
        paths = self.attempt_paths(ask)
        self.assertEqual(len(paths), 1)
        return paths[0]

    def original_snapshot(self, ask):
        attempt = self.one_attempt(ask)
        request = ready.requests_dir() / (ready.request_key(CALLER, ask["marker"]) + ".json")
        return {path: path.read_bytes() for path in (attempt, request)}

    def assert_original_unchanged(self, ask, snapshot):
        for path, original in snapshot.items():
            self.assertEqual(path.read_bytes(), original, str(path))
        self.assertEqual(len(self.attempt_paths(ask)), 1, "old attempt must never be retransmitted")

    def first_no_input(self):
        self.fixture.rpc.side_effect = TimeoutError(CONNECT_ERROR)
        state = self.tick()
        self.fixture.rpc.side_effect = lambda **kwargs: self.rpc
        ask = copy.deepcopy(state["asks"][-1])
        attempt = self.one_attempt(ask)
        record = read_json(attempt)
        self.assertEqual(record["phase"], "NO_INPUT")
        self.assertEqual(record["events"], [])
        self.assertIn(CONNECT_ERROR, record["error"])
        self.assertEqual(self.rpc.mutations, [])
        self.assertFalse(ask.get("native_received"))
        self.assertFalse(ask.get("native_input_intent"))
        self.assertFalse(ask.get("terminal_attempted"))
        self.assertEqual(state.get("native_receipts", 0), 0)
        return state, ask, attempt

    def restore_unresolved_hold(self, state, ask):
        # Model this controller's persisted provisional state, without a
        # cached NO_INPUT verdict. Keep the authenticated original ownership;
        # a legacy hold without it must remain unresolved (tested separately).
        state["terminal_pending"] = dict(
            marker=ask["marker"], terminal=True, outcome="INTENT_UNRESOLVED",
            native_input_intent=False, paste_intent=False, terminal_attempted=False,
            origin=copy.deepcopy(ask["origin"]))
        reask.save(state)

    def assert_fresh_only(self, old, snapshot, state):
        new = state["asks"][-1]
        self.assertNotEqual(new["marker"], old["marker"])
        self.assertEqual(new["outcome"], "CONFIRMED", new.get("detail"))
        self.assertIsNone(state.get("terminal_pending"))
        self.assertEqual(state.get("native_receipts", 0), 1, "NO_INPUT is not a receipt")
        self.assertEqual(state.get("terminal_sends", 0), 0)
        self.assertEqual(state.get("terminal_attempts", 0), 0)
        wire = ready._original_request(CALLER, TARGET, new["marker"])["text"]
        old_wire = ready._original_request(CALLER, TARGET, old["marker"])["text"]
        self.assertNotEqual(wire, old_wire)
        self.assertEqual(self.rpc.mutations, [
            ("turn/steer", dict(
                threadId=self.fixture.session_id, expectedTurnId=self.rpc.turn,
                input=[dict(type="text", text=wire)]))
        ])
        self.assert_original_unchanged(old, snapshot)

    def assert_next_tick_stays_pending(self, old):
        before_calls = copy.deepcopy(self.rpc.calls)
        self.fixture.rpc.reset_mock()
        self.fixture.rpc.side_effect = AssertionError("pending original permits read-only reconciliation only")
        self.addCleanup(self.fixture.rpc.assert_not_called)
        state = self.tick(1001.0 + reask.INTERVAL)
        self.fixture.rpc.assert_not_called()
        self.assertEqual(self.rpc.calls, before_calls)
        self.assertEqual(state["state"], reask.WAITING)
        self.assertEqual((state.get("terminal_pending") or {}).get("marker"), old["marker"])
        self.assertNotEqual(state["asks"][-1]["outcome"], "CONFIRMED")
        self.assertFalse(state["asks"][-1].get("native_received"))
        self.assertEqual(state.get("native_receipts", 0), 0)
        self.assertEqual(state.get("terminal_sends", 0), 0)
        self.assertEqual(state.get("terminal_attempts", 0), 0)
        return state

    def test_current_no_input_releases_hold_and_next_round_uses_fresh_marker(self):
        state, old, _attempt = self.first_no_input()
        snapshot = self.original_snapshot(old)
        self.assertIsNone(state.get("terminal_pending"), "durable NO_INPUT must not pin the next round")
        self.assertIsNone(reask.load(CALLER).get("terminal_pending"), "release must survive reload")
        self.assert_fresh_only(old, snapshot, self.tick(1001.0 + reask.INTERVAL))

    def test_restored_hold_releases_only_from_original_no_input_evidence(self):
        state, old, _attempt = self.first_no_input()
        snapshot = self.original_snapshot(old)
        self.restore_unresolved_hold(state, old)
        self.assert_fresh_only(old, snapshot, self.tick(1001.0 + reask.INTERVAL))

    def test_legacy_hold_without_controller_cannot_borrow_zero_input_proof(self):
        state, old, _attempt = self.first_no_input()
        snapshot = self.original_snapshot(old)
        self.restore_unresolved_hold(state, old)
        restored = reask.load(CALLER)
        restored["terminal_pending"].pop("origin")
        reask.save(restored)
        state = self.assert_next_tick_stays_pending(old)
        self.assertEqual(state["asks"][-1]["outcome"],
                         "SKIPPED_ORIGINAL_CONTROLLER_REQUIRED")
        self.assert_original_unchanged(old, snapshot)

    def test_other_controller_cannot_reconcile_or_replay_an_original_hold(self):
        state, old, _attempt = self.first_no_input()
        snapshot = self.original_snapshot(old)
        self.restore_unresolved_hold(state, old)
        state_path = reask.state_path(CALLER)
        state_bytes = state_path.read_bytes()
        before_calls = copy.deepcopy(self.rpc.calls)
        other = copy.deepcopy(reask.controller())
        other["source"] += "-successor"
        with patch.object(reask, "controller", return_value=other):
            self.fixture.rpc.reset_mock()
            result = self.tick(1001.0 + reask.INTERVAL)
        self.assertEqual(result["outcome"], "ORIGINAL_CONTROLLER_REQUIRED")
        self.assertEqual(result["input_operations"], 0)
        self.fixture.rpc.assert_not_called()
        self.assertEqual(self.rpc.calls, before_calls)
        # A foreign controller must stop before creating even a new inquiry;
        # the original waiting state, request and unresolved hold stay intact.
        self.assertEqual(state_path.read_bytes(), state_bytes)
        self.assertEqual(reask.load(CALLER)["terminal_pending"]["marker"], old["marker"])
        self.assert_original_unchanged(old, snapshot)

    def test_missing_original_attempt_stays_pending_despite_false_input_flags(self):
        state, old, attempt = self.first_no_input()
        self.restore_unresolved_hold(state, old)
        original_request = ready._original_request(CALLER, TARGET, old["marker"])
        attempt.unlink()  # Only this test's owned temporary evidence.
        self.assert_next_tick_stays_pending(old)
        self.assertEqual(self.attempt_paths(old), [])
        self.assertEqual(ready._original_request(CALLER, TARGET, old["marker"]), original_request)

    def test_unreadable_original_attempt_stays_pending_despite_false_input_flags(self):
        state, old, attempt = self.first_no_input()
        self.restore_unresolved_hold(state, old)
        # A truncated journal is unreadable through every JSON reader, without
        # depending on OS permission semantics or mocking the release decision.
        attempt.write_bytes(b'{"phase":"NO_INPUT","events":[')
        self.assertIsNone(ready.read_json(attempt))
        snapshot = self.original_snapshot(old)
        self.assert_next_tick_stays_pending(old)
        self.assert_original_unchanged(old, snapshot)

    def test_post_intent_failure_stays_pending_even_with_zero_rpc_mutations(self):
        original_require_bound = native.require_bound
        journal_root = self.state_root / "message-dispatch-v1"

        def lose_binding_after_intent(*args, **kwargs):
            for path in journal_root.glob("*/attempt-*.json"):
                if any(event.get("phase") == direct.INPUT_PHASE
                       for event in read_json(path).get("events", [])):
                    raise native.NativeDeliveryError(POST_INTENT_ERROR)
            return original_require_bound(*args, **kwargs)

        # Exercise the real durable event, then fail the check immediately
        # after emit and before client.request(turn/steer). This cannot safely
        # be reclassified as NO_INPUT even though this fake saw no mutation.
        with patch.object(native, "require_bound", side_effect=lose_binding_after_intent):
            state = self.tick()
        old = state["asks"][-1]
        attempt = read_json(self.one_attempt(old))
        self.assertEqual(attempt["phase"], direct.INPUT_PHASE)
        self.assertEqual([event["phase"] for event in attempt["events"]], [direct.INPUT_PHASE])
        self.assertIn(POST_INTENT_ERROR, old.get("detail", ""))
        self.assertTrue(old.get("native_input_intent"))
        self.assertEqual(self.rpc.mutations, [])
        snapshot = self.original_snapshot(old)
        self.assert_next_tick_stays_pending(old)
        self.assert_original_unchanged(old, snapshot)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Offline Codex routes for successor delivery and executor re-asking.

Run this file alone with repository scripts on PYTHONPATH, or move it into
scripts and run ``python3 -B -S -m unittest -v test_codex_auxiliary_input``.
Only fake external observations and temporary journals/transcripts are used.
The real binding, dispatch, intent, receipt and reconciliation code executes.
"""
import copy
from datetime import datetime, timezone
import hashlib
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
import successor_native_delivery as successor
import successor_rebind as rebind
from native_test_support import NativeFixture
from test_codex_input import FakeRPC


WORKSPACE = "00000000-0000-4000-8000-000000000001"
CALLER = "00000000-0000-4000-8000-000000000002"
TARGET = "00000000-0000-4000-8000-000000000003"
TARGET_PANE = "00000000-0000-4000-8000-000000000004"
CALLER_WORKSPACE = "00000000-0000-4000-8000-000000000005"
CALLER_PANE = "00000000-0000-4000-8000-000000000006"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class OfflineRouteCase(unittest.TestCase):
    def setUp(self):
        self.fixture = NativeFixture.attach(self, identity=dict(
            workspace_uuid=WORKSPACE, caller_surface_uuid=CALLER,
            target_surface_uuid=TARGET, target_pane_uuid=TARGET_PANE))
        # executor_ready also pins the caller by keyword; keep the shared
        # positional fixture unchanged and adapt its external observation here.
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
            socket, "socket", side_effect=AssertionError("no real socket in offline tests")))
        self.guards["Popen"] = self.fixture.stack.enter_context(patch.object(
            subprocess, "Popen", side_effect=AssertionError("no external process in offline tests")))
        # Catch even forbidden calls swallowed by a production error handler.
        self.addCleanup(self.assert_no_external_input)

    def assert_no_external_input(self):
        for name, mock in self.guards.items():
            with self.subTest(forbidden=name):
                mock.assert_not_called()
        for call in self.fixture.run.call_args_list:
            self.assertTrue(call.args == ("tree", "--all", "--json", "--id-format", "both")
                            or call.args[:2] == ('rpc', 'surface.input_state'))

    def forbid_further_rpc(self):
        self.fixture.rpc.reset_mock()
        self.fixture.rpc.side_effect = AssertionError("reconciliation must not open RPC")

    def assert_exact_input(self, method, text, *, index=0):
        params = {"threadId": self.fixture.session_id,
                  "input": [{"type": "text", "text": text}]}
        if method == "turn/steer":
            params["expectedTurnId"] = self.rpc.turn
        self.assertEqual(self.rpc.mutations[index], (method, params))


class SuccessorCodexRouteTests(OfflineRouteCase):
    def setUp(self):
        super().setUp()
        pins = dict(successor_surface_uuid=CALLER, successor_workspace_uuid=CALLER_WORKSPACE,
                    successor_pane_uuid=CALLER_PANE, target_executor_uuid=TARGET,
                    target_workspace_uuid=WORKSPACE, target_pane_uuid=TARGET_PANE)
        message = "Offline fixture authorizes communication with this synthetic successor pair."
        authorization = dict(pins, authorization_source="user_message",
                             scope="maintenance_handshake", communication_only=True,
                             user_message=message,
                             user_message_sha256=hashlib.sha256(message.encode()).hexdigest())
        path = self.fixture.home / "offline-authorization.json"
        path.write_text(json.dumps(authorization), encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.artifact = dict(pins, schema=rebind.SCHEMA_V2, binding_mode=rebind.MODE_V2,
            authorization_source="user_message", scope="maintenance_handshake",
            communication_only=True, allow_task_replay=False, allow_shared_writes=False,
            authorization_record={"path": str(path), "sha256": digest},
            authorization_record_sha256=digest)
        pair = dict(mode=rebind.MODE_V2, caller_surface_uuid=CALLER,
                    caller_workspace_uuid=CALLER_WORKSPACE, caller_pane_uuid=CALLER_PANE,
                    target_surface_uuid=TARGET, target_workspace_uuid=WORKSPACE,
                    target_pane_uuid=TARGET_PANE)
        self.pair = pair
        self.fixture.stack.enter_context(patch.object(
            bridge, "pin_successor_pair", side_effect=lambda *a, **kw: copy.deepcopy(pair)))
        self.marker = "SUCCESSOR_CODEX_OFFLINE_AUX"
        self.text = "STATUS: " + self.marker + " exact  native payload 完整正文"

    def journal(self):
        return successor._paths(self.artifact, self.marker, self.state_root)[1]

    def events(self):
        return [read_json(path) for path in sorted(self.journal().glob("event-*.json"))]

    def originals(self):
        paths = [self.journal() / "attempt.json", *sorted(self.journal().glob("event-*.json"))]
        return {path.name: path.read_bytes() for path in paths}

    def submit(self, **kwargs):
        return successor.submit(bridge, self.artifact, self.text, marker=self.marker,
                                state_root=self.state_root, wait_seconds=0, **kwargs)

    def reconcile(self):
        return successor.reconcile(bridge, self.artifact, self.text,
                                   marker=self.marker, state_root=self.state_root)

    def assert_native_result(self, result, *, confirmed):
        self.assertIs(result["confirmed"], confirmed)
        self.assertEqual(result["transport"], direct.TRANSPORT)
        self.assertEqual(result["terminal_input_operations"], 0)
        self.assertEqual(result["input_operations"], 1)
        self.assertIsNone(result["submission_key"])
        self.assertEqual(result["retries"], 0)
        self.assertFalse(result["reconciled_read_only"])
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assertFalse({"PASTE_INTENT", "PASTED", "KEY_INTENT", "KEY_SENT"}
                         & {event["phase"] for event in self.events()})

    def test_active_send_persists_original_fence_before_one_steer(self):
        def before_input(method, params):
            attempt = read_json(self.journal() / "attempt.json")
            event, = self.events()
            self.assertEqual(event["phase"], "NATIVE_INPUT_INTENT")
            self.assertEqual(event["native_request"]["params"], params)
            self.assertEqual(event["native_input_fence"]["transcript"]["offset"],
                             self.fixture.transcript.stat().st_size)
            identity = attempt["native_binding"]["identity"]
            self.assertEqual(identity["caller_workspace_uuid"], CALLER_WORKSPACE)
            self.assertEqual(identity["target_workspace_uuid"], WORKSPACE)
            self.assertEqual(attempt["wire"], self.text)
        self.rpc.before_mutation = before_input
        self.assert_native_result(self.submit(), confirmed=True)
        self.assert_exact_input("turn/steer", self.text)

    def test_idle_send_starts_same_thread_without_setting_overrides(self):
        self.rpc.active = False
        self.assert_native_result(self.submit(), confirmed=True)
        self.assert_exact_input("turn/start", self.text)

    def native_only(self):
        fields = dict(transport=rebind.CODEX_SAME_PANE, caller_thread_id=CALLER,
                      target_thread_id=self.fixture.session_id)
        self.artifact.update(fields)
        path = Path(self.artifact['authorization_record']['path'])
        auth = read_json(path)
        auth.update(fields)
        path.write_text(json.dumps(auth))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.artifact.update(authorization_record={'path': str(path), 'sha256': digest},
                             authorization_record_sha256=digest)

    def test_native_only_authorization_keeps_one_steer_and_original_receipt(self):
        self.native_only()
        self.assert_native_result(self.submit(), confirmed=True)
        self.assert_exact_input('turn/steer', self.text)
        self.forbid_further_rpc()
        self.assertEqual(self.submit()['input_operations'], 0)

    def test_native_only_rejects_claude_and_different_thread_before_input(self):
        self.native_only()
        for provider, session in [('claude', self.fixture.session_id), ('codex', CALLER)]:
            with patch.object(successor.native, '_provider', return_value=provider), \
                 patch.object(successor.native, '_live_session', return_value=session):
                with self.assertRaisesRegex(Exception, 'CODEX_THREAD_REQUIRED'):
                    self.submit()
        self.assertEqual(self.rpc.mutations, [])
        self.assertFalse((self.journal() / 'attempt.json').exists())

    def test_legacy_tab_argument_cannot_select_terminal_input(self):
        self.assert_native_result(self.submit(key="tab"), confirmed=True)
        self.assert_exact_input("turn/steer", self.text)

    def test_pending_submit_and_late_receipt_reconcile_original_without_rpc(self):
        self.rpc.receive = False
        self.assert_native_result(self.submit(), confirmed=False)
        originals = self.originals()
        calls = copy.deepcopy(self.rpc.calls)
        self.forbid_further_rpc()
        pending = self.submit()
        self.assertFalse(pending["confirmed"])
        self.assertTrue(pending["reconciled_read_only"])
        self.assertEqual(pending["input_operations"], 0)
        self.fixture.append_user(self.text)
        received = self.reconcile()
        self.assertTrue(received["confirmed"])
        self.assertTrue(received["reconciled_read_only"])
        self.assertEqual(received["input_operations"], 0)
        self.assertEqual(self.originals(), originals)
        self.assertEqual(self.rpc.calls, calls)
        self.fixture.rpc.assert_not_called()

    def test_confirmed_repeat_uses_saved_receipt_without_rpc(self):
        first = self.submit()
        originals = self.originals()
        self.forbid_further_rpc()
        repeated = self.submit()
        self.assertTrue(repeated["confirmed"])
        self.assertEqual(repeated["receipt"], first["receipt"])
        self.assertEqual(repeated["input_operations"], 0)
        self.assertEqual(self.originals(), originals)
        self.fixture.rpc.assert_not_called()

    def test_successor_abort_and_late_receipt_use_original_events_and_one_input(self):
        self.rpc.receive = False
        self.assertFalse(self.submit()['confirmed'])
        originals = self.originals()
        self.forbid_further_rpc()
        self.fixture.append_record(dict(timestamp=datetime.now(timezone.utc).isoformat(),
            type='event_msg', payload=dict(type='turn_aborted', turn_id=self.rpc.turn,
                                          reason='interrupted')))
        pending = self.reconcile()
        self.assertFalse(pending['confirmed'])
        self.assertEqual(pending['native_turn_diagnostic']['status'], 'ORIGINAL_TURN_ABORTED')
        self.fixture.append_user(self.text)
        received = self.reconcile()
        self.assertTrue(received['confirmed'])
        self.assertEqual(received['input_operations'], 0)
        self.assertEqual(received['native_turn_diagnostic']['status'], 'ORIGINAL_TURN_ABORTED')
        self.assertFalse(received['native_turn_diagnostic']['replay_authorized'])
        self.assertEqual(self.originals(), originals)
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assertTrue((self.journal() / 'turn-scan.json').exists())
        self.fixture.rpc.assert_not_called()

    def test_timeout_without_receipt_never_retries_or_falls_back_to_tab(self):
        self.rpc.receive = False
        self.rpc.mutation_error = TimeoutError("offline ambiguous timeout")
        result = self.submit(key="tab")
        self.assert_native_result(result, confirmed=False)
        self.assertEqual(result["native_rpc_outcome"], "RPC_ERROR_OR_UNKNOWN")
        self.forbid_further_rpc()
        self.assertFalse(self.submit()["confirmed"])
        self.assertEqual(len(self.rpc.mutations), 1)
        self.fixture.rpc.assert_not_called()

    def test_timeout_after_actual_receipt_confirms_once(self):
        self.rpc.mutation_error = TimeoutError("offline response lost after receipt")
        result = self.submit()
        self.assert_native_result(result, confirmed=True)
        self.assertEqual(result["native_rpc_outcome"], "RPC_ERROR_OR_UNKNOWN")

    def test_rpc_failure_without_receipt_preserves_one_intent(self):
        self.rpc.receive = False
        self.rpc.mutation_error = RuntimeError("offline server rejected turn")
        result = self.submit()
        self.assert_native_result(result, confirmed=False)
        self.assertEqual(result["native_rpc_outcome"], "RPC_ERROR_OR_UNKNOWN")
        self.assertEqual([event["phase"] for event in self.events()],
                         ["NATIVE_INPUT_INTENT", "NATIVE_INPUT_RESULT"])

    def test_connection_failure_consumes_original_without_terminal_fallback(self):
        self.fixture.rpc.side_effect = TimeoutError("offline connect failure")
        with self.assertRaisesRegex(TimeoutError, "offline connect failure"):
            self.submit()
        self.assertEqual(self.rpc.mutations, [])
        self.assertEqual([event["phase"] for event in self.events()], ["ERROR"])
        self.forbid_further_rpc()
        result = self.submit()
        self.assertFalse(result["confirmed"])
        self.assertEqual(result["state"], "NO_INPUT")
        self.assertEqual(result["input_operations"], 0)
        self.fixture.rpc.assert_not_called()

    def test_nonaccepting_native_method_uses_terminal_once(self):
        self.rpc.thread_changes["canAcceptDirectInput"] = False
        terminal = self.guards.pop("_run_successor")
        def send(*args, **kwargs):
            if args[:2] == ('rpc', 'surface.input_state'):
                return json.dumps(dict(json.loads(args[2]), agent=True, terminal=True,
                                       state='empty', blocks_typing=False))
            if args[0] == 'paste':
                self.fixture.append_user('Wrapped notice: ' + self.text)
            return '{"submitted":true}'
        terminal.side_effect = send
        result = self.submit()
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['transport'], 'terminal-direct-marker-v1')
        self.assertEqual(result['input_operations'], 2)
        self.assertEqual(terminal.call_count, 2)  # State query plus one protected input.
        self.assertEqual(self.rpc.mutations, [])
        self.assertNotIn("NATIVE_INPUT_INTENT", [event["phase"] for event in self.events()])

    def test_reference_notice_is_sent_natively_and_does_not_claim_body_read(self):
        self.text += "\n" + "保留全部正文\t  " * 200
        result = self.submit()
        self.assert_native_result(result, confirmed=True)
        attempt = read_json(self.journal() / "attempt.json")
        self.assertNotEqual(attempt["wire"], self.text)
        self.assert_exact_input("turn/steer", attempt["wire"])
        self.assertEqual(result["confirmation_scope"], "reference_notice")
        self.assertFalse(result["body_read_confirmed"])
        self.assertFalse(result["business_accepted"])
        self.assertEqual(Path(result["body_reference"]["path"]).read_text(), self.text)

    def test_result_event_write_failure_still_observes_original_receipt(self):
        original_event = successor._event
        def emit(journal, phase, **fields):
            if phase == "NATIVE_INPUT_RESULT":
                raise OSError("offline result journal unavailable")
            return original_event(journal, phase, **fields)
        with patch.object(successor, "_event", side_effect=emit):
            result = self.submit()
        self.assert_native_result(result, confirmed=True)
        self.assertEqual(result["native_rpc_outcome"], "RPC_ACCEPTED")
        self.assertEqual(result["native_result_journal_error"]["error_type"], "OSError")
        self.assertEqual([event["phase"] for event in self.events()], ["NATIVE_INPUT_INTENT"])


class ExecutorReaskCodexRouteTests(OfflineRouteCase):
    def setUp(self):
        super().setUp()
        self.now = 1000.0
        self.fixture.stack.enter_context(patch.object(
            reask.time, "time", side_effect=lambda: self.now))
        append_user = self.fixture.append_user

        def receive(text, **kwargs):
            kwargs.setdefault("timestamp", datetime.fromtimestamp(self.now, timezone.utc).isoformat())
            return append_user(text, **kwargs)

        self.fixture.stack.enter_context(patch.object(self.fixture, "append_user", side_effect=receive))
        reask.start(CALLER, TARGET, task_id="offline-task", now=1000.0,
                    workspace=WORKSPACE, channels=[str(self.fixture.home / "offline-channel")])

    def tick(self, now=1001.0):
        self.now = now
        return reask.tick(CALLER, now)

    def original(self, ask):
        return ready._original_request(CALLER, TARGET, ask["marker"])

    def attempt_paths(self, ask):
        journal = self.state_root / "message-dispatch-v1" / ready.request_key(CALLER, ask["marker"])
        return sorted(journal.glob("attempt-*.json"))

    def events(self, ask):
        paths = self.attempt_paths(ask)
        self.assertEqual(len(paths), 1)
        return read_json(paths[0])["events"]

    def assert_native_ask(self, state, *, confirmed, intent=True, no_input=False):
        ask = state["asks"][-1]
        self.assertEqual(ask["transport"], direct.TRANSPORT)
        self.assertFalse(ask["terminal"])
        self.assertFalse(ask["terminal_attempted"])
        self.assertFalse(ask["paste_intent"])
        self.assertIs(ask["native_input_intent"], intent, ask.get("detail"))
        self.assertIs(ask["native_received"], confirmed, ask.get("detail"))
        self.assertFalse(confirmed and no_input)
        expected = "CONFIRMED" if confirmed else ("NO_INPUT" if no_input else "UNCONFIRMED_DO_NOT_RESEND")
        self.assertEqual(ask["outcome"], expected,
                         ask.get("detail"))
        if no_input:
            self.assertFalse(intent)
            self.assertTrue(ask["zero_input_verified"])
            self.assertEqual(ask["no_input_proof"]["input_operations"], 0)
            self.assertEqual(self.events(ask), [])
        self.assertEqual(state["terminal_sends"], 0)
        self.assertEqual(state["terminal_attempts"], 0)
        self.assertEqual(state["native_receipts"], int(confirmed))
        self.assertFalse({"PASTE_INTENT", "PASTED", "KEY_INTENT", "KEY_SENT"}
                         & {event["phase"] for event in self.events(ask)})
        return ask

    def test_active_tick_steers_once_with_durable_pending_and_intent(self):
        def before_input(method, params):
            state = reask.load(CALLER)
            pending = state["terminal_pending"]
            ask = state["asks"][-1]
            self.assertEqual(pending["marker"], ask["marker"])
            event = self.events(ask)[-1]
            self.assertEqual(event["phase"], "NATIVE_INPUT_INTENT")
            self.assertEqual(event["native_request"]["params"], params)
            self.assertEqual(event["native_input_fence"]["transcript"]["offset"],
                             self.fixture.transcript.stat().st_size)
        self.rpc.before_mutation = before_input
        state = self.tick()
        ask = self.assert_native_ask(state, confirmed=True)
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_exact_input("turn/steer", self.original(ask)["text"])
        self.assertIsNone(state["terminal_pending"])
        self.assertEqual(state["ask_count"], 1)
        self.assertEqual(state["file_posts"], 1)

    def test_idle_tick_starts_same_thread_without_setting_overrides(self):
        self.rpc.active = False
        ask = self.assert_native_ask(self.tick(), confirmed=True)
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_exact_input("turn/start", self.original(ask)["text"])

    def test_pending_next_tick_only_reconciles_original_and_posts_file(self):
        self.rpc.receive = False
        first = self.tick()
        ask = self.assert_native_ask(first, confirmed=False)
        saved = {path: path.read_bytes() for path in self.attempt_paths(ask)}
        self.forbid_further_rpc()
        state = self.tick(1001.0 + reask.INTERVAL)
        self.assertEqual(state["asks"][-1]["outcome"], "SKIPPED_UNVERIFIED")
        self.assertEqual(state["terminal_pending"]["marker"], ask["marker"])
        self.assertEqual(state["file_posts"], 2)
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assertEqual({path: path.read_bytes() for path in self.attempt_paths(ask)}, saved)
        self.fixture.rpc.assert_not_called()

    def test_pending_survives_eviction_from_short_history(self):
        self.rpc.receive = False
        state = self.tick()
        marker = state["terminal_pending"]["marker"]
        state["asks"] = []
        reask.save(state)
        self.forbid_further_rpc()
        state = self.tick(1001.0 + reask.INTERVAL)
        self.assertEqual(state["terminal_pending"]["marker"], marker)
        self.assertEqual(state["asks"][-1]["outcome"], "SKIPPED_UNVERIFIED")
        self.assertEqual(len(self.rpc.mutations), 1)
        self.fixture.rpc.assert_not_called()

    def test_unrelated_legacy_pending_preserved_without_blocking_new_task(self):
        for old_supervisor, old_task in ((TARGET, 'earlier-task'), ('other-supervisor', 'offline-task')):
            with self.subTest(supervisor=old_supervisor, task=old_task):
                marker = 'legacy-' + old_supervisor + old_task
                path, original = ready.write_record(CALLER, marker, 'STATUS: ' + marker,
                                                     old_supervisor, old_task, 'old-episode')
                original_bytes = path.read_bytes()
                pending = dict(marker=marker, outcome='UNCONFIRMED_DO_NOT_RESEND', terminal=True)
                state = reask.load(CALLER)
                state['terminal_pending'] = pending.copy()
                reask.save(state)
                mutations_before = len(self.rpc.mutations)
                state = self.tick(1100.0 + len(self.rpc.mutations) * reask.INTERVAL)
                self.assertTrue(state['asks'][-1]['native_received'])
                self.assertEqual(len(self.rpc.mutations), mutations_before + 1)
                self.assertIsNone(state['terminal_pending'])
                archive = read_json(Path(state['preserved_pending']['path']))
                self.assertEqual(archive['original_pending'], pending)
                self.assertEqual(archive['original_request'], original)
                self.assertEqual(path.read_bytes(), original_bytes)

    def test_same_task_legacy_pending_still_requires_original_controller(self):
        marker = 'legacy-same-task'
        ready.write_record(CALLER, marker, 'STATUS: ' + marker, TARGET, 'offline-task', 'old-episode')
        state = reask.load(CALLER)
        pending = dict(marker=marker, outcome='UNKNOWN', terminal=True)
        state['terminal_pending'] = pending.copy()
        reask.save(state)
        state = self.tick()
        self.assertEqual(state['asks'][-1]['outcome'], 'SKIPPED_ORIGINAL_CONTROLLER_REQUIRED')
        self.assertEqual(state['terminal_pending'], pending)
        self.assertEqual(self.rpc.mutations, [])

    def test_missing_original_request_cannot_detach_pending(self):
        state = reask.load(CALLER)
        pending = dict(marker='missing-original', outcome='UNKNOWN', terminal=True)
        state['terminal_pending'] = pending.copy()
        reask.save(state)
        state = self.tick()
        self.assertEqual(state['terminal_pending'], pending)
        self.assertEqual(self.rpc.mutations, [])

    def test_held_native_only_ask_reconciles_late_receipt_without_rpc(self):
        self.rpc.receive = False
        ask = self.assert_native_ask(self.tick(), confirmed=False)
        self.assertFalse(ask["terminal"])
        self.forbid_further_rpc()
        self.assertEqual(reask._held(bridge, TARGET, [ask], CALLER, provider="codex"),
                         ("UNVERIFIED", None))
        self.fixture.append_user(self.original(ask)["text"])
        self.assertEqual(reask._held(bridge, TARGET, [ask], CALLER, provider="codex"),
                         (None, None))
        self.assertEqual(ask["outcome"], "CONFIRMED")
        self.assertTrue(ask["receipt"])
        self.assertEqual(len(self.rpc.mutations), 1)
        self.fixture.rpc.assert_not_called()

    def test_late_pending_receipt_waits_sixty_seconds_before_fresh_marker(self):
        self.rpc.receive = False
        first = self.tick()
        old = first["asks"][-1]
        wire = self.original(old)["text"]
        self.now = 1041.0
        self.fixture.append_user(wire)
        self.rpc.receive = True
        state = self.tick(1001.0 + reask.INTERVAL)
        self.assertEqual(state["asks"][-1]["outcome"], "SKIPPED_NATIVE_RECEIPT_INTERVAL")
        self.assertEqual(state["terminal_next_due_epoch"], 1041.0 + reask.INTERVAL)
        self.assertEqual(len(self.rpc.mutations), 1)
        state = self.tick(1001.0 + 2 * reask.INTERVAL)
        new = state["asks"][-1]
        self.assertNotEqual(new["marker"], old["marker"])
        self.assertEqual(new["outcome"], "CONFIRMED")
        self.assertEqual(len(self.rpc.mutations), 2)
        self.assert_exact_input("turn/steer", wire, index=0)
        self.assert_exact_input("turn/steer", self.original(new)["text"], index=1)
        self.assertNotEqual(wire, self.original(new)["text"])
        self.assertIsNone(state["terminal_pending"])
        self.assertEqual(state["native_receipts"], 2)
        self.assertEqual(state["terminal_sends"], 0)

    def test_timeout_without_receipt_stays_pending_and_never_uses_tab(self):
        self.rpc.receive = False
        self.rpc.mutation_error = TimeoutError("offline ambiguous timeout")
        first = self.tick()
        ask = self.assert_native_ask(first, confirmed=False)
        self.assertEqual(first["terminal_pending"]["marker"], ask["marker"])
        self.forbid_further_rpc()
        state = self.tick(1001.0 + reask.INTERVAL)
        self.assertEqual(state["asks"][-1]["outcome"], "SKIPPED_UNVERIFIED")
        self.assertEqual(len(self.rpc.mutations), 1)
        self.fixture.rpc.assert_not_called()

    def test_timeout_after_receipt_confirms_without_another_send(self):
        self.rpc.mutation_error = TimeoutError("offline accepted input with lost response")
        ask = self.assert_native_ask(self.tick(), confirmed=True)
        self.assertEqual(len(self.rpc.mutations), 1)
        result, = [event for event in self.events(ask) if event["phase"] == "NATIVE_INPUT_RESULT"]
        self.assertEqual(result["native_result"]["rpc_outcome"], "RPC_ERROR_OR_UNKNOWN")

    def test_rpc_failure_stays_native_with_zero_terminal_attempts(self):
        self.rpc.receive = False
        self.rpc.mutation_error = RuntimeError("offline server rejected input")
        self.assert_native_ask(self.tick(), confirmed=False)
        self.assertEqual(len(self.rpc.mutations), 1)

    def test_connection_failure_proves_no_input_and_allows_only_later_fresh_request(self):
        self.fixture.rpc.side_effect = TimeoutError("offline connection unavailable")
        first = self.tick()
        ask = self.assert_native_ask(first, confirmed=False, intent=False, no_input=True)
        self.assertEqual(self.rpc.mutations, [])
        self.assertIsNone(first["terminal_pending"])
        saved = {path: path.read_bytes() for path in self.attempt_paths(ask)}
        connection_calls = self.fixture.rpc.call_count
        self.tick(1002.0)
        self.assertEqual(self.fixture.rpc.call_count, connection_calls)
        self.assertEqual(self.rpc.mutations, [])
        self.fixture.rpc.side_effect = lambda **kwargs: self.rpc
        state = self.tick(1001.0 + reask.INTERVAL)
        new = self.assert_native_ask(state, confirmed=True)
        self.assertNotEqual(new["marker"], ask["marker"])
        self.assertEqual(len(self.rpc.mutations), 1)
        self.assert_exact_input("turn/steer", self.original(new)["text"])
        self.assertEqual({path: path.read_bytes() for path in self.attempt_paths(ask)}, saved)

    def test_nonaccepting_native_method_falls_back_for_original_reask(self):
        self.rpc.thread_changes["canAcceptDirectInput"] = False
        send = self.guards.pop('send_text')
        key = self.guards.pop('send_key')
        payload = []
        send.side_effect = lambda surface, text: payload.append(text)
        key.side_effect = lambda surface, key: self.fixture.append_user('Wrapped: ' + payload[-1])
        state = self.tick()
        ask = state['asks'][-1]
        self.assertTrue(ask['native_received'], ask)
        self.assertEqual(ask['outcome'], 'CONFIRMED')
        self.assertEqual(self.rpc.mutations, [])
        self.assertIsNone(state['terminal_pending'])
        send.assert_called_once()
        key.assert_called_once()

    def test_long_reask_confirms_reference_notice_without_claiming_body_read(self):
        state = self.tick()
        ask = self.assert_native_ask(state, confirmed=True)
        original = self.original(ask)
        self.assertIn("body_reference", original)
        self.assertLessEqual(len(original["text"].encode()), ready.prompt_reference.MAX_INLINE_BYTES)
        self.assert_exact_input("turn/steer", original["text"])
        self.assertEqual(ask["confirmation_scope"], "reference_notice")
        self.assertFalse(ask["body_read_confirmed"])


if __name__ == "__main__":
    unittest.main()

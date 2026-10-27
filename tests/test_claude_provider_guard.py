"""Provider holds survive crashes and refuse every terminal input boundary."""
import contextlib
import copy
import dataclasses
import datetime as dt
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import cmux_codex_watch as w
from claude_ccc_protocol import classify_stop_failure
from tests import test_claude_delivery_recovery as recovery
from tests import test_claude_submit_not_sent as not_sent
from tests.test_watch import (claude_grid_payload, claude_hook_event, claude_idle_screen,
                              claude_wrapped_composer_payload, wrap_to_cells)


AUTH = "⏺ API Error: 403 API Key 所属分组已删除"
RPM = "⏺ API Error: Request rejected (429) · group requests-per-minute limit exceeded"


class ProviderGuardTests(unittest.TestCase):
    def setUp(self):
        # Keep tests independent of host notification settings and OS threads.
        for name in ("_notify", "_notify_async"):
            patcher = mock.patch.object(w.WatchDaemon, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def case(self, root, *, pending=False):
        factory = not_sent.SubmitNotSentTests() if pending else recovery.DeliveryRecoveryTests()
        d, t, r, e, c = factory.setup_case(root)
        r.claude_process_pid = 1234
        d._claude_send_process_identity = mock.Mock(return_value={
            "pid": 1234, "generation": "generation-a", "started_epoch": 1.0})
        return d, t, r, e, c

    def failure(self, e, text=AUTH, *, stamp=None, event_id="provider-error"):
        return {**e, **classify_stop_failure("unknown", text),
                "event_name": "StopFailure", "event_id": event_id, "agent_pid": 1234,
                "synthetic_fallback": False, "created_at": stamp or time.time() - 10}

    def test_foreign_history_is_skipped_but_current_pending_claim_blocks(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            claim = self.failure(e, RPM, stamp=time.time())
            d.claude_event_ledger.events = {
                str(i): dict(claim, surface_id='other-surface', status='handling')
                for i in range(1000)}
            with mock.patch.object(d.claude_event_ledger, 'provider_claim_pending',
                                   wraps=d.claude_event_ledger.provider_claim_pending) as pending:
                self.assertEqual(d._claude_provider_guard_reason(t, r, event=e), '')
                pending.assert_not_called()
                d.claude_event_ledger.events['current-claim'] = dict(
                    claim, session_id=r.claude_session_id, status='handling',
                    process_generation=r.claude_process_generation)
                self.assertEqual(d._claude_provider_guard_reason(t, r, event=e),
                                 'claude_provider_verifying')
                pending.assert_called_once()

    def hold(self, d, t, r, e, text=AUTH, *, stamp=None):
        event = self.failure(e, text, stamp=stamp)
        d._remember_claude_provider_failure(t, r, event, event=event)
        return event

    def restart(self, d, c):
        restarted = w.WatchDaemon(d.config_path, d.state_path, client=c)
        restarted._claude_send_process_identity = mock.Mock(return_value={
            "pid": 1234, "generation": "generation-a", "started_epoch": 1.0})
        return restarted, restarted.runtime["surface-uuid"]

    def native_prompt(self, root, e, *, text="开始一个新的人工任务", stamp=None, **changes):
        stamp = stamp or time.time() - 1
        event = {**e, "event_id": "new-human-prompt", "event_name": "UserPromptSubmit",
                 "created_at": stamp, "agent_pid": 1234, "synthetic_fallback": False,
                 "prompt_kind": "human", "message_hash": w.dispatch_recovery.digest(text)}
        row = {"type": "user", "sessionId": event["session_id"], "isSidechain": False,
               "uuid": "new-native-prompt", "timestamp": dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat(),
               "message": {"role": "user", "content": text}, "promptSource": "typed",
               "origin": {"kind": "human"}, **changes}
        path = Path(root).resolve() / "native.jsonl"
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n")
        return event, path

    def guard_transport(self, c, before=None):
        """Model the connected transport: recheck immediately before its write."""
        @contextlib.contextmanager
        def guard(check):
            raw_text, raw_key = c.send_text, c.send_key
            def checked(raw, kind, *args):
                if before:
                    before(kind)
                if not check():
                    raise w.InputNotSentError("connected guard rejected input")
                return raw(*args)
            with mock.patch.object(c, "send_text", side_effect=lambda *a: checked(raw_text, "text", *a)), \
                 mock.patch.object(c, "send_key", side_effect=lambda *a: checked(raw_key, "enter", *a)):
                yield
        c.input_guard = guard

    def assert_no_input(self, c, before=(0, 0)):
        self.assertEqual((len(c.sent_text), len(c.sent_keys)), before)

    def old_pending(self, root, *, unknown=True):
        d, t, r, e, c = self.case(root, pending=True)
        now = time.time()
        e["created_at"] = r.claude_submit_since = now - 20
        d.claude_event_ledger.mark(e, "reserved")
        r.claude_submit_write_unknown = unknown
        r.claude_submit_attempts = 1
        r.claude_submit_last_attempt_at = now - 15
        r.claude_last_submit_event_id = e["event_id"]
        r.claude_last_submit_message_hash = r.claude_submit_message_hash
        r.claude_last_submit_session_id = r.claude_session_id
        r.claude_last_submit_generation = r.claude_process_generation
        r.claude_last_submit_at = now - 15
        r.claude_orphan_enter_count, r.claude_orphan_enter_at = 2, now - 16
        r.claude_fallback_retry_count = 2
        r.send_count, r.last_send_at = 3, now - 30
        self.hold(d, t, r, e, stamp=now - 10)
        return d, t, r, e, c

    @staticmethod
    def idle(c):
        # A real new human task consumed/replaced the prior composer. Leaving
        # FakeClient's old echo here would correctly prevent a new paste.
        c._submit_echo = False
        c.payload = claude_grid_payload()
        c._base_payload = copy.deepcopy(c.payload)
        c.text = claude_idle_screen()

    def receipt_case(self, root, mode, *, lost_ack=True):
        d, t, r, e, c = self.case(root)
        now = time.time()
        if mode == "released":
            self.hold(d, t, r, e, stamp=now - 10)
            prompt, path = self.native_prompt(root, e, stamp=now - 1)
            with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                d._handle_claude_event(prompt, c)
        elif mode == "retry_wait":
            d._handle_claude_event(self.failure(e, RPM, stamp=now - 10), c)
            now = r.claude_provider_guard["retry_after"] + 1
        stop = dict(claude_hook_event("receipt-source", "Stop", created_at=now),
                    agent_pid=1234, process_generation="generation-a")
        raw_key = c.send_key
        def accepted_but_ack_lost(*args):
            raw_key(*args)
            raise w.UncertainDeliveryError("accepted key, ACK lost")
        with mock.patch.object(w.time, "time", return_value=now), \
             (mock.patch.object(c, "send_key", side_effect=accepted_but_ack_lost)
              if lost_ack else contextlib.nullcontext()):
            if mode == "retry_wait":
                d._maybe_send_deferred_claude_stop(t, r, w.ScreenState("claude_stopped", message_kind="claude"), c)
            else:
                d._handle_claude_event(stop, c)
        self.assertEqual((len(c.sent_text), len(c.sent_keys)), (1, 1))
        self.assertEqual(r.claude_submit_write_unknown, lost_ack)
        if mode in {"blocked", "active_retry_wait"}:
            with mock.patch.object(w.time, "time", return_value=now + 1):
                d._handle_claude_event(self.failure(
                    e, AUTH if mode == "blocked" else RPM, stamp=now + 1), c)
        prompt, path = self.native_prompt(root, e, text=w.CLAUDE_MESSAGE, stamp=now + 2)
        prompt.update(event_id="native-watchdog-receipt", prompt_kind="watchdog")
        return d, t, r, c, prompt, path, now

    def test_current_native_errors_survive_real_wraps_and_busy_composer(self):
        for columns in (40, 80):
            for text, kind in ((AUTH, "claude_auth"), (RPM, "claude_429"),
                               ('⏺ API Error: 403 {"error":{"message":"预扣费失败：余额不足"}}', "claude_billing")):
                for busy in (False, True):
                    with self.subTest(columns=columns, text=text, busy=busy):
                        lines = wrap_to_cells(text, columns)
                        payload = (claude_wrapped_composer_payload(columns=columns, lines=lines) if busy
                                   else claude_grid_payload(columns=columns, lines=lines))
                        state = w.classify_claude_grid(w.Grid.from_rpc(payload, "surface-uuid"))
                        self.assertEqual(state.claude_provider_failure["error_kind"], kind)
                        if busy:
                            self.assertTrue(state.watchdog_echo)

    def test_json_continuations_and_ansi_are_current_but_quoted_errors_are_not(self):
        lines = ["⏺ API Error: 403 {", '        "error": {',
                 '            "message": "预扣费失败：余额不足"', "        }", "}"]
        grid = w.Grid.from_rpc(claude_grid_payload(lines=lines), "surface-uuid")
        grid = dataclasses.replace(grid, lines=tuple(
            "\x1b[31m" + s + "\x1b[0m" if s.strip() else s for s in grid.lines))
        self.assertEqual(w._claude_current_error(grid, grid.cursor.row)["error_kind"], "claude_billing")
        for body in (["> " + AUTH], ["```text", AUTH, "```"], ["    " + AUTH],
                     [AUTH, "Previous problem has been resolved."], [AUTH, "⏺ All work is complete."],
                     [AUTH, "❯ 新任务", "新的正常结果"]):
            with self.subTest(body=body):
                state = w.classify_claude_grid(w.Grid.from_rpc(claude_grid_payload(lines=body), "surface-uuid"))
                self.assertIsNone(state.claude_provider_failure)

    def test_hold_survives_pruning_restart_rearm_and_stale_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            d.save()  # This snapshot predates the guard and must not override it.
            self.hold(d, t, r, e)
            with mock.patch.object(w, "CLAUDE_EVENT_LEDGER_LIMIT", 2):
                for i in range(6):
                    d.claude_event_ledger.mark(dict(e, event_id=f"old-{i}"), "completed")
            d2, r2 = self.restart(d, c)
            for runtime in (r2, w.TargetRuntime()):
                for state in (w.ScreenState("claude_stopped"), w.ScreenState("working")):
                    self.assertEqual(d2._claude_provider_guard_reason(t, runtime, state), "claude_provider_retry_wait")
                d2._remember_claude_provider_failure(t, runtime, classify_stop_failure("", RPM))
                self.assertEqual(runtime.claude_provider_guard["status"], "retry_wait")
            self.assertEqual(len(d2.claude_event_ledger.events), 2)
            # retry_wait 是有界续跑，不发人工通知；durable 持有才通知。
            self.assertEqual(self._notify_async.call_count, 0)
            self.assert_no_input(c)

    def test_claim_crash_is_replayed_and_blocks_until_hold_is_durable(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            failure = self.failure(e)
            with mock.patch.object(d.claude_event_ledger, "put_provider_guard", side_effect=OSError("disk unavailable")):
                d._handle_claude_event_safely(failure, c)
            self.assertEqual(d.claude_event_ledger.status_of(failure["event_id"]), "handling")
            self.assertNotIn(failure["event_id"], d.claude_event_ledger.known_ids())
            self.assertEqual(d._claude_provider_guard_reason(t, r), "claude_provider_verifying")
            d2, r2 = self.restart(d, c)
            d2._handle_claude_event(failure, c)
            self.assertEqual(r2.claude_provider_guard["status"], "retry_wait")
            self.assertIn(failure["event_id"], d2.claude_event_ledger.known_ids())
            self.assert_no_input(c)

    def test_guard_write_failure_never_exposes_release_in_memory(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            self.hold(d, t, r, e)
            before = copy.deepcopy(r.claude_provider_guard)
            observation = {"pid": 4321, "generation": "generation-b", "started_epoch": time.time() - 1}
            with mock.patch.object(w, "atomic_write_json", side_effect=OSError("fsync failed")):
                with self.assertRaises(OSError):
                    d._release_claude_provider_generation(r, observation)
            self.assertEqual(r.claude_provider_guard, before)
            self.assertEqual(d.claude_event_ledger.provider_guard(t["surface_id"]), before)
            self.assert_no_input(c)

    def test_429_backoff_is_bounded_and_screen_hook_does_not_double_count(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            now = time.time()
            failure = {**classify_stop_failure("", RPM), "screen_fingerprint": "first-screen"}
            with mock.patch.object(w.time, "time", return_value=now):
                guard = d._remember_claude_provider_failure(t, r, failure)
            self.assertEqual(guard["retry_after"], now + 60)
            with mock.patch.object(w.time, "time", return_value=now + 10):
                other = d._remember_claude_provider_failure(t, r, dict(failure, screen_fingerprint="repaint"))
                hook = self.failure(e, RPM, stamp=now + 5)
                guard = d._remember_claude_provider_failure(t, r, hook, event=hook)
            self.assertEqual(other["retry_after"], now + 60)
            self.assertEqual((guard["retry_count"], guard["retry_after"]), (1, now + 60))
            for count, delay in enumerate((120, 240, 480, 900, 900), 2):
                stamp = now + count * 1000
                hook = self.failure(e, RPM, stamp=stamp, event_id=f"429-{count}")
                with mock.patch.object(w.time, "time", return_value=stamp):
                    guard = d._remember_claude_provider_failure(t, r, hook, event=hook)
                    same = d._remember_claude_provider_failure(t, r, hook, event=hook)
                # 同一 event_id 不重备：计数与期限保持首次落盘值，
                # 第二次调用返回完全相同的 guard（见下一行的等值断言）。
                self.assertEqual(guard["retry_count"], 1)
                self.assertEqual(guard["retry_after"], now + 60)
                self.assertEqual(same, guard)
            self.assertEqual(self._notify_async.call_count, 0)

    def test_429_original_event_restores_and_sends_exactly_once_when_due(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            failure = self.failure(e, RPM)
            d._handle_claude_event(failure, c)
            d.save()
            deadline = r.claude_provider_guard["retry_after"]
            d2, r2 = self.restart(d, c)
            self.assertEqual(r2.claude_deferred_event["event_id"], failure["event_id"])
            self.assertEqual(r2.claude_deferred_event["created_at"], failure["created_at"])
            state = w.ScreenState("claude_stopped", message_kind="claude")
            with mock.patch.object(w.time, "time", return_value=deadline - 1):
                self.assertTrue(d2._maybe_send_deferred_claude_stop(t, r2, state, c))
            self.assert_no_input(c)
            with mock.patch.object(w.time, "time", return_value=deadline + 1):
                self.assertTrue(d2._maybe_send_deferred_claude_stop(t, r2, state, c))
                d2._maybe_send_deferred_claude_stop(t, r2, state, c)
            self.assertEqual((len(c.sent_text), len(c.sent_keys)), (1, 1))
            self.assertEqual(d2.claude_event_ledger.status_of(failure["event_id"]), "sent")

    def test_429_guard_ahead_of_state_restores_only_unconsumed_event(self):
        for status in ("handling", "provider_retry_wait", "reserved", "sent"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.case(root)
                d.save()
                event = self.failure(e, RPM)
                d.claude_event_ledger.mark(event, status)
                d._remember_claude_provider_failure(t, r, event, event=event)
                d2, r2 = self.restart(d, c)
                self.assertEqual(bool(r2.claude_deferred_event), status in {"handling", "provider_retry_wait"})
                self.assert_no_input(c)

    def test_pending_unknown_and_reserved_are_not_reopened_by_a_provider_error(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.case(root, pending=True)
                r.claude_submit_write_unknown = unknown
                r.claude_orphan_enter_count = 2
                r.claude_orphan_enter_at = 123.0
                before = {k: v for k, v in r.to_dict().items() if k.startswith(("claude_submit_", "claude_orphan_"))}
                d._handle_claude_event(self.failure(e), c)
                state = w.classify_claude_grid(w.Grid.from_rpc(c.replay(t["workspace_id"], t["surface_id"]), t["surface_id"]))
                d._reconcile_claude_submit(t, r, state, c)
                d._recover_orphan_watchdog_submit(t, r, state, c)
                self.assertEqual({k: getattr(r, k) for k in before}, before)
                self.assertEqual(d.claude_event_ledger.status_of(e["event_id"]), "reserved")
                self.assert_no_input(c, (1, 0))

    def test_all_automatic_entry_points_refuse_a_persistent_hold(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            self.hold(d, t, r, e)
            state = w.ScreenState("claude_stopped", message_kind="claude")
            self.assertFalse(d._send_claude_event(e, t, r, c)[0])
            self.assertFalse(d._send_claude_enter(t, r, c, reason="hold", input_check=lambda: True))
            self.assertTrue(d._maybe_send_deferred_claude_stop(t, r, state, c))
            self.assertFalse(d._refresh_claude_dispatch_recovery(t, r, state))
            self.assertFalse(d._recover_claude_dispatch_wait(t["surface_id"], r, state))
            self.assertTrue(d._maybe_send_claude_dispatch_gap(t, r, state, c))
            self.assertTrue(d._maybe_send_claude_hook_gap_fallback(t, r, state, None, c))
            self.assertEqual(d._apply_claude_runtime_guards(t["surface_id"], r, state).kind, "claude_provider_retry_wait")
            self.assert_no_input(c)

    def test_text_guard_catches_fresh_replay_and_last_identity_or_migration_io(self):
        for boundary in ("replay", "identity", "migration"):
            for text in (AUTH, RPM):
                with self.subTest(boundary=boundary, error=text), tempfile.TemporaryDirectory() as root:
                    d, t, r, e, c = self.case(root)
                    r.claude_orphan_enter_count, r.claude_orphan_enter_at = 2, 123.0
                    inside = {"transport": False, "replayed": False}
                    def before(kind):
                        inside.update(transport=True, replayed=False)
                    self.guard_transport(c, before)
                    raw_replay = c.replay
                    def replay(*args):
                        if inside["transport"]:
                            inside["replayed"] = True
                            if boundary == "replay":
                                c.payload = claude_grid_payload(lines=[text])
                                c._base_payload = copy.deepcopy(c.payload)
                        return raw_replay(*args)
                    def late(*args):
                        if inside["transport"] and inside["replayed"]:
                            self.hold(d, t, r, e, text)
                        return True
                    raw_identity = d._claude_send_process_identity
                    def identity(*args):
                        late()
                        return raw_identity(*args)
                    with mock.patch.object(c, "replay", side_effect=replay), \
                         (mock.patch.object(d, "_claude_send_process_identity", side_effect=identity) if boundary == "identity"
                          else mock.patch.object(d, "_claude_migration_sources_current", side_effect=late) if boundary == "migration"
                          else contextlib.nullcontext()):
                        sent, detail = d._send_claude_event(e, t, r, c)
                    self.assertFalse(sent, detail)
                    self.assertTrue(inside["transport"])
                    self.assert_no_input(c)
                    self.assertEqual((r.claude_orphan_enter_count, r.claude_orphan_enter_at), (2, 123.0))
                    self.assertEqual(r.claude_submit_phase, "none")
                    self.assertEqual(d.claude_event_ledger.events[e["event_id"]]["input_not_sent"]["phase"], "text")

    def test_enter_guard_preserves_original_attempt_at_every_last_boundary(self):
        for boundary in ("replay", "identity", "migration"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.case(root, pending=True)
                r.claude_submit_attempts, r.claude_orphan_enter_count = 1, 2
                r.claude_submit_last_attempt_at, r.claude_orphan_enter_at = 50.0, 60.0
                before_state = {k: v for k, v in r.to_dict().items()
                                if k.startswith(("claude_submit_", "claude_last_submit_", "claude_orphan_"))}
                inside = {"transport": False, "replayed": False}
                self.guard_transport(c, lambda kind: inside.update(transport=True, replayed=False))
                raw_replay = c.replay
                def replay(*args):
                    if inside["transport"]:
                        inside["replayed"] = True
                        if boundary == "replay":
                            c.payload = claude_grid_payload(lines=[AUTH])
                            c._base_payload = copy.deepcopy(c.payload)
                    return raw_replay(*args)
                def late(*args):
                    if inside["transport"] and inside["replayed"]:
                        self.hold(d, t, r, e)
                    return True
                raw_identity = d._claude_send_process_identity
                def identity(*args):
                    late()
                    return raw_identity(*args)
                with mock.patch.object(c, "replay", side_effect=replay), \
                     (mock.patch.object(d, "_claude_send_process_identity", side_effect=identity) if boundary == "identity"
                      else mock.patch.object(d, "_claude_migration_sources_current", side_effect=late) if boundary == "migration"
                      else contextlib.nullcontext()):
                    self.assertFalse(d._send_claude_enter(t, r, c, reason="late provider"))
                self.assertTrue(inside["transport"])
                self.assertEqual({k: getattr(r, k) for k in before_state}, before_state)
                self.assert_no_input(c, (1, 0))

    def test_zero_input_proof_failure_retains_reservation_and_unknown_write(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            self.guard_transport(c, lambda kind: self.hold(d, t, r, e))
            raw_write = d.claude_event_ledger._write
            def write(events=None):
                if events and events.get(e["event_id"], {}).get("input_not_sent"):
                    raise OSError("no space for zero-input evidence")
                return raw_write(events)
            with mock.patch.object(d.claude_event_ledger, "_write", side_effect=write), self.assertRaises(OSError):
                d._send_claude_event(e, t, r, c)
            self.assertTrue(r.claude_submit_write_unknown)
            self.assertEqual(d.claude_event_ledger.status_of(e["event_id"]), "reserved")
            d2, r2 = self.restart(d, c)
            self.assertTrue(r2.claude_submit_write_unknown)
            self.assertFalse(d2._send_claude_event(e, t, r2, c)[0])
            self.assert_no_input(c)

    def test_provider_event_cannot_adopt_wrong_identity_or_old_watermark(self):
        for changes in ({"agent_pid": 777}, {"agent_pid": 0}, {"workspace_id": "other"},
                        {"session_id": "nested"}, {"process_generation": "old"},
                        {"created_at": 1.0}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.case(root)
                r.claude_last_hook_at = time.time() - 100
                before = (r.claude_process_pid, r.claude_session_id, r.claude_last_hook_at)
                d._handle_claude_event({**self.failure(e), **changes}, c)
                self.assertIsNone(r.claude_provider_guard)
                self.assertEqual((r.claude_process_pid, r.claude_session_id, r.claude_last_hook_at), before)
                self.assert_no_input(c)

    def test_native_human_release_requires_current_record_and_preserves_unknown(self):
        for valid in (False, True):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.old_pending(root)
                prompt, path = self.native_prompt(root, e, isMeta=not valid)
                before = {k: v for k, v in r.to_dict().items() if k.startswith(("claude_submit_", "claude_orphan_"))}
                with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                    d._handle_claude_event(prompt, c)
                self.assertEqual(r.claude_provider_guard["status"], "released" if valid else "retry_wait")
                if valid:
                    archived = d.claude_event_ledger.events[e["event_id"]]["provider_superseded_submission"]
                    self.assertEqual({k: archived["pending_submit"][k] for k in before}, before)
                    self.assertEqual(r.claude_submit_phase, "none")
                    self.assertIsNone(r.claude_submit_event_id)
                else:
                    self.assertEqual({k: getattr(r, k) for k in before}, before)
                self.assertEqual(d.claude_event_ledger.status_of(e["event_id"]), "reserved")
                self.assert_no_input(c, (1, 0))

    def test_new_human_task_retires_old_slot_and_resumes_exactly_once(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.old_pending(root, unknown=unknown)
                original = copy.deepcopy(d.claude_event_ledger.events[e["event_id"]])
                pending = copy.deepcopy(r.to_dict())
                prompt, path = self.native_prompt(root, e)
                with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                    d._handle_claude_event(prompt, c)
                audit = d.claude_event_ledger.events[e["event_id"]]["provider_superseded_submission"]
                self.assertEqual(audit["event"], {**original, "event_id": e["event_id"]})
                for key, value in audit["pending_submit"].items():
                    self.assertEqual(value, pending[key], key)
                self.assertEqual(audit["pending_submit"]["claude_submit_write_unknown"], unknown)
                self.assertEqual(audit["pending_submit"]["claude_submit_attempts"], 1)
                self.assertEqual(audit["pending_submit"]["send_count"], 3)
                self.assertEqual(audit["sha256"], w.supervisor_closeout.snapshot_sha256(
                    {k: v for k, v in audit.items() if k != "sha256"}))
                self.assertEqual(r.claude_orphan_enter_count, 2)
                self.assertEqual(r.claude_submit_confirmed_at, 0)
                self.assertEqual(d.claude_event_ledger.status_of(e["event_id"]), "reserved")
                self.idle(c)
                stop = dict(claude_hook_event("new-task-stop", "Stop"),
                            agent_pid=1234, process_generation="generation-a")
                d._handle_claude_event(stop, c)
                d._handle_claude_event(stop, c)
                self.assertEqual(d.claude_event_ledger.status_of(stop["event_id"]), "sent")
                self.assertEqual(r.claude_submit_event_id, stop["event_id"])
                self.assertEqual((len(c.sent_text), len(c.sent_keys)), (2, 1))
                self.assertEqual(d.claude_event_ledger.events[e["event_id"]]["provider_superseded_submission"], audit)

    def test_release_archive_failure_preserves_slot_then_restart_retires_idempotently(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.old_pending(root)
            d.save()  # Deliberately keep this old runtime throughout the test.
            before = copy.deepcopy(r.to_dict())
            original = copy.deepcopy(d.claude_event_ledger.events[e["event_id"]])
            prompt, path = self.native_prompt(root, e)
            raw_write = d.claude_event_ledger._write
            def disk_full(events=None):
                if events and events.get(e["event_id"], {}).get("provider_superseded_submission"):
                    raise OSError("archive fsync failed")
                return raw_write(events)
            with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path), \
                 mock.patch.object(d.claude_event_ledger, "_write", side_effect=disk_full), \
                 self.assertRaises(OSError):
                d._release_claude_provider_guard(t["surface_id"], r, prompt, reason="native_human_prompt")
            self.assertEqual(d.claude_event_ledger.provider_guard(t["surface_id"])["status"], "released")
            for key, value in before.items():
                if key != "claude_provider_guard":
                    self.assertEqual(getattr(r, key), value, key)
            self.assertEqual(d.claude_event_ledger.events[e["event_id"]], original)
            self.assert_no_input(c, (1, 0))
            d2, r2 = self.restart(d, c)
            audit = copy.deepcopy(d2.claude_event_ledger.events[e["event_id"]]["provider_superseded_submission"])
            self.assertTrue(audit["pending_submit"]["claude_submit_write_unknown"])
            self.assertEqual(r2.claude_submit_phase, "none")
            self.assertEqual(r2.claude_orphan_enter_count, 2)
            self.assertEqual(d2.claude_event_ledger.status_of(e["event_id"]), "reserved")
            # The same old state plus a now-durable archive must not rewrite
            # its first evidence or reclaim an input budget on any restart.
            for _ in range(2):
                d2, r2 = self.restart(d, c)
                d2._sync_claude_provider_guard(t["surface_id"], r2)
                self.assertEqual(d2.claude_event_ledger.events[e["event_id"]]["provider_superseded_submission"], audit)
                self.assertEqual(r2.claude_submit_phase, "none")
            with mock.patch.object(w, "CLAUDE_EVENT_LEDGER_LIMIT", 1):
                d2.claude_event_ledger.mark(e, "deferred_expired")
                for i in range(4):
                    d2.claude_event_ledger.mark(dict(e, event_id=f"unrelated-{i}"), "completed")
            self.assertEqual(d2.claude_event_ledger.events[e["event_id"]]["provider_superseded_submission"], audit)
            self.assert_no_input(c, (1, 0))

    def test_old_event_echo_and_orphan_composer_cannot_reopen_archived_input(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.old_pending(root)
            prompt, path = self.native_prompt(root, e)
            with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                d._handle_claude_event(prompt, c)
            audit = copy.deepcopy(d.claude_event_ledger.events[e["event_id"]]["provider_superseded_submission"])
            old_echo = dict(claude_hook_event("old-echo", "UserPromptSubmit", prompt_kind="watchdog",
                           created_at=e["created_at"] + 1), agent_pid=1234, process_generation="generation-a")
            d._handle_claude_event(old_echo, c)
            d._handle_claude_event(e, c)
            self.assertFalse(d._send_claude_event(e, t, r, c)[0])
            state = w.ScreenState("composer_busy", watchdog_echo=True, message_kind="claude")
            with mock.patch.object(w.time, "time", return_value=time.time() + 10000):
                d._reconcile_claude_submit(t, r, state, c)
                d._recover_orphan_watchdog_submit(t, r, state, c)
            self.assertEqual(d.claude_event_ledger.status_of(old_echo["event_id"]), "stale_native_event")
            self.assertIsNone(r.claude_submit_event_id)
            self.assertEqual(r.claude_orphan_enter_count, 2)
            self.assertEqual(d.claude_event_ledger.events[e["event_id"]]["provider_superseded_submission"], audit)
            self.assert_no_input(c, (1, 0))

    def test_native_receipt_settles_only_its_submit_and_keeps_provider_policy(self):
        for mode in ("released", "retry_wait", "active_retry_wait", "blocked"):
            for lost_ack in (False, True):
                with self.subTest(mode=mode, lost_ack=lost_ack), tempfile.TemporaryDirectory() as root:
                    d, t, r, c, prompt, path, now = self.receipt_case(root, mode, lost_ack=lost_ack)
                    source = r.claude_submit_event_id
                    prior_guard = copy.deepcopy(r.claude_provider_guard)
                    prior_count = r.send_count
                    with mock.patch.object(w.time, "time", return_value=now + 3), \
                         mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                        d._handle_claude_event(prompt, c)
                        d._handle_claude_event(prompt, c)
                    self.assertEqual(r.claude_submit_phase, "none")
                    self.assertFalse(r.claude_submit_write_unknown)
                    self.assertEqual(r.claude_provider_guard, prior_guard)
                    self.assertEqual(r.send_count, prior_count)
                    self.assertEqual(d.claude_event_ledger.status_of(prompt["event_id"]), "watchdog_confirmed")
                    row = d.claude_event_ledger.events[source]
                    self.assertEqual(row["status"], "reserved" if lost_ack else "sent")
                    self.assertEqual(row["provider_native_receipt"]["event"]["event_id"], prompt["event_id"])
                    self.assertEqual(row["provider_native_receipt"]["pending_submit"]["claude_submit_write_unknown"], lost_ack)
                    proof = row["provider_native_receipt"]["native_prompt"]
                    self.assertEqual(proof["path"], str(path))
                    self.assertEqual(proof["prompt_uuid"], "new-native-prompt")
                    self.assertEqual(proof["message_hash"], prompt["message_hash"])
                    if not lost_ack:
                        self.assertEqual(row["watchdog_submission"]["consumed_by"], prompt["event_id"])
                    self.assert_no_input(c, (1, 1))
                    self.idle(c)
                    stop = dict(claude_hook_event("after-receipt", "Stop", created_at=now + 4),
                                agent_pid=1234, process_generation="generation-a")
                    with mock.patch.object(w.time, "time", return_value=now + 4):
                        d._handle_claude_event(stop, c)
                        d._handle_claude_event(stop, c)
                    # 每个 distinct 失败回合在自己的期限后各续一次：
                    # 2 次发送来自 2 个事件，不是同一事件的重复输入。
                    self.assertEqual((len(c.sent_text), len(c.sent_keys)), (2, 2))

    def test_provider_receipt_rejects_wrong_hash_identity_time_and_unrelated_anchor(self):
        changes = ({"message_hash": "f" * 64}, {"message_hash": "too-short"},
                   {"agent_pid": 777}, {"agent_pid": 0}, {"workspace_id": "other"},
                   {"session_id": "other"}, {"process_generation": "other"},
                   {"created_at": 1.0}, {"synthetic_fallback": True},
                   {"anchor": "absent"}, {"anchor": "other-event"}, {"anchor": "short-hash"})
        for change in changes:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                d, t, r, c, prompt, path, now = self.receipt_case(root, "released")
                source = r.claude_submit_event_id
                if change.get("anchor") == "absent":
                    r.claude_last_submit_at = 0
                elif change.get("anchor") == "other-event":
                    r.claude_last_submit_event_id = "different-submit"
                elif change.get("anchor") == "short-hash":
                    r.claude_last_submit_message_hash = r.claude_submit_message_hash[:1]
                before = copy.deepcopy(r.to_dict())
                with mock.patch.object(w.time, "time", return_value=now + 3), \
                     mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                    d._handle_claude_event({**prompt, **change}, c)
                self.assertTrue(r.claude_submit_write_unknown)
                for key, value in before.items():
                    if key.startswith(("claude_submit_", "claude_last_submit_", "claude_orphan_")):
                        self.assertEqual(getattr(r, key), value, key)
                self.assertNotIn("provider_native_receipt", d.claude_event_ledger.events[source])
                self.assert_no_input(c, (1, 1))

    def test_provider_receipt_requires_exact_current_native_user(self):
        for change in ("missing", "body", "old", "meta", "sidechain", "synthetic",
                       "session", "ambiguous", "changed_after_proof"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                d, t, r, c, prompt, path, now = self.receipt_case(root, "blocked")
                source = r.claude_submit_event_id
                before = d._claude_provider_submission_snapshot(r)
                row_before = copy.deepcopy(d.claude_event_ledger.events[source])
                guard_before = copy.deepcopy(r.claude_provider_guard)
                native = json.loads(path.read_text())
                if change == "missing":
                    path.unlink()
                elif change == "body":
                    native["message"]["content"] = "unrelated native body"
                elif change == "old":
                    native["timestamp"] = dt.datetime.fromtimestamp(now - 1, dt.timezone.utc).isoformat()
                elif change in {"meta", "sidechain", "synthetic"}:
                    native[{"meta": "isMeta", "sidechain": "isSidechain", "synthetic": "isSynthetic"}[change]] = True
                elif change == "session":
                    native["sessionId"] = "unrelated-session"
                if change != "missing":
                    path.write_text((json.dumps(native, ensure_ascii=False) + "\n") *
                                    (2 if change == "ambiguous" else 1))
                verify = w.supervisor_closeout.native_human_prompt
                def source_change(*args, **kwargs):
                    proof = verify(*args, **kwargs)
                    if change == "changed_after_proof":
                        path.write_text(path.read_text() + "{}\n")
                    return proof
                with mock.patch.object(w.time, "time", return_value=now + 3), \
                     mock.patch.object(w.dispatch_recovery, "native_path", return_value=path), \
                     mock.patch.object(w.supervisor_closeout, "native_human_prompt", side_effect=source_change):
                    d._handle_claude_event(prompt, c)
                self.assertEqual(d._claude_provider_submission_snapshot(r), before)
                self.assertEqual(d.claude_event_ledger.events[source], row_before)
                self.assertEqual(r.claude_provider_guard, guard_before)
                self.assert_no_input(c, (1, 1))

    def test_provider_receipt_write_failure_retains_unknown_and_original_budget(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, c, prompt, path, now = self.receipt_case(root, "blocked")
            source = r.claude_submit_event_id
            before = copy.deepcopy(r.to_dict())
            raw_write = d.claude_event_ledger._write
            def disk_full(events=None):
                if events and events.get(source, {}).get("provider_native_receipt"):
                    raise OSError("receipt fsync failed")
                return raw_write(events)
            with mock.patch.object(w.time, "time", return_value=now + 3), \
                 mock.patch.object(w.dispatch_recovery, "native_path", return_value=path), \
                 mock.patch.object(d.claude_event_ledger, "_write", side_effect=disk_full), \
                 self.assertRaises(OSError):
                d._handle_claude_event(prompt, c)
            for key, value in before.items():
                if key.startswith(("claude_submit_", "claude_last_submit_", "claude_orphan_")) or key == "send_count":
                    self.assertEqual(getattr(r, key), value, key)
            self.assertNotIn("provider_native_receipt", d.claude_event_ledger.events[source])
            d2, r2 = self.restart(d, c)
            self.assertTrue(r2.claude_submit_write_unknown)
            self.assertEqual(r2.claude_submit_event_id, source)
            self.assert_no_input(c, (1, 1))

    def test_native_receipt_before_state_crash_retires_once_and_survives_prune(self):
        for mode in ("released", "retry_wait", "blocked"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                d, t, r, c, prompt, path, now = self.receipt_case(root, mode)
                source = r.claude_submit_event_id
                d.save()
                def crash_after_receipt(*args, **kwargs):
                    raise RuntimeError("crash before runtime settlement")
                with mock.patch.object(w.time, "time", return_value=now + 3), \
                     mock.patch.object(w.dispatch_recovery, "native_path", return_value=path), \
                     mock.patch.object(d, "_clear_claude_submit", side_effect=crash_after_receipt), \
                     self.assertRaises(RuntimeError):
                    d._handle_claude_event(prompt, c)
                audit = copy.deepcopy(d.claude_event_ledger.events[source]["provider_native_receipt"])
                for _ in range(2):
                    with mock.patch.object(w.time, "time", return_value=now + 4):
                        d2, r2 = self.restart(d, c)
                        d2._handle_claude_event(prompt, c)
                        d2._sync_claude_provider_guard(t["surface_id"], r2)
                    self.assertEqual(r2.claude_submit_phase, "none")
                    self.assertFalse(r2.claude_submit_write_unknown)
                    self.assertEqual(d2.claude_event_ledger.status_of(source), "reserved")
                    self.assertEqual(d2.claude_event_ledger.status_of(prompt["event_id"]), "watchdog_confirmed")
                    self.assertEqual(d2.claude_event_ledger.events[source]["provider_native_receipt"], audit)
                with mock.patch.object(w, "CLAUDE_EVENT_LEDGER_LIMIT", 1):
                    d2.claude_event_ledger.events[source]["status"] = "deferred_expired"
                    d2.claude_event_ledger.mark(dict(prompt, event_id="unrelated"), "completed")
                self.assertEqual(d2.claude_event_ledger.events[source]["provider_native_receipt"], audit)
                self.assert_no_input(c, (1, 1))

    def test_native_release_rejects_synthetic_old_wrong_identity_and_watchdog(self):
        cases = ({"synthetic_fallback": True}, {"agent_pid": 0}, {"agent_pid": 777},
                 {"process_generation": "old"}, {"workspace_id": "other"},
                 {"session_id": "other"}, {"created_at": 1.0}, {"message_hash": "bad"})
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.case(root)
                self.hold(d, t, r, e)
                prompt, path = self.native_prompt(root, e)
                with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                    self.assertFalse(d._release_claude_provider_guard(t["surface_id"], r, {**prompt, **changes}, reason="native_human_prompt"))
                self.assertEqual(r.claude_provider_guard["status"], "retry_wait")
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root, pending=True)
            self.hold(d, t, r, e)
            prompt, path = self.native_prompt(root, e, text=w.CLAUDE_MESSAGE)
            with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                self.assertFalse(d._release_claude_provider_guard(t["surface_id"], r, prompt, reason="native_human_prompt"))

    def test_native_source_change_after_release_candidate_keeps_hold(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            self.hold(d, t, r, e)
            prompt, path = self.native_prompt(root, e)
            raw_put = d.claude_event_ledger.put_provider_guard
            def changing(surface, value):
                result = raw_put(surface, value)
                if value.get("release_candidate"):
                    path.write_text(path.read_text() + "{}\n")
                return result
            with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path), \
                 mock.patch.object(d.claude_event_ledger, "put_provider_guard", side_effect=changing):
                self.assertFalse(d._release_claude_provider_guard(t["surface_id"], r, prompt, reason="native_human_prompt"))
            self.assertEqual(r.claude_provider_guard["status"], "retry_wait")
            self.assertIn("release_candidate", r.claude_provider_guard)
            self.assert_no_input(c)

    def test_verified_new_session_and_process_release_but_same_or_unknown_do_not(self):
        for new in (False, True):
            with self.subTest(new_session=new), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.case(root)
                self.hold(d, t, r, e)
                event = {**e, "event_name": "SessionStart", "event_id": "session-start",
                         "agent_pid": 1234, "synthetic_fallback": False, "created_at": time.time() - 1,
                         "session_id": "new-session" if new else r.claude_session_id}
                d._handle_claude_event(event, c)
                self.assertEqual(r.claude_provider_guard["status"], "released" if new else "retry_wait")
        for observation, released in (({}, False),
                ({"pid": 1234, "generation": "generation-a", "started_epoch": time.time() - 1}, False),
                ({"pid": 2345, "generation": "generation-b", "started_epoch": time.time() - 1}, True)):
            with self.subTest(process=observation), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.case(root)
                self.hold(d, t, r, e)
                d._release_claude_provider_generation(r, observation)
                self.assertEqual(r.claude_provider_guard["status"], "released" if released else "retry_wait")

    def test_release_watermark_blocks_old_deferred_pending_and_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.old_pending(root)
            d.save()  # An old blocked state is weaker than the release ledger.
            prompt, path = self.native_prompt(root, e)
            with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                self.assertTrue(d._release_claude_provider_guard(t["surface_id"], r, prompt, reason="native_human_prompt"))
            d2, r2 = self.restart(d, c)
            self.assertEqual(r2.claude_provider_guard["status"], "released")
            self.assertIsNone(r2.claude_submit_event_id)
            self.assertEqual(d2._claude_provider_guard_reason(t, r2, event=e), "claude_provider_event_superseded")
            d2.claude_event_ledger.mark(e, "deferred_expired")
            r2.claude_last_event_id, r2.claude_deferred_reason = e["event_id"], "expired"
            self.assertFalse(d2._restore_expired_claude_deferred(r2))
            self.assert_no_input(c, (1, 0))

    def test_old_error_repaint_does_not_undo_verified_release(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            state = w.classify_claude_grid(w.Grid.from_rpc(claude_grid_payload(lines=[AUTH]), "surface-uuid"))
            self.hold(d, t, r, e)
            d._claude_provider_guard_reason(t, r, state)
            prompt, path = self.native_prompt(root, e)
            with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                self.assertTrue(d._release_claude_provider_guard(t["surface_id"], r, prompt, reason="native_human_prompt"))
            # The old repaint remains fenced until a new native lifecycle.
            self.assertEqual(d._claude_provider_guard_reason(t, r, state), "")
            self.assertEqual(r.claude_provider_guard["status"], "released")
            d._claude_provider_guard_reason(t, r, w.ScreenState("working", claude_native_activity="working"))
            self.assertEqual(d._claude_provider_guard_reason(t, r, state), "claude_provider_retry_wait")
            self.assertEqual((r.claude_provider_guard["status"], r.claude_provider_guard["retry_count"]), ("retry_wait", 1))
            self.assertNotIn("invalid", r.claude_provider_guard)
    def consume(self, d, r, attempt, stamp, *, surface="surface-uuid", generation="generation-a"):
        """Credit one identity-bound durable submission to a recorded attempt."""
        guard = dict(r.claude_provider_guard)
        with d.claude_event_ledger._lock:
            row = dict(d.claude_event_ledger.events.get(attempt["event_id"]) or {})
            row.update({key: guard.get(key) for key in (
                "surface_id", "workspace_id", "session_id", "agent_pid", "process_generation",
                "error_kind", "error_reason", "stop_failure_error", "error_evidence_hash")})
            row.update(event_name="StopFailure", created_at=attempt["created_at"])
            row["watchdog_submission"] = {
                "surface_id": surface, "session_id": r.claude_session_id,
                "generation": generation, "agent_pid": r.claude_process_pid,
                "message_hash": "a" * 64, "submitted_at": stamp}
            d.claude_event_ledger.events[attempt["event_id"]] = row
        return row

    def test_consumed_retries_escalate_bounded_deadlines_but_wrong_identity_does_not(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            now = time.time()
            first = self.failure(e, RPM, stamp=now, event_id="429-1")
            with mock.patch.object(w.time, "time", return_value=now):
                guard = d._remember_claude_provider_failure(t, r, first, event=first)
            self.assertEqual((guard["retry_count"], guard["retry_after"]), (1, now + 60))
            previous, seen = first, [(1, 60.0)]
            for count, delay in enumerate((120, 240, 480, 900, 900), 2):
                stamp = now + count * 1000
                self.consume(d, r, previous, stamp - 1)
                event = self.failure(e, RPM, stamp=stamp, event_id=f"429-{count}")
                with mock.patch.object(w.time, "time", return_value=stamp):
                    guard = d._remember_claude_provider_failure(t, r, event, event=event)
                seen.append((guard["retry_count"], guard["retry_after"] - stamp))
                previous = event
            # The ladder itself, not whatever the code happened to produce.
            self.assertEqual(seen, [(1, 60.0), (2, 120.0), (3, 240.0), (4, 480.0), (5, 900.0), (6, 900.0)])
            # Each consumed attempt opens its own consumption window; the count
            # carries the ladder, the episode stamp does not.
            self.assertEqual(guard["retry_episode_started_at"], now + 6000)
        for field, value in (("session_id", "other-session"), ("agent_pid", 4321),
                             ("generation", "generation-z"), ("message_hash", ""),
                             ("surface_id", "other-surface")):
            with self.subTest(receipt=field), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.case(root)
                now = time.time()
                first = self.failure(e, RPM, stamp=now, event_id="429-1")
                with mock.patch.object(w.time, "time", return_value=now):
                    d._remember_claude_provider_failure(t, r, first, event=first)
                stamp = now + 1000
                row = self.consume(d, r, first, stamp - 1)
                row["watchdog_submission"][field] = value
                event = self.failure(e, RPM, stamp=stamp, event_id="429-2")
                with mock.patch.object(w.time, "time", return_value=stamp):
                    guard = d._remember_claude_provider_failure(t, r, event, event=event)
                # A receipt from another identity is not this episode's attempt:
                # the count stays 1 and the original deadline does not move.
                self.assertEqual((guard["retry_count"], guard["retry_after"]), (1, now + 60))

    def screen(self, text=AUTH):
        return w.classify_claude_grid(w.Grid.from_rpc(claude_grid_payload(lines=[text]), "surface-uuid"))

    def released(self, root, d, t, r, e, state):
        self.hold(d, t, r, e)
        d._claude_provider_guard_reason(t, r, state)
        prompt, path = self.native_prompt(root, e)
        with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
            self.assertTrue(d._release_claude_provider_guard(
                t["surface_id"], r, prompt, reason="native_human_prompt"))

    def test_hook_born_hold_records_its_screen_so_release_can_fence_that_repaint(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            state = self.screen()
            fingerprint = str(state.claude_provider_failure["screen_fingerprint"])
            self.assertTrue(fingerprint)
            self.hold(d, t, r, e)
            # A Hook-born hold has no viewport of its own.
            self.assertEqual(r.claude_provider_guard["screen_fingerprint"], "")
            d._claude_provider_guard_reason(t, r, state)
            self.assertEqual(r.claude_provider_guard["screen_fingerprint"], fingerprint)
            prompt, path = self.native_prompt(root, e)
            with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                self.assertTrue(d._release_claude_provider_guard(
                    t["surface_id"], r, prompt, reason="native_human_prompt"))
            self.assertEqual(r.claude_provider_guard["released_screen_fingerprint"], fingerprint)
            self.assertEqual(d._claude_provider_guard_reason(t, r, state), "")
            self.assertEqual(r.claude_provider_guard["status"], "released")
            d._claude_provider_guard_reason(t, r, w.ScreenState("working", claude_native_activity="working"))
            self.assertEqual(r.claude_provider_guard["released_screen_fingerprint"], "")
            # The same screen after native work is a new rejection: one bounded
            # episode, never the invalid fail-closed hold.
            self.assertEqual(d._claude_provider_guard_reason(t, r, state), "claude_provider_retry_wait")
            guard = r.claude_provider_guard
            self.assertEqual((guard["status"], guard["retry_count"], guard.get("invalid")),
                             ("retry_wait", 1, None))

    def test_release_fence_ignores_unknown_other_and_foreign_screens(self):
        for case, text in (("unknown", ""), ("other_error", RPM)):
            with self.subTest(screen=case), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.case(root)
                state = self.screen()
                self.released(root, d, t, r, e, state)
                failure = dict(state.claude_provider_failure)
                if text:
                    failure = dict(self.screen(text).claude_provider_failure)
                else:
                    failure["screen_fingerprint"] = ""
                d._remember_claude_provider_failure(t, r, failure)
                self.assertEqual(r.claude_provider_guard["status"], "retry_wait")
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            state = self.screen()
            self.released(root, d, t, r, e, state)
            r.claude_process_pid = 4321
            d._claude_send_process_identity = mock.Mock(return_value={
                "pid": 4321, "generation": "generation-a", "started_epoch": 1.0})
            d._remember_claude_provider_failure(t, r, state.claude_provider_failure)
            guard = r.claude_provider_guard
            self.assertEqual((guard["status"], guard["agent_pid"]), ("retry_wait", 4321))
            self.assertEqual(float(guard["release_created_at"]), 0.0)
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root)
            state = self.screen()
            self.released(root, d, t, r, e, state)
            self.assertEqual(d._claude_provider_guard_reason(t, r, state), "")
            # A verified new StopFailure stays eligible behind the screen fence.
            event = self.failure(e, event_id="provider-error-2", stamp=time.time())
            d._remember_claude_provider_failure(t, r, event, event=event)
            self.assertEqual(r.claude_provider_guard["status"], "retry_wait")
            self.assertEqual(r.claude_provider_guard["source_event_id"], "provider-error-2")


if __name__ == "__main__":
    unittest.main()

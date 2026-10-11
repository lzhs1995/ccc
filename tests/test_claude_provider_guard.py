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
from tests.test_watch import claude_grid_payload, claude_wrapped_composer_payload, wrap_to_cells


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
        self.assertEqual(w._claude_current_error(grid, grid.rows - 4)["error_kind"], "claude_billing")
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
                    self.assertEqual(d2._claude_provider_guard_reason(t, runtime, state), "claude_provider_blocked")
                d2._remember_claude_provider_failure(t, runtime, classify_stop_failure("", RPM))
                self.assertEqual(runtime.claude_provider_guard["status"], "blocked")
            self.assertEqual(len(d2.claude_event_ledger.events), 2)
            self.assertEqual(self._notify_async.call_count, 1)
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
            self.assertEqual(r2.claude_provider_guard["status"], "blocked")
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
                self.assertEqual((guard["retry_count"], guard["retry_after"]), (count, stamp + delay))
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
            self.assertEqual(d._apply_claude_runtime_guards(t["surface_id"], r, state).kind, "claude_provider_blocked")
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
                d, t, r, e, c = self.case(root, pending=True)
                r.claude_submit_write_unknown = True
                r.claude_orphan_enter_count = 2
                self.hold(d, t, r, e)
                prompt, path = self.native_prompt(root, e, isMeta=not valid)
                before = {k: v for k, v in r.to_dict().items() if k.startswith(("claude_submit_", "claude_orphan_"))}
                with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                    d._handle_claude_event(prompt, c)
                self.assertEqual(r.claude_provider_guard["status"], "released" if valid else "blocked")
                self.assertEqual({k: getattr(r, k) for k in before}, before)
                self.assertEqual(d.claude_event_ledger.status_of(e["event_id"]), "reserved")
                self.assert_no_input(c, (1, 0))

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
                self.assertEqual(r.claude_provider_guard["status"], "blocked")
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
            self.assertEqual(r.claude_provider_guard["status"], "blocked")
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
                self.assertEqual(r.claude_provider_guard["status"], "released" if new else "blocked")
        for observation, released in (({}, False),
                ({"pid": 1234, "generation": "generation-a", "started_epoch": time.time() - 1}, False),
                ({"pid": 2345, "generation": "generation-b", "started_epoch": time.time() - 1}, True)):
            with self.subTest(process=observation), tempfile.TemporaryDirectory() as root:
                d, t, r, e, c = self.case(root)
                self.hold(d, t, r, e)
                d._release_claude_provider_generation(r, observation)
                self.assertEqual(r.claude_provider_guard["status"], "released" if released else "blocked")

    def test_release_watermark_blocks_old_deferred_pending_and_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            d, t, r, e, c = self.case(root, pending=True)
            e["created_at"] = time.time() - 20
            d.claude_event_ledger.mark(e, "reserved")
            self.hold(d, t, r, e)
            d.save()  # An old blocked state is weaker than the release ledger.
            prompt, path = self.native_prompt(root, e)
            with mock.patch.object(w.dispatch_recovery, "native_path", return_value=path):
                self.assertTrue(d._release_claude_provider_guard(t["surface_id"], r, prompt, reason="native_human_prompt"))
            d2, r2 = self.restart(d, c)
            self.assertEqual(r2.claude_provider_guard["status"], "released")
            self.assertEqual(d2._claude_provider_guard_reason(t, r2), "claude_provider_event_superseded")
            r2.claude_submit_event_id, r2.claude_submit_phase = None, "none"
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
            self.assertEqual(d._claude_provider_guard_reason(t, r, state), "")
            self.assertEqual(r.claude_provider_guard["status"], "released")
            d._claude_provider_guard_reason(t, r, w.ScreenState("working", claude_native_activity="working"))
            self.assertEqual(d._claude_provider_guard_reason(t, r, state), "claude_provider_blocked")


if __name__ == "__main__":
    unittest.main()

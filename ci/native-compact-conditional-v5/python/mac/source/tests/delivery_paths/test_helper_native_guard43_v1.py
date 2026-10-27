#!/usr/bin/env python3
"""实际43适配器→真实bridge/journal/native→新版强制native guard；仅隔离终端与进程观察。"""
import argparse
import copy
import datetime
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from unittest import mock

import test_sender_path42_v1 as F

A = B = E = G = I = J = N = None
SECOND = "77777777-7777-4777-8777-777777777777"


class HelperGuardCases(unittest.TestCase):
    configure = F.SenderPathCases.configure
    compose = F.SenderPathCases.compose
    ps = F.SenderPathCases.ps
    read = F.SenderPathCases.read
    key = F.SenderPathCases.key

    def setUp(self):
        F.SenderPathCases.setUp(self)
        self.state = self.root / ".local/state/multi-agent-collaboration"
        self.adapter = A.Adapter()
        self.live_rows = [dict(ref="surface:99", surface_id=F.PINS["target_surface_uuid"],
                               workspace_id=F.PINS["workspace_uuid"], surface_type="terminal",
                               pane_ref="pane:9")]
        self.stack.enter_context(mock.patch.object(B, "list_surfaces", side_effect=lambda: self.live_rows))
        self.stack.enter_context(mock.patch.object(B, "whoami", return_value=dict(
            workspace_id=F.PINS["workspace_uuid"], surface_id=F.PINS["caller_surface_uuid"], pane_ref="pane:8")))
        self.stack.enter_context(mock.patch.object(G.cmux_hook_identity, "resolve",
            return_value=(F.PINS["workspace_uuid"], F.PINS["caller_surface_uuid"])))
        import cmux_consensus_stop_guard as stop
        self.stack.enter_context(mock.patch.object(stop, "ACTIVE_DIR", self.root / "isolated-active"))
        self.prepare()

    def paste(self, surface, text):
        self.assertEqual(text, self.payload)
        self.pastes.append(text)
        self.events.append(("paste", text))
        self.view = self.compose(text)
        self.on_paste()

    def append_native(self, kind="user", text=None):
        F.SenderPathCases.append_native(self, kind, self.payload if text is None else text)

    def prepare(self, mode="send", message="STATUS: 原文必须完整入站。", request_id="helper42", provider=None):
        if provider:
            self.configure(provider)
        self.mode, self.message, self.request_id = mode, message, request_id
        self.request = self.adapter.request(F.PINS, mode, message, request_id)
        self.payload = self.request["payload"]
        self.argv = [mode, "surface:99", "--request-id", request_id, message]
        self.command = shlex.join(["rtk", "cmux-agent", *self.argv])
        self.intent = self.adapter.channel(F.PINS) / "intents" / (self.request["marker"] + ".json")
        self.pending = self.intent.parent.parent / "pending.json"

    def invoke(self, argv=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = A.main(self.argv if argv is None else argv)
        return code, out.getvalue(), err.getvalue()

    def receive(self, **kwargs):
        if kwargs:
            self.prepare(**kwargs)
        def receive(key, count):
            self.append_native()
            self.view = self.compose("")
        self.on_key = receive
        code, out, err = self.invoke()
        self.assertEqual(code, 0, out + err)
        self.assertTrue(json.loads(out)["confirmed"])
        return json.loads(out)

    def files(self):
        return {str(path.relative_to(self.root)): (
            hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size,
            path.stat().st_ino, path.stat().st_mtime_ns)
            for path in self.root.rglob("*") if path.is_file()}

    def guard(self, command=None):
        before = self.files(), list(self.keys), list(self.pastes)
        result = G.evaluate({"tool_input": {"command": self.command if command is None else command}},
                            state_root=self.state)
        self.assertEqual((self.files(), self.keys, self.pastes), before,
                         "只读核验改了证据、键或粘贴")
        return result

    def resolve(self):
        calls = I.delivery_calls(self.command)
        self.assertEqual(len(calls), 1)
        return E.resolve(calls[0], B, F.PINS["caller_surface_uuid"], F.PINS["workspace_uuid"])

    def test_actual_ask_and_send_full_native_user(self):
        for mode in ("ask", "send"):
            with self.subTest(mode=mode):
                result = self.receive(mode=mode, request_id=mode)
                self.assertFalse(self.pending.exists())
                self.assertEqual(json.loads(self.intent.read_text()), self.request)
                self.assertEqual(self.guard()["action"], "pass")
                before = list(self.keys), list(self.pastes)
                self.assertEqual(self.invoke()[0], 0)
                self.assertEqual((self.keys, self.pastes), before)
                self.assertEqual(result["request"], str(self.intent))

    def test_python_adapter_cli_has_same_original_binding(self):
        self.receive()
        command = shlex.join(["rtk", "proxy", "/test/bin/python3", "-B",
                             str(Path(A.__file__).resolve()), *self.argv])
        self.assertEqual(self.guard(command)["action"], "pass")

    def test_enter_newline_never_confirms_and_guard_does_not_retry(self):
        self.on_key = lambda key, count: setattr(self, "view", self.compose(self.payload + "\n"))
        self.assertEqual(self.invoke()[0], 75)
        self.assertTrue(self.pending.exists())
        self.assertNotEqual(self.guard()["action"], "pass")
        self.assertLessEqual(len(self.keys), 2)
        self.assertEqual(len(self.pastes), 1)

    def test_empty_composer_without_native_user(self):
        self.on_key = lambda key, count: setattr(self, "view", self.compose(""))
        self.assertEqual(self.invoke()[0], 75)
        self.assertEqual(self.guard()["action"], "block")

    def test_assistant_echo_cannot_confirm(self):
        def echoed(key, count):
            self.append_native(kind="assistant")
            self.view = self.compose("")
        self.on_key = echoed
        self.assertEqual(self.invoke()[0], 75)
        self.assertEqual(self.guard()["action"], "block")

    def test_native_user_without_original_marker_cannot_confirm(self):
        def altered(key, count):
            self.append_native(text="Unrelated user input without the original marker")
            self.view = self.compose("")
        self.on_key = altered
        self.assertEqual(self.invoke()[0], 75)
        self.assertEqual(self.guard()["action"], "block")

    def test_same_full_text_before_original_baseline_is_not_receipt(self):
        self.append_native()
        self.on_key = lambda key, count: setattr(self, "view", self.compose(""))
        self.assertEqual(self.invoke()[0], 75)
        self.assertEqual(self.guard()["action"], "block")

    def test_claude_queued_command_then_exact_user_readonly_reconcile(self):
        self.prepare(provider="claude")
        def queued(key, count):
            self.append_native(kind="queued")
            self.view = self.compose("")
        self.on_key = queued
        self.assertEqual(self.invoke()[0], 75)
        self.assertEqual(self.guard()["action"], "block")
        before = list(self.keys), list(self.pastes)
        self.append_native()
        argv = ["reconcile", "surface:99", "--intent", str(self.intent)]
        self.assertEqual(self.invoke(argv)[0], 0)
        self.assertEqual(self.guard(shlex.join(["cmux-agent", *argv]))["action"], "pass")
        self.assertEqual((self.keys, self.pastes), before)

    def test_changed_request_cannot_borrow_same_surface_receipt(self):
        self.receive()
        self.prepare(message="STATUS: 这是后继任务，不能借旧回执。")
        self.assertEqual(self.guard()["action"], "block")

    def test_reconcile_requires_original_intent_even_after_success(self):
        self.receive()
        argv = ["reconcile", "surface:99"]
        self.assertEqual(self.invoke(argv)[0], 75)
        self.assertEqual(self.guard(shlex.join(["cmux-agent", *argv]))["action"], "block")

    def test_copied_intent_does_not_authorize_reconcile(self):
        self.receive()
        copy_path = self.root / self.intent.name
        copy_path.write_bytes(self.intent.read_bytes())
        command = shlex.join(["cmux-agent", "reconcile", "surface:99", "--intent", str(copy_path)])
        self.assertEqual(self.guard(command)["action"], "block")

    def test_other_pending_cannot_authorize_old_success(self):
        self.receive()
        self.pending.write_text(json.dumps(dict(identity=F.PINS, marker="CMUX_HELPER_" + "a" * 64)))
        self.assertEqual(self.guard()["action"], "block")

    def test_absent_pending_is_pinned_until_verification_finishes(self):
        self.receive()
        call = self.resolve()[0]
        self.pending.write_text(json.dumps(dict(identity=F.PINS, marker="CMUX_HELPER_" + "a" * 64)))
        before = self.files()
        with self.assertRaises(ValueError):
            E.verify(call, B)
        self.assertEqual(self.files(), before)

    def test_intent_drift_during_native_verification_is_refused(self):
        self.receive()
        call = self.resolve()[0]
        original = E.verified_receipt
        def mutate(*args, **kwargs):
            proof = original(*args, **kwargs)
            self.intent.write_text("{}")
            return proof
        with mock.patch.object(E, "verified_receipt", side_effect=mutate), self.assertRaises(ValueError):
            E.verify(call, B)

    def test_target_drift_during_native_verification_is_refused(self):
        self.receive()
        call = self.resolve()[0]
        original = E.verified_receipt
        drifted = False
        def pin(surface):
            return dict(F.PINS, target_pane_uuid=SECOND) if drifted else dict(F.PINS)
        def drift(*args, **kwargs):
            nonlocal drifted
            proof = original(*args, **kwargs)
            drifted = True
            return proof
        with mock.patch.object(B, "pin_workspace", side_effect=pin), \
                mock.patch.object(E, "verified_receipt", side_effect=drift), self.assertRaises(ValueError):
            E.verify(call, B)

    def test_quoted_punctuation_and_hash_are_part_of_original_payload(self):
        pieces = ["STATUS:", ";", "|", "literal#hash", "$BODY"]
        self.receive(message=" ".join(pieces))
        command = shlex.join(["cmux-agent", self.mode, "surface:99", "--request-id", self.request_id, *pieces])
        self.assertEqual(I.delivery_calls(command)[0]["text"], self.message)
        self.assertEqual(self.guard(command)["action"], "pass")
        self.receive(message="literal#hash", request_id="unquoted-hash")
        command = "cmux-agent send surface:99 --request-id unquoted-hash literal#hash"
        self.assertEqual(self.guard(command)["action"], "pass")

    def test_shell_expansions_cannot_borrow_literal_message_receipt(self):
        cases = [
            ("$BODY", '"$BODY"'), ("$BODY", "$BODY"),
            ("$(printf x)", '"$(printf x)"'), ("$(printf x)", "$(printf x)"),
            ("`printf x`", '"`printf x`"'), ("*", "*"),
        ]
        for index, (literal, expression) in enumerate(cases):
            with self.subTest(expression=expression):
                self.receive(message=literal, request_id="dynamic-" + str(index))
                self.assertEqual(self.guard()["action"], "pass")
                command = "cmux-agent send surface:99 --request-id " + self.request_id + " " + expression
                self.assertEqual(self.guard(command)["action"], "block")

    def test_dynamic_request_id_cannot_borrow_literal_id(self):
        self.receive(request_id="$REQUEST")
        command = 'cmux-agent send surface:99 --request-id "$REQUEST" ' + shlex.quote(self.message)
        self.assertEqual(self.guard(command)["action"], "block")

    def test_static_shell_and_js_wrappers_keep_binding(self):
        self.receive()
        shell = shlex.join(["rtk", "proxy", "bash", "-c", self.command])
        js = "await tools.exec_command({cmd: " + json.dumps(self.command) + "});"
        self.assertEqual(self.guard(shell)["action"], "pass")
        self.assertEqual(self.guard(js)["action"], "pass")

    def test_dynamic_js_helper_call_stays_unverified(self):
        command = 'const cmd = "cmux-agent send surface:99 " + message; await tools.exec_command({cmd});'
        self.receive()
        self.assertEqual(self.guard(command)["action"], "block")

    def test_unquoted_python_heredoc_is_not_literal_receipt_input(self):
        # Helper envelopes now use a reference notice, so $BODY would live only
        # in the pinned body file. Exercise shell expansion on the actual wire
        # through the same real bridge/journal/native implementation instead.
        self.payload = "STATUS: HEREDOC_LITERAL $BODY"
        marker = "HEREDOC_LITERAL"
        def receive(key, count):
            self.append_native()
            self.view = self.compose("")
        self.on_key = receive
        result = B.submit_text(F.PINS["target_surface_uuid"], self.payload, marker=marker)
        self.assertTrue(result["confirmed"])
        body = "import cmux_bridge\ncmux_bridge.submit_text(" + repr(F.PINS["target_surface_uuid"]) + ", " + repr(self.payload) + ", marker=" + repr(marker) + ")"
        self.assertIn("$BODY", body)
        self.assertEqual(self.guard("python3 - <<'PY'\n" + body + "\nPY\n")["action"], "pass")
        self.assertEqual(self.guard("python3 - <<PY\n" + body + "\nPY\n")["action"], "block")

    def setup_broadcast(self):
        pins2 = dict(F.PINS, target_surface_uuid=SECOND,
                     target_pane_uuid="88888888-8888-4888-8888-888888888888")
        self.pins = {F.PINS["target_surface_uuid"]: dict(F.PINS), SECOND: pins2}
        self.live_rows.append(dict(ref="surface:100", surface_id=SECOND,
                                   workspace_id=F.PINS["workspace_uuid"], surface_type="terminal", pane_ref="pane:10"))
        self.native_paths = {F.PINS["target_surface_uuid"]: self.transcript}
        procs, rows = {}, {}
        for index, (target, pins) in enumerate(self.pins.items()):
            sid = F.SESSION if not index else "99999999-9999-4999-8999-999999999999"
            proc = copy.deepcopy(self.proc)
            proc.update(pid=41000 + index, birth=[10, 20 + index], argv=["codex", "resume", sid])
            proc["env"]["CMUX_SURFACE_ID"] = target
            procs[target] = proc
            rows[target] = dict(pins, tty="ttys" + str(999 + index), kind="terminal", dock="workspace")
            if index:
                path = self.transcript.with_name("rollout-2026-09-29T00-00-00-" + sid + ".jsonl")
                path.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n")
                self.native_paths[target] = path
        self.stack.enter_context(mock.patch.object(B, "pin_workspace", side_effect=lambda surface: self.pins[
            {"surface:99": F.PINS["target_surface_uuid"], "surface:100": SECOND}.get(surface, surface.lower())]))
        self.stack.enter_context(mock.patch.object(N, "_tree_row", side_effect=lambda bridge, target: rows[target.lower()]))
        self.stack.enter_context(mock.patch.object(N, "_target_process", side_effect=lambda row: procs[row["target_surface_uuid"]]))
        self.stack.enter_context(mock.patch.object(N.identity, "process", side_effect=lambda pid, **kw:
            next(p for p in procs.values() if p["pid"] == pid)))
        def ps(args, **kwargs):
            self.assertEqual(args[:2], ["/bin/ps", "-p"])
            pid = int(args[2])
            return subprocess.CompletedProcess(args, 0, stdout="ttys" + str(999 + pid - 41000) + "\n", stderr="")
        self.stack.enter_context(mock.patch.object(N.subprocess, "run", side_effect=ps))
        self.views = {target: self.compose("") for target in self.pins}
        self.payloads = {}
        self.stack.enter_context(mock.patch.object(B, "read_screen", side_effect=lambda surface, **kw: self.views[surface.lower()]))
        def paste(surface, text):
            target = surface.lower()
            self.payloads[target] = text
            self.pastes.append((target, text))
            self.views[target] = self.compose(text)
        def key(surface, key):
            target = surface.lower()
            self.keys.append((target, key))
            self.views[target] = self.compose("")
            if target != SECOND or self.complete_broadcast:
                self.append_broadcast_user(target)
        self.stack.enter_context(mock.patch.object(B, "send_text", side_effect=paste))
        self.stack.enter_context(mock.patch.object(B, "send_key", side_effect=key))
        self.complete_broadcast = False
        self.argv = ["broadcast", "--request-id", "broadcast42", "STATUS: 同组两条均需入站。"]
        self.command = shlex.join(["cmux-agent", *self.argv])

    def append_broadcast_user(self, target):
        row = dict(type="response_item", timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   payload=dict(type="message", role="user", content=[dict(type="input_text", text=self.payloads[target])]))
        with self.native_paths[target].open("a") as out:
            out.write(json.dumps(row) + "\n")

    def test_broadcast_partial_receipt_cannot_claim_all_and_retry_is_readonly(self):
        self.setup_broadcast()
        code, out, err = self.invoke()
        self.assertEqual(code, 75, out + err)
        self.assertEqual(len(self.pastes), 2, err)
        self.assertEqual(self.guard()["action"], "block")
        before = list(self.keys), list(self.pastes)
        self.append_broadcast_user(SECOND)
        code, out, err = self.invoke()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.guard()["action"], "pass")
        self.assertEqual((self.keys, self.pastes), before)

    def test_broadcast_rechecks_prior_member_when_verifying_later_member(self):
        self.setup_broadcast()
        self.complete_broadcast = True
        code, out, err = self.invoke()
        self.assertEqual(code, 0, out + err)
        calls = self.resolve()
        self.assertIsNotNone(E.verify(calls[0], B))
        self.pins[F.PINS["target_surface_uuid"]]["target_pane_uuid"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        with self.assertRaises(ValueError):
            E.verify(calls[1], B)

#!/usr/bin/env python3
"""独立发送路径回归；仅 mock 终端及进程观察，bridge/journal/native 保持真实。"""
import argparse
import contextlib
import datetime
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

B = J = N = None
SESSION = "11111111-1111-4111-8111-111111111111"
PINS = {
    "workspace_uuid": "33333333-3333-4333-8333-333333333333",
    "caller_surface_uuid": "44444444-4444-4444-8444-444444444444",
    "target_surface_uuid": "55555555-5555-4555-8555-555555555555",
    "target_pane_uuid": "66666666-6666-4666-8666-666666666666",
}
MARKER = "SENDER_PATH42_20261009"
PAYLOAD = "STATUS: " + MARKER + " 完整消息必须由原接收会话记录。"


class SenderPathCases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sender42-private-")
        self.root = Path(self.temp.name).resolve()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(Path, "home", return_value=self.root))
        self.stack.enter_context(mock.patch.object(B.time, "sleep"))
        self.stack.enter_context(mock.patch.object(B, "pin_workspace", return_value=dict(PINS)))
        self.row = dict(PINS, tty="ttys999", kind="terminal", dock="workspace")
        self.stack.enter_context(mock.patch.object(N, "_tree_row", return_value=self.row))
        self.stack.enter_context(mock.patch.object(N, "_target_process", side_effect=lambda row: self.proc))
        self.stack.enter_context(mock.patch.object(N.identity, "process", side_effect=lambda *a, **k: self.proc))
        self.stack.enter_context(mock.patch.object(N.subprocess, "run", side_effect=self.ps))
        self.stack.enter_context(mock.patch.object(B, "read_screen", side_effect=self.read))
        self.stack.enter_context(mock.patch.object(B, "send_text", side_effect=self.paste))
        self.stack.enter_context(mock.patch.object(B, "send_key", side_effect=self.key))
        self.events = []
        self.screens = []
        self.pastes = []
        self.keys = []
        self.on_key = lambda key, count: None
        self.on_paste = lambda: None
        self.configure("codex")

    def configure(self, provider):
        self.provider = provider
        self.glyph = "›" if provider == "codex" else "❯"
        self.footer = "GPT-6 high" if provider == "codex" else "[claude-opus-5]"
        self.proc = {
            "pid": 41000, "birth": [10, 20], "executable": "/test/bin/" + provider,
            "file_identity": [1, 2, 3], "argv": [provider, "resume", SESSION],
            "env": {"CMUX_SURFACE_ID": PINS["target_surface_uuid"],
                    "CMUX_WORKSPACE_ID": PINS["workspace_uuid"]},
        }
        native_root = self.root / (".codex/sessions" if provider == "codex" else ".claude/projects")
        self.transcript = native_root / "2026/09/29" / (
            "rollout-2026-09-29T00-00-00-" + SESSION + ".jsonl"
            if provider == "codex" else SESSION + ".jsonl")
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        meta = ({"type": "session_meta", "payload": {"id": SESSION}} if provider == "codex"
                else {"type": "queue-operation", "sessionId": SESSION})
        self.transcript.write_text(json.dumps(meta) + "\n")
        self.view = self.compose("")
        self.pending = self.compose(PAYLOAD)

    def compose(self, text, status="", hint=False):
        rows = [status] if status else []
        lines = text.split("\n")
        rows += [self.glyph + " " + lines[0]] + ["  " + s for s in lines[1:]]
        rows.append(self.footer)
        if hint:
            rows.append("tab to queue message")
        return "\n".join(rows)

    def ps(self, args, **kwargs):
        if args != ["/bin/ps", "-p", "41000", "-o", "tty="]:
            raise AssertionError("Unexpected external command: " + repr(args))
        return subprocess.CompletedProcess(args, 0, stdout="ttys999\n", stderr="")

    def read(self, surface, **kwargs):
        if self.screens:
            self.view = self.screens.pop(0)
        self.events.append(("read", self.view))
        return self.view

    def paste(self, surface, text):
        self.assertEqual(text, PAYLOAD)
        self.pastes.append(text)
        self.events.append(("paste", text))
        self.view = self.pending
        self.on_paste()

    def key(self, surface, key):
        self.keys.append(key)
        self.events.append(("key", key))
        self.on_key(key, len(self.keys))

    def append_native(self, kind="user", text=PAYLOAD):
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if self.provider == "codex":
            row = {"type": "response_item", "timestamp": stamp,
                   "payload": {"type": "message", "role": kind,
                               "content": [{"type": "input_text" if kind == "user" else "output_text",
                                            "text": text}]}}
        elif kind == "queued":
            row = {"type": "attachment", "timestamp": stamp, "sessionId": SESSION,
                   "attachment": {"type": "queued_command", "prompt": text}}
        else:
            row = {"type": kind, "timestamp": stamp, "sessionId": SESSION,
                   "message": {"role": kind, "content": text}}
        with self.transcript.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def deliver(self, **kwargs):
        return B.submit_text(PINS["target_surface_uuid"], PAYLOAD, marker=MARKER, **kwargs)

    def journal(self):
        paths = list((self.root / ".local/state/multi-agent-collaboration/message-dispatch-v1").glob("*"))
        self.assertEqual(len(paths), 1)
        return paths[0]

    def original_attempt(self):
        paths = list(self.journal().glob("attempt-*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], json.loads(paths[0].read_text())

    def unconfirmed(self, **kwargs):
        with self.assertRaises(B.DispatchUnconfirmed):
            self.deliver(**kwargs)
        self.assertFalse((self.journal() / "receipt.json").exists())

    def test_first_key_requires_two_full_consecutive_observations(self):
        full = self.pending
        partial = self.compose(PAYLOAD[:32])
        self.screens = [self.view, full, partial, full, "unknown screen", full, full]
        def receive(key, count):
            reads = [e[1] for e in self.events if e[0] == "read"]
            self.assertEqual(reads, [self.compose(""), full, partial, full, "unknown screen", full, full])
            self.append_native()
            self.view = self.compose("")
        self.on_key = receive
        result = self.deliver()
        self.assertIs(result["confirmed"], True)
        self.assertEqual(self.keys, ["enter"])
        self.assertEqual(self.pastes, [PAYLOAD])
        self.assertIsNotNone(J.verified_receipt(B, PINS["target_surface_uuid"], PAYLOAD, MARKER))

    def test_partial_paste_never_gets_submit_key(self):
        self.screens = [self.view] + [self.compose(PAYLOAD[:25])] * 20
        self.unconfirmed()
        self.assertEqual(self.keys, [])
        self.assertEqual(self.pastes, [PAYLOAD])

    def test_enter_newline_and_native_changed_payload_cannot_confirm(self):
        def newline(key, count):
            self.view = self.compose(PAYLOAD + "\n")
            self.append_native(text=PAYLOAD + "\n")
        self.on_key = newline
        self.unconfirmed()
        self.assertLessEqual(len(self.keys), 2)
        self.assertEqual(self.pastes, [PAYLOAD])

    def test_empty_composer_without_native_message_cannot_confirm(self):
        self.on_key = lambda key, count: setattr(self, "view", self.compose(""))
        self.unconfirmed()
        self.assertEqual(self.keys, ["enter"])

    def test_unrelated_activity_and_assistant_echo_cannot_confirm(self):
        def unrelated(key, count):
            self.view = PAYLOAD + "\n• Read unrelated file\n" + self.compose("")
            self.append_native(kind="assistant")
        self.on_key = unrelated
        self.unconfirmed()
        self.assertEqual(self.keys, ["enter"])

    def test_claude_queue_waits_then_reconciles_original_without_more_input(self):
        self.configure("claude")
        def queue(key, count):
            self.append_native(kind="queued")
            self.view = self.compose("")
        self.on_key = queue
        self.unconfirmed()
        self.assertEqual(self.keys, ["enter"])
        self.unconfirmed(reconcile_only=True)
        self.unconfirmed(recover_stranded=True)
        before = (list(self.keys), list(self.pastes))
        path, old = self.original_attempt()
        self.append_native()
        result = self.deliver(reconcile_only=True)
        self.assertIs(result["confirmed"], True)
        self.assertIs(result["reconciled_read_only"], True)
        self.assertEqual((self.keys, self.pastes), before)
        self.assertEqual(result["attempt"], str(path))
        self.assertEqual(result["native_proof"]["reception_kind"], "native_user_message")
        self.assertIsNotNone(J.verified_receipt(B, PINS["target_surface_uuid"], PAYLOAD, MARKER))

    def test_codex_busy_queue_uses_tab_once_and_waits_for_native_user(self):
        self.pending = self.compose(PAYLOAD, status="• Working (3s • esc to interrupt)", hint=True)
        def queue(key, count):
            self.view = ("Messages to be submitted after next tool call\n" + PAYLOAD
                         + "\n" + self.compose(""))
        self.on_key = queue
        self.unconfirmed()
        self.assertEqual(self.keys, ["tab"])
        with self.assertRaises(B.TaskPackContractError):
            self.deliver(recover_stranded=True)
        self.append_native()
        result = self.deliver(reconcile_only=True)
        self.assertIs(result["confirmed"], True)
        self.assertEqual(self.keys, ["tab"])
        self.assertEqual(self.pastes, [PAYLOAD])

    def test_recovery_budget_is_shared_with_automatic_extra_enter(self):
        self.unconfirmed()
        self.assertEqual(self.keys, ["enter", "enter"])
        with self.assertRaises(B.TaskPackContractError) as caught:
            self.deliver(recover_stranded=True)
        self.assertIn("STRANDED_RECOVERY_ALREADY_USED", str(caught.exception))
        self.assertEqual(self.keys, ["enter", "enter"])
        self.assertEqual(self.pastes, [PAYLOAD])

    def test_same_original_explicit_recovery_pastes_only_once(self):
        def first_key(key, count):
            if count == 1:
                self.view = self.compose("")
            else:
                self.append_native()
                self.view = self.compose("")
        self.on_key = first_key
        self.unconfirmed()
        self.assertEqual(self.keys, ["enter"])
        path, before = self.original_attempt()
        self.view = self.pending
        result = self.deliver(recover_stranded=True)
        self.assertIs(result["confirmed"], True)
        self.assertEqual(result["attempt"], str(path))
        self.assertEqual(self.pastes, [PAYLOAD])
        self.assertEqual(self.keys, ["enter", "enter"])
        _, after = self.original_attempt()
        self.assertEqual(sum(e["phase"] == "PASTE_INTENT" for e in after["events"]), 1)
        self.assertEqual(sum(e["phase"] == "EXTRA_ENTER_INTENT" for e in after["events"]), 1)

    def test_existing_receipt_blocks_second_send(self):
        self.on_key = lambda key, count: self.append_native()
        self.assertIs(self.deliver()["confirmed"], True)
        with self.assertRaises(B.TaskPackContractError):
            self.deliver()
        self.assertEqual(self.pastes, [PAYLOAD])
        self.assertEqual(self.keys, ["enter"])

    def test_foreign_draft_is_preserved_without_key_or_paste(self):
        self.view = self.compose("User's unrelated unfinished draft")
        self.unconfirmed()
        self.assertEqual(self.keys, [])
        self.assertEqual(self.pastes, [])

    def test_target_process_change_after_paste_refuses_submit_key(self):
        def changed_process():
            self.proc = dict(self.proc, birth=[30, 40])
        self.on_paste = changed_process
        with self.assertRaises(N.NativeDeliveryError):
            self.deliver()
        self.assertEqual(self.keys, [])
        self.assertEqual(self.pastes, [PAYLOAD])
        self.assertFalse((self.journal() / "receipt.json").exists())

    def test_replaced_held_journal_lock_refuses_submit_key(self):
        def replace_lock():
            lock = self.journal() / "delivery.lock"
            previous = lock.stat().st_ino
            replacement = lock.with_suffix(".replacement")
            replacement.write_bytes(b"")
            replacement.replace(lock)
            self.assertNotEqual(previous, lock.stat().st_ino)
        self.on_paste = replace_lock
        self.on_key = lambda key, count: self.append_native()
        # 被替换的 lock pathname 不再代表已持有的 inode；不得写键或发布回执。
        try:
            result = self.deliver()
        except (B.TaskPackContractError, B.DispatchUnconfirmed, N.NativeDeliveryError):
            result = None
        self.assertEqual(self.keys, [], "held lock inode replaced but sender still submitted")
        self.assertFalse((self.journal() / "receipt.json").exists())

#!/usr/bin/env python3
"""原生投递 PostToolUse 的独立离线回归：真实定位、journal 与 JSONL 核收。

只替换 live 身份及外部进程/cmux 观测，不替换 _original、绑定验证或入站解析。
所有夹具均位于 TemporaryDirectory；绝不发送文本、按键、回调或启动客户端。
"""
from __future__ import annotations

from contextlib import ExitStack
import copy
import hashlib
import io
import json
from pathlib import Path
import shlex
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_native_delivery as native
import cmux_native_delivery_guard as guard
from native_test_support import LegacyTerminalFixture


def sha256(value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


class NativeDeliveryGuardTests(unittest.TestCase):
    def setUp(self):
        self.identity = dict(
            workspace_uuid="AAAAAAAA-1111-2222-3333-444444444444",
            caller_surface_uuid="BBBBBBBB-1111-2222-3333-444444444444",
            target_surface_uuid="CCCCCCCC-1111-2222-3333-444444444444",
            target_pane_uuid="DDDDDDDD-1111-2222-3333-444444444444",
        )
        self.hook_identity = (self.identity["workspace_uuid"],
                              self.identity["caller_surface_uuid"])
        self.fixtures = []
        self._use_provider("codex")
        self.counter = 0
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.resolve = patches.enter_context(patch.object(
            guard.cmux_hook_identity, "resolve",
            side_effect=lambda _: self.hook_identity))
        self.screen = patches.enter_context(patch.object(
            bridge, "read_screen",
            side_effect=AssertionError("guard must not read a live screen")))

    def tearDown(self):
        # 即使断言失败，也验证守卫从未产生终端输入。
        self.screen.assert_not_called()
        for fixture in self.fixtures:
            fixture.send.assert_not_called()
            fixture.key.assert_not_called()

    def _use_provider(self, provider):
        self.native = LegacyTerminalFixture.attach(
            self, identity=self.identity, provider=provider)
        self.fixtures.append(self.native)
        self.root = self.native.home / ".local/state/multi-agent-collaboration"

    def _command(self, case, *, verb=None, text=None, marker=None):
        if case.kind == "callback":
            argv = ["submit-completion-callback", "--task-pack",
                    str(case.pack_path)]
        else:
            argv = [verb or ("submit-task-pack" if case.kind == "task" else "submit-text"),
                    "--surface", case.target, "--text",
                    case.text if text is None else text,
                    "--marker", case.marker if marker is None else marker]
            if case.pack_path:
                argv += ["--task-pack", str(case.pack_path)]
        return shlex.join(["rtk", "proxy", "python3", "-B",
                           str(Path(bridge.__file__).resolve()), *argv])

    def _install_attempt(self, case, number=1):
        # 在任何模拟入站之前，通过真实原生绑定及 EOF fence 构造原始意图。
        binding = native.bind_target(bridge, case.target, case.text)
        fence = native.capture_paste_fence(binding)
        stamp = time.time()
        before = "› \nGPT-6 high"
        attempt = dict(
            binding=copy.deepcopy(case.bound),
            native_binding=binding,
            phase="ENTER_SENT",
            events=[
                dict(phase="PASTE_INTENT", at_epoch=stamp, screen=before,
                     screen_sha256=sha256(before)[:16], native_paste_fence=fence),
                dict(phase="ENTER_INTENT", at_epoch=stamp),
                dict(phase="ENTER_SENT", at_epoch=stamp),
            ],
        )
        case.attempt_path = case.journal / f"attempt-{number:04d}.json"
        write_json(case.attempt_path, attempt)
        case.attempt = attempt
        return case

    def _case(self, kind="text", *, received=False, text=None, marker=None):
        self.counter += 1
        task_id = f"guard-task-{self.counter}"
        marker = marker or f"GUARD_LITERAL_{self.counter}_20261009"
        target = "surface:701"
        text = text or f"STATUS: {marker}\n原始完整消息：同一个任务。"
        pack_path = report_path = pack = None
        if kind in ("task", "callback"):
            directory = self.native.home / "task packs" / task_id
            directory.mkdir(parents=True)
            pack_path = directory / "task pack.json"
            report_path = directory / "report.md"
            report_path.write_text("独立夹具：原始报告\n", encoding="utf-8")
            pack = dict(
                task_id=task_id,
                completion_callback=text,
                completion_nonce=marker,
                completion_receipt=str(directory / "completion receipt.json"),
                report=str(report_path),
                callback_target=target,
                executor_uuid=self.identity["caller_surface_uuid"],
            )
            if kind == "callback":
                controller = Path(bridge.__file__).resolve()
                pack.update(required_skill=str(controller.parent.parent / "SKILL.md"),
                            completion_command_argv=["rtk", "proxy", sys.executable,
                                "-B", str(controller), "submit-completion-callback",
                                "--task-pack", str(pack_path)])
            write_json(pack_path, pack)
        bound = dict(identity=copy.deepcopy(self.identity))
        if kind == "callback":
            bound.update(
                completion_callback=text,
                completion_nonce=marker,
                report_sha256=sha256(report_path.read_bytes()),
            )
            receipt = Path(pack["completion_receipt"])
            journal = receipt.with_name(receipt.stem + "-attempts")
        else:
            bound.update(marker=marker, payload_sha256=sha256(text))
            slot = [self.identity["caller_surface_uuid"],
                    task_id if kind == "task" else marker]
            journal = self.root / ("task-dispatch-v1" if kind == "task"
                                   else "message-dispatch-v1") / sha256(
                json.dumps(slot, sort_keys=kind == "text"))
        if pack_path:
            bound.update(task_id=task_id,
                         task_pack_sha256=sha256(pack_path.read_bytes()))
        journal.mkdir(parents=True)
        case = SimpleNamespace(kind=kind, text=text, marker=marker, target=target,
                               task_id=task_id, pack=pack, pack_path=pack_path,
                               report_path=report_path, journal=journal, bound=bound)
        self._install_attempt(case)
        case.command = self._command(case)
        case.payload = dict(tool_name="exec_command",
                            tool_input={"cmd": case.command})
        if received:
            self.native.append_user(text)
        return case

    def _evaluate(self, case, payload=None):
        return guard.evaluate(case.payload if payload is None else payload,
                              state_root=self.root)

    def _received(self, result, case):
        self.assertEqual(result["action"], "pass", result)
        self.assertEqual(len(result["results"]), 1, result)
        item = result["results"][0]
        self.assertEqual(item["state"], "NATIVE_RECEIVED")
        self.assertIs(item["confirmed"], True)
        self.assertEqual(item["attempt"], str(case.attempt_path))
        self.assertEqual(item["kind"], case.kind)
        self.assertEqual(item["marker"], case.marker)
        proof = item["native_proof"]
        self.assertEqual(proof["reception_kind"], "native_user_message")
        self.assertEqual(proof["session_id"], self.native.session_id)
        self.assertEqual(proof["path"], str(self.native.transcript))
        self.assertEqual(proof["payload_sha256"], sha256(case.text))
        raw = self.native.transcript.read_bytes()
        record = raw[proof["offset"]:proof["offset"] + proof["length"]]
        self.assertEqual(sha256(record), proof["sha256"])
        return item

    def _blocked(self, result, *, state=None, reason=None):
        self.assertEqual(result["action"], "block", result)
        self.assertEqual(len(result["results"]), 1, result)
        item = result["results"][0]
        self.assertNotEqual(item.get("state"), "NATIVE_RECEIVED", result)
        if state is not None:
            self.assertEqual(item["state"], state, result)
        if reason is not None:
            self.assertIn(reason, item.get("reason", ""), result)
        return item

    def _no_recovery(self, result):
        for item in result["results"]:
            self.assertFalse(item.get("recover"), result)
        self.assertNotIn("--recover-stranded", guard._render(result))

    def _snapshot(self):
        # 只盘点本测试的临时目录，核对调用前后的内容与 inode/mtime。
        return {
            str(path.relative_to(self.native.home)):
                (sha256(path.read_bytes()), path.stat().st_ino,
                 path.stat().st_mtime_ns)
            for path in self.native.home.rglob("*") if path.is_file()
        }


    def _reference_case(self, *, received=True):
        import cmux_prompt_reference as reference
        marker = f"GUARD_REFERENCE_{self.counter + 1}"
        full = f"STATUS: {marker}\r\n" + ("原始正文\t  \r\n" * 100)
        plan = reference.plan(full, marker)
        pin = reference.persist(plan["reference"], full)
        case = self._case(text=plan["text"], marker=marker)
        case.body_path = Path(plan["reference"]["path"])
        case.attempt.update(body_reference=plan["reference"], body_pin=pin)
        write_json(case.attempt_path, case.attempt)
        if received:
            self.native.append_user(case.text)
        return case

    def test_reference_hook_confirms_notice_only_and_preserves_body_pin(self):
        case = self._reference_case()
        item = self._received(self._evaluate(case), case)
        self.assertEqual(item["confirmation_scope"], "reference_notice")
        self.assertIs(item["body_read_confirmed"], False)
        self.assertEqual(item["body_pin"], case.attempt["body_pin"])

    def test_reference_damage_blocks_even_with_exact_native_notice(self):
        for damage in ("missing", "writable", "same_byte_replacement", "changed"):
            with self.subTest(damage=damage):
                case = self._reference_case()
                path = case.body_path
                if damage == "missing":
                    path.unlink()
                elif damage == "writable":
                    path.chmod(0o600)
                elif damage == "same_byte_replacement":
                    replacement = path.with_name("replacement.txt")
                    replacement.write_bytes(path.read_bytes())
                    replacement.chmod(0o400)
                    replacement.replace(path)
                else:
                    path.chmod(0o600)
                    path.write_bytes(b"changed")
                    path.chmod(0o400)
                self._blocked(self._evaluate(case))

    def test_reference_body_mutation_during_native_probe_blocks(self):
        case = self._reference_case()
        probe = native.probe
        def race(*args, **kwargs):
            result = probe(*args, **kwargs)
            case.body_path.chmod(0o600)
            return result
        with patch.object(native, "probe", side_effect=race):
            self._blocked(self._evaluate(case))

    def test_reference_missing_original_pin_cannot_be_reconstructed_by_hook(self):
        case = self._reference_case()
        case.attempt.pop("body_pin")
        write_json(case.attempt_path, case.attempt)
        self._blocked(self._evaluate(case), reason="MESSAGE_BODY_ORIGINAL_PIN_REQUIRED")

    def test_legacy_long_inline_reconciles_original_bytes_with_zero_input(self):
        original = "STATUS: LEGACY_REFERENCE_TEST\r\n" + ("旧原文  \t\r\n" * 200)
        case = self._case(text=original, marker="LEGACY_REFERENCE_TEST", received=True)
        before = case.attempt_path.read_bytes()
        result = bridge.submit_text(case.target, original, marker=case.marker, reconcile_only=True)
        self.assertTrue(result["confirmed"])
        self.assertTrue(result["reconciled_read_only"])
        self.assertEqual(case.attempt_path.read_bytes(), before)
        self.assertNotIn("body_reference", result)
        self.assertFalse((self.root / "message-bodies-v1").exists())
        self._received(self._evaluate(case), case)


    def test_ordinary_tools_skip_without_identity_or_history(self):
        payloads = [
            {},
            {"tool_name": "exec_command",
             "tool_input": {"cmd": "rtk rg --files ."}},
            {"tool_name": "read_file",
             "tool_input": {"path": "/offline/notes.md"}},
            {"tool_name": "exec_command",
             "tool_input": {"cmd": "rtk proxy python3 -B -c 'print(1)'"},
             "tool_response": {"cmd": "cmux submit-text --surface surface:701 "
                                      "--text FAKE --marker FAKE"}},
        ]
        with patch.object(guard.cmux_hook_identity, "evaluation") as identity, \
                patch.object(guard, "attempt_paths") as paths, \
                patch.object(guard, "_load") as load, \
                patch.object(native, "_open") as opened, \
                patch.object(Path, "rglob") as history, \
                patch.object(Path, "glob") as slot_scan, \
                patch.object(Path, "iterdir") as directory_scan:
            for payload in payloads:
                with self.subTest(payload=payload):
                    self.assertEqual(guard.evaluate(payload, state_root=self.root),
                                     {"action": "skip", "results": []})
            identity.assert_not_called()
            self.resolve.assert_not_called()
            paths.assert_not_called()
            load.assert_not_called()
            opened.assert_not_called()
            history.assert_not_called()
            slot_scan.assert_not_called()
            directory_scan.assert_not_called()

    def test_literal_bridge_help_skips_without_identity(self):
        for command in ("rtk proxy cmux_bridge.py --help",
                        "cmux_bridge.py submit-text --help"):
            with self.subTest(command=command):
                result = guard.evaluate({"tool_input": {"cmd": command}},
                                        state_root=self.root)
                self.assertEqual(result["action"], "skip")
        self.resolve.assert_not_called()

    def test_text_literal_receipt_uses_exact_original_slot(self):
        marker = "LITERAL_GUARD_PAYLOAD"
        text = ("STATUS: " + marker + "\n" +
                "literal --help; cmux submit-text --surface surface:999 "
                "--text 'quoted content'\n引文不是另一次发送。")
        case = self._case(text=text, marker=marker, received=True)
        before = self._snapshot()
        self._received(self._evaluate(case), case)
        self.assertEqual(self._snapshot(), before)

    def test_task_literal_receipt_uses_task_id_slot(self):
        case = self._case("task", received=True)
        self._received(self._evaluate(case), case)

    def test_submit_text_with_pack_resolves_original_task_slot(self):
        case = self._case("task", received=True)
        payload = {"tool_input": {"cmd": self._command(case, verb="submit-text")}}
        self._received(self._evaluate(case, payload), case)

    def test_callback_literal_receipt_uses_pack_receipt_slot(self):
        self._use_provider("claude")
        case = self._case("callback", received=True)
        self._received(self._evaluate(case), case)

    def _callback_receiver(self, case):
        """原主管观察自己收到的 callback；self-target 仍被 live guard 拒绝。"""
        pins = case.bound["identity"]
        reverse = dict(workspace_uuid=pins["workspace_uuid"],
                       caller_surface_uuid=pins["target_surface_uuid"],
                       target_surface_uuid=pins["caller_surface_uuid"],
                       caller_pane_uuid=pins["target_pane_uuid"],
                       target_pane_uuid="EEEEEEEE-1111-2222-3333-444444444444")
        self.hook_identity = (pins["workspace_uuid"], pins["target_surface_uuid"])
        def pin(surface):
            if surface == pins["caller_surface_uuid"]:
                return copy.deepcopy(reverse)
            raise RuntimeError("executor must be another terminal side pane")
        self.native.pin.side_effect = pin
        return reverse

    def test_callback_receiver_literal_reconcile_confirms_original_without_input(self):
        case = self._case("callback", received=True)
        self._callback_receiver(case)
        before = self._snapshot()
        payload = {"tool_input": {"cmd": case.command + " --reconcile-only"}}
        self._received(self._evaluate(case, payload), case)
        self.assertEqual(self._snapshot(), before)

    def test_callback_receiver_pending_reconcile_remains_unconfirmed(self):
        case = self._case("callback")
        self._callback_receiver(case)
        payload = {"tool_input": {"cmd": case.command + " --reconcile-only"}}
        self._blocked(self._evaluate(case, payload), state="NATIVE_PENDING")

    def test_callback_receiver_python_literal_reconcile_confirms_without_input(self):
        case = self._case("callback", received=True)
        self._callback_receiver(case)
        source = (f"bridge.submit_completion_callback({str(case.pack_path)!r}, "
                  "confirm_lines=200, reconcile_only=True, resume_queue_only=False)")
        before = self._snapshot()
        payload = {"tool_input": {"cmd": shlex.join([sys.executable, "-B", "-c", source])}}
        self._received(self._evaluate(case, payload), case)
        self.assertEqual(self._snapshot(), before)

    def test_callback_receiver_python_cannot_infer_read_only_from_truthy_or_dynamic_modes(self):
        case = self._case("callback", received=True)
        self._callback_receiver(case)
        for suffix in ("reconcile_only=False", "reconcile_only='True'", "reconcile_only=1",
                       "reconcile_only=mode", "**{'reconcile_only': True}",
                       "reconcile_only=True, **options", "reconcile_only=True, confirm_lines=limit",
                       "reconcile_only=True, resume_queue_only=True",
                       "reconcile_only=True, reconcile_only=False"):
            with self.subTest(suffix=suffix):
                source = f"bridge.submit_completion_callback({str(case.pack_path)!r}, {suffix})"
                payload = {"tool_input": {"cmd": shlex.join([sys.executable, "-c", source])}}
                self._blocked(self._evaluate(case, payload))

    def test_callback_receiver_cannot_claim_a_send_as_read_only(self):
        case = self._case("callback", received=True)
        self._callback_receiver(case)
        self._blocked(self._evaluate(case))
        for suffix in (" --reconcile-only=false", " --reconcile-only --resume-queue-only"):
            with self.subTest(suffix=suffix):
                self._blocked(self._evaluate(case, {
                    "tool_input": {"cmd": case.command + suffix}}))

    def test_callback_receiver_wrong_workspace_or_pane_remains_blocked(self):
        case = self._case("callback", received=True)
        reverse = self._callback_receiver(case)
        payload = {"tool_input": {"cmd": case.command + " --reconcile-only"}}
        for key in ("workspace_uuid", "caller_surface_uuid", "target_surface_uuid", "caller_pane_uuid"):
            with self.subTest(key=key):
                previous = reverse[key]
                try:
                    reverse[key] = "FFFFFFFF-1111-2222-3333-444444444444"
                    self._blocked(self._evaluate(case, payload))
                finally:
                    reverse[key] = previous

    def test_callback_receiver_cannot_borrow_another_executor(self):
        case = self._case("callback", received=True)
        case.pack["executor_uuid"] = "FFFFFFFF-1111-2222-3333-444444444444"
        write_json(case.pack_path, case.pack)
        case.attempt["binding"]["task_pack_sha256"] = sha256(case.pack_path.read_bytes())
        write_json(case.attempt_path, case.attempt)
        self._callback_receiver(case)
        self._blocked(self._evaluate(case, {
            "tool_input": {"cmd": case.command + " --reconcile-only"}}))

    def test_callback_receiver_hook_identity_must_match_authenticated_reverse(self):
        case = self._case("callback", received=True)
        self._callback_receiver(case)
        self.hook_identity = (self.identity["workspace_uuid"],
                              "FFFFFFFF-1111-2222-3333-444444444444")
        self._blocked(self._evaluate(case, {
            "tool_input": {"cmd": case.command + " --reconcile-only"}}),
            reason="hook identity differs")

    def test_callback_native_identity_cannot_differ_from_original_outer_binding(self):
        case = self._case("callback", received=True)
        case.attempt["binding"]["identity"]["target_pane_uuid"] = "different-pane"
        write_json(case.attempt_path, case.attempt)
        self._callback_receiver(case)
        self._blocked(self._evaluate(case, {
            "tool_input": {"cmd": case.command + " --reconcile-only"}}),
            reason="NATIVE_JOURNAL_IDENTITY_MISMATCH")

    def test_callback_receiver_changed_process_and_missing_fence_remain_blocked(self):
        for damage in ("process", "fence"):
            with self.subTest(damage=damage):
                case = self._case("callback", received=True)
                self._callback_receiver(case)
                old_birth = self.native.process_state["birth"]
                if damage == "process":
                    self.native.process_state["birth"] = "different-receiver-process"
                else:
                    case.attempt["events"][0].pop("native_paste_fence")
                    write_json(case.attempt_path, case.attempt)
                try:
                    self._blocked(self._evaluate(case, {
                        "tool_input": {"cmd": case.command + " --reconcile-only"}}))
                finally:
                    self.native.process_state["birth"] = old_birth
                    self.native.pin.side_effect = lambda *_: copy.deepcopy(self.identity)

    def test_literal_python_api_inputs_resolve_all_three_kinds(self):
        for kind in ("text", "task", "callback"):
            with self.subTest(kind=kind):
                case = self._case(kind, received=True)
                if kind == "callback":
                    source = f"bridge.submit_completion_callback({str(case.pack_path)!r})"
                elif kind == "task":
                    source = (f"bridge.submit_task_pack({case.target!r}, {case.text!r}, "
                              f"task_pack_path={str(case.pack_path)!r}, marker={case.marker!r})")
                else:
                    source = (f"bridge.submit_text({case.target!r}, {case.text!r}, "
                              f"marker={case.marker!r})")
                payload = {"tool_input": {"cmd": shlex.join(
                    ["rtk", "proxy", "python3", "-B", "-c", source])}}
                self._received(self._evaluate(case, payload), case)

    def test_literal_codex_exec_wrapper_preserves_exact_payload(self):
        case = self._case(received=True)
        source = "text(await tools.exec_command(" + json.dumps(
            {"cmd": case.command}, ensure_ascii=False) + "));"
        self._received(self._evaluate(case, {"tool_name": "functions.exec",
                                             "tool_input": source}), case)

    def test_locator_never_scans_other_slots_or_history(self):
        case = self._case("task", received=True)
        other = self.root / "message-dispatch-v1" / "unrelated-slot"
        other.mkdir(parents=True)
        (other / "attempt-0001.json").write_text("invalid decoy journal")
        real_glob = Path.glob

        def only_current(path, pattern, *args, **kwargs):
            self.assertEqual(path, case.journal)
            self.assertEqual(pattern, "attempt-*.json")
            return real_glob(path, pattern, *args, **kwargs)

        with patch.object(Path, "glob", autospec=True, side_effect=only_current) as glob, \
                patch.object(Path, "rglob",
                             side_effect=AssertionError("no history sweep")):
            self._received(self._evaluate(case), case)
            # 同一槽位可在核收结束时再次复核列表；禁止的是跨槽位扫描。
            self.assertGreaterEqual(glob.call_count, 1)

    def test_missing_current_slot_cannot_borrow_other_receipt(self):
        case = self._case(received=True)
        changed_marker = case.marker + "_OTHER"
        command = self._command(case, text="STATUS: " + changed_marker,
                                marker=changed_marker)
        with patch.object(guard, "_load", wraps=guard._load) as opened:
            result = self._evaluate(case, {"tool_input": {"cmd": command}})
            self._blocked(result, reason="no original attempt journal")
            opened.assert_not_called()

    def test_latest_original_attempt_cannot_borrow_earlier_receipt(self):
        case = self._case(received=True)
        # 第二个真实 EOF fence 位于旧入站之后；不可退回第一份成功证据。
        self._install_attempt(case, number=2)
        item = self._blocked(self._evaluate(case), state="NATIVE_PENDING")
        self.assertEqual(item["attempt"], str(case.attempt_path))
        self.assertFalse(item.get("native_proof"))

    def test_hook_uuid_case_preserves_bridge_slot_spelling(self):
        case = self._case(received=True)
        self.hook_identity = tuple(value.lower() for value in self.hook_identity)
        self._received(self._evaluate(case), case)

    def test_unresolved_caller_blocks_before_journal_scan(self):
        case = self._case(received=True)
        self.hook_identity = ("default", None)
        with patch.object(guard, "attempt_paths") as paths:
            self._blocked(self._evaluate(case), reason="caller resolution")
            paths.assert_not_called()

    def test_different_live_caller_cannot_claim_original(self):
        case = self._case(received=True)
        self.hook_identity = (self.identity["workspace_uuid"],
                              "EEEEEEEE-1111-2222-3333-444444444444")
        with patch.object(guard, "attempt_paths") as paths:
            self._blocked(self._evaluate(case), reason="hook identity differs")
            paths.assert_not_called()

    def test_different_journal_caller_is_rejected(self):
        case = self._case(received=True)
        case.attempt["binding"]["identity"]["caller_surface_uuid"] = (
            "EEEEEEEE-1111-2222-3333-444444444444")
        write_json(case.attempt_path, case.attempt)
        self._blocked(self._evaluate(case), reason="different live caller/workspace")

    def test_different_journal_workspace_is_rejected(self):
        case = self._case(received=True)
        case.attempt["binding"]["identity"]["workspace_uuid"] = (
            "EEEEEEEE-1111-2222-3333-444444444444")
        write_json(case.attempt_path, case.attempt)
        self._blocked(self._evaluate(case), reason="different live caller/workspace")

    def test_target_surface_and_pane_drift_are_rejected(self):
        case = self._case(received=True)
        for key in ("target_surface_uuid", "target_pane_uuid"):
            with self.subTest(key=key):
                old = self.identity[key]
                try:
                    self.identity[key] = "EEEEEEEE-1111-2222-3333-444444444444"
                    self._blocked(self._evaluate(case),
                                  reason="current receiver differs")
                finally:
                    self.identity[key] = old

    def test_native_process_birth_drift_is_rejected(self):
        case = self._case(received=True)
        self.native.process_state["birth"] = "different-process-birth"
        self._blocked(self._evaluate(case), reason="NATIVE_PROCESS_BIRTH")

    def test_target_drift_after_probe_is_rejected(self):
        case = self._case(received=True)
        real_probe = native.probe

        def change_target(*args, **kwargs):
            result = real_probe(*args, **kwargs)
            self.identity["target_surface_uuid"] = "EEEEEEEE-1111-2222-3333-444444444444"
            return result

        with patch.object(native, "probe", side_effect=change_target) as probe:
            self._blocked(self._evaluate(case), state="NATIVE_UNVERIFIABLE")
            probe.assert_called_once()

    def test_changed_literal_payload_is_rejected(self):
        case = self._case(received=True)
        payload = {"tool_input": {"cmd": self._command(case, text=case.text + " changed")}}
        self._blocked(self._evaluate(case, payload), reason="payload/marker differs")

    def test_dynamic_or_missing_payload_marker_cannot_claim_delivery(self):
        commands = [
            "bridge.submit_text('surface:701', dynamic_text, marker='MARKER')",
            "cmux_bridge.py submit-text --surface surface:701 --text 'STATUS: text'",
            "cmux send-key --surface surface:701 --text Enter",
        ]
        with patch.object(guard, "attempt_paths") as paths:
            for command in commands:
                with self.subTest(command=command):
                    self._blocked(guard.evaluate({"tool_input": {"cmd": command}},
                                                 state_root=self.root))
            paths.assert_not_called()

    def test_stored_confirmed_flag_without_native_receipt_blocks(self):
        case = self._case()
        case.attempt.update(phase="CONFIRMED", confirmed=True,
                            native_proof={"state": "NATIVE_RECEIVED", "confirmed": True})
        write_json(case.attempt_path, case.attempt)
        write_json(case.journal / "receipt.json",
                   {"confirmed": True, "confirmation_source": "screen", "attempt": str(case.attempt_path)})
        self._blocked(self._evaluate(case), state="NATIVE_PENDING")

    def test_missing_original_native_binding_is_not_backfilled(self):
        case = self._case(received=True)
        case.attempt.pop("native_binding")
        write_json(case.attempt_path, case.attempt)
        before = self._snapshot()
        self._blocked(self._evaluate(case), reason="ORIGINAL_NATIVE_BINDING_REQUIRED")
        self.assertEqual(self._snapshot(), before)

    def test_queued_native_record_blocks_without_recovery_keys(self):
        self._use_provider("claude")
        case = self._case()
        self.native.append_queued(case.text)
        result = self._evaluate(case)
        item = self._blocked(result, state="NATIVE_QUEUED")
        self.assertEqual(item["queued_proof"]["reception_kind"], "native_queued_command")
        self.assertFalse(item.get("native_proof"))
        self._no_recovery(result)

    def test_queued_then_complete_native_user_record_passes(self):
        self._use_provider("claude")
        case = self._case()
        self.native.append_queued(case.text)
        self.native.append_user(case.text)
        self._received(self._evaluate(case), case)

    def test_marker_only_native_message_does_not_confirm_full_text(self):
        case = self._case()
        self.native.append_user(case.marker)
        self._blocked(self._evaluate(case), state="NATIVE_PENDING")

    def test_tool_response_cannot_supply_native_proof(self):
        case = self._case()
        payload = copy.deepcopy(case.payload)
        payload["tool_response"] = {"confirmed": True, "state": "NATIVE_RECEIVED",
                                    "native_proof": {"text": case.text}}
        self._blocked(self._evaluate(case, payload), state="NATIVE_PENDING")

    def test_mixed_current_calls_block_until_each_has_native_receipt(self):
        first = self._case(received=True)
        second = self._case("task")
        payload = {"tool_uses": [first.payload, second.payload]}
        result = guard.evaluate(payload, state_root=self.root)
        self.assertEqual(result["action"], "block")
        self.assertEqual([item["state"] for item in result["results"]],
                         ["NATIVE_RECEIVED", "NATIVE_PENDING"])
        self.native.append_user(second.text)
        result = guard.evaluate(payload, state_root=self.root)
        self.assertEqual(result["action"], "pass")
        self.assertEqual(len(result["results"]), 2)
        self.assertTrue(all(item.get("native_proof") for item in result["results"]))

    def test_task_and_callback_pack_drift_are_rejected(self):
        for kind in ("task", "callback"):
            with self.subTest(kind=kind):
                case = self._case(kind, received=True)
                case.pack["unrelated_field"] = "also changes exact pack bytes"
                write_json(case.pack_path, case.pack)
                self._blocked(self._evaluate(case), reason="task pack differs")

    def test_callback_report_drift_is_rejected(self):
        case = self._case("callback", received=True)
        case.report_path.write_text("不同报告\n", encoding="utf-8")
        self._blocked(self._evaluate(case), reason="callback/report differs")

    def test_pack_report_and_attempt_race_after_probe_are_rejected(self):
        for artifact in ("pack_path", "report_path", "attempt_path", "new_attempt"):
            with self.subTest(artifact=artifact):
                case = self._case("callback", received=True)
                target = (case.journal / "attempt-0002.json" if artifact == "new_attempt"
                          else getattr(case, artifact))
                real_probe = native.probe

                def changed(*args, **kwargs):
                    result = real_probe(*args, **kwargs)
                    if artifact == "new_attempt":
                        target.write_text("{}\n", encoding="utf-8")
                    else:
                        with target.open("ab") as output:
                            output.write(b"\n ")
                    return result

                with patch.object(native, "probe", side_effect=changed) as probe:
                    self._blocked(self._evaluate(case),
                                  reason="original evidence changed")
                    probe.assert_called_once()

    def test_no_input_without_paste_intent_never_suggests_recovery(self):
        for kind in ("text", "task", "callback"):
            with self.subTest(kind=kind):
                case = self._case(kind)
                case.attempt.update(phase="NO_INPUT", events=[])
                write_json(case.attempt_path, case.attempt)
                with patch.object(native, "probe", wraps=native.probe) as probe:
                    result = self._evaluate(case)
                    self._blocked(result)
                    self._no_recovery(result)
                    probe.assert_not_called()

    def test_no_input_with_stale_events_never_suggests_recovery(self):
        case = self._case()
        case.attempt["phase"] = "NO_INPUT"
        write_json(case.attempt_path, case.attempt)
        result = self._evaluate(case)
        self._blocked(result)
        self._no_recovery(result)

    def test_text_pending_can_suggest_only_original_controller_recovery(self):
        case = self._case()
        result = self._evaluate(case)
        item = self._blocked(result, state="NATIVE_PENDING")
        for mode in ("reconcile", "recover"):
            argv = shlex.split(item[mode])
            self.assertEqual(argv[:2], ["rtk", "proxy"])
            self.assertEqual(argv[4], str(Path(bridge.__file__).resolve()))
            self.assertEqual(argv[5], "submit-text")
            self.assertEqual(argv[argv.index("--surface") + 1], case.target)
            self.assertEqual(argv[argv.index("--text") + 1], case.text)
            self.assertEqual(argv[argv.index("--marker") + 1], case.marker)
        self.assertIn("--reconcile-only", shlex.split(item["reconcile"]))
        self.assertIn("--recover-stranded", shlex.split(item["recover"]))
        self.assertIn(item["recover"], guard._render(result))

    def test_task_and_callback_only_suggest_supported_read_only_command(self):
        for kind in ("task", "callback"):
            with self.subTest(kind=kind):
                case = self._case(kind)
                result = self._evaluate(case)
                item = self._blocked(result, state="NATIVE_PENDING")
                self._no_recovery(result)
                argv = shlex.split(item["reconcile"])
                self.assertEqual(argv[5], "submit-task-pack" if kind == "task"
                                 else "submit-completion-callback")
                self.assertIn("--reconcile-only", argv)
                self.assertEqual(argv[argv.index("--task-pack") + 1], str(case.pack_path))

    def _repin_callback_pack(self, case):
        write_json(case.pack_path, case.pack)
        case.attempt["binding"]["task_pack_sha256"] = sha256(case.pack_path.read_bytes())
        write_json(case.attempt_path, case.attempt)

    def test_callback_hint_preserves_frozen_original_release_and_python(self):
        case = self._case("callback")
        old = self.native.home / "original release/source"
        (old / "scripts").mkdir(parents=True)
        (old / "SKILL.md").write_text("offline original release fixture")
        controller = old / "scripts/cmux_bridge.py"
        controller.write_text("# non-executable offline controller fixture\n")
        case.pack["required_skill"] = str(old / "SKILL.md")
        case.pack["completion_command_argv"][4] = str(controller)
        case.pack["callback_command"] = shlex.join(case.pack["completion_command_argv"][2:])
        self._repin_callback_pack(case)
        before = self._snapshot()
        item = self._blocked(self._evaluate(case), state="NATIVE_PENDING")
        self.assertEqual(shlex.split(item["reconcile"]),
                         case.pack["completion_command_argv"] + ["--reconcile-only"])
        self.assertNotIn(str(Path(bridge.__file__).resolve()), item["reconcile"])
        self.assertEqual(self._snapshot(), before)

    def test_callback_missing_or_conflicting_original_argv_never_suggests_current_release(self):
        for damage in ("missing", "wrong_controller", "wrong_pack", "conflicting", "shell_tail"):
            with self.subTest(damage=damage):
                case = self._case("callback")
                argv = case.pack["completion_command_argv"]
                if damage == "missing":
                    case.pack.pop("completion_command_argv")
                elif damage == "wrong_controller":
                    argv[4] = "/unrelated/source/scripts/cmux_bridge.py"
                elif damage == "wrong_pack":
                    argv[-1] = "/different/task-pack.json"
                elif damage == "conflicting":
                    case.pack["callback_command"] = shlex.join(argv + ["--reconcile-only"])
                else:
                    argv += [";", "echo", "wrong"]
                self._repin_callback_pack(case)
                item = self._blocked(self._evaluate(case), state="NATIVE_PENDING")
                self.assertNotIn("reconcile", item)
                self.assertTrue(item["reconcile_unavailable"])
                self._no_recovery({"results": [item]})

    def test_callback_missing_command_metadata_does_not_erase_native_reception(self):
        case = self._case("callback", received=True)
        case.pack.pop("completion_command_argv")
        self._repin_callback_pack(case)
        self._callback_receiver(case)
        item = self._received(self._evaluate(case, {
            "tool_input": {"cmd": case.command + " --reconcile-only"}}), case)
        self.assertNotIn("reconcile", item)
        self.assertTrue(item["reconcile_unavailable"])

    def test_missing_enter_sent_never_suggests_recovery(self):
        case = self._case()
        case.attempt["events"] = case.attempt["events"][:1]
        case.attempt["phase"] = "PASTE_INTENT"
        write_json(case.attempt_path, case.attempt)
        result = self._evaluate(case)
        self._blocked(result)
        self._no_recovery(result)

    def test_consumed_recovery_key_budget_never_suggests_another(self):
        for phase in ("QUEUE_TAB_INTENT", "EXTRA_ENTER_INTENT"):
            with self.subTest(phase=phase):
                case = self._case()
                case.attempt["events"].append(dict(phase=phase, at_epoch=time.time()))
                case.attempt["phase"] = phase
                write_json(case.attempt_path, case.attempt)
                result = self._evaluate(case)
                self._blocked(result)
                self._no_recovery(result)

    def test_incomplete_native_record_never_suggests_recovery(self):
        case = self._case()
        with self.native.transcript.open("ab") as output:
            output.write(b'{"type": "response_item", "payload": ')
        result = self._evaluate(case)
        self._blocked(result, state="NATIVE_RECORD_INCOMPLETE")
        self._no_recovery(result)

    def test_duplicate_paste_intent_never_suggests_recovery(self):
        case = self._case()
        case.attempt["events"].append(copy.deepcopy(case.attempt["events"][0]))
        write_json(case.attempt_path, case.attempt)
        result = self._evaluate(case)
        self._blocked(result)
        self._no_recovery(result)

    def test_main_success_emits_real_proof_and_exit_zero(self):
        case = self._case(received=True)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(case.payload))), \
                patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            status = guard.main()
        self.assertEqual(status, 0)
        self.assertEqual(stderr.getvalue(), "")
        output = json.loads(stdout.getvalue())
        self._received(self._posttooluse_result(output), case)

    def _posttooluse_result(self, output):
        # Frozen upstream schema, not the guard's own output definitions. These
        # assertions cover every field emitted on our successful wire path.
        schema_path = (Path(__file__).resolve().parent.parent / 'tests/fixtures'
                       / 'codex-post-tool-use-output.schema.json')
        schema = json.loads(schema_path.read_text(encoding='utf-8'))
        self.assertIs(schema['additionalProperties'], False)
        self.assertEqual(set(output) - set(schema['properties']), set())
        self.assertEqual(set(output), {'hookSpecificOutput'})
        specific = output['hookSpecificOutput']
        spec_schema = schema['definitions']['PostToolUseHookSpecificOutputWire']
        self.assertIs(spec_schema['additionalProperties'], False)
        self.assertEqual(set(specific) - set(spec_schema['properties']), set())
        self.assertTrue(set(spec_schema['required']).issubset(specific))
        self.assertEqual(specific['hookEventName'],
                         spec_schema['properties']['hookEventName']['const'])
        self.assertIsInstance(specific['additionalContext'], str)
        return json.loads(specific['additionalContext'])

    def test_main_callback_success_preserves_native_proof_for_both_providers(self):
        for provider in ('codex', 'claude'):
            with self.subTest(provider=provider):
                self._use_provider(provider)
                case = self._case('callback', received=True)
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(sys, 'stdin', io.StringIO(json.dumps(case.payload))), \
                        patch.object(sys, 'stdout', stdout), patch.object(sys, 'stderr', stderr):
                    status = guard.main()
                self.assertEqual(status, 0)
                self.assertEqual(stderr.getvalue(), '')
                self._received(self._posttooluse_result(json.loads(stdout.getvalue())), case)

    def test_legacy_success_result_is_rejected_by_official_wire_schema(self):
        case = self._case(received=True)
        legacy = self._evaluate(case)
        self._received(legacy, case)
        with self.assertRaises(AssertionError):
            self._posttooluse_result(legacy)

    def test_main_unrelated_tool_emits_no_hook_output(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, 'stdin', io.StringIO(json.dumps({
                'tool_name': 'exec_command', 'tool_input': {'cmd': 'rtk git status'}}))), \
                patch.object(sys, 'stdout', stdout), patch.object(sys, 'stderr', stderr):
            status = guard.main()
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')

    def test_main_unconfirmed_exits_two_with_original_reconciliation(self):
        case = self._case("callback")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(case.payload))), \
                patch.object(sys, "stdout", stdout), patch.object(sys, "stderr", stderr):
            status = guard.main()
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("--reconcile-only", stderr.getvalue())
        self.assertNotIn("--recover-stranded", stderr.getvalue())
        self.assertIn(str(case.pack_path), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

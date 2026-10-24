#!/usr/bin/env python3
"""独立 native 验收：真实临时 JSONL、绑定、EOF fence、probe 和 proof 校验。

外部替身仅覆盖临时 home 定位、墙钟、kernel process/ps 和 cmux I/O。
不导入旧测试夹具，不替换被测模块的绑定、文件读取、探测或校验函数。
临时文件放在本测试目录内；所有终端输入和屏幕操作均为调用即失败。
"""
import copy
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import cmux_native_delivery as native


WORKSPACE = "11111111-1111-4111-8111-111111111111"
CALLER = "22222222-2222-4222-8222-222222222222"
TARGET = "33333333-3333-4333-8333-333333333333"
PANE = "44444444-4444-4444-8444-444444444444"
CALLER_PANE = "55555555-5555-4555-8555-555555555555"
SESSION = "66666666-6666-4666-8666-666666666666"
OTHER_SESSION = "77777777-7777-4777-8777-777777777777"
PAYLOAD = "DONE: 独立验收\n保留完整消息与换行\nnonce=native-independent-01"
EPOCH = 1700000000
PID = 654321
TTY = "ttys987"


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def jsonl(record):
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def stamp(epoch, precision="microseconds"):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec=precision).replace("+00:00", "Z")


class OfflineBridge:
    """cmux 的外部 I/O 替身；任何发送、按键、屏幕读取都使测试失败。"""

    class DispatchUnconfirmed(RuntimeError):
        pass

    def __init__(self, case):
        self.case = case

    def pin_workspace(self, surface):
        case = self.case
        expected = CALLER if case.receiver else TARGET
        if surface != expected:
            raise RuntimeError("test fixture: unexpected target")
        if case.receiver:
            return dict(workspace_uuid=WORKSPACE, caller_surface_uuid=TARGET,
                        target_surface_uuid=CALLER,
                        caller_pane_uuid=case.observer_pane,
                        target_pane_uuid=CALLER_PANE)
        return dict(workspace_uuid=WORKSPACE, caller_surface_uuid=CALLER,
                    target_surface_uuid=TARGET, target_pane_uuid=PANE,
                    caller_pane_uuid=CALLER_PANE)

    def _run(self, *args):
        if args != ("tree", "--all", "--json", "--id-format", "both"):
            return self._forbidden(*args)
        return json.dumps({"windows": [{"workspaces": [{
            "id": WORKSPACE, "panes": [{"id": PANE, "surfaces": [{
                "id": TARGET, "type": "terminal", "tty": TTY,
                "dock_scope": "workspace"
            }]}]
        }]}]})

    def _forbidden(self, *args, **kwargs):
        self.case.input_operations.append((args, kwargs))
        raise AssertionError("offline acceptance must perform zero terminal I/O")

    send = send_text = send_key = key = paste = read_screen = _forbidden


class NativeCase:
    """每个场景自建独立 home；所有原生日志和注册文件均真实落盘。"""

    def __init__(self, provider="claude", *, registered=True, node=False):
        self.provider = provider
        self.registered = registered
        self.node = node
        self.stack = ExitStack()

    def __enter__(self):
        try:
            temporary = self.stack.enter_context(tempfile.TemporaryDirectory(
                prefix="native-independent-", dir=Path(__file__).resolve().parent))
            self.root = Path(temporary).resolve()
            self.home = self.root / "home"
            self.home.mkdir()
            self.now = float(EPOCH)
            self.receiver = False
            self.observer_pane = PANE
            self.input_operations = []
            self.process_calls = []
            self.ps_calls = []
            executable = ("/opt/homebrew/bin/node" if self.node else
                          "/test/bin/" + self.provider)
            argv = [executable]
            if self.node:
                argv.append("/opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js")
            argv.extend(["--resume", SESSION])
            self.process = dict(
                pid=PID, ppid=1, birth=[EPOCH - 120, 654321],
                executable=executable, argv=argv,
                file_identity=[1, 90210, 1024, 1700000000000000000],
                env={"CMUX_SURFACE_ID": TARGET, "CMUX_WORKSPACE_ID": WORKSPACE})
            self.transcript = self.create_transcript(SESSION)
            self.registration = self.home / ".claude/sessions" / (str(PID) + ".json")
            if self.provider == "claude" and self.registered:
                self.write_registration()
            self.bridge = OfflineBridge(self)
            self.stack.enter_context(mock.patch.object(Path, "home", return_value=self.home))
            self.stack.enter_context(mock.patch.object(native.time, "time",
                                                       side_effect=lambda: self.now))
            self.stack.enter_context(mock.patch.object(native.identity, "process",
                                                       side_effect=self._process))
            self.stack.enter_context(mock.patch.object(native.subprocess, "run",
                                                       side_effect=self._ps))
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None and self.input_operations:
                raise AssertionError("unexpected terminal I/O")
        finally:
            self.stack.close()

    def _process(self, pid, **kwargs):
        if pid != PID:
            raise AssertionError("unexpected real process request")
        self.process_calls.append((pid, kwargs))
        return copy.deepcopy(self.process)

    def _ps(self, args, **kwargs):
        self.ps_calls.append(list(args))
        if args == ["/bin/ps", "-ww", "-U", str(os.getuid()), "-o", "pid=,comm="]:
            output = str(PID) + " " + self.process["executable"] + "\n"
        elif args == ["/bin/ps", "-p", str(PID), "-o", "tty="]:
            output = TTY + "\n"
        else:
            raise AssertionError("unexpected external command: " + repr(args))
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    def create_transcript(self, session):
        if self.provider == "codex":
            path = self.home / ".codex/sessions/2026/10/09" / (
                "rollout-2026-10-09T00-00-00-" + session + ".jsonl")
            metadata = {"type": "session_meta", "payload": {"id": session}}
        else:
            path = self.home / ".claude/projects/-offline-independent" / (session + ".jsonl")
            metadata = {"type": "system", "sessionId": session}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(jsonl(metadata))
        return path

    def write_registration(self, *, session=SESSION, local=False, **changes):
        clock = time.localtime if local else time.gmtime
        record = dict(pid=PID, kind="interactive", sessionId=session,
                      procStart=time.strftime("%a %b %e %H:%M:%S %Y",
                                              clock(self.process["birth"][0])))
        record.update(changes)
        self.registration.parent.mkdir(parents=True, exist_ok=True)
        self.registration.write_text(json.dumps(record), encoding="utf-8")

    def bind(self):
        return native.bind_target(self.bridge, TARGET, PAYLOAD)

    def fence(self, binding):
        self.now = EPOCH + 1.1231
        fence = native.capture_paste_fence(binding)
        intent = EPOCH + 1.123999
        self.now = EPOCH + 2.0
        return fence, intent

    def ready(self):
        binding = self.bind()
        fence, intent = self.fence(binding)
        return binding, fence, intent

    def user_record(self, text=PAYLOAD, *, timestamp=None, session=SESSION):
        timestamp = timestamp or stamp(EPOCH + 1.5)
        if self.provider == "codex":
            return {"timestamp": timestamp, "type": "response_item", "payload": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}]}}
        return {"timestamp": timestamp, "type": "user", "sessionId": session,
                "message": {"role": "user", "content": text}}

    def queued_record(self):
        return {"timestamp": stamp(EPOCH + 1.5), "type": "attachment",
                "sessionId": SESSION,
                "attachment": {"type": "queued_command", "prompt": PAYLOAD}}

    def append(self, record=None, *, raw=None):
        raw = jsonl(record) if raw is None else raw
        offset = self.transcript.stat().st_size
        with self.transcript.open("ab") as handle:
            handle.write(raw)
        return offset, raw

    def raw_proof(self, binding, offset, raw, kind="native_user_message"):
        # 独立构造伪证用于拒绝场景；其字节哈希来自真实文件内容。
        pin = binding["transcript"]
        return dict(schema="native-delivery-v1", session_id=SESSION,
                    path=str(self.transcript), device=pin["device"], inode=pin["inode"],
                    offset=offset, length=len(raw), sha256=sha256(raw),
                    reception_kind=kind, payload_sha256=sha256(PAYLOAD.encode()))

    def snapshot(self):
        result = {}
        for path in self.root.rglob("*"):
            key = str(path.relative_to(self.root))
            result[key] = ("link", os.readlink(path)) if path.is_symlink() else (
                ("directory",) if path.is_dir() else ("file", sha256(path.read_bytes())))
        return result


class NativeFileAcceptance(unittest.TestCase):
    def test_generated_summary_is_not_receipt_or_valid_old_proof(self):
        for flags in ({'isCompactSummary': True}, {'isVisibleInTranscriptOnly': True},
                      {'isCompactSummary': True, 'isVisibleInTranscriptOnly': True}):
            with self.subTest(flags=flags), NativeCase() as case:
                binding, fence, intent = case.ready()
                offset, raw = case.append(dict(case.user_record(), **flags))
                result = native.probe(binding, PAYLOAD, paste_fence=fence, not_before=intent)
                self.assertFalse(result['confirmed'])
                self.assertFalse(native.validate_proof(binding, PAYLOAD,
                    case.raw_proof(binding, offset, raw), paste_fence=fence, not_before=intent))
                case.append(case.user_record())
                self.assert_received(case, binding, fence, intent, cursor=result['next_offset'])

    def assert_received(self, case, binding, fence, intent, **options):
        result = native.probe(binding, PAYLOAD, paste_fence=fence,
                              not_before=intent, **options)
        self.assertIs(result["confirmed"], True)
        proof = result["native_proof"]
        self.assertEqual(proof["reception_kind"], "native_user_message")
        self.assertTrue(native.validate_proof(
            binding, PAYLOAD, proof, paste_fence=fence, not_before=intent))
        with case.transcript.open("rb") as handle:
            handle.seek(proof["offset"])
            self.assertEqual(sha256(handle.read(proof["length"])), proof["sha256"])
        return result

    def test_fresh_exact_user_codex(self):
        with NativeCase("codex") as case:
            binding, fence, intent = case.ready()
            offset, raw = case.append(case.user_record())
            result = self.assert_received(case, binding, fence, intent)
            self.assertEqual(result["native_proof"]["offset"], offset)
            self.assertEqual(result["next_offset"], offset + len(raw))

    def test_fresh_exact_user_claude(self):
        with NativeCase() as case:
            binding, fence, intent = case.ready()
            case.append(case.user_record())
            result = self.assert_received(case, binding, fence, intent)
            self.assertTrue(native.confirmed(
                case.bridge, TARGET, binding, PAYLOAD,
                proof=result["native_proof"], paste_fence=fence, not_before=intent)["confirmed"])

    def test_millisecond_truncation_after_fresh_fence(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), NativeCase(provider) as case:
                binding, fence, intent = case.ready()
                case.append(case.user_record(timestamp=stamp(intent, "milliseconds")))
                self.assertLess(datetime.fromisoformat(
                    stamp(intent, "milliseconds").replace("Z", "+00:00")).timestamp(), intent)
                self.assert_received(case, binding, fence, intent)

    def test_preceding_millisecond_is_still_rejected(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), NativeCase(provider) as case:
                binding, fence, intent = case.ready()
                offset, raw = case.append(case.user_record(
                    timestamp=stamp(EPOCH + 1.122, "milliseconds")))
                result = native.probe(binding, PAYLOAD, paste_fence=fence, not_before=intent)
                self.assertIs(result["confirmed"], False)
                self.assertFalse(native.validate_proof(
                    binding, PAYLOAD, case.raw_proof(binding, offset, raw),
                    paste_fence=fence, not_before=intent))

    def test_old_user_between_bind_and_paste_cannot_cross_fence(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), NativeCase(provider) as case:
                binding = case.bind()
                # 时间戳与 intent 同毫秒，拒绝必须依赖新的 EOF，而非旧时间过滤。
                offset, raw = case.append(case.user_record(
                    timestamp=stamp(EPOCH + 1.123, "milliseconds")))
                fence, intent = case.fence(binding)
                self.assertEqual(fence["transcript"]["offset"], offset + len(raw))
                result = native.probe(binding, PAYLOAD, paste_fence=fence, not_before=intent)
                self.assertIs(result["confirmed"], False)
                self.assertFalse(native.validate_proof(
                    binding, PAYLOAD, case.raw_proof(binding, offset, raw),
                    paste_fence=fence, not_before=intent))
                case.append(case.user_record())
                self.assert_received(case, binding, fence, intent, cursor=result["next_offset"])

    def test_half_line_crossing_paste_fence_is_excluded(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), NativeCase(provider) as case:
                binding = case.bind()
                raw = jsonl(case.user_record())
                split = len(raw) // 2
                offset, _ = case.append(raw=raw[:split])
                fence, intent = case.fence(binding)
                self.assertIs(fence["transcript"]["at_record_boundary"], False)
                case.append(raw=raw[split:])
                result = native.probe(binding, PAYLOAD, paste_fence=fence, not_before=intent)
                self.assertIs(result["confirmed"], False)
                self.assertFalse(native.validate_proof(
                    binding, PAYLOAD, case.raw_proof(binding, offset, raw),
                    paste_fence=fence, not_before=intent))
                case.append(case.user_record())
                self.assert_received(case, binding, fence, intent, cursor=result["next_offset"])

    def test_incomplete_new_line_keeps_cursor_until_completed(self):
        with NativeCase("codex") as case:
            binding, fence, intent = case.ready()
            raw = jsonl(case.user_record())
            offset, _ = case.append(raw=raw[:-1])
            pending = native.probe(binding, PAYLOAD, paste_fence=fence, not_before=intent)
            self.assertIs(pending["confirmed"], False)
            self.assertEqual(pending["state"], "NATIVE_RECORD_INCOMPLETE")
            self.assertEqual(pending["next_offset"], offset)
            case.append(raw=b"\n")
            self.assert_received(case, binding, fence, intent, cursor=pending["next_offset"])

    def test_missing_fence_or_intent_never_confirms(self):
        with NativeCase() as case:
            binding, fence, intent = case.ready()
            case.append(case.user_record())
            proof = self.assert_received(case, binding, fence, intent)["native_proof"]
            for supplied_fence, supplied_intent in ((None, intent), (fence, None)):
                with self.subTest(fence=supplied_fence is not None,
                                  intent=supplied_intent is not None):
                    with self.assertRaises(native.NativeDeliveryError):
                        native.probe(binding, PAYLOAD, paste_fence=supplied_fence,
                                     not_before=supplied_intent)
                    self.assertFalse(native.validate_proof(
                        binding, PAYLOAD, proof, paste_fence=supplied_fence,
                        not_before=supplied_intent))

    def test_changed_fence_and_retroactive_times_are_rejected(self):
        with NativeCase() as case:
            binding, fence, intent = case.ready()
            case.append(case.user_record())
            proof = self.assert_received(case, binding, fence, intent)["native_proof"]
            candidates = []
            for key, value in (
                    ("binding_sha256", "0" * 64),
                    ("captured_at_epoch", binding["bound_at_epoch"] - 1),
                    ("captured_at_epoch", intent + 1)):
                altered = copy.deepcopy(fence)
                altered[key] = value
                candidates.append(altered)
            altered = copy.deepcopy(fence)
            altered["transcript"]["offset"] = binding["transcript"]["offset"] - 1
            candidates.append(altered)
            for index, altered in enumerate(candidates):
                with self.subTest(index=index):
                    with self.assertRaises(native.NativeDeliveryError):
                        native.probe(binding, PAYLOAD, paste_fence=altered, not_before=intent)
                    self.assertFalse(native.validate_proof(
                        binding, PAYLOAD, proof, paste_fence=altered, not_before=intent))

    def test_changed_proof_fields_are_rejected(self):
        with NativeCase("codex") as case:
            binding, fence, intent = case.ready()
            case.append(case.user_record())
            proof = self.assert_received(case, binding, fence, intent)["native_proof"]
            mutations = {
                "schema": "other", "session_id": OTHER_SESSION,
                "path": str(case.root / "wrong.jsonl"),
                "device": proof["device"] + 1, "inode": proof["inode"] + 1,
                "offset": proof["offset"] + 1, "length": proof["length"] - 1,
                "sha256": "0" * 64, "reception_kind": "native_queued_command",
                "payload_sha256": "0" * 64}
            for key, value in mutations.items():
                with self.subTest(field=key):
                    altered = dict(proof, **{key: value})
                    self.assertFalse(native.validate_proof(
                        binding, PAYLOAD, altered, paste_fence=fence, not_before=intent))
            self.assertTrue(native.validate_proof(
                binding, PAYLOAD, proof, paste_fence=fence, not_before=intent))

    def test_proof_rechecks_actual_transcript_bytes(self):
        with NativeCase() as case:
            binding, fence, intent = case.ready()
            case.append(case.user_record())
            proof = self.assert_received(case, binding, fence, intent)["native_proof"]
            altered = jsonl(case.user_record(PAYLOAD.replace("-01", "-02")))
            self.assertEqual(len(altered), proof["length"])
            with case.transcript.open("r+b") as handle:
                handle.seek(proof["offset"])
                handle.write(altered)
            self.assertFalse(native.validate_proof(
                binding, PAYLOAD, proof, paste_fence=fence, not_before=intent))
            self.assertIs(native.probe(
                binding, PAYLOAD, paste_fence=fence, not_before=intent)["confirmed"], False)

    def test_full_text_equality_rejects_prefix_quotes_and_whitespace_changes(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), NativeCase(provider) as case:
                binding, fence, intent = case.ready()
                for text in (PAYLOAD.split("\n")[0], PAYLOAD + " ",
                             PAYLOAD.replace("\n", " "), "> " + PAYLOAD):
                    case.append(case.user_record(text))
                result = native.probe(binding, PAYLOAD, paste_fence=fence, not_before=intent)
                self.assertIs(result["confirmed"], False)
                with self.assertRaises(case.bridge.DispatchUnconfirmed):
                    native.confirmed(case.bridge, TARGET, binding, PAYLOAD,
                                     paste_fence=fence, not_before=intent)
                case.append(case.user_record())
                self.assert_received(case, binding, fence, intent, cursor=result["next_offset"])

    def test_queued_command_cannot_be_promoted_by_changing_proof_kind(self):
        with NativeCase() as case:
            binding, fence, intent = case.ready()
            case.append(case.queued_record())
            queued = native.probe(binding, PAYLOAD, paste_fence=fence, not_before=intent)
            self.assertIs(queued["confirmed"], False)
            self.assertEqual(queued["state"], "NATIVE_QUEUED")
            for proof in (queued["queued_proof"],
                          dict(queued["queued_proof"], reception_kind="native_user_message")):
                self.assertFalse(native.validate_proof(
                    binding, PAYLOAD, proof, paste_fence=fence, not_before=intent))
                with self.assertRaises(case.bridge.DispatchUnconfirmed):
                    native.confirmed(case.bridge, TARGET, binding, PAYLOAD,
                                     proof=proof, paste_fence=fence, not_before=intent)
            case.append(case.user_record())
            self.assert_received(case, binding, fence, intent, cursor=queued["next_offset"])

    def test_queue_then_real_user_in_same_scan_confirms_only_real_user(self):
        with NativeCase() as case:
            binding, fence, intent = case.ready()
            case.append(case.queued_record())
            offset, _ = case.append(case.user_record())
            result = self.assert_received(case, binding, fence, intent)
            self.assertEqual(result["native_proof"]["offset"], offset)

    def test_scan_budget_advances_by_whole_records_without_skips(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), NativeCase(provider) as case:
                binding, fence, intent = case.ready()
                ends = []
                for index in range(5):
                    offset, raw = case.append({"type": "assistant",
                                               "content": "unrelated-" + str(index)})
                    ends.append(offset + len(raw))
                target_offset, raw = case.append(case.user_record())
                ends.append(target_offset + len(raw))
                cursor = fence["transcript"]["offset"]
                for index, end in enumerate(ends):
                    result = native.probe(binding, PAYLOAD, paste_fence=fence,
                                          not_before=intent, max_bytes=1, cursor=cursor)
                    self.assertGreater(result["next_offset"], cursor)
                    self.assertEqual(result["next_offset"], end)
                    if index < len(ends) - 1:
                        self.assertIs(result["confirmed"], False)
                        self.assertEqual(result["state"], "NATIVE_SCAN_BUDGET")
                    else:
                        self.assertIs(result["confirmed"], True)
                        self.assertEqual(result["native_proof"]["offset"], target_offset)
                        self.assertTrue(native.validate_proof(
                            binding, PAYLOAD, result["native_proof"],
                            paste_fence=fence, not_before=intent))
                    cursor = result["next_offset"]

    def test_invalid_budget_and_nonboundary_cursor_are_rejected(self):
        with NativeCase("codex") as case:
            binding, fence, intent = case.ready()
            case.append(case.user_record())
            for value in (0, -1, True, native.MAX_SCAN + 1):
                with self.subTest(budget=value), self.assertRaises(native.NativeDeliveryError):
                    native.probe(binding, PAYLOAD, paste_fence=fence,
                                 not_before=intent, max_bytes=value)
            for cursor in (fence["transcript"]["offset"] - 1,
                           fence["transcript"]["offset"] + 1,
                           case.transcript.stat().st_size + 1):
                with self.subTest(cursor=cursor), self.assertRaises(native.NativeDeliveryError):
                    native.probe(binding, PAYLOAD, paste_fence=fence,
                                 not_before=intent, cursor=cursor)

    def test_receiver_observation_does_not_grant_sender_recovery_authority(self):
        with NativeCase() as case:
            binding, fence, intent = case.ready()
            case.append(case.user_record())
            proof = self.assert_received(case, binding, fence, intent)["native_proof"]
            attempt_path = case.root / "attempt-001.json"
            attempt = dict(native_binding=binding, binding={"identity": binding["identity"]},
                           events=[dict(phase="PASTE_INTENT", at_epoch=intent, screen="",
                                        screen_sha256=sha256(b"")[:16], native_paste_fence=fence)])
            attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
            original = attempt_path.read_bytes()
            case.receiver = True
            native.require_bound(case.bridge, CALLER, binding, PAYLOAD, read_only=True)
            observed = native.scan_original(
                case.bridge, CALLER, PAYLOAD, attempt_path, binding, read_only=True)
            self.assertIs(observed["confirmed"], True)
            sidecar = attempt_path.with_name("native-scan-" + attempt_path.stem + ".json")
            self.assertTrue(sidecar.is_file())
            sidecar_bytes = sidecar.read_bytes()
            with self.assertRaises(native.NativeDeliveryError):
                native.require_bound(case.bridge, CALLER, binding, PAYLOAD)
            with self.assertRaises(native.NativeDeliveryError):
                native.confirmed(case.bridge, CALLER, binding, PAYLOAD, proof=proof,
                                 paste_fence=fence, not_before=intent)
            with self.assertRaises(native.NativeDeliveryError):
                native.scan_original(case.bridge, CALLER, PAYLOAD, attempt_path, binding)
            self.assertEqual(attempt_path.read_bytes(), original)
            self.assertEqual(sidecar.read_bytes(), sidecar_bytes)
            self.assertEqual(case.input_operations, [])

    def test_receiver_wrong_reverse_pane_is_not_an_observer(self):
        with NativeCase() as case:
            binding, fence, intent = case.ready()
            case.append(case.user_record())
            self.assert_received(case, binding, fence, intent)
            case.receiver = True
            case.observer_pane = CALLER_PANE
            with self.assertRaises(native.NativeDeliveryError):
                native.require_bound(case.bridge, CALLER, binding, PAYLOAD, read_only=True)


class NativeSessionAcceptance(unittest.TestCase):
    def test_native_registration_accepts_kernel_birth_in_utc_or_local_format(self):
        for local in (False, True):
            with self.subTest(local=local), NativeCase() as case:
                case.write_registration(local=local)
                binding = case.bind()
                self.assertEqual(binding["session_id"], SESSION)
                self.assertEqual(binding["process"]["birth"], case.process["birth"])

    def test_resume_registration_overrides_stale_argv(self):
        with NativeCase() as case:
            stale = case.create_transcript(OTHER_SESSION)
            case.process["argv"][-1] = OTHER_SESSION
            binding = case.bind()
            self.assertEqual(binding["session_id"], SESSION)
            self.assertEqual(binding["transcript"]["path"], str(case.transcript))
            self.assertNotEqual(binding["transcript"]["path"], str(stale))

    def test_valid_registration_wins_even_when_argv_sessions_are_ambiguous(self):
        with NativeCase() as case:
            case.process["argv"].extend(["--session-id", OTHER_SESSION])
            self.assertEqual(case.bind()["session_id"], SESSION)

    def test_resume_after_binding_invalidates_original_sender_confirmation(self):
        with NativeCase() as case:
            binding, fence, intent = case.ready()
            case.append(case.user_record())
            result = native.probe(binding, PAYLOAD, paste_fence=fence, not_before=intent)
            self.assertIs(result["confirmed"], True)
            resumed_path = case.create_transcript(OTHER_SESSION)
            case.write_registration(session=OTHER_SESSION)
            # 真实 /resume 改原生注册，旧进程的启动 argv 可以保持不变。
            self.assertEqual(case.process["argv"][-1], SESSION)
            with self.assertRaises(native.NativeDeliveryError):
                native.require_bound(case.bridge, TARGET, binding, PAYLOAD)
            with self.assertRaises(native.NativeDeliveryError):
                native.confirmed(case.bridge, TARGET, binding, PAYLOAD,
                                 proof=result["native_proof"], paste_fence=fence,
                                 not_before=intent)
            resumed = case.bind()
            self.assertEqual(resumed["session_id"], OTHER_SESSION)
            self.assertEqual(resumed["transcript"]["path"], str(resumed_path))

    def test_birth_mismatch_does_not_fall_back_to_valid_argv(self):
        with NativeCase() as case:
            mismatch = time.strftime("%a %b %e %H:%M:%S %Y",
                                     time.gmtime(case.process["birth"][0] - 1))
            case.write_registration(procStart=mismatch)
            with self.assertRaises(native.NativeDeliveryError):
                case.bind()

    def test_pid_and_noninteractive_registration_are_rejected(self):
        for changed in ({"pid": PID + 1}, {"kind": "background"}):
            with self.subTest(changed=changed), NativeCase() as case:
                case.write_registration(**changed)
                with self.assertRaises(native.NativeDeliveryError):
                    case.bind()

    def test_existing_malformed_registration_never_falls_back(self):
        for raw in ("{", "[]", '{"sessionId":"bad"}'):
            with self.subTest(raw=raw), NativeCase() as case:
                case.registration.write_text(raw, encoding="utf-8")
                with self.assertRaises(native.NativeDeliveryError):
                    case.bind()

    def test_native_registration_symlink_is_rejected(self):
        with NativeCase() as case:
            original = case.root / "registration-real.json"
            case.registration.rename(original)
            case.registration.symlink_to(original)
            with self.assertRaises(native.NativeDeliveryError):
                case.bind()

    def test_absent_registration_accepts_unique_argv_session(self):
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider), NativeCase(provider, registered=False) as case:
                self.assertFalse(case.registration.exists())
                self.assertEqual(case.bind()["session_id"], SESSION)

    def test_absent_registration_rejects_missing_or_ambiguous_argv(self):
        for provider in ("claude", "codex"):
            for ambiguous in (False, True):
                with self.subTest(provider=provider, ambiguous=ambiguous), NativeCase(
                        provider, registered=False) as case:
                    if ambiguous:
                        case.process["argv"].extend(["--session-id", OTHER_SESSION])
                    else:
                        case.process["argv"] = case.process["argv"][:1]
                    with self.assertRaises(native.NativeDeliveryError):
                        case.bind()

    def test_official_node_cli_is_a_claude_provider(self):
        for node_flag in (False, True):
            with self.subTest(node_flag=node_flag), NativeCase(node=True) as case:
                if node_flag:
                    case.process["argv"].insert(1, "--no-warnings")
                binding = case.bind()
                self.assertEqual(binding["provider"], "claude")
                self.assertEqual(binding["session_id"], SESSION)

    def test_unrelated_node_cli_is_not_a_claude_provider(self):
        with NativeCase(node=True) as case:
            case.process["argv"][1] = "/test/not-anthropic/cli.js"
            with self.assertRaises(native.NativeDeliveryError):
                case.bind()

    def test_process_birth_change_after_binding_is_rejected(self):
        with NativeCase() as case:
            binding = case.bind()
            case.process["birth"][1] += 1
            with self.assertRaises(native.NativeDeliveryError):
                native.require_bound(case.bridge, TARGET, binding, PAYLOAD)

    def test_retired_manual_hook_cannot_create_a_session_identity(self):
        with NativeCase(registered=False) as case:
            case.process["argv"] = case.process["argv"][:1]
            snapshot = case.snapshot()
            process_calls, ps_calls = len(case.process_calls), len(case.ps_calls)
            payload = dict(hook_event_name="SessionStart", session_id=SESSION,
                           transcript_path=str(case.transcript), pid=PID,
                           kind="interactive",
                           procStart=time.strftime("%a %b %e %H:%M:%S %Y",
                                                   time.gmtime(case.process["birth"][0])))
            with self.assertRaisesRegex(native.NativeDeliveryError,
                                        "NATIVE_MANUAL_REGISTRATION_DISABLED"):
                native.register_hook(payload)
            self.assertEqual(case.snapshot(), snapshot)
            self.assertEqual(len(case.process_calls), process_calls)
            self.assertEqual(len(case.ps_calls), ps_calls)
            self.assertFalse(case.registration.exists())
            with self.assertRaises(native.NativeDeliveryError):
                case.bind()

    def test_retired_manual_hook_cannot_overwrite_a_native_registration(self):
        with NativeCase() as case:
            snapshot = case.snapshot()
            with self.assertRaisesRegex(native.NativeDeliveryError,
                                        "NATIVE_MANUAL_REGISTRATION_DISABLED"):
                native.register_hook(dict(session_id=OTHER_SESSION, pid=PID,
                                          transcript_path=str(case.transcript)))
            self.assertEqual(case.snapshot(), snapshot)
            self.assertEqual(case.bind()["session_id"], SESSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)

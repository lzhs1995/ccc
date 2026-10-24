"""callback 只读诊断的独立行为回归；真实任务、锁、attempt、报告和封口逻辑。

只替换外部进程观测，不替换 active marker、terminal_report、身份求值或诊断结果。
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_callback_diagnose as diagnose
import cmux_consensus_stop_guard as stop_guard
import cmux_daemon_identity as daemon
import cmux_executor_closeout_guard as closeout_guard
import cmux_hook_identity as hook_identity
import cmux_workspace_guard as workspace_guard
import executor_closeout as closeout


def _ordinary_process(pid, **_kwargs):
    """外部 OS 观测夹具：普通终端父进程；真实身份解析仍会复核祖先。"""
    return dict(pid=pid, ppid=1, birth=[1, 0], argv=["/offline/test-client"],
                executable="/offline/test-client", env={})


class CallbackDiagnoseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="callback-diagnose-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.artifact = self.root / "frozen task with spaces"
        self.artifact.mkdir()
        self.active = self.root / "active"
        self.workspace = "aaaaaaaa-1111-2222-3333-444444444444"
        self.executor = "bbbbbbbb-1111-2222-3333-444444444444"
        self.supervisor = "cccccccc-1111-2222-3333-444444444444"
        self.outsider = "dddddddd-1111-2222-3333-444444444444"
        self.marker_path = self.active / self.workspace / "diagnose-task.json"
        self.marker_path.parent.mkdir(parents=True)
        self.marker = dict(
            task_id="diagnose-task", workspace_uuid=self.workspace,
            artifact_root=str(self.artifact),
            armed_at=datetime.now(timezone.utc).isoformat(), ttl_seconds=3600,
            participants=[
                dict(role="supervisor", surface_uuid=self.supervisor,
                     surface_ref="surface:1"),
                dict(role="executor", surface_uuid=self.executor, surface_ref="surface:2"),
            ],
        )
        self.write(self.marker_path, self.marker)
        self.report = self.artifact / "report.md"
        self.report.write_bytes("已完成有界检查；投递结果仍待核对。\n".encode())
        self.pack_path = self.artifact / "task-pack.json"
        self.receipt = self.artifact / "completion-receipt.json"
        self.pack = dict(
            draft=False, task_id="diagnose-task", executor_uuid=self.executor,
            completion_nonce="original-nonce",
            completion_callback="DONE|diagnose-task|original-nonce",
            callback_target="surface:1", report=str(self.report),
            completion_receipt=str(self.receipt),
        )
        self.write(self.pack_path, self.pack)
        self.journal = self.artifact / "completion-receipt-attempts"
        self.journal.mkdir()
        self.lock_path = self.journal / "delivery.lock"
        self.lock_path.write_bytes(b"")
        self.attempt_path = self.journal / "attempt-0001.json"
        self.attempt = dict(
            binding={
                **{key: self.pack[key] for key in
                   ("task_id", "completion_nonce", "completion_callback",
                    "callback_target", "report")},
                "task_pack_sha256": self.sha(self.pack_path),
                "report_sha256": self.sha(self.report),
                "report_bytes": self.report.stat().st_size,
                "identity": dict(
                    workspace_uuid=self.workspace, caller_surface_uuid=self.executor,
                    target_surface_uuid=self.supervisor, target_pane_uuid="original-pane",
                ),
            },
            native_binding={"provider": "claude", "fixture": "presence-only"},
            phase="ENTER_SENT", started_at_epoch=100.0, ended_at_epoch=104.0,
            events=[
                dict(phase="PASTE_INTENT", at_epoch=101.0,
                     native_paste_fence={"fixture": "presence-only"}),
                dict(phase="ENTER_INTENT", at_epoch=102.0),
                dict(phase="ENTER_SENT", at_epoch=103.0),
            ],
            error="original delivery remains unconfirmed",
        )
        self.write(self.attempt_path, self.attempt)
        self.receipt_seeded = False
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(patch.object(daemon.sys, "platform", "darwin"))
        # Pin the observed parent as well as its process data. A container's
        # PID 1 parent otherwise skips ancestry and never exercises refusal.
        patches.enter_context(patch.object(daemon.os, "getppid", return_value=4242))
        self.process_reader = patches.enter_context(patch.object(
            daemon, "process", side_effect=_ordinary_process))
        patches.enter_context(patch.object(stop_guard, "ACTIVE_DIR", self.active))
        patches.enter_context(patch.dict(os.environ, {
            "CMUX_WORKSPACE_ID": self.workspace, "CMUX_SURFACE_ID": self.executor,
        }))
        self.forbidden = []
        for module, name in (
            (bridge, "send_text"), (bridge, "send_key"), (bridge, "read_screen"),
            (bridge, "_run"), (workspace_guard, "_read_json_command"),
            (subprocess, "Popen"),
        ):
            self.forbidden.append(patches.enter_context(patch.object(
                module, name, side_effect=AssertionError("diagnosis must not call " + name))))
        self.frozen = closeout.terminal_report(self.marker, self.workspace, self.executor)
        self.assertIsInstance(self.frozen, dict, "真实文件夹具必须先满足封口证据条件")

    def tearDown(self):
        for forbidden in self.forbidden:
            forbidden.assert_not_called()
        self.assertEqual(self.receipt.exists(), self.receipt_seeded)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def tree(self):
        # 不比较读取引起的 atime；保留 inode、内容、mtime/ctime 和目录变化。
        rows = {}
        for path in self.root.rglob("*"):
            info = path.lstat()
            meta = (info.st_mode, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
            if stat.S_ISLNK(info.st_mode):
                value = ("symlink", os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                value = ("file", info.st_size, self.sha(path))
            else:
                value = ("other",)
            rows[str(path.relative_to(self.root))] = (meta, value)
        return rows

    def readonly(self, function, *args, **kwargs):
        before = self.tree()
        try:
            return function(*args, **kwargs)
        finally:
            self.assertEqual(self.tree(), before, "诊断不得新增、改写或删除证据")

    def payload(self):
        command = shlex.join([
            "rtk", "proxy", str(Path(diagnose.__file__).resolve()),
            "--task-pack", str(self.pack_path),
        ])
        return dict(hook_event_name="PreToolUse", tool_name="Bash",
                    tool_input=dict(command=command, run_in_background=False))

    def call(self, payload=None):
        return self.readonly(diagnose.diagnose, self.pack_path, payload)

    def repin(self):
        # 仅用于模拟外部并发改写；产品仍必须拒绝两次核验之间的变化。
        self.attempt["binding"].update(
            task_pack_sha256=self.sha(self.pack_path),
            report_sha256=self.sha(self.report), report_bytes=self.report.stat().st_size,
        )
        self.write(self.attempt_path, self.attempt)

    def assert_race_rejected(self, mutation, *, before_read=False, message="changed"):
        real_read = diagnose.read_bytes
        observed = []

        def read_with_mutation(path, *args, **kwargs):
            self.assertEqual(Path(path), self.attempt_path)
            self.assertEqual(len(observed), 0, "诊断只能读取指定的原 attempt")
            if before_read:
                mutation()
            raw = real_read(path, *args, **kwargs)
            if not before_read:
                mutation()
            observed.append(self.tree())
            return raw

        with patch.object(diagnose, "read_bytes", side_effect=read_with_mutation):
            with self.assertRaisesRegex(ValueError, message):
                diagnose.diagnose(self.pack_path)
        self.assertEqual(len(observed), 1)
        self.assertEqual(self.tree(), observed[0], "产品不得在竞态之后写入任何文件")

    def test_exact_bash_diagnostic_is_allowed(self):
        self.assertTrue(self.readonly(
            diagnose.allowed, self.payload(), self.marker, self.frozen))

    def test_admission_rejects_wrong_tool_task_and_artifact_root(self):
        for name in ("exec_command", "functions.exec", "Write", ""):
            payload = self.payload()
            payload["tool_name"] = name
            with self.subTest(tool=name):
                self.assertFalse(diagnose.allowed(payload, self.marker, self.frozen))
        for key, value in (("task_id", "another-task"),
                           ("artifact_root", str(self.root / "other"))):
            evidence = dict(self.frozen, **{key: value})
            with self.subTest(key=key):
                self.assertFalse(diagnose.allowed(self.payload(), self.marker, evidence))

    def test_admission_rejects_shell_tail_wrapper_and_background(self):
        expected = self.payload()["tool_input"]["command"]
        variants = (
            expected + " && true", expected + "; true", expected + "\ntrue",
            expected + " &", "env " + expected, "bash -c " + shlex.quote(expected),
            expected.replace(str(self.pack_path), str(self.artifact / "other.json")),
            expected + " --task-pack " + shlex.quote(str(self.pack_path)),
        )
        for command in variants:
            payload = self.payload()
            payload["tool_input"]["command"] = command
            with self.subTest(command=command):
                self.assertFalse(diagnose.allowed(payload, self.marker, self.frozen))
        for background in (True, "false", 1):
            payload = self.payload()
            payload["tool_input"]["run_in_background"] = background
            with self.subTest(background=background):
                self.assertFalse(diagnose.allowed(payload, self.marker, self.frozen))

    def test_malformed_admission_inputs_are_denied(self):
        for invalid in (None, [], 123, "command"):
            payload = self.payload()
            payload["tool_input"] = invalid
            with self.subTest(tool_input=invalid):
                self.assertFalse(diagnose.allowed(payload, self.marker, self.frozen))
        for payload, marker, frozen in (
            (None, self.marker, self.frozen), ([], self.marker, self.frozen),
            (self.payload(), None, self.frozen), (self.payload(), self.marker, None),
        ):
            with self.subTest(payload=payload, marker=marker, frozen=frozen):
                self.assertFalse(diagnose.allowed(payload, marker, frozen))

    def test_sealed_environment_admits_only_exact_sync_diagnostic(self):
        allowed, reason = self.readonly(closeout_guard.evaluate, self.payload())
        self.assertTrue(allowed, reason)
        for command in ("echo unrelated", self.payload()["tool_input"]["command"] + "; true"):
            payload = self.payload()
            payload["tool_input"]["command"] = command
            with self.subTest(command=command):
                allowed, reason = self.readonly(closeout_guard.evaluate, payload)
                self.assertFalse(allowed)
                self.assertIn("EXECUTOR_CLOSEOUT", reason)
        payload = self.payload()
        payload["tool_input"]["run_in_background"] = True
        self.assertFalse(self.readonly(closeout_guard.evaluate, payload)[0])

    def test_sealed_environment_denies_malformed_tool_input(self):
        for invalid in (None, [], 123):
            payload = self.payload()
            payload["tool_input"] = invalid
            with self.subTest(value=invalid):
                allowed, reason = self.readonly(closeout_guard.evaluate, payload)
                self.assertFalse(allowed)
                self.assertIn("EXECUTOR_CLOSEOUT", reason)

    def test_executor_diagnosis_returns_original_hashes_without_side_effects(self):
        result = self.call()
        self.assertEqual(result["state"], "WAITING_SUPERVISOR")
        self.assertEqual(result["task_id"], self.pack["task_id"])
        self.assertEqual(result["attempt"], str(self.attempt_path))
        self.assertEqual(result["task_pack_sha256"], self.sha(self.pack_path))
        self.assertEqual(result["report_sha256"], self.sha(self.report))
        self.assertEqual(result["attempt_sha256"], self.sha(self.attempt_path))
        self.assertTrue(result["original_native_binding_present"])
        self.assertTrue(result["original_paste_fence_present"])
        self.assertEqual(result["delivery_proof"], "NOT_CHECKED")
        self.assertEqual(result["input_operations"], 0)
        self.assertIs(result["receipt_created"], False)
        self.assertIs(result["accepted"], False)
        self.assertIs(result["disarmed"], False)

    def test_bound_supervisor_can_diagnose_the_same_executor(self):
        with patch.dict(os.environ, {"CMUX_SURFACE_ID": self.supervisor}):
            result = self.call()
        self.assertEqual(result["attempt"], str(self.attempt_path))
        self.assertEqual(result["supervisor_uuid"], self.supervisor)

    def test_wrong_workspace_and_unrelated_caller_cannot_spoof_payload_identity(self):
        for workspace, caller in (
            ("other-workspace", self.executor), (self.workspace, self.outsider),
        ):
            with self.subTest(workspace=workspace, caller=caller), patch.dict(os.environ, {
                "CMUX_WORKSPACE_ID": workspace, "CMUX_SURFACE_ID": caller,
            }):
                with self.assertRaises(ValueError):
                    self.call(dict(workspace_id=self.workspace, surface_id=self.executor))

    def test_identity_failure_is_not_an_ordinary_caller_fallback(self):
        # This refusal belongs to an explicitly enrolled native conversation.
        session = "eeeeeeee-1111-2222-3333-444444444444"
        self.marker["participants"][1]["native_session_id"] = session
        self.write(self.marker_path, self.marker)
        payload = dict(self.payload(), session_id=session)
        with patch.object(daemon, "process", side_effect=daemon.IdentityError("identity denied")):
            with self.assertRaisesRegex(daemon.IdentityError, "identity denied"):
                self.call(payload)
            allowed, reason = self.readonly(closeout_guard.evaluate, payload)
        self.assertTrue(allowed, reason)

    def test_relative_misnamed_and_symlink_pack_paths_are_rejected(self):
        alias_dir = self.root / "alias"
        alias_dir.mkdir()
        alias = alias_dir / "task-pack.json"
        alias.symlink_to(self.pack_path)
        for path in (Path("task-pack.json"), self.artifact / "other.json", alias):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.readonly(diagnose.diagnose, path)

    def test_absent_expired_or_other_task_markers_do_not_authorize(self):
        for variation in ("absent", "expired", "task", "workspace", "root"):
            marker = copy.deepcopy(self.marker)
            if variation == "absent":
                self.marker_path.unlink(missing_ok=True)
            else:
                if variation == "expired":
                    marker["armed_at"] = (
                        datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
                elif variation == "task":
                    marker["task_id"] = "other-task"
                elif variation == "workspace":
                    marker["workspace_uuid"] = "other-workspace"
                else:
                    marker["artifact_root"] = str(self.root / "other-root")
                self.write(self.marker_path, marker)
            with self.subTest(variation=variation), self.assertRaises(ValueError):
                self.call()

    def test_duplicate_matching_marker_is_ambiguous(self):
        duplicate = self.marker_path.with_name("duplicate.json")
        self.write(duplicate, self.marker)
        with self.assertRaisesRegex(ValueError, "unique frozen task"):
            self.call()

    def test_missing_or_multiple_supervisors_do_not_authorize(self):
        original = copy.deepcopy(self.marker)
        for supervisors in ([], [original["participants"][0]] * 2):
            marker = copy.deepcopy(original)
            marker["participants"] = supervisors + [original["participants"][1]]
            self.write(self.marker_path, marker)
            with self.subTest(count=len(supervisors)), self.assertRaises(ValueError):
                self.call()

    def test_changed_task_pack_hash_is_rejected(self):
        self.pack["revision"] = "changed after original attempt"
        self.write(self.pack_path, self.pack)
        with self.assertRaises(ValueError):
            self.call()

    def test_changed_report_hash_is_rejected(self):
        self.report.write_bytes(b"changed report after original attempt\n")
        with self.assertRaises(ValueError):
            self.call()

    def test_wrong_original_binding_is_rejected(self):
        original = copy.deepcopy(self.attempt)
        fields = ("task_id", "completion_nonce", "completion_callback", "callback_target",
                  "report", "task_pack_sha256", "report_sha256", "report_bytes")
        for key in fields:
            self.attempt = copy.deepcopy(original)
            self.attempt["binding"][key] = "wrong"
            self.write(self.attempt_path, self.attempt)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.call()
        for key in ("workspace_uuid", "caller_surface_uuid", "target_surface_uuid",
                    "target_pane_uuid"):
            self.attempt = copy.deepcopy(original)
            self.attempt["binding"]["identity"][key] = "" if key == "target_pane_uuid" else "wrong"
            self.write(self.attempt_path, self.attempt)
            with self.subTest(identity=key), self.assertRaises(ValueError):
                self.call()

    def test_malformed_attempt_and_inflight_latest_attempt_are_rejected(self):
        for raw in (b"{broken", b"[]"):
            self.attempt_path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.call()
        self.write(self.attempt_path, self.attempt)
        latest = dict(self.attempt)
        latest.pop("ended_at_epoch")
        self.write(self.journal / "attempt-0002.json", latest)
        with self.assertRaises(ValueError):
            self.call()

    def test_attempt_hash_change_before_direct_read_is_rejected(self):
        def mutate():
            self.attempt["error"] = "concurrent change"
            self.write(self.attempt_path, self.attempt)
        self.assert_race_rejected(mutate, before_read=True,
                                  message="original attempt changed")

    def test_attempt_hash_change_after_direct_read_is_rejected(self):
        def mutate():
            self.attempt["error"] = "concurrent change"
            self.write(self.attempt_path, self.attempt)
        self.assert_race_rejected(mutate, message="frozen report evidence changed")

    def test_report_and_binding_changed_together_are_rejected(self):
        def mutate():
            self.report.write_bytes(b"coherently repinned concurrent report\n")
            self.repin()
        self.assert_race_rejected(mutate, message="frozen report evidence changed")

    def test_task_pack_and_binding_changed_together_are_rejected(self):
        def mutate():
            self.pack["revision"] = "coherent concurrent pack change"
            self.write(self.pack_path, self.pack)
            self.repin()
        self.assert_race_rejected(mutate, message="frozen report evidence changed")

    def test_new_attempt_after_direct_read_is_rejected(self):
        self.assert_race_rejected(
            lambda: self.write(self.journal / "attempt-0002.json", self.attempt),
            message="frozen report evidence changed")

    def test_missing_original_attempt_after_direct_read_is_rejected(self):
        self.assert_race_rejected(self.attempt_path.unlink,
                                  message="frozen report evidence changed")

    def test_active_sender_lock_blocks_without_touching_files(self):
        with self.lock_path.open("rb") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError):
                self.call()
            fcntl.flock(held, fcntl.LOCK_UN)

    def test_missing_lock_is_not_recreated(self):
        self.lock_path.unlink()
        with self.assertRaises(ValueError):
            self.call()
        self.assertFalse(self.lock_path.exists())

    def test_missing_or_duplicate_native_metadata_only_changes_presence_flags(self):
        original = copy.deepcopy(self.attempt)
        for variation in ("binding", "fence", "duplicate-paste"):
            self.attempt = copy.deepcopy(original)
            if variation == "binding":
                self.attempt.pop("native_binding")
            elif variation == "fence":
                self.attempt["events"][0].pop("native_paste_fence")
            else:
                extra = dict(self.attempt["events"][0], at_epoch=101.5)
                self.attempt["events"].insert(1, extra)
            self.write(self.attempt_path, self.attempt)
            with self.subTest(variation=variation):
                result = self.call()
                self.assertEqual(result["original_native_binding_present"],
                                 variation != "binding")
                self.assertEqual(result["original_paste_fence_present"],
                                 variation == "binding")
                self.assertEqual(result["delivery_proof"], "NOT_CHECKED")
                self.assertIs(result["receipt_created"], False)

    def test_no_input_diagnosis_never_creates_input_or_receipt(self):
        self.attempt.update(phase="NO_INPUT", events=[])
        self.attempt.pop("native_binding")
        self.write(self.attempt_path, self.attempt)
        result = self.call()
        self.assertEqual(result["attempt_phase"], "NO_INPUT")
        self.assertFalse(result["original_native_binding_present"])
        self.assertFalse(result["original_paste_fence_present"])
        self.assertEqual(result["input_operations"], 0)
        self.assertIs(result["receipt_created"], False)

    def test_stored_confirmed_flag_is_not_delivery_proof_or_acceptance(self):
        self.attempt.update(phase="CONFIRMED", result={"confirmed": True})
        self.write(self.attempt_path, self.attempt)
        result = self.call()
        self.assertEqual(result["delivery_proof"], "NOT_CHECKED")
        self.assertIs(result["accepted"], False)
        self.assertIs(result["disarmed"], False)

    def test_existing_receipt_is_preserved_without_becoming_acceptance(self):
        self.receipt.write_bytes(b'{"legacy_receipt": true}\n')
        self.receipt_seeded = True
        result = self.call()
        self.assertIs(result["receipt_created"], False)
        self.assertIs(result["accepted"], False)
        self.assertEqual(result["delivery_proof"], "NOT_CHECKED")

    def test_main_success_outputs_json_and_exit_zero(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", [str(Path(diagnose.__file__)),
                                        "--task-pack", str(self.pack_path)]):
            with redirect_stdout(out), redirect_stderr(err):
                status = self.readonly(diagnose.main)
        self.assertEqual(status, 0)
        self.assertEqual(err.getvalue(), "")
        result = json.loads(out.getvalue())
        self.assertEqual(result["attempt_sha256"], self.sha(self.attempt_path))
        self.assertEqual(result["input_operations"], 0)
        self.assertIs(result["receipt_created"], False)

    def test_main_identity_failure_is_exit_two_without_success_output(self):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(daemon, "process", side_effect=daemon.IdentityError("identity denied")):
            with patch.object(sys, "argv", [str(Path(diagnose.__file__)),
                                            "--task-pack", str(self.pack_path)]):
                with redirect_stdout(out), redirect_stderr(err):
                    status = self.readonly(diagnose.main)
        self.assertEqual(status, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("CALLBACK_DIAGNOSTIC_UNAVAILABLE: identity denied", err.getvalue())


if __name__ == "__main__":
    unittest.main()

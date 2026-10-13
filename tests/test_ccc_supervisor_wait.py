"""离线行为回归：真实 WatchDaemon / 共享验证器，所有终端输入均由 spy 拦截。"""
from __future__ import annotations

import contextlib
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

sys.dont_write_bytecode = True
CCC_BASE = Path(__file__).resolve().parents[1]
# 显式指定待集成的共享源码；缺失即失败，禁止导入旧 release 冒称新回归。
SHARED_BASE = Path(os.environ["SHARED_SOURCE_UNDER_TEST"]).resolve(strict=True)
sys.path.insert(0, str(CCC_BASE))
import ccc_claude_closeout as adapter
import claude_dispatch_recovery as recovery
import cmux_codex_watch as watch
import tests.test_claude_dispatch_recovery as legacy_recovery
from tests.test_watch import claude_grid_payload, claude_wrapped_composer_payload

assert Path(adapter.__file__).resolve() == CCC_BASE / "ccc_claude_closeout.py"
assert Path(watch.__file__).resolve() == CCC_BASE / "cmux_codex_watch.py"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class RefuseInput:
    """没有外部命令或 cmux socket；guard 在每次虚拟输入前重新执行。"""
    def __init__(self, workspace, surface, pane):
        self.inputs = []
        self.check = None
        self.before_input = None
        self.tree_data = {"windows": [{"workspaces": [{
            "id": workspace, "ref": "workspace:7", "panes": [{
                "id": pane, "ref": "pane:7", "surfaces": [{
                    "id": surface, "ref": "surface:77", "type": "terminal",
                }],
            }],
        }]}]}
        self.payload = claude_grid_payload()

    @contextlib.contextmanager
    def input_guard(self, check):
        self.check = check
        try:
            yield
        finally:
            self.check = None

    def _input(self, kind, args):
        if self.before_input:
            self.before_input()
        if self.check and not self.check():
            raise watch.InputNotSentError("offline spy: final guard withheld input")
        self.inputs.append((kind, args))
        raise AssertionError("unexpected terminal input: " + kind)

    def send_text(self, *args):
        return self._input("text", args)

    def send_key(self, *args):
        return self._input("key", args)

    def tree(self):
        return self.tree_data

    def top(self, *args):
        return {}

    def replay(self, *args):
        return self.payload


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ccc-supervisor-wait-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.now = datetime.now(timezone.utc).timestamp()
        self.workspace, self.surface, self.supervisor = [str(uuid.uuid4()) for _ in range(3)]
        self.identity = dict(workspace_id=self.workspace, surface_id=self.surface,
                             session_id="session-current", agent_pid=424242,
                             process_generation="424242@current")
        self.projects = self.root / "projects"
        self.native = self.projects / "fixture-project" / "session-current.jsonl"
        self.native.parent.mkdir(parents=True)
        self.native.touch()
        patcher = mock.patch.object(recovery, "PROJECTS_DIR", self.projects)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.target = dict(workspace_id=self.workspace, surface_id=self.surface,
                           ref="surface:77", enabled=True, paused=False)
        self.active = self.root / "active"
        self.releases = self.root / "releases"
        scripts = self.releases / "verified-release/source/scripts"
        scripts.mkdir(parents=True)
        self.helper = scripts / "executor_closeout.py"
        shutil.copyfile(SHARED_BASE / "executor_closeout.py", self.helper)
        self.evidence_io = scripts / "cmux_evidence_io.py"
        shutil.copyfile(SHARED_BASE / self.evidence_io.name, self.evidence_io)
        self.hook = scripts / "cmux_executor_closeout_guard.py"
        shutil.copyfile(SHARED_BASE / self.hook.name, self.hook)
        self.settings = self.root / "claude-settings.json"
        self.setting_doc = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": shlex.join([sys.executable, "-B", str(self.hook)])}
        ]}]}}
        write(self.settings, self.setting_doc)
        self.evidence = adapter.CloseoutEvidence(
            self.settings, releases_root=self.releases, active_root=self.active)
        self.task = self.make_task("task-one", self.now - 120, self.now - 60)
        self.task_root = Path(self.task["binding"]["artifact_root"])
        self.attempt_path = self.task_root / "receipt-attempts/attempt-0001.json"
        self.lock = self.attempt_path.with_name("delivery.lock")
        self.lock.parent.mkdir()
        self.lock.touch()
        pack = self.task["pack"]
        self.attempt = dict(
            binding={**{k: pack[k] for k in ("task_id", "completion_nonce",
                     "completion_callback", "callback_target", "report")},
                     "task_pack_sha256": sha(self.task_root / "task-pack.json"),
                     "report_sha256": sha(pack["report"]),
                     "report_bytes": Path(pack["report"]).stat().st_size,
                     "identity": dict(workspace_uuid=self.workspace,
                                      caller_surface_uuid=self.surface,
                                      target_surface_uuid=self.supervisor,
                                      target_pane_uuid="supervisor-pane")},
            phase="POST_ENTER_OBSERVATION", started_at_epoch=self.now - 40,
            ended_at_epoch=self.now - 30,
            events=[dict(phase="PASTE_INTENT", at_epoch=self.now - 39),
                    dict(phase="POST_ENTER_OBSERVATION", at_epoch=self.now - 31)],
            error="delivery detector unconfirmed", delivery_state="QUEUED")
        write(self.attempt_path, self.attempt)

    def make_task(self, task_id, armed_at, dispatch_at):
        root = self.root / task_id
        root.mkdir()
        report = root / "report.md"
        report.write_text("有界任务报告；主管尚未核收。\n", encoding="utf-8")
        challenge = dict(task_id=task_id, agent="claude:executor", ack_nonce="ack-" + task_id,
                         executor="surface:77", receipt_path=str(root / "preflight.json"))
        ack = "PREFLIGHT_ACK|{}|{}|READY|INLINE|{}".format(
            task_id, challenge["agent"], challenge["ack_nonce"])
        pack = dict(draft=False, task_id=task_id, executor="surface:77",
                    executor_uuid=self.surface, completion_nonce="nonce-" + task_id,
                    callback_target="surface:1", callback=ack, report=str(report),
                    completion_receipt=str(root / "receipt.json"),
                    completion_callback="DONE|{}|nonce-{}|REPORT={}".format(task_id, task_id, report),
                    required_skill="multi-agent-collaboration")
        pack_path = root / "task-pack.json"
        write(pack_path, pack)
        marker = dict(task_id=task_id, workspace_uuid=self.workspace, artifact_root=str(root),
                      armed_at=iso(armed_at), ttl_seconds=3600, participants=[
                          dict(role="supervisor", surface_ref="surface:1", surface_uuid=self.supervisor),
                          dict(role="executor", surface_uuid=self.surface)])
        marker_path = self.active / self.workspace / (task_id + ".json")
        write(marker_path, marker)
        dispatch = dict(task_id=task_id, marker=pack["completion_nonce"], task_pack=str(pack_path),
                        callback_target="surface:1", required_skill=pack["required_skill"],
                        completion_tail=pack["completion_callback"])
        binding = {}
        self.assertTrue(watch.WatchDaemon._claude_dispatch_matches(
            challenge, dispatch, self.target, binding_out=binding))
        binding.update(self.identity, dispatch_event_id="dispatch-" + task_id,
                       dispatch_created_at=dispatch_at)
        return dict(pack=pack, challenge=challenge, dispatch=dispatch, binding=binding,
                    marker=marker, marker_path=marker_path)

    def returned(self, binding=None, identity=None, **kwargs):
        return self.evidence.returned(
            self.task["binding"] if binding is None else binding,
            self.identity if identity is None else identity, **kwargs)

    def event(self, name="Stop", offset=-20, **changes):
        text = str(changes.pop("message", changes.get("prompt") or name))
        value = dict(self.identity, version=1, event_id=str(uuid.uuid4()), event_name=name,
                     created_at=self.now + offset, completed=False, stop_hook_active=False,
                     error_kind="claude_stopped", message_hash=recovery.digest(text))
        value.update(changes)
        return value

    def prompt(self, text="Continue the native Goal until complete", offset=-10,
               *, native_changes=None, **changes):
        event = self.event("UserPromptSubmit", offset=offset, message=text,
                           prompt_kind="human", transcript_id=recovery.digest(str(self.native)))
        event.update(changes)
        row = dict(type="user", sessionId=self.identity["session_id"], isSidechain=False,
                   uuid=str(uuid.uuid4()), timestamp=iso(self.now + offset),
                   promptSource="typed", origin={"kind": "human"},
                   message={"role": "user", "content": text})
        row.update(native_changes or {})
        with self.native.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
        return event


class EvidenceTests(Fixture):
    def test_original_return_uses_real_validator_and_keeps_completion_separate(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.returned(event=self.event())
        self.assertEqual(result["status"], adapter.WAITING)
        self.assertEqual(result["deadline_at"], self.attempt["ended_at_epoch"] + 300)
        self.assertEqual(result["validator"]["sha256"], sha(self.helper))
        self.assertEqual(result["validator"]["evidence_io_sha256"], sha(self.evidence_io))
        self.assertNotIn("completed", result)
        self.assertFalse((self.task_root / "receipt.json").exists())
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_validator_dependency_is_bound_to_release_and_revalidated(self):
        # 缓存命中仍须重读依赖；不能用别的 release 的已导入模块代替。
        self.assertIsNotNone(self.returned())
        original = self.evidence_io.read_bytes()
        self.evidence_io.unlink()
        self.assertIsNone(self.returned())
        self.evidence_io.write_bytes(original)
        self.assertIsNotNone(self.returned())
        self.evidence_io.write_text('syntax error in replaced dependency!')
        self.assertIsNone(self.returned())
        self.evidence_io.write_bytes(original)
        self.assertIsNotNone(self.returned())

    def test_validator_dependency_rejects_symlink_and_nonregular_file(self):
        original = self.evidence_io.read_bytes()
        other = self.root / 'evidence-copy.py'
        other.write_bytes(original)
        self.evidence_io.unlink()
        self.evidence_io.symlink_to(other)
        self.assertIsNone(self.returned())
        self.evidence_io.unlink()
        os.mkfifo(self.evidence_io)
        # 外层测试进程超时兜底，打开 FIFO 必须以 O_NONBLOCK 立即拒绝。
        self.assertIsNone(self.returned())

    def test_report_ready_text_without_callback_is_not_evidence(self):
        self.attempt_path.unlink()
        self.assertIsNone(self.returned(event=self.event(report_ready_task_id="task-one")))

    def test_missing_or_changed_binding_rejected(self):
        for key in ("task_id", "completion_nonce", "task_pack_sha256", "artifact_root",
                    "dispatch_event_id"):
            with self.subTest(key=key):
                bad = dict(self.task["binding"], **{key: "" if key == "dispatch_event_id" else "wrong"})
                self.assertIsNone(self.returned(binding=bad))

    def test_runtime_and_event_identity_must_match_all_native_dimensions(self):
        for key in adapter.IDENTITY_KEYS:
            with self.subTest(key=key):
                bad = dict(self.identity, **{key: 999 if key == "agent_pid" else "wrong"})
                self.assertIsNone(self.returned(identity=bad))
                self.assertIsNone(self.returned(event=self.event(**{key: bad[key]})))
        self.assertIsNone(self.returned(event=self.event(synthetic_fallback=True)))

    def test_stale_future_and_malformed_dispatch_times_rejected(self):
        for stamp in (self.now + 100, self.now - 130, self.now - 20, True, float("nan"), 0):
            with self.subTest(stamp=stamp):
                self.assertIsNone(self.returned(binding=dict(
                    self.task["binding"], dispatch_created_at=stamp)))

    def test_future_callback_return_rejected(self):
        self.attempt["ended_at_epoch"] = self.now + 100
        write(self.attempt_path, self.attempt)
        self.assertIsNone(self.returned())

    def test_original_exclusive_delivery_lock_excludes_inflight_and_preserves_attempt(self):
        original = self.attempt_path.read_bytes()
        with self.lock.open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIsNone(self.returned())
        self.assertIsNotNone(self.returned())
        self.assertEqual(original, self.attempt_path.read_bytes())

    def test_changed_report_pack_and_attempt_pins_rejected(self):
        for path in (self.task_root / "report.md", self.task_root / "task-pack.json"):
            original = path.read_bytes()
            with self.subTest(path=path.name):
                path.write_bytes(original + b" ")
                self.assertIsNone(self.returned())
                path.write_bytes(original)
        for key in ("completion_nonce", "task_pack_sha256", "report_sha256"):
            bad = copy.deepcopy(self.attempt)
            bad["binding"][key] = "wrong"
            write(self.attempt_path, bad)
            self.assertIsNone(self.returned())

    def test_marker_ttl_v1_and_v2(self):
        v2 = self.task["marker_path"]
        v1 = self.active / (self.workspace + ".json")
        v2.rename(v1)
        self.assertIsNotNone(self.returned())
        for updates in ({"ttl_seconds": 1}, {"armed_at": iso(self.now + 10)},
                        {"armed_at": iso(self.now - 50)}, {"ttl_seconds": True}):
            write(v1, dict(self.task["marker"], **updates))
            self.assertIsNone(self.returned())

    def test_only_installed_exact_trusted_hook_can_select_helper(self):
        self.assertIsNotNone(self.evidence._validator())
        hook = self.setting_doc["hooks"]["PreToolUse"][0]["hooks"][0]
        original = hook["command"]
        for command in (original + " --extra", "echo " + str(self.hook),
                        shlex.join([sys.executable, "-B", str(self.root / self.hook.name)])):
            hook["command"] = command
            write(self.settings, self.setting_doc)
            self.assertIsNone(self.evidence._validator())
        hook["command"] = original
        write(self.settings, self.setting_doc)
        self.helper.chmod(0o666)
        self.assertIsNone(self.evidence._validator())
        self.helper.chmod(0o644)
        saved = self.helper.with_suffix(".saved")
        self.helper.rename(saved)
        self.helper.symlink_to(saved)
        self.assertIsNone(self.evidence._validator())

    def test_prompt_cannot_supply_code_path_and_module_change_is_rechecked(self):
        payload = self.root / "prompt-code.py"
        payload.write_text("raise RuntimeError('MUST NOT EXECUTE')\n")
        binding = dict(self.task["binding"], validator_path=str(payload), helper_path=str(payload))
        self.assertIsNotNone(self.returned(binding=binding))
        self.helper.write_text("def terminal_report(*a): return None\n"
                               "def superseded(*a, **k): return False\n")
        self.assertIsNone(self.returned())

    def test_timeout_is_fixed_and_never_becomes_completion(self):
        original = self.returned()
        late = adapter.wait_state(original, original["deadline_at"] + 1)
        self.assertEqual(late["status"], adapter.TIMED_OUT)
        self.assertEqual(late["deadline_at"], original["deadline_at"])
        self.assertEqual(adapter.wait_state(late, self.now)["status"], adapter.TIMED_OUT)
        self.assertNotIn("completed", late)

    def test_only_same_supervisor_new_task_new_root_dispatch_releases(self):
        wait = self.returned()
        later = self.make_task("task-two", self.now - 10, self.now - 5)
        self.assertTrue(self.evidence.admits_successor(wait, later["binding"]))
        self.assertFalse(self.evidence.admits_successor(wait, self.task["binding"]))
        for delta in ({"task_id": "unrelated"}, {"artifact_root": str(self.task_root)},
                      {"callback_target": "surface:99"}, {"dispatch_created_at": self.now - 15},
                      {"workspace_id": str(uuid.uuid4())}, {"surface_id": str(uuid.uuid4())}):
            self.assertFalse(self.evidence.admits_successor(wait, dict(later["binding"], **delta)))
        marker = copy.deepcopy(later["marker"])
        marker["participants"][0]["surface_uuid"] = str(uuid.uuid4())
        write(later["marker_path"], marker)
        self.assertFalse(self.evidence.admits_successor(wait, later["binding"]))

    def test_compatible_shared_patch_preserves_honest_closeout(self):
        module, _ = self.evidence._validator()
        evidence = self.returned()["evidence"]
        line = module.handoff_line(evidence)
        self.assertTrue(module.honest_closeout(line, evidence))
        self.assertTrue(module.honest_closeout(line + "\n" + module.COMPLETION_SENTENCE, evidence))
        self.assertFalse(module.honest_closeout(line + "\nextra", evidence))


class WatcherTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client = RefuseInput(self.workspace, self.surface, str(uuid.uuid4()))
        delivery_root = mock.patch.object(watch, "DELIVERY_LOCK_ROOT", self.root / "deliveries")
        delivery_root.start()
        self.addCleanup(delivery_root.stop)
        self.config_path, self.state_path = self.root / "config.json", self.root / "state.json"
        config = watch.default_config()
        config.update(mode="armed", claude_enabled=True, targets=[self.target])
        write(self.config_path, config)
        self.daemon = watch.WatchDaemon(config_path=self.config_path, state_path=self.state_path,
                                       client=self.client)
        self.daemon.claude_closeout = self.evidence
        self.runtime = watch.TargetRuntime()
        self.runtime.claude_session_id = self.identity["session_id"]
        self.runtime.claude_process_pid = self.identity["agent_pid"]
        self.runtime.claude_process_generation = self.identity["process_generation"]
        self.runtime.claude_hook_process_generation = self.identity["process_generation"]
        self.runtime.claude_hook_health = "healthy"
        self.runtime.claude_turn_started_at = self.now - 100
        self.runtime.claude_last_hook_at = self.now - 90
        self.runtime.claude_task_binding = copy.deepcopy(self.task["binding"])
        self.daemon.runtime[self.surface] = self.runtime
        self.stopped = watch.ScreenState("claude_stopped", message_kind="claude",
                                         content_fingerprint="stable-content")
        self.composer = watch.ScreenState("composer_busy", message_kind="claude", watchdog_echo=True)

    def handle(self, event):
        self.daemon._handle_claude_event_locked(event, self.client)
        return self.daemon.claude_event_ledger.status_of(event["event_id"])

    def wait(self):
        self.assertEqual(self.daemon._claude_supervisor_wait_guard(self.target, self.runtime),
                         adapter.WAITING)
        return copy.deepcopy(self.runtime.claude_supervisor_wait)

    def test_native_stop_enters_wait_without_completion_or_input(self):
        self.assertEqual(self.handle(self.event()), "suppressed_waiting_supervisor")
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertIsNone(self.runtime.claude_completion_event_id)
        self.assertEqual(self.client.inputs, [])

    def test_stop_recursive_stop_failure_and_goal_text_are_zero_input(self):
        wait = self.wait()
        for i, (name, values) in enumerate([
            ("Stop", {}), ("Stop", {"stop_hook_active": True}),
            ("StopFailure", {}),
        ]):
            self.assertEqual(self.handle(self.event(name, offset=-19 + i, **values)),
                             "suppressed_waiting_supervisor")
        self.assertEqual(self.runtime.claude_supervisor_wait, wait)
        self.assertEqual(self.handle(self.prompt(offset=-16)), "human_prompt")
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertIsNone(self.runtime.claude_task_binding)
        self.assertEqual(self.client.inputs, [])

    def test_human_between_callback_end_and_observation_persists_audited_takeover(self):
        wait = self.wait()
        original_binding = copy.deepcopy(self.runtime.claude_task_binding)
        original_attempt = self.attempt_path.read_bytes()
        event = self.prompt(offset=-10)
        self.assertLess(wait["evidence"]["ended_at_epoch"], event["created_at"])
        self.assertLess(event["created_at"], wait["observed_at"])
        self.runtime.claude_completed_latched = True
        self.runtime.claude_handshake_wait = True
        self.runtime.send_count = 4
        self.assertEqual(self.handle(event), "human_prompt")
        audit = self.runtime.claude_supervisor_human_takeover
        self.assertEqual(audit["wait"], wait)
        self.assertEqual(audit["task_binding"], original_binding)
        self.assertEqual(audit["event"], event)
        self.assertEqual(audit["native_prompt"]["sha256"], sha(self.native))
        self.assertEqual(audit["native_prompt"]["message_hash"], event["message_hash"])
        self.assertEqual(audit["native_prompt"]["path"], str(self.native))
        self.assertEqual(audit["callback_ended_at"], self.attempt["ended_at_epoch"])
        self.assertEqual(audit["sha256"], adapter.snapshot_sha256(
            {key: value for key, value in audit.items() if key != "sha256"}))
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertFalse(self.runtime.claude_handshake_wait)
        self.assertEqual(self.runtime.send_count, 0)
        # No wrapper save: the locked handler itself must commit this boundary.
        restarted = watch.WatchDaemon(config_path=self.config_path, state_path=self.state_path,
                                      client=self.client)
        runtime = restarted.runtime[self.surface]
        self.assertEqual(runtime.claude_supervisor_human_takeover, audit)
        self.assertIsNone(runtime.claude_supervisor_wait)
        self.assertIsNone(runtime.claude_task_binding)
        self.assertEqual(restarted.claude_event_ledger.events[event["event_id"]][
            "supervisor_human_takeover"], audit)
        restarted.claude_closeout = self.evidence
        self.assertIsNone(restarted._claude_supervisor_wait_guard(self.target, runtime))
        old_stop = self.event(offset=-20)
        restarted._handle_claude_event_locked(old_stop, self.client)
        self.assertEqual(restarted.claude_event_ledger.status_of(old_stop["event_id"]),
                         "stale_native_event")
        self.assertIsNone(restarted._claude_supervisor_wait_guard(self.target, runtime))
        self.assertEqual(self.attempt_path.read_bytes(), original_attempt)
        self.assertFalse((self.task_root / "receipt.json").exists())
        self.assertEqual(self.client.inputs, [])

    def test_fresh_human_discovers_unobserved_wait_and_can_override_timeout(self):
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out):
                self.runtime.claude_task_binding = copy.deepcopy(self.task["binding"])
                self.runtime.claude_supervisor_wait = None
                self.runtime.claude_last_hook_at = self.now - 90
                self.runtime.claude_turn_started_at = self.now - 100
                self.native.write_text("")
                if timed_out:
                    self.wait()
                    self.runtime.claude_supervisor_wait.update(
                        status=adapter.TIMED_OUT, deadline_at=self.now - 1)
                self.assertEqual(self.handle(self.prompt()), "human_prompt")
                self.assertIsNone(self.runtime.claude_supervisor_wait)
                self.assertIsNone(self.runtime.claude_task_binding)
        self.assertEqual(self.client.inputs, [])

    def test_hook_and_native_time_must_both_follow_callback_end_and_watermark(self):
        wait = self.wait()
        watermark = self.runtime.claude_last_hook_at
        for hook_offset, native_offset in ((-31, -31), (-30, -30), (-29, -30),
                                           (-30, -29), (-10, -14), (-10, 5)):
            with self.subTest(hook=hook_offset, native=native_offset):
                self.native.write_text("")
                event = self.prompt(offset=hook_offset,
                                    native_changes={"timestamp": iso(self.now + native_offset)})
                self.assertEqual(self.handle(event), "suppressed_waiting_supervisor")
                self.assertEqual(self.runtime.claude_supervisor_wait, wait)
                self.assertEqual(self.runtime.claude_last_hook_at, watermark)
        for stamp in (self.now + 10, float("nan"), float("inf"), True, self.now - 100):
            with self.subTest(hook_stamp=stamp):
                self.assertEqual(self.handle(self.prompt(created_at=stamp)), "stale_native_event")
                self.assertEqual(self.runtime.claude_supervisor_wait, wait)
        self.runtime.claude_last_hook_at = self.now - 8
        for hook_offset, native_offset in ((-8, -8), (-7, -8), (-10, -10)):
            with self.subTest(watermark_hook=hook_offset):
                self.native.write_text("")
                status = self.handle(self.prompt(offset=hook_offset,
                    native_changes={"timestamp": iso(self.now + native_offset)}))
                self.assertIn(status, ("suppressed_waiting_supervisor", "stale_native_event"))
                self.assertEqual(self.runtime.claude_last_hook_at, self.now - 8)
        self.assertEqual(self.client.inputs, [])

    def test_human_label_needs_native_message_hash_and_source(self):
        wait = self.wait()
        watermark = self.runtime.claude_last_hook_at
        for changes in ({}, {"transcript_id": "wrong"}, {"message_hash": "wrong"},
                        {"message_hash": None}, {"message_hash": "f" * 24},
                        {"message_hash": recovery.digest("different native text")},
                        {"transcript_path": str(self.native), "transcript_id": None}):
            with self.subTest(changes=changes):
                self.native.write_text("")
                event = self.prompt(**changes) if changes else self.event(
                    "UserPromptSubmit", offset=-10, prompt_kind="human")
                self.assertEqual(self.handle(event), "suppressed_waiting_supervisor")
                self.assertEqual(self.runtime.claude_supervisor_wait, wait)
                self.assertIsNone(self.runtime.claude_supervisor_human_takeover)
                self.assertEqual(self.runtime.claude_last_hook_at, watermark)
        self.assertEqual(self.client.inputs, [])

    def test_human_identity_and_synthetic_fallback_cannot_release_old_wait(self):
        wait = self.wait()
        watermark = self.runtime.claude_last_hook_at
        for key in adapter.IDENTITY_KEYS:
            for value in (None, 99 if key == "agent_pid" else "wrong"):
                with self.subTest(key=key, value=value):
                    self.native.write_text("")
                    event = self.prompt(**{key: value})
                    with mock.patch.object(watch, "inspect_claude_process", return_value={}):
                        self.assertNotEqual(self.handle(event), "human_prompt")
                    self.assertEqual(self.runtime.claude_supervisor_wait, wait)
                    self.assertEqual(self.runtime.claude_last_hook_at, watermark)
        for observed in (True, False):
            with self.subTest(observed_wait=observed):
                self.runtime.claude_supervisor_wait = wait if observed else None
                self.native.write_text("")
                self.assertEqual(self.handle(self.prompt(synthetic_fallback=True)),
                                 "suppressed_waiting_supervisor")
                self.assertIsNotNone(self.runtime.claude_supervisor_wait)
                self.assertEqual(self.runtime.claude_task_binding, self.task["binding"])
                self.assertIsNone(self.runtime.claude_supervisor_human_takeover)
        self.assertEqual(self.client.inputs, [])

    def test_missing_human_generation_is_derived_only_from_current_process(self):
        self.wait()
        event = self.prompt()
        event.pop("process_generation")
        with mock.patch.object(watch, "inspect_claude_process", return_value={
                "generation": self.identity["process_generation"], "started_epoch": self.now - 500}):
            self.assertEqual(self.handle(event), "human_prompt")
        self.assertEqual(self.runtime.claude_supervisor_human_takeover["event"]["process_generation"],
                         self.identity["process_generation"])
        self.assertEqual(self.client.inputs, [])

    def test_native_origin_role_and_timestamp_are_independent_human_evidence(self):
        wait = self.wait()
        cases = [{"isSidechain": True}, {"isSidechain": None}, {"sessionId": "other"},
                 {"isMeta": True}, {"isCompactSummary": True},
                 {"isVisibleInTranscriptOnly": True}, {"isSynthetic": True}, {"uuid": ""},
                 {"promptSource": "tool"}, {"origin": {"kind": "agent"}}, {"origin": "human"},
                 {"timestamp": None}, {"timestamp": "not a date"},
                 {"timestamp": datetime.fromtimestamp(self.now - 10).isoformat()},
                 {"message": {"role": "assistant", "content": "Continue the native Goal until complete"}},
                 {"message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
                 {"message": {"role": "user", "content": [
                     {"type": "text", "text": "Continue the native Goal until complete"},
                     {"type": "tool_result", "content": "ok"}]}}]
        for changes in cases:
            with self.subTest(native=changes):
                self.native.write_text("")
                self.assertEqual(self.handle(self.prompt(native_changes=changes)),
                                 "suppressed_waiting_supervisor")
                self.assertEqual(self.runtime.claude_supervisor_wait, wait)
                self.assertEqual(self.runtime.claude_task_binding, self.task["binding"])
        self.assertEqual(self.client.inputs, [])

    def test_native_protocol_replay_cannot_be_labeled_human(self):
        wait = self.wait()
        for text in ("DELIVERY_NONCE=old-protocol", "TASK_DISPATCH old-marker",
                     "TASK_PACK=/tmp/old/task-pack.json", "READ_AND_OBEY_REQUIRED_SKILL_FIRST",
                     "PREFLIGHT_ACK|task-one|claude:identity|READY|INLINE|old-nonce",
                     self.task["pack"]["completion_callback"]):
            with self.subTest(text=text):
                self.native.write_text("")
                self.assertEqual(self.handle(self.prompt(text, collaboration_protocol=False)),
                                 "suppressed_waiting_supervisor")
                self.assertEqual(self.runtime.claude_supervisor_wait, wait)
                self.assertIsNone(self.runtime.claude_supervisor_human_takeover)
        self.assertEqual(self.client.inputs, [])

    def test_old_identical_prompt_does_not_hide_fresh_native_prompt(self):
        self.wait()
        self.prompt("continue", offset=-70)
        fresh = self.prompt("continue", offset=-10)
        self.assertEqual(self.handle(fresh), "human_prompt")
        self.assertEqual(self.runtime.claude_supervisor_human_takeover["event"]["event_id"],
                         fresh["event_id"])
        self.assertEqual(self.client.inputs, [])

    def test_only_latest_unambiguous_native_prompt_can_take_over(self):
        wait = self.wait()
        older = self.prompt("New Goal one", offset=-12)
        later = self.prompt("New Goal two", offset=-10)
        self.assertEqual(self.handle(older), "suppressed_waiting_supervisor")
        self.assertEqual(self.runtime.claude_supervisor_wait, wait)
        self.assertEqual(self.handle(later), "human_prompt")
        self.assertEqual(self.client.inputs, [])

    def test_ambiguous_or_malformed_native_records_grant_no_takeover(self):
        wait = self.wait()
        for suffix in ("duplicate", "null\n", "{broken json\n"):
            with self.subTest(suffix=suffix):
                self.native.write_text("")
                event = self.prompt()
                original = self.native.read_text()
                with self.native.open("a") as stream:
                    stream.write(original if suffix == "duplicate" else suffix)
                self.assertEqual(self.handle(event), "suppressed_waiting_supervisor")
                self.assertEqual(self.runtime.claude_supervisor_wait, wait)
        self.assertEqual(self.client.inputs, [])

    def test_untrusted_native_path_cannot_supply_human_evidence(self):
        wait = self.wait()
        event = self.prompt()
        raw = self.native.read_bytes()
        other = self.root / "untrusted-transcript.jsonl"
        other.write_bytes(raw)
        self.native.unlink()
        self.native.symlink_to(other)
        self.assertEqual(self.handle(event), "suppressed_waiting_supervisor")
        self.native.unlink()
        os.mkfifo(self.native)
        self.assertEqual(self.handle(dict(event, event_id=str(uuid.uuid4()))),
                         "suppressed_waiting_supervisor")
        self.assertEqual(self.runtime.claude_supervisor_wait, wait)
        self.assertEqual(self.client.inputs, [])

    def test_verified_human_cancels_unknown_submit_without_confirming_it(self):
        self.wait()
        self.runtime.claude_submit_phase = "text_written"
        self.runtime.claude_submit_event_id = "original-unknown"
        self.runtime.claude_submit_write_unknown = True
        original = self.event(event_id="original-unknown")
        self.daemon.claude_event_ledger.mark(original, "reserved")
        event = self.prompt()
        self.assertEqual(self.handle(event), "human_prompt")
        self.assertEqual(self.runtime.claude_submit_phase, "none")
        self.assertEqual(self.runtime.claude_submit_last_reason, "human_prompt_override")
        self.assertEqual(self.runtime.claude_submit_confirmed_at, 0)
        self.assertEqual(self.daemon.claude_event_ledger.status_of("original-unknown"), "reserved")
        pending = self.runtime.claude_supervisor_human_takeover["pending_submit"]
        self.assertEqual(pending["claude_submit_event_id"], "original-unknown")
        self.assertTrue(pending["claude_submit_write_unknown"])
        self.assertEqual(self.client.inputs, [])

    def test_pending_watchdog_native_echo_does_not_cancel_unknown_write(self):
        wait = self.wait()
        self.runtime.claude_submit_phase = "text_written"
        self.runtime.claude_submit_event_id = "original-unknown"
        self.runtime.claude_submit_write_unknown = True
        for text, pending_hash in ((watch.CLAUDE_MESSAGE, None),
                                   ("previous custom watchdog", recovery.digest("previous custom watchdog"))):
            with self.subTest(text=text):
                self.native.write_text("")
                self.runtime.claude_submit_message_hash = pending_hash
                self.assertEqual(self.handle(self.prompt(text, prompt_kind="human")),
                                 "suppressed_waiting_supervisor")
                self.assertEqual(self.runtime.claude_supervisor_wait, wait)
                self.assertEqual(self.runtime.claude_submit_event_id, "original-unknown")
                self.assertTrue(self.runtime.claude_submit_write_unknown)
                self.assertEqual(self.runtime.claude_submit_confirmed_at, 0)
        self.assertEqual(self.client.inputs, [])

    def test_durable_and_legacy_watchdog_echo_checks_are_read_only_during_wait(self):
        wait = self.wait()
        r = self.runtime
        text = "previous configured watchdog"
        r.claude_last_submit_event_id = "original-sent"
        r.claude_last_submit_at = self.now - 12
        r.claude_last_submit_message_hash = watch._short_hash(text)
        r.claude_last_submit_session_id = self.identity["session_id"]
        r.claude_last_submit_generation = self.identity["process_generation"]
        self.daemon.claude_event_ledger.mark(self.event(event_id="original-sent"), "sent")
        receipt = {"surface_id": self.surface, "session_id": self.identity["session_id"],
                   "agent_pid": self.identity["agent_pid"], "generation": self.identity["process_generation"],
                   "turn_id": r.claude_generation_id, "turn_started_at": r.claude_turn_started_at,
                   "message_hash": r.claude_last_submit_message_hash, "submitted_at": r.claude_last_submit_at}
        for durable, observed, kind in ((d, o, k) for d in (False, True)
                                       for o in (False, True) for k in ("human", "watchdog")):
            with self.subTest(durable=durable, observed=observed, kind=kind):
                self.native.write_text("")
                row = self.daemon.claude_event_ledger.events["original-sent"]
                row.pop("watchdog_submission", None)
                r.claude_last_submit_has_receipt = durable
                r.claude_supervisor_wait = copy.deepcopy(wait) if observed else None
                self.daemon.config["claude_watchdog_echo_window_sec"] = 0.1 if durable else 30
                if durable:
                    row["watchdog_submission"] = copy.deepcopy(receipt)
                original = copy.deepcopy(row)
                self.assertEqual(self.handle(self.prompt(text, prompt_kind=kind)),
                                 "suppressed_waiting_supervisor")
                self.assertEqual(self.daemon.claude_event_ledger.events["original-sent"], original)
                self.assertEqual(r.claude_supervisor_wait["binding"], wait["binding"])
                self.assertEqual(r.claude_supervisor_wait["evidence"], wait["evidence"])
                self.assertEqual(r.claude_supervisor_wait["deadline_at"], wait["deadline_at"])
                self.assertIsNone(r.claude_supervisor_human_takeover)
        self.assertEqual(self.client.inputs, [])

    def test_audit_write_failure_keeps_wait_and_binding(self):
        wait = self.wait()
        event = self.prompt()
        writer = watch.atomic_write_json

        def fail_audit(path, data, *args, **kwargs):
            if "supervisor_human_takeover" in data.get("events", {}).get(event["event_id"], {}):
                raise OSError("injected audit write failure")
            return writer(path, data, *args, **kwargs)

        with mock.patch.object(watch, "atomic_write_json", side_effect=fail_audit):
            with self.assertRaises(OSError):
                self.handle(event)
        self.assertEqual(self.runtime.claude_supervisor_wait, wait)
        self.assertEqual(self.runtime.claude_task_binding, self.task["binding"])
        self.assertIsNone(self.runtime.claude_supervisor_human_takeover)
        self.assertNotIn("supervisor_human_takeover", self.daemon.claude_event_ledger.events[event["event_id"]])
        self.assertEqual(self.client.inputs, [])

    def test_runtime_save_failure_restores_wait_binding_and_unknown_submit(self):
        wait = self.wait()
        self.runtime.claude_submit_phase = "text_written"
        self.runtime.claude_submit_event_id = "original-unknown"
        self.runtime.claude_submit_write_unknown = True
        self.daemon.save()
        previous = self.runtime.to_dict()
        original_state = self.state_path.read_bytes()
        event = self.prompt()
        with mock.patch.object(self.daemon, "save", side_effect=OSError("injected state write failure")):
            with self.assertRaises(OSError):
                self.handle(event)
        self.assertEqual(self.runtime.to_dict(), previous)
        self.assertEqual(self.state_path.read_bytes(), original_state)
        audit = self.daemon.claude_event_ledger.events[event["event_id"]]["supervisor_human_takeover"]
        self.assertEqual(audit["wait"], wait)
        self.assertEqual(audit["task_binding"], self.task["binding"])
        self.assertTrue(audit["pending_submit"]["claude_submit_write_unknown"])
        self.assertEqual(json.loads(self.daemon.claude_event_ledger.path.read_text())[
            "events"][event["event_id"]]["supervisor_human_takeover"], audit)
        self.assertEqual(self.client.inputs, [])

    def test_native_source_change_after_audit_keeps_wait_and_binding(self):
        wait = self.wait()
        event = self.prompt()
        writer = watch.atomic_write_json
        changed = []

        def append_after_audit(path, data, *args, **kwargs):
            result = writer(path, data, *args, **kwargs)
            if (not changed and "supervisor_human_takeover"
                    in data.get("events", {}).get(event["event_id"], {})):
                changed.append(self.prompt("An even newer native Goal", offset=-5))
            return result

        with mock.patch.object(watch, "atomic_write_json", side_effect=append_after_audit):
            self.assertEqual(self.handle(event), "suppressed_waiting_supervisor")
        self.assertTrue(changed)
        self.assertEqual(self.runtime.claude_supervisor_wait, wait)
        self.assertEqual(self.runtime.claude_task_binding, self.task["binding"])
        self.assertIsNone(self.runtime.claude_supervisor_human_takeover)
        self.assertEqual(self.handle(changed[0]), "human_prompt")
        self.assertEqual(self.client.inputs, [])

    def test_pending_unknown_submit_is_not_confirmed_or_cleared(self):
        self.runtime.claude_submit_phase = "text_written"
        self.runtime.claude_submit_event_id = "original-pending"
        self.runtime.claude_submit_write_unknown = True
        for i, name in enumerate(("Stop", "StopFailure", "UserPromptSubmit")):
            self.handle(self.event(name, offset=-18 + i, prompt_kind="watchdog"))
        self.assertEqual(self.runtime.claude_submit_phase, "text_written")
        self.assertEqual(self.runtime.claude_submit_event_id, "original-pending")
        self.assertTrue(self.runtime.claude_submit_write_unknown)
        self.assertEqual(self.client.inputs, [])

    def test_all_send_and_recovery_origins_honor_wait(self):
        d, r, t = self.daemon, self.runtime, self.target
        self.wait()
        event = self.event()
        r.claude_deferred_event = dict(event)
        r.claude_deferred_reason = "expired"
        r.claude_submit_phase = "text_written"
        r.claude_submit_event_id = "pending"
        r.claude_submit_write_unknown = True
        self.assertFalse(d._send_claude_event(event, t, r, self.client)[0])
        self.assertFalse(d._send_claude_enter(t, r, self.client, reason="test"))
        self.assertTrue(d._recover_orphan_watchdog_submit(t, r, self.composer, self.client))
        self.assertTrue(d._reconcile_claude_submit(t, r, self.composer, self.client))
        self.assertTrue(d._maybe_send_deferred_claude_stop(t, r, self.stopped, self.client))
        self.assertFalse(d._restore_expired_claude_deferred(r))
        self.assertTrue(d._maybe_send_claude_hook_gap_fallback(t, r, self.stopped, {}, self.client))
        self.assertEqual(d._prepare_claude_send(t, r, self.stopped, self.client, {}, ""), (None, {}))
        self.assertFalse(d._recover_claude_report_stop(self.surface, r))
        self.assertFalse(d._claude_report_source_current(event, t, r))
        r.claude_completed_latched = True
        self.assertFalse(d._reopen_claude_completion_from_hook(r, self.event(stop_hook_active=True), t))
        d._defer_claude_event(self.surface, r, self.event(offset=-10), "busy")
        self.assertEqual(r.claude_deferred_event, event)
        self.assertEqual(r.claude_submit_event_id, "pending")
        self.assertTrue(r.claude_submit_write_unknown)
        self.assertEqual(self.client.inputs, [])

    def test_native_identity_failures_do_not_advance_watermark(self):
        previous = self.runtime.claude_last_hook_at
        for key in ("session_id", "agent_pid", "process_generation", "workspace_id"):
            event = self.event(**{key: 999 if key == "agent_pid" else "wrong"})
            self.assertEqual(self.handle(event), "task_identity_unverified")
            self.assertEqual(self.runtime.claude_last_hook_at, previous)
            self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertEqual(self.client.inputs, [])

    def test_missing_native_generation_requires_fresh_process_identity(self):
        event = self.event()
        event.pop("process_generation")
        with mock.patch.object(watch, "inspect_claude_process", return_value={
                "generation": self.identity["process_generation"], "started_epoch": self.now - 500}):
            self.assertEqual(self.handle(event), "suppressed_waiting_supervisor")

    def test_restart_retains_wait_and_timeout_without_revalidation_or_input(self):
        wait = self.wait()
        self.daemon.save()
        restarted = watch.WatchDaemon(config_path=self.config_path, state_path=self.state_path,
                                      client=self.client)
        runtime = restarted.runtime[self.surface]
        self.assertEqual(runtime.claude_supervisor_wait, wait)
        restarted.claude_closeout = mock.Mock()
        with mock.patch.object(adapter.time, "time", return_value=wait["deadline_at"] + 10):
            self.assertEqual(restarted._claude_supervisor_wait_guard(self.target, runtime),
                             adapter.TIMED_OUT)
        restarted.claude_closeout.returned.assert_not_called()
        self.assertEqual(runtime.claude_supervisor_wait["deadline_at"], wait["deadline_at"])
        self.assertFalse(runtime.claude_completed_latched)
        self.assertFalse(restarted._send_claude_event(self.event(), self.target, runtime, self.client)[0])
        self.assertEqual(self.client.inputs, [])

    def test_process_replacement_drops_task_binding_but_keeps_wait(self):
        wait = self.wait()
        self.daemon._apply_claude_process_observation(self.runtime, {
            "pid": 525252, "generation": "525252@replacement", "started_epoch": self.now})
        self.assertIsNone(self.runtime.claude_task_binding)
        self.assertEqual(self.runtime.claude_supervisor_wait, wait)
        self.assertFalse(self.daemon._send_claude_event(
            self.event(), self.target, self.runtime, self.client)[0])

    def handshake(self, task, start):
        challenge = self.event("UserPromptSubmit", offset=start, collaboration_protocol=True,
                               handshake_challenge=task["challenge"], prompt_kind="human")
        ack = self.event("Stop", offset=start + 1, collaboration_protocol=True,
                         handshake_ack={k: task["challenge"][k] for k in ("task_id", "agent", "ack_nonce")})
        dispatch = self.event("UserPromptSubmit", offset=start + 2, collaboration_protocol=True,
                              task_dispatch=task["dispatch"], prompt_kind="human")
        return self.handle(challenge), self.handle(ack), self.handle(dispatch)

    def test_verified_successor_full_native_handshake_releases_without_input(self):
        self.wait()
        self.runtime.claude_submit_phase = "text_written"
        self.runtime.claude_submit_event_id = "old-pending"
        self.runtime.claude_submit_write_unknown = True
        later = self.make_task("task-two", self.now - 10, self.now - 3)
        self.assertEqual(self.handshake(later, -5),
                         ("handshake_challenge", "handshake_wait", "task_dispatch"))
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertEqual(self.runtime.claude_task_binding["task_id"], "task-two")
        self.assertEqual(self.runtime.claude_submit_phase, "none")
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertEqual(self.client.inputs, [])

    def test_same_task_replay_and_unrelated_marker_do_not_release(self):
        wait = self.wait()
        self.make_task("unrelated", self.now - 10, self.now - 3)
        self.assertEqual(self.handshake(self.task, -5)[-1], "suppressed_waiting_supervisor")
        self.assertEqual(self.runtime.claude_supervisor_wait, wait)
        self.assertEqual(self.client.inputs, [])

    def test_true_completion_keeps_existing_latch_and_does_not_settle_callback(self):
        self.wait()
        event = self.event(completed=True)
        self.assertEqual(self.handle(event), "completed")
        self.assertTrue(self.runtime.claude_completed_latched)
        self.assertEqual(self.runtime.claude_completion_event_id, event["event_id"])
        self.assertIsNotNone(self.runtime.claude_supervisor_wait)
        self.assertFalse((self.task_root / "receipt.json").exists())
        self.assertEqual(self.client.inputs, [])

    def test_plain_handshake_without_closeout_still_dispatches(self):
        self.attempt_path.unlink()
        self.runtime.claude_task_binding = None
        self.assertEqual(self.handshake(self.task, -65),
                         ("handshake_challenge", "handshake_wait", "task_dispatch"))
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertEqual(self.runtime.claude_task_binding["task_id"], "task-one")

    def test_report_text_without_evidence_preserves_ordinary_stop_path(self):
        self.attempt_path.unlink()
        with mock.patch.object(self.daemon, "_send_claude_event", return_value=(True, "offline")) as send:
            self.handle(self.event(report_ready_task_id="task-one"))
            send.assert_called_once()
        self.assertIsNone(self.runtime.claude_supervisor_wait)
        self.assertFalse(self.runtime.claude_completed_latched)

    def test_synthetic_fallback_without_evidence_still_reaches_send_path(self):
        self.attempt_path.unlink()
        event = self.event(synthetic_fallback=True)
        event.pop("process_generation")
        with mock.patch.object(self.daemon, "_send_claude_event", return_value=(True, "offline")) as send:
            self.handle(event)
            send.assert_called_once()
        self.assertIsNone(self.runtime.claude_supervisor_wait)

    def test_legacy_latch_clearing_remains_available_without_closeout(self):
        self.attempt_path.unlink()
        source = self.event(offset=-30)
        self.daemon.claude_event_ledger.mark(source, "report_ready")
        self.runtime.claude_completed_latched = True
        self.runtime.claude_report_ready_task_id = "task-one"
        self.runtime.claude_completion_event_id = source["event_id"]
        with mock.patch.object(self.daemon, "_send_claude_event", return_value=(True, "offline")) as send:
            self.handle(self.event())
            send.assert_called_once()
        self.assertFalse(self.runtime.claude_completed_latched)
        self.assertIsNone(self.runtime.claude_supervisor_wait)

    def test_ui_wait_is_claude_only(self):
        self.wait()
        guarded = self.daemon._apply_claude_runtime_guards(self.surface, self.runtime, self.stopped)
        self.assertEqual(guarded.kind, "claude_waiting_supervisor")
        codex = watch.ScreenState("working", message_kind="codex")
        self.assertIs(self.daemon._apply_claude_runtime_guards(self.surface, self.runtime, codex), codex)
        row = dict(surface_id=self.surface, workspace_id=self.workspace,
                   status="readable", agent_kind="codex", agent_pid=123)
        readiness = watch.registration_readiness(
            self.daemon.config, {"targets": [row]}, {self.surface: self.runtime.to_dict()})
        self.assertEqual(readiness[0]["status"], "ready")

    def test_final_enter_guard_discovers_return_after_preflight_and_sends_nothing(self):
        saved = self.attempt_path.read_bytes()
        self.attempt_path.unlink()
        self.runtime.claude_submit_phase = "text_written"
        self.runtime.claude_submit_event_id = "original-text"
        self.client.payload = claude_wrapped_composer_payload()
        self.client.before_input = mock.Mock(side_effect=lambda: self.attempt_path.write_bytes(saved))
        self.assertFalse(self.daemon._send_claude_enter(
            self.target, self.runtime, self.client, reason="race", input_check=lambda: True))
        self.client.before_input.assert_called_once()
        self.assertEqual(self.attempt_path.read_bytes(), saved)
        self.assertEqual(self.runtime.claude_supervisor_wait["status"], adapter.WAITING)
        self.assertEqual(self.runtime.claude_submit_phase, "text_written")
        self.assertEqual(self.client.inputs, [])

    def test_final_text_guard_discovers_return_after_preflight_and_sends_nothing(self):
        saved = self.attempt_path.read_bytes()
        self.attempt_path.unlink()
        self.client.before_input = mock.Mock(side_effect=lambda: self.attempt_path.write_bytes(saved))
        process = dict(agent_kind="claude", agent_pid=self.identity["agent_pid"])
        identity = dict(generation=self.identity["process_generation"], started_epoch=self.now - 500)
        with contextlib.ExitStack() as stack:
            for obj, name, value in (
                (self.daemon, "_candidate_process_label", process),
                (self.daemon, "_claude_send_process_identity", identity),
                (self.daemon, "_active_send_target", self.target),
                (self.daemon, "_claude_event_snapshot", ({}, False, self.stopped, "frame")),
                (self.daemon, "_apply_claude_context_guard", self.stopped),
                (watch, "classify_surface_processes", {}),
                (watch, "surface_process_label", process),
                (watch, "classify_claude_grid", self.stopped),
            ):
                stack.enter_context(mock.patch.object(obj, name, return_value=value))
            stack.enter_context(mock.patch.object(watch.time, "sleep", return_value=None))
            sent, _ = self.daemon._send_claude_event(
                self.event(), self.target, self.runtime, self.client)
        self.assertFalse(sent)
        self.client.before_input.assert_called_once()
        self.assertEqual(self.attempt_path.read_bytes(), saved)
        self.assertEqual(self.runtime.claude_supervisor_wait["status"], adapter.WAITING)
        self.assertEqual(self.client.inputs, [])


class LegacyDispatchCloseoutTests(Fixture):
    """旧 parser 的真实恢复链必须接上共享封口验证，不能只修显示状态。"""

    def recover_original_task(self):
        legacy = legacy_recovery.DispatchRecoveryTests()
        legacy.setUp()
        self.addCleanup(legacy.doCleanups)
        legacy.f.at = self.now - 70
        # 老测试的伪 UUID 不能通过共享 marker 验证；所有本次原生证据
        # 使用同一组合法身份，绝不 mock terminal_report 的裁决。
        legacy.target.update(workspace_id=self.workspace, surface_id=self.surface)
        # 真实配置发布会替换注册列表并重建 UUID 索引；只原地改目标
        # 会留下旧 surface-uuid 索引，使所有事件提前成为 unmapped。
        legacy.daemon.config["targets"] = [legacy.target]
        write(legacy.daemon.config_path, legacy.daemon.config)
        legacy.daemon.runtime[self.surface] = legacy.daemon.runtime.pop("surface-uuid")
        live_workspace = legacy.client.tree_data["windows"][0]["workspaces"][0]
        live_workspace["id"] = self.workspace
        live_workspace["panes"][0]["surfaces"][0]["id"] = self.surface
        legacy.client.top_data["windows"][0]["workspaces"][0]["surfaces"][0]["id"] = self.surface
        make_event = legacy.f.event
        patcher = mock.patch.object(legacy.f, "event", side_effect=lambda *args, **kwargs:
            make_event(*args, workspace_id=self.workspace, surface_id=self.surface, **kwargs))
        patcher.start()
        self.addCleanup(patcher.stop)
        legacy.f.write_pack(executor_uuid=self.surface,
                            completion_receipt=str(legacy.root / "receipt.json"))
        legacy.history()
        legacy.daemon.claude_closeout = self.evidence
        self.assertIsNone(legacy.runtime.claude_task_binding)
        legacy.daemon._apply_claude_runtime_guards(
            self.surface, legacy.runtime, legacy.f.screen())
        self.assertFalse(legacy.runtime.claude_handshake_wait)
        self.assertIsNone(legacy.daemon._claude_supervisor_wait_guard(
            legacy.target, legacy.runtime))
        legacy.no_input()
        return legacy

    def return_original_callback(self, legacy):
        pack_path = legacy.root / "task-pack.json"
        pack = json.loads(pack_path.read_bytes())
        Path(pack["report"]).write_text("原任务报告已冻结，等待主管核收。\n", encoding="utf-8")
        marker = copy.deepcopy(self.task["marker"])
        marker.update(task_id=pack["task_id"], artifact_root=str(legacy.root))
        marker["participants"][0]["surface_ref"] = pack["callback_target"]
        write(self.active / self.workspace / (pack["task_id"] + ".json"), marker)
        attempt = copy.deepcopy(self.attempt)
        attempt["binding"].update({key: pack[key] for key in (
            "task_id", "completion_nonce", "completion_callback", "callback_target", "report")})
        attempt["binding"].update(task_pack_sha256=sha(pack_path),
                                  report_sha256=sha(pack["report"]),
                                  report_bytes=Path(pack["report"]).stat().st_size)
        path = legacy.root / "receipt-attempts/attempt-0001.json"
        write(path, attempt)
        path.with_name("delivery.lock").touch()
        return path

    def test_recovered_dispatch_callback_stops_every_resume_origin_without_receipt_or_input(self):
        legacy = self.recover_original_task()
        path = self.return_original_callback(legacy)
        original_attempt = path.read_bytes()
        d, r, t, client = legacy.daemon, legacy.runtime, legacy.target, legacy.client
        stopped = legacy.f.screen()
        composer = watch.ScreenState("composer_busy", message_kind="claude", watchdog_echo=True)
        # First discovery is the real deferred sender, exactly where a stale
        # handshake used to release an extra continuation after the callback.
        self.assertTrue(d._maybe_send_deferred_claude_stop(t, r, stopped, client))
        self.assertIsNotNone(r.claude_supervisor_wait)
        self.assertEqual(r.claude_supervisor_wait["status"], adapter.WAITING)
        self.assertEqual(r.claude_supervisor_wait["deadline_at"],
                         self.attempt["ended_at_epoch"] + adapter.WAIT_SECONDS)
        event = legacy.f.event("Stop", "报告已冻结，等待主管核收", offset=45)
        legacy.f.handle(event)
        self.assertEqual(d.claude_event_ledger.status_of(event["event_id"]),
                         "suppressed_waiting_supervisor")
        self.assertFalse(d._send_claude_event(event, t, r, client)[0])
        self.assertFalse(d._send_claude_enter(t, r, client, reason="legacy-closeout"))
        self.assertTrue(d._recover_orphan_watchdog_submit(t, r, composer, client))
        self.assertTrue(d._reconcile_claude_submit(t, r, composer, client))
        self.assertTrue(d._maybe_send_claude_hook_gap_fallback(t, r, stopped, {}, client))
        self.assertEqual(d._prepare_claude_send(t, r, stopped, client, {}, ""), (None, {}))
        self.assertFalse(d._restore_expired_claude_deferred(r))
        self.assertFalse(d._recover_claude_report_stop(self.surface, r))
        legacy.no_input()
        legacy.originals_unchanged()
        self.assertEqual(path.read_bytes(), original_attempt)
        self.assertFalse((legacy.root / "receipt.json").exists())
        self.assertFalse(r.claude_completed_latched)

    def test_restart_keeps_recovered_task_and_discovers_callback_returned_later(self):
        legacy = self.recover_original_task()
        legacy.daemon.save()
        restarted = watch.WatchDaemon(config_path=legacy.daemon.config_path,
                                      state_path=legacy.daemon.state_path, client=legacy.client)
        restarted.claude_closeout = self.evidence
        runtime = restarted.runtime[self.surface]
        self.return_original_callback(legacy)
        self.assertEqual(restarted._claude_supervisor_wait_guard(legacy.target, runtime),
                         adapter.WAITING)
        self.assertFalse(restarted._send_claude_enter(
            legacy.target, runtime, legacy.client, reason="restarted-closeout"))
        legacy.no_input()
        self.assertFalse(runtime.claude_completed_latched)
        self.assertFalse((legacy.root / "receipt.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

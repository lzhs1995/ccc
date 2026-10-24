"""The closeout hook allows one fixed queue-only entrypoint, never resending."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import cmux_callback_queue_resume as recovery
import cmux_executor_closeout_guard as guard
import executor_closeout
import offline_test_hook
import cmux_bridge as bridge
import cmux_native_delivery as native
from native_test_support import LegacyTerminalFixture, native_hook_command


class QueueResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace, self.executor, self.supervisor = "WORKSPACE", "EXECUTOR", "SUPERVISOR"
        self.pack_path = self.root / "task-pack.json"
        self.report = self.root / "executor-report.md"
        self.report.write_text("Frozen independent findings.")
        self.receipt = self.root / "receipt.json"
        self.journal = self.root / "receipt-attempts"
        self.journal.mkdir()
        self.lock = self.journal / "delivery.lock"
        self.lock.touch()
        self.skill = self.root / "controller/SKILL.md"
        self.skill.parent.mkdir()
        self.skill.write_text("Original controller.")
        scripts = self.skill.parent / "scripts"
        scripts.mkdir()
        (scripts / "cmux_bridge.py").write_text("# test controller; not imported")
        self.pack = dict(draft=False, task_id="task", executor_uuid=self.executor,
                         completion_nonce="nonce", completion_callback="DONE|task|nonce",
                         callback_target="surface:40", report=str(self.report),
                         required_skill=str(self.skill), completion_receipt=str(self.receipt))
        self.write(self.pack_path, self.pack)
        self.marker = dict(task_id="task", workspace_uuid=self.workspace, artifact_root=str(self.root),
                           participants=[dict(role="executor", surface_uuid=self.executor),
                                         dict(role="supervisor", surface_ref="surface:40",
                                              surface_uuid=self.supervisor)])
        self.attempt_path = self.journal / "attempt-0001.json"
        self.attempt = dict(phase="POST_ENTER_OBSERVATION", started_at_epoch=1,
                            ended_at_epoch=5, error="COMPOSE_OCCUPIED",
                            events=[dict(phase=p, at_epoch=i+2) for i, p in enumerate(
                                ["PASTE_INTENT", "ENTER_INTENT", "POST_ENTER_OBSERVATION"])],
                            binding={**{k:self.pack[k] for k in ("task_id", "completion_nonce",
                                "completion_callback", "callback_target", "report")},
                                "task_pack_sha256":self.sha(self.pack_path),
                                "report_sha256":self.sha(self.report),
                                "report_bytes":self.report.stat().st_size,
                                "identity":dict(workspace_uuid=self.workspace,
                                    caller_surface_uuid=self.executor,
                                    target_surface_uuid=self.supervisor, target_pane_uuid="PANE")})
        self.native = LegacyTerminalFixture.attach(self, home=self.root,
            identity=self.attempt['binding']['identity'])
        # CLI 子进程使用真实 provider 目录结构，不导入夹具以免提前加载 bridge。
        native_root = self.root / '.codex/sessions'
        native_root.mkdir(parents=True)
        self.native.transcript = self.native.transcript.rename(
            native_root / self.native.transcript.name)
        self.native.native_root = native_root
        # 保留原用例 1..5 的时间语义；绑定和 fence 都由真实实现生成。
        with mock.patch.object(native.time, 'time', return_value=1):
            self.attempt['native_binding'] = native.bind_target(
                bridge, self.pack['callback_target'], self.pack['completion_callback'])
        with mock.patch.object(native.time, 'time', return_value=2):
            fence = native.capture_paste_fence(self.attempt['native_binding'])
        screen = self.native.draft('')
        self.attempt['events'][0].update(screen=screen, screen_sha256=bridge.screen_hash(screen),
                                        native_paste_fence=fence)
        self.write(self.attempt_path, self.attempt)
        self.command = shlex.join(["rtk", "proxy", str(Path(recovery.__file__).resolve()),
                                   "--task-pack", str(self.pack_path)])
        self.payload = dict(hook_event_name="PreToolUse", tool_name="Bash",
                            tool_input=dict(command=self.command))

    @staticmethod
    def write(path, obj):
        path.write_text(json.dumps(obj))

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def evaluate(self, payload=None):
        with mock.patch.object(guard, "_surface_key", return_value=self.executor), \
             mock.patch.object(guard, "_workspace_key", return_value=self.workspace), \
             mock.patch.object(guard, "_active_markers", return_value=[self.marker]):
            return guard._evaluate_resolved(payload or self.payload)[0]

    def test_exact_queue_only_command_is_allowed(self):
        before = {p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertTrue(self.evaluate())
        self.assertEqual(before, {p:p.read_bytes() for p in before})
        self.assertFalse(self.receipt.exists())

    def test_real_hook_entrypoint_allows_original_task(self):
        active = self.root / "active"
        active.mkdir()
        self.write(active / (self.workspace + ".json"), self.marker)
        state = self.native.export_state(self.root / 'native-state.json')
        result = subprocess.run(native_hook_command(Path(guard.__file__), active, state),
                                input=json.dumps(self.payload), text=True, capture_output=True,
                                env=dict(os.environ, CMUX_WORKSPACE_ID=self.workspace,
                                         CMUX_SURFACE_ID=self.executor), timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_original_binding_or_fence_denies_without_traceback(self):
        active = self.root / 'active'
        active.mkdir()
        self.write(active / (self.workspace + '.json'), self.marker)
        for missing in ('native_binding', 'native_paste_fence'):
            with self.subTest(missing=missing):
                attempt = copy.deepcopy(self.attempt)
                if missing == 'native_binding':
                    attempt.pop(missing)
                else:
                    attempt['events'][0].pop(missing)
                self.write(self.attempt_path, attempt)
                self.assertFalse(self.evaluate())
                before = self.attempt_path.read_bytes()
                result = subprocess.run(offline_test_hook.command(Path(guard.__file__), active),
                    input=json.dumps(self.payload), text=True, capture_output=True,
                    env=dict(os.environ, CMUX_WORKSPACE_ID=self.workspace,
                             CMUX_SURFACE_ID=self.executor), timeout=10)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertEqual(self.attempt_path.read_bytes(), before)
                self.assertFalse(self.receipt.exists())

    def test_arbitrary_tool_or_shell_tail_stays_sealed(self):
        for suffix in ["; true", " && true", "\ntrue", " --reconcile-only", " > /tmp/output"]:
            payload = copy.deepcopy(self.payload)
            payload["tool_input"]["command"] += suffix
            self.assertFalse(self.evaluate(payload), suffix)
        for name in ["Read", "Write", "mcp__shell__exec"]:
            payload = copy.deepcopy(self.payload)
            payload["tool_name"] = name
            self.assertFalse(self.evaluate(payload), name)

    def test_other_path_environment_and_background_stay_sealed(self):
        for command in [self.command.replace(str(self.pack_path), str(self.root/"other.json")),
                        "ENV=1 " + self.command, self.command.replace("rtk proxy", "python3")]:
            payload = copy.deepcopy(self.payload)
            payload["tool_input"]["command"] = command
            self.assertFalse(self.evaluate(payload), command)
        payload = copy.deepcopy(self.payload)
        payload["tool_input"]["run_in_background"] = True
        self.assertFalse(self.evaluate(payload))

    def test_receipt_exists_refuses_queue(self):
        self.receipt.write_text("{}")
        self.assertFalse(self.evaluate())

    def test_broken_receipt_symlink_also_refuses_queue(self):
        self.receipt.symlink_to(self.root / "missing")
        self.assertFalse(self.evaluate())

    def test_original_controller_symlink_refuses_queue(self):
        other = self.skill.with_name("ORIGINAL.md")
        self.skill.rename(other)
        self.skill.symlink_to(other)
        self.assertFalse(self.evaluate())

    def test_uncertain_resume_is_terminal_and_cannot_tab_again(self):
        self.attempt["events"].extend([
            dict(phase="QUEUE_TAB_INTENT", at_epoch=6),
            dict(phase="POST_QUEUE_TAB_OBSERVATION", at_epoch=7)])
        self.attempt["phase"] = "POST_QUEUE_TAB_OBSERVATION"
        self.write(self.attempt_path, self.attempt)
        evidence = executor_closeout.terminal_report(self.marker, self.workspace, self.executor)
        self.assertIsNotNone(evidence)
        self.assertFalse(self.evaluate())
        payload = copy.deepcopy(self.payload)
        payload["tool_input"]["command"] = "touch NEVER"
        self.assertFalse(self.evaluate(payload))
        self.assertFalse(self.receipt.exists())

    def test_interrupted_tab_intent_still_seals_but_is_not_delivery(self):
        self.attempt["events"].append(dict(phase="QUEUE_TAB_INTENT", at_epoch=6))
        self.attempt["phase"] = "QUEUE_TAB_INTENT"
        self.write(self.attempt_path, self.attempt)
        self.assertIsNotNone(executor_closeout.terminal_report(
            self.marker, self.workspace, self.executor))
        self.assertFalse(self.evaluate())
        self.assertFalse(self.receipt.exists())

    def test_resume_does_not_extend_original_event_deadline(self):
        self.attempt['events'][-1]['at_epoch'] = 5.5
        self.attempt['events'].append(dict(phase='QUEUE_TAB_INTENT', at_epoch=6))
        self.attempt['phase'] = 'QUEUE_TAB_INTENT'
        self.write(self.attempt_path, self.attempt)
        self.assertIsNone(executor_closeout.terminal_report(
            self.marker, self.workspace, self.executor))
        self.assertFalse(self.receipt.exists())

    def test_duplicate_queue_or_extra_enter_not_resumable(self):
        for phase in ["QUEUE_TAB_INTENT", "EXTRA_ENTER_INTENT"]:
            self.attempt["events"].insert(2, dict(phase=phase, at_epoch=3.5))
            self.write(self.attempt_path, self.attempt)
            with self.assertRaises(ValueError):
                recovery.resumable(self.pack_path)
            self.attempt["events"].pop(2)

    def test_cli_uses_original_bound_controller_and_no_other_input(self):
        log = self.root / "call.json"
        script = self.skill.parent / "scripts/cmux_bridge.py"
        script.write_text("import json\nfrom pathlib import Path\n"
            "def submit_completion_callback(path, **options):\n"
            "    Path(" + repr(str(log)) + ").write_text(json.dumps([path, options]))\n"
            "    return {'confirmed': False}\n")
        result = subprocess.run([sys.executable, str(Path(recovery.__file__)),
                                 "--task-pack", str(self.pack_path)],
                                text=True, capture_output=True, timeout=10,
                                env=dict(os.environ, HOME=str(self.root)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(log.read_text()),
                         [str(self.pack_path), {"resume_queue_only":True}])
        self.assertFalse(self.receipt.exists())

    def test_original_controller_refusal_is_preserved(self):
        script = self.skill.parent / "scripts/cmux_bridge.py"
        script.write_text("def submit_completion_callback(*a, **k):\n"
                          "    raise RuntimeError('ORIGINAL_COMPOSER_NOT_RECOVERABLE')\n")
        result = subprocess.run([sys.executable, str(Path(recovery.__file__)),
                                 "--task-pack", str(self.pack_path)],
                                text=True, capture_output=True, timeout=10,
                                env=dict(os.environ, HOME=str(self.root)))
        self.assertEqual(result.returncode, 2)
        self.assertIn("ORIGINAL_COMPOSER_NOT_RECOVERABLE", result.stderr)
        self.assertFalse(self.receipt.exists())


if __name__ == "__main__":
    unittest.main()

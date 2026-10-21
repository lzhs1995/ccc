"""Sealed closeout keeps one zero-input reconcile legal (2026-10-08 loop)."""
import shlex
import unittest
from pathlib import Path
from unittest import mock

import cmux_callback_reconcile as reconcile
import cmux_executor_closeout_guard as guard
import test_callback_queue_resume as base


class CallbackReconcileTests(base.QueueResumeTests):
    def setUp(self):
        super().setUp()
        self.queue_command = self.command
        self.command = shlex.join(["rtk", "proxy", str(Path(reconcile.__file__).resolve()),
                                   "--task-pack", str(self.pack_path)])
        self.payload = dict(hook_event_name="PreToolUse", tool_name="Bash",
                            tool_input=dict(command=self.command))

    def test_exact_reconcile_command_is_allowed_while_sealed(self):
        self.assertTrue(self.evaluate())

    def test_reconcile_stays_legal_after_queue_tab_was_used(self):
        # The loop case: Tab already spent, so queue resume is refused; the
        # executor must still have one honest read-only action.
        self.attempt["events"] += [dict(phase="QUEUE_TAB_INTENT", at_epoch=6),
                                   dict(phase="POST_QUEUE_TAB_OBSERVATION", at_epoch=7)]
        self.attempt["phase"] = "POST_QUEUE_TAB_OBSERVATION"
        self.attempt["ended_at_epoch"] = 7
        self.write(self.attempt_path, self.attempt)
        self.assertTrue(self.evaluate())
        queue = dict(self.payload, tool_input=dict(command=self.queue_command))
        self.assertFalse(self.evaluate(queue))

    def test_shell_tail_background_or_other_pack_stay_sealed(self):
        for command in (self.command + " && echo x", self.command + "; true",
                        self.command.replace("task-pack.json", "other.json"),
                        "python3 " + str(Path(reconcile.__file__).resolve())):
            with self.subTest(command=command[-30:]):
                self.assertFalse(self.evaluate(dict(self.payload, tool_input=dict(command=command))))
        background = dict(self.payload, tool_input=dict(command=self.command, run_in_background=True))
        self.assertFalse(self.evaluate(background))

    def test_receipt_or_no_input_attempt_refuses(self):
        self.receipt.write_text("{}")
        self.assertFalse(self.evaluate())
        self.receipt.unlink()
        self.attempt.update(phase="NO_INPUT", events=[])
        self.write(self.attempt_path, self.attempt)
        with self.assertRaises(ValueError):
            reconcile.reconcilable(self.pack_path)

    def test_main_uses_reconcile_only_and_never_input(self):
        bridge = mock.Mock()
        bridge.__file__ = str(self.skill.parent / "scripts/cmux_bridge.py")
        bridge.submit_completion_callback.return_value = {"confirmed": True}
        with mock.patch.object(reconcile.importlib, "import_module", return_value=bridge), \
                mock.patch.object(reconcile.sys, "argv", ["x", "--task-pack", str(self.pack_path)]):
            self.assertEqual(reconcile.main(), 0)
        bridge.submit_completion_callback.assert_called_once_with(str(self.pack_path), reconcile_only=True)
        self.assertFalse(bridge.send_key.called or bridge.send_text.called)

    def test_sealed_message_names_both_legal_actions(self):
        with mock.patch.object(guard, "_surface_key", return_value=self.executor), \
             mock.patch.object(guard, "_workspace_key", return_value=self.workspace), \
             mock.patch.object(guard, "_active_markers", return_value=[self.marker]):
            ok, reason = guard._evaluate_resolved(dict(self.payload, tool_input=dict(command="ls")))
        self.assertFalse(ok)
        self.assertIn("cmux_callback_reconcile.py", reason)
        self.assertIn("zero terminal input", reason)
        self.assertIn("existing guarded queue-resume entrypoint", reason)
        self.assertIn("no new paste, watcher, or retry loop", reason)


# Do not re-run the inherited queue-resume cases under this class.
for _name in [n for n in dir(base.QueueResumeTests) if n.startswith("test_")]:
    if _name not in CallbackReconcileTests.__dict__:
        setattr(CallbackReconcileTests, _name, None)

if __name__ == "__main__":
    unittest.main()

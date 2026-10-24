"""Read-only CLI help must not become a send, including a Codex batch leaf."""
from __future__ import annotations

import json
import shlex
import unittest
from unittest.mock import patch

import cmux_native_delivery_guard as guard
from cmux_submission_inputs import delivery_calls
from cmux_submit_confirmation_guard import is_bridge_help_command


BRIDGE = "/offline/release with spaces/source/scripts/cmux_bridge.py"
PYTHON = "/opt/homebrew/opt/python@3.14/bin/python3.14"
HELP = shlex.join(["rtk", "proxy", PYTHON, "-B", BRIDGE,
                   "submit-completion-callback", "--help"])
CALLBACK = shlex.join(["rtk", "proxy", PYTHON, "-B", BRIDGE,
                       "submit-completion-callback", "--task-pack", "/offline/task-pack.json"])


def wrapped(*commands):
    return "const r = await Promise.allSettled([" + ",".join(
        "tools.exec_command(" + json.dumps({"cmd": command}) + ")"
        for command in commands) + "]); for (const x of r) text(x);"


class BridgeHelpInputsTests(unittest.TestCase):
    def test_python_script_help_is_read_only_for_each_literal_form(self):
        commands = [HELP, "cmux_bridge.py --help", "rtk cmux-bridge-toolchain -h"]
        for python in ("python", "python3", PYTHON):
            for flags in ([], ["-B"], ["-I", "-B", "-u"]):
                for tail in (["--help"], ["read-screen", "-h"],
                             ["submit-task-pack", "--help"],
                             ["submit-completion-callback", "--help"]):
                    commands.append(shlex.join([python, *flags, BRIDGE, *tail]))
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(is_bridge_help_command(command))
                self.assertEqual(delivery_calls(command), [])

    def test_help_skips_native_identity_and_evidence_for_shell_and_codex(self):
        payloads = [
            {"tool_name": "Bash", "tool_input": {"command": HELP}},
            {"tool_name": "exec_command", "tool_input": {"cmd": HELP}},
            {"tool_name": "functions.exec", "tool_input": wrapped(
                "rtk rg -n callback /offline/notes.md", HELP, "rtk cat /offline/report.md")},
            {"tool_input": {"arguments": json.dumps({"calls": [
                {"parameters": {"cmd": HELP}}]})}},
        ]
        with patch.object(guard.cmux_hook_identity, "evaluation") as identity, \
                patch.object(guard, "attempt_paths") as paths, \
                patch.object(guard, "_load") as load:
            for payload in payloads:
                with self.subTest(payload=payload):
                    self.assertEqual(guard.evaluate(payload), {"action": "skip", "results": []})
            identity.assert_not_called()
            paths.assert_not_called()
            load.assert_not_called()

    def test_mixed_codex_batch_preserves_the_real_callback(self):
        expected = delivery_calls(CALLBACK)
        self.assertEqual(len(expected), 1)
        self.assertEqual(expected[0]["kind"], "callback")
        self.assertEqual(expected[0]["pack"], "/offline/task-pack.json")
        self.assertEqual(guard._calls({"tool_input": wrapped(HELP, CALLBACK)}), expected)
        self.assertEqual(guard._calls({"tool_input": wrapped(CALLBACK, HELP)}), expected)

    def test_help_in_payload_is_not_a_help_invocation(self):
        command = shlex.join([PYTHON, "-B", BRIDGE, "submit-text", "--surface", "surface:701",
                              "--text", "STATUS: HELP_NOTE --help", "--marker", "HELP_NOTE"])
        self.assertFalse(is_bridge_help_command(command))
        calls = delivery_calls(command)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["text"], "STATUS: HELP_NOTE --help")

    def test_python_code_with_trailing_help_still_records_its_callback(self):
        command = shlex.join([PYTHON, "-B", "-c",
                              "bridge.submit_completion_callback('/offline/task-pack.json')", "--help"])
        self.assertFalse(is_bridge_help_command(command))
        self.assertEqual(delivery_calls(command), delivery_calls(CALLBACK))

    def test_callback_reconcile_literal_cli_and_python_have_identical_mode(self):
        expected = delivery_calls(CALLBACK + " --reconcile-only")
        self.assertIs(expected[0]["reconcile_only"], True)
        for source in (
                "bridge.submit_completion_callback('/offline/task-pack.json', reconcile_only=True)",
                "bridge.submit_completion_callback(task_pack_path='/offline/task-pack.json', "
                "confirm_lines=200, reconcile_only=True, resume_queue_only=False)"):
            with self.subTest(source=source):
                self.assertEqual(delivery_calls(shlex.join([PYTHON, "-B", "-c", source])), expected)
        self.assertIs(delivery_calls(CALLBACK)[0]["reconcile_only"], False)

    def test_callback_cli_rejects_false_flags_unknown_modes_and_duplicate_arguments(self):
        for suffix in (" --reconcile-only=false", " --reconcile-only=True",
                       " --reconcile-only --resume-queue-only", " --reconcile-onl",
                       " --reconcile-only --reconcile-only", " --confirm-lines=0",
                       " --task-pack /different/task-pack.json", " --reconcile-only extra"):
            with self.subTest(suffix=suffix):
                self.assertEqual(delivery_calls(CALLBACK + suffix)[0]["kind"], "unresolved")

    def test_callback_python_rejects_dynamic_kwargs_false_strings_and_conflicts(self):
        for arguments in (
                "'/offline/task-pack.json', reconcile_only='False'",
                "'/offline/task-pack.json', reconcile_only=1",
                "'/offline/task-pack.json', reconcile_only=flag",
                "'/offline/task-pack.json', **{'reconcile_only': True}",
                "'/offline/task-pack.json', reconcile_only=True, **options",
                "'/offline/task-pack.json', reconcile_only=True, resume_queue_only=True",
                "'/offline/task-pack.json', 200, True",
                "*args, reconcile_only=True",
                "'/offline/task-pack.json', task_pack_path='/different/task-pack.json', reconcile_only=True"):
            with self.subTest(arguments=arguments):
                self.assertEqual(delivery_calls(
                    "bridge.submit_completion_callback(" + arguments + ")")[0]["kind"], "unresolved")

    def test_only_exact_help_argv_qualifies(self):
        for command in (CALLBACK, HELP + " --task-pack /offline/task-pack.json",
                        HELP + " && " + CALLBACK, HELP + "\n" + CALLBACK,
                        "env " + HELP, HELP.replace("python3.14", "python-not-an-interpreter")):
            with self.subTest(command=command):
                self.assertFalse(is_bridge_help_command(command))
        for command in (HELP + " && " + CALLBACK, HELP + "\n" + CALLBACK):
            self.assertEqual(delivery_calls(command), delivery_calls(CALLBACK))

    def test_dynamic_codex_send_cannot_borrow_literal_help_exemption(self):
        source = ("const cmd = " + json.dumps(CALLBACK) + "; "
                  "const extra = '--help'; "
                  "text(await tools.exec_command({cmd: cmd + ' ' + extra}));")
        calls = guard._calls({"tool_input": source})
        self.assertTrue(calls)
        self.assertTrue(all(call["kind"] == "unresolved" for call in calls))

    def test_dynamic_edit_of_help_command_remains_guarded(self):
        source = ("const cmd = " + json.dumps(HELP) + "; "
                  "text(await tools.exec_command({cmd: cmd.replace('--help', "
                  "'--task-pack /offline/task-pack.json')}));")
        calls = guard._calls({"tool_input": source})
        self.assertTrue(calls)
        self.assertTrue(all(call["kind"] == "unresolved" for call in calls))

    def test_mixed_help_does_not_bypass_missing_native_receipt(self):
        with patch.object(guard.cmux_hook_identity, "evaluation"), \
                patch.object(guard.cmux_hook_identity, "identity", return_value=("workspace", "caller")), \
                patch.object(guard, "_original", side_effect=ValueError("no original native receipt")) as original:
            result = guard.evaluate({"tool_input": wrapped(HELP, CALLBACK)})
        self.assertEqual(result["action"], "block")
        original.assert_called_once()
        self.assertEqual(result["results"][0]["call"]["pack"], "/offline/task-pack.json")


if __name__ == "__main__":
    unittest.main()

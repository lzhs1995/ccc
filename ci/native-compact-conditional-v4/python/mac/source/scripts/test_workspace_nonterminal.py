"""Workspace transport checks must not take jurisdiction over ordinary tools."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

import cmux_workspace_guard as guard


class WorkspaceNonterminalTests(unittest.TestCase):
    def assert_allowed(self, command):
        ok, message = guard.validate_command(command)
        self.assertTrue(ok, (command, message))

    def assert_blocked(self, command):
        ok, message = guard.validate_command(command)
        self.assertFalse(ok, (command, message))
        self.assertIn("WORKSPACE_SCOPE_DENIED", message)

    def test_document_heredoc_does_not_parse_prose_as_shell(self):
        command = (
            "rtk proxy /opt/homebrew/opt/python@3.14/bin/python3.14 -B - <<'PY'\n"
            "from pathlib import Path\n"
            "body = '''The user's request fixes a broken quoted command.\n"
            "cmux send examples are documentation; surface.send_text is a name.\n"
            'One unmatched double quote belongs to prose: "\n'
            "'''\n"
            "Path('/tmp/pr-body.md').write_text(body)\n"
            "PY\n"
        )
        self.assert_allowed(command)
        self.assert_allowed(command + "rtk cmux send --surface surface:2 hello\n")

    def test_unparseable_nonterminal_input_is_not_a_workspace_error(self):
        # This hook does not decide Python/shell syntax validity. The real tool
        # reports that normally; there is no terminal transport to authorize.
        for command in (
            "rtk python3 - <<'PY'\nfrom pathlib import Path\nbody = '''owner's \" note\nPY\n",
            "rtk python3 -c 'print(\"ordinary unfinished source)\n",
            "rtk rg 'an unfinished documentation quote README.md",
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_quoted_searches_and_prints_are_not_terminal_invocations(self):
        for command in (
            'rtk rg "cmux" "send" README.md',
            'rtk rg "terminal.send|surface.write|terminal.paste" scripts',
            'rtk git log --grep="cmux send-key"',
            'rtk printf "%s" "cmux" "send-key"',
            'rtk python3 -c "print(\'terminal.send\')"',
            'rtk rg "cmux send; cmux send-key" README.md',
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_raw_sender_is_allowed_without_shell_parse_admission(self):
        for command in (
            'rtk cmux send --surface surface:2 "unfinished',
            "rtk cmux send-key --surface surface:2 'unfinished",
            "rtk proxy cmux-agent ask surface:2 'unfinished",
            "cmux 'send --surface surface:2 unfinished",
            "cmux --help; cmux send surface:2 'unfinished",
            "CMUX_WORKSPACE_ID=wrong rtk cmux send surface:2 'unfinished",
            "rtk sudo -u owner cmux send surface:2 'unfinished",
            'rtk sh -c "cmux send --surface surface:2 unfinished',
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_raw_rpc_and_nested_shell_sender_is_allowed(self):
        for command in (
            'rtk cmux rpc terminal.paste \'{"text":"hello"}\'',
            'cmux rpc terminal.send_text \'{"text":"hello"}\'',
            'cmux rpc surface.write \'{"text":"hello"}\'',
            "rtk sh -c 'cmux send --surface surface:2 hello'",
            "rtk sh -c \"cmux send --surface surface:2 'unfinished\"",
            'rtk rg "cmux send" README.md; cmux send-key surface:2 enter',
        ):
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_real_hook_calls_only_classify_never_execute_test_commands(self):
        scripts = Path(__file__).parent
        document = (
            "rtk python3 - <<'PY'\nfrom pathlib import Path\n"
            "Path('/tmp/never-executed.md').write_text('''User's PR body says \"hello\n''')\nPY\n"
        )
        cases = ((document, 0), ('rtk rg "terminal.send" README.md', 0),
                 ('rtk cmux send surface:2 "unfinished', 0))
        for script in ("cmux_workspace_guard.py", "cmux_agent_panel_guard.py"):
            for command, expected in cases:
                with self.subTest(script=script, command=command):
                    result = subprocess.run(
                        [sys.executable, "-B", str(scripts / script)],
                        input=json.dumps({"tool_name": "exec_command", "tool_input": {"cmd": command}}),
                        text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == "__main__":
    unittest.main()

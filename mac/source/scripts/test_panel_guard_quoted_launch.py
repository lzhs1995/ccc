"""8701 regression: inspect commands as data; never execute the supplied command."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import cmux_agent_panel_guard as guard
import render_cmux_agent as renderer


GUARD = Path(__file__).with_name("cmux_agent_panel_guard.py").resolve()
HELPER = Path("/Users/lzhs/.local/bin/cmux-agent")
ORIGINAL = (
    "rtk proxy /Users/lzhs/.local/bin/cmux-agent send "
    "E765373A-4081-4038-ACC8-EACEE2540F08 'STATUS: RESUME8701R2-20261013. "
    "Compact completed in your original session; supervisor restored the same task reservation. "
    "Continue original stata8701-runtime-wire-v2 from PROGRESS.md/candidate with unchanged pack "
    "a39c5a7c8e2e5bffb43c7d411f6c8aadbd1ed6f775a964c6f25fc4622c089ac8 and nonce 3122b60333d232a7. "
    "Write only its executor-output; Codex alone integrates shared files. Finish actual execute-entry "
    "tests, mutations and frozen REPORT, then the original pack-bound callback once. "
    "No new compact, task dispatch, session or model. Reply STATUS: RESUMED8701R2 before continuing.'"
)


class QuotedLaunchTests(unittest.TestCase):
    def test_original_message_is_literal_without_rewording(self):
        self.assertIn("; Codex alone", ORIGINAL)
        self.assertFalse(guard._shell_starts_agent_launch(ORIGINAL))
        self.assertFalse(guard._cmux_send_starts_agent_launch(ORIGINAL))

    def test_quoted_operators_and_agent_mentions_are_data(self):
        for body in ("status; Codex alone", "status\nclaude prose", ";", "&&", "|",
                     "status || codex prose", "status (codex)", "sh -c 'codex'", "中文; Codex"):
            for quoted in (shlex.quote(body), '"' + body.replace('"', '\\"') + '"'):
                command = "rtk proxy printf '%s' " + quoted + " Codex"
                with self.subTest(command=command):
                    self.assertFalse(guard._shell_starts_agent_launch(command))
                    self.assertTrue(guard.validate_command(command)[0])

    def test_single_quotes_and_escaped_expansions_stay_literal(self):
        commands = [
            "printf '%s' '$(codex); `claude`'",
            r'''printf '%s' "\$(codex); \`claude\`"''',
            r"printf '%s' status\; Codex alone",
            "printf '%s' 'executor'\"'\"'s status; Codex alone'",
            "printf '%s' ok # ; codex\ntrue",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(guard._shell_starts_agent_launch(command))

    def test_real_adjacent_commands_survive_quoted_message(self):
        for separator in (";", "&&", "||", "|", "&", "\n"):
            for command in (ORIGINAL + separator + " codex resume deadbeef",
                            "claude --resume existing" + separator + ORIGINAL):
                with self.subTest(command=command):
                    self.assertTrue(guard._shell_starts_agent_launch(command))
        for command in ("(codex)", "{ claude; }", "if true; then codex; fi",
                        '"co"\'dex\' resume deadbeef', "co\\\ndex resume deadbeef"):
            with self.subTest(command=command):
                self.assertTrue(guard._shell_starts_agent_launch(command))

    def test_substitutions_are_code_even_inside_double_quotes(self):
        commands = [
            "printf '%s' $(codex resume deadbeef)",
            'printf "%s" "status $(claude --resume existing)"',
            'printf "%s" "$(printf "%s" "$(codex)")"',
            "printf '%s' `codex resume deadbeef`",
            r"printf '%s' `printf '%s' \`codex\``",
            'printf "%s" "$(printf \'%s\' \')\'; codex)"',
            "cat <(codex)",
            "cat >(claude)",
            'X="$(codex)" true',
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(guard._shell_starts_agent_launch(command))
                self.assertFalse(guard.validate_command(command)[0])
        self.assertFalse(guard._shell_starts_agent_launch(
            'printf "%s" "$(printf \'%s\' \'status; Codex alone\')"'))

    def test_wrappers_inspect_only_executable_arguments(self):
        for command in (
            "rtk proxy env TASK_FLAG=1 nohup codex",
            "/usr/bin/env -u TASK_FLAG command -p /usr/local/bin/claude",
            "TASK_FLAG='status; Codex alone' exec -a worker codex",
            "env -S 'rtk proxy codex'",
            "rtk proxy /bin/sh -c 'codex resume deadbeef'",
            "env TASK_FLAG=1 /bin/bash -lc 'true; claude --resume existing'",
            "zsh -ic 'printf \"%s\" \"$(codex)\"'",
            "eval 'true;' 'codex'",
        ):
            with self.subTest(command=command):
                self.assertTrue(guard._shell_starts_agent_launch(command))
        for command in ("sh -c 'printf \"%s\" \"status; Codex alone\"'",
                        "command -v codex", "env TASK_FLAG=codex printf '%s' ok",
                        "env -S 'printf %s status; Codex alone'", "'if' codex"):
            with self.subTest(command=command):
                self.assertFalse(guard._shell_starts_agent_launch(command))

    def test_arithmetic_names_are_data_but_substitutions_execute(self):
        for command in ('printf "%s" "$((codex + 1))"', "((codex + (1)))"):
            with self.subTest(command=command):
                self.assertFalse(guard._shell_starts_agent_launch(command))
        self.assertTrue(guard._shell_starts_agent_launch('printf "%s" "$((1 + $(codex)))"'))

    def test_help_cannot_exempt_another_launch(self):
        for command in ("printf '%s' --help; codex", "codex --help; claude",
                        "sh -c 'codex' --help", 'printf --help "$(codex)"'):
            with self.subTest(command=command):
                self.assertFalse(guard.validate_command(command)[0])
        self.assertTrue(guard.validate_command("rtk proxy codex --help")[0])

    def test_nesting_limit_does_not_fail_open(self):
        command = "printf '%s' " + "$(printf '%s' " * 40 + "$(codex)" + ")" * 40
        self.assertTrue(guard._shell_starts_agent_launch(command))

    def check_entrypoints(self, command, allowed, *, rejection="direct agent CLI launch"):
        argv = [sys.executable, "-B", str(GUARD)]
        dry = subprocess.run(argv + ["--check-command", command, "--json"],
                             capture_output=True, text=True, timeout=20)
        self.assertEqual(dry.returncode, 0 if allowed else 2, dry.stdout + dry.stderr)
        self.assertEqual(json.loads(dry.stdout)["ok"], allowed)
        for tool, key in (("exec_command", "cmd"), ("Bash", "command")):
            result = subprocess.run(argv, input=json.dumps({"tool_name": tool,
                                    "tool_input": {key: command}}),
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0 if allowed else 2, result.stdout + result.stderr)
            if allowed:
                self.assertEqual(json.loads(result.stdout), {})
            else:
                self.assertIn(rejection, result.stderr)

    def test_real_dry_run_and_hook_entries(self):
        self.check_entrypoints("printf '%s' 'status; Codex alone'", True)
        for command in ("printf '%s' 'status; Codex alone'; codex",
                        'printf "%s" "$(printf \"%s\" \"$(codex)\")"',
                        "rtk proxy env TASK_FLAG=1 sh -lc 'claude --resume existing'",
                        "printf --help; codex"):
            with self.subTest(command=command):
                self.check_entrypoints(command, False)

    def test_exact_original_body_at_same_release_real_entries(self):
        # Render the complete helper for this candidate, as installation does.
        # The real CLI/hook loads only its own release's dependencies; no live
        # installation or send is needed to test the original quoted message.
        release = GUARD.parents[1]
        raw = renderer.render(
            (release / "tests/fixtures/cmux-agent-legacy.sh").read_bytes(),
            expected_sha256=renderer.BASELINE_SHA256, python=sys.executable,
            adapter=release / "scripts/cmux_agent_adapter.py")
        with tempfile.TemporaryDirectory() as temporary:
            helper = Path(temporary).resolve() / "cmux-agent"
            helper.write_bytes(raw)
            helper.chmod(0o755)
            original_prefix = "rtk proxy " + str(HELPER)
            self.assertTrue(ORIGINAL.startswith(original_prefix + " send "))
            command = "rtk proxy " + shlex.quote(str(helper)) + ORIGINAL[len(original_prefix):]
            self.assertEqual(shlex.split(command)[3:], shlex.split(ORIGINAL)[3:])
            self.assertIn("; Codex alone", command)
            self.check_entrypoints(command, True)
            self.check_entrypoints(command + "; codex resume deadbeef", False,
                                   rejection="direct agent CLI launch")
            self.assertEqual(helper.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()

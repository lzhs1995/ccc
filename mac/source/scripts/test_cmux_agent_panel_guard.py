#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("cmux_agent_panel_guard.py")
SPEC = importlib.util.spec_from_file_location("cmux_agent_panel_guard", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


class PanelGuardTests(unittest.TestCase):
    def assert_allowed(self, command: str) -> None:
        ok, message = GUARD.validate_command(command)
        self.assertTrue(ok, message)

    def assert_blocked(self, command: str) -> None:
        ok, message = GUARD.validate_command(command)
        self.assertFalse(ok, message)

    def test_raw_coordination_and_reads_are_allowed(self) -> None:
        self.assert_allowed(
            'rtk cmux-agent ask surface:36 "STATUS: continue the existing task with Claude"'
        )
        self.assert_allowed("rtk cmux-agent read surface:36 120")

    def test_raw_task_text_is_communication_not_business_acceptance(self) -> None:
        self.assert_allowed(
            'rtk cmux-agent ask surface:36 "TASK:\\nreview this implementation"'
        )
        self.assert_allowed(
            'rtk cmux send --surface surface:36 -- "TASK:\\nrun the task"'
        )

    def test_raw_callback_and_guarded_task_entry_are_allowed(self) -> None:
        self.assert_allowed(
            'rtk cmux-agent ask surface:164 "DONE|task|nonce|REPORT=/tmp/report.md"'
        )
        self.assert_allowed(
            "rtk python3 dispatch.py cmux_bridge.submit_task_pack"
        )

    def test_direct_agent_launch_is_blocked(self) -> None:
        self.assert_blocked('rtk claude --resume "existing-id"')
        self.assert_blocked('rtk claude-safe-resume "existing-id"')
        self.assert_blocked("rtk claude-pinned")
        self.assert_blocked("env FOO=1 nohup codex")
        self.assertTrue(GUARD._shell_starts_agent_launch("setup && claude"))

    def test_exact_history_pinned_same_session_recovery_is_allowed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_id = "132509c7-e356-455d-af8d-645a9d832e5a"
            history = root / f"{session_id}.jsonl"
            history.write_bytes(b'{"sessionId":"same"}\n')
            auth = root / "recovery.json"
            auth.write_text(json.dumps({
                "schemaVersion": 1,
                "action": "resume-existing-executor-session",
                "executor": "claude",
                "sessionId": session_id,
                "previousSurfaceMissing": True,
                "requireSameSession": True,
                "forbidReplacement": True,
                "oneTimeByHistoryPin": True,
                "historyPath": str(history),
                "historyBytes": history.stat().st_size,
                "historySha256": hashlib.sha256(history.read_bytes()).hexdigest(),
                "recoverySurfaceRef": "surface:172",
            }), encoding="utf-8")
            command = (
                f"rtk env {GUARD.RECOVERY_AUTH_ENV}={auth} "
                f"claude --resume {session_id}"
            )
            self.assert_allowed(command)
            self.assert_allowed(
                f"rtk cmux send --surface surface:172 {command!r}"
            )
            self.assert_blocked(
                f"rtk cmux send --surface surface:173 {command!r}"
            )

            replacement = command.replace(session_id, "replacement-id", 1)
            self.assert_blocked(replacement)

            history.write_bytes(history.read_bytes() + b'{"appended":true}\n')
            self.assert_blocked(command)

    def test_agent_launch_pasted_into_terminal_is_blocked(self) -> None:
        self.assert_blocked(
            'rtk cmux send --surface surface:170 "claude --resume replacement"'
        )
        self.assert_blocked('rtk cmux-agent send surface:170 "claude"')

    def test_mentions_of_claude_are_not_launches(self) -> None:
        self.assert_allowed(
            'rtk cmux-agent ask surface:36 "Continue with Claude on the existing surface"'
        )
        self.assert_allowed("rtk rg -n Claude README.md")

    def test_python_file_update_with_multiline_agent_prose_is_allowed(self) -> None:
        # The original failure wrote a HANDOFF string; no agent was launched.
        for python in ("python3", "/opt/homebrew/bin/python3.14"):
            for delimiter in ("'PY'", '"PY"'):
                command = (
                    f"rtk proxy {python} -B - <<{delimiter}\n"
                    "from pathlib import Path\n"
                    "base = '/tmp/evidence'\n"
                    "body = f'''Checkpoint\n"
                    "Codex daemon48363 attribution: {base}/result.json\n"
                    "Claude original session retained.\n'''\n"
                    "Path('/tmp/HANDOFF.md').write_text(body)\nPY\n"
                )
                with self.subTest(python=python, delimiter=delimiter):
                    self.assert_allowed(command)

    def test_sqlite_disk_inventory_literals_are_not_agent_launches(self) -> None:
        command = ("rtk proxy python3 - <<'PY'\n"
                   "import json, sqlite3, shutil\n"
                   "from pathlib import Path\n"
                   "db=sqlite3.connect('file:/tmp/inventory.sqlite3?mode=ro', uri=True)\n"
                   "rows=db.execute('select state,count(*) from requests group by state').fetchall()\n"
                   "record={'scope':'Local inventory; Claude artifacts remain unchanged.', "
                   "'rows':rows, 'free':shutil.disk_usage('/tmp').free}\n"
                   "Path('/tmp/inventory.json').write_text(json.dumps(record))\nPY\n")
        self.assert_allowed(command)
        self.assert_blocked(command + 'claude --resume existing-id')

    def test_python_document_update_does_not_hide_adjacent_launch(self) -> None:
        document = "python3 - <<'PY'\ntext = '''\nCodex status\n'''\nPY\n"
        self.assert_blocked(document + "rtk claude --resume existing-id")
        self.assert_blocked("codex\n" + document)

    def test_python_helper_import_and_read_subprocess_do_not_turn_prose_into_shell(self) -> None:
        # Reduced original panel27 failure: the semicolon inside this JSON
        # value was treated as a shell boundary because subprocess was present.
        command = ("rtk proxy python3 -B - <<'PY'\n"
                   "import sys, json, subprocess\n"
                   "from pathlib import Path\n"
                   "import executor_ready as ready, executor_reply\n"
                   "out = subprocess.run(['rtk', 'cmux-agent', 'read', 'surface:9', '75'], "
                   "capture_output=True, text=True, check=True)\n"
                   "record = {'status': 'mechanical intake accepted; Claude review pending; "
                   "18 historical differences remain'}\n"
                   "Path('/tmp/report.json').write_text(json.dumps(record))\nPY\n")
        self.assert_allowed(command)
        for tool, key in (("Bash", "command"), ("exec_command", "cmd")):
            result = subprocess.run([sys.executable, "-B", str(MODULE_PATH)],
                input=json.dumps({"tool_name": tool, "tool_input": {key: command}}),
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_blocked(command + "rtk proxy claude --resume existing-id")

    def test_python_actual_execution_arguments_remain_protected(self) -> None:
        programs = [
            "import subprocess\nsubprocess.run(['claude', '--resume', 'id'])",
            "import subprocess as sp\nsp.Popen(['rtk', 'proxy', 'codex'])",
            "from subprocess import run as launch\ncmd=['codex']\nlaunch(cmd)",
            "import os\nlaunch=os.system\nnext_launch=launch\ncmd='true; codex'\nnext_launch(cmd)",
            "import os\ncommands={'resume':'claude --resume id'}\nos.system(commands['resume'])",
            "import subprocess\nsubprocess.run(args=['codex'])",
            "import subprocess\nsubprocess.run(['sh', '-c', 'claude --resume id'])",
            "import subprocess\nsubprocess.run(['true; codex'], shell=True)",
            "import os\ncmd='clau'+'de --resume id'\nos.system(cmd)",
        ]
        for program in programs:
            with self.subTest(program=program):
                self.assert_blocked("python3 - <<'PY'\n" + program + "\nPY\n")

    def test_python_argv_prose_is_not_a_command(self) -> None:
        for argv in (["echo", "Claude status"], ["echo", "ok; Claude review pending"],
                     ["printf", "%s", "\nCodex status\n"]):
            command = ("python3 - <<'PY'\nimport subprocess\n"
                       f"subprocess.run({argv!r})\nPY\n")
            self.assert_allowed(command)

    def test_nonliteral_or_executing_heredocs_are_not_exempted(self) -> None:
        commands = [
            "sh <<'SH'\ncodex\nSH\n",
            "python3 - <<PY\ntext = '''$(\ncodex\n)'''\nPY\n",
            "python3 - <<'PY' | sh\nprint('''\ncodex\n''')\nPY\n",
            "python3 - <<'PY'\nimport os\nos.system('''\ncodex\n''')\nPY\n",
            "python3 - <<'PY'\nfrom subprocess import run as launch\nlaunch('''\ncodex\n''', shell=True)\nPY\n",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assert_blocked(command)

    def test_python_document_update_is_allowed_at_hook_entry(self) -> None:
        command = ("rtk proxy python3 -B - <<'PY'\n"
                   "from pathlib import Path\n"
                   "Path('/tmp/HANDOFF.md').write_text('''Checkpoint\nCodex status\n''')\nPY\n")
        for tool, key in (("Bash", "command"), ("exec_command", "cmd")):
            payload = {"tool_name": tool, "tool_input": {key: command}}
            result = subprocess.run([sys.executable, "-B", str(MODULE_PATH)],
                input=json.dumps(payload), text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_executable_aliases_and_unknown_imports_keep_launch_scan(self) -> None:
        for setup in (
                "import os\nlaunch = os.system",
                "import os as operating_system\nlaunch = operating_system.popen",
                "launch = eval",
                "from os import system as launch",
                "import builtins as helpers\nlaunch = helpers.eval",
                "from some_package import launch",
                "from .pathlib import launch",
                "launch = globals",
                "launch = ().__class__",
        ):
            command = ("python3 - <<'PY'\n" + setup
                       + "\nlaunch('''\ncodex\n''')\nPY\n")
            with self.subTest(setup=setup):
                self.assert_blocked(command)

    def test_unknown_python_headers_and_invalid_programs_keep_launch_scan(self) -> None:
        for header in ('python3 -X unknown -', '"$TASK_PY/python3" -',
                       '"$(choose)/python3" -', 'python3 - > /tmp/out'):
            command = header + " <<'PY'\ntext = '''\ncodex\n'''\nPY\n"
            with self.subTest(header=header):
                self.assert_blocked(command)
        self.assert_blocked("python3 - <<'PY'\ntext = '''\ncodex\nPY\n")

    def test_non_ascii_document_offsets_and_crlf_preserve_shell_boundaries(self) -> None:
        command = ("python3 - <<'PY'\nfrom pathlib import Path\n"
                   "Path('/tmp/HANDOFF.md').write_text('''中文\u2028说明\n"
                   "Codex status\nClaude status\n''')\nPY\n")
        for rendered in (command, command.replace("\n", "\r\n")):
            with self.subTest(crlf="\r" in rendered):
                self.assert_allowed(rendered)
                self.assert_blocked(rendered + "claude --resume existing-id\n")

    def test_non_shell_tool_payload_is_not_treated_as_a_command(self) -> None:
        forbidden_example = "cmux new-" + "surface followed by claude"
        payload = {"toolName": "apply_patch", "input": {"patch": forbidden_example}}
        self.assertEqual(GUARD._extract_command(payload), "")

    def test_edit_command_field_is_document_content_at_real_hook(self) -> None:
        for name in ("apply_patch", "functions.apply_patch", "Write", "Edit"):
            with self.subTest(tool=name):
                payload = {"tool_name": name, "command": "claude --resume example"}
                result = subprocess.run([sys.executable, "-B", str(MODULE_PATH)],
                    input=json.dumps(payload), text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_shell_command_variants_remain_blocked_at_real_hook(self) -> None:
        payloads = [
            {"tool_name": "Bash", "tool_input": {"command": "claude --resume example"}},
            {"tool_name": "exec_command", "tool_input": {"cmd": "claude --resume example"}},
            {"tool_name": "functions.exec_command", "cmd": "claude --resume example"},
            {"command": "claude --resume example"},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                result = subprocess.run([sys.executable, "-B", str(MODULE_PATH)],
                    input=json.dumps(payload), text=True, capture_output=True)
                self.assertEqual(result.returncode, 2, result.stdout)

    def test_agent_new_surface_is_always_blocked(self) -> None:
        self.assert_blocked("rtk cmux new-surface --type agent-session --provider claude")
        self.assert_blocked("rtk cmux new-surface && claude")

    def test_non_agent_surface_and_side_split_remain_allowed(self) -> None:
        self.assert_allowed("rtk cmux new-surface --type browser")
        self.assert_allowed(
            "rtk cmux new-split right --surface surface:164 --focus true"
        )

    def test_harness_double_authorization_path_is_allowed(self) -> None:
        self.assert_allowed(
            "rtk python3 mac_harness.py preflight --task-id t "
            "--executor claude --spawn --spawn-authorized"
        )


if __name__ == "__main__":
    unittest.main()

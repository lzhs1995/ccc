"""Runtime deployment tests. Every write and every launchctl call is isolated."""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cmux_codex_watch as core


SOURCE = Path(__file__).resolve().parents[1]


class RuntimeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ccc-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "spaces & characters"
        self.source = self.root / "Documents" / "checkout"
        self.source.mkdir(parents=True)
        for name in core.RUNTIME_FILES:
            (self.source / name).write_text("VALUE = 1\n", encoding="utf-8")
        self.app = self.root / "Library" / "Application Support" / core.APP_NAME
        self.runtime = self.app / "runtime"
        self.plist = self.root / "LaunchAgents" / "watcher.plist"
        self.logs = self.root / "Logs"
        for key, value in {
            "PROJECT_DIR": self.source,
            "DEFAULT_RUNTIME_ROOT": self.runtime,
            "DEFAULT_PLIST_PATH": self.plist,
            "DEFAULT_LOG_DIR": self.logs,
        }.items():
            patcher = mock.patch.object(core, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.launcher = mock.patch.object(core, "_run_launchctl")
        self.launch = self.launcher.start()
        self.launch.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.addCleanup(self.launcher.stop)
        links = mock.patch.object(core, "install_cli_link")
        links.start()
        self.addCleanup(links.stop)

    def old_install(self):
        release = core.stage_runtime_release()
        core._set_runtime_pointer(self.runtime, release)
        core.write_plist(runtime_dir=release)
        return release, self.plist.read_bytes()

    def change_source(self):
        (self.source / core.RUNTIME_FILES[0]).write_text("VALUE = 2\n", encoding="utf-8")

    def test_stage_is_complete_versioned_and_not_activated(self):
        release = core.stage_runtime_release()
        self.assertEqual(release.parent, self.runtime / "releases")
        self.assertFalse((self.runtime / "current").exists())
        for name in core.RUNTIME_FILES:
            self.assertEqual((release / name).read_bytes(), (self.source / name).read_bytes())
            self.assertFalse((release / name).is_symlink())
            self.assertEqual((release / name).stat().st_mode & 0o777, 0o600)
        core.validate_runtime_release(release)
        self.assertEqual(core.stage_runtime_release(), release)
        self.change_source()
        self.assertNotEqual(core.stage_runtime_release(), release)
        self.launch.assert_not_called()

    def test_installed_modules_load_with_checkout_removed(self):
        for name in core.RUNTIME_FILES:
            shutil.copyfile(SOURCE / name, self.source / name)
        release = core.stage_runtime_release()
        shutil.rmtree(self.source)
        result = subprocess.run(
            [sys.executable, "-B", "-E", "-s", str(release / "cmux_codex_watch.py"), "--help"],
            cwd=self.root, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hook-audit", result.stdout)
        # Empty input cannot create a Hook event or write to a real journal.
        hook = subprocess.run(
            [sys.executable, "-B", "-E", "-s", str(release / "claude_ccc_event_hook.py")],
            input="{}", cwd=self.root, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(hook.returncode, 0, hook.stderr)

    def test_copy_failure_keeps_previous_current_and_removes_partial_stage(self):
        old, old_plist = self.old_install()
        self.change_source()
        real_write = core._atomic_write_bytes

        def fail_protocol(path, data):
            if path.name == "claude_ccc_protocol.py":
                raise OSError("injected copy failure")
            real_write(path, data)

        with mock.patch.object(core, "_atomic_write_bytes", side_effect=fail_protocol):
            with self.assertRaisesRegex(OSError, "injected copy"):
                core.launchctl("install")
        self.assertEqual((self.runtime / "current").resolve(), old)
        self.assertEqual(self.plist.read_bytes(), old_plist)
        self.assertEqual(list((self.runtime / "releases").glob(".stage-*")), [])
        self.launch.assert_not_called()

    def test_tampered_release_is_not_overwritten_or_accepted(self):
        release = core.stage_runtime_release()
        (release / "claude_ccc_protocol.py").write_text("VALUE = 99\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "incomplete or changed"):
            core.stage_runtime_release()
        self.assertEqual((release / "claude_ccc_protocol.py").read_text(), "VALUE = 99\n")

    def test_plist_pins_deployed_files_and_escapes_paths(self):
        release = core.stage_runtime_release()
        core.write_plist(runtime_dir=release)
        document = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(document["ProgramArguments"], [
            core.DEFAULT_PYTHON, str(release / "cmux_codex_watch.py"), "watch",
        ])
        self.assertEqual(document["WorkingDirectory"], str(release))
        self.assertNotIn(str(self.source), self.plist.read_text())
        self.assertIn("&amp;", self.plist.read_text())

    def test_bootstrap_failure_restores_old_runtime_plist_and_data(self):
        old, old_plist = self.old_install()
        protected = {}
        for name in ("config.json", "state.json", "claude-event-ledger.json"):
            path = self.app / name
            path.write_text(json.dumps({"sent": "must not rewind", "file": name}))
            protected[path] = path.read_bytes()
        self.change_source()
        bootstraps = []

        def launch(args, check):
            if args[0] == "bootstrap":
                bootstraps.append(self.plist.read_bytes())
                if len(bootstraps) == 1:
                    raise RuntimeError("injected bootstrap failure")
            return subprocess.CompletedProcess(args, 0, "", "")

        self.launch.side_effect = launch
        with self.assertRaisesRegex(RuntimeError, "previous runtime pointer and plist restored"):
            core.launchctl("install")
        self.assertEqual((self.runtime / "current").resolve(), old)
        self.assertEqual(self.plist.read_bytes(), old_plist)
        self.assertEqual(bootstraps[-1], old_plist)
        for path, content in protected.items():
            self.assertEqual(path.read_bytes(), content)

    def test_failed_first_install_removes_only_new_activation(self):
        def launch(args, check):
            if args[0] == "print":
                return subprocess.CompletedProcess(args, 1, "", "not loaded")
            if args[0] == "bootstrap":
                raise RuntimeError("injected bootstrap failure")
            return subprocess.CompletedProcess(args, 0, "", "")

        self.launch.side_effect = launch
        with self.assertRaisesRegex(RuntimeError, "runtime install failed"):
            core.launchctl("install")
        self.assertFalse((self.runtime / "current").is_symlink())
        self.assertFalse(self.plist.exists())
        # The complete staged version remains available for inspection/retry.
        self.assertEqual(len(list((self.runtime / "releases").iterdir())), 1)

    def test_rollback_failure_is_reported(self):
        self.old_install()
        self.change_source()

        def launch(args, check):
            if args[0] == "bootstrap":
                raise RuntimeError("bootstrap unavailable")
            return subprocess.CompletedProcess(args, 0, "", "")

        self.launch.side_effect = launch
        with self.assertRaisesRegex(RuntimeError, "rollback failed: bootstrap unavailable"):
            core.launchctl("install")

    def test_invalid_source_does_not_stop_old_service(self):
        old, old_plist = self.old_install()
        (self.source / "claude_ccc_protocol.py").write_text("invalid python ???\n")
        with self.assertRaises(SyntaxError):
            core.launchctl("install")
        self.launch.assert_not_called()
        self.assertEqual((self.runtime / "current").resolve(), old)
        self.assertEqual(self.plist.read_bytes(), old_plist)

    def test_start_uses_installed_version_after_checkout_changes(self):
        core.launchctl("install")
        release = (self.runtime / "current").resolve()
        installed = (release / "cmux_codex_watch.py").read_bytes()
        self.change_source()
        with mock.patch.object(core, "stage_runtime_release", side_effect=AssertionError("must not redeploy")):
            core.launchctl("start")
        self.assertEqual((release / "cmux_codex_watch.py").read_bytes(), installed)
        self.assertEqual(plistlib.loads(self.plist.read_bytes())["WorkingDirectory"], str(release))
        self.assertTrue(self.logs.is_dir())

    def test_existing_current_directory_is_preserved(self):
        (self.runtime / "current").mkdir(parents=True)
        sentinel = self.runtime / "current" / "keep.txt"
        sentinel.write_text("preserve")
        with self.assertRaisesRegex(RuntimeError, "must be a symlink"):
            core.launchctl("install")
        self.assertEqual(sentinel.read_text(), "preserve")
        self.launch.assert_not_called()

    def test_source_and_deployed_modules_agree_on_hook_command(self):
        for name in core.RUNTIME_FILES:
            shutil.copyfile(SOURCE / name, self.source / name)
        release = core.stage_runtime_release()
        commands = []
        for location in (self.source, release):
            result = subprocess.run(
                [sys.executable, "-B", "-E", "-s", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); "
                 "import cmux_codex_watch as c; print(c.CLAUDE_HOOK_COMMAND)", str(location)],
                cwd=self.root, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            commands.append(result.stdout.strip())
        self.assertEqual(commands[0], commands[1])
        self.assertIn("/runtime/current/claude_ccc_event_hook.py", commands[0])
        self.assertNotIn("Documents", commands[0])


class RuntimeHookMigrationTests(unittest.TestCase):
    def test_legacy_commands_are_detected_and_narrowly_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "settings.json"
            legacy = '/opt/homebrew/bin/python3 "/old/Documents/claude_ccc_event_hook.py"'
            original = {
                "env": {"fixture": "untouched"}, "model": "keep-model",
                "hooks": {name: [{"matcher": "*", "hooks": [
                    {"type": "command", "command": legacy, "timeout": 5},
                    {"type": "command", "command": "other-hook"},
                ]}] for name in core.CLAUDE_HOOK_EVENTS},
            }
            original["hooks"]["PreToolUse"] = [{"hooks": [{"command": "another-hook"}]}]
            raw = json.dumps(original).encode()
            settings_path.write_bytes(raw)
            manager = core.ClaudeHookSettingsManager(
                settings_path, lock_path=root / "settings.lock", backup_dir=root / "backups",
            )
            before = manager.inspect()
            self.assertEqual(before["event_counts"], dict.fromkeys(core.CLAUDE_HOOK_EVENTS, 1))
            self.assertFalse(before["healthy"])
            report = manager.ensure(repair=True, automatic=False)
            self.assertTrue(report["healthy"])
            self.assertTrue(report["changed"])
            self.assertEqual(Path(report["backup_path"]).read_bytes(), raw)
            changed = json.loads(settings_path.read_bytes())
            self.assertEqual(changed["env"], original["env"])
            self.assertEqual(changed["model"], original["model"])
            self.assertEqual(changed["hooks"]["PreToolUse"], original["hooks"]["PreToolUse"])
            for name in core.CLAUDE_HOOK_EVENTS:
                commands = changed["hooks"][name][0]["hooks"]
                self.assertEqual(commands[0]["command"], core.CLAUDE_HOOK_COMMAND)
                self.assertEqual(commands[1], original["hooks"][name][0]["hooks"][1])
            self.assertFalse(manager.ensure(repair=True, automatic=False)["changed"])


if __name__ == "__main__":
    unittest.main()

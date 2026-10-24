"""Isolated native installer process regression tests; never send input."""
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import cmux_native_delivery as native

SURFACE = "11111111-1111-4111-8111-111111111111"
WORKSPACE = "22222222-2222-4222-8222-222222222222"


class VersionedClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.addCleanup(patch.stopall)
        patch.object(Path, "home", return_value=self.home).start()
        self.exe = self.home / ".local/share/claude/versions/2.1.303"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"isolated native executable fixture")
        self.exe.chmod(0o755)
        st = self.exe.stat()
        self.process = dict(pid=1234, birth=[100, 123], ppid=42,
                            executable=str(self.exe), argv=[str(self.exe)],
                            file_identity=[st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns],
                            env=dict(CMUX_SURFACE_ID=SURFACE, CMUX_WORKSPACE_ID=WORKSPACE))
        self.row = dict(target_surface_uuid=SURFACE, workspace_uuid=WORKSPACE, tty="ttys041")

    def select(self, processes, labels=None, tty="ttys041", drift=False):
        labels = labels or [p["executable"] for p in processes]
        listing = "\n".join(str(p["pid"]) + " " + label for p, label in zip(processes, labels))
        reads = {}
        def process(pid, **kwargs):
            result = copy.deepcopy(next(p for p in processes if p["pid"] == pid))
            reads[pid] = reads.get(pid, 0) + 1
            if drift and reads[pid] > 1:
                result["birth"][1] += 1
            return result
        def run(argv, **kwargs):
            if "-U" in argv:
                self.assertIn("-ww", argv)
                return subprocess.CompletedProcess(argv, 0, stdout=listing)
            return subprocess.CompletedProcess(argv, 0, stdout=tty)
        with patch.object(native.subprocess, "run", side_effect=run), \
             patch.object(native.identity, "process", side_effect=process):
            return native._target_process(self.row)

    def test_native_install_path_and_exact_argv_are_claude(self):
        self.assertEqual(native._provider(self.process), "claude")

    def test_old_native_and_node_providers_unchanged(self):
        for name, expected in [("codex", "codex"), ("claude", "claude"), ("claude.exe", "claude")]:
            self.assertEqual(native._provider(dict(executable="/usr/local/bin/" + name)), expected)
        self.assertEqual(native._provider(dict(executable="/usr/local/bin/node",
            argv=["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"])), "claude")

    def test_native_launcher_symlink_may_resolve_to_same_kernel_executable(self):
        alias = self.home / "claude"
        alias.symlink_to(self.exe)
        self.process["argv"] = [str(alias)]
        self.assertEqual(native._provider(self.process), "claude")

    def test_full_path_and_short_kernel_label_both_discover_same_process(self):
        for label in [str(self.exe), "2.1.303"]:
            self.assertEqual(self.select([self.process], [label]), self.process)

    def test_version_name_outside_install_root_is_not_provider(self):
        self.process["executable"] = str(self.home / "other/2.1.303")
        self.assertIsNone(native._provider(self.process))
        with self.assertRaisesRegex(native.NativeDeliveryError, "NOT_UNIQUE"):
            self.select([self.process])

    def test_other_home_and_nested_paths_are_rejected(self):
        for path in ["/Users/other/.local/share/claude/versions/2.1.303",
                     str(self.exe.parent / "extra/2.1.303")]:
            self.process["executable"] = path
            self.assertIsNone(native._provider(self.process))

    def test_arbitrary_version_syntax_is_rejected(self):
        for name in ["2.1.303.exe", "2.1.303-beta", "２.1.303", "2.1", "claude-2.1.303"]:
            self.process["executable"] = str(self.exe.parent / name)
            self.assertIsNone(native._provider(self.process))

    def test_relative_or_different_argv0_is_rejected(self):
        for arg in ["2.1.303", "/bin/sh"]:
            self.process["argv"] = [arg]
            self.assertIsNone(native._provider(self.process))

    def test_kernel_path_must_be_canonical(self):
        alias = self.exe.parent / "2.1.304"
        alias.symlink_to(self.exe)
        self.process["executable"] = str(alias)
        self.process["argv"] = [str(alias)]
        self.assertIsNone(native._provider(self.process))

    def test_mutated_executable_identity_is_rejected(self):
        self.exe.write_bytes(b"replaced")
        self.assertIsNone(native._provider(self.process))

    def test_unprotected_or_nonexecutable_version_file_is_rejected(self):
        for mode in [0o644, 0o777, 0o775]:
            self.exe.chmod(mode)
            self.assertIsNone(native._provider(self.process))

    def test_wrong_workspace_remains_rejected(self):
        self.process["env"]["CMUX_WORKSPACE_ID"] = "OTHER"
        with self.assertRaisesRegex(native.NativeDeliveryError, "WORKSPACE_MISMATCH"):
            self.select([self.process])

    def test_wrong_tty_remains_rejected(self):
        with self.assertRaisesRegex(native.NativeDeliveryError, "TTY_MISMATCH"):
            self.select([self.process], tty="ttys999")

    def test_changed_birth_remains_rejected(self):
        with self.assertRaisesRegex(native.NativeDeliveryError, "PROCESS_CHANGED"):
            self.select([self.process], drift=True)

    def test_multiple_versioned_processes_remain_ambiguous(self):
        other = copy.deepcopy(self.process)
        other["pid"] += 1
        with self.assertRaisesRegex(native.NativeDeliveryError, "NOT_UNIQUE"):
            self.select([self.process, other])

    def test_other_surface_is_not_selected(self):
        self.process["env"]["CMUX_SURFACE_ID"] = "OTHER"
        with self.assertRaisesRegex(native.NativeDeliveryError, "NOT_UNIQUE"):
            self.select([self.process])


if __name__ == "__main__":
    unittest.main()

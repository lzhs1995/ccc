import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("retire", Path(__file__).parents[1] / "scripts/retire_codex_copy_loop.py")
retire = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retire)


class RetirementTests(unittest.TestCase):
    def test_remove_only_the_exact_legacy_background_invocation(self):
        unrelated = 'echo "codex-tcc-keep/grant.py"\nexport KEEP_ME=1\n'
        text = unrelated + retire.COMMENT + "\n" + retire.TRIGGER + "\n"
        self.assertEqual(retire.shell_without_trigger(text), unrelated)
        self.assertEqual(retire.shell_without_trigger(unrelated), unrelated)

    def test_backups_and_repeated_runs_preserve_unrelated_files(self):
        with tempfile.TemporaryDirectory() as root:
            user_home = Path(root)
            shell = user_home / ".zshrc"
            shell.write_text("echo keep\n" + retire.COMMENT + "\n" + retire.TRIGGER + "\n")
            directory = user_home / "Library/Application Support/codex-tcc-keep"
            directory.mkdir(parents=True)
            helper = directory / "grant.py"
            helper.write_text('def refresh_stable_copy(source):\n    return ".codex.new"\n')
            helper.chmod(0o755)
            sentinel = user_home / "unrelated"
            sentinel.write_text("unchanged")
            plan = retire.changes(user_home)
            self.assertIn(retire.TRIGGER, shell.read_text())  # preview is read-only
            records = retire.apply_changes(plan, user_home / "backup")
            self.assertEqual(shell.read_text(), "echo keep\n")
            self.assertIn(retire.TRIGGER, Path(records[0]["backup"]).read_text())
            self.assertEqual(sentinel.read_text(), "unchanged")
            self.assertEqual(retire.changes(user_home), [])
            proc = subprocess.run([sys.executable, str(helper)], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(helper.stat().st_mode & 0o777, 0o755)

    def test_unrecognized_helper_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "Library/Application Support/codex-tcc-keep"
            directory.mkdir(parents=True)
            helper = directory / "update-stable.sh"
            helper.write_text("#!/bin/sh\necho unrelated\n")
            with self.assertRaisesRegex(RuntimeError, "Unrecognized helper"):
                retire.changes(Path(root))
            self.assertEqual(helper.read_text(), "#!/bin/sh\necho unrelated\n")


if __name__ == "__main__":
    unittest.main()

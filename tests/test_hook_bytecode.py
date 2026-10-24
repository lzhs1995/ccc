"""Legacy hook commands must not mutate a frozen runtime during an import."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class HookBytecodeTests(unittest.TestCase):
    def test_legacy_direct_and_current_symlink_commands_leave_no_bytecode(self):
        source = Path(__file__).resolve().parents[1]
        for linked in (False, True):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                release = root / "release with spaces"
                release.mkdir()
                for name in ("claude_ccc_event_hook.py", "claude_ccc_protocol.py"):
                    shutil.copy2(source / name, release / name)
                entry = release / "claude_ccc_event_hook.py"
                if linked:
                    (root / "current").symlink_to(release, target_is_directory=True)
                    entry = root / "current" / entry.name
                before = {str(p.relative_to(release)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in release.rglob("*") if p.is_file()}
                # Deliberately omit -B, as already-running Claude clients can
                # retain the old registered command. -E ignores the parent's
                # bytecode environment. An unsupported event emits no live
                # CCC event and performs no daemon or journal interaction.
                result = subprocess.run(
                    [sys.executable, "-E", "-s", str(entry)],
                    input="{}", capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {})
                after = {str(p.relative_to(release)): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in release.rglob("*") if p.is_file()}
                self.assertEqual(after, before,
                                 "an automatically invoked legacy hook must not add bytecode")


if __name__ == "__main__":
    unittest.main()

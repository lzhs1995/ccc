import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class CliBytecodeTests(unittest.TestCase):
    def test_direct_and_symlinked_cli_preserve_release_inventory(self):
        launcher = Path(__file__).resolve().parents[1] / "bin/cmux-codex-continue"
        for linked in (False, True):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                release = root / "release with spaces"
                (release / "bin").mkdir(parents=True)
                entry = release / "bin/cmux-codex-continue"
                shutil.copy2(launcher, entry)
                (release / "helper.py").write_text("VALUE = 'ready'\n")
                (release / "cmux_codex_watch.py").write_text(
                    "import helper, json, sys\n"
                    "print(json.dumps([helper.VALUE, sys.argv[1:]]))\n"
                )
                if linked:
                    (root / "relative-link").symlink_to(
                        "release with spaces/bin/cmux-codex-continue"
                    )
                    entry = root / "ccc"
                    entry.symlink_to(root / "relative-link")
                before = sorted(str(p.relative_to(release)) for p in release.rglob("*"))
                environment = dict(os.environ)
                environment.pop("PYTHONDONTWRITEBYTECODE", None)
                environment.pop("PYTHONPYCACHEPREFIX", None)
                environment["PYTHON"] = sys.executable
                result = subprocess.run(
                    [str(entry), "--help", "argument with spaces"],
                    env=environment, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), ["ready", ["--help", "argument with spaces"]])
                self.assertEqual(
                    sorted(str(p.relative_to(release)) for p in release.rglob("*")),
                    before,
                    "the CLI must not add Python bytecode to its frozen release",
                )


if __name__ == "__main__":
    unittest.main()

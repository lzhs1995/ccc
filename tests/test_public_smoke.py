#!/usr/bin/env python3
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicSmokeTests(unittest.TestCase):
    def test_modules_compile_and_import(self):
        files = [ROOT / name for name in (
            "cmux_supervisor_tui.py", "cmux_codex_watch.py",
            "ccc_session_audit.py", "ccp_new.py", "bin/cmux-stack",
        )]
        subprocess.run([sys.executable, "-m", "py_compile", *map(str, files)], check=True)
        sys.path.insert(0, str(ROOT))
        import cmux_supervisor_tui  # noqa: F401
        import cmux_codex_watch  # noqa: F401
        import ccc_session_audit  # noqa: F401

    def test_stack_uses_portable_label_overrides(self):
        text = (ROOT / "bin/cmux-stack").read_text(encoding="utf-8")
        self.assertIn("CMUX_STACK_WATCHER_LABEL", text)
        self.assertIn("CMUX_STACK_JANITOR_LABEL", text)

    def test_no_private_path_in_public_source(self):
        for path in ROOT.rglob("*"):
            if (
                path.is_file()
                and ".git" not in path.parts
                and "__pycache__" not in path.parts
                and path.suffix in {".py", ".sh", ".command", ".env", ".json", ".md", ".txt", ".template"}
            ):
                raw = path.read_text(encoding="utf-8", errors="ignore")
                self.assertNotIn("/Users/" + "lzhs", raw, str(path))
                self.assertNotIn("com." + "lzhs", raw, str(path))


if __name__ == "__main__":
    unittest.main()

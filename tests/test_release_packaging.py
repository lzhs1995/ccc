"""Exercise release integrity and plist rendering without live service actions."""
import hashlib
import json
import os
import plistlib
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cmux_codex_watch as core
from scripts.package_release import package
from scripts.render_launchagents import render


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ccc-package-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "source checkout"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "CCC test")
        self.git("config", "user.email", "ccc-test@example.invalid")
        (self.repo / "VERSION").write_text("0.2.0\n")
        (self.repo / "entry").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "entry").chmod(0o755)
        (self.repo / ".gitignore").write_text("private.json\n")
        self.git("add", ".")
        self.git("commit", "-qm", "release fixture")
        (self.repo / "private.json").write_text('{"private": "must not package"}')

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.repo), *args], stderr=subprocess.STDOUT)

    def test_reproducible_archive_manifest_permissions_and_no_untracked_data(self):
        first = package(self.repo, self.root / "one")
        second = package(self.repo, self.root / "two")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        manifest = json.loads((first.parent / "RELEASE-MANIFEST.json").read_text())
        self.assertEqual(manifest["commit"], self.git("rev-parse", "HEAD").decode().strip())
        self.assertEqual(manifest["version"], "0.2.0")
        with tarfile.open(first) as archive:
            prefix = "ccc-0.2.0/"
            names = {m.name.removeprefix(prefix) for m in archive.getmembers()}
            self.assertEqual(names, set(manifest["files"]) | {"RELEASE-MANIFEST.json"})
            self.assertNotIn("private.json", names)
            self.assertEqual(archive.getmember(prefix + "entry").mode, 0o755)
            self.assertEqual(archive.extractfile(prefix + "RELEASE-MANIFEST.json").read(),
                             (first.parent / "RELEASE-MANIFEST.json").read_bytes())
            for name, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.extractfile(prefix + name).read()).hexdigest(), digest)
        for line in (first.parent / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ")
            self.assertEqual(hashlib.sha256((first.parent / name).read_bytes()).hexdigest(), digest)

    def test_modified_and_untracked_files_prevent_packaging(self):
        for path in (self.repo / "entry", self.repo / "untracked.txt"):
            with self.subTest(path=path.name):
                original = path.read_bytes() if path.exists() else None
                path.write_text("uncommitted data")
                with self.assertRaisesRegex(RuntimeError, "clean committed tree"):
                    package(self.repo, self.root / "out")
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_symlinks_are_not_followed_into_private_files(self):
        (self.repo / "link").symlink_to(self.repo / "private.json")
        self.git("add", "link")
        self.git("commit", "-qm", "unsupported symlink")
        with self.assertRaisesRegex(RuntimeError, "unsupported release entry"):
            package(self.repo, self.root / "out")


class LaunchAgentRenderingTests(unittest.TestCase):
    def test_renderer_pins_verified_runtime_without_activation(self):
        with tempfile.TemporaryDirectory(prefix="ccc-render-test-") as directory:
            root = Path(directory) / "spaces & characters"
            source = root / "source"
            source.mkdir(parents=True)
            for name in core.RUNTIME_FILES:
                (source / name).write_text("VALUE = 1\n")
            runtime = root / "runtime"
            release = core.stage_runtime_release(source, runtime).resolve()
            core._set_runtime_pointer(runtime, release)
            pointer = os.readlink(runtime / "current")
            with mock.patch.object(core, "_run_launchctl", side_effect=AssertionError("must not activate")):
                paths = render(root / "plists", runtime, root / "janitor")
            self.assertEqual(os.readlink(runtime / "current"), pointer)
            payloads = [plistlib.loads(path.read_bytes()) for path in paths]
            self.assertEqual(payloads[0]["ProgramArguments"][1], str(release / "cmux_codex_watch.py"))
            self.assertEqual(payloads[0]["WorkingDirectory"], str(release))
            self.assertEqual(payloads[1]["StartInterval"], 1800)
            self.assertEqual(payloads[2]["StartInterval"], 60)
            self.assertEqual(payloads[3]["StartInterval"], 300)
            self.assertEqual(payloads[3]["ProgramArguments"][-1], str(root / "janitor" / "expire.sh"))
            self.assertNotIn(str(source), paths[0].read_text())
            (release / "ccc_observation.py").write_text("tampered\n")
            with self.assertRaisesRegex(RuntimeError, "incomplete or changed"):
                render(root / "bad", runtime, root / "janitor")
            self.assertFalse((root / "bad").exists())
            self.assertEqual(os.readlink(runtime / "current"), pointer)

    def test_renderer_requires_an_existing_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(FileNotFoundError):
                render(root / "plists", root / "absent-runtime", root / "janitor")
            self.assertFalse((root / "plists").exists())


if __name__ == "__main__":
    unittest.main()

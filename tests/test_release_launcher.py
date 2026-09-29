import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import release_launcher  # noqa: E402


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ReleaseLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.release = self.root / "release"
        self.release.mkdir()
        self.entrypoint = self.release / "cmux_codex_watch.py"
        self.entrypoint.write_text("print('verified release')\n", encoding="utf-8")
        self.config = self.root / "config.json"
        self.config.write_text('{"mode":"armed"}\n', encoding="utf-8")
        self.manifest = self.root / "approved-release.json"
        self._write_manifest()
        self.entrypoint.chmod(0o444)
        self.release.chmod(0o555)

    def tearDown(self):
        self.release.chmod(0o755)
        self.entrypoint.chmod(0o644)
        self.temp.cleanup()

    def _write_manifest(self):
        self.manifest.write_text(json.dumps({
            "schema_version": 1,
            "release_id": "test-release",
            "approved_by": "unit-test",
            "approved_at": "2026-08-27T00:00:00Z",
            "release_root": str(self.release),
            "python": sys.executable,
            "entrypoint": self.entrypoint.name,
            "files": {self.entrypoint.name: digest(self.entrypoint)},
            "config": {"path": str(self.config), "sha256": digest(self.config)},
        }), encoding="utf-8")

    def test_valid_read_only_release_is_verified_without_running(self):
        result = release_launcher.verify_manifest(self.manifest)
        self.assertEqual(result["release_id"], "test-release")
        self.assertEqual(result["files"][self.entrypoint.name], digest(self.entrypoint))
        self.assertEqual(release_launcher.main(["--manifest", str(self.manifest)]), 0)

    def test_tampered_release_is_rejected(self):
        self.release.chmod(0o755)
        self.entrypoint.chmod(0o644)
        self.entrypoint.write_text("print('tampered')\n", encoding="utf-8")
        self.entrypoint.chmod(0o444)
        self.release.chmod(0o555)
        with self.assertRaises(release_launcher.ReleaseVerificationError):
            release_launcher.verify_manifest(self.manifest)

    def test_writable_release_is_rejected_even_when_digest_matches(self):
        self.release.chmod(0o755)
        self.entrypoint.chmod(0o644)
        self._write_manifest()
        with self.assertRaisesRegex(
            release_launcher.ReleaseVerificationError, "release_root must have no write",
        ):
            release_launcher.verify_manifest(self.manifest)

    def test_symlink_release_root_is_rejected(self):
        alias = self.root / "release-link"
        alias.symlink_to(self.release, target_is_directory=True)
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        value["release_root"] = str(alias)
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(
            release_launcher.ReleaseVerificationError, "release_root must not be a symlink",
        ):
            release_launcher.verify_manifest(self.manifest)

    def test_symlink_release_file_is_rejected_even_when_digest_matches(self):
        external = self.root / "external.py"
        external.write_text("print('outside release')\n", encoding="utf-8")
        linked = self.release / "linked.py"
        self.release.chmod(0o755)
        linked.symlink_to(external)
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        value["entrypoint"] = linked.name
        value["files"] = {linked.name: digest(external)}
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        self.release.chmod(0o555)
        with self.assertRaisesRegex(
            release_launcher.ReleaseVerificationError, "not a symlink",
        ):
            release_launcher.verify_manifest(self.manifest)

    def test_entrypoint_must_be_inside_hashed_file_set(self):
        self.release.chmod(0o755)
        self.entrypoint.chmod(0o644)
        value = json.loads(self.manifest.read_text(encoding="utf-8"))
        value["entrypoint"] = "other.py"
        self.manifest.write_text(json.dumps(value), encoding="utf-8")
        self.entrypoint.chmod(0o444)
        self.release.chmod(0o555)
        with self.assertRaisesRegex(
            release_launcher.ReleaseVerificationError, "entrypoint must be present",
        ):
            release_launcher.verify_manifest(self.manifest)


if __name__ == "__main__":
    unittest.main()

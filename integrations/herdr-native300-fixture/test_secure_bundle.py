from pathlib import Path
import base64
import io
import json
import os
import stat
import tempfile
import unittest
import warnings
from unittest.mock import patch
import zipfile
from cryptography.exceptions import InvalidTag
import secure_bundle as s


def fixture(change_name=None, mode=stat.S_IFREG | 0o600, corrupt=False):
    files = {"source/%04d.py" % n: b"pass\n" for n in range(562)}
    manifest = json.dumps({"files": {n: s.sha(b) for n, b in files.items()}}).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
        for index, (name, data) in enumerate(files.items()):
            item = zipfile.ZipInfo(change_name if index == 0 and change_name else name)
            item.external_attr = mode << 16
            z.writestr(item, data + b"changed" if corrupt and index == 0 else data)
        item = zipfile.ZipInfo("PUBLIC-SOURCE-MANIFEST.json")
        item.external_attr = (stat.S_IFREG | 0o600) << 16
        z.writestr(item, manifest)
    return stream.getvalue(), s.sha(manifest)


class SecureSource(unittest.TestCase):
    def test_authentication_and_random_nonce(self):
        key = os.urandom(32)
        a = s.seal(b"original source", key, b"bound manifest")
        b = s.seal(b"original source", key, b"bound manifest")
        self.assertNotEqual(a[:12], b[:12])
        self.assertEqual(s.unseal(a, key, b"bound manifest"), b"original source")

    def test_wrong_key_or_aad_or_ciphertext_refused(self):
        key = os.urandom(32)
        data = s.seal(b"original", key, b"manifest")
        for ciphertext, k, aad in ((data, os.urandom(32), b"manifest"), (data, key, b"other"),
                                   (data[:-1] + bytes([data[-1] ^ 1]), key, b"manifest")):
            with self.subTest(aad=aad), self.assertRaises(InvalidTag):
                s.unseal(ciphertext, k, aad)

    def test_secret_removed_before_child_can_inherit(self):
        value = os.urandom(32)
        with patch.dict(os.environ, {s.KEY_ENV: base64.b64encode(value).decode()}):
            self.assertEqual(s.key_from_environment(), value)
            self.assertNotIn(s.KEY_ENV, os.environ)

    def test_invalid_key_refused_and_removed(self):
        with patch.dict(os.environ, {s.KEY_ENV: base64.b64encode(b"short").decode()}):
            with self.assertRaises(ValueError): s.key_from_environment()
            self.assertNotIn(s.KEY_ENV, os.environ)

    def test_complete_source_extracts_all_original_bytes(self):
        raw, manifest = fixture()
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "original"
            s.extract(raw, output, manifest)
            self.assertEqual(len(list(output.rglob("*.py"))), 562)
            self.assertEqual((output / "source/0000.py").read_bytes(), b"pass\n")
            with self.assertRaisesRegex(ValueError, "already exists"):
                s.extract(raw, output, manifest)

    def test_changed_source_and_manifest_refused(self):
        raw, manifest = fixture(corrupt=True)
        with self.assertRaisesRegex(ValueError, "source bytes"):
            s.checked_archive(raw, manifest)
        raw, _ = fixture()
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            s.checked_archive(raw, "0" * 64)

    def test_path_escape_and_duplicate_refused_before_write(self):
        for name in ("../escape", "/absolute", "source\\escape", "source/0001.py", "source//bad"):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                raw, manifest = fixture(change_name=name)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe source member"):
                s.checked_archive(raw, manifest)

    def test_symlink_and_special_file_refused(self):
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600):
            raw, manifest = fixture(mode=mode)
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "nonregular"):
                s.checked_archive(raw, manifest)

    def test_no_local_runner_decryption(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            s.runner_root()

    def test_bounded_diagnostics_are_encrypted_not_plaintext(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            root = base / "private"
            output = base / "export"
            root.mkdir(); output.mkdir()
            marker = b"PRIVATE_ORIGINAL_DIAGNOSTIC_TEXT"
            (root / "fixture.log").write_bytes(marker)
            (root / "pair").mkdir()
            (root / "pair/old-screen.txt").write_bytes(b"OLD_SCREEN_MUST_NOT_BE_INCLUDED")
            key = os.urandom(32)
            env = {s.KEY_ENV: base64.b64encode(key).decode(), "GITHUB_REPOSITORY": "lzhs1995/ccc",
                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "test-head"}
            with patch.object(s, "runner_root", return_value=base), patch.dict(os.environ, env):
                s.seal_diagnostics(root, output)
            metadata = json.loads((output / "PRIVATE-DIAGNOSTICS.json").read_bytes())
            encrypted = (output / "private-diagnostics.aesgcm").read_bytes()
            self.assertNotIn(marker, encrypted)
            self.assertNotIn(marker, (output / "PRIVATE-DIAGNOSTICS.json").read_bytes())
            plaintext = s.unseal(encrypted, key, base64.b64decode(metadata["aad_base64"]))
            with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
                self.assertEqual(archive.namelist(), ["fixture.log"])
                self.assertEqual(archive.read("fixture.log"), marker)

    def test_diagnostic_growth_is_read_at_most_budget_plus_one(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path = root / "fixture.log"
            path.write_bytes(b"four")
            original = os.fdopen
            sizes = []
            class GrowingFile:
                def __init__(self, stream): self.stream = stream
                def __enter__(self): return self
                def __exit__(self, *unused): self.stream.close()
                def fileno(self): return self.stream.fileno()
                def read(self, count):
                    with path.open("ab") as target: target.write(b"x" * 128)
                    sizes.append(count)
                    return self.stream.read(count)
            with patch.object(s.os, "fdopen", side_effect=lambda fd, mode: GrowingFile(original(fd, mode))):
                with self.assertRaisesRegex(ValueError, "changed during bounded read"):
                    s.read_diagnostic(root, "fixture.log", 32)
            self.assertEqual(sizes, [33])

    def test_diagnostic_parent_symlink_and_hardlink_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            original = root / "original"
            original.mkdir()
            (original / "fixture.log").write_bytes(b"private")
            (root / "linked").symlink_to(original, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "linked diagnostic path"):
                s.read_diagnostic(root, "linked/fixture.log", 32)
            os.link(original / "fixture.log", root / "fixture.log")
            with self.assertRaisesRegex(ValueError, "unsafe diagnostic input"):
                s.read_diagnostic(root, "fixture.log", 32)


    def test_diagnostics_survive_missing_public_export_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "private"
            root.mkdir()
            (root / "fixture.log").write_bytes(b"actual private fixture failure")
            output = base / "export-not-created"
            key = os.urandom(32)
            env = {s.KEY_ENV: base64.b64encode(key).decode(), "GITHUB_REPOSITORY": "lzhs1995/ccc",
                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "head"}
            with patch.object(s, "runner_root", return_value=base), patch.dict(os.environ, env):
                s.seal_diagnostics(root, output)
            meta = json.loads((output / "PRIVATE-DIAGNOSTICS.json").read_bytes())
            raw = s.unseal((output / "private-diagnostics.aesgcm").read_bytes(), key, base64.b64decode(meta["aad_base64"]))
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                self.assertEqual(archive.read("fixture.log"), b"actual private fixture failure")
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)

    def test_diagnostic_output_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base / "private"
            root.mkdir()
            actual = base / "another-output"
            actual.mkdir()
            output = base / "alias"
            output.symlink_to(actual, target_is_directory=True)
            env = {s.KEY_ENV: base64.b64encode(os.urandom(32)).decode()}
            with patch.object(s, "runner_root", return_value=base), patch.dict(os.environ, env):
                with self.assertRaisesRegex(ValueError, "foreign evidence export"):
                    s.seal_diagnostics(root, output)
            self.assertEqual(list(actual.iterdir()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

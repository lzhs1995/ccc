"""Offline negative controls; no app download, mount, launch, or installation."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import admit_nightly as a


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.asset = {"id": a.ASSET_ID, "name": a.ASSET_NAME, "state": "uploaded",
                      "size": a.ASSET_BYTES, "digest": "sha256:" + a.ASSET_SHA,
                      "browser_download_url": a.ASSET_URL}
        self.run = {"id": a.BUILD_RUN, "run_attempt": 1, "head_sha": a.SOURCE_HEAD,
                    "head_branch": "main", "path": ".github/workflows/nightly.yml",
                    "event": "push", "status": "completed", "conclusion": "success",
                    "repository": {"full_name": a.REPOSITORY}}
        self.info = {"CFBundleIdentifier": a.BUNDLE, "CFBundleVersion": a.BUILD_VERSION,
                     "CMUXCommit": a.SOURCE_HEAD[:7], "CFBundleExecutable": "cmux"}

    def test_exact_metadata(self):
        a.validate_metadata(self.asset, self.run)
        a.validate_plist(self.info)

    def test_wrong_asset_not_replaced_by_mutable_latest(self):
        for key, value in [("id", 1), ("name", "cmux-nightly-macos-arm64.dmg"),
                           ("digest", "sha256:" + "0" * 64), ("size", a.ASSET_BYTES + 1),
                           ("state", "new"), ("browser_download_url", "https://example.org/x")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                a.validate_metadata(dict(self.asset, **{key: value}), self.run)

    def test_wrong_build_cannot_pass_success_only(self):
        for key, value in [("head_sha", "0" * 40), ("run_attempt", 2), ("id", 1),
                           ("head_branch", "nightly-next"), ("event", "pull_request"),
                           ("path", "other.yml"), ("status", "in_progress"),
                           ("conclusion", "failure"), ("repository", {"full_name": "other/repo"})]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                a.validate_metadata(self.asset, dict(self.run, **{key: value}))

    def test_signed_app_identity_fields_required(self):
        for key in self.info:
            data = dict(self.info)
            data[key] = "different"
            with self.subTest(key=key), self.assertRaises(ValueError):
                a.validate_plist(data)

    def test_local_launch_or_download_refused_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            output = Path(directory) / "never-created"
            with self.assertRaises(ValueError):
                a.admit(output)
            self.assertFalse(output.exists())

    def test_bundle_framework_symlink_recorded_and_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            app = root / "app"
            app.mkdir()
            (app / "binary").write_bytes(b"fixture executable")
            (app / "current").symlink_to("binary")
            inventory = a.inventory(app)
            self.assertEqual(inventory["current"], {"symlink": "binary"})
            self.assertEqual(inventory["binary"]["sha256"], a.digest(b"fixture executable"))
            (root / "external").write_bytes(b"external")
            (app / "escape").symlink_to("../external")
            with self.assertRaises(ValueError):
                a.inventory(app)

    def test_foreign_repository_or_missing_run_refused_before_output(self):
        correct = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "lzhs1995/ccc",
                   "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
        for key, value in [("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_RUN_ID", ""),
                           ("GITHUB_RUN_ATTEMPT", ""), ("GITHUB_RUN_ATTEMPT", "0")]:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "never-created"
                with mock.patch.dict(os.environ, dict(correct, **{key: value}), clear=True), mock.patch.object(a.sys, "platform", "darwin"):
                    with self.assertRaises(ValueError):
                        a.admit(output)
                self.assertFalse(output.exists())

    def test_hardlinks_cannot_claim_independent_app_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "one").write_bytes(b"x")
            os.link(root / "one", root / "two")
            with self.assertRaises(ValueError):
                a.inventory(root)

    def test_receipt_is_exclusive_and_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "proof.json"
            sha = a.save(receipt, {"passed": False})
            self.assertEqual(a.file_sha(receipt), sha)
            with self.assertRaises(FileExistsError):
                a.save(receipt, {"passed": True})
            self.assertEqual(json.loads(receipt.read_bytes()), {"passed": False})

    def test_inventory_canonical_matches_original_factory_unicode_encoding(self):
        self.assertEqual(a.canonical({"界面": "é"}), b'{"\\u754c\\u9762":"\\u00e9"}')


if __name__ == "__main__":
    unittest.main(verbosity=2)

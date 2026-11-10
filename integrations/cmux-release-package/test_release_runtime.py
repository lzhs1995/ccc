"""Offline rejection tests for the new Release runtime/evidence boundary.

Only temporary fixtures are written. No real app, socket, workflow, account,
process signal or existing saved-session file is accessed.
"""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

import ci_release
import evidence_release as evidence
import release_smoke as smoke
import source_contract as shared


def snapshot(workspace, surface):
    return {"windows": [{"tabManager": {"workspaces": [{"workspaceId": workspace,
        "stableId": "original-workspace", "layout": {"kind": "leaf", "surfaceIds": [surface]},
        "panels": [{"id": surface, "stableSurfaceId": "original-surface", "directory": "/tmp/synthetic-cwd",
                    "type": "terminal"}]}]}}]}


class RuntimeEvidenceBoundaries(unittest.TestCase):
    def test_actual_arm64_runner_required_before_any_native_gui_check(self):
        from types import SimpleNamespace
        with patch.object(smoke.os, "uname", return_value=SimpleNamespace(machine="arm64")):
            self.assertEqual(smoke.require_arm64_runner(), "arm64")
        for machine in ("x86_64", "unknown", "aarch64"):
            with self.subTest(machine=machine), patch.object(smoke.os, "uname", return_value=SimpleNamespace(machine=machine)), \
                 self.assertRaises(ValueError):
                smoke.require_arm64_runner()

    def test_snapshot_raw_fence_and_projection_are_from_one_read(self):
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        data = json.dumps(snapshot(workspace, surface)).encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "snapshot.json"; path.write_bytes(data)
            raw, projection, fence = smoke.snapshot_capture(path, workspace, [surface])
            path.write_text("replaced only after the original read")
            self.assertEqual(raw, data)
            self.assertEqual(fence["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(projection, smoke.workspace_projection(json.loads(raw), workspace, [surface]))

    def test_saved_surface_substitution_and_duplicate_workspace_are_refused(self):
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        document = snapshot(workspace, surface)
        with self.assertRaises(ValueError):
            smoke.workspace_projection(document, workspace, [str(uuid.uuid4())])
        document["windows"][0]["tabManager"]["workspaces"] *= 2
        with self.assertRaises(ValueError):
            smoke.workspace_projection(document, workspace, [surface])

    def test_snapshot_changes_during_read_are_refused(self):
        from types import SimpleNamespace
        import stat
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        data = json.dumps(snapshot(workspace, surface)).encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "snapshot.json"; path.write_bytes(data)
            before = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_size=len(data),
                                     st_dev=1, st_ino=2, st_mtime_ns=5, st_ctime_ns=5, st_uid=os.geteuid())
            after = SimpleNamespace(st_mode=before.st_mode, st_nlink=1, st_size=len(data),
                                    st_dev=1, st_ino=2, st_mtime_ns=6, st_ctime_ns=5)
            with patch.object(smoke.os, "fstat", side_effect=[before, after]), self.assertRaises(ValueError):
                smoke.snapshot_capture(path, workspace, [surface])

    def test_old_valid_snapshot_is_not_a_new_generation_save(self):
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); path = root / "snapshot.json"
            path.write_text(json.dumps(snapshot(workspace, surface)))
            _, _, fence = smoke.snapshot_capture(path, workspace, [surface])
            with self.assertRaises(ValueError):
                smoke.fresh_saved_snapshot(path, workspace, [surface], fence, 0)

    def test_fresh_save_requires_new_writer_time_and_same_read_projection(self):
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        raw = json.dumps(snapshot(workspace, surface)).encode()
        projection = smoke.workspace_projection(json.loads(raw), workspace, [surface])
        old = {"device": 1, "inode": 2, "mtime_ns": 5, "ctime_ns": 5}
        for change in ({"inode": 3}, {"mtime_ns": 6}, {"ctime_ns": 6}):
            observed = old | change
            with self.subTest(change=change), patch.object(smoke, "snapshot_capture", return_value=(raw, projection, observed)):
                with self.assertRaises(ValueError):
                    smoke.fresh_saved_snapshot(Path("/synthetic/snapshot"), workspace, [surface], old, 7)
        new = old | {"inode": 3, "ctime_ns": 10}
        with patch.object(smoke, "snapshot_capture", return_value=(raw, projection, new)) as read:
            result = smoke.fresh_saved_snapshot(Path("/synthetic/snapshot"), workspace, [surface], old, 9)
        self.assertEqual(result, (raw, projection, new)); self.assertEqual(read.call_count, 1)

    def test_snapshot_symlink_is_refused(self):
        workspace, surface = str(uuid.uuid4()), str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "real.json"; target.write_text(json.dumps(snapshot(workspace, surface)))
            path = root / "snapshot.json"; path.symlink_to(target)
            with self.assertRaises(OSError):
                smoke.snapshot_capture(path, workspace, [surface])

    def test_launch_timeout_partial_is_kept_by_outer_failure_record(self):
        record = smoke.error_record(subprocess.TimeoutExpired(["/synthetic/open"], 3, output=b"launched?", stderr=b"partial"))
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertEqual(base64.b64decode(record["stderr_partial_base64"]), b"partial")
        self.assertFalse(record["automatic_retry"])

    def test_unknown_cli_timeout_retains_exact_partial_and_never_reissues(self):
        with tempfile.TemporaryDirectory() as directory:
            driver = smoke.Smoke.__new__(smoke.Smoke)
            driver.root = Path(directory).resolve(); driver.pin = {}; driver.identity = {"pid": 1}
            driver.cli = Path("/synthetic/new/cmux"); driver.old_cli = Path("/synthetic/old/cmux")
            driver.socket_path = "/synthetic/account/.local/state/cmux/cmux.sock"
            driver.environment = {}; driver.sequence = 0; driver.deadline = 100
            failure = subprocess.TimeoutExpired([str(driver.cli)], 10, output=b"partial\x00\xff", stderr=b"waiting")
            with patch.object(smoke.paired, "peer", return_value={"pid": 1}), \
                 patch.object(smoke.time, "monotonic", return_value=1), \
                 patch.object(smoke.subprocess, "run", side_effect=failure) as run:
                with self.assertRaises(subprocess.TimeoutExpired):
                    driver.call(["workspace", "create"])
            self.assertEqual(run.call_count, 1)
            receipt = json.loads((driver.root / "CLI-001.json").read_bytes())
            self.assertEqual(receipt["status"], "UNKNOWN")
            self.assertIsNone(receipt["returncode"])
            self.assertEqual(base64.b64decode(receipt["stdout_partial_base64"]), b"partial\x00\xff")
            self.assertTrue((driver.root / "CLI-001-INTENT.json").is_file())

    def test_expired_acceptance_budget_stops_before_a_new_command(self):
        driver = smoke.Smoke.__new__(smoke.Smoke); driver.deadline = 180
        with patch.object(smoke.time, "monotonic", return_value=180.01), self.assertRaises(ValueError):
            driver.remaining()

    def test_aqua_environment_drops_credentials_and_cmux_routing(self):
        original = {"HOME": "/synthetic/account", "PATH": "/unsafe/bin", "GH_TOKEN": "synthetic-test-token",
                    "CMUX_SOCKET_PATH": "/different.sock", "CMUX_TAG": "foreign-tag", "SHELL": "/foreign/shell"}
        for key in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
                    "GITHUB_SHA", "GITHUB_WORKSPACE", "RUNNER_ENVIRONMENT", "RUNNER_TEMP"):
            original[key] = "fixture"
        with patch.dict(os.environ, original, clear=True):
            result = ci_release.aqua_environment()
        self.assertNotIn("GH_TOKEN", result)
        self.assertNotIn("CMUX_SOCKET_PATH", result)
        self.assertNotIn("CMUX_TAG", result)
        self.assertEqual(result["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")

    def test_export_inventory_omits_app_zip_and_build_products(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("package/cmux-release.zip", "old-cli/original-artifact.zip", "build/derived/secret.app/code",
                         "build/commands/001.stdout", "package/PACKAGE-PIN.json", "runtime/RESULT.json"):
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text("fixture")
            names = {p.relative_to(root).as_posix() for p in evidence.evidence_candidates(root)}
            self.assertEqual(names, {"build/commands/001.stdout", "package/PACKAGE-PIN.json", "runtime/RESULT.json"})

    def test_symlink_evidence_member_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); (root / "target").write_text("fixture")
            (root / "RESULT.json").symlink_to(root / "target")
            with self.assertRaises(ValueError):
                evidence.evidence_candidates(root)

    def test_large_command_stream_export_is_lossless_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "attempt"; root.mkdir()
            output = Path(directory).resolve() / "evidence"; output.mkdir()
            path = root / "command.stdout"; raw = b"exact log bytes\x00\xff\n" * 1000; path.write_bytes(raw)
            row = evidence.export_member(path, root, output)
            self.assertEqual(row["encoding"], "gzip")
            self.assertEqual(row["original_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertGreater(evidence.verify_exported(output, [row]), 0)
            (output / row["exported"]).write_bytes(b"changed")
            with self.assertRaises(ValueError):
                evidence.verify_exported(output, [row])

    def test_small_export_ceiling_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "attempt"; root.mkdir()
            output = Path(directory).resolve() / "evidence"; output.mkdir()
            path = root / "receipt.json"; path.write_bytes(b"x" * 200)
            row = evidence.export_member(path, root, output)
            with patch.object(evidence, "MAX_EVIDENCE_BYTES", 100), self.assertRaises(ValueError):
                evidence.verify_exported(output, [row])

    def test_wrong_artifact_url_fails_without_any_network_call(self):
        context = {"repository": "lzhs1995/ccc", "head_sha": "a" * 40, "run_id": 10, "run_attempt": 1}
        with patch.object(shared, "run_identity", return_value=context), \
             patch.object(evidence.subprocess, "run") as run, self.assertRaises(ValueError):
            evidence.bind_app_artifact(Path("/synthetic/attempt"), "20", "b" * 64,
                                      "https://github.com/lzhs1995/ccc/actions/runs/11/artifacts/20")
        run.assert_not_called()

    def test_incomplete_artifact_upload_outputs_are_unknown(self):
        with patch.object(shared, "run_identity", return_value={}), self.assertRaises(ValueError):
            evidence.bind_app_artifact(Path("/synthetic/attempt"), "", "b" * 64, "")

    def test_publication_workflow_splits_full_app_and_small_evidence(self):
        workflow = (Path(__file__).parent / "cmux-release-package.yml.draft").read_text()
        self.assertIn("runs-on: macos-26", workflow)
        self.assertIn("name: cmux-release-app-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn("name: cmux-release-evidence-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn("steps.app.outputs.artifact-id", workflow)
        self.assertNotIn("reload.sh", workflow)
        self.assertNotIn("cancel-in-progress: true", workflow)


if __name__ == "__main__":
    unittest.main()

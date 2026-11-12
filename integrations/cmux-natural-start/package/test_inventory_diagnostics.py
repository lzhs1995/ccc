"""Synthetic inventory diagnostics; no App, process, socket or network operation."""
import copy
from contextlib import contextmanager
import json
from pathlib import PurePosixPath
import plistlib
from types import SimpleNamespace
import unittest
from unittest import mock

import release_package as package
import release_paired as paired
import release_smoke as smoke
import test_release_package as fixtures


class InventoryDiagnostics(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReleasePackageBoundaries()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.app, _, self.pin, self.rows, _ = self.fixture.receipt()
        self.root = self.fixture.root
        self.pin["required_install_path"] = str(self.app)

    @contextmanager
    def boundary(self):
        signed = "CDHash=" + self.pin["code_directory_hash"] + "\nIdentifier=" + package.BUNDLE_ID + "\nSignature=adhoc\nTeamIdentifier=not set\n"
        def signature(argv, **kwargs):
            return {"stdout": plistlib.dumps(package.MAIN_ENTITLEMENTS).decode() if "--entitlements" in argv else "",
                    "stderr": signed, "returncode": 0, "argv": argv}
        inventory = mock.Mock(wraps=package.original_admission().inventory)
        with mock.patch.object(package, "INSTALL_PATH", str(self.app)), \
             mock.patch.object(package, "command", side_effect=signature) as signatures, \
             mock.patch.object(package, "original_admission", return_value=SimpleNamespace(inventory=inventory)), \
             mock.patch.object(paired.pwd, "getpwuid") as account, \
             mock.patch.object(paired, "installed_gui_processes", return_value=[]), \
             mock.patch.object(paired, "require_quiescent_socket", return_value={"status": "absent"}), \
             mock.patch.object(paired.subprocess, "run") as opened:
            account.return_value.pw_dir = str(self.root)
            opened.return_value.returncode = 0
            opened.return_value.stdout = opened.return_value.stderr = ""
            yield opened, inventory, signatures

    def rejected(self, reason, expected_rows=None):
        with self.boundary() as (opened, inventory, signatures):
            with self.assertRaisesRegex(ValueError, reason) as caught:
                paired.launch(self.app, self.pin, {"HOME": str(self.root)}, timeout=3,
                              expected_rows=self.rows if expected_rows is None else expected_rows)
            opened.assert_not_called()
            signatures.assert_not_called()
            # The failure observation is the exact rows that reached verification.
            self.assertEqual(inventory.call_count, 1)
            self.assertEqual(repr(caught.exception), "ValueError(" + repr(reason) + ")")
            record = smoke.error_record(caught.exception)
            self.assertEqual(record["status"], "UNKNOWN")
            self.assertFalse(record["automatic_retry"])
            return caught.exception, record["bundle_inventory_observation"]

    def test_normal_bundle_preserves_full_verification_and_old_launch_call(self):
        with self.boundary() as (opened, inventory, signatures):
            result = paired.launch(self.app, self.pin, {"HOME": str(self.root)}, timeout=3,
                                   expected_rows=self.rows)
            self.assertTrue(result["immediate_full_app_verification"]["passed"])
            self.assertEqual(result["immediate_full_app_verification"]["complete_members"], len(self.rows))
            self.assertEqual(inventory.call_count, 3)
            self.assertEqual(signatures.call_count, 3)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args.args[0], ["/usr/bin/open", "-a", str(self.app)])
            self.assertEqual(result["bundle_icon_persistence_policy"], "disabled_by_signed_bundle_metadata")
            self.assertNotIn("CMUX_SOCKET_PATH", opened.call_args.kwargs["env"])
            # The optional diagnostics argument does not invalidate old callers.
            self.assertEqual(paired.launch(self.app, self.pin, {"HOME": str(self.root)}, timeout=3)["returncode"], 0)
            self.assertEqual(opened.call_args.args[0], ["/usr/bin/open", "-a", str(self.app)])

    def test_launch_failure_does_not_retry_with_argument_fallback(self):
        with self.boundary() as (opened, inventory, signatures):
            opened.return_value.returncode = 1
            opened.return_value.stderr = "synthetic LaunchServices refusal"
            with self.assertRaisesRegex(ValueError, "LaunchServices refused"):
                paired.launch(self.app, self.pin, {"HOME": str(self.root)}, timeout=3,
                              expected_rows=self.rows)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args.args[0],
                             ["/usr/bin/open", "-a", str(self.app)])
            self.assertEqual(inventory.call_count, 3)
            self.assertEqual(signatures.call_count, 3)

    def test_added_member_is_rejected_with_exact_bounded_observation(self):
        (self.app / "Contents/Resources/new-resource").write_bytes(b"added")
        _, observation = self.rejected("member count differs")
        self.assertEqual(observation["expected_count"], len(self.rows))
        self.assertEqual(observation["actual_count"], len(self.rows) + 1)
        self.assertEqual(observation["difference_counts"], {"added": 1, "removed": 0, "changed": 0})
        self.assertEqual(observation["details"][0]["path"], "Contents/Resources/new-resource")
        self.assertEqual(observation["details"][0]["actual"]["sha256"], fixtures.sha(b"added"))
        self.assertIsNone(observation["details"][0]["expected"])
        self.assertEqual(observation["observation"], "original_preverify_inventory_no_rescan")
        self.assertFalse(observation["actor_established"])

    def test_removed_member_is_rejected_with_original_manifest_source(self):
        (self.app / "Contents/Resources/bin/cmux").unlink()
        _, observation = self.rejected("member count differs")
        self.assertEqual(observation["difference_counts"], {"added": 0, "removed": 1, "changed": 0})
        self.assertEqual(observation["actual_count"], len(self.rows) - 1)
        self.assertEqual(observation["details"][0]["path"], "Contents/Resources/bin/cmux")
        self.assertIsNone(observation["details"][0]["actual"])
        self.assertEqual(observation["expected_inventory_sha256"], self.pin["inventory_canonical_sha256"])

    def test_same_count_changed_bytes_remain_rejected(self):
        path = self.app / "Contents/Resources/bin/cmux"
        before = path.read_bytes()
        path.write_bytes(before[:-1] + bytes([before[-1] ^ 1]))
        _, observation = self.rejected("canonical inventory differs")
        self.assertEqual(observation["actual_count"], observation["expected_count"])
        self.assertEqual(observation["difference_counts"], {"added": 0, "removed": 0, "changed": 1})
        detail = observation["details"][0]
        self.assertEqual(detail["path"], "Contents/Resources/bin/cmux")
        self.assertEqual(detail["expected"]["size"], detail["actual"]["size"])
        self.assertNotEqual(detail["expected"]["sha256"], detail["actual"]["sha256"])

    def test_same_count_path_replacement_is_not_accepted(self):
        path = self.app / "Contents/Resources/bin/cmux"
        path.rename(path.with_name("renamed"))
        _, observation = self.rejected("canonical inventory differs")
        self.assertEqual(observation["actual_count"], observation["expected_count"])
        self.assertEqual(observation["difference_counts"], {"added": 1, "removed": 1, "changed": 0})
        self.assertEqual({d["path"] for d in observation["details"]},
                         {"Contents/Resources/bin/cmux", "Contents/Resources/bin/renamed"})

    def test_many_and_oversized_details_are_bounded_without_losing_totals(self):
        actual = copy.deepcopy(self.rows)
        names = ["Contents/Resources/new-" + str(i).zfill(3) for i in range(80)]
        names.append("Contents/Resources/" + "x" * (paired.INVENTORY_DETAIL_BYTES + 1))
        for name in names:
            actual.append({"path": name, "type": "file", "mode": 0o600, "size": 1, "sha256": "a" * 64})
        actual.sort(key=lambda row: PurePosixPath(row["path"]))
        observation = paired.inventory_observation(self.pin, self.rows, actual)
        self.assertEqual(observation["difference_counts"], {"added": 81, "removed": 0, "changed": 0})
        self.assertLessEqual(len(observation["details"]), paired.INVENTORY_DETAIL_LIMIT)
        self.assertLessEqual(sum(len(package.canonical(d)) for d in observation["details"]),
                             paired.INVENTORY_DETAIL_BYTES)
        self.assertEqual(observation["omitted_details"], 81 - len(observation["details"]))
        self.assertLess(len(package.canonical(observation)), 32 * 1024)
        self.assertNotEqual(observation["expected_inventory_sha256"], observation["actual_inventory_sha256"])

    def test_unpinned_expected_rows_only_make_diagnostics_unavailable(self):
        (self.app / "Contents/Resources/new-resource").write_bytes(b"added")
        unpinned = package.original_admission().inventory(self.app)
        _, observation = self.rejected("member count differs", expected_rows=unpinned)
        self.assertEqual(observation["status"], "UNAVAILABLE")
        self.assertEqual(observation["error"], "ValueError('member count differs')")

    def test_gen2_diagnostic_write_failure_keeps_original_error_and_rows(self):
        (self.app / "Contents/Resources/new-resource").write_bytes(b"added")
        primary, observation = self.rejected("member count differs")
        runtime = self.root / "runtime"
        runtime.mkdir()
        agent = smoke.Smoke(runtime, self.pin, self.app, self.root / "old-cli",
                            expected_rows=self.rows)
        agent.generation = 1
        original_save = smoke.save
        def save_with_failure(path, record):
            if path.name == "GEN-2-OPEN-UNKNOWN.json":
                raise OSError("synthetic diagnostic disk failure")
            return original_save(path, record)
        with mock.patch.object(paired, "launch", side_effect=primary) as launched, \
             mock.patch.object(smoke, "save", side_effect=save_with_failure):
            with self.assertRaises(ValueError) as caught:
                agent.start()
        self.assertIs(caught.exception, primary)
        self.assertIs(launched.call_args.kwargs["expected_rows"], self.rows)
        self.assertEqual(agent.generation, 2)
        self.assertIsNone(agent.identity)
        smoke.save_failure(runtime / "FAILURE.json", primary, {"generation": agent.generation, "peer": None})
        final = json.loads((runtime / "FAILURE.json").read_text())
        self.assertEqual(final["error"], "ValueError('member count differs')")
        self.assertEqual(final["bundle_inventory_observation"], observation)
        self.assertEqual(final["diagnostic_write_errors"][0]["file"], "GEN-2-OPEN-UNKNOWN.json")
        self.assertEqual(final["status"], "UNKNOWN")

    def test_runtime_failure_and_cleanup_record_errors_do_not_mask_primary(self):
        primary = ValueError("member count differs")
        root = self.root / "runtime-failure"
        old_cli = self.fixture.write("old-cli", b"synthetic-old-cli")
        runtime = SimpleNamespace(generation=2, identity=None, launch_fence=None,
                                  start=mock.Mock(side_effect=primary), stop=mock.Mock(return_value=None))
        original_save = smoke.save
        def failing_error_records(path, value):
            if path.name in ("FAILURE.json", "CLEANUP.json", "CLEANUP-UNKNOWN.json"):
                raise OSError("synthetic record failure: " + path.name)
            return original_save(path, value)
        with mock.patch.object(smoke, "runtime_preflight", return_value={"home": str(self.root)}), \
             mock.patch.object(package, "INSTALL_PATH", str(self.app)), \
             mock.patch.object(package, "command", return_value={}), \
             mock.patch.object(paired, "read_pin", return_value=(self.pin, self.rows)), \
             mock.patch.object(paired, "verify_app", return_value={"passed": True}), \
             mock.patch.object(paired, "installed_gui_processes", return_value=[]), \
             mock.patch.object(smoke.shared, "original_pin", return_value={"cli_sha256": package.digest(old_cli)}), \
             mock.patch.object(smoke, "Smoke", return_value=runtime) as constructed, \
             mock.patch.object(smoke, "save", side_effect=failing_error_records):
            with self.assertRaises(ValueError) as caught:
                smoke.runtime(root, self.root, old_cli)
        self.assertIs(caught.exception, primary)
        self.assertIs(constructed.call_args.kwargs["expected_rows"], self.rows)
        runtime.stop.assert_called_once_with(cold=False)
        self.assertEqual(primary.diagnostic_write_errors[0]["file"], "FAILURE.json")
        self.assertFalse((root / "RESULT.json").exists())


if __name__ == "__main__":
    unittest.main()


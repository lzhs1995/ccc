"""Synthetic file-only coverage for new Release package and paired boundaries.

No xcodebuild, codesign, open, download, process inventory or socket operation.
The fake native headers/signature receipts are explicitly not signed App proof.
"""
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import struct
import tempfile
import unittest
from unittest import mock

import release_package as package
import release_paired as paired


def sha(data):
    return hashlib.sha256(data).hexdigest()


def native(cpu=0x0100000c):
    return struct.pack("<8I", 0xfeedfacf, cpu, 0, 2, 0, 0, 0, 0) + b"SYNTHETIC NOT EXECUTABLE"


class ReleasePackageBoundaries(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="cmux-release-files-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        for owner, name in ((package.subprocess, "run"), (package, "command"),
                            (paired, "_library"), (paired.socket, "socket")):
            patcher = mock.patch.object(owner, name, side_effect=AssertionError("forbidden real operation: " + name))
            patcher.start(); self.addCleanup(patcher.stop)

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def snapshot(self, terminal=None, *, workspace_extra=None, dock=None):
        workspace = {"workspaceId": "workspace", "stableId": "stable",
                     "panels": [{"id": "surface", "type": "terminal", "terminal": terminal or {"workingDirectory": str(self.root)}}]}
        workspace.update(workspace_extra or {})
        window = {"windowId": "window", "tabManager": {"workspaces": [workspace]}}
        if dock is not None: window["dock"] = dock
        return {"version": 1, "windows": [window]}

    def install_snapshot(self, snapshot):
        return self.write("Library/Application Support/cmux/session-com.cmuxterm.app.json",
                          json.dumps(snapshot).encode())

    def bundle(self):
        app = self.root / "fixture.app"
        self.write("fixture.app/Contents/MacOS/cmux", native())
        self.write("fixture.app/Contents/Resources/bin/cmux", native())
        self.write("fixture.app/Contents/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": package.BUNDLE_ID, "CFBundleExecutable": "cmux",
            "LSEnvironment": package.ENVIRONMENT, "CMUXDisableBundleIconPersistence": True,
            "CMUXLocalRepair": {"sourceTree": package.shared.current_source_tree()}}))
        return app

    def native_admission(self):
        # Synthetic summaries exercise binding only; they are not native test proof.
        source = package.shared.current_source_contract()
        return {"schema": "cmux-natural-start-native-admission-v1", "passed": True,
            "run": {"repository": "lzhs1995/ccc", "head_sha": "c" * 40, "run_id": 1, "run_attempt": 1},
            "source": {key: source[key] for key in
                       ("upstream_commit", "base_green_tree", "red_tree", "green_tree", "patches")} | {
                "base_commit": "1" * 40, "base_parent_commit": source["upstream_commit"],
                "red_commit": "2" * 40, "red_parent_commit": "1" * 40,
                "green_commit": "3" * 40, "green_parent_commit": "2" * 40},
            "contract_sha256": source["native_contract_sha256"],
            "verifier_sha256": source["native_verifier_sha256"],
            "native_success_sha256": "4" * 64, "manifest_sha256": "5" * 64}

    def receipt(self):
        app = self.bundle()
        rows = package.original_admission().inventory(app)
        native_rows = package.native_code_inventory(app)
        closure = {"all_native_code_enumerated": True, "main_entitlements": package.MAIN_ENTITLEMENTS,
                   "upstream_team_features_verified": False, "nested_components_removed": 0, "targets": []}
        for target, executable in package.signing_targets(app):
            row = next(row for row in native_rows if row["path"] == executable.relative_to(app).as_posix())
            closure["targets"].append({"target": target.relative_to(app).as_posix(),
                "executable": row["path"], "after_sha256": row["sha256"], "architectures": row["architectures"],
                "cdhash": "b" * 40, "resigned_locally": True})
        transform = {"source_preserved": True, "source_build_result_sha256": "a" * 64}
        manifest = self.write("PACKAGE-MANIFEST.json", json.dumps(rows).encode())
        close = self.write("SIGNATURE-CLOSURE.json", json.dumps(closure).encode())
        transformed = self.write("TRANSFORM.json", json.dumps(transform).encode())
        native_admission = self.native_admission()
        native_path = self.write("NATIVE-ADMISSION.json", json.dumps(native_admission).encode())
        pin = {"schema": package.SCHEMA, "passed": True, "source_tree": package.shared.current_source_tree(),
               "source_base_tree": package.shared.GREEN_TREE,
               "native_evidence": native_admission, "native_admission_sha256": package.digest(native_path),
               "source_upstream": package.shared.UPSTREAM,
               "original_source_app_pin_sha256": package.shared.SOURCE_PIN_SHA256,
               "bundle_id": package.BUNDLE_ID, "bundle_executable": "cmux", "app_name": package.APP_NAME,
               "configuration": "Release", "signature": "ad-hoc", "official_release": False,
               "architecture": ["arm64"], "required_install_path": package.INSTALL_PATH,
               "socket_rule": package.SOCKET_RULE, "main_entitlements": package.MAIN_ENTITLEMENTS,
               "signed_environment": package.ENVIRONMENT,
               "signed_bundle_options": {"CMUXDisableBundleIconPersistence": True},
               "status": "SIGNED_ARCHIVE_RUNTIME_NOT_YET_ACCEPTED",
               "runtime_verified": False, "business_adoption_ready": False, "installed_on_user_machine": False,
               "upstream_team_features_verified": False, "code_directory_hash": "b" * 40,
               "run": {"repository": "lzhs1995/ccc", "head_sha": "c" * 40, "run_id": 1, "run_attempt": 1},
               "archive": {"name": "cmux-release.zip", "sha256": "d" * 64, "bytes": 1},
               "manifest_sha256": package.digest(manifest), "inventory_canonical_sha256": sha(package.canonical(rows)),
               "signature_closure_sha256": package.digest(close), "transform_sha256": package.digest(transformed),
               "member_count": len(rows), "native_code": native_rows,
               "signing_targets": [{key: row[key] for key in ("target", "executable")} for row in closure["targets"]],
               "native_code_canonical_sha256": sha(package.canonical(native_rows)),
               "application_sha256": package.digest(app / "Contents/MacOS/cmux"),
               "cli_sha256": package.digest(app / "Contents/Resources/bin/cmux"),
               "info_plist_sha256": package.digest(app / "Contents/Info.plist"),
               "build_result_sha256": "a" * 64, "resolved_settings_sha256": "a" * 64,
               "unsigned_manifest_sha256": "a" * 64}
        path = self.write("PACKAGE-PIN.json", json.dumps(pin).encode())
        return app, path, pin, rows, manifest

    def test_current_source_is_written_into_signed_metadata(self):
        before = {"CFBundleIdentifier": package.BUNDLE_ID, "CFBundleExecutable": "cmux",
                  "CMUXIrohRelayPolicyKeyID": "cmux-production-relay-policy-2026-07",
                  "CFBundleURLTypes": [{"CFBundleURLName": package.BUNDLE_ID + ".auth",
                                        "CFBundleURLSchemes": ["cmux"]}]}
        result, _ = package.transform_plist(plistlib.dumps(before))
        tree = plistlib.loads(result)["CMUXLocalRepair"]["sourceTree"]
        self.assertEqual(tree, package.shared.current_source_tree())
        self.assertNotEqual(tree, package.shared.GREEN_TREE)

    def test_native_admission_requires_original_bytes_and_exact_summary(self):
        summary = self.native_admission()
        raw = json.dumps(summary, indent=2).encode()
        path = self.write("admission.json", raw)
        actual, retained = package.read_native_admission(path, sha(raw), copy.deepcopy(summary),
                                                       expected_run=summary["run"])
        self.assertEqual(actual, summary)
        self.assertEqual(retained, raw)
        for digest in (None, "", "0" * 64):
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                package.read_native_admission(path, digest, summary, expected_run=summary["run"])
        for changed in (None, summary | {"passed": 1}, summary | {"manifest_sha256": "6" * 64}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "receipt and summary"):
                package.read_native_admission(path, sha(raw), changed, expected_run=summary["run"])
        path.write_bytes(raw + b"\n")
        with self.assertRaisesRegex(ValueError, "pinned receipt"):
            package.read_native_admission(path, sha(raw), summary, expected_run=summary["run"])

    def test_native_admission_rejects_coherent_wrong_attempt_and_source(self):
        original = self.native_admission()
        wrong_attempt = copy.deepcopy(original)
        wrong_attempt["run"]["run_attempt"] += 1
        wrong_source = copy.deepcopy(original)
        wrong_source["source"]["green_tree"] = package.shared.GREEN_TREE
        for summary in (wrong_attempt, wrong_source, original | {"passed": 1}):
            raw = json.dumps(summary).encode()
            path = self.write("foreign-admission.json", raw)
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                package.read_native_admission(path, sha(raw), summary, expected_run=original["run"])

    def test_package_pin_rejects_old_tree_and_unbound_native_summary(self):
        _, _, pin, _, _ = self.receipt()
        bad_summary = copy.deepcopy(pin["native_evidence"])
        bad_summary["run"]["run_id"] += 1
        for change in ({"source_tree": package.shared.GREEN_TREE}, {"source_base_tree": "0" * 40},
                       {"native_admission_sha256": ""}, {"native_evidence": None},
                       {"native_evidence": bad_summary}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                paired.validate_pin(pin | change)

    def test_read_pin_requires_original_native_admission_file(self):
        _, path, pin, _, manifest = self.receipt()
        self.assertEqual(paired.read_pin(path, package.digest(path), manifest)[0], pin)
        native_path = self.root / "NATIVE-ADMISSION.json"
        native_path.write_bytes(native_path.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "pinned receipt"):
            paired.read_pin(path, package.digest(path), manifest)

    def test_plist_release_transform_preserves_resources_and_derives_prod_route(self):
        before = {"CFBundleIdentifier": package.BUNDLE_ID, "CFBundleExecutable": "cmux",
                  "CMUXIrohRelayPolicyKeyID": "cmux-production-relay-policy-2026-07",
                  "CFBundleURLTypes": [{"CFBundleURLName": package.BUNDLE_ID + ".auth", "CFBundleURLSchemes": ["cmux"]}],
                  "CFBundleVersion": "77", "ResourceSentinel": ["untouched"]}
        raw = plistlib.dumps(before)
        after, delta = package.transform_plist(raw)
        value = plistlib.loads(after)
        self.assertEqual(value["ResourceSentinel"], ["untouched"])
        self.assertEqual(value["CFBundleVersion"], "77")
        self.assertEqual(value["LSEnvironment"], package.ENVIRONMENT)
        self.assertNotIn("CMUX_SOCKET_PATH", value["LSEnvironment"])
        self.assertFalse(value["CMUXLocalRepair"]["officialRelease"])
        self.assertEqual(plistlib.loads(raw), before)
        for mutation in ({"CFBundleIdentifier": "foreign"}, {"Foreign": "/Users/runner/wrong"}):
            with self.assertRaises(ValueError): package.transform_plist(plistlib.dumps(before | mutation))

    def test_manifest_rejects_escape_duplicate_symlink_parent_and_changed_shape(self):
        rows = package.original_admission().inventory(self.bundle())
        self.assertEqual(len(package.validate_manifest(rows)), len(rows))
        for bad in [rows + [copy.deepcopy(rows[0])], [{"path": "../x", "type": "directory", "mode": 0o700}],
                    [{"path": "x/y", "type": "file", "mode": 0o600, "size": 0, "sha256": sha(b"")}],
                    [{"path": "x", "type": "symlink", "mode": 0o777, "target": "../escape"}],
                    [rows[0] | {"unexpected": True}] + rows[1:]]:
            with self.assertRaises(ValueError): package.validate_manifest(bad)

    def test_natural_start_package_embeds_boolean_icon_policy(self):
        before = {"CFBundleIdentifier": package.BUNDLE_ID, "CFBundleExecutable": "cmux",
                  "CMUXIrohRelayPolicyKeyID": "cmux-production-relay-policy-2026-07",
                  "CFBundleURLTypes": [{"CFBundleURLName": package.BUNDLE_ID + ".auth",
                                        "CFBundleURLSchemes": ["cmux"]}],
                  "CMUXDisableBundleIconPersistence": False}
        raw = plistlib.dumps(before)
        after, delta = package.transform_plist(raw)
        self.assertIs(plistlib.loads(after).get("CMUXDisableBundleIconPersistence"), True)
        self.assertIs(delta["CMUXDisableBundleIconPersistence"]["before"], False)
        self.assertIs(delta["CMUXDisableBundleIconPersistence"]["after"], True)
        self.assertEqual(plistlib.loads(raw), before)

    def test_natural_start_pin_rejects_missing_false_and_nonboolean_policy(self):
        _, _, pin, _, _ = self.receipt()
        missing = copy.deepcopy(pin)
        del missing["signed_bundle_options"]
        variants = [missing]
        for value in (None, False, 1, "true", "false"):
            variants.append(pin | {"signed_bundle_options": {"CMUXDisableBundleIconPersistence": value}})
        variants.append(pin | {"signed_bundle_options": {"CMUXDisableBundleIconPersistence": True, "unknown": True}})
        for value in variants:
            with self.subTest(options=value.get("signed_bundle_options")):
                with self.assertRaisesRegex(ValueError, "signed bundle"):
                    paired.validate_pin(value)
        self.assertIs(paired.validate_pin(pin), pin)

    def test_transform_delta_records_integer_to_boolean_policy_change(self):
        before = {"CFBundleIdentifier": package.BUNDLE_ID, "CFBundleExecutable": "cmux",
                  "CMUXIrohRelayPolicyKeyID": "cmux-production-relay-policy-2026-07",
                  "CFBundleURLTypes": [{"CFBundleURLName": package.BUNDLE_ID + ".auth",
                                        "CFBundleURLSchemes": ["cmux"]}],
                  "CMUXDisableBundleIconPersistence": 1}
        raw = plistlib.dumps(before)
        after, delta = package.transform_plist(raw)
        self.assertIs(plistlib.loads(after)["CMUXDisableBundleIconPersistence"], True)
        self.assertIn("CMUXDisableBundleIconPersistence", delta)
        self.assertIs(type(delta["CMUXDisableBundleIconPersistence"]["before"]), int)
        self.assertIs(delta["CMUXDisableBundleIconPersistence"]["after"], True)
        self.assertEqual(plistlib.loads(raw), before)

    def test_transform_delta_records_nested_metadata_type_change(self):
        before = {"CFBundleIdentifier": package.BUNDLE_ID, "CFBundleExecutable": "cmux",
                  "CMUXIrohRelayPolicyKeyID": "cmux-production-relay-policy-2026-07",
                  "CFBundleURLTypes": [{"CFBundleURLName": package.BUNDLE_ID + ".auth",
                                        "CFBundleURLSchemes": ["cmux"]}]}
        initial, _ = package.transform_plist(plistlib.dumps(before))
        before = plistlib.loads(initial)
        before["CMUXLocalRepair"]["officialRelease"] = 0
        raw = plistlib.dumps(before)
        after, delta = package.transform_plist(raw)
        self.assertIs(plistlib.loads(after)["CMUXLocalRepair"]["officialRelease"], False)
        self.assertIn("CMUXLocalRepair", delta)
        self.assertIs(type(delta["CMUXLocalRepair"]["before"]["officialRelease"]), int)
        self.assertIs(delta["CMUXLocalRepair"]["after"]["officialRelease"], False)
        self.assertEqual(plistlib.loads(raw), before)

    def test_natural_start_verifier_rejects_wrong_policy_even_with_matching_hashes(self):
        app, _, original_pin, _, _ = self.receipt()
        original_info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
        signed = ("CDHash=" + original_pin["code_directory_hash"] + "\nIdentifier=" + package.BUNDLE_ID
                  + "\nSignature=adhoc\nTeamIdentifier=not set\n")
        def signature(argv, **kwargs):
            return {"stdout": plistlib.dumps(package.MAIN_ENTITLEMENTS).decode() if "--entitlements" in argv else "",
                    "stderr": signed, "returncode": 0, "argv": argv}
        for value in (None, False, 1, "true", "false"):
            info = copy.deepcopy(original_info)
            if value is None:
                del info["CMUXDisableBundleIconPersistence"]
            else:
                info["CMUXDisableBundleIconPersistence"] = value
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
            rows = package.original_admission().inventory(app)
            pin = original_pin | {"info_plist_sha256": package.digest(app / "Contents/Info.plist"),
                "member_count": len(rows), "inventory_canonical_sha256": sha(package.canonical(rows)),
                "required_install_path": str(app)}
            with self.subTest(info_value=value), mock.patch.object(package, "INSTALL_PATH", str(app)), \
                 mock.patch.object(package, "command", side_effect=signature) as external:
                with self.assertRaisesRegex(ValueError, "signed bundle"):
                    paired.verify_app(app, pin, rows, timeout=3)
                external.assert_not_called()

    def test_regular_receipt_rejects_leaf_parent_symlink_hardlink_write_and_growth_limit(self):
        path = self.write("actual/data", b"known")
        self.assertEqual(package.read_pinned(path, sha(b"known")), b"known")
        link = self.root / "leaf"; link.symlink_to(path)
        ancestor = self.root / "ancestor"; ancestor.symlink_to(path.parent, target_is_directory=True)
        for p in [link, ancestor / "data"]:
            with self.assertRaises(ValueError): package.read_pinned(p)
        with self.assertRaises(ValueError): package.read_pinned(path, maximum=4)
        hard = self.root / "hard"; os.link(path, hard)
        with self.assertRaises(ValueError): package.read_pinned(path)
        hard.unlink(); path.chmod(0o666)
        with self.assertRaises(ValueError): package.read_pinned(path)

    def test_duplicate_and_nonfinite_json_rejected(self):
        for raw in [b'{"key":1,"key":2}', b'{"key":NaN}']:
            with self.assertRaises(ValueError): package.decode_json(raw)

    def test_native_architecture_and_inside_out_framework_aliases(self):
        app = self.bundle()
        base = "fixture.app/Contents/Frameworks/Foo.framework"
        self.write(base + "/Versions/A/Foo", native(0x01000007))
        self.write(base + "/Versions/A/Resources/Info.plist", plistlib.dumps({"CFBundleExecutable": "Foo"}))
        framework = self.root / base
        (framework / "Versions/Current").symlink_to("A")
        (framework / "Resources").symlink_to("Versions/Current/Resources")
        (framework / "Foo").symlink_to("Versions/Current/Foo")
        targets = package.signing_targets(app)
        self.assertEqual(targets[-1][0], app)
        self.assertEqual(len(targets), 3)
        self.assertEqual({e for _, e in targets}, set(package.macho_files(app)))
        self.assertEqual(package.macho_architectures(framework / "Versions/A/Foo"), ["x86_64"])
        bad = self.write("bad", b"\xcf\xfa\xed\xfe")
        with self.assertRaises(ValueError): package.macho_architectures(bad)
        bad.write_bytes(native(123))
        with self.assertRaises(ValueError): package.macho_architectures(bad)

    def test_external_pin_and_full_signature_inventory_closure(self):
        app, path, pin, rows, manifest = self.receipt()
        self.assertEqual(paired.read_pin(path, package.digest(path), manifest), (pin, rows))
        with self.assertRaises(ValueError): paired.read_pin(path, "0" * 64, manifest)
        closure = json.loads((self.root / "SIGNATURE-CLOSURE.json").read_text())
        closure["targets"].pop()
        self.write("SIGNATURE-CLOSURE.json", json.dumps(closure).encode())
        pin["signature_closure_sha256"] = package.digest(self.root / "SIGNATURE-CLOSURE.json")
        self.write("PACKAGE-PIN.json", json.dumps(pin).encode())
        with self.assertRaisesRegex(ValueError, "incomplete signature closure"):
            paired.read_pin(path, package.digest(path), manifest)

    def test_pin_cannot_claim_runtime_official_wrong_arch_or_path(self):
        _, _, pin, rows, _ = self.receipt()
        for mutation in ({"runtime_verified": True}, {"official_release": True},
                         {"architecture": ["x86_64"]}, {"bundle_executable": "../foreign"},
                         {"required_install_path": "/other/cmux.app"}):
            with self.assertRaises(ValueError): paired.validate_pin(pin | mutation)
        wrong = copy.deepcopy(pin); wrong["native_code"][0]["architectures"] = ["x86_64"]
        wrong["native_code_canonical_sha256"] = sha(package.canonical(wrong["native_code"]))
        with self.assertRaises(ValueError): paired.validate_native_manifest(wrong, rows)

    def test_verify_app_mock_signature_still_requires_entitlement_and_actual_inventory(self):
        app, _, pin, rows, _ = self.receipt()
        signed = "CDHash=" + pin["code_directory_hash"] + "\nIdentifier=" + package.BUNDLE_ID + "\nSignature=adhoc\nTeamIdentifier=not set\n"
        def response(argv, **kwargs):
            self.assertGreater(kwargs["timeout"], 0)
            output = plistlib.dumps(package.MAIN_ENTITLEMENTS).decode() if "--entitlements" in argv else ""
            return {"stdout": output, "stderr": signed, "returncode": 0, "argv": argv}
        with mock.patch.object(package, "INSTALL_PATH", str(app)), mock.patch.object(package, "command", side_effect=response):
            pin["required_install_path"] = str(app)
            self.assertTrue(paired.verify_app(app, pin, rows, timeout=3)["passed"])
            def wrong(argv, **kwargs):
                result = response(argv, **kwargs)
                if "--entitlements" in argv: result["stdout"] = plistlib.dumps({"keychain-access-groups": ["FORGED"]}).decode()
                return result
            with mock.patch.object(package, "command", side_effect=wrong):
                with self.assertRaisesRegex(ValueError, "entitlements differ"): paired.verify_app(app, pin, rows, timeout=3)
            (app / "Contents/Resources/bin/cmux").write_bytes(native() + b"changed")
            with self.assertRaisesRegex(ValueError, "complete bundle differs"): paired.verify_app(app, pin, rows, timeout=3)

    def test_shell_snapshot_allowed_without_mutation(self):
        path = self.install_snapshot(self.snapshot())
        before = path.read_bytes()
        result = paired.first_adoption_snapshot_check(self.root)
        self.assertEqual(result["status"], "no_saved_agent_or_command_state")
        self.assertEqual(path.read_bytes(), before)

    def test_snapshot_builtin_only_managed_tmux_remote_and_dock_blocked(self):
        cases = [self.snapshot({field: {"synthetic": True}}) for field in
                 ("agent", "resumeBinding", "managedAgentResumeBinding", "tmuxStartCommand", "claudeBackgroundViewer", "hibernation")]
        cases += [self.snapshot(workspace_extra={"remote": {"host": "synthetic"}}),
                  self.snapshot(dock={"panels": [{"id": "dock", "terminal": {"agent": {"kind": "codex"}}}]})]
        for value in cases:
            path = self.install_snapshot(value); before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "original-session ownership"):
                paired.first_adoption_snapshot_check(self.root)
            self.assertEqual(path.read_bytes(), before)

    def test_snapshot_unknown_shape_and_dangling_symlink_fail_closed(self):
        self.install_snapshot({"version": 99, "windows": []})
        with self.assertRaises(ValueError): paired.first_adoption_snapshot_check(self.root)
        path = self.root / "Library/Application Support/cmux/session-com.cmuxterm.app.json"
        path.unlink(); path.symlink_to(self.root / "absent")
        with self.assertRaises(ValueError): paired.first_adoption_snapshot_check(self.root)

    def test_route_uses_actual_account_and_refuses_foreign_overrides(self):
        with mock.patch.dict(os.environ, {"HOME": "/forged"}), mock.patch.object(paired.pwd, "getpwuid") as account:
            account.return_value.pw_dir = str(self.root)
            self.assertEqual(paired.stable_socket_path(), str(self.root / ".local/state/cmux/cmux.sock"))
        for env in [{"CMUX_SOCKET_PATH": "/foreign"}, {"CMUX_TAG": "dev"}, {"CMUX_TEST_SOMETHING": "1"}]:
            with self.assertRaises(ValueError): paired.require_environment(self.root, env)
        for args in [["--socket", "/foreign", "capabilities"], ["--socket=/foreign"]]:
            with self.assertRaises(ValueError): paired.cli_argv(self.root, args)
        self.assertNotIn("CMUX_SOCKET_PATH", paired.safe_launch_environment({"CMUX_SOCKET_PATH": "/foreign"}))


    def test_r2_native_header_nofollow_and_replacement_during_same_fd_read(self):
        path = self.write("header", native())
        link = self.root / "header-link"; link.symlink_to(path)
        with self.assertRaises(ValueError): package.macho_architectures(link)
        opened = os.open
        flags = []
        def observe(name, options, *args, **kwargs):
            flags.append(options)
            return opened(name, options, *args, **kwargs)
        with mock.patch.object(package.os, "open", side_effect=observe):
            self.assertEqual(package.macho_architectures(path), ["arm64"])
        self.assertTrue(flags[0] & os.O_NOFOLLOW)
        read = os.read
        def replace_after_read(fd, amount):
            result = read(fd, amount)
            replacement = self.write("replacement", native(0x01000007))
            replacement.replace(path)
            return result
        with mock.patch.object(package.os, "read", side_effect=replace_after_read):
            with self.assertRaisesRegex(ValueError, "native header changed"):
                package.macho_architectures(path)

    def test_r2_signature_targets_must_be_exact_manifest_native_closure(self):
        _, path, pin, rows, manifest = self.receipt()
        self.assertEqual(paired.read_pin(path, package.digest(path), manifest), (pin, rows))
        for value in ("../../outside", "Contents/missing", "Contents"):
            changed = copy.deepcopy(pin)
            changed["signing_targets"][0]["target"] = value
            with self.assertRaises(ValueError): paired.validate_native_manifest(changed, rows)
        duplicate = copy.deepcopy(pin)
        duplicate["signing_targets"][0]["executable"] = duplicate["signing_targets"][-1]["executable"]
        with self.assertRaisesRegex(ValueError, "coverage differs"):
            paired.validate_native_manifest(duplicate, rows)
        closure = json.loads((self.root / "SIGNATURE-CLOSURE.json").read_text())
        closure["targets"][0]["target"] = "Contents/missing"
        self.write("SIGNATURE-CLOSURE.json", json.dumps(closure).encode())
        pin["signature_closure_sha256"] = package.digest(self.root / "SIGNATURE-CLOSURE.json")
        self.write("PACKAGE-PIN.json", json.dumps(pin).encode())
        with self.assertRaisesRegex(ValueError, "target inventory differs"):
            paired.read_pin(path, package.digest(path), manifest)

    def test_r2_launch_rechecks_whole_app_before_open_without_external_admission(self):
        app, _, pin, rows, _ = self.receipt()
        signed = "CDHash=" + pin["code_directory_hash"] + "\nIdentifier=" + package.BUNDLE_ID + "\nSignature=adhoc\nTeamIdentifier=not set\n"
        def signature(argv, **kwargs):
            return {"stdout": plistlib.dumps(package.MAIN_ENTITLEMENTS).decode() if "--entitlements" in argv else "",
                    "stderr": signed, "returncode": 0, "argv": argv}
        with mock.patch.object(package, "INSTALL_PATH", str(app)), \
             mock.patch.object(package, "command", side_effect=signature) as commands, \
             mock.patch.object(paired.pwd, "getpwuid") as account, \
             mock.patch.object(paired, "installed_gui_processes", return_value=[]), \
             mock.patch.object(paired, "require_quiescent_socket", return_value={"status": "absent"}), \
             mock.patch.object(paired.subprocess, "run") as opened:
            account.return_value.pw_dir = str(self.root)
            pin["required_install_path"] = str(app)
            opened.return_value.returncode = 0
            opened.return_value.stdout = opened.return_value.stderr = ""
            result = paired.launch(app, pin, {"HOME": str(self.root)}, timeout=3)
            self.assertTrue(result["immediate_full_app_verification"]["passed"])
            self.assertEqual(commands.call_count, 3)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(opened.call_args.args[0], ["/usr/bin/open", "-a", str(app)])
            self.assertGreater(opened.call_args.kwargs["timeout"], 0)
            self.write("fixture.app/Contents/Resources/foreign-resource", b"changed after prior caller check")
            opened.reset_mock()
            with self.assertRaisesRegex(ValueError, "member count differs"):
                paired.launch(app, pin, {"HOME": str(self.root)}, timeout=3)
            opened.assert_not_called()


    def test_r3_shell_only_rejects_three_root_counterexamples_and_unknown_panel(self):
        cases = [
            {"id": "p", "type": "agentSession", "agentSession": {"sessionId": "preserved-synthetic-session"}},
            {"id": "p", "type": "browser", "browser": {"url": "https://example.invalid/"}},
            {"id": "p", "type": "terminal", "terminal": {"restoreRecovery": {
                "reason": "original-agent-unavailable", "recoveryInput": "codex resume preserved-synthetic-session"}}},
            {"id": "p", "type": "unknown", "terminal": {}},
            {"id": "p", "terminal": {}},
            {"id": "p", "type": "terminal", "terminal": {}, "browser": {}},
            {"id": "p", "type": "terminal", "terminal": {}, "agentSession": {}},
        ]
        for panel in cases:
            snapshot = self.snapshot()
            snapshot["windows"][0]["tabManager"]["workspaces"][0]["panels"] = [panel]
            path = self.install_snapshot(snapshot); before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "original-session ownership"):
                paired.first_adoption_snapshot_check(self.root)
            self.assertEqual(path.read_bytes(), before)
        self.install_snapshot(self.snapshot())
        self.assertEqual(paired.first_adoption_snapshot_check(self.root)["status"], "no_saved_agent_or_command_state")

    def test_r3_metadata_rejects_any_users_home_without_username_enumeration(self):
        before = {"CFBundleIdentifier": package.BUNDLE_ID, "CFBundleExecutable": "cmux",
                  "CMUXIrohRelayPolicyKeyID": "cmux-production-relay-policy-2026-07",
                  "CFBundleURLTypes": [{"CFBundleURLName": package.BUNDLE_ID + ".auth", "CFBundleURLSchemes": ["cmux"]}]}
        # Deliberately synthetic usernames absent from any hosted-runner list.
        # Keep both original /Users path values without publishing a host locator.
        for synthetic_name, suffix in [("not-a-known-runner", "Library/token"), ("future-user", "private")]:
            value = "/Users/" + synthetic_name + "/" + suffix
            with self.assertRaisesRegex(ValueError, "CI/dev package metadata"):
                package.transform_plist(plistlib.dumps(before | {"UnintendedFixedHome": value}))
        transformed, _ = package.transform_plist(plistlib.dumps(before))
        self.assertEqual(plistlib.loads(transformed)["LSEnvironment"], package.ENVIRONMENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)

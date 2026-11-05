"""Small, offline negative controls for the build gates; never compiles Codex."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prepared_ci", Path(__file__).with_name("ci.py"))
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.value = json.loads(Path(__file__).with_name("freeze.template.json").read_bytes())
        self.value["status"] = "FROZEN_FOR_ISOLATED_BUILD"
        for field in list(self.value):
            if field.endswith("sha256"):
                self.value[field] = "a" * 64
        self.value["overlay"] = {"codex-rs/core/src/recovery.rs": {
            "source": "source/codex-rs/core/src/recovery.rs", "before_sha256": None,
            "after_sha256": "b" * 64}}
        self.value["generated_schema_files"] = {"v2/RecoveryParams.json": "c" * 64}
        self.value["required_cases"] = [
            {"role": role, "package": package, "binary": package.replace("-", "_"),
             "name": "isolated_" + role, "new": True}
            for role, package in ci.ROLES.items()]
        self.value["required_cases"].append({"role": "foreground_regression", "package": "codex-tui",
                   "binary": "codex_tui", "name": "foreground_existing", "new": False})

    def load(self, value):
        path = self.root / "freeze.json"
        path.write_text(json.dumps(value))
        return ci.load_freeze(path, ci.sha(path))

    def test_complete_contract_parses_without_running_build(self):
        self.assertEqual(self.load(self.value)["upstream_commit"], ci.UPSTREAM)

    def test_unfrozen_template_is_rejected(self):
        path = Path(__file__).with_name("freeze.template.json")
        with self.assertRaises(ValueError):
            ci.load_freeze(path, ci.sha(path))

    def test_exact_native_source_is_allowed_but_unrelated_overlays_are_rejected(self):
        value = copy.deepcopy(self.value)
        source = value["overlay"].pop("codex-rs/core/src/recovery.rs")
        value["overlay"]["native/client-foreground/src/compaction_socket.rs"] = source
        self.assertIn("native/client-foreground/src/compaction_socket.rs", self.load(value)["overlay"])
        for bad in ("native/client-foreground-evil/file.rs", "native/other/file.rs",
                    "native/client-foreground/../../other.rs", ".github/workflows/other.yml"):
            value["overlay"] = {bad: source}
            with self.subTest(path=bad), self.assertRaises(ValueError):
                self.load(value)

    def test_goal_and_native_authority_cases_must_be_new_and_correct_packages(self):
        for role in ("goal", "native_authority"):
            for mutation in ({"new": False}, {"package": "codex-tui"}):
                value = copy.deepcopy(self.value)
                next(x for x in value["required_cases"] if x["role"] == role).update(mutation)
                with self.subTest(role=role, mutation=mutation), self.assertRaises(ValueError):
                    self.load(value)

    def test_missing_new_roles_cannot_inherit_old_passes(self):
        for role in ci.ROLES:
            with self.subTest(role=role):
                value = copy.deepcopy(self.value)
                value["required_cases"] = [x for x in value["required_cases"] if x["role"] != role]
                with self.assertRaises(ValueError):
                    self.load(value)

    def test_changed_hash_upstream_or_path_rejected(self):
        for field, bad in (("upstream_commit", "f" * 40), ("test_cargo_lock_sha256", None)):
            value = copy.deepcopy(self.value)
            value[field] = bad
            with self.assertRaises(ValueError):
                self.load(value)
        with self.assertRaises(ValueError):
            ci.checked_path(self.root, "../outside")
        target = self.root / "linked"
        target.symlink_to(self.root / "unowned")
        with self.assertRaises(ValueError):
            ci.checked_path(self.root, "linked/file.rs")

    def test_local_full_build_is_rejected(self):
        with patch.dict(ci.os.environ, {}, clear=True), self.assertRaises(ValueError):
            ci.require_github_runner()

    def test_junit_missing_failed_skipped_and_duplicate_are_rejected(self):
        target = self.root / "result.xml"
        for xml in ('<testsuites/>', '<testsuites><testcase name="new"><skipped/></testcase></testsuites>',
                    '<testsuites><testcase name="new"><failure/></testcase></testsuites>',
                    '<testsuites><testcase name="old"/></testsuites>',
                    '<testsuites><testcase name="new"/><testcase name="new"/></testsuites>'):
            target.write_text(xml)
            with self.assertRaises(ValueError):
                ci.check_junit(target, ["new"])
        target.write_text('<testsuites><testcase name="new"/></testsuites>')
        self.assertEqual(ci.check_junit(target, ["new"])["cases"], 1)

    def test_external_graph_change_is_visible(self):
        first = b'[[package]]\nname="local"\nversion="0.0.0"\n[[package]]\nname="external"\nversion="1"\nsource="registry+x"\nchecksum="a"\n'
        local_version = first.replace(b'version="0.0.0"', b'version="0.164.0"')
        external_change = first.replace(b'checksum="a"', b'checksum="b"')
        self.assertEqual(ci.external_graph(first), ci.external_graph(local_version))
        self.assertNotEqual(ci.external_graph(first), ci.external_graph(external_change))

    def test_standalone_native_external_versions_must_exist_in_workspace_lock(self):
        root = b'[[package]]\nname="one"\nversion="1"\nsource="registry+x"\nchecksum="a"\n'
        native = root + b'[[package]]\nname="local"\nversion="0.1.0"\n'
        ci.require_native_dependency_subset(native, root)
        for bad in (native.replace(b'checksum="a"', b'checksum="b"'),
                    native.replace(b'version="1"', b'version="2"')):
            with self.assertRaises(ValueError):
                ci.require_native_dependency_subset(bad, root)


if __name__ == "__main__":
    unittest.main(verbosity=2)

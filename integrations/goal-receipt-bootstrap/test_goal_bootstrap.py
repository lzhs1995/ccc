import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import goal_bootstrap as g


class BootstrapGates(unittest.TestCase):
    def test_actual_complete_bundle_validates_without_build(self):
        path = g.HERE / "bootstrap.json"
        value, members = g.validate(path, g.sha(path))
        self.assertEqual(len(members), 18)
        self.assertEqual(value["goal_members"], 18)

    def test_wrong_manifest_pin_refused(self):
        with self.assertRaisesRegex(ValueError, "manifest SHA changed"):
            g.validate(g.HERE / "bootstrap.json", "0" * 64)

    def test_manifest_traversal_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bootstrap.json"
            value = json.loads((g.HERE / "bootstrap.json").read_bytes())
            value["input_files"] = {"../escape": "0" * 64}
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                g.validate(path, g.sha(path))

    def test_external_version_change_detected(self):
        raw = (g.HERE / "r7/release-Cargo.lock").read_bytes()
        # Alter an actual external package, not the first local package.
        import tomllib
        package = next(p for p in tomllib.loads(raw.decode())["package"] if "source" in p)
        marker = (f'name = "{package["name"]}"\nversion = "{package["version"]}"').encode()
        altered = raw.replace(marker, marker.replace(package["version"].encode(), b"999.0.0"), 1)
        self.assertNotEqual(g.external_graph(raw), g.external_graph(altered))

    def test_format_changed_other_module_refused(self):
        before = {"files": [{"path": "a.rs", "sha256": "a"}, {"path": "b.rs", "sha256": "b"}]}
        after = {"files": [{"path": "a.rs", "sha256": "A"}, {"path": "b.rs", "sha256": "B"}]}
        with self.assertRaisesRegex(ValueError, "unrelated"):
            g.check_format_scope(before, after, ["a.rs"])

    def test_format_deleted_file_refused(self):
        with self.assertRaises(ValueError):
            g.check_format_scope({"files": [{"path": "a.rs"}]}, {"files": []}, ["a.rs"])

    def test_fake_generator_success_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "generator.log"
            path.write_text("test unrelated ... ok\ntest result: ok. 1 passed; 0 failed; 0 ignored;\n")
            with self.assertRaises(ValueError):
                g.check_generator_log(path)

    def test_schema_review_records_old_interface_changes_without_approving(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old, new, output = root / "old", root / "new", root / "output"
            for path in (old, new, output):
                path.mkdir()
            (old / "old.json").write_text('{"required":["old"]}\n')
            (new / "old.json").write_text('{"required":["new"]}\n')
            g.prepare_review_material(old, new, output)
            result = json.loads((output / "SCHEMA-REVIEW-MATERIAL.json").read_text())
            self.assertFalse(result["compatibility_passed"])
            self.assertFalse(result["changed"][0]["json_semantics_equal"])
            self.assertIn('-{"required":["old"]}', (output / "SCHEMA-CANDIDATE.diff").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Offline real official fetcher tests; only URL bytes and fixture trust root vary.
No compiler or production process is run. Tiny fixture archive is not V8.
"""
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("candidate_v8_ci", HERE / "ci.py")
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)


class V8PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "assembled"; self.root.mkdir()
        self.output = self.base / "output"; self.output.mkdir()
        self.archive_name = "librusty_v8_ptrcomp_sandbox_release_aarch64-apple-darwin.a.gz"
        self.binding_name = "src_binding_ptrcomp_sandbox_release_aarch64-apple-darwin.rs"
        self.manifest_name = "rusty_v8_ptrcomp_sandbox_release_aarch64-apple-darwin.sha256"
        self.artifacts = {self.archive_name: b"TINY TEST ONLY ARCHIVE", self.binding_name: b"// tiny test binding\n"}
        manifest = "".join(hashlib.sha256(raw).hexdigest() + "  " + name + "\n"
                           for name, raw in self.artifacts.items()).encode()
        self.responses = {**self.artifacts, self.manifest_name: manifest}
        for name in ci.V8_SOURCE_PINS:
            dest = self.root / name; dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HERE / "test-inputs" / name, dest)
        self.trust_name = "third_party/v8/rusty_v8_152_2_0_release_manifests.sha256"
        (self.root / self.trust_name).write_text(hashlib.sha256(manifest).hexdigest() + "  " + self.manifest_name + "\n")
        (self.root / "codex-rs").mkdir()
        (self.root / "codex-rs/Cargo.lock").write_text('[[package]]\nname="v8"\nversion="152.2.0"\n')
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.expected = ci.full_source(self.root)
        self.pins = {name: ci.sha(self.root / name) for name in ci.V8_SOURCE_PINS}
        self.calls = []
        self.environment = patch.dict(os.environ, {}, clear=True); self.environment.start()
        self.addCleanup(self.environment.stop)

    def urlopen(self, url, timeout):
        self.calls.append(url)
        self.assertEqual(timeout, 120)
        self.assertTrue(url.startswith("https://github.com/openai/codex/releases/download/rusty-v8-v152.2.0/"))
        return io.BytesIO(self.responses[url.rsplit("/", 1)[1]])

    def prepare(self):
        # Original helper/targets bytes are exact. Only this synthetic test trust
        # manifest is pinned to the tiny network fixture; production constants stay.
        with patch.object(ci, "V8_SOURCE_PINS", self.pins), patch("urllib.request.urlopen", self.urlopen):
            return ci.prepare_v8_artifacts(self.root, self.output, self.expected)

    def test_real_official_fetcher_verifies_two_layers_and_receipt_no_source_writes(self):
        old_path, old_bytecode = sys.path[:], sys.dont_write_bytecode
        receipt = self.prepare()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(ci.full_source(self.root), self.expected)
        self.assertEqual(list(self.root.rglob("__pycache__")), [])
        self.assertEqual(sys.path, old_path); self.assertEqual(sys.dont_write_bytecode, old_bytecode)
        self.assertNotIn("CODEX_REPO_ROOT", os.environ)
        self.assertFalse(any(n.startswith("codex_package") for n in sys.modules))
        self.assertEqual(receipt["function"], "codex_package.v8.fetch_codex_v8_artifacts")
        self.assertEqual(receipt["official_source_pins"], self.pins)
        self.assertEqual(receipt["release_manifest"]["sha256"], hashlib.sha256(self.responses[self.manifest_name]).hexdigest())
        self.assertEqual({Path(x["path"]).name: x["sha256"] for x in receipt["artifacts"]},
                         {n: hashlib.sha256(v).hexdigest() for n, v in self.artifacts.items()})
        self.assertEqual(os.environ["RUSTY_V8_ARCHIVE"], receipt["artifacts"][0]["path"])
        self.assertEqual(os.environ["RUSTY_V8_SRC_BINDING_PATH"], receipt["artifacts"][1]["path"])
        self.assertEqual(json.loads((self.output / "V8-ARTIFACTS.json").read_bytes()), receipt)

    def test_corrupt_release_manifest_fails_before_asset_download(self):
        self.responses[self.manifest_name] += b"tampered"
        with self.assertRaisesRegex(RuntimeError, "trusted SHA-256"): self.prepare()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("RUSTY_V8_ARCHIVE", os.environ)
        self.assertFalse((self.output / "V8-ARTIFACTS.json").exists())
        self.assertEqual(ci.full_source(self.root), self.expected)

    def test_corrupt_archive_fails_without_binding_or_environment(self):
        self.responses[self.archive_name] = b"bad archive"
        with self.assertRaisesRegex(RuntimeError, "checksum validation"): self.prepare()
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn("RUSTY_V8_ARCHIVE", os.environ)
        self.assertFalse((self.output / "V8-ARTIFACTS.json").exists())

    def test_corrupt_binding_fails_without_receipt_or_environment(self):
        self.responses[self.binding_name] = b"bad binding"
        with self.assertRaisesRegex(RuntimeError, "checksum validation"): self.prepare()
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn("RUSTY_V8_ARCHIVE", os.environ)
        self.assertFalse((self.output / "V8-ARTIFACTS.json").exists())

    def test_source_changes_before_import_rejected(self):
        (self.root / "scripts/codex_package/v8.py").write_text("raise RuntimeError('must never import')")
        with self.assertRaisesRegex(ValueError, "assembled source changed"): self.prepare()
        self.assertEqual(self.calls, [])

    def test_helper_pin_rejected_even_if_passed_inventory_updated(self):
        p = self.root / "scripts/codex_package/v8.py"; p.write_text(p.read_text() + "\n# changed\n")
        self.expected = ci.full_source(self.root)
        with self.assertRaisesRegex(ValueError, "official V8 fetcher"): self.prepare()
        self.assertEqual(self.calls, [])

    def test_any_v8_override_rejected_including_false_and_partial(self):
        for key, value in [("V8_FROM_SOURCE", "0"), ("V8_FROM_SOURCE", "1"), ("V8_FORCE_DEBUG", "0"),
                           ("RUSTY_V8_ARCHIVE", "/tmp/archive"), ("RUSTY_V8_SRC_BINDING_PATH", "/tmp/binding"),
                           ("RUSTY_V8_MIRROR", "https://other"), ("RUSTY_V8_SKIP_DOWNLOAD", "1")]:
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}), self.assertRaisesRegex(ValueError, "unreviewed V8"):
                self.prepare()
        self.assertEqual(self.calls, [])

    def test_loaded_module_rejected_without_replacing_it(self):
        marker = object()
        with patch.dict(sys.modules, {"codex_package": marker}):
            with self.assertRaisesRegex(ValueError, "preloaded package"): self.prepare()
            self.assertIs(sys.modules["codex_package"], marker)
        self.assertEqual(self.calls, [])

    def test_changed_lock_version_rejected_before_network(self):
        p = self.root / "codex-rs/Cargo.lock"; p.write_text(p.read_text().replace("152.2.0", "152.1.0"))
        self.expected = ci.full_source(self.root)
        with self.assertRaisesRegex(ValueError, "V8 version differs"): self.prepare()
        self.assertEqual(self.calls, [])

    def test_cache_in_source_or_preexisting_refused(self):
        original_output = self.output
        self.output = self.root / "bad-output"; self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "fresh cache outside"): self.prepare()
        self.output = original_output; (self.output / "v8-artifacts").mkdir()
        with self.assertRaisesRegex(ValueError, "fresh cache outside"): self.prepare()
        self.assertEqual(self.calls, [])

    def test_source_mutation_during_fetch_rejected(self):
        real = self.urlopen
        def mutation(url, timeout):
            answer = real(url, timeout)
            if url.endswith(self.binding_name): (self.root / "injected").write_text("bad")
            return answer
        self.urlopen = mutation
        with self.assertRaisesRegex(ValueError, "modified frozen source"): self.prepare()
        self.assertNotIn("RUSTY_V8_ARCHIVE", os.environ)
        self.assertFalse((self.output / "V8-ARTIFACTS.json").exists())

    def test_import_environment_restored_after_download_failure(self):
        os.environ["CODEX_REPO_ROOT"] = "prior-value"
        old_path, old_bytecode = sys.path[:], sys.dont_write_bytecode
        self.responses[self.manifest_name] = b"bad"
        with self.assertRaises(RuntimeError): self.prepare()
        self.assertEqual(os.environ["CODEX_REPO_ROOT"], "prior-value")
        self.assertEqual(sys.path, old_path); self.assertEqual(sys.dont_write_bytecode, old_bytecode)
        self.assertFalse(any(n.startswith("codex_package") for n in sys.modules))

    def test_actual_original_test_inputs_match_production_source_pins(self):
        for name, pin in ci.V8_SOURCE_PINS.items(): self.assertEqual(ci.sha(HERE / "test-inputs" / name), pin)

    def test_ci_sequence_prepares_before_helpers_and_keeps_failed_junit(self):
        tree = ast.parse((HERE / "ci.py").read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)]
        prep = next(n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "prepare_v8_artifacts")
        builds = [n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "run"
                  and n.args and isinstance(n.args[0], ast.List)
                  and any(isinstance(x, ast.Constant) and x.value == "test_stdio_server" for x in n.args[0].elts)]
        self.assertEqual(len(builds), 1); self.assertLess(prep.lineno, builds[0].lineno)
        source = (HERE / "ci.py").read_text()
        self.assertIn('finally:\n            # Preserve actual failed JUnit', source)
        self.assertIn('"v8_artifacts_receipt_sha256": sha(output / "V8-ARTIFACTS.json")', source)


if __name__ == "__main__": unittest.main(verbosity=2)

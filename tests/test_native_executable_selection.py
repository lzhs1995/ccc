"""An obsolete launcher receipt must not downgrade new standby clients."""
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import ccc_batch_guard as guard


class NativeExecutableSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.entry = self.root / 'codex'
        self.metadata = self.root / 'state/codex-launcher.json'
        self.metadata.parent.mkdir()
        self.old = self.binary(self.root / 'old-codex')
        self.metadata.write_text(json.dumps({'native_binary': str(self.old)}))

    def binary(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\xcf\xfa\xed\xfe' + b'fixture, never executed')
        path.chmod(0o700)
        return path

    def select(self):
        return guard.native_binary(entrypoint=self.entry, metadata=self.metadata)

    def managed(self):
        package = self.root / 'new package'
        binary = self.binary(package / 'bin/codex')
        (package / 'codex-package.json').write_text(json.dumps({
            'layoutVersion': 1, 'variant': 'codex', 'entrypoint': 'bin/codex'}))
        wrapper = package / 'managed-codex'
        wrapper.write_text('#!/bin/sh\nexport CODEX_CLIENT_THREAD_OBSERVER=1\nexec '
                           + shlex.quote(str(binary)) + ' "$@"\n')
        wrapper.chmod(0o700)
        self.entry.symlink_to(wrapper)
        return wrapper, binary

    def test_managed_install_supersedes_old_receipt(self):
        _, binary = self.managed()
        self.assertEqual(self.select(), str(binary))

    def test_native_install_supersedes_old_receipt(self):
        binary = self.binary(self.root / 'new-codex')
        self.entry.symlink_to(binary)
        self.assertEqual(self.select(), str(binary))

    def test_legacy_guard_still_uses_its_native(self):
        wrapper = self.metadata.with_name('codex-guard')
        wrapper.write_text('#!/bin/sh\nlegacy fixture\n')
        self.entry.symlink_to(wrapper)
        self.assertEqual(self.select(), str(self.old))

    def test_missing_current_entry_never_falls_back(self):
        with self.assertRaises(RuntimeError): self.select()

    def test_unknown_script_never_executes_or_falls_back(self):
        wrapper, _ = self.managed()
        wrapper.write_text('#!/bin/sh\nexec /old/codex "$@"\n')
        with patch('subprocess.Popen') as spawn, self.assertRaises(RuntimeError): self.select()
        spawn.assert_not_called()

    def test_manifest_entrypoint_mismatch_rejected(self):
        wrapper, _ = self.managed()
        manifest = wrapper.parent / 'codex-package.json'
        row = json.loads(manifest.read_text()); row['entrypoint'] = '../old-codex'
        manifest.write_text(json.dumps(row))
        with self.assertRaises(RuntimeError): self.select()

    def test_packaged_target_symlink_rejected(self):
        _, binary = self.managed()
        binary.unlink(); binary.symlink_to(self.old)
        with self.assertRaises(RuntimeError): self.select()

    def test_changed_installed_link_rejected(self):
        self.managed()
        original = guard.os.access
        def replace(path, mode):
            self.entry.unlink(); self.entry.symlink_to(self.old)
            return original(path, mode)
        with patch.object(guard.os, 'access', side_effect=replace), self.assertRaises(RuntimeError): self.select()

    def test_changed_wrapper_after_read_rejected(self):
        wrapper, _ = self.managed()
        original = guard.os.access
        def replace(path, mode):
            wrapper.write_text('#!/bin/sh\nexit 0\n')
            return original(path, mode)
        with patch.object(guard.os, 'access', side_effect=replace), self.assertRaises(RuntimeError): self.select()

    def test_current_binary_must_be_executable(self):
        _, binary = self.managed(); binary.chmod(0o600)
        with self.assertRaises(RuntimeError): self.select()

    def test_legacy_recursion_rejected(self):
        wrapper = self.metadata.with_name('codex-guard')
        wrapper.write_text('#!/bin/sh\nfixture\n'); self.entry.symlink_to(wrapper)
        self.metadata.write_text(json.dumps({'native_binary': str(wrapper)}))
        with self.assertRaises(RuntimeError): self.select()


if __name__ == '__main__': unittest.main()

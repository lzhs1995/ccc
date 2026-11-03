import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ccc_batch_guard as guard

LAUNCHER = '''#!/bin/bash
# Synthetic wrapper fixture; it is read and hashed, never executed.
exec "$HOME/.local/bin/codex" "$@"
'''
LAUNCHER_SHA256 = hashlib.sha256(LAUNCHER.encode()).hexdigest()

class ToolchainEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.bin = self.home / '.local/bin'
        self.bin.mkdir(parents=True)
        self.wrapper = self.bin / 'codex-toolchain-launch'
        self.wrapper.write_text(LAUNCHER)
        self.wrapper.chmod(0o700)
        self.native = self.home / 'current-native'
        self.native.write_bytes(b'\x7fELFverified-test-native')
        self.native.chmod(0o700)
        self.alias = self.bin / 'codex'
        self.alias.symlink_to(self.native)
        self.entry = self.home / 'installed-codex'
        self.entry.symlink_to(self.wrapper)

    def resolve(self, *, trusted_fixture=True):
        # Replace only the expected digest for this one resolver invocation.
        # Hashing, file rereads, symlink fences and native-magic checks remain real.
        expected = LAUNCHER_SHA256 if trusted_fixture else guard.TOOLCHAIN_LAUNCHER_SHA256
        with patch.object(Path, 'home', return_value=self.home), \
             patch.object(guard, 'TOOLCHAIN_LAUNCHER_SHA256', expected):
            return guard.native_binary(entrypoint=self.entry)

    def test_trusted_wrapper_uses_current_native_and_ignores_old_guard_metadata(self):
        app = self.home / 'Library/Application Support/cmux-codex-continue'
        app.mkdir(parents=True)
        (app / 'codex-launcher.json').write_text('{"native_binary":"/stale/codex"}')
        self.assertEqual(self.resolve(), str(self.native))

    def test_changed_wrapper_is_rejected_without_executing_shell(self):
        for body in (LAUNCHER + 'echo changed\n', LAUNCHER.replace('"$@"', '$(id)'),
                     '#!/bin/bash\nexec codex "$@"\n'):
            with self.subTest(body=body):
                self.wrapper.write_text(body)
                with self.assertRaisesRegex(RuntimeError, 'cannot be proved'):
                    self.resolve()

    def test_synthetic_wrapper_is_untrusted_without_fixture_digest(self):
        published_digest = guard.TOOLCHAIN_LAUNCHER_SHA256
        self.assertNotEqual(LAUNCHER_SHA256, published_digest)
        with self.assertRaisesRegex(RuntimeError, 'cannot be proved'):
            self.resolve(trusted_fixture=False)
        self.assertEqual(self.resolve(), str(self.native))
        self.assertEqual(guard.TOOLCHAIN_LAUNCHER_SHA256, published_digest)
        with self.assertRaisesRegex(RuntimeError, 'cannot be proved'):
            self.resolve(trusted_fixture=False)

    def test_missing_native_or_unknown_leaf_script_is_rejected(self):
        self.alias.unlink()
        with self.assertRaises(RuntimeError):
            self.resolve()
        self.native.write_text('#!/bin/sh\nexit 0\n')
        self.alias.symlink_to(self.native)
        with self.assertRaises(RuntimeError):
            self.resolve()

    def test_native_symlink_changed_during_resolution_is_rejected(self):
        other = self.home / 'replacement'
        other.write_bytes(b'\x7fELFother')
        other.chmod(0o700)
        original = Path.resolve
        calls = 0
        def changing(path, *args, **kwargs):
            nonlocal calls
            result = original(path, *args, **kwargs)
            if path == self.alias:
                calls += 1
                if calls == 1:
                    self.alias.unlink()
                    self.alias.symlink_to(other)
            return result
        with patch.object(Path, 'resolve', changing):
            with self.assertRaises(RuntimeError):
                self.resolve()

if __name__ == '__main__':
    unittest.main()

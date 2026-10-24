"""Backup publication failures retain foreign or crashed evidence."""
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

RUNTIME = Path(os.environ["CCC_COMPACT_TEST_RUNTIME"])
sys.path.insert(0, str(RUNTIME))
import ccc_compact_backup as backup
from ccc_compact_support import OwnedDirectory


class BackupOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.owned = OwnedDirectory(self.directory)
        self.addCleanup(self.owned.close)
        self.original = b'{"original":"retained"}\n'
        self.owned.write_once_bytes("original.json", self.original)

    def temps(self):
        return list(self.directory.glob(".backup-*.tmp"))

    def test_persistent_exclusive_rename_failure_does_not_accumulate_copies(self):
        with patch.object(backup, "_rename_exclusive", side_effect=OSError(errno.ENOTSUP, "unsupported")):
            for _ in range(5):
                with self.assertRaises(OSError):
                    backup.publish_backup(self.owned, "backup.json", self.original)
                self.assertEqual(self.temps(), [])
        self.assertEqual(self.owned.read_bytes("original.json", limit=100), self.original)
        self.assertFalse((self.directory / "backup.json").exists())
        backup.publish_backup(self.owned, "backup.json", self.original)
        self.assertEqual(self.owned.read_bytes("backup.json", limit=100), self.original)

    def test_partial_enospc_write_cleans_only_owned_inode(self):
        real_fdopen = os.fdopen

        class Torn:
            def __init__(self, descriptor, *args, **kwargs):
                self.stream = real_fdopen(descriptor, *args, **kwargs)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def write(self, raw):
                self.stream.write(raw[:len(raw) // 2])
                self.stream.flush()
                raise OSError(errno.ENOSPC, "disk full during temp write")

        with patch.object(backup.os, "fdopen", Torn):
            with self.assertRaisesRegex(OSError, "disk full during temp write"):
                backup.publish_backup(self.owned, "backup.json", self.original)
        self.assertEqual(self.temps(), [])
        self.assertEqual(self.owned.read_bytes("original.json", limit=100), self.original)

    def test_name_collision_never_deletes_foreign_existing_temp(self):
        with patch.object(backup.secrets, "token_hex", return_value="a" * 24):
            name = ".backup-" + backup.hashlib.sha256(self.original).hexdigest() + "-" + "a" * 24 + ".tmp"
            self.owned.write_once_bytes(name, b"foreign unique evidence")
            before = (self.directory / name).stat()
            with self.assertRaises(FileExistsError):
                backup.publish_backup(self.owned, "backup.json", self.original)
            self.assertEqual(self.owned.read_bytes(name, limit=100), b"foreign unique evidence")
            self.assertEqual((self.directory / name).stat().st_ino, before.st_ino)

    def test_replaced_temp_is_retained_and_original_error_survives(self):
        def replace_then_fail(fd, source, destination):
            foreign = ".replacement.tmp"
            self.owned.write_once_bytes(foreign, b"foreign unique evidence")
            os.rename(foreign, source, src_dir_fd=fd, dst_dir_fd=fd)
            raise OSError(errno.ENOTSUP, "original rename failure")

        with patch.object(backup, "_rename_exclusive", replace_then_fail):
            with self.assertRaisesRegex(OSError, "original rename failure") as raised:
                backup.publish_backup(self.owned, "backup.json", self.original)
        self.assertTrue(any("retained" in note for note in getattr(raised.exception, "__notes__", [])))
        self.assertEqual(len(self.temps()), 1)
        self.assertEqual(self.temps()[0].read_bytes(), b"foreign unique evidence")
        self.assertEqual(self.owned.read_bytes("original.json", limit=100), self.original)


if __name__ == "__main__":
    unittest.main(verbosity=2)

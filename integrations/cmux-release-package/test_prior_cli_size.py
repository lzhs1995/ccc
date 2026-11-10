"""Original metadata admission only; no archive download or native execution."""
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import release_package as package


class PriorCLIMember(unittest.TestCase):
    def setUp(self):
        self.pin = package.shared.original_pin()
        self.guard = patch.object(package.subprocess, "run", side_effect=AssertionError("no external commands"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def archive(self, size=106086096, *, count=1, mode=stat.S_IFREG | 0o755):
        rows = []
        for _ in range(count):
            item = zipfile.ZipInfo(self.pin["app_name"] + "/Contents/Resources/bin/cmux")
            item.file_size = size
            item.external_attr = mode << 16
            rows.append(item)
        class OriginalDirectory:
            def infolist(self):
                return rows
        return OriginalDirectory()

    def test_actual_original_manifest_size_above_old_cap_is_admitted(self):
        row, expected = package.prior_cli_member(self.archive(), self.pin)
        self.assertEqual(row.file_size, 106086096)
        self.assertGreater(row.file_size, 96 * 1024**2)
        self.assertEqual(expected["size"], row.file_size)
        self.assertEqual(expected["sha256"], self.pin["cli_sha256"])

    def test_smaller_larger_missing_duplicate_and_nonregular_members_rejected(self):
        for archive in (self.archive(106086095), self.archive(106086097),
                        self.archive(count=0), self.archive(count=2), self.archive(mode=stat.S_IFLNK | 0o777)):
            with self.assertRaises(ValueError):
                package.prior_cli_member(archive, self.pin)

    def test_original_provenance_tamper_rejected_before_member_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "PROVENANCE.json").write_text('{"bundle_members": []}')
            with patch.object(package.shared, "PRE", root), self.assertRaisesRegex(ValueError, "pinned receipt differs"):
                package.prior_cli_member(self.archive(), self.pin)


if __name__ == "__main__":
    unittest.main(verbosity=2)

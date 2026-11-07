from pathlib import Path
import base64
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import seal_nested as n


class ReceiptSelectionTests(unittest.TestCase):
    def test_valid_synthetic_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); (p/'receipts').mkdir()
            name = 'nested-fixture-0123456789ab-nested-four-3-123-10000.json'
            (p/'receipts'/name).write_text('{}')
            self.assertIn('receipts/' + name, n.selected(p))
    def test_foreign_filename_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); (p/'receipts').mkdir(); (p/'receipts'/'auth.json').write_text('{}')
            with self.assertRaises(ValueError): n.selected(p)
    def test_receipt_link_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); (p/'receipts').mkdir(); (p/'private').write_text('{}')
            (p/'receipts'/'nested-fixture-0123456789ab-nested-two-1-123-10000.json').symlink_to(p/'private')
            with self.assertRaises(ValueError): n.selected(p)
    def test_receipt_directory_link_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); (p/'private').mkdir(); (p/'receipts').symlink_to(p/'private')
            with self.assertRaises(ValueError): n.selected(p)


class FixedEvidenceSelectionTests(unittest.TestCase):
    REQUIRED = ('FIXED-APP-ADMISSION.json',
        'gui/GEN-1-APP-RECHECK.json', 'gui/GEN-2-APP-RECHECK.json',
        'gui/GEN-1-ENVIRONMENT.json', 'gui/GEN-2-ENVIRONMENT.json')

    def test_exact_five_new_records_only_and_no_screen_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / 'gui').mkdir()
            for name in self.REQUIRED:
                (root / name).write_text('{}')
            foreign = ('gui/GEN-3-ENVIRONMENT.json', 'gui/arbitrary.json',
                       'FIXED-APP-SECRETS.json', 'POST-FAILURE-SCREEN-INTENT.json', 'POST-FAILURE-SCREENS.json')
            for name in foreign:
                (root / name).write_text('{}')
            selected = n.selected(root)
            self.assertEqual(len(selected), len(set(selected)))
            for name in self.REQUIRED:
                self.assertEqual(selected.count(name), 1)
            for name in foreign:
                self.assertNotIn(name, selected)

    def test_all_five_original_bytes_survive_authenticated_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve(); root = base / 'private'; root.mkdir(); (root / 'gui').mkdir()
            values = {name: (json.dumps({'original_member': name}) + '\n').encode() for name in self.REQUIRED}
            for name, value in values.items():
                (root / name).write_bytes(value)
            (root / 'gui/GEN-3-ENVIRONMENT.json').write_text('DO_NOT_INCLUDE')
            key = os.urandom(32)
            env = {n.secure.KEY_ENV: base64.b64encode(key).decode(), 'GITHUB_REPOSITORY': 'lzhs1995/ccc',
                   'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'reviewed-test-head'}
            output = base / 'encrypted'
            with patch.object(n.secure, 'DIAGNOSTICS', n.selected(root)), \
                 patch.object(n.secure, 'runner_root', return_value=base), patch.dict(os.environ, env):
                n.secure.seal_diagnostics(root, output)
            meta = json.loads((output / 'PRIVATE-DIAGNOSTICS.json').read_bytes())
            aad = base64.b64decode(meta['aad_base64'])
            self.assertEqual(json.loads(aad)['run']['GITHUB_SHA'], 'reviewed-test-head')
            plain = n.secure.unseal((output / 'private-diagnostics.aesgcm').read_bytes(), key, aad)
            with zipfile.ZipFile(io.BytesIO(plain)) as archive:
                self.assertEqual(set(archive.namelist()), set(self.REQUIRED))
                self.assertEqual({name: archive.read(name) for name in archive.namelist()}, values)
            sealed = {row['name']: row for row in meta['members'] if row['status'] == 'sealed'}
            self.assertEqual(set(sealed), set(self.REQUIRED))
            for name, value in values.items():
                self.assertEqual(sealed[name]['sha256'], n.secure.sha(value))


if __name__ == '__main__': unittest.main()

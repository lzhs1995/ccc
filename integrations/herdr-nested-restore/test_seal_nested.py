from pathlib import Path
import tempfile
import unittest
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


if __name__ == '__main__': unittest.main()

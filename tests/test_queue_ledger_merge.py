"""Distinct native interpreters must not overwrite each other's queue intent."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from ccc_codex_queue import QueueRecovery


class QueueLedgerTests(unittest.TestCase):
    def queue(self,root):
        return QueueRecovery(root/'ledger.json',root/'bindings.json',root,'continue')

    def test_concurrent_independent_owners_merge_durable_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            owners=[self.queue(root) for _ in range(8)]
            def write(i):
                for n in range(5):
                    owners[i].write_attempt(f'{i}-{n}',{'surface_id':str(i),'phase':'editing'})
            with ThreadPoolExecutor(8) as pool:
                list(pool.map(write,range(8)))
            ledger=json.loads((root/'ledger.json').read_text())
            self.assertEqual(set(ledger),{f'{i}-{n}' for i in range(8) for n in range(5)})
            self.assertEqual(list(root.glob('*.tmp')),[])

    def test_missing_previously_written_ledger_cannot_be_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);owner=self.queue(root)
            owner.write_attempt('first',{'surface_id':'original','phase':'submitting'})
            owner.ledger.unlink()
            with self.assertRaises(RuntimeError):
                owner.write_attempt('second',{'surface_id':'other','phase':'editing'})
            self.assertFalse(owner.ledger.exists())

    def test_other_owner_pending_draft_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);first=self.queue(root);second=self.queue(root)
            first.write_attempt('first',{'surface_id':'original','phase':'edited'})
            self.assertTrue(second.has_pending_draft('original'))
            first.write_attempt('first',{'surface_id':'original','phase':'submitted'})
            self.assertFalse(second.has_pending_draft('original'))

    def test_corrupt_external_record_blocks_append(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);owner=self.queue(root)
            owner.ledger.write_text('{"bad":null}')
            with self.assertRaises(RuntimeError):
                owner.write_attempt('new',{'surface_id':'original','phase':'editing'})
            self.assertEqual(owner.ledger.read_text(),'{"bad":null}')


if __name__=='__main__':
    unittest.main()

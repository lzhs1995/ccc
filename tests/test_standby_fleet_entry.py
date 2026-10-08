"""Entry order and failure semantics without native launches or model calls."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools import standby_five_workspace as entry


class ReviewedEntryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.source = self.root/'source'
        (self.source/'tools').mkdir(parents=True)
        self.script = self.source/'tools/standby_five_workspace.py'
        self.script.write_text('# entry\n')
        self.fixture = self.root/'fixture.py'
        self.fixture.write_text('# fixture\n')
        self.receipt = self.root/'result.json'
        self.receipt.write_text(json.dumps({'source_before': {
            str(p): entry.digest(p) for p in (self.fixture, self.script)}}))
        self.admission = dict(eligible=True, reasons=[], planning_reserve_bytes=40*1024**3,
            prerequisite_sha256=entry.digest(self.receipt), original_ui50_proof={'original_ui50_replayed': True})
        self.proof = {'evidence_replayed': True}
        self.experiment = Mock(output=self.root/'fleet', plan=self.root/'run')
        self.experiment.run.return_value = {'fleet_original_replay': self.proof}

    def run_entry(self, *, loader=None, final_proof=None):
        with patch.object(entry, '__file__', str(self.script)), \
                patch.object(entry, 'preflight', return_value=self.admission), \
                patch.object(entry, 'load_fixture', side_effect=loader or (lambda p: SimpleNamespace(__file__=str(p)))) as load, \
                patch.object(entry, 'Experiment', return_value=self.experiment) as construct, \
                patch.object(entry.shutil, 'disk_usage', return_value=SimpleNamespace(free=50*1024**3)), \
                patch('tools.standby_fifty_prerequisite.verify', return_value=self.admission['original_ui50_proof']), \
                patch('tools.standby_fleet_replay.verify', return_value=final_proof or self.proof):
            result = entry.run_reviewed(self.source, self.receipt, self.fixture, self.root/'out')
        return result, load, construct

    def test_complete_replay_is_required_for_final_acceptance(self):
        result, _, _ = self.run_entry()
        self.assertTrue(result['passed'])
        self.assertTrue(result['full_500_acceptance'])
        self.assertEqual(self.experiment.write.call_args.args[0], 'final-acceptance.json')

    def test_ineligible_never_imports_fixture(self):
        self.admission.update(eligible=False, reasons=['capacity'])
        with patch.object(entry, 'load_fixture', side_effect=AssertionError('must not import')):
            with self.assertRaisesRegex(ValueError, 'launch blocked'):
                self.run_entry()
        self.experiment.run.assert_not_called()

    def test_source_changed_during_fixture_import_prevents_native_setup(self):
        def loader(path):
            self.script.write_text('# changed\n')
            return SimpleNamespace(__file__=str(path))
        with self.assertRaisesRegex(ValueError, 'tested source changed'):
            self.run_entry(loader=loader)
        self.experiment.run.assert_not_called()

    def test_changed_final_proof_is_saved_as_failure(self):
        result, _, _ = self.run_entry(final_proof={'evidence_replayed': True, 'changed': True})
        self.assertFalse(result['passed'])
        self.assertIn('differs', result['error'])

    def test_new_source_during_fixture_import_prevents_native_setup(self):
        def loader(path):
            (self.source/'tools/standby_unreviewed.py').write_text('# new source\n')
            return SimpleNamespace(__file__=str(path))
        with self.assertRaisesRegex(ValueError, 'source set changed'):
            self.run_entry(loader=loader)
        self.experiment.run.assert_not_called()

    def test_fixture_not_in_ui50_freeze_never_runs(self):
        self.receipt.write_text(json.dumps({'source_before': {str(self.script): entry.digest(self.script)}}))
        self.admission['prerequisite_sha256'] = entry.digest(self.receipt)
        with self.assertRaisesRegex(ValueError, 'fixture was not tested'):
            self.run_entry()
        self.experiment.run.assert_not_called()

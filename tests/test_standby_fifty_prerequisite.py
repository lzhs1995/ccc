"""UI50 admission joins real private files; terminal/performance mocked explicitly."""
import json
import unittest
from unittest.mock import patch

from tests import test_standby_fleet_replay_integration as fleet_fixture
from tools import standby_fifty_prerequisite as prerequisite


class FiftyPrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.case = fleet_fixture.AggregateIntegration()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        c.manifest.plan.update(planned_sessions=50, batches=[{'batch_id': '0'}])
        c.bindings['batches'] = c.bindings['batches'][:1]
        terminal = json.loads((c.root/'terminal.json').read_text())
        terminal['batches'] = terminal['batches'][:1]
        c.save('terminal.json', terminal)
        c.save('rpc-responses.ndjson', {})
        self.report = dict(run_verification=c.proof, startup_timing={'startup_passed': True},
            recovery_chains=c.args['metrics']['0']['chains'])
        c.save('result.json', self.report)

    def replay(self, performance=None):
        c = self.case
        with patch.object(prerequisite, 'RunManifest', return_value=c.manifest), \
                patch.object(prerequisite, 'verify_terminal', return_value=c.proof), \
                patch.object(prerequisite, 'replay_performance', side_effect=performance or (
                    lambda *a, **k: dict(startup_passed=True, recovery_passed=True, evidence_sha256={}))):
            return prerequisite.verify(c.root/'result.json')

    def test_constructs_fifty_exact_terminal_bound_transcripts(self):
        def performance(result, **kw):
            self.assertEqual(len(result['transcript_bindings']), 50)
            self.assertEqual(result['transcript_bindings'][49]['original'], '/original/49.jsonl')
            self.assertEqual(set(kw['witnesses']), set(range(50)))
            return dict(startup_passed=True, recovery_passed=True, evidence_sha256={})
        self.assertTrue(self.replay(performance)['original_ui50_replayed'])

    def test_saved_green_summary_with_failed_original_recovery_refused(self):
        with self.assertRaisesRegex(ValueError, 'requirement failed'):
            self.replay(lambda *a, **k: dict(startup_passed=True, recovery_passed=False))

    def test_wrong_terminal_refused(self):
        self.report['run_verification'] = {'succeeded': True}
        self.case.save('result.json', self.report)
        with self.assertRaisesRegex(ValueError, 'terminal changed'):
            self.replay()

    def test_report_mutation_during_replay_refused(self):
        def performance(*a, **kw):
            self.case.save('result.json', {})
            return dict(startup_passed=True, recovery_passed=True, evidence_sha256={})
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            self.replay(performance)

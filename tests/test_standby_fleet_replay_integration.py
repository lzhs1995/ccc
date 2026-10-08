"""Aggregate IO/identity contract; native terminal and timing replays are mocked.

Their real verifier suites run separately. These tests do not certify live500.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import test_standby_fleet_identity as identity_fixture
from tools import standby_fleet_replay as aggregate


class AggregateIntegration(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        case = identity_fixture.FleetIdentityTests()
        case.setUp()
        self.args = case.args
        # Resource PID1 is intentionally invalid; shift all fixture generations.
        for rows in self.args['witnesses'].values():
            for row in rows.values():
                row['pid'] += 1
        for phase in ('settled', 'completed'):
            for rows in self.args[phase]['writers'].values():
                for row in rows:
                    row['pid'] += 1
        for row in self.args['overlap']['originals'] + self.args['resources']['processes']:
            row['pid'] += 1
        self.source = self.root/'source'
        self.source.mkdir()
        (self.source/'tools').mkdir()
        module = self.source/'module.py'
        module.write_text('# original\n')
        self.save('module-origin-baseline.json', dict(source=str(self.source),
            python_sources={str(module): self.sha(module)}))
        self.bindings = {'batches': []}
        terminal_rows = []
        watcher = dict(close_started=False, queue_closed=False, close_errors=[], remaining_owned_fds=1)
        self.observer = dict(generation_sha256='generation', route_identity='route',
            source_resources=dict(closed=False, watcher=watcher),
            routes=dict(closed=False, failed=False, unattributed_requests=0,
                action_id='action', pending_connections=0,
                slots=[dict(before_activation=0, requests=1) for _ in range(50)]))
        for key, witnesses in self.args['witnesses'].items():
            originals = [dict(row, index=i) for i, row in witnesses.items()]
            ui = self.save(key+'/activation-ui.json', dict(originals=originals))
            terminal = self.save(key+'/activation-terminal.json', {})
            rpc = self.save(key+'/rpc-responses.ndjson', {})
            binding = dict(batch_id=key, action_id='action', activation_ui_path=str(ui))
            self.bindings['batches'].append({'binding': binding})
            slots = []
            result = self.args['metrics'][key]
            for row in result['transcript_bindings']:
                row['sha256'] = 'transcript-hash'
                slots.append(dict(index=row['index'], original=originals[row['index']],
                    transcript=row['original'], transcript_sha256=row['sha256']))
            completion = self.save(key+'/completion.json', dict(action_id='action', failed_rounds=1, slots=slots))
            job = self.save(key+'/job-terminal.json', dict(completion={'observation_path': str(completion)}))
            terminal_rows.append(dict(batch_id=key, terminal_path=str(job)))
            result.update(passed=True, zero_requests_before_activation=True,
                evidence_sha256={str(p): self.sha(p) for p in (ui, terminal, rpc)},
                route=copy.deepcopy(self.observer['routes']))
            self.save(key+'/performance.json', result)
            baseline = copy.deepcopy(self.observer)
            baseline['routes']['action_id'] = None
            for slot in baseline['routes']['slots']:
                slot['requests'] = 0
            self.save('observer-baseline-'+key+'.json', baseline)
            for phase in ('settled', 'completed'):
                self.args[phase]['cohorts'][key] = copy.deepcopy(self.observer)
        for phase in ('settled', 'completed'):
            self.save('observer-bindings-'+phase+'.json', self.args[phase])
        resource = self.args['resources']
        resource['processes'] += [dict(pid=1000+i, birth=[100, i], role='auxiliary') for i in range(21)]
        for row in resource['processes']:
            row.update(rss_bytes=10, fd_count=2)
        resource.update(process_count=521, native_process_count=500, auxiliary_process_count=21,
            retained_ui_count=10, rss_sum_bytes=5210, fd_sum=1042, started_monotonic=1, finished_monotonic=2)
        resource['disk'] = dict(root=str(self.root/'private'), root_identity=[1, 2],
            regular_files=1, logical_bytes=10, allocated_metadata_bytes=4096,
            filesystem_free_bytes=100, allocation_atomic=False, unique_physical_bytes_proven=False)
        self.save('root.json', {'path': resource['disk']['root']})
        self.save('original-process-resources.json', resource)
        self.save('run/run-bindings.json', self.bindings)
        terminal = self.save('terminal.json', dict(batches=terminal_rows))
        self.proof = dict(terminal_path=str(terminal), succeeded=True, run_terminal=True)
        self.save('execution-state.json', dict(lifecycle_completed=True, scoped_cleanup_complete=True,
            cleanup_errors=[], source_unchanged=True, run_verification=self.proof))
        boot = '11111111-1111-4111-8111-111111111111'
        clock = lambda n: dict(boot_id=boot, wall=n, monotonic=n)
        overlap = self.args['overlap']
        overlap.update(kind='standby_original_process_overlap', native_live_checks=True,
            process_overlap_proven=True, run_id='run', boot_id=boot, plan_sha256='plan', bindings=self.bindings,
            overlap_started=clock(2), overlap_finished=clock(3), overlap_seconds=1,
            before=[dict(index=i, started=clock(0), finished=clock(1)) for i in range(500)],
            after=[dict(index=i, started=clock(4), finished=clock(5)) for i in range(500)])
        self.save('overlap/process-overlap.json', overlap)
        self.manifest = Mock(plan=dict(run_id='run', boot_id=boot), sha256='plan')
        self.manifest.resolve.return_value = self.bindings

    def sha(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def save(self, name, value):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def replay(self, performance=None):
        proof = dict(startup_passed=True, recovery_passed=True, evidence_sha256={})
        with patch.object(aggregate, 'RunManifest', return_value=self.manifest), \
                patch.object(aggregate, 'validate_topology'), \
                patch.object(aggregate, 'verify_terminal', return_value=self.proof), \
                patch.object(aggregate, 'verify_performance', side_effect=performance or (lambda *a, **k: proof)):
            return aggregate.verify(self.root, self.root/'run')

    def test_joins_ten_cohorts_and_keeps_full_acceptance_separate(self):
        proof = self.replay()
        self.assertTrue(proof['evidence_replayed'])
        self.assertEqual(proof['identity_join']['original_sessions'], 500)
        self.assertFalse(proof['full_500_acceptance'])

    def test_completion_transcript_substitution_refused(self):
        path = self.root/'9/completion.json'
        value = json.loads(path.read_text())
        value['slots'][49]['transcript'] = '/foreign'
        self.save('9/completion.json', value)
        with self.assertRaisesRegex(ValueError, 'transcript differs'):
            self.replay()

    def test_disk_sample_of_another_root_refused(self):
        self.save('root.json', {'path': '/foreign'})
        with self.assertRaisesRegex(ValueError, 'private root'):
            self.replay()

    def test_original_ui_mutated_in_later_cohort_refused(self):
        calls = 0
        def performance(*a, **kw):
            nonlocal calls
            calls += 1
            if calls == 10:
                self.save('0/activation-ui.json', {})
            return dict(startup_passed=True, recovery_passed=True, evidence_sha256={})
        with self.assertRaisesRegex(ValueError, 'changed during replay'):
            self.replay(performance)

    def test_tested_source_mutated_in_last_cohort_refused(self):
        def performance(*a, **kw):
            (self.source/'module.py').write_text('# changed\n')
            return dict(startup_passed=True, recovery_passed=True, evidence_sha256={})
        with self.assertRaisesRegex(ValueError, 'tested source changed'):
            self.replay(performance)

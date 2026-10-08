import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools.standby_fleet_replay import recheck_dependencies, validate_resources


class AggregateBoundaries(unittest.TestCase):
    def resource(self):
        rows = [dict(pid=i+2, birth=[100, i],
                     role='native' if i < 500 else 'auxiliary',
                     rss_bytes=10, fd_count=2) for i in range(521)]
        return dict(processes=rows, process_count=521, native_process_count=500,
                    auxiliary_process_count=21, retained_ui_count=10,
                    rss_sum_bytes=5210, fd_sum=1042,
                    disk=dict(root='/private/test', root_identity=[1, 2], regular_files=1,
                              logical_bytes=10, allocated_metadata_bytes=4096, filesystem_free_bytes=100,
                              allocation_atomic=False, unique_physical_bytes_proven=False),
                    started_monotonic=1, finished_monotonic=2)

    def test_complete_resource_sample(self):
        validate_resources(self.resource())

    def test_disk_measurement_must_not_claim_unique_physical_usage(self):
        value = self.resource()
        value['disk']['unique_physical_bytes_proven'] = True
        with self.assertRaisesRegex(ValueError, 'disk observation'):
            validate_resources(value)

    def test_auxiliary_generation_must_be_real_integer_pair(self):
        for birth in ([True, 1], [1, 1000000], [0, 0], None):
            with self.subTest(birth=birth):
                value = self.resource()
                value['processes'][-1]['birth'] = birth
                with self.assertRaises(ValueError):
                    validate_resources(value)

    def test_nonfinite_and_boolean_clocks_refused(self):
        for clock in (float('nan'), float('inf'), True, -1):
            value = self.resource()
            value['finished_monotonic'] = clock
            with self.assertRaises(ValueError):
                validate_resources(value)

    def test_earlier_cohort_changed_after_later_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve()/'transcript'
            path.write_bytes(b'original')
            proof = {'evidence_sha256': {str(path): hashlib.sha256(b'original').hexdigest()}}
            self.assertEqual(recheck_dependencies([proof]), proof['evidence_sha256'])
            path.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                recheck_dependencies([proof])

    def test_cross_cohort_hash_conflict(self):
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            recheck_dependencies([{'evidence_sha256': {'/same': 'a'}},
                                  {'evidence_sha256': {'/same': 'b'}}])

    def test_inherited_deadline_applies_to_final_reread(self):
        def expired():
            raise TimeoutError('deadline')
        with self.assertRaises(TimeoutError):
            recheck_dependencies([{'evidence_sha256': {'/missing': 'a'}}], check=expired)

    def test_run_persists_state_before_replay_and_keeps_failure(self):
        from tools.standby_five_workspace import Experiment
        for failure in (False, True):
            with self.subTest(failure=failure):
                experiment = Experiment.__new__(Experiment)
                experiment.report = {}
                experiment.pool = None
                experiment.output = Path('/synthetic/output')
                experiment.plan = Path('/synthetic/run')
                for method in ('setup', 'execute', 'cleanup'):
                    setattr(experiment, method, Mock())
                written = {}
                experiment.write = lambda name, value: written.update({name: dict(value)})
                experiment.acceptance_progress = lambda: experiment.report.update(
                    acceptance_progress={'ready': True}, passed=False, full_500_acceptance=False)
                def replay(*args):
                    self.assertIn('execution-state.json', written)
                    if failure:
                        raise ValueError('original changed')
                    return {'evidence_replayed': True, 'full_500_acceptance': False}
                with patch('tools.standby_fleet_replay.verify', side_effect=replay):
                    result = experiment.run()
                self.assertFalse(result['full_500_acceptance'])
                self.assertEqual('replay_error' in result, failure)
                self.assertEqual('fleet-original-replay.json' in written, not failure)
                self.assertIn('result.json', written)

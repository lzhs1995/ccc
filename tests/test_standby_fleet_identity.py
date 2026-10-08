import copy
import unittest

from tools.standby_fleet_identity import verify


class FleetIdentityTests(unittest.TestCase):
    def setUp(self):
        witnesses, metrics, writers, flat = {}, {}, {}, []
        for cohort in range(10):
            key = str(cohort)
            witnesses[key], writers[key] = {}, []
            bindings, chains = [], []
            for index in range(50):
                n = cohort*50+index
                row = dict(pid=n+1, birth=[100, n], session_id='s'+str(n), surface_id='u'+str(n))
                witnesses[key][index] = row
                flat.append(copy.deepcopy(row))
                path = '/original/'+str(n)+'.jsonl'
                writers[key].append(dict(row, index=index, transcript=path))
                bindings.append(dict(index=index, original=path))
                chains.append(dict(session_id=row['session_id'], surface_id=row['surface_id']))
            metrics[key] = dict(transcript_bindings=bindings, chains=chains)
        self.args = dict(witnesses=witnesses, metrics=metrics,
            settled=dict(phase='settled', writers=copy.deepcopy(writers), cohorts=dict.fromkeys(writers)),
            completed=dict(phase='completed', writers=copy.deepcopy(writers), cohorts=dict.fromkeys(writers)),
            overlap=dict(originals=flat),
            resources=dict(processes=[dict(pid=r['pid'], birth=r['birth'], role='native') for r in flat]))

    def test_same_five_hundred_across_all_observations(self):
        result = verify(**self.args)
        self.assertTrue(result['identity_join_verified'])
        self.assertFalse(result['full_500_acceptance'])

    def test_count_preserving_foreign_writer_overlap_resource_and_chain(self):
        paths = [
            ('settled', 'writers', '0', 0, 'session_id'),
            ('completed', 'writers', '0', 0, 'surface_id'),
            ('overlap', 'originals', 0, 'pid'),
            ('resources', 'processes', 0, 'pid'),
            ('metrics', '0', 'chains', 0, 'session_id'),
            ('metrics', '0', 'transcript_bindings', 0, 'original'),
            ('completed', 'writers', '0', 0, 'transcript'),
        ]
        for path in paths:
            with self.subTest(path=path):
                args = copy.deepcopy(self.args)
                row = args
                for part in path[:-1]:
                    row = row[part]
                row[path[-1]] = 9999 if path[-1] == 'pid' else '/foreign'
                with self.assertRaises(ValueError):
                    verify(**args)

    def test_generation_drift_rejected(self):
        for root, field in [('overlap', 'originals'), ('resources', 'processes')]:
            with self.subTest(root=root):
                args = copy.deepcopy(self.args)
                args[root][field][0]['birth'] = [101, 0]
                with self.assertRaises(ValueError):
                    verify(**args)

    def test_duplicate_and_boolean_indices_rejected(self):
        for index in [1, False]:
            with self.subTest(index=index):
                args = copy.deepcopy(self.args)
                args['metrics']['0']['transcript_bindings'][0]['index'] = index
                with self.assertRaises(ValueError):
                    verify(**args)

    def test_swapped_cohort_rejected(self):
        self.args['metrics']['0'], self.args['metrics']['1'] = self.args['metrics']['1'], self.args['metrics']['0']
        with self.assertRaises(ValueError):
            verify(**self.args)

    def test_deadline_propagates(self):
        def expired():
            raise TimeoutError('expired')
        with self.assertRaises(TimeoutError):
            verify(**self.args, check=expired)

    def test_collector_refusal_does_not_publish_join(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from tools.standby_five_workspace import Experiment
        args = self.args
        args['resources']['processes'][0]['pid'] = 9999
        exp = SimpleNamespace(native_setup=SimpleNamespace(witnesses=args['witnesses']),
            metrics=args['metrics'], write=Mock(), report=dict(
                observer_bindings_settled=args['settled'], observer_bindings_completed=args['completed'],
                process_overlap=args['overlap'], original_process_resources=args['resources']))
        with self.assertRaises(ValueError):
            Experiment.collect_identity_join(exp, check=lambda: None)
        exp.write.assert_not_called()
        self.assertNotIn('fleet_identity_join', exp.report)

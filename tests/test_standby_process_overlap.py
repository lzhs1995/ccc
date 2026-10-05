import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from tools import standby_process_overlap as subject


def uid(n):
    return str(UUID(int=n))


BOOT = uid(9000)


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        self.value += .001
        return dict(boot_id=BOOT, wall=self.value, monotonic=self.value)


def entries():
    return [(dict(pid=i + 1, birth=[100, i], session_id=uid(i + 1),
                  surface_id=uid(i + 1001), writer_identity=[1, i + 1]),
             lambda: None) for i in range(500)]


def plan():
    return dict(version=1, kind='standby_run_plan', run_id=uid(8000),
                boot_id=BOOT, declared=Clock()(), planned_sessions=500,
                workspace_slots={uid(6000 + i): 100 for i in range(5)},
                batches=[dict(batch_id=uid(7000 + i), workspace_id=uid(6000 + i // 2),
                              mode='b', slots=50) for i in range(10)])


class OverlapTests(unittest.TestCase):
    def observe(self, rows=None, check=lambda: None, **kwargs):
        return subject.observe_sweeps(entries() if rows is None else rows, check,
                                      boot=BOOT, clock=kwargs.pop('clock', Clock()), **kwargs)

    def test_common_process_interval_does_not_claim_full_acceptance(self):
        result = self.observe()
        self.assertGreater(result['overlap_seconds'], 0)
        self.assertEqual(len(result['after']), 500)
        self.assertTrue(result['process_overlap_proven'])
        for key in ('continuous_session_ownership_proven',
                    'atomic_configuration_or_writer_snapshot', 'full_500_acceptance',
                    'run_terminal'):
            self.assertFalse(result[key])

    def test_exact_topology(self):
        subject.validate_topology(plan())
        wrong = plan()
        wrong['workspace_slots'][uid(6000)] = 50
        with self.assertRaises(ValueError):
            subject.validate_topology(wrong)

    def test_missing_original(self):
        with self.assertRaises(ValueError):
            self.observe(entries()[:-1])

    def test_duplicates_including_uuid_case(self):
        for key in ('pid', 'session_id', 'surface_id', 'writer_identity'):
            with self.subTest(key=key):
                rows = entries()
                if key in ('session_id', 'surface_id'):
                    rows[0][0][key] = uid(0xabcdef)
                    rows[1][0][key] = uid(0xabcdef).upper()
                else:
                    rows[1][0][key] = copy.deepcopy(rows[0][0][key])
                with self.assertRaises(ValueError):
                    self.observe(rows)

    def test_invalid_birth(self):
        for birth in ([1, 1000000], [True, 0], [1], [0, 2]):
            rows = entries()
            rows[0][0]['birth'] = birth
            with self.assertRaises(ValueError):
                self.observe(rows)

    def test_exit_or_replaced_birth_on_second_sweep(self):
        rows = entries()
        calls = []
        def live():
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('native process no longer matches original birth')
        rows[499] = rows[499][0], live
        with self.assertRaisesRegex(ValueError, 'birth'):
            self.observe(rows)

    def test_mutation_during_live_read(self):
        rows = entries()
        rows[0] = rows[0][0], lambda: rows[0][0].update(pid=800)
        with self.assertRaisesRegex(ValueError, 'mutated during'):
            self.observe(rows)

    def test_binding_drift_between_sweeps(self):
        calls = []
        def current():
            calls.append(1)
            if len(calls) == 2:
                raise ValueError('owner or manifest drift')
        with self.assertRaisesRegex(ValueError, 'drift'):
            self.observe(check=current)

    def test_boot_and_clock_rollback(self):
        for field, value in (('boot_id', uid(1)), ('wall', -1), ('monotonic', -1)):
            clock = Clock()
            calls = []
            def bad_clock():
                result = clock()
                calls.append(1)
                if len(calls) == 2:
                    result[field] = value
                return result
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.observe(clock=bad_clock)

    def test_deadline(self):
        ticks = iter([0, 121])
        with self.assertRaisesRegex(ValueError, 'deadline'):
            self.observe(monotonic=lambda: next(ticks))

    def test_capture_wires_original_client_and_whole_budget(self):
        rows = entries()
        manifest = SimpleNamespace(plan=plan(), sha256='plan-sha', current=lambda: None,
                                   attempts=lambda: ['original-attempts'])
        manifest.plan['boot_id'] = subject.stamp()['boot_id']
        manifest.plan['declared'] = subject.stamp()
        bindings = {'batches': [dict(binding=dict(batch_id=uid(7000+i),
                    config_path='config'+str(i), job_id='job'+str(i),
                    action_id='action', settlement_sha256='settled')) for i in range(10)]}
        manifest.resolve = lambda: copy.deepcopy(bindings)
        clients, constructed = [], []
        def original(directory, batch_id):
            i = int(UUID(batch_id)) - 7000
            return manifest, {}, SimpleNamespace(value={'batch': i}), dict(
                config_path='config'+str(i), job_id='job'+str(i))
        def connect(value):
            client = object()
            clients.append(client)
            return client
        class Observer:
            def __init__(self, config, job, action, *, client):
                self.index = int(job[3:])
                constructed.append(client)
            def _bind(self, index):
                return rows[self.index*50+index][0], {}, {}
            def _live(self, row, claim, hook):
                pass
            def _current(self):
                pass
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(subject, 'RunManifest', return_value=manifest), \
                patch.object(subject, 'original', side_effect=original), \
                patch.object(subject, 'connect', side_effect=connect), \
                patch.object(subject, 'FirstTaskObserver', Observer), \
                patch.object(subject, 'live_settlement', return_value=({'action_id': 'action'}, 'settled')):
            tmp = str(Path(tmp).resolve())
            result = subject.capture(tmp, tmp)
            self.assertTrue(result['process_overlap_proven'])
            self.assertEqual(constructed, clients)
            self.assertEqual(len(clients), 10)
            self.assertTrue((Path(tmp)/'process-overlap.json').exists())
            with self.assertRaises(FileExistsError):
                subject.capture(tmp, tmp)
            with patch.object(subject.time, 'monotonic', side_effect=[0, 121]):
                with self.assertRaisesRegex(ValueError, 'deadline'):
                    subject.capture(tmp, tmp)
            with patch.object(subject, 'original', return_value=(manifest, {},
                    SimpleNamespace(value={}), dict(config_path='foreign', job_id='job0'))):
                with self.assertRaisesRegex(ValueError, 'invocation differs'):
                    subject.capture(tmp, tmp)


if __name__ == '__main__':
    unittest.main()

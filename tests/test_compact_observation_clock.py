"""Observation-only deadlines survive retries without creating control authority."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ccc_compact_watchdog import Worker, read_only_root_clock


def facts():
    return dict(session_id='original', path='/fixture/rollout', file_id=[1, 2], caught_up=True,
                user_offset=10, user_at=90.0, compacted_offset=0, compacted_at=0.0,
                replacement_items=0)


def compact(key='first', started=100.0):
    return dict(key=key, started_at=started, turn_id=key)


class ObservationClockTests(unittest.TestCase):
    def test_reconnect_and_missing_sample_keep_original_start_across_persistence(self):
        current = facts()
        original = read_only_root_clock(None, 'original', current, compact())
        missing = read_only_root_clock(original, 'original', current, None)
        reconnected = read_only_root_clock(copy.deepcopy(missing), 'original', current,
                                          compact('different-turn', 500.0))
        self.assertEqual(reconnected['episode']['key'], original['episode']['key'])
        self.assertEqual(reconnected['episode']['root_started_at'], 100.0)
        self.assertFalse(missing['current_start_observed'])
        self.assertFalse(reconnected['control_verified'])
        self.assertFalse(reconnected['recovery_enabled'])
        self.assertNotIn('attempt', reconnected['episode'])

    def test_native_completion_closes_original_and_rejects_stale_start(self):
        current = facts()
        original = read_only_root_clock(None, 'original', current, compact())
        current.update(compacted_offset=20, compacted_at=400.0, replacement_items=1)
        closed = read_only_root_clock(original, 'original', current, compact())
        self.assertIsNone(closed['episode'])
        self.assertEqual(closed['last_closed']['reason'], 'compacted')
        fresh = read_only_root_clock(closed, 'original', current, compact('next', 500.0))
        self.assertEqual(fresh['episode']['root_started_at'], 500.0)
        self.assertNotEqual(fresh['episode']['key'], original['episode']['key'])

    def test_actual_new_input_separates_incidents(self):
        current = facts()
        original = read_only_root_clock(None, 'original', current, compact())
        current.update(user_offset=30, user_at=350.0)
        closed = read_only_root_clock(original, 'original', current, compact())
        self.assertIsNone(closed['episode'])
        self.assertEqual(closed['last_closed']['reason'], 'new_input')
        fresh = read_only_root_clock(closed, 'original', current, compact('next', 360.0))
        self.assertEqual(fresh['episode']['root_started_at'], 360.0)

    def test_empty_replacement_is_not_proof_of_completion(self):
        current = facts()
        original = read_only_root_clock(None, 'original', current, compact())
        current.update(compacted_offset=20, compacted_at=400.0, replacement_items=0)
        unknown = read_only_root_clock(original, 'original', current, None)
        self.assertEqual(unknown['episode']['root_started_at'], 100.0)

    def test_new_input_after_completion_prevents_rearming_an_old_start(self):
        current = facts()
        original = read_only_root_clock(None, 'original', current, compact())
        current.update(compacted_offset=20, compacted_at=400.0, replacement_items=1,
                       user_offset=30, user_at=450.0)
        closed = read_only_root_clock(original, 'original', current, compact('stale', 420.0))
        self.assertIsNone(closed['episode'])
        self.assertEqual(closed['last_closed']['boundary_at'], 450.0)

    def test_incomplete_changed_or_regressed_facts_preserve_original_state(self):
        current = facts()
        original = read_only_root_clock(None, 'original', current, compact())
        for field, value in (('caught_up', False), ('session_id', 'foreign'),
                             ('file_id', [1, 3]), ('user_offset', 9)):
            changed = dict(current, **{field: value})
            before = copy.deepcopy(original)
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                read_only_root_clock(original, 'original', changed, compact('retry', 500.0))
            self.assertEqual(original, before)

    def test_worker_warns_from_root_clock_once_per_level_without_control(self):
        current = facts()
        with tempfile.TemporaryDirectory() as directory:
            worker = object.__new__(Worker)
            worker.directory = Path(directory).resolve()
            worker.config_path = worker.directory / 'config.json'
            worker.config_path.write_text('{}')
            worker.target = {'session_id': 'original', 'path': current['path'], 'surface_id': 'surface'}
            worker.lock = threading.RLock()
            worker.lib = [None, None]
            worker.db_path = worker.directory / 'unused-log.sqlite'
            worker.facts = type('Facts', (), {'refresh': lambda _: dict(current, offset=10)})()
            notices = []
            worker.notify = lambda *args: notices.append(args)
            with patch('ccc_compact_watchdog.binding', return_value={'process': {'pid': 123}}), \
                    patch('ccc_compact_watchdog.paused', return_value=False), \
                    patch('ccc_compact_watchdog.unsettled_compaction') as start, \
                    patch('ccc_compact_watchdog.time.time') as now:
                start.return_value = compact()
                now.return_value = 100.0
                first = worker.observe_without_control('no control', root_clock=True)
                self.assertEqual(first['root_started_at'], 100.0)
                self.assertEqual(len(notices), 0)
                now.return_value = 280.0
                warning = worker.observe_without_control('no control', root_clock=True)
                self.assertEqual(warning['warning_level'], 180)
                self.assertEqual(len(notices), 1)
                # A missing query sample and a later HTTP/reconnect compact
                # key retain the persisted original start and warning budget.
                start.return_value = None
                now.return_value = 410.0
                late = worker.observe_without_control('no control', root_clock=True)
                self.assertEqual(late['elapsed'], 310.0)
                self.assertEqual(late['handling_status'], 'CAPABILITY_UNAVAILABLE_OBSERVE_ONLY')
                self.assertEqual(len(notices), 2)
                start.return_value = compact('reconnected', 405.0)
                again = worker.observe_without_control('no control', root_clock=True)
                self.assertEqual(again['root_started_at'], 100.0)
                self.assertEqual(len(notices), 2)
                self.assertFalse(again['control_verified'])
                self.assertEqual(again['native_rpc_count'], 0)
                saved = json.loads((worker.directory / 'read-only-root-clock.json').read_text())
                self.assertEqual(saved['episode']['key'], first['compact_key'])
                self.assertFalse((worker.directory / 'state.json').exists())

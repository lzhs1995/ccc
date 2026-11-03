"""Retry/reconnect clocks keep occurrence admission and the original deadline."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ccc_compact_trace import new_request, trace_id
from ccc_compact_watchdog import Worker, make_episode, settled


def snapshot(start=100.0, turn='turn-1', occurrence='first'):
    return dict(binding_ok=True, paused=False,
        turn={'id': turn, 'status': 'inProgress'},
        compact={'key': occurrence, 'turn_id': turn, 'started_at': start,
                 'source': 'native_compaction_item', 'compaction_occurrence': occurrence},
        facts={'caught_up': True, 'path': '/fixture/rollout.jsonl', 'file_id': [1, 2],
               'compacted_offset': 0, 'compacted_at': 0, 'replacement_items': 0,
               'user_offset': 10, 'tool_result_offset': 0},
        input_guard={'token': 'original-input'}, goal=None, goal_revision=None,
        client_pid=123, client_epoch='original-epoch')


class RecordingDeadline:
    def __init__(self, callback, **kwargs):
        self.started = 1000.0
        self.stop = threading.Event()
        self.callback = callback

    def start(self):
        return self


class CompactRootClockTests(unittest.TestCase):
    def worker(self, snap):
        worker = object.__new__(Worker)
        worker.state = {'episode': make_episode(snap), 'closed': [], 'progress_after': None}
        worker.lock = threading.RLock()
        worker.timer = None
        worker.stop = threading.Event()
        worker.native_receipt_pending = lambda ep: False
        worker.snapshot = lambda **kwargs: snap
        worker.handoff_step = lambda current: None
        worker.save = lambda: None
        worker.records = []
        worker.journal = type('Journal', (), {'record': lambda _, event, **fields:
            worker.records.append((event, fields))})()
        worker.target = {'session_id': 'original-session'}
        worker.original_process_known = lambda binding: binding == {'pid': 123}
        return worker

    def arm(self, worker, episode, now):
        with patch('ccc_compact_watchdog.Deadline', RecordingDeadline), \
                patch('ccc_compact_watchdog.time.time', return_value=now):
            Worker.arm(worker, episode)
        return worker.records[-1][1]

    def test_manual_retry_adoption_keeps_total_deadline_and_own_settlement_fence(self):
        original = snapshot()
        worker = self.worker(original)
        ep = worker.state['episode']
        ep['phase'] = 'control_pending'
        request = new_request(worker.target['session_id'])
        raw = json.dumps(request, ensure_ascii=False, separators=(',', ':')).encode()
        intent = {'epoch': 400.0, 'session_id': worker.target['session_id'],
                  'binding': {'pid': 123}, 'input_token': ep['input_token'],
                  'native_request': request, 'native_request_sha256': hashlib.sha256(raw).hexdigest()}
        retry = snapshot(401.0, 'turn-2', 'second')
        retry['compact'].update(source='manual_submission',
            trace_id=trace_id(request['trace']['traceparent']),
            thread_id=worker.target['session_id'], submission_id='turn-2')
        with tempfile.TemporaryDirectory() as directory:
            worker.directory = Path(directory)
            (worker.directory / (ep['key'] + '-compact.json')).write_text(json.dumps(intent))
            with patch('ccc_compact_watchdog.clear_guard', return_value=True), \
                    patch('ccc_compact_watchdog.Deadline', RecordingDeadline):
                self.assertTrue(worker.reconcile_compact(ep, retry))
        adopted = worker.state['episode']
        self.assertEqual((adopted['root'], adopted['root_started_at']), (ep['root'], 100.0))
        self.assertEqual((adopted['started_at'], adopted['attempt']), (400.0, 1))
        deadlines = self.arm(worker, adopted, 410.0)
        self.assertEqual(deadlines['recovery_at'], 400.0)
        self.assertEqual(worker.records[-1][0], 'ROOT_DEADLINE_HANDLED_OBSERVING')
        past = copy.deepcopy(retry)
        past['facts'].update(compacted_offset=20, compacted_at=399.0, replacement_items=1)
        self.assertFalse(settled(adopted, past))
        past['facts']['compacted_at'] = 405.0
        self.assertTrue(settled(adopted, past))

    def test_native_retry_adoption_keeps_original_clock(self):
        original = snapshot()
        worker = self.worker(original)
        ep = worker.state['episode']
        request = {'attemptId': ep['key'], 'expectedTurnId': ep['turn_id'],
                   'compactionOccurrence': ep['compaction_occurrence'], 'clientPid': ep['client_pid']}
        intent = {'client_request': request, 'client_epoch': ep['client_epoch'],
                  'session_id': worker.target['session_id'], 'binding': {'pid': 123},
                  'input_token': ep['input_token'], 'epoch': 400.0}
        retry = snapshot(401.0, 'turn-2', 'second')
        receipts = {'client-claimed': {'status': 'UNKNOWN'},
                    'interrupted': {'turnId': 'turn-1'}, 'compact-started': {'turnId': 'turn-2'}}
        worker.directory = Path('/fixture')
        with patch('ccc_compact_watchdog.read_receipt', side_effect=lambda d, k, phase, r: receipts[phase]), \
                patch('ccc_compact_watchdog.goal_after_join', return_value=ep), \
                patch('ccc_compact_watchdog.clear_guard', return_value=True), \
                patch('ccc_compact_watchdog.Deadline', RecordingDeadline):
            self.assertTrue(worker.reconcile_native_compact(ep, retry, intent))
        adopted = worker.state['episode']
        self.assertEqual((adopted['root_started_at'], adopted['started_at'], adopted['attempt']),
                         (100.0, 401.0, 1))
        self.assertEqual(self.arm(worker, adopted, 410.0)['recovery_at'], 400.0)

    def test_reconnect_preserves_clock_only_without_new_input_or_progress(self):
        for changed in (None, 'input', 'progress', 'identity', 'goal'):
            with self.subTest(changed=changed):
                original = snapshot()
                worker = self.worker(original)
                ep = worker.state['episode']
                ep.update(phase='cancelled', attempt=1)
                current = snapshot(250.0, 'turn-2', 'reconnected')
                if changed == 'input':
                    current['facts']['user_offset'] += 1
                    current['input_guard']['token'] = 'new-input'
                elif changed == 'progress':
                    current['facts']['compacted_offset'] = 20
                elif changed == 'identity':
                    current['client_epoch'] = 'new-epoch'
                elif changed == 'goal':
                    current['goal'] = {'objective': 'changed'}
                worker.snapshot = lambda **kwargs: current
                worker.arm = lambda episode: None
                with patch('ccc_compact_watchdog.clear_guard', return_value=True):
                    worker.tick()
                adopted = worker.state['episode']
                self.assertEqual(adopted['root_started_at'], 100.0 if changed is None else 250.0)
                self.assertEqual(adopted['attempt'], 1 if changed is None else 0)

    def test_original_episode_without_new_field_keeps_existing_deadline(self):
        worker = self.worker(snapshot())
        ep = worker.state['episode']
        del ep['root_started_at']
        self.assertEqual(self.arm(worker, ep, 250.0)['recovery_at'], 400.0)

    def test_expired_adopted_retry_never_creates_a_second_control_request(self):
        worker = self.worker(snapshot())
        ep = worker.state['episode']
        ep.update(attempt=1, started_at=401.0)
        before = copy.deepcopy(worker.state)
        worker.apply = True
        worker.recovery_locks = lambda: self.fail('expired retry attempted control locks')
        with patch('ccc_compact_watchdog.time.time', return_value=410.0):
            worker.recover(ep['key'])
        self.assertEqual(worker.state, before)
        self.assertIsNone(worker.timer)

    def test_pending_original_control_remains_unchanged_after_total_deadline(self):
        worker = self.worker(snapshot())
        ep = worker.state['episode']
        ep.update(phase='control_pending', attempt=0)
        before = copy.deepcopy(worker.state)
        with patch('ccc_compact_watchdog.time.time', return_value=410.0):
            worker.recover(ep['key'])
        self.assertEqual(worker.state, before)


if __name__ == '__main__':
    unittest.main()

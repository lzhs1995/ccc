"""Real private transcripts/receipts; synthetic live owner and process binding."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import standby_completion_evidence as evidence


class CompletionEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.root.chmod(0o700)
        self.saved = dict(job_id='job', action_id='action', cohort_id='cohort',
                          workspace_id='workspace', boot_id='00000000-0000-4000-8000-000000000001', mode='b')
        self.rows, self.paths = [], []
        for i in range(2):
            row = {'index': i, 'session_id': f'session-{i}'}
            self.rows.append(row)
            records = [dict(type='session_meta', payload=dict(id=row['session_id'])),
                       self.event('task_started', 'first'),
                       self.event('task_complete', 'first', error={'message': '429'}),
                       self.event('task_started', 'last'),
                       self.event('task_complete', 'last', last_agent_message='OK')]
            raw = b''.join((json.dumps(r)+'\n').encode() for r in records)
            path = self.root / f'native-{i}.jsonl'
            path.write_bytes(raw); path.chmod(0o600)
            self.paths.append(path)
            prefix = b''.join((json.dumps(r)+'\n').encode() for r in records[:2])
            info = path.stat()
            self.write_first(i, {'original': row, 'action_id': 'action', 'transcript': str(path),
                'transcript_prefix': {'bytes': len(prefix), 'identity': [info.st_dev, info.st_ino],
                                      'sha256': hashlib.sha256(prefix).hexdigest()},
                'confirmation': {'task_id': 'first'}})
        self.live_calls = []
        self.observer = SimpleNamespace(path=self.root/'job.json',
            _bind=lambda index: (self.rows[index], {}, {}),
            _live=lambda row, *_: (self.live_calls.append(row['index']) or self.root, {}),
            _transcript_path=lambda row, *_: self.paths[row['index']], _current=lambda: None)
        self.enter(patch.object(evidence, 'COUNT', 2))
        self.enter(patch.object(evidence, 'FirstTaskObserver', return_value=self.observer))
        self.owner = self.enter(patch.object(evidence, 'live_settlement', return_value=(self.saved, 'hash')))

    def enter(self, context):
        result = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return result

    def event(self, kind, turn, **extra):
        return {'type': 'event_msg', 'payload': {'type': kind, 'turn_id': turn, **extra}}

    def write_first(self, index, value):
        path = self.root / f'standby-first-task-{index}.json'
        path.write_text(json.dumps(value)+'\n'); path.chmod(0o600)

    def capture(self, **kwargs):
        return evidence.capture(self.root/'config.json', 'job', self.root,
            failed_rounds=1, client=object(), clock=lambda: dict(boot_id='00000000-0000-4000-8000-000000000001', wall=10, monotonic=10), **kwargs)

    def rejected(self):
        with self.assertRaises((ValueError, OSError)):
            self.capture()
        self.assertFalse((self.root/'completion-observation.json').exists())

    def prepare_verification(self):
        self.capture()
        directory = self.root/'standby'
        directory.mkdir(mode=0o700)
        path = directory/'activation-ui.json'
        path.write_text(json.dumps({'originals': self.rows})+'\n')
        path.chmod(0o600)
        self.saved['activation_ui_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.enter(patch.object(evidence.batch, 'job_path', return_value=self.root/'job.json'))
        return self.enter(patch.object(evidence, 'read_settlement', return_value=(self.saved, 'hash')))

    def verify(self):
        return evidence.verify_capture(self.root/'config.json', 'job',
            self.root/'completion-observation.json',
            clock=lambda: dict(boot_id=self.saved['boot_id'], wall=11, monotonic=11))

    def test_post_cleanup_verification_reopens_originals_without_live_owner(self):
        self.prepare_verification()
        self.owner.side_effect = ValueError('owner has closed')
        result = self.verify()
        self.assertEqual(result['slots'], 2)
        self.assertFalse(result['job_terminal'])
        self.assertFalse(result['run_terminal'])

    def test_post_cleanup_last_transcript_change_rejected(self):
        self.prepare_verification()
        with self.paths[-1].open('a') as stream:
            stream.write(json.dumps(self.event('task_started', 'late'))+'\n')
        with self.assertRaises(ValueError): self.verify()

    def test_post_cleanup_saved_success_flag_is_not_sufficient(self):
        self.prepare_verification()
        path = self.root/'completion-observation.json'
        record = json.loads(path.read_text())
        record['slots'][-1]['lifecycle']['passed'] = False
        path.write_text(json.dumps(record)+'\n')
        with self.assertRaises(ValueError): self.verify()

    def test_post_cleanup_omitted_last_slot_rejected(self):
        self.prepare_verification()
        path = self.root/'completion-observation.json'
        record = json.loads(path.read_text())
        record['slots'].pop()
        path.write_text(json.dumps(record)+'\n')
        with self.assertRaises(ValueError): self.verify()

    def test_post_cleanup_last_settlement_read_drift_rejected(self):
        reader = self.prepare_verification()
        reader.side_effect = [(self.saved, 'hash'), (self.saved, 'changed')]
        with self.assertRaises(ValueError): self.verify()

    def test_post_cleanup_ui_must_match_settlement_hash(self):
        self.prepare_verification()
        self.saved['activation_ui_sha256'] = '0'*64
        with self.assertRaises(ValueError): self.verify()

    def test_post_cleanup_final_slot_recheck_detects_earlier_slot_drift(self):
        reader = self.prepare_verification()
        calls = []
        def settlement(*_):
            calls.append(True)
            if len(calls) == 2:
                with self.paths[0].open('a') as stream:
                    stream.write('{}\n')
            return self.saved, 'hash'
        reader.side_effect = settlement
        with self.assertRaises(ValueError): self.verify()

    def test_complete_records_original_hashes_without_run_terminal(self):
        value = self.capture()
        self.assertEqual(len(value['slots']), 2)
        self.assertFalse(value['job_terminal'])
        self.assertFalse(value['run_terminal'])
        self.assertEqual(self.live_calls, [0, 0, 1, 1, 0, 1])
        self.assertEqual(value['slots'][0]['transcript_sha256'], hashlib.sha256(self.paths[0].read_bytes()).hexdigest())
        self.assertEqual(json.loads((self.root/'completion-observation.json').read_text()), value)
        with self.assertRaises(ValueError): self.capture()

    def test_last_slot_still_running_never_writes_partial_success(self):
        with self.paths[-1].open('a') as f:
            f.write(json.dumps(self.event('task_started', 'new'))+'\n')
        self.rejected()

    def test_original_first_task_prefix_or_identity_change_rejected(self):
        first = json.loads((self.root/'standby-first-task-0.json').read_text())
        first['transcript_prefix']['sha256'] = '0'*64
        self.write_first(0, first)
        self.rejected()

    def test_first_task_id_must_match_chain(self):
        first = json.loads((self.root/'standby-first-task-0.json').read_text())
        first['confirmation']['task_id'] = 'unrelated'
        self.write_first(0, first)
        self.rejected()

    def test_owner_change_before_persist_rejected(self):
        self.owner.side_effect = [(self.saved, 'hash'), (self.saved, 'changed')]
        self.rejected()

    def test_append_during_final_owner_check_rejected(self):
        def changed(*_):
            if self.owner.call_count == 2:
                with self.paths[0].open('a') as f:
                    f.write(json.dumps(self.event('task_started', 'new'))+'\n')
            return self.saved, 'hash'
        self.owner.side_effect = changed
        self.rejected()

    def test_final_live_identity_rejection_prevents_write(self):
        def live(row, *_):
            self.live_calls.append(row['index'])
            if len(self.live_calls) == 6:
                raise ValueError('PID changed')
            return self.root, {}
        self.observer._live = live
        self.rejected()

    def test_symlink_and_partial_tail_rejected(self):
        other = self.root/'other.jsonl'
        self.paths[0].rename(other); self.paths[0].symlink_to(other)
        self.rejected()
        self.paths[0].unlink(); other.rename(self.paths[0])
        with self.paths[0].open('ab') as f: f.write(b'{')
        self.rejected()

    def test_bounded_read_and_storage_error(self):
        with patch.object(evidence, 'TRANSCRIPT_LIMIT', 1): self.rejected()
        with patch.object(evidence, 'write_once', side_effect=OSError('disk full')): self.rejected()

    def test_malformed_record_is_rejected_without_partial_output(self):
        with self.paths[0].open('a') as stream:
            stream.write('null\n')
        self.rejected()

    def test_wall_clock_jump_rejected_before_persistence(self):
        times = iter([dict(boot_id='00000000-0000-4000-8000-000000000001', wall=10, monotonic=10),
                      dict(boot_id='00000000-0000-4000-8000-000000000001', wall=100, monotonic=11)])
        with self.assertRaises(ValueError):
            evidence.capture(self.root/'config.json', 'job', self.root,
                failed_rounds=1, client=object(), clock=lambda: next(times))
        self.assertFalse((self.root/'completion-observation.json').exists())

    def test_default_client_uses_configured_cmux_path(self):
        client = object()
        with patch.object(evidence.core, 'ConfigStore') as store, patch.object(
                evidence.core, 'CmuxClient', return_value=client) as factory, patch.object(
                evidence, 'FirstTaskObserver', return_value=self.observer) as observer:
            store.return_value.load.return_value = {'cmux_path': '/configured/cmux'}
            evidence.capture(self.root/'config.json', 'job', self.root, failed_rounds=1,
                clock=lambda: dict(boot_id=self.saved['boot_id'], wall=10, monotonic=10))
            factory.assert_called_once_with('/configured/cmux')
            self.assertIs(observer.call_args.kwargs['client'], client)

    def test_cumulative_budget_rejects_second_individually_valid_transcript(self):
        limit = max(p.stat().st_size for p in self.paths)
        with self.assertRaisesRegex(ValueError, 'total transcript budget'):
            self.capture(byte_limit=limit)
        self.assertFalse((self.root/'completion-observation.json').exists())

    def test_exact_cumulative_budget_succeeds(self):
        size = sum(p.stat().st_size for p in self.paths)
        result = self.capture(byte_limit=size)
        self.assertEqual(result['budget']['transcript_bytes'], size)

    def test_slow_initial_owner_lookup_cannot_restart_deadline(self):
        now = [0.0]
        def owner(*_):
            now[0] = 31.0
            return self.saved, 'hash'
        self.owner.side_effect = owner
        with self.assertRaisesRegex(ValueError, 'deadline exceeded'):
            self.capture(monotonic=lambda: now[0])
        self.assertFalse(self.live_calls)
        self.assertFalse((self.root/'completion-observation.json').exists())

    def test_late_final_identity_check_cannot_persist_success(self):
        now, calls = [0.0], []
        def live(*_):
            calls.append(True)
            if len(calls) == 6:
                now[0] = 30.0
            return self.root, {}
        self.observer._live = live
        with self.assertRaisesRegex(ValueError, 'deadline exceeded'):
            self.capture(monotonic=lambda: now[0])
        self.assertFalse((self.root/'completion-observation.json').exists())

    def test_invalid_budget_rejected_before_owner_access(self):
        for kwargs in ({'seconds': float('nan')}, {'seconds': 0},
                       {'byte_limit': True}, {'byte_limit': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.capture(**kwargs)
        self.owner.assert_not_called()


class ProductionBindingTests(unittest.TestCase):
    """Actual observer/settlement and Unix owner; synthetic OS identity, one slot.

    Other 49 first-task receipts come from the settlement fixture. They are not
    observed as completed native tasks by these focused integration tests.
    """
    from tests.test_standby_settlement import SettlementTests as _fixture
    setUp = _fixture.setUp
    start_native = _fixture.start_native
    hook_fixture = _fixture.hook_fixture
    bind = _fixture.bind
    task = _fixture.task
    stamp = _fixture.stamp
    complete = _fixture.complete
    endpoint = _fixture.endpoint

    def prepare_completed_slot(self):
        endpoint, state = self.endpoint()
        first = json.loads((self.worker.path.parent/'standby-first-task-0.json').read_text())
        turn = first['confirmation']['task_id']
        events = [dict(type='task_complete', turn_id=turn, error={'message': '429'}),
                  dict(type='task_started', turn_id='final-turn'),
                  dict(type='task_complete', turn_id='final-turn', last_agent_message='OK')]
        with self.transcript.open('a') as stream:
            for event in events:
                stream.write(json.dumps(dict(type='event_msg', payload=event))+'\n')
        directory = self.worker.path.parent/'completion-test'
        directory.mkdir(mode=0o700)
        return endpoint, directory.resolve(strict=True)

    def capture_original(self, directory):
        import ccc_standby_settlement as settlement
        with patch.object(evidence, 'COUNT', 1), patch.object(
                settlement, 'boot_id', return_value=self.boot):
            return evidence.capture(self.config, self.worker.job['id'], directory,
                failed_rounds=1, client=self.client, clock=lambda: self.stamp(12))

    def test_actual_observer_and_live_owner_bind_completion(self):
        _, directory = self.prepare_completed_slot()
        result = self.capture_original(directory)
        self.assertEqual(result['slots'][0]['original']['session_id'], self.session)
        self.assertTrue(result['slots'][0]['lifecycle']['passed'])
        self.assertFalse(result['run_terminal'])
        self.assertFalse(self.client.sent)

    def test_closed_owner_prevents_completion(self):
        endpoint, directory = self.prepare_completed_slot()
        endpoint.close()
        with self.assertRaises((OSError, ValueError)):
            self.capture_original(directory)
        self.assertFalse((directory/'completion-observation.json').exists())

    def test_actual_observer_rejects_writer_replacement(self):
        _, directory = self.prepare_completed_slot()
        self.lock.rename(self.lock.with_suffix('.old'))
        self.lock.write_bytes(b'')
        with self.assertRaises(ValueError):
            self.capture_original(directory)
        self.assertFalse((directory/'completion-observation.json').exists())


if __name__ == '__main__':
    unittest.main()

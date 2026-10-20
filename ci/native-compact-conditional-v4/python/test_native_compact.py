"""Native compact producer and one-submission regressions; no live inputs."""
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

RUNTIME = Path(os.environ.get('CCC_COMPACT_TEST_RUNTIME', "/Users/lzhs/Documents/cnm/tasks/06_Mintransition/verification/ccc-successor-01a12b57-20261020-53bc-v1/native-compact-integrated-v4/python"))
sys.path.insert(0, str(RUNTIME))
from ccc_client_thread_observation import COMPACT_INTERRUPT_CAPABILITY
from ccc_compact_inputs import PendingInputs
from ccc_compact_intents import IntentIndex
from ccc_compact_join_accounting import goal_after_join
from ccc_compact_native_client import pinned_file
from ccc_compact_runtime import conditional_submit, control_request
from ccc_compact_sensor import RolloutFacts, native_compaction_start
from ccc_compact_support import Journal, OwnedDirectory, validate_native_receipt_fields
from ccc_compact_watchdog import Worker, make_episode


class Fixture:
    def __init__(self, parent):
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.base = Path(self.temporary.name).resolve()
        os.chmod(self.base, 0o700)
        self.sid = str(uuid.uuid4())
        self.key = '0123456789abcdef01234567'
        self.journal = self.base / self.sid
        self.journal.mkdir(mode=0o700)
        self.rollout = self.base / 'rollout.jsonl'
        self.rollout.write_bytes(b'{}\n')
        self.index = IntentIndex(self.base / 'native-input-intents.json',
            state_root=self.base / 'mac-state', active_root=self.base / 'active')
        self.index.save()
        self.request = dict(threadId=self.sid, expectedTurnId='turn-1', attemptId=self.key,
            intentPath=str(self.journal / (self.key + '-interrupt.json')),
            clientPid=os.getpid(), compactionOccurrence=str(uuid.uuid4()))

    def put(self, name, value, *, directory=None):
        directory = directory or self.base
        with OwnedDirectory(directory) as owned:
            owned.atomic_json(name, value)
        return directory / name

    def receipt(self, phase, fields):
        return self.put(self.key + '-' + phase + '.json',
            dict(episode_key=self.key, original_intent=self.request['intentPath'], fields=fields),
            directory=self.journal)

    def close(self):
        self.temporary.cleanup()


class NativeCompactTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parent / 'fixtures'
        parent.mkdir(mode=0o700, exist_ok=True)
        self.f = Fixture(parent)
        self.addCleanup(self.f.close)

    def test_owned_write_once_survives_restart_and_rejects_redirected_ancestor(self):
        with OwnedDirectory(self.f.journal) as owned:
            owned.write_once('request.json', {'budget': 1})
            with self.assertRaises(FileExistsError):
                owned.write_once('request.json', {'budget': 2})
            moved = self.f.base / 'moved'
            self.f.journal.rename(moved)
            os.symlink(moved, self.f.journal)
            with self.assertRaises((ValueError, OSError)):
                owned.write_once('late.json', {})
            self.assertFalse((moved / 'late.json').exists())
        with self.assertRaises(ValueError):
            OwnedDirectory(self.f.journal)
        self.assertEqual(json.loads((moved / 'request.json').read_text()), {'budget': 1})

    def test_shared_input_index_remains_unchanged_until_exact_terminal_receipt(self):
        before = self.f.index.path.read_bytes()
        with self.f.index.lock:
            self.f.index.freeze_for_control(self.f.request)
        self.assertTrue(self.f.index.control_is_pending())
        recreated = IntentIndex(self.f.index.path,
            state_root=self.f.base / 'mac-state', active_root=self.f.base / 'active')
        with self.assertRaisesRegex(RuntimeError, 'input evidence pinned'):
            recreated.observe(self.f.sid, SimpleNamespace(snapshot=lambda: {}))
        self.assertEqual(recreated.path.read_bytes(), before)
        self.f.receipt('client-claimed', {'status': 'UNKNOWN'})
        self.assertTrue(recreated.control_is_pending())
        self.f.receipt('compact-started', {'turnId': 'turn-2', 'joinAccounting': {'status': 'not_accounted'}})
        self.assertFalse(recreated.control_is_pending())
        self.assertEqual(recreated.path.read_bytes(), before)

    def test_denied_receipt_releases_only_the_original_bound_control(self):
        self.f.index.freeze_for_control(self.f.request)
        path = self.f.receipt('not-interrupted', {'turnId': 'foreign', 'attemptId': self.f.key})
        with self.assertRaises(ValueError):
            self.f.index.control_is_pending()
        self.f.put(path.name, {'episode_key': self.f.key,
            'original_intent': self.f.request['intentPath'],
            'fields': {'turnId': 'turn-1', 'attemptId': self.f.key}}, directory=self.f.journal)
        self.assertFalse(self.f.index.control_is_pending())

    def test_pending_input_refresh_cannot_write_a_pinned_local_ledger(self):
        db_path = self.f.base / 'logs.sqlite'
        with sqlite3.connect(db_path):
            pass
        facts = RolloutFacts(self.f.rollout)
        pending = PendingInputs(self.f.journal / 'pending-inputs.json', self.f.sid,
                                facts, db_path, intent_index=self.f.index)
        pending.save()
        before = pending.path.read_bytes()
        self.f.index.freeze_for_control(self.f.request)
        with self.assertRaisesRegex(RuntimeError, 'input evidence pinned'):
            pending.refresh()
        self.assertEqual(pending.path.read_bytes(), before)

    def test_incomplete_terminal_receipt_does_not_release_input_pins(self):
        self.f.index.freeze_for_control(self.f.request)
        self.f.receipt('compact-started', {'turnId': 'turn-2'})
        with self.assertRaisesRegex(ValueError, 'receipt shape'):
            self.f.index.control_is_pending()
        for fields in (
            {'turnId': 'turn-2', 'joinAccounting': {}},
            {'turnId': 'turn-1', 'joinAccounting': {'status': 'not_accounted'}},
            {'turnId': 'turn-2', 'joinAccounting': {'status': 'accounted',
                'turnId': 'turn-1', 'attemptId': 'foreign', 'before': {}, 'after': {}}},
        ):
            with self.assertRaises(ValueError):
                validate_native_receipt_fields(fields, 'compact-started', self.f.request)
        for reason in ('goal_join_accounting_rejected', 'goal_changed',
                       'settings_changed', 'compact_budget_consumed'):
            fields = {'turnId': 'turn-1', 'attemptId': self.f.key, 'reason': reason}
            self.assertEqual(validate_native_receipt_fields(fields, 'compact-denied', self.f.request), fields)
        with self.assertRaises(ValueError):
            validate_native_receipt_fields({'turnId': 'turn-1', 'attemptId': self.f.key,
                'reason': 'unknown_error'}, 'compact-denied', self.f.request)

    def test_atomic_ledger_writes_do_not_follow_fixed_temp_or_destination_links(self):
        outside = self.f.put('outside.json', {'original': True})
        fixed = self.f.index.path.with_suffix('.tmp')
        fixed.symlink_to(outside)
        self.f.index.state['unknown']['new'] = 'pending'
        self.f.index.save()
        self.assertTrue(fixed.is_symlink())
        self.assertEqual(json.loads(outside.read_bytes()), {'original': True})
        self.f.index.path.unlink()
        self.f.index.path.symlink_to(outside)
        self.f.index.state['unknown']['next'] = 'pending'
        with self.assertRaisesRegex(ValueError, 'untrusted journal file'):
            self.f.index.save()
        self.assertTrue(self.f.index.path.is_symlink())
        self.assertEqual(json.loads(outside.read_bytes()), {'original': True})

    def test_worker_cancels_a_replaced_compaction_occurrence_in_the_same_turn(self):
        episode = dict(key=self.f.key, phase='compacting', turn_id='turn-1',
            compact_key='original-occurrence', compaction_occurrence='original-id',
            path=str(self.f.rollout), file_id=[1, 2], compacted_offset=0, started_at=100)
        worker = object.__new__(Worker)
        worker.lock = threading.RLock()
        worker.state = {'episode': episode}
        worker.native_receipt_pending = lambda ep: False
        worker.snapshot = lambda **kwargs: dict(binding_ok=True, paused=False,
            turn={'id': 'turn-1', 'status': 'inProgress'},
            facts={'caught_up': True, 'path': str(self.f.rollout), 'file_id': [1, 2],
                   'compacted_offset': 0, 'compacted_at': 0, 'replacement_items': 0},
            compact={'key': 'new-occurrence', 'compaction_occurrence': 'new-id'})
        worker.handoff_step = lambda snap: None
        worker.unchanged = lambda ep, snap: True
        closed = []
        worker.close_episode = lambda phase, **fields: closed.append((phase, fields))
        worker.tick()
        self.assertEqual([phase for phase, _ in closed], ['cancelled'])

    def test_worker_waits_for_receipt_before_snapshot_or_process_adoption(self):
        self.f.put(self.f.key + '-interrupt.json', {'client_request': self.f.request},
                   directory=self.f.journal)
        worker = object.__new__(Worker)
        worker.directory = self.f.journal
        worker.state = {'episode': dict(key=self.f.key, phase='control_pending', turn_id='turn-1')}
        worker.lock = threading.RLock()
        calls = []
        worker.snapshot = lambda **kwargs: self.fail('pending request refreshed a snapshot')
        worker.observe_without_control = lambda *args, **kwargs: calls.append((args, kwargs))
        worker.tick()
        self.assertEqual(len(calls), 1)
        self.assertEqual(worker.state['episode']['phase'], 'control_pending')

    def test_timed_out_native_request_is_never_replayed_or_sent_through_tui(self):
        operations = []
        request = self.f.request
        epoch = str(uuid.uuid4())
        snapshot = dict(client_epoch=epoch, foreground={'path': 'same', 'sha256': 'same'})
        class RPC:
            def __init__(self, **kwargs): operations.append('connect')
            def request(self, method, params):
                operations.append((method, params));raise TimeoutError('original request unknown')
            def close(self): operations.append('close')
        bridge = SimpleNamespace(require_prompt_free=lambda *args, **kwargs: operations.append('draft-check'),
                                 submit_text_protected=lambda *args: self.fail('RPC timeout fell back to TUI'))
        with OwnedDirectory(self.f.journal) as owned:
            worker = SimpleNamespace(target={'identity': {}, 'process': {}, 'session_id': self.f.sid,
                'surface_id': str(uuid.uuid4())}, lib=(bridge, None, None, None, RPC),
                journal_directory=owned)
            for expected in (TimeoutError, FileExistsError):
                with self.assertRaises(expected):
                    conditional_submit(worker, request, snapshot, lambda: None,
                        lambda: snapshot, lambda fresh: None)
        self.assertEqual(sum(isinstance(x, tuple) for x in operations), 1)
        submit = json.loads((self.f.journal / (self.f.key + '-client-submit.json')).read_text())
        self.assertEqual(submit['status'], 'UNKNOWN')
        self.assertFalse(submit['replay_authorized'])

    def test_occupied_draft_prevents_authority_and_submission(self):
        operations = []
        class RPC:
            def __init__(self, **kwargs): pass
            def close(self): pass
            def request(self, *args): self.fail('draft check bypassed')
        def occupied(*args, **kwargs): raise ValueError('actual draft occupied')
        bridge = SimpleNamespace(require_prompt_free=occupied)
        with OwnedDirectory(self.f.journal) as owned:
            worker = SimpleNamespace(target={'identity': {}, 'process': {}, 'session_id': self.f.sid,
                'surface_id': str(uuid.uuid4())}, lib=(bridge,None,None,None,RPC),journal_directory=owned)
            with self.assertRaisesRegex(ValueError, 'draft occupied'):
                conditional_submit(worker, self.f.request, {'client_epoch': str(uuid.uuid4())},
                    lambda: None, lambda: operations.append('recheck'),
                    lambda fresh: operations.append('authority'))
        self.assertEqual(operations, [])
        self.assertFalse((self.f.journal / (self.f.key + '-client-submit.json')).exists())

    def test_raw_interrupt_and_independent_compact_are_rejected(self):
        rpc = SimpleNamespace(request=lambda *args: self.fail('legacy control reached RPC'))
        for method, params in [('turn/interrupt', {'threadId':self.f.sid,'turnId':'turn-1'}),
                               ('thread/compact/start', {'threadId':self.f.sid})]:
            with self.assertRaises(PermissionError): control_request(rpc, self.f.sid, method, params)

    def test_compact_occurrence_deadline_ignores_http_duplicate_and_old_aba(self):
        sid = self.f.sid
        occurrence = str(uuid.uuid4())
        millis = 1760000000000
        stamp = '2025-10-09T08:53:20Z'
        facts = RolloutFacts(self.f.rollout)
        facts.session_id = sid
        facts.observed_turn = dict(id='turn-1', status='started_without_end_record', started_at=millis/1000)
        payload = dict(type='item_started', thread_id=sid, turn_id='turn-1', started_at_ms=millis,
                       item=dict(type='ContextCompaction', id=occurrence))
        raw = json.dumps(payload).encode()
        facts.observe_compaction_item('item_started', payload, stamp, 3, raw)
        first = dict(facts.compaction_occurrence)
        facts.observe_record({'type':'response_item','payload':{'type':'reasoning'}}, b'reasoning', 5, 10)
        facts.observe_compaction_item('item_started', payload, stamp, 20, raw)
        self.assertEqual(facts.compaction_occurrence, first)
        facts.observe_compaction_item('item_completed', payload, stamp, 30, raw)
        self.assertIsNone(facts.compaction_occurrence)
        with self.assertRaisesRegex(ValueError, 'cannot be rearmed'):
            facts.observe_compaction_item('item_started', payload, stamp, 40, raw)
        payload['item']['id'] = str(uuid.uuid4())
        payload['started_at_ms'] += 500
        facts.observe_compaction_item('item_started', payload, stamp, 50, raw)
        self.assertNotEqual(facts.compaction_occurrence['id'], occurrence)
        self.assertEqual(facts.compaction_occurrence['started_at_ms'], millis+500)

    def test_exact_join_accounting_is_adopted_and_external_writes_are_denied(self):
        before = dict(goal_id='goal-1',created_at_ms=1000123,updated_at_ms=1000123,
                      tokens_used=20,time_used_seconds=10)
        after = dict(before,updated_at_ms=1000456,tokens_used=25,time_used_seconds=310)
        goal = dict(threadId=self.f.sid, objective='original',status='active',tokenBudget=2000,
                    tokensUsed=20,timeUsedSeconds=10,createdAt=1000,updatedAt=1000)
        ep = dict(key=self.f.key,turn_id='turn-1',goal=goal,
            goal_revision={k:before[k] for k in ('goal_id','created_at_ms','updated_at_ms')})
        current = dict(goal,tokensUsed=25,timeUsedSeconds=310)
        snap = dict(goal=current,goal_full=current,
            goal_revision={k:after[k] for k in ('goal_id','created_at_ms','updated_at_ms')})
        receipt = dict(joinAccounting=dict(status='accounted',turnId='turn-1',attemptId=self.f.key,
                                          before=before,after=after))
        adopted = goal_after_join(ep,snap,receipt)
        self.assertEqual(adopted['goal'],current)
        self.assertEqual(ep['goal'],goal)
        for mutate in (lambda s: s['goal'].update(objective='edited'),
                       lambda s: s['goal'].update(status='paused'),
                       lambda s: s['goal'].update(tokenBudget=3000),
                       lambda s: s['goal'].update(tokensUsed=26),
                       lambda s: s['goal_revision'].update(goal_id='goal-2'),
                       lambda s: s['goal_revision'].update(updated_at_ms=1000457)):
            foreign = copy.deepcopy(snap);mutate(foreign)
            with self.assertRaises(ValueError): goal_after_join(ep,foreign,receipt)
        wrong = copy.deepcopy(receipt);wrong['joinAccounting']['attemptId'] = 'foreign'
        with self.assertRaises(ValueError): goal_after_join(ep,snap,wrong)
        with self.assertRaises(ValueError): goal_after_join(ep,snap,{'joinAccounting':{'status':'not_accounted'}})
        unchanged = dict(goal=goal,goal_revision=ep['goal_revision'])
        self.assertEqual(goal_after_join(ep,unchanged,{'joinAccounting':{'status':'not_accounted'}}),ep)


if __name__ == '__main__':
    unittest.main(verbosity=2)

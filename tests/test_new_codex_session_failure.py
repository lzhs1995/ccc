"""A distinct authenticated failure must not inherit a dead session's input."""
import copy
import dataclasses
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import cmux_codex_watch as core
from ccc_delivery import DeliveryStore


ERROR = ('rate limit exceeded: Your requests to gpt-6.1-sol for gpt-6.1-sol '
         'in eastus2 have exceeded token rate limit.')


class NewSessionUnknownTests(unittest.TestCase):
    def setUp(self):
        self.target = dict(surface_id='surface', workspace_id='workspace')
        self.old = dict(kind='goal_blocked', session_id='old-session',
                        turn_id='old-turn', goal_id='old-goal', at=200.0,
                        pid=501, birth=[100, 1000], process_start=100)
        self.turn = dict(kind='task_complete', session_id='new-session',
                         turn_id='new-turn', at=400.0, pid=601,
                         birth=[300, 2000], process_start=300,
                         signature=[10, 800, 11],
                         terminal_tail=dict(offset=600, end=800, sha256='a' * 64),
                         error=dict(message=ERROR))
        self.runtime = core.TargetRuntime(
            delivery_status='unknown', codex_input_phase='goal_native_pending',
            codex_goal_resume=True, codex_goal_proof=copy.deepcopy(self.old),
            codex_sent_turn_key=core.WatchDaemon._goal_turn_key(self.old),
            send_started_at=210.0, send_completed_at=0.0, send_io_started_at=0.0,
            send_attempt_id='old-unknown-attempt', delivery_revision=9,
            last_send_error='original outcome unknown',
            native_failure_at=400.0,
            native_failure_turn_key='new-session:new-turn:400.0',
            observed_at=401.0, observed_state='recoverable_error')
        self.daemon = object.__new__(core.WatchDaemon)
        self.daemon._native_dispatch = None
        self.daemon.config = dict(mode='armed', global_paused=False)
        self.daemon._runtime_lock = threading.Lock()
        self.daemon.runtime = {'surface': self.runtime}
        self.daemon._event_target = lambda _: self.target
        self.daemon.codex_queue_recovery = Mock()
        self.daemon.codex_queue_recovery.current_turn.return_value = self.turn
        self.daemon._current_blocked_goal = Mock(return_value=None)
        self.daemon._record_observation = Mock()
        self.daemon.logger = logging.getLogger(__name__)
        self.birth = self.enterContext(patch('ccc_guard_scope.birth', return_value=None))
        self.kill = self.enterContext(patch.object(core.os, 'kill', side_effect=ProcessLookupError()))
        self.binding = self.enterContext(patch('ccc_shared_codex_turn.binding_for_turn',
            return_value={'process': {'birth': list(self.turn['birth'])}}))
        self.valid = self.enterContext(patch('ccc_shared_codex_turn.valid', return_value=True))

    def reconcile(self, kind='recoverable_error', error='rate_limit'):
        state = core.ScreenState(kind, message_kind='codex', error_type=error)
        return self.daemon._reconcile_codex_delivery('surface', self.runtime, state)

    def eligible(self):
        return self.daemon._new_codex_session_failure(
            self.target, self.runtime, self.turn, 'rate_limit')

    def test_zero_completion_dead_predecessor_yields_without_settling_or_sending(self):
        before = dataclasses.asdict(self.runtime)
        self.assertTrue(self.eligible())
        self.assertFalse(self.reconcile())
        self.assertEqual(dataclasses.asdict(self.runtime), before)
        self.kill.assert_called_with(501, 0)

    def test_completed_predecessor_keeps_existing_path(self):
        self.runtime.send_completed_at = 220.0
        self.assertTrue(self.eligible())
        self.assertFalse(self.reconcile())

    def test_live_original_generation_or_unproved_absence_refuses(self):
        for birth, kill_error in (([100, 1000], ProcessLookupError()),
                                  (None, None), (None, PermissionError()),
                                  (['bad', 0], ProcessLookupError())):
            with self.subTest(birth=birth, kill_error=kill_error):
                self.birth.return_value = birth
                self.kill.side_effect = kill_error
                self.assertFalse(self.eligible())
                self.assertTrue(self.reconcile())
                self.assertEqual(self.runtime.delivery_status, 'unknown')

    def test_recycled_pid_requires_a_distinct_proved_generation(self):
        self.birth.return_value = [350, 9000]
        self.assertTrue(self.eligible())
        self.kill.assert_not_called()

    def test_bad_completion_and_start_clocks_refuse(self):
        for field, values in (
            ('send_completed_at', (-1, float('nan'), float('inf'), '0', True, 310)),
            ('send_started_at', (0, -1, 199, 301, float('nan'), '210', True)),
        ):
            for value in values:
                original = getattr(self.runtime, field)
                with self.subTest(field=field, value=value):
                    setattr(self.runtime, field, value)
                    self.assertFalse(self.eligible())
                setattr(self.runtime, field, original)

    def test_process_session_turn_and_error_mismatch_refuse(self):
        changes = ({'session_id': 'old-session'}, {'session_id': ''},
                   {'pid': 0}, {'pid': True}, {'process_start': 301},
                   {'birth': [300, 1000000]}, {'turn_id': ''},
                   {'at': 299}, {'at': float('nan')}, {'error': None},
                   {'error': {'message': 'unexpected status 502 Bad Gateway'}})
        for change in changes:
            original = copy.deepcopy(self.turn)
            with self.subTest(change=change):
                self.turn.update(change)
                self.assertFalse(self.eligible())
            self.turn.clear()
            self.turn.update(original)

    def test_missing_or_changed_stable_native_eof_refuses(self):
        for change in ({'kind': 'task_started'}, {'signature': None},
                       {'terminal_tail': None},
                       {'terminal_tail': dict(offset=600, end=801, sha256='a' * 64)}):
            original = copy.deepcopy(self.turn)
            with self.subTest(change=change):
                self.turn.update(change)
                self.assertFalse(self.eligible())
            self.turn.clear()
            self.turn.update(original)

    def test_current_binding_and_reread_must_stay_identical(self):
        self.binding.return_value = None
        self.assertFalse(self.eligible())
        self.binding.return_value = {'process': {'birth': [300, 2001]}}
        self.assertFalse(self.eligible())
        self.binding.return_value = {'process': {'birth': [300, 2000]}}
        self.valid.return_value = False
        self.assertFalse(self.eligible())
        self.valid.return_value = True
        self.daemon.codex_queue_recovery.current_turn.return_value = {**self.turn, 'turn_id': 'changed'}
        self.assertFalse(self.eligible())

    def test_changed_original_proof_cannot_retire_unknown(self):
        for field, value in (('goal_id', ''), ('at', 199), ('pid', 1),
                             ('birth', [100, -1]), ('kind', 'task_complete')):
            original = copy.deepcopy(self.runtime.codex_goal_proof)
            with self.subTest(field=field):
                self.runtime.codex_goal_proof[field] = value
                self.assertFalse(self.eligible())
            self.runtime.codex_goal_proof = original

    def test_draft_queue_retained_input_and_other_error_still_block(self):
        for kind in ('composer_busy', 'queued_followup', 'menu'):
            with self.subTest(kind=kind):
                self.assertTrue(self.reconcile(kind))
        self.runtime.codex_input_phase = 'text_retained'
        self.assertTrue(self.reconcile())
        self.runtime.codex_input_phase = 'goal_native_pending'
        self.assertTrue(self.reconcile(error='http_502'))

    def test_distinct_session_hint_wakes_observation_only(self):
        before = dataclasses.asdict(self.runtime)
        self.assertTrue(self.daemon._native_retry_needed('surface', 'workspace', 400))
        self.assertEqual(dataclasses.asdict(self.runtime), before)
        for key in ('old-session:old-turn:400.0', 'new-session:new-turn:399.0', 'bad'):
            with self.subTest(key=key):
                self.runtime.native_failure_turn_key = key
                self.assertFalse(self.daemon._native_retry_needed('surface', 'workspace', 400))

    def test_hint_paused_draft_queue_and_retained_text_never_wake(self):
        for state in ('menu', 'queued_followup', 'composer_busy'):
            with self.subTest(state=state):
                self.runtime.observed_state = state
                self.assertFalse(self.daemon._native_retry_needed('surface', 'workspace', 400))
        self.runtime.observed_state = 'recoverable_error'
        self.runtime.codex_input_phase = 'text_retained'
        self.assertFalse(self.daemon._native_retry_needed('surface', 'workspace', 400))
        self.runtime.codex_input_phase = 'goal_native_pending'
        self.target['paused'] = True
        self.assertFalse(self.daemon._native_retry_needed('surface', 'workspace', 400))
        self.target.pop('paused')
        self.daemon.config['global_paused'] = True
        self.assertFalse(self.daemon._native_retry_needed('surface', 'workspace', 400))

    def test_new_attempt_archives_exact_old_unknown_in_file_and_sqlite(self):
        for database in (False, True):
            with self.subTest(database=database), tempfile.TemporaryDirectory() as directory:
                store = DeliveryStore(directory)
                if database:
                    store.start()
                try:
                    old = copy.deepcopy(self.runtime)
                    store.persist('surface', old)
                    if database:
                        connection = sqlite3.connect(Path(directory) / 'delivery.sqlite3')
                        previous = connection.execute('SELECT record FROM delivery WHERE surface_id=?', ('surface',)).fetchone()[0]
                    else:
                        previous = (Path(directory) / (store.key('surface') + '.json')).read_bytes()
                    replacement = copy.deepcopy(old)
                    replacement.send_attempt_id = 'new-independent-failure'
                    replacement.delivery_status = 'sending'
                    replacement.codex_goal_proof = {}
                    replacement.send_started_at = 410.0
                    store.persist('surface', replacement)
                    if database:
                        archived = connection.execute('SELECT record FROM delivery_history WHERE surface_id=? AND revision=?',
                            ('surface', old.delivery_revision)).fetchone()[0]
                        connection.close()
                    else:
                        archived = (Path(directory) / 'history' / store.key('surface') /
                                    (str(old.delivery_revision) + '.json')).read_bytes()
                    self.assertEqual(archived, previous)
                    record = json.loads(archived)['runtime']
                    self.assertEqual(record['delivery_status'], 'unknown')
                    self.assertEqual(record['send_completed_at'], 0.0)
                    self.assertEqual(record['send_attempt_id'], 'old-unknown-attempt')
                finally:
                    store.close()


if __name__ == '__main__':
    unittest.main()

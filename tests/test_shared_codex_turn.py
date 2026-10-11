import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import ccc_shared_codex_turn as shared
import ccc_codex_goal as goal
from ccc_codex_queue import QueueRecovery


class SharedTurnTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.sessions = self.root / 'sessions'
        self.sessions.mkdir()
        self.sid, self.other = str(uuid.uuid4()), str(uuid.uuid4())
        self.target = {'surface_id': 'surface', 'workspace_id': 'workspace'}
        self.process = dict(pid=41, birth=[100, 4], process_start=100,
                            surface_id='surface', environment_workspace_id='workspace', remote=False)
        self.selection = ('ok', self.sid, ('stable',))
        self.files = {}
        (self.root / 'thread-writer-locks').mkdir()
        for sid in (self.sid, self.other):
            lock = self.root / 'thread-writer-locks' / (sid + '.lock')
            lock.touch()
            self.hold(lock)
        self.path = self.sessions / ('rollout-' + self.sid + '.jsonl')
        self.write_turn('task_complete')
        self.hold(self.path)
        self.db = self.root / 'state_5.sqlite'
        db = sqlite3.connect(self.db)
        db.execute('CREATE TABLE threads(id,model_provider,rollout_path)')
        db.execute('INSERT INTO threads VALUES (?,?,?)', (self.sid, 'selected-provider', str(self.path)))
        db.execute('INSERT INTO threads VALUES (?,?,?)', (self.other, 'other-provider', ''))
        db.commit()
        db.close()
        self.hold(self.db)
        self.patch(shared.scope, 'process', side_effect=lambda pid: dict(self.process) if pid == 41 else None)
        self.patch(shared.scope, 'birth', side_effect=lambda pid, **kw: [100, 4] if pid == 41 else [50, 1])
        self.foreground = self.patch(shared, 'read_foreground', side_effect=lambda *a: self.selection)
        self.patch(shared, 'writer_candidates', return_value=(81,))
        self.connection = self.patch(shared, 'connected_writer', return_value=True)
        self.inventory = self.patch(shared.native, 'process_writable_files',
                                    side_effect=lambda pid, **kw: dict(self.files) if pid == 81 else {})

    def patch(self, obj, name, **kw):
        patcher = patch.object(obj, name, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def hold(self, p):
        s = p.stat()
        self.files[p] = {'device': s.st_dev, 'inode': s.st_ino}

    def write_turn(self, kind):
        self.path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': self.sid}}) + '\n' +
                             json.dumps({'type': 'event_msg', 'timestamp': '2026-10-03T00:00:00Z',
                                         'payload': {'type': kind, 'turn_id': 'turn',
                                                     'error': {'message': 'rate limit exceeded'}}}) + '\n')

    def read(self):
        return shared.current_turn(self.target, 41, self.sessions)

    def test_exact_foreground_among_multiple_locks_and_provider(self):
        turn = self.read()
        self.assertEqual(turn['kind'], 'task_complete')
        self.assertEqual(turn['session_id'], self.sid)
        self.assertEqual(turn['pid'], 41)
        self.assertEqual(turn['shared_writer']['pid'], 81)
        self.assertEqual(goal.provider_for_turn(self.target, turn), 'selected-provider')

    def test_native_working_aborted_user_message_preserved(self):
        for kind in ('task_started', 'turn_aborted', 'user_message'):
            with self.subTest(kind=kind):
                self.write_turn(kind)
                self.assertEqual(self.read()['kind'], kind)

    def test_missing_foreground_never_uses_resume_argv(self):
        self.selection = ('absent', None, None)
        result = self.read()
        self.assertEqual(result['kind'], 'unknown')
        self.assertIn('upgrade required', result['reason'])
        self.inventory.assert_not_called()

    def test_cleared_invalid_and_foreign_selection_reject(self):
        for selection in (('ok', None, ('clear',)), ('invalid', None, None),
                          ('ok', self.other, ('other',))):
            with self.subTest(selection=selection):
                self.selection = selection
                self.assertEqual(self.read()['kind'], 'unknown')

    def test_socket_disconnect_rejects(self):
        self.connection.return_value = False
        self.assertEqual(self.read()['kind'], 'unknown')

    def test_transient_inventory_drift_requires_a_new_complete_snapshot(self):
        rejected = False
        def inventory(pid, **kw):
            nonlocal rejected
            if pid == 81 and not rejected:
                rejected = True
                raise shared.native.VnodeInventoryChanged('concurrent descriptor change')
            return dict(self.files) if pid == 81 else {}
        self.inventory.side_effect = inventory
        turn = self.read()
        self.assertTrue(rejected)
        self.assertEqual(turn['kind'], 'task_complete')
        self.assertEqual(turn['session_id'], self.sid)
        self.assertEqual(turn['shared_writer']['pid'], 81)

    def test_persistent_inventory_drift_stays_unknown_after_three_reads(self):
        calls = []
        def inventory(pid, **kw):
            if pid == 81:
                calls.append(pid)
                raise shared.native.VnodeInventoryChanged('still changing')
            return {}
        self.inventory.side_effect = inventory
        self.assertEqual(self.read()['kind'], 'unknown')
        self.assertEqual(len(calls), 3)

    def test_incomplete_inventory_is_not_retried_or_used(self):
        calls = []
        def inventory(pid, **kw):
            if pid == 81:
                calls.append(pid)
                raise shared.native.IncompleteVnodeRead('short native descriptor read')
            return {}
        self.inventory.side_effect = inventory
        self.assertEqual(self.read()['kind'], 'unknown')
        self.assertEqual(len(calls), 1)

    def test_foreground_change_during_inventory_retry_stays_unknown(self):
        rejected = False
        def inventory(pid, **kw):
            nonlocal rejected
            if pid == 81 and not rejected:
                rejected = True
                self.selection = ('ok', self.other, ('new-foreground',))
                raise shared.native.VnodeInventoryChanged('concurrent descriptor change')
            return dict(self.files) if pid == 81 else {}
        self.inventory.side_effect = inventory
        self.assertEqual(self.read()['kind'], 'unknown')

    def test_lock_change_during_validation_retry_stays_unknown(self):
        proof, reason = shared.binding(self.target, 41, self.sessions)
        self.assertIsNotNone(proof, reason)
        lock = self.root / 'thread-writer-locks' / (self.sid + '.lock')
        rejected = False
        def inventory(pid, **kw):
            nonlocal rejected
            if not rejected:
                rejected = True
                lock.rename(lock.with_suffix('.old'))
                lock.touch()
                self.hold(lock)
                raise shared.native.VnodeInventoryChanged('selected lock changed')
            return dict(self.files)
        self.inventory.side_effect = inventory
        self.assertFalse(shared.valid(self.target, proof))

    def test_selection_change_after_snapshot_rejects(self):
        original = shared.native.task_snapshot
        def read(*args):
            value = original(*args)
            self.selection = ('ok', self.other, ('changed',))
            return value
        self.patch(shared.native, 'task_snapshot', side_effect=read)
        self.assertEqual(self.read()['kind'], 'unknown')

    def test_client_reuse_after_snapshot_rejects(self):
        original = shared.native.task_snapshot
        def read(*args):
            value = original(*args)
            self.process['birth'] = [200, 1]
            return value
        self.patch(shared.native, 'task_snapshot', side_effect=read)
        self.assertEqual(self.read()['kind'], 'unknown')

    def test_lock_replacement_rejects(self):
        lock = self.root / 'thread-writer-locks' / (self.sid + '.lock')
        lock.rename(lock.with_suffix('.old'))
        lock.touch()
        self.assertEqual(self.read()['kind'], 'unknown')

    def test_provider_rechecks_lifecycle(self):
        turn = self.read()
        self.write_turn('task_started')
        self.assertIsNone(goal.provider_for_turn(self.target, turn))

    def test_provider_rechecks_database_identity(self):
        turn = self.read()
        self.db.rename(self.db.with_suffix('.old'))
        self.db.touch()
        self.assertIsNone(goal.provider_for_turn(self.target, turn))

    def test_closed_rollout_uses_exact_writer_database(self):
        del self.files[self.path]
        turn = self.read()
        self.assertEqual(turn['kind'], 'task_complete')
        self.assertEqual(goal.provider_for_turn(self.target, turn), 'selected-provider')

    def test_closed_rollout_requires_held_database(self):
        del self.files[self.path]
        del self.files[self.db]
        self.assertEqual(self.read()['kind'], 'unknown')

    def test_closed_rollout_rejects_foreign_symlink_and_duplicate_mapping(self):
        del self.files[self.path]
        for value in ('relative.jsonl', str(self.root / self.path.name), str(self.sessions / 'other.jsonl')):
            with self.subTest(value=value), closing(sqlite3.connect(self.db)) as db, db:
                db.execute('UPDATE threads SET rollout_path=? WHERE id=?', (value, self.sid))
                db.commit()
                self.assertEqual(self.read()['kind'], 'unknown')
        other = self.path.with_suffix('.original')
        self.path.rename(other)
        self.path.symlink_to(other)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(self.path), self.sid))
        self.assertEqual(self.read()['kind'], 'unknown')
        self.path.unlink()
        other.rename(self.path)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('INSERT INTO threads VALUES (?,?,?)', (self.sid, 'duplicate', str(self.path)))
        self.assertEqual(self.read()['kind'], 'unknown')

    def test_closed_rollout_rechecks_mapping_and_file_after_snapshot(self):
        del self.files[self.path]
        original = shared.native.task_snapshot
        def read(*args):
            result = original(*args)
            with closing(sqlite3.connect(self.db)) as db, db:
                db.execute('UPDATE threads SET rollout_path=? WHERE id=?', ('changed', self.sid))
            return result
        self.patch(shared.native, 'task_snapshot', side_effect=read)
        self.assertEqual(self.read()['kind'], 'unknown')

    def test_closed_rollout_database_error_is_unknown(self):
        del self.files[self.path]
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('DROP TABLE threads')
        self.assertEqual(self.read()['kind'], 'unknown')

    def make_goal(self, writer=81):
        now = int(time.time()) - 1
        tid = str(uuid.uuid4())
        error = 'rate limit exceeded'
        self.path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': self.sid}}) + '\n' +
                             json.dumps({'type': 'event_msg',
                                         'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
                                         'payload': {'type': 'task_complete', 'turn_id': tid,
                                                     'error': {'message': error}}}) + '\n')
        goals, logs = self.root / 'goals_1.sqlite', self.root / 'logs_2.sqlite'
        with closing(sqlite3.connect(goals)) as db, db:
            db.execute('CREATE TABLE thread_goals(thread_id,goal_id,status,updated_at_ms)')
            db.execute('INSERT INTO thread_goals VALUES(?,?,?,?)', (self.sid, 'goal', 'blocked', now * 1000))
        with closing(sqlite3.connect(logs)) as db, db:
            db.execute('CREATE TABLE logs(id,ts,ts_nanos,thread_id,target,feedback_log_body,process_uuid)')
            db.execute('INSERT INTO logs VALUES(?,?,?,?,?,?,?)',
                       (1, now, 0, self.sid, 'codex_core::session::turn',
                        f'turn{{turn.id={tid} model=model}}: Turn error: {error}', f'pid:{writer}:generation'))
        self.hold(logs)
        # The closed goal DB and closed rollout both require the foreground binding.
        self.files.pop(self.path)
        return self.read()

    def test_shared_goal_reads_backend_but_preserves_frontend_input_identity(self):
        turn = self.make_goal()
        proof = goal.blocked_goal(self.target, 41, current_turn=turn)
        self.assertIsNotNone(proof)
        self.assertEqual((proof['pid'], proof['birth'], proof['session_id']),
                         (41, self.process['birth'], self.sid))

    def test_shared_goal_rejects_log_from_frontend_or_different_writer(self):
        turn = self.make_goal(writer=41)
        self.assertIsNone(goal.blocked_goal(self.target, 41, current_turn=turn))

    def test_shared_goal_rejects_new_foreground_and_native_reconnect(self):
        turn = self.make_goal()
        self.selection = ('ok', self.other, ('switched',))
        self.assertIsNone(goal.blocked_goal(self.target, 41, current_turn=turn))
        self.selection = ('ok', self.sid, ('stable',))
        self.write_turn('task_started')
        self.assertIsNone(goal.blocked_goal(self.target, 41, current_turn=turn))

    def test_queue_uses_foreground_before_old_hooks(self):
        queue = QueueRecovery(self.root / 'ledger', self.root / 'hooks', self.sessions, 'continue')
        queue.process_lookup = lambda _: {'agent_kind': 'codex', 'agent_pids': [41]}
        with patch('ccc_client_thread_observation.read_foreground', side_effect=lambda *a: self.selection), \
                patch.object(queue, 'records', side_effect=AssertionError('old hooks must not win')):
            self.assertEqual(queue.current_turn(self.target)['session_id'], self.sid)

    def foreground_queue(self):
        queue = QueueRecovery(self.root / 'ledger', self.root / 'hooks', self.sessions, 'continue')
        self.label = {'agent_kind': 'codex', 'agent_pids': [41]}
        queue.process_lookup = lambda _: self.label
        patcher = patch('ccc_client_thread_observation.read_foreground',
                        side_effect=lambda *a: self.selection)
        self.addCleanup(patcher.stop)
        patcher.start()
        return queue

    def test_shared_foreground_registers_native_failure_wakeup(self):
        queue = self.foreground_queue()
        self.assertEqual(queue.current_turn(self.target)['kind'], 'task_complete')
        with patch.object(shared.native, 'codex_process_starts', return_value={41: 100}):
            sources = queue.wakeup_sources([self.target])
        self.assertEqual(len(sources), 1)
        self.assertEqual((sources[0]['session_id'], sources[0]['path'], sources[0]['pid']),
                         (self.sid, self.path, 41))
        self.assertTrue(sources[0]['identity_current'])

    def test_shared_foreground_survives_process_refresh_gap(self):
        queue = self.foreground_queue()
        original = queue.current_turn(self.target)
        for reason in ('process refresh pending', 'process lookup unavailable'):
            self.label = {'agent_kind': 'unknown', 'summary': reason}
            with patch.object(queue, 'records', side_effect=AssertionError('old hooks must not win')):
                self.assertEqual(queue.current_turn(self.target), original)

    def test_refresh_gap_reads_current_turn_instead_of_cached_failure(self):
        queue = self.foreground_queue()
        queue.current_turn(self.target)
        self.label = {'agent_kind': 'unknown', 'summary': 'process refresh pending'}
        self.write_turn('task_started')
        self.assertEqual(queue.current_turn(self.target)['kind'], 'task_started')

    def test_stalled_goal_uses_authenticated_client_during_inventory_refresh(self):
        from types import SimpleNamespace
        from cmux_codex_watch import WatchDaemon
        self.make_goal()
        queue = self.foreground_queue()
        queue.current_turn(self.target)
        self.label = {'agent_kind': 'unknown', 'summary': 'process refresh pending'}
        daemon = SimpleNamespace(codex_queue_recovery=queue)
        proof = WatchDaemon._current_blocked_goal(daemon, self.target)
        self.assertIsNotNone(proof)
        self.assertEqual(proof['session_id'], self.sid)
        self.write_turn('task_started')
        self.assertIsNone(WatchDaemon._current_blocked_goal(daemon, self.target))

    def test_refresh_gap_revalidates_socket_and_original_client_birth(self):
        queue = self.foreground_queue()
        queue.current_turn(self.target)
        self.label = {'agent_kind': 'unknown', 'summary': 'process refresh pending'}
        self.connection.return_value = False
        self.assertEqual(queue.current_turn(self.target)['kind'], 'unknown')
        self.connection.return_value = True
        self.process['birth'] = [100, 5]
        self.assertEqual(queue.current_turn(self.target)['kind'], 'unknown')

    def test_refresh_gap_cannot_redirect_to_a_different_selected_session(self):
        queue = self.foreground_queue()
        queue.current_turn(self.target)
        self.sid = self.other
        self.path = self.sessions / ('rollout-' + self.sid + '.jsonl')
        self.write_turn('task_complete')
        self.hold(self.path)
        self.selection = ('ok', self.sid, ('switched',))
        self.label = {'agent_kind': 'unknown', 'summary': 'process refresh pending'}
        self.assertEqual(queue.current_turn(self.target)['kind'], 'unknown')
        self.label = {'agent_kind': 'codex', 'agent_pids': [41]}
        self.assertEqual(queue.current_turn(self.target)['session_id'], self.sid)

    def test_fresh_conflicting_process_label_and_cleared_selection_veto_hint(self):
        queue = self.foreground_queue()
        queue.current_turn(self.target)
        for label in ({'agent_kind': 'shell'}, {'agent_kind': 'codex', 'agent_pids': [41, 42]}):
            self.label = label
            with self.subTest(label=label):
                self.assertEqual(queue.current_turn(self.target)['kind'], 'unknown')
        self.label = {'agent_kind': 'unknown', 'summary': 'process refresh pending'}
        for status in ('absent', 'invalid'):
            self.selection = (status, None, None)
            with self.subTest(status=status):
                self.assertEqual(queue.current_turn(self.target)['kind'], 'unknown')

    def test_native_hint_is_scoped_to_its_original_workspace(self):
        queue = self.foreground_queue()
        queue.current_turn(self.target)
        self.label = {'agent_kind': 'unknown', 'summary': 'process refresh pending'}
        self.assertEqual(queue.current_turn({**self.target, 'workspace_id': 'other'})['kind'], 'unknown')


if __name__ == '__main__':
    unittest.main()

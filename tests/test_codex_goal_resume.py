"""Native goal recovery never replaces lost rollout evidence with a fake Stop."""
from contextlib import closing
from pathlib import Path
import sqlite3
import json
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

import ccc_codex_goal as goal
from ccc_codex_queue import task_snapshot
import cmux_codex_watch as core
from ccc_provider_retry import ProviderRetryStore
from tests.test_codex_status_chrome import status_payload, visible_text, ERRORS
from tests.test_watch import FakeClient, armed_daemon, span
from tests.native_goal_fixture import install as install_goal_evidence


class NativeGoalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.sid, self.tid = str(uuid.uuid4()), str(uuid.uuid4())
        self.target = {"surface_id": "surface", "workspace_id": "workspace"}
        self.now = time.time() - 2
        self.process = {"pid": 12345, "birth": [int(self.now) - 100, 3], "surface_id": "surface",
                        "environment_workspace_id": "workspace"}
        lock = root / "thread-writer-locks" / (self.sid + ".lock")
        lock.parent.mkdir()
        lock.touch()
        self.goals, self.logs = root / "goals_1.sqlite", root / "logs_2.sqlite"
        with closing(sqlite3.connect(self.goals)) as db, db:
            db.execute("CREATE TABLE thread_goals(thread_id,goal_id,status,updated_at_ms)")
            db.execute("INSERT INTO thread_goals VALUES(?,?,?,?)", (self.sid, "goal", "blocked", int(self.now * 1000) + 5))
        with closing(sqlite3.connect(self.logs)) as db, db:
            db.execute("CREATE TABLE logs(id,ts,ts_nanos,thread_id,target,feedback_log_body,process_uuid)")
            db.execute("INSERT INTO logs VALUES(?,?,?,?,?,?,?)", (1, int(self.now), int(self.now % 1 * 1e9), self.sid,
                "codex_core::session::turn", f"turn{{turn.id={self.tid} model=model}}:session_task.run:run_turn: Turn error: " + ERRORS["rate_limit"],
                "pid:12345:process-generation"))
        self.files = {p: {"device": p.stat().st_dev, "inode": p.stat().st_ino} for p in (lock, self.goals, self.logs)}
        self.provider_state()
        self.scope = patch.object(goal.scope, "process", return_value=self.process)
        self.inventory = patch.object(goal.native, "process_writable_files", return_value=self.files)
        self.scope.start()
        self.inventory.start()
        self.addCleanup(self.scope.stop)
        self.addCleanup(self.inventory.stop)

    def test_blocked_native_goal_and_matching_error_without_rollout(self):
        result = goal.blocked_goal(self.target, 12345)
        self.assertEqual(result["kind"], "goal_blocked")
        self.assertEqual(result["session_id"], self.sid)
        self.assertEqual(result["turn_id"], self.tid)
        self.assertEqual(result['model_provider'], 'original-provider')

    def test_goal_and_provider_retry_complete_inventory_churn_at_each_boundary(self):
        self.provider_state()
        readers = (lambda: goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()),
                   lambda: goal.provider_for_turn(self.target, self.primary_turn()))
        for reader in readers:
            for boundary in (0, 1):
                with self.subTest(reader=readers.index(reader), boundary=boundary):
                    drift = goal.native.VnodeInventoryChanged('concurrent writer inventory changed')
                    values = ([self.files] if boundary else []) + [drift, drift, self.files]
                    if not boundary:
                        values.append(self.files)
                    with patch.object(goal.native, 'process_writable_files', side_effect=values) as read:
                        self.assertIsNotNone(reader())
                        self.assertEqual(read.call_count, 4)

    def test_goal_and_provider_persistent_inventory_drift_stops_after_three_reads(self):
        self.provider_state()
        for reader in (lambda: goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()),
                       lambda: goal.provider_for_turn(self.target, self.primary_turn())):
            with patch.object(goal.native, 'process_writable_files',
                              side_effect=goal.native.VnodeInventoryChanged('drift')) as read:
                self.assertIsNone(reader())
                self.assertEqual(read.call_count, 3)

    def test_incomplete_inventory_never_borrows_a_retry_or_prior_snapshot(self):
        self.provider_state()
        for reader in (lambda: goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()),
                       lambda: goal.provider_for_turn(self.target, self.primary_turn())):
            for boundary in (0, 1):
                values = ([self.files] if boundary else []) + [goal.native.IncompleteVnodeRead('short read')]
                with patch.object(goal.native, 'process_writable_files', side_effect=values) as read:
                    self.assertIsNone(reader())
                    self.assertEqual(read.call_count, boundary + 1)

    def test_inventory_retry_does_not_accept_replaced_selected_lock(self):
        self.provider_state()
        replacement = {p: dict(identity) for p, identity in self.files.items()}
        lock = next(p for p in replacement if p.suffix == '.lock')
        replacement[lock]['inode'] += 1
        for reader in (lambda: goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()),
                       lambda: goal.provider_for_turn(self.target, self.primary_turn())):
            with patch.object(goal.native, 'process_writable_files', side_effect=[
                    self.files, goal.native.VnodeInventoryChanged('drift'), replacement]):
                self.assertIsNone(reader())

    def primary_turn(self):
        return {'pid': 12345, 'process_start': self.process['birth'][0],
                'session_id': self.sid, 'turn_id': self.tid, 'kind': 'task_complete',
                'at': self.now + .054,
                'error': {'message': ERRORS['rate_limit'], 'codex_error_info': 'rate_limit_exceeded'}}

    def test_day_old_blocked_goal_requires_matching_live_completed_turn(self):
        updated = (int(self.now * 1000) + 5) / 1000
        for age in (86399, 86400, 86401, 7 * 86400):
            with self.subTest(age=age), patch.object(goal.time, 'time', return_value=updated + age):
                legacy = goal.blocked_goal(self.target, 12345)
                self.assertEqual(legacy is not None, age < 86400)
                proof = goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn())
                self.assertIsNotNone(proof)
                self.assertEqual(proof['turn_id'], self.tid)
                self.assertEqual(proof['at'], updated)

    def test_day_old_goal_cannot_borrow_wrong_turn_error_or_process(self):
        with patch.object(goal.time, 'time', return_value=self.now + 2 * 86400):
            for change in ({'session_id': str(uuid.uuid4())}, {'pid': 999},
                           {'process_start': 1}, {'turn_id': str(uuid.uuid4())},
                           {'kind': 'task_started'}, {'error': {'message': 'other'}}):
                with self.subTest(change=change):
                    self.assertIsNone(goal.blocked_goal(self.target, 12345,
                        current_turn={**self.primary_turn(), **change}))

    def test_future_or_nonfinite_goal_age_never_authorizes(self):
        for now in (self.now - 1, float('nan'), float('inf'), float('-inf')):
            for turn in (None, self.primary_turn()):
                with self.subTest(now=now, turn=turn), patch.object(goal.time, 'time', return_value=now):
                    self.assertIsNone(goal.blocked_goal(self.target, 12345, current_turn=turn))

    def test_day_old_goal_still_rejects_later_unmatched_submission(self):
        with patch.object(goal.time, 'time', return_value=self.now + 2 * 86400):
            self.assertIsNotNone(goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()))
            self.append_submission(self.submission(mode='StartOrSteer'))
            self.assertIsNone(goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()))

    def test_day_old_goal_still_rechecks_native_lifecycle(self):
        from types import SimpleNamespace
        turn = self.primary_turn()
        turns = iter([turn, {**turn, 'kind': 'task_started'}])
        queue = SimpleNamespace(process_lookup=lambda _: {'agent_kind': 'codex', 'agent_pids': [12345]},
                                current_turn=lambda _: next(turns))
        with patch.object(goal.time, 'time', return_value=self.now + 2 * 86400):
            self.assertIsNone(core.WatchDaemon._current_blocked_goal(
                SimpleNamespace(codex_queue_recovery=queue), self.target))

    def add_child_lock(self):
        lock = self.goals.parent / 'thread-writer-locks' / (str(uuid.uuid4()) + '.lock')
        lock.touch()
        self.files[lock] = {'device': lock.stat().st_dev, 'inode': lock.stat().st_ino}

    def test_primary_binding_selects_original_lock_among_children(self):
        self.add_child_lock()
        self.assertIsNone(goal.blocked_goal(self.target, 12345))
        proof = goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn())
        self.assertEqual(proof['session_id'], self.sid)

    def test_closed_goal_database_requires_bound_primary_turn(self):
        del self.files[self.goals]
        self.add_child_lock()
        self.assertIsNone(goal.blocked_goal(self.target, 12345))
        self.assertIsNotNone(goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()))

    def test_wrong_primary_generation_turn_and_active_turn_reject(self):
        self.add_child_lock()
        del self.files[self.goals]
        for change in ({'session_id': str(uuid.uuid4())}, {'pid': 999},
                       {'process_start': 1}, {'turn_id': str(uuid.uuid4())},
                       {'kind': 'task_started'}, {'error': {'message': 'other'}}):
            with self.subTest(change=change):
                self.assertIsNone(goal.blocked_goal(self.target, 12345,
                    current_turn={**self.primary_turn(), **change}))

    def test_closed_goal_symlink_rejected(self):
        del self.files[self.goals]
        other = self.goals.with_name('other.sqlite')
        self.goals.rename(other)
        self.goals.symlink_to(other)
        self.assertIsNone(goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()))

    def test_closed_goal_replacement_during_read_rejected(self):
        del self.files[self.goals]
        original = goal.sqlite3.connect
        calls = 0
        def connect(*args, **kwargs):
            nonlocal calls
            if str(args[0]).startswith(self.goals.as_uri() + '?'):
                calls += 1
            if calls == 2:
                calls += 1
                replacement = self.goals.with_name('replacement.sqlite')
                replacement.write_bytes(self.goals.read_bytes())
                replacement.replace(self.goals)
            return original(*args, **kwargs)
        with patch.object(goal.sqlite3, 'connect', side_effect=connect):
            self.assertIsNone(goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()))

    def test_open_logs_must_share_primary_writer_root(self):
        other = self.goals.parent / 'other'
        other.mkdir()
        moved = other / self.logs.name
        self.logs.rename(moved)
        self.files[moved] = self.files.pop(self.logs)
        self.assertIsNone(goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()))

    def test_daemon_accepts_native_error_metadata_but_rejects_lifecycle_change(self):
        from types import SimpleNamespace
        turn = self.primary_turn()
        queue = SimpleNamespace(process_lookup=lambda _: {'agent_kind': 'codex', 'agent_pids': [12345]},
                                current_turn=lambda _: turn)
        daemon = SimpleNamespace(codex_queue_recovery=queue)
        method = core.WatchDaemon._current_blocked_goal
        self.assertIsNotNone(method(daemon, self.target))
        turns = iter([turn, {**turn, 'kind': 'task_started'}])
        queue.current_turn = lambda _: next(turns)
        self.assertIsNone(method(daemon, self.target))

    def test_provider_comes_from_original_open_database_and_writer(self):
        state = self.provider_state()
        turn = {'pid': 12345, 'session_id': self.sid, 'process_start': self.process['birth'][0]}
        self.assertEqual(goal.provider_for_turn(self.target, turn), 'original-provider')
        self.assertIsNone(goal.provider_for_turn(self.target, {**turn, 'session_id': str(uuid.uuid4())}))
        self.assertIsNone(goal.provider_for_turn({**self.target, 'workspace_id': 'other'}, turn))
        with patch.object(goal.scope, 'process', side_effect=[self.process, {**self.process, 'birth': [1, 2]}]):
            self.assertIsNone(goal.provider_for_turn(self.target, turn))
        self.files[state]['inode'] += 1
        self.assertIsNone(goal.provider_for_turn(self.target, turn))

    def provider_state(self):
        state = self.goals.parent / 'state_5.sqlite'
        with closing(sqlite3.connect(state)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS threads(id PRIMARY KEY, model_provider)')
            db.execute('INSERT OR REPLACE INTO threads VALUES(?,?)', (self.sid, 'original-provider'))
        self.files[state] = {'device': state.stat().st_dev, 'inode': state.stat().st_ino}
        return state

    def test_multilock_provider_and_durable_gate_after_cooldown(self):
        from types import SimpleNamespace
        self.provider_state()
        self.add_child_lock()
        turn = self.primary_turn()
        self.assertIsNotNone(goal.blocked_goal(self.target, 12345, current_turn=turn))
        self.assertEqual(goal.provider_for_turn(self.target, turn), 'original-provider')
        clock = [turn["at"] + .1]
        store = ProviderRetryStore(self.goals.parent / 'retry.sqlite', clock=lambda: clock[0], jitter=lambda: 0)
        daemon = SimpleNamespace(codex_queue_recovery=SimpleNamespace(current_turn=lambda _: turn),
            _provider_retry=store, _codex_turn_key_identity=core.WatchDaemon._codex_turn_key_identity)
        runtime = core.TargetRuntime(paused_reason='')
        method = core.WatchDaemon._provider_retry_gate
        state = SimpleNamespace(error_type='rate_limit')
        self.assertFalse(method(daemon, self.target, runtime, state))
        clock[0] += .15
        self.assertTrue(method(daemon, self.target, runtime, state), runtime.paused_reason)

    def test_multilock_missing_primary_rejects_goal_and_provider(self):
        self.provider_state()
        self.add_child_lock()
        for path in list(self.files):
            if path.stem == self.sid:
                del self.files[path]
        self.assertIsNone(goal.provider_for_turn(self.target, self.primary_turn()))
        self.assertIsNone(goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()))

    def test_multilock_provider_rejects_database_replacement(self):
        state = self.provider_state()
        self.add_child_lock()
        original = goal.sqlite3.connect
        calls = 0
        def connect(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                replacement = state.with_name('replacement.sqlite')
                replacement.write_bytes(state.read_bytes())
                replacement.replace(state)
            return original(*args, **kwargs)
        with patch.object(goal.sqlite3, 'connect', side_effect=connect):
            self.assertIsNone(goal.provider_for_turn(self.target, self.primary_turn()))

    def test_multilock_provider_rejects_symlink(self):
        state = self.provider_state()
        self.add_child_lock()
        other = state.with_name('other.sqlite')
        state.rename(other)
        state.symlink_to(other)
        self.assertIsNone(goal.provider_for_turn(self.target, self.primary_turn()))

    def test_provider_missing_database_never_guesses_global_config(self):
        turn = {'pid': 12345, 'session_id': self.sid, 'process_start': self.process['birth'][0]}
        del self.files[self.goals.parent / 'state_5.sqlite']
        self.assertIsNone(goal.provider_for_turn(self.target, turn))
        self.assertIsNone(goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()))

    def test_shared_goal_keeps_provider_without_impersonating_native_turn(self):
        native_turn = {**self.primary_turn(), 'shared_writer': {'pid': 54321}}
        binding = {'writer_pid': 54321, 'writer_birth': [int(self.now) - 101, 0]}
        with closing(sqlite3.connect(self.logs)) as db, db:
            db.execute("UPDATE logs SET process_uuid='pid:54321:writer-generation'")
        def inventory(pid, *, identities):
            self.assertEqual(pid, 54321, 'must not scan the frontend for the shared provider')
            self.assertTrue(identities)
            return self.files
        with patch('ccc_shared_codex_turn.binding_for_turn', return_value=binding) as verify, \
             patch.object(goal.native, 'process_writable_files', side_effect=inventory):
            proof = goal.blocked_goal(self.target, 12345, current_turn=native_turn)
        self.assertIsNotNone(proof)
        self.assertEqual(proof['model_provider'], 'original-provider')
        self.assertNotIn('shared_writer', proof)
        self.assertEqual(verify.call_count, 2)
        self.assertTrue(all(call.args == (self.target, native_turn) for call in verify.call_args_list))
        with patch.object(goal, 'provider_for_turn', side_effect=AssertionError('lost provider identity')):
            identity = core.WatchDaemon._codex_input_identity(self.target, proof, goal=True)
        self.assertEqual(identity['model_provider'], 'original-provider')
        with patch('ccc_shared_codex_turn.binding_for_turn', side_effect=[binding, None]):
            self.assertIsNone(goal.blocked_goal(self.target, 12345, current_turn=native_turn))

    def test_goal_provider_replacement_or_update_during_read_is_rejected(self):
        state = self.provider_state()
        read = goal._read_provider
        for change in ('replace', 'update'):
            with self.subTest(change=change):
                self.provider_state()
                calls = 0
                def changed(database, sid):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        if change == 'replace':
                            replacement = state.with_name('replacement.sqlite')
                            replacement.write_bytes(state.read_bytes())
                            replacement.replace(state)
                        else:
                            with closing(sqlite3.connect(state)) as db, db:
                                db.execute("UPDATE threads SET model_provider='replacement'")
                    return read(database, sid)
                with patch.object(goal, '_read_provider', side_effect=changed):
                    self.assertIsNone(goal.blocked_goal(self.target, 12345, current_turn=self.primary_turn()))

    def retry_daemon(self, turn, store):
        from types import SimpleNamespace
        return SimpleNamespace(codex_queue_recovery=SimpleNamespace(current_turn=lambda _: turn),
            _current_blocked_goal=lambda target: goal.blocked_goal(target, 12345),
            _provider_retry=store, _codex_turn_key_identity=core.WatchDaemon._codex_turn_key_identity)

    def test_none_and_unknown_rollout_use_bound_goal_retry_evidence(self):
        from types import SimpleNamespace
        for index, turn in enumerate((None, {}, {'kind': 'unknown'})):
            with self.subTest(turn=turn):
                store = ProviderRetryStore(self.goals.parent / f'goal-retry-{index}.sqlite',
                    clock=lambda: self.now + 1, jitter=lambda: 0)
                runtime = core.TargetRuntime()
                self.assertTrue(core.WatchDaemon._provider_retry_gate(
                    self.retry_daemon(turn, store), self.target, runtime,
                    SimpleNamespace(error_type='rate_limit'), dry_run=True))
                with store.transaction() as db:
                    self.assertEqual(db.execute('SELECT identity FROM episodes').fetchall(),
                        [(store.identity(self.sid, 'original-provider'),)])

    def test_missing_retry_evidence_is_not_a_broken_ledger(self):
        from types import SimpleNamespace
        turn = {**self.primary_turn(), 'model_provider': 'original-provider'}
        for change in ({'session_id': None}, {'turn_id': None}, {'model_provider': None},
                       {'at': None}, {'at': 'yesterday'}, {'at': True}, {'at': float('nan')}):
            with self.subTest(change=change):
                store = ProviderRetryStore(self.goals.parent / 'missing-evidence.sqlite')
                runtime = core.TargetRuntime()
                with patch.object(goal, 'provider_for_turn', return_value=None):
                    self.assertFalse(core.WatchDaemon._provider_retry_gate(
                        self.retry_daemon({**turn, **change}, store), self.target, runtime,
                        SimpleNamespace(error_type='rate_limit')))
                self.assertEqual(runtime.provider_retry_status['reason_code'], 'provider_retry_evidence_missing')
                self.assertFalse(store.path.exists())

    def test_corrupt_ledger_json_keeps_ledger_failure_classification(self):
        from types import SimpleNamespace
        turn = {**self.primary_turn(), 'model_provider': 'original-provider'}
        store = ProviderRetryStore(self.goals.parent / 'corrupt-ledger.sqlite')
        with store.transaction() as db:
            db.execute('INSERT INTO episodes VALUES (?, ?)', (store.identity(self.sid, 'original-provider'), '{'))
        runtime = core.TargetRuntime()
        self.assertFalse(core.WatchDaemon._provider_retry_gate(
            self.retry_daemon(turn, store), self.target, runtime, SimpleNamespace(error_type='rate_limit')))
        self.assertEqual(runtime.provider_retry_status['reason_code'], 'provider_retry_ledger_unavailable')

    def test_later_submission_active_goal_and_replaced_database_each_veto(self):
        with closing(sqlite3.connect(self.logs)) as db, db:
            db.execute("INSERT INTO logs VALUES(?,?,?,?,?,?,?)", (2, int(self.now) + 1, 0, self.sid,
                "codex_core::session::handlers", "session_loop: Submission sub=new task", "pid:12345:process-generation"))
        self.assertIsNone(goal.blocked_goal(self.target, 12345))
        with closing(sqlite3.connect(self.logs)) as db, db:
            db.execute("DELETE FROM logs WHERE id=2")
        with closing(sqlite3.connect(self.goals)) as db, db:
            db.execute("UPDATE thread_goals SET status='active'")
        self.assertIsNone(goal.blocked_goal(self.target, 12345))
        with closing(sqlite3.connect(self.goals)) as db, db:
            db.execute("UPDATE thread_goals SET status='blocked'")
        self.files[self.goals]["inode"] += 1
        self.assertIsNone(goal.blocked_goal(self.target, 12345))

    def test_wrong_workspace_pid_generation_or_older_goal_cannot_authorize(self):
        self.assertIsNone(goal.blocked_goal({**self.target, "workspace_id": "other"}, 12345))
        with patch.object(goal.scope, "process", side_effect=[self.process, {**self.process, "birth": [999, 1]}]):
            self.assertIsNone(goal.blocked_goal(self.target, 12345))
        with closing(sqlite3.connect(self.goals)) as db, db:
            db.execute("UPDATE thread_goals SET updated_at_ms=updated_at_ms-60000")
        self.assertIsNone(goal.blocked_goal(self.target, 12345))

    def submission(self, *, turn=None, text='任务请继续', mode=None):
        mode = mode or ('Steer { expected_turn_id: ' + json.dumps(turn or self.tid) + ' }')
        return (f'session_loop{{thread_id={self.sid}}}: Submission sub=Submission {{ id: "{uuid.uuid4()}", '
                'op: TurnInput { request: TurnInputRequest { input: UserInput { content: [Text { text: '
                + json.dumps(text, ensure_ascii=False) + ', text_elements: [] }], client_id: Some("client") }, '
                'thread_settings: ThreadSettingsOverrides { model: None }, start: TurnStartOptions { turn_trigger: None }, '
                'additional_context: {}, responsesapi_client_metadata: None, trace: None }, mode: ' + mode + ', '
                'reply: Sender { inner: Some(Inner { state: State { is_complete: false } }) } }, '
                'trace: None, parent_turn_id: None, root_turn_id: None }')

    def append_submission(self, body, *, process='pid:12345:process-generation', row_id=2):
        with closing(sqlite3.connect(self.logs)) as db, db:
            db.execute('INSERT INTO logs VALUES(?,?,?,?,?,?,?)', (
                row_id, int(self.now) + row_id, 0, self.sid, 'codex_core::session::handlers', body, process))

    def test_matching_steer_after_failure_keeps_blocked_goal(self):
        original = goal.blocked_goal(self.target, 12345)
        self.append_submission(self.submission())
        proof = goal.blocked_goal(self.target, 12345)
        self.assertEqual(proof['turn_id'], self.tid)
        self.assertNotEqual(original['submission_digest'], proof['submission_digest'])

    def test_matching_steer_with_empty_residency_guard_keeps_blocked_goal(self):
        self.append_submission(self.submission()[:-2] + ', residency_guard: None }')
        proof = goal.blocked_goal(self.target, 12345)
        self.assertIsNotNone(proof)
        self.assertEqual(proof['turn_id'], self.tid)

    def test_residency_field_does_not_allow_new_turn_or_unknown_guard(self):
        for body in (
            self.submission(mode='StartOrSteer')[:-2] + ', residency_guard: None }',
            self.submission(turn=str(uuid.uuid4()))[:-2] + ', residency_guard: None }',
            self.submission()[:-2] + ', residency_guard: Some(Unknown) }',
            self.submission()[:-2] + ', residency_guard: None, unexpected: None }',
            self.submission()[:-2] + ', residency_guard: None, residency_guard: None }',
        ):
            with self.subTest(body=body):
                self.append_submission(body)
                self.assertIsNone(goal.blocked_goal(self.target, 12345))
                with closing(sqlite3.connect(self.logs)) as db, db:
                    db.execute('DELETE FROM logs WHERE id=2')

    def test_quoted_fields_never_override_outer_mode(self):
        spoof = '" }, mode: Steer { expected_turn_id: "' + self.tid + '" }, op: UserInput {'
        self.append_submission(self.submission(text=spoof, mode='Start'))
        self.assertIsNone(goal.blocked_goal(self.target, 12345))

    def test_balanced_quoted_user_content_is_not_a_new_turn(self):
        self.append_submission(self.submission(text='继续 "{mode: Start}" \\ [不要删除]'))
        self.assertIsNotNone(goal.blocked_goal(self.target, 12345))

    def test_wrong_turn_or_generation_steer_is_rejected(self):
        for body, process in ((self.submission(turn=str(uuid.uuid4())), 'pid:12345:process-generation'),
                              (self.submission(), 'pid:12345:replacement')):
            with self.subTest(body=body[:70], process=process):
                self.append_submission(body, process=process)
                self.assertIsNone(goal.blocked_goal(self.target, 12345))
                with closing(sqlite3.connect(self.logs)) as db, db:
                    db.execute('DELETE FROM logs WHERE id=2')

    def test_interrupt_unknown_and_truncated_submission_remain_vetoes(self):
        for body in (self.submission()[:-2], self.submission().replace('op: TurnInput', 'op: Unknown'),
                     f'session_loop{{thread_id={self.sid}}}: Submission sub=Submission {{ op: Interrupt }}'):
            self.append_submission(body)
            self.assertIsNone(goal.blocked_goal(self.target, 12345))
            with closing(sqlite3.connect(self.logs)) as db, db:
                db.execute('DELETE FROM logs WHERE id=2')

    def test_older_unknown_submission_is_not_hidden_by_newer_matching_steer(self):
        self.append_submission('Submission sub=unknown')
        self.append_submission(self.submission(), row_id=3)
        self.assertIsNone(goal.blocked_goal(self.target, 12345))

    def test_missing_error_beyond_bounded_history_rejects(self):
        for i in range(2, 68):
            self.append_submission(self.submission(), row_id=i)
        self.assertIsNone(goal.blocked_goal(self.target, 12345))

    def test_submission_changed_during_goal_reread_rejects(self):
        original = goal.sqlite3.connect
        calls = 0
        def connect(*args, **kwargs):
            nonlocal calls
            if str(args[0]).startswith(self.goals.as_uri() + '?'):
                calls += 1
            if calls == 2:
                calls += 1
                with closing(original(self.logs)) as db, db:
                    db.execute('INSERT INTO logs VALUES(?,?,?,?,?,?,?)', (
                        2, int(self.now) + 1, 0, self.sid, 'codex_core::session::handlers',
                        self.submission(), 'pid:12345:process-generation'))
            return original(*args, **kwargs)
        with patch.object(goal.sqlite3, 'connect', side_effect=connect):
            self.assertIsNone(goal.blocked_goal(self.target, 12345))


class GoalResumeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # Rate limits use ordinary continuation even with a Goal. The separate
        # Goal-resume route is exercised with a budgeted HTTP502 failure.
        self.goal_error = 'unexpected status 502 Bad Gateway: Unknown error, url: http://127.0.0.1:18765/v1/responses'
        self.payload = status_payload("rate_limit")
        for item in self.payload['render_grid']['row_spans']:
            if ERRORS['rate_limit'] in item['text']:
                item['text'] = item['text'].replace(ERRORS['rate_limit'], self.goal_error)
                item['cell_width'] = len(item['text'])
        row = self.payload["render_grid"]["cursor"]["row"]
        self.payload["render_grid"]["row_spans"].append(span(row + 2, 2, "GPT-6-Astra · Goal stalled (/goal resume)", 1))
        self.client = FakeClient(self.payload, visible_text(self.payload))
        self.client.resume_codex_goal = lambda wid, sid, **kwargs: self.client.send(wid, sid, "/goal resume")
        self.daemon = armed_daemon(self.temp.name, self.client)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.target = self.daemon.config_store.load()['targets'][0]
        self.sid = str(uuid.uuid4())
        self.daemon.codex_queue_recovery.current_turn = lambda _: {"kind": "unknown"}
        self.daemon.codex_queue_recovery.process_lookup = lambda _: {"agent_kind": "codex", "agent_pids": [12345]}
        self.proof = {"session_id": self.sid, "goal_id": "goal", "turn_id": "failed", "at": 200,
                      "kind": "goal_blocked",
                      "pid": 12345, "process_start": 1,
                      "model_provider": "synthetic-provider", "error": {"message": self.goal_error}}
        # A missing blocked-Goal proof cannot establish that the writer has no
        # Goal. Ordinary fallback needs the writer's real, authenticated absent
        # database row; the production reader and its stable rechecks stay live.
        self.goal_evidence = install_goal_evidence(
            self, {**self.proof, 'kind': 'task_complete'})
        self.retry_now = 200.0
        self.daemon._provider_retry = ProviderRetryStore(self.daemon._provider_retry.path,
            clock=lambda: self.retry_now, jitter=lambda: 0)
        evidence = self.daemon._provider_retry.observe(self.sid, 'synthetic-provider', 'failed',
            'http_502', self.goal_error, 200)
        self.retry_due = self.daemon._provider_retry.evaluate(evidence).retry_at
        self.retry_now = self.retry_due + .001

    def test_goal_waits_for_durable_error_delay_before_one_resume(self):
        self.retry_now = 200.0
        with patch.object(goal, 'blocked_goal', return_value=self.proof):
            self.daemon.process_once(self.client)
            self.assertEqual(self.client.sent, [])
            self.retry_now = self.retry_due
            self.daemon.process_once(self.client)
            self.assertEqual([row[-1] for row in self.client.sent], ['/goal resume'])

    def test_only_verified_stalled_goal_uses_native_resume_once(self):
        with patch.object(goal, "blocked_goal", return_value=self.proof):
            self.daemon.process_once(self.client)
            runtime = self.daemon.runtime["surface-uuid"]
            runtime.awaiting = False
            runtime.last_send_at = 0
            self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.client.sent[0][-1], "/goal resume")

    def test_goal_resume_measures_latency_from_matching_native_completion(self):
        self.daemon._native_failed('surface-uuid', 'workspace-uuid', self.sid, 'failed', 199.75)
        with patch.object(goal, 'blocked_goal', return_value=self.proof):
            self.daemon.process_once(self.client)
        runtime = self.daemon.runtime['surface-uuid']
        self.assertEqual([row[-1] for row in self.client.sent], ['/goal resume'])
        self.assertTrue(runtime.native_send_latency_measured)
        self.assertTrue(runtime.native_send_deadline_missed)
        self.assertAlmostEqual(runtime.native_complete_to_send_ms,
                               (runtime.send_io_started_at - 199.75) * 1000, places=2)

    def test_goal_resume_cannot_reuse_another_turns_latency(self):
        self.daemon._native_failed('surface-uuid', 'workspace-uuid', self.sid, 'other-turn', 199.75)
        runtime = self.daemon.runtime['surface-uuid']
        runtime.native_complete_to_send_ms = 5000
        runtime.native_send_deadline_missed = True
        with patch.object(goal, 'blocked_goal', return_value=self.proof):
            self.daemon.process_once(self.client)
        self.assertEqual([row[-1] for row in self.client.sent], ['/goal resume'])
        self.assertFalse(runtime.native_send_latency_measured)
        self.assertEqual(runtime.native_complete_to_send_ms, 0)
        self.assertFalse(runtime.native_send_deadline_missed)

    def test_immediate_observation_does_not_use_an_old_queued_candidate_clock(self):
        runtime = self.daemon.runtime.setdefault('surface-uuid', core.TargetRuntime())
        runtime.candidate_observed_at = 1.0
        with patch.object(goal, 'blocked_goal', return_value=self.proof):
            self.daemon.process_once(self.client)
        self.assertEqual([row[-1] for row in self.client.sent], ['/goal resume'])
        self.assertGreater(runtime.candidate_observed_at, 1.0)
        self.assertLess(runtime.detection_to_send_ms, 5000)

    def test_goal_resumed_during_persistence_cancels_io(self):
        with patch.object(goal, "blocked_goal", side_effect=[self.proof, None]):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

    def test_missing_evidence_or_different_native_error_does_not_resume(self):
        for value in (None, {**self.proof, "error": {"message": "Permission denied"}}):
            with patch.object(goal, "blocked_goal", return_value=value):
                self.daemon.process_once(self.client)
                self.assertEqual(self.client.sent, [])

    def test_unverified_goal_continues_a_matching_native_failed_turn(self):
        self.daemon.codex_queue_recovery.current_turn = lambda _: {
            **self.proof, "kind": "task_complete"}
        with patch.object(goal, "blocked_goal", return_value=None):
            self.daemon.process_once(self.client)
        self.assertEqual([row[-1] for row in self.client.sent], ["任务请继续"])

    def test_unverified_goal_without_native_binding_never_uses_viewport_only(self):
        self.daemon.codex_queue_recovery.current_turn = lambda _: None
        with patch.object(goal, 'blocked_goal', return_value=None):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

    def test_high_demand_goal_footer_recovers_only_new_original_failed_turn(self):
        self.payload = status_payload('high_demand')
        row = self.payload['render_grid']['cursor']['row']
        self.payload['render_grid']['row_spans'].append(
            span(row + 2, 2, 'GPT-6-Astra · Goal stalled (/goal resume)', 1))
        self.client.payload, self.client.text = self.payload, visible_text(self.payload)
        turn = {**self.proof, 'kind': 'task_complete', 'error': {'message': ERRORS['high_demand']}}
        self.daemon.codex_queue_recovery.current_turn = lambda _: dict(turn)
        with patch.object(goal, 'blocked_goal', return_value=None):
            self.daemon.process_once(self.client)
            runtime = self.daemon.runtime['surface-uuid']
            for _ in range(3):
                runtime.awaiting = False
                runtime.last_send_at = 0
                self.daemon.process_once(self.client)
            self.assertEqual([r[-1] for r in self.client.sent], ['任务请继续'])
            turn.update(turn_id='new-failure', at=201)
            self.daemon.process_once(self.client)
        self.assertEqual([r[-1] for r in self.client.sent], ['任务请继续', '任务请继续'])

    def test_already_resumed_goal_cannot_receive_plain_continuation(self):
        self.daemon.codex_queue_recovery.current_turn = lambda _: {
            **self.proof, "kind": "task_complete"}
        with patch.object(goal, "blocked_goal", return_value=self.proof):
            self.daemon.process_once(self.client)
            runtime = self.daemon.runtime["surface-uuid"]
            runtime.awaiting = False
            runtime.last_send_at = 0
            self.daemon.process_once(self.client)
        self.assertEqual([row[-1] for row in self.client.sent], ["/goal resume"])

    def test_inflight_user_echo_keeps_verified_blocked_goal_recoverable(self):
        self.payload['render_grid']['row_spans'].append(span(49, 0, '› 请检查原任务，不要丢失消息', 0))
        self.client.text = visible_text(self.payload)
        with patch.object(goal, 'blocked_goal', return_value=self.proof):
            self.daemon.process_once(self.client)
            runtime = self.daemon.runtime['surface-uuid']
            runtime.awaiting, runtime.last_send_at = False, 0
            self.daemon.process_once(self.client)
        self.assertEqual([r[-1] for r in self.client.sent], ['/goal resume'])

    def test_inflight_status_without_goal_cannot_fall_back_to_old_failed_turn(self):
        self.payload['render_grid']['row_spans'].append(span(49, 0, '• 后续消息已到达', 0))
        self.client.text = visible_text(self.payload)
        self.daemon.codex_queue_recovery.current_turn = lambda _: {**self.proof, 'kind': 'task_complete'}
        with patch.object(goal, 'blocked_goal', return_value=None):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

    def native_notice_turn(self):
        """Real journal ordering from the stalled surface28 incident."""
        path = Path(self.temp.name) / 'original.jsonl'
        completed = {'type': 'event_msg', 'timestamp': '1970-01-01T00:03:20Z',
                     'payload': {'type': 'task_complete', 'turn_id': 'failed',
                                 'error': self.proof['error']}}
        notice = {'type': 'response_item', 'timestamp': '1970-01-01T00:03:19Z',
                  'payload': {'type': 'message', 'role': 'user',
                              'content': [{'type': 'input_text', 'text': '后续消息已到达'}]}}
        path.write_text('\n'.join(json.dumps(row) for row in (
            {'type': 'session_meta', 'payload': {'id': self.sid,
                                                'model_provider': 'synthetic-provider'}},
            {**completed, 'timestamp': '1970-01-01T00:02:00Z',
             'payload': {**completed['payload'], 'turn_id': 'old-goal-turn'}},
            notice, completed)) + '\n')
        self.payload['render_grid']['row_spans'].append(span(49, 0, '› 后续消息已到达', 0))
        self.client.text = visible_text(self.payload)
        self.daemon.codex_queue_recovery.current_turn = lambda _: {
            'session_id': self.sid, 'pid': 12345, 'process_start': 1,
            'model_provider': 'synthetic-provider',
            **(task_snapshot(path, self.sid) or {'kind': 'unknown'})}
        return path, notice

    def test_new_native_completion_after_notices_recovers_despite_stale_goal(self):
        self.native_notice_turn()
        with patch.object(goal, 'blocked_goal', return_value=None):
            self.daemon.process_once(self.client)
            runtime = self.daemon.runtime['surface-uuid']
            runtime.awaiting, runtime.last_send_at = False, 0
            self.daemon.process_once(self.client)
        self.assertEqual([r[-1] for r in self.client.sent], ['任务请继续'])
        self.assertFalse(runtime.codex_goal_resume)

    def test_native_notice_after_failed_completion_cannot_reuse_old_error(self):
        path, notice = self.native_notice_turn()
        with path.open('a') as stream:
            stream.write(json.dumps(notice) + '\n')
        # Even a backwards/equal timestamp cannot override physical append order.
        with patch.object(goal, 'blocked_goal', return_value=None):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

    def test_native_notice_arriving_during_reservation_cancels_continuation(self):
        path, notice = self.native_notice_turn()
        original = self.daemon._save_delivery
        injected = []
        def persist(*args, **kwargs):
            result = original(*args, **kwargs)
            if not injected:
                with path.open('a') as stream:
                    stream.write(json.dumps(notice) + '\n')
                injected.append(True)
            return result
        with patch.object(goal, 'blocked_goal', return_value=None), \
                patch.object(self.daemon, '_save_delivery', side_effect=persist):
            self.daemon.process_once(self.client)
        self.assertEqual(injected, [True])
        self.assertEqual(self.client.sent, [])

    def test_torn_native_input_after_completion_cannot_recover_stale_goal(self):
        path, _ = self.native_notice_turn()
        with path.open('a') as stream:
            stream.write('{"type":"response_item","payload":')
        with patch.object(goal, 'blocked_goal', return_value=None):
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])


if __name__ == "__main__":
    unittest.main()

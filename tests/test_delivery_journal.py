"""Actual SQLite durability and writer failure boundaries for native input."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import multiprocessing
import os
from pathlib import Path
import pickle
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import cmux_codex_watch as core
from ccc_delivery import DeliveryStore, PreInputRecoveryError
from tests.test_watch import FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload
from tests.native_failure_fixture import bind_native_failure


def competing_file_writer(directory, attempt, started, held, release, finished, outcomes):
    store = DeliveryStore(directory)
    runtime = core.TargetRuntime(send_attempt_id=attempt, delivery_revision=1, delivery_status='sending')
    head = store.root / (store.key('surface') + '.json')
    write = store._write_file

    def hold_head(path, data):
        if held is not None and path == head:
            held.set()
            if not release.wait(10):
                raise TimeoutError('competing writer was not released')
        write(path, data)

    store._write_file = hold_head
    started.set()
    try:
        store.persist('surface', runtime)
    except Exception as exc:
        outcomes.put((attempt, type(exc).__name__, str(exc), runtime.delivery_revision))
    else:
        outcomes.put((attempt, 'accepted', '', runtime.delivery_revision))
    finally:
        finished.set()


class DeliveryJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = DeliveryStore(self.directory.name)
        self.store.start()
        self.addCleanup(self.store.close)

    def test_selected_restore_never_loads_unassigned_history_or_its_corrupt_rows(self):
        self.store.persist('assigned', core.TargetRuntime(send_attempt_id='original', delivery_status='unknown'))
        self.store.persist('other', core.TargetRuntime(send_attempt_id='other'))
        database = Path(self.directory.name) / 'delivery.sqlite3'
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("UPDATE delivery SET record=? WHERE surface_id='other'", (b'broken-json',))
        foreign = Path(self.directory.name) / (self.store.key('foreign') + '.json')
        foreign.write_bytes(b'broken-original-file')
        reader, recovered = DeliveryStore(self.directory.name), {}
        reader.restore(recovered, core.TargetRuntime, surface_ids=('assigned',))
        self.assertEqual(set(recovered), {'assigned'})
        self.assertEqual(recovered['assigned'].delivery_status, 'unknown')
        self.assertEqual(recovered['assigned'].send_attempt_id, 'original')
        self.assertEqual(reader.faults, set())
        self.assertIsNone(reader.database_error)
        # Full parent recovery still sees and preserves the unrelated faults.
        parent, history = DeliveryStore(self.directory.name), {}
        parent.restore(history, core.TargetRuntime)
        self.assertTrue(parent.blocked('other'))
        self.assertTrue(parent.blocked('foreign'))
        self.assertEqual(foreign.read_bytes(), b'broken-original-file')

    def test_selected_restore_preserves_legacy_head_and_same_revision_conflict(self):
        legacy = DeliveryStore(self.directory.name)
        runtime = core.TargetRuntime(send_attempt_id='legacy', delivery_status='unknown')
        legacy.persist('selected', runtime)
        reader, recovered = DeliveryStore(self.directory.name), {}
        reader.restore(recovered, core.TargetRuntime, surface_ids=('selected',))
        self.assertEqual(recovered['selected'].send_attempt_id, 'legacy')
        self.assertEqual(recovered['selected'].delivery_status, 'unknown')
        path = legacy.root / (legacy.key('selected') + '.json')
        original = path.read_bytes()
        value = json.loads(original)
        value['runtime']['send_attempt_id'] = 'conflicting-same-revision'
        with closing(sqlite3.connect(legacy.root / 'delivery.sqlite3')) as connection, connection:
            connection.execute('INSERT INTO delivery VALUES (?, ?, ?)',
                               ('selected', runtime.delivery_revision, json.dumps(value).encode()))
        conflicted, rows = DeliveryStore(self.directory.name), {}
        conflicted.restore(rows, core.TargetRuntime, surface_ids=('selected',))
        self.assertTrue(conflicted.blocked('selected'))
        self.assertFalse(conflicted.blocked('unrelated'))
        self.assertEqual(rows['selected'].send_attempt_id, 'legacy')
        self.assertEqual(path.read_bytes(), original)

    def test_selected_corrupt_record_blocks_only_the_assigned_surface(self):
        self.store.persist('assigned', core.TargetRuntime(send_attempt_id='original'))
        with closing(sqlite3.connect(self.store.root / 'delivery.sqlite3')) as connection, connection:
            connection.execute("UPDATE delivery SET record=? WHERE surface_id='assigned'", (b'broken',))
        reader, recovered = DeliveryStore(self.directory.name), {}
        reader.restore(recovered, core.TargetRuntime, surface_ids=('assigned',))
        self.assertEqual(recovered, {})
        self.assertTrue(reader.blocked('assigned'))
        self.assertFalse(reader.blocked('unassigned'))

    def test_committed_intent_and_unknown_ack_survive_a_new_reader(self):
        runtime = core.TargetRuntime(send_attempt_id='original', delivery_status='sending',
                                    codex_sent_turn_key='session:turn:10', send_started_at=11)
        self.store.persist('surface', runtime)
        for state in ('sending', 'unknown'):
            runtime.delivery_status = state
            self.store.persist('surface', runtime)
            recovered = {}
            DeliveryStore(self.directory.name).restore(recovered, core.TargetRuntime)
            self.assertEqual(recovered['surface'].send_attempt_id, 'original')
            self.assertEqual(recovered['surface'].delivery_status, state)
            self.assertEqual(recovered['surface'].codex_sent_turn_key, 'session:turn:10')
            self.assertEqual(recovered['surface'].delivery_revision, runtime.delivery_revision)

    def test_commit_error_prevents_actual_send(self):
        client = FakeClient(grid_payload([], error=HIGH_DEMAND_TEXT), '■ ' + HIGH_DEMAND_TEXT)
        daemon = armed_daemon(self.directory.name, client)
        bind_native_failure(daemon, HIGH_DEMAND_TEXT)
        self.addCleanup(daemon._process_snapshots.close)
        daemon._delivery_store.start()
        self.addCleanup(daemon._delivery_store.close)
        original = daemon._delivery_store._commit_batch

        def reject(connection, batch):
            connection.execute("CREATE TRIGGER IF NOT EXISTS reject_input BEFORE INSERT ON delivery BEGIN SELECT RAISE(ABORT, 'disk rejected transaction'); END")
            connection.commit()
            return original(connection, batch)

        with mock.patch.object(daemon._delivery_store, '_commit_batch', side_effect=reject):
            daemon.process_once(client)
        self.assertEqual(client.sent, [])
        self.assertEqual(daemon.runtime['surface-uuid'].delivery_status, 'failed')
        recovered = {}
        DeliveryStore(daemon._delivery_store.root).restore(recovered, core.TargetRuntime)
        self.assertEqual(recovered, {})

    def test_writer_exit_drains_pending_waiters_and_rejects_late_enqueues(self):
        entered, release = threading.Event(), threading.Event()

        def fail(connection, batch):
            entered.set()
            if not release.wait(2):
                raise AssertionError('test failed to release writer')
            raise RuntimeError('writer lost its connection')

        with ThreadPoolExecutor(2) as pool, mock.patch.object(self.store, '_commit_batch', side_effect=fail):
            first = pool.submit(self.store.persist, 'first', core.TargetRuntime(send_attempt_id='one'))
            self.assertTrue(entered.wait(1))
            second = pool.submit(self.store.persist, 'second', core.TargetRuntime(send_attempt_id='two'))
            release.set()
            for future in (first, second):
                with self.assertRaises(RuntimeError):
                    future.result(1)
        with self.assertRaises(RuntimeError):
            self.store.persist('third', core.TargetRuntime(send_attempt_id='three'))

    def test_close_drains_existing_commit_and_never_falls_back_to_json(self):
        entered, release = threading.Event(), threading.Event()
        original = self.store._commit_batch

        def held(connection, batch):
            entered.set()
            if not release.wait(2):
                raise AssertionError('test failed to release writer')
            return original(connection, batch)

        with ThreadPoolExecutor(2) as pool, mock.patch.object(self.store, '_commit_batch', side_effect=held):
            first = pool.submit(self.store.persist, 'first', core.TargetRuntime(send_attempt_id='one'))
            self.assertTrue(entered.wait(1))
            closer = pool.submit(self.store.close)
            deadline = time.monotonic() + 1
            while self.store._phase == 'running' and time.monotonic() < deadline:
                time.sleep(.001)
            try:
                with self.assertRaises(RuntimeError):
                    self.store.persist('late', core.TargetRuntime(send_attempt_id='two'))
            finally:
                release.set()
            first.result(1)
            closer.result(1)
        self.assertEqual(list(Path(self.directory.name).glob('*.json')), [])
        with self.assertRaises(RuntimeError):
            self.store.start()

    def test_stale_revision_is_rejected_without_blocking_a_different_surface(self):
        original = core.TargetRuntime(send_attempt_id='original')
        self.store.persist('first', original)
        with ThreadPoolExecutor(2) as pool:
            stale = pool.submit(self.store.persist, 'first', core.TargetRuntime(send_attempt_id='stale'))
            valid = pool.submit(self.store.persist, 'second', core.TargetRuntime(send_attempt_id='other'))
            with self.assertRaises(RuntimeError):
                stale.result(1)
            valid.result(1)
        recovered = {}
        DeliveryStore(self.directory.name).restore(recovered, core.TargetRuntime)
        self.assertEqual(recovered['first'].send_attempt_id, 'original')
        self.assertEqual(recovered['second'].send_attempt_id, 'other')

    def test_invalid_row_types_are_isolated_and_never_applied(self):
        self.store.persist('good', core.TargetRuntime(send_attempt_id='good'))
        self.store.persist('bad', core.TargetRuntime(send_attempt_id='bad'))
        database = Path(self.directory.name) / 'delivery.sqlite3'
        with closing(sqlite3.connect(database)) as connection:
            original = json.loads(connection.execute("SELECT record FROM delivery WHERE surface_id='bad'").fetchone()[0])
            for field, value in (('awaiting', 'false'), ('send_count', -1), ('send_started_at', float('nan')),
                                 ('codex_goal_resume', 1), ('codex_private_check', []), ('delivery_status', 'anything')):
                with self.subTest(field=field):
                    damaged = {**original, 'runtime': {**original['runtime'], field: value}}
                    connection.execute("UPDATE delivery SET record=? WHERE surface_id='bad'", (json.dumps(damaged),))
                    connection.commit()
                    reader, recovered = DeliveryStore(self.directory.name), {}
                    reader.restore(recovered, core.TargetRuntime)
                    self.assertTrue(reader.blocked('bad'))
                    self.assertFalse(reader.blocked('good'))
                    self.assertNotIn('bad', recovered)
                    self.assertEqual(recovered['good'].send_attempt_id, 'good')

    def test_corrupt_sqlite_head_blocks_only_its_surface_and_survives_restart(self):
        runtime = core.TargetRuntime(send_attempt_id='original', delivery_status='unknown')
        self.store.persist('bad', runtime)
        database = Path(self.directory.name) / 'delivery.sqlite3'
        damaged = b'{broken-original-json'
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('UPDATE delivery SET record=? WHERE surface_id=?', (damaged, 'bad'))
        runtime.send_attempt_id = 'replacement'
        with self.assertRaises(RuntimeError):
            self.store.persist('bad', runtime)
        self.assertEqual(self.store._phase, 'running')
        self.assertIsNone(self.store.database_error)
        self.assertTrue(self.store.blocked('bad'))
        self.assertFalse(self.store.blocked('good'))
        self.assertEqual(runtime.delivery_revision, 1)
        self.store.persist('good', core.TargetRuntime(send_attempt_id='healthy'))
        with self.assertRaisesRegex(RuntimeError, 'original durable delivery record is unreadable'):
            self.store.persist('bad', runtime)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute('SELECT revision,record FROM delivery WHERE surface_id=?',
                ('bad',)).fetchone(), (1, damaged))
            self.assertEqual(db.execute('SELECT count(*) FROM delivery_history').fetchone()[0], 0)
        reader, recovered = DeliveryStore(self.directory.name), {}
        reader.restore(recovered, core.TargetRuntime)
        self.assertTrue(reader.blocked('bad'))
        self.assertFalse(reader.blocked('good'))
        self.assertNotIn('bad', recovered)
        self.assertEqual(recovered['good'].send_attempt_id, 'healthy')

    def test_sqlite_mixed_batch_preserves_bad_original_and_commits_independent_row(self):
        for damage in ('syntax', 'identity', 'revision', 'runtime', 'clock_overflow'):
            with self.subTest(damage=damage):
                bad, good = 'bad-' + damage, 'good-' + damage
                for sid in (bad, good):
                    self.store.persist(sid, core.TargetRuntime(send_attempt_id='original-' + sid,
                                                              delivery_status='unknown'))
                with closing(sqlite3.connect(Path(self.directory.name) / 'delivery.sqlite3')) as db:
                    originals = {sid: db.execute('SELECT record FROM delivery WHERE surface_id=?',
                        (sid,)).fetchone()[0] for sid in (bad, good)}
                    batch = []
                    for sid in (bad, good):
                        replacement = json.loads(originals[sid])
                        replacement['runtime'].update(delivery_revision=2, send_attempt_id='replacement-' + sid)
                        batch.append((sid, 2, json.dumps(replacement).encode(), None))
                    damaged = json.loads(originals[bad])
                    if damage == 'identity':
                        damaged['surface_id'] = 'different-surface'
                    elif damage == 'revision':
                        damaged['runtime']['delivery_revision'] = 99
                    elif damage == 'runtime':
                        del damaged['runtime']['send_attempt_id']
                    elif damage == 'clock_overflow':
                        damaged['runtime']['send_started_at'] = 10 ** 400
                    damaged_bytes = b'broken-json' if damage == 'syntax' else json.dumps(damaged).encode()
                    db.execute('UPDATE delivery SET record=? WHERE surface_id=?', (damaged_bytes, bad))
                    db.commit()
                    outcomes = self.store._commit_batch(db, batch)
                    self.assertIsInstance(outcomes[0], RuntimeError)
                    self.assertIs(outcomes[1], True)
                    self.assertEqual(db.execute('SELECT revision,record FROM delivery WHERE surface_id=?',
                        (bad,)).fetchone(), (1, damaged_bytes))
                    self.assertEqual(db.execute('SELECT revision,record FROM delivery WHERE surface_id=?',
                        (good,)).fetchone(), (2, batch[1][2]))
                    self.assertEqual(db.execute('SELECT surface_id,revision,record FROM delivery_history WHERE surface_id IN (?,?)',
                        (bad, good)).fetchall(), [(good, 1, originals[good])])
                self.assertTrue(self.store.blocked(bad))
                self.assertFalse(self.store.blocked(good))
                reader, recovered = DeliveryStore(self.directory.name), {}
                reader.restore(recovered, core.TargetRuntime)
                self.assertTrue(reader.blocked(bad))
                self.assertFalse(reader.blocked(good))
                self.assertNotIn(bad, recovered)
                self.assertEqual(recovered[good].send_attempt_id, 'replacement-' + good)

    def test_replacement_preserves_exact_unknown_attempt_and_restores_new_head(self):
        runtime = core.TargetRuntime(send_attempt_id='unknown-goal', delivery_status='unknown',
            codex_sent_turn_key='goal:session:goal:turn:10', codex_input_phase='text_pending',
            last_send_error='original receipt was lost', send_started_at=11)
        self.store.persist('surface', runtime)
        database = Path(self.directory.name) / 'delivery.sqlite3'
        with closing(sqlite3.connect(database)) as db:
            original = db.execute("SELECT revision,record FROM delivery WHERE surface_id='surface'").fetchone()
        runtime.send_attempt_id = 'new-failed-turn'
        runtime.delivery_status = 'sending'
        runtime.codex_sent_turn_key = 'session:later:20'
        self.store.persist('surface', runtime)
        with closing(sqlite3.connect(database)) as db:
            archived = db.execute("SELECT revision,record FROM delivery_history WHERE surface_id='surface'").fetchall()
        self.assertEqual(archived, [original])
        restored = {}
        DeliveryStore(self.directory.name).restore(restored, core.TargetRuntime)
        self.assertEqual(restored['surface'].send_attempt_id, 'new-failed-turn')
        self.assertEqual(json.loads(archived[0][1])['runtime']['delivery_status'], 'unknown')

    def test_same_attempt_and_stale_revision_do_not_create_history(self):
        runtime = core.TargetRuntime(send_attempt_id='original', delivery_status='sending')
        self.store.persist('surface', runtime)
        runtime.delivery_status = 'unknown'
        self.store.persist('surface', runtime)
        with self.assertRaises(RuntimeError):
            self.store.persist('surface', core.TargetRuntime(send_attempt_id='stale'))
        with closing(sqlite3.connect(Path(self.directory.name) / 'delivery.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delivery_history').fetchone()[0], 0)

    def test_archive_failure_rolls_back_replacement_and_preserves_revision(self):
        runtime = core.TargetRuntime(send_attempt_id='original', delivery_status='unknown')
        self.store.persist('surface', runtime)
        revision = runtime.delivery_revision
        database = Path(self.directory.name) / 'delivery.sqlite3'
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("CREATE TRIGGER reject_history BEFORE INSERT ON delivery_history BEGIN SELECT RAISE(ABORT, 'history write failed'); END")
        runtime.send_attempt_id = 'replacement'
        with self.assertRaisesRegex(RuntimeError, 'history write failed'):
            self.store.persist('surface', runtime)
        self.assertEqual(runtime.delivery_revision, revision)
        restored = {}
        DeliveryStore(self.directory.name).restore(restored, core.TargetRuntime)
        self.assertEqual(restored['surface'].send_attempt_id, 'original')
        self.assertEqual(restored['surface'].delivery_status, 'unknown')

    def test_file_mode_archives_original_bytes_before_replacement(self):
        store = DeliveryStore(Path(self.directory.name) / 'file-mode')
        runtime = core.TargetRuntime(send_attempt_id='original', delivery_status='unknown')
        store.persist('surface', runtime)
        original = (store.root / (store.key('surface') + '.json')).read_bytes()
        revision = runtime.delivery_revision
        runtime.send_attempt_id = 'replacement'
        store.persist('surface', runtime)
        archive = store.root / 'history' / store.key('surface') / (str(revision) + '.json')
        self.assertEqual(archive.read_bytes(), original)
        recovered = {}
        DeliveryStore(store.root).restore(recovered, core.TargetRuntime)
        self.assertEqual(recovered['surface'].send_attempt_id, 'replacement')

    def _legacy_file_heads(self, name, *, goal=False):
        legacy = DeliveryStore(Path(self.directory.name) / name)
        runtime, _ = self._pending_input('legacy-original', goal=goal)
        runtime.delivery_status = 'unknown'
        runtime.last_send_error = 'original receipt was lost'
        legacy.persist('surface', runtime)
        path = legacy.root / (legacy.key('surface') + '.json')
        original = (runtime.delivery_revision, path.read_bytes())
        runtime.send_attempt_id = 'legacy-head'
        legacy.persist('surface', runtime)
        return legacy, path, original, (runtime.delivery_revision, path.read_bytes())

    @staticmethod
    def _seed_legacy_sqlite(root, head=None, history=()):
        with closing(sqlite3.connect(root / 'delivery.sqlite3')) as db, db:
            db.execute('CREATE TABLE delivery (surface_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, record BLOB NOT NULL)')
            db.execute('CREATE TABLE delivery_history (surface_id TEXT NOT NULL, revision INTEGER NOT NULL, record BLOB NOT NULL, PRIMARY KEY(surface_id,revision))')
            if head is not None:
                db.execute('INSERT INTO delivery VALUES (?, ?, ?)', ('surface', *head))
            for row in history:
                db.execute('INSERT INTO delivery_history VALUES (?, ?, ?)', ('surface', *row))

    def _pending_after_legacy(self, revision, *, goal=False):
        runtime, proof = self._pending_input('new-native-turn', goal=goal)
        proof['identity'].update(turn_id='next-turn', at=20.0)
        proof['turn_key'] = ('goal:session:original-goal:next-turn:20.0' if goal
                             else 'session:next-turn:20.0')
        runtime.codex_sent_turn_key = runtime.codex_observed_turn_key = proof['turn_key']
        runtime.codex_goal_proof = dict(proof['identity']) if goal else {}
        runtime.delivery_revision, runtime.send_started_at = revision, 21.0
        return runtime, proof

    def test_legacy_file_head_migrates_before_new_native_turn_and_keeps_original_evidence(self):
        for goal in (False, True):
            for mode in ('absent', 'empty', 'behind', 'behind_archived', 'equal_blob', 'equal_text'):
                with self.subTest(goal=goal, mode=mode):
                    legacy, path, original, head = self._legacy_file_heads('migration-' + str(goal) + '-' + mode, goal=goal)
                    sql_head = (original if mode.startswith('behind') else
                                (head[0], head[1].decode()) if mode == 'equal_text' else
                                head if mode == 'equal_blob' else None)
                    if mode != 'absent':
                        self._seed_legacy_sqlite(legacy.root, sql_head,
                            [original] if mode == 'behind_archived' else [])
                    store, recovered = DeliveryStore(legacy.root), {}
                    store.restore(recovered, core.TargetRuntime)
                    self.assertEqual(recovered['surface'].delivery_revision, head[0])
                    self.assertEqual(recovered['surface'].delivery_status, 'unknown')
                    store.start()
                    self.addCleanup(store.close)
                    expected = sql_head if mode.startswith('equal') else head
                    with closing(sqlite3.connect(legacy.root / 'delivery.sqlite3')) as db:
                        self.assertEqual(db.execute("SELECT revision,record FROM delivery WHERE surface_id='surface'").fetchone(), expected)
                    runtime, proof = self._pending_after_legacy(recovered['surface'].delivery_revision, goal=goal)
                    with store.pre_input('surface', runtime, identity=proof['identity']) as pending:
                        pending.persist()
                    self.assertEqual(runtime.delivery_revision, head[0] + 1)
                    with closing(sqlite3.connect(legacy.root / 'delivery.sqlite3')) as db:
                        archived = db.execute("SELECT revision,record FROM delivery_history WHERE surface_id='surface' ORDER BY revision").fetchall()
                        self.assertEqual(archived, ([original] if mode.startswith('behind') else []) + [expected])
                    self.assertEqual(path.read_bytes(), head[1])
                    archive = legacy.root / 'history' / legacy.key('surface') / '1.json'
                    self.assertEqual(archive.read_bytes(), original[1])
                    store.close()
                    restarted, restored = DeliveryStore(legacy.root), {}
                    restarted.restore(restored, core.TargetRuntime)
                    restarted.start()
                    self.addCleanup(restarted.close)
                    self.assertFalse(restarted.blocked('surface'))
                    self.assertEqual(restored['surface'].send_attempt_id, 'new-native-turn')
                    self.assertEqual(restored['surface'].delivery_revision, head[0] + 1)

    def test_legacy_migration_never_replaces_a_newer_sqlite_head(self):
        for with_history in (False, True):
            with self.subTest(with_history=with_history):
                legacy, path, original, head = self._legacy_file_heads('newer-sql-' + str(with_history))
                latest = json.loads(head[1])
                latest['runtime'].update(delivery_revision=3, send_attempt_id='newer-sqlite')
                sql_head = (3, json.dumps(latest).encode())
                self._seed_legacy_sqlite(legacy.root, sql_head, [head] if with_history else [])
                store, recovered = DeliveryStore(legacy.root), {}
                store.restore(recovered, core.TargetRuntime)
                self.assertEqual(recovered['surface'].send_attempt_id, 'newer-sqlite')
                store.start()
                self.addCleanup(store.close)
                with closing(sqlite3.connect(legacy.root / 'delivery.sqlite3')) as db:
                    self.assertEqual(db.execute("SELECT revision,record FROM delivery WHERE surface_id='surface'").fetchone(), sql_head)
                runtime, proof = self._pending_after_legacy(recovered['surface'].delivery_revision)
                with store.pre_input('surface', runtime, identity=proof['identity']) as pending:
                    pending.persist()
                self.assertEqual(runtime.delivery_revision, 4)
                self.assertEqual(path.read_bytes(), head[1])
                with closing(sqlite3.connect(legacy.root / 'delivery.sqlite3')) as db:
                    self.assertEqual(db.execute("SELECT revision,record FROM delivery_history WHERE surface_id='surface' ORDER BY revision").fetchall(),
                                     ([head] if with_history else []) + [sql_head])

    def test_legacy_head_already_archived_remains_idempotent_for_the_next_attempt(self):
        for mode in ('empty', 'behind', 'equal_blob', 'equal_text'):
            for archive_as_text in (False, True):
                with self.subTest(mode=mode, archive_as_text=archive_as_text):
                    legacy, path, original, head = self._legacy_file_heads('archived-head-' + mode + '-' + str(archive_as_text))
                    sql_head = (original if mode == 'behind' else
                                head if mode == 'equal_blob' else
                                (head[0], head[1].decode()) if mode == 'equal_text' else None)
                    archived_head = (head[0], head[1].decode() if archive_as_text else head[1])
                    self._seed_legacy_sqlite(legacy.root, sql_head, [archived_head])
                    store, recovered = DeliveryStore(legacy.root), {}
                    store.restore(recovered, core.TargetRuntime)
                    store.start()
                    self.addCleanup(store.close)
                    runtime, proof = self._pending_after_legacy(recovered['surface'].delivery_revision)
                    with store.pre_input('surface', runtime, identity=proof['identity']) as pending:
                        pending.persist()
                    self.assertEqual(runtime.delivery_revision, head[0] + 1)
                    self.assertFalse(store.blocked('surface'))
                    with closing(sqlite3.connect(legacy.root / 'delivery.sqlite3')) as db:
                        self.assertEqual(db.execute("SELECT revision,record FROM delivery_history WHERE surface_id='surface' ORDER BY revision").fetchall(),
                                         ([original] if mode == 'behind' else []) + [archived_head])
                    self.assertEqual(path.read_bytes(), head[1])
                    self.assertEqual((legacy.root / 'history' / legacy.key('surface') / '1.json').read_bytes(), original[1])
                    store.close()

    def test_conflicting_existing_archive_rejects_only_its_surface_in_the_same_batch(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                bad, good = 'archive-bad-' + str(reverse), 'archive-good-' + str(reverse)
                for sid in (bad, good):
                    self.store.persist(sid, core.TargetRuntime(send_attempt_id='original-' + sid, delivery_status='unknown'))
                with closing(sqlite3.connect(Path(self.directory.name) / 'delivery.sqlite3')) as db:
                    originals = {sid: db.execute('SELECT revision,record FROM delivery WHERE surface_id=?',
                        (sid,)).fetchone() for sid in (bad, good)}
                    damaged = originals[bad][1] + b' '
                    db.execute('INSERT INTO delivery_history VALUES (?, ?, ?)', (bad, 1, damaged))
                    db.commit()
                    batch = []
                    for sid in ((good, bad) if reverse else (bad, good)):
                        replacement = json.loads(originals[sid][1])
                        replacement['runtime'].update(delivery_revision=2, send_attempt_id='replacement-' + sid)
                        batch.append((sid, 2, json.dumps(replacement).encode(), None))
                    outcomes = dict(zip((item[0] for item in batch), self.store._commit_batch(db, batch)))
                    self.assertIsInstance(outcomes[bad], RuntimeError)
                    self.assertIs(outcomes[good], True)
                    self.assertEqual(db.execute('SELECT revision,record FROM delivery WHERE surface_id=?', (bad,)).fetchone(), originals[bad])
                    self.assertEqual(db.execute('SELECT revision,record FROM delivery_history WHERE surface_id=?', (bad,)).fetchone(), (1, damaged))
                    self.assertEqual(db.execute('SELECT revision FROM delivery WHERE surface_id=?', (good,)).fetchone(), (2,))
                    self.assertEqual(db.execute('SELECT revision,record FROM delivery_history WHERE surface_id=?', (good,)).fetchone(), originals[good])
                self.assertTrue(self.store.blocked(bad))
                self.assertFalse(self.store.blocked(good))
                self.assertEqual(self.store._phase, 'running')

    def test_legacy_migration_conflicting_or_corrupt_records_isolate_only_their_surface(self):
        for conflict in ('same_revision_payload', 'same_revision_bytes', 'archived_file', 'archived_sql', 'corrupt_sql', 'corrupt_file'):
            with self.subTest(conflict=conflict):
                legacy, path, original, head = self._legacy_file_heads('migration-conflict-' + conflict)
                damaged = json.loads(head[1])
                damaged['runtime']['send_attempt_id'] = 'conflicting-attempt'
                other = json.dumps(damaged).encode()
                sql_head, history = head, []
                if conflict == 'same_revision_payload':
                    sql_head = (head[0], other)
                elif conflict == 'same_revision_bytes':
                    sql_head = (head[0], head[1] + b' ')
                elif conflict == 'archived_file':
                    damaged['runtime']['delivery_revision'] = 3
                    sql_head, history = (3, json.dumps(damaged).encode()), [(head[0], other)]
                elif conflict == 'archived_sql':
                    sql_head, history = original, [(original[0], original[1] + b' ')]
                elif conflict == 'corrupt_sql':
                    sql_head = (1, b'broken original SQLite evidence')
                elif conflict == 'corrupt_file':
                    path.write_bytes(b'broken original file evidence')
                self._seed_legacy_sqlite(legacy.root, sql_head, history)
                file_before = path.read_bytes()
                legacy.persist('healthy', core.TargetRuntime(send_attempt_id='healthy-original'))
                store, recovered = DeliveryStore(legacy.root), {}
                store.restore(recovered, core.TargetRuntime)
                store.start()
                self.addCleanup(store.close)
                self.assertTrue(store.blocked('surface'))
                self.assertFalse(store.blocked('healthy'))
                self.assertIsNone(store.database_error)
                self.assertEqual(store._phase, 'running')
                healthy = recovered['healthy']
                healthy.send_attempt_id = 'healthy-next'
                store.persist('healthy', healthy)
                self.assertEqual(healthy.delivery_revision, 2)
                runtime, proof = self._pending_after_legacy(head[0])
                with store.pre_input('surface', runtime, identity=proof['identity']) as pending:
                    with self.assertRaisesRegex(RuntimeError, 'unreadable'):
                        pending.persist()
                with closing(sqlite3.connect(legacy.root / 'delivery.sqlite3')) as db:
                    self.assertEqual(db.execute("SELECT revision,record FROM delivery WHERE surface_id='surface'").fetchone(), sql_head)
                    self.assertEqual(db.execute("SELECT revision,record FROM delivery_history WHERE surface_id='surface'").fetchall(), history)
                self.assertEqual(path.read_bytes(), file_before)
                store.close()
                restarted, restored = DeliveryStore(legacy.root), {}
                restarted.restore(restored, core.TargetRuntime)
                restarted.start()
                self.addCleanup(restarted.close)
                self.assertTrue(restarted.blocked('surface'))
                self.assertFalse(restarted.blocked('healthy'))

    def test_legacy_migration_commit_failure_rolls_back_before_writer_ready(self):
        for table in ('delivery', 'delivery_history'):
            with self.subTest(table=table):
                legacy, path, original, head = self._legacy_file_heads('migration-failure-' + table)
                self._seed_legacy_sqlite(legacy.root, original)
                with closing(sqlite3.connect(legacy.root / 'delivery.sqlite3')) as db, db:
                    db.execute('CREATE TRIGGER reject_migration BEFORE INSERT ON ' + table +
                               " BEGIN SELECT RAISE(ABORT, 'migration transaction rejected'); END")
                store, recovered = DeliveryStore(legacy.root), {}
                store.restore(recovered, core.TargetRuntime)
                with self.assertRaisesRegex(sqlite3.Error, 'migration transaction rejected'):
                    store.start()
                self.addCleanup(store.close)
                self.assertEqual(store._phase, 'failed')
                self.assertTrue(store.blocked('surface'))
                with self.assertRaises(RuntimeError):
                    store.persist('other', core.TargetRuntime(send_attempt_id='must-not-run'))
                with closing(sqlite3.connect(legacy.root / 'delivery.sqlite3')) as db, db:
                    self.assertEqual(db.execute("SELECT revision,record FROM delivery WHERE surface_id='surface'").fetchone(), original)
                    self.assertEqual(db.execute('SELECT count(*) FROM delivery_history').fetchone()[0], 0)
                    db.execute('DROP TRIGGER reject_migration')
                self.assertEqual(path.read_bytes(), head[1])
                restarted, restored = DeliveryStore(legacy.root), {}
                restarted.restore(restored, core.TargetRuntime)
                restarted.start()
                self.addCleanup(restarted.close)
                runtime, proof = self._pending_after_legacy(restored['surface'].delivery_revision)
                with restarted.pre_input('surface', runtime, identity=proof['identity']) as pending:
                    pending.persist()
                self.assertEqual(runtime.delivery_revision, head[0] + 1)

    def test_legacy_file_writer_contention_cannot_stall_independent_startup(self):
        legacy, path, original, head = self._legacy_file_heads('migration-active-writer')
        legacy.persist('healthy', core.TargetRuntime(send_attempt_id='healthy-original'))
        store, recovered = DeliveryStore(legacy.root), {}
        store.restore(recovered, core.TargetRuntime)
        with ThreadPoolExecutor(1) as pool:
            try:
                with legacy._file_lock('surface'):
                    started = pool.submit(store.start)
                    started.result(1)
                    self.assertTrue(store.blocked('surface'))
                    self.assertFalse(store.blocked('healthy'))
                    store.persist('healthy', core.TargetRuntime(send_attempt_id='healthy-next', delivery_revision=1))
            finally:
                store.close()
        self.assertEqual(path.read_bytes(), head[1])
        restarted, restored = DeliveryStore(legacy.root), {}
        restarted.restore(restored, core.TargetRuntime)
        restarted.start()
        self.addCleanup(restarted.close)
        self.assertFalse(restarted.blocked('surface'))
        runtime, proof = self._pending_after_legacy(restored['surface'].delivery_revision)
        with restarted.pre_input('surface', runtime, identity=proof['identity']) as pending:
            pending.persist()
        self.assertEqual(runtime.delivery_revision, head[0] + 1)

    def _assert_competing_file_writers(self, processes):
        directory = Path(self.directory.name) / ('file-processes' if processes else 'file-instances')
        store = DeliveryStore(directory)
        store.persist('surface', core.TargetRuntime(send_attempt_id='original', delivery_status='unknown'))
        head = directory / (store.key('surface') + '.json')
        original = head.read_bytes()
        context = multiprocessing.get_context('spawn') if processes else None
        event = context.Event if processes else threading.Event
        runner = context.Process if processes else threading.Thread
        outcomes = context.Queue() if processes else queue.Queue()
        if processes:
            self.addCleanup(outcomes.close)
        held, release = event(), event()
        started, finished = [event(), event()], [event(), event()]
        workers = [runner(target=competing_file_writer, daemon=True,
            args=(str(directory), attempt, started[i], held if i == 0 else None,
                  release, finished[i], outcomes)) for i, attempt in enumerate(('first', 'second'))]
        launched = []
        try:
            workers[0].start()
            launched.append(workers[0])
            self.assertTrue(held.wait(10), 'first writer never reached head replacement')
            workers[1].start()
            launched.append(workers[1])
            self.assertTrue(started[1].wait(10), 'second writer never started')
            second_finished_early = finished[1].wait(.2)
        finally:
            release.set()
            for worker in launched:
                worker.join(10)
                if processes and worker.is_alive():
                    worker.terminate()
                    worker.join(5)
        self.assertFalse(second_finished_early, 'second writer passed an uncommitted competing intent')
        for worker in launched:
            self.assertFalse(worker.is_alive())
            if processes:
                self.assertEqual(worker.exitcode, 0)
                worker.close()
        results = {row[0]: row[1:] for row in (outcomes.get(timeout=2), outcomes.get(timeout=2))}
        self.assertEqual(results['first'], ('accepted', '', 2))
        self.assertEqual(results['second'], ('RuntimeError', 'durable delivery revision is stale', 1))
        self.assertEqual(json.loads(head.read_bytes())['runtime']['send_attempt_id'], 'first')
        archive = directory / 'history' / store.key('surface') / '1.json'
        self.assertEqual(archive.read_bytes(), original)

    def test_file_mode_independent_instances_admit_only_one_revision(self):
        self._assert_competing_file_writers(processes=False)

    def test_file_mode_independent_processes_admit_only_one_revision(self):
        self._assert_competing_file_writers(processes=True)

    def test_file_mode_archive_uses_original_bytes_and_serialized_attempt(self):
        store = DeliveryStore(Path(self.directory.name) / 'file-snapshot')
        runtime = core.TargetRuntime(send_attempt_id='original', delivery_status='unknown')
        store.persist('surface', runtime)
        head = store.root / (store.key('surface') + '.json')
        record = json.loads(head.read_bytes())
        record['opaque-extension'] = {'text': '保留原始字节'}
        original = (json.dumps(record, ensure_ascii=False, indent=2) + '\n').encode()
        head.write_bytes(original)
        read = Path.read_bytes

        def drift_after_snapshot(path):
            data = read(path)
            if path == head:
                runtime.send_attempt_id = 'original'
            return data

        runtime.send_attempt_id = 'replacement'
        with mock.patch.object(Path, 'read_bytes', autospec=True, side_effect=drift_after_snapshot):
            store.persist('surface', runtime)
        archive = store.root / 'history' / store.key('surface') / '1.json'
        self.assertEqual(archive.read_bytes(), original)
        self.assertEqual(json.loads(head.read_bytes())['runtime']['send_attempt_id'], 'replacement')

    def test_file_mode_write_failure_preserves_head_and_allows_original_revision_retry(self):
        for stage in ('archive', 'head'):
            with self.subTest(stage=stage):
                store = DeliveryStore(Path(self.directory.name) / ('file-failure-' + stage))
                runtime = core.TargetRuntime(send_attempt_id='original', delivery_status='unknown')
                store.persist('surface', runtime)
                head = store.root / (store.key('surface') + '.json')
                archive = store.root / 'history' / store.key('surface') / '1.json'
                original, write = head.read_bytes(), store._write_file
                rejected = archive if stage == 'archive' else head

                def reject(path, data):
                    if path == rejected:
                        raise OSError('simulated file write failure')
                    write(path, data)

                runtime.send_attempt_id = 'replacement'
                with mock.patch.object(store, '_write_file', side_effect=reject):
                    with self.assertRaisesRegex(OSError, 'simulated file write failure'):
                        store.persist('surface', runtime)
                self.assertEqual(head.read_bytes(), original)
                self.assertEqual(runtime.delivery_revision, 1)
                store.persist('surface', runtime)
                self.assertEqual(runtime.delivery_revision, 2)
                self.assertEqual(archive.read_bytes(), original)

    def test_file_mode_conflicting_archive_never_overwrites_original_evidence(self):
        store = DeliveryStore(Path(self.directory.name) / 'file-archive-conflict')
        runtime = core.TargetRuntime(send_attempt_id='original', delivery_status='unknown')
        store.persist('surface', runtime)
        head = store.root / (store.key('surface') + '.json')
        original = head.read_bytes()
        archive = store.root / 'history' / store.key('surface') / '1.json'
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b'conflicting original evidence')
        runtime.send_attempt_id = 'replacement'
        with self.assertRaisesRegex(RuntimeError, 'original delivery archive changed'):
            store.persist('surface', runtime)
        self.assertEqual(head.read_bytes(), original)
        self.assertEqual(archive.read_bytes(), b'conflicting original evidence')
        self.assertEqual(runtime.delivery_revision, 1)

    def test_file_mode_archive_directory_fsync_failure_blocks_head_and_is_retried(self):
        for level in ('leaf', 'history', 'root'):
            with self.subTest(level=level):
                store = DeliveryStore(Path(self.directory.name) / ('file-fsync-' + level))
                runtime = core.TargetRuntime(send_attempt_id='original', delivery_status='unknown')
                store.persist('surface', runtime)
                head = store.root / (store.key('surface') + '.json')
                archive = store.root / 'history' / store.key('surface') / '1.json'
                blocked = {'leaf': archive.parent, 'history': archive.parent.parent, 'root': store.root}[level]
                original, fsync = head.read_bytes(), os.fsync

                def reject(fd):
                    actual = os.fstat(fd)
                    if blocked.exists():
                        expected = blocked.stat()
                        if (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino):
                            raise OSError('archive directory fsync failed')
                    fsync(fd)

                runtime.send_attempt_id = 'replacement'
                with mock.patch('ccc_delivery.os.fsync', side_effect=reject):
                    for _ in range(2):
                        with self.assertRaisesRegex(OSError, 'archive directory fsync failed'):
                            store.persist('surface', runtime)
                        self.assertEqual(head.read_bytes(), original)
                        self.assertEqual(runtime.delivery_revision, 1)
                        self.assertEqual(archive.read_bytes(), original)
                store.persist('surface', runtime)
                self.assertEqual(runtime.delivery_revision, 2)
                self.assertEqual(archive.read_bytes(), original)

    def _pending_input(self, attempt='live-input', *, goal=False):
        identity = dict(surface_id='surface', workspace_id='workspace', session_id='session',
                        turn_id='turn', at=10.0, pid=123, process_start=5.0, model_provider='provider')
        key = 'session:turn:10.0'
        if goal:
            identity['goal_id'] = 'original-goal'
            key = 'goal:session:original-goal:turn:10.0'
        runtime = core.TargetRuntime(send_attempt_id=attempt, delivery_status='sending',
            send_started_at=11, codex_sent_turn_key=key, codex_observed_turn_key=key,
            codex_input_phase='text_pending', codex_goal_resume=goal,
            codex_goal_proof=dict(identity) if goal else {})
        proof = dict(identity=identity, attempt_id=attempt, turn_key=key)
        return runtime, proof

    def _head(self):
        with closing(sqlite3.connect(self.store.root / 'delivery.sqlite3')) as db:
            return db.execute("SELECT revision,record FROM delivery WHERE surface_id='surface'").fetchone()

    def _database_statement(self, sql):
        with closing(sqlite3.connect(self.store.root / 'delivery.sqlite3')) as db, db:
            db.execute(sql)

    def _lost_commit_ack(self, connection, batch):
        DeliveryStore._commit_batch(self.store, connection, batch)
        raise sqlite3.OperationalError('commit succeeded but its acknowledgement was lost')

    def test_real_sqlite_rollback_can_durably_recover_only_inside_original_context(self):
        runtime, proof = self._pending_input(goal=True)
        self._database_statement("CREATE TRIGGER reject_pending BEFORE INSERT ON delivery BEGIN SELECT RAISE(ABORT, 'pending rejected'); END")
        with self.store.pre_input('surface', runtime, identity=proof['identity']) as pending:
            with self.assertRaisesRegex(RuntimeError, 'pending rejected'):
                pending.persist()
            self.assertIsNone(self._head())
            self.assertEqual(runtime.delivery_revision, 0)
            self.assertEqual(runtime.codex_input_not_sent, {})
            self._database_statement('DROP TRIGGER reject_pending')
            with ThreadPoolExecutor(1) as pool:
                with self.assertRaisesRegex(PreInputRecoveryError, 'no longer active'):
                    pool.submit(pending.recover_input_not_sent, proof).result(1)
            with mock.patch('ccc_delivery.os.getpid', return_value=os.getpid() + 1):
                with self.assertRaisesRegex(PreInputRecoveryError, 'no longer active'):
                    pending.recover_input_not_sent(proof)
            with self.assertRaises(TypeError):
                pickle.dumps(pending)
            pending.recover_input_not_sent(proof)
        self.assertEqual(runtime.delivery_revision, 1)
        self.assertEqual(runtime.delivery_status, 'retryable')
        self.assertEqual(runtime.codex_input_not_sent, proof)
        self.assertEqual(runtime.codex_input_phase, 'input_not_sent')
        restored = {}
        DeliveryStore(self.store.root).restore(restored, core.TargetRuntime)
        self.assertEqual(restored['surface'].codex_input_not_sent, proof)
        with self.assertRaisesRegex(PreInputRecoveryError, 'no longer active'):
            pending.recover_input_not_sent(proof)

    def test_committed_pending_with_lost_ack_recovers_by_exact_cas(self):
        runtime, proof = self._pending_input()
        with self.store.pre_input('surface', runtime, identity=proof['identity']) as pending:
            with mock.patch.object(self.store, '_commit_batch', side_effect=self._lost_commit_ack):
                with self.assertRaisesRegex(RuntimeError, 'acknowledgement was lost'):
                    pending.persist()
            self.assertEqual(self._head()[0], 1)
            self.assertEqual(runtime.delivery_revision, 0)
            self.assertEqual(json.loads(self._head()[1])['runtime']['delivery_status'], 'sending')
            pending.recover_input_not_sent(proof)
        self.assertEqual(runtime.delivery_revision, 2)
        self.assertEqual(runtime.codex_input_not_sent, proof)
        with closing(sqlite3.connect(self.store.root / 'delivery.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delivery_history').fetchone()[0], 0)

    def test_restart_cannot_reconstruct_zero_input_from_old_pending_or_zero_io_clock(self):
        runtime, proof = self._pending_input()
        with self.store.pre_input('surface', runtime, identity=proof['identity']) as pending:
            with mock.patch.object(self.store, '_commit_batch', side_effect=self._lost_commit_ack):
                with self.assertRaises(RuntimeError):
                    pending.persist()
        original = self._head()
        self.store.close()
        reader, restored = DeliveryStore(self.store.root), {}
        reader.restore(restored, core.TargetRuntime)
        reader.start()
        self.addCleanup(reader.close)
        runtime = restored['surface']
        self.assertEqual(runtime.send_io_started_at, 0)
        with reader.pre_input('surface', runtime, identity=proof['identity']) as replacement:
            with self.assertRaisesRegex(PreInputRecoveryError, 'historical same-attempt'):
                replacement.persist()
            with self.assertRaisesRegex(PreInputRecoveryError, 'does not exactly match'):
                replacement.recover_input_not_sent(proof)
        self.assertEqual(self._head(), original)
        self.assertEqual(runtime.codex_input_not_sent, {})

    def test_failed_proof_commit_never_publishes_runtime_authorization(self):
        runtime, proof = self._pending_input()
        with self.store.pre_input('surface', runtime, identity=proof['identity']) as pending:
            with mock.patch.object(self.store, '_commit_batch', side_effect=self._lost_commit_ack):
                with self.assertRaises(RuntimeError):
                    pending.persist()
            original = self._head()
            self._database_statement("CREATE TRIGGER reject_proof BEFORE INSERT ON delivery BEGIN SELECT RAISE(ABORT, 'proof rejected'); END")
            with self.assertRaisesRegex(RuntimeError, 'proof rejected'):
                pending.recover_input_not_sent(proof)
            self.assertEqual(runtime.codex_input_not_sent, {})
            self.assertEqual(runtime.codex_input_phase, 'text_pending')
            self.assertNotEqual(runtime.delivery_status, 'retryable')
            with self.assertRaises(PreInputRecoveryError):
                pending.recover_input_not_sent(proof)
        self.assertEqual(self._head(), original)
        restored = {}
        DeliveryStore(self.store.root).restore(restored, core.TargetRuntime)
        self.assertEqual(restored['surface'].delivery_status, 'sending')
        self.assertEqual(restored['surface'].codex_input_not_sent, {})

    def test_recovery_preserves_unrelated_unknown_head_after_rollback(self):
        old = core.TargetRuntime(send_attempt_id='older-unknown', delivery_status='unknown',
                                last_send_error='original acknowledgement lost')
        self.store.persist('surface', old)
        original = self._head()
        runtime, proof = self._pending_input()
        runtime.delivery_revision = old.delivery_revision
        self._database_statement("CREATE TRIGGER reject_pending BEFORE INSERT ON delivery BEGIN SELECT RAISE(ABORT, 'pending rejected'); END")
        with self.store.pre_input('surface', runtime, identity=proof['identity']) as pending:
            with self.assertRaisesRegex(RuntimeError, 'pending rejected'):
                pending.persist()
            self._database_statement('DROP TRIGGER reject_pending')
            with self.assertRaisesRegex(PreInputRecoveryError, 'does not exactly match'):
                pending.recover_input_not_sent(proof)
        self.assertEqual(self._head(), original)
        with closing(sqlite3.connect(self.store.root / 'delivery.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delivery_history').fetchone()[0], 0)

    def test_rollback_to_original_same_attempt_zero_input_proof_can_recover(self):
        runtime, proof = self._pending_input()
        prior = core.TargetRuntime.from_dict(runtime.to_dict())
        prior.delivery_status, prior.codex_input_phase = 'retryable', 'input_not_sent'
        prior.codex_input_not_sent = proof
        self.store.persist('surface', prior)
        original = self._head()
        runtime.delivery_revision = prior.delivery_revision
        self._database_statement("CREATE TRIGGER reject_pending BEFORE INSERT ON delivery BEGIN SELECT RAISE(ABORT, 'pending rejected'); END")
        with self.store.pre_input('surface', runtime, identity=proof['identity']) as pending:
            with self.assertRaisesRegex(RuntimeError, 'pending rejected'):
                pending.persist()
            self.assertEqual(self._head(), original)
            self._database_statement('DROP TRIGGER reject_pending')
            pending.recover_input_not_sent(proof)
        self.assertEqual(runtime.delivery_revision, 2)
        self.assertEqual(runtime.delivery_status, 'retryable')
        self.assertEqual(runtime.codex_input_not_sent, proof)
        self.assertEqual(json.loads(self._head()[1])['runtime']['codex_input_not_sent'], proof)

    def test_invalid_old_proof_identity_does_not_stop_unrelated_surface_writer(self):
        runtime, proof = self._pending_input()
        prior = core.TargetRuntime.from_dict(runtime.to_dict())
        prior.delivery_status, prior.codex_input_phase = 'retryable', 'input_not_sent'
        prior.codex_input_not_sent = proof
        self.store.persist('surface', prior)
        record = json.loads(self._head()[1])
        record['runtime']['codex_input_not_sent']['identity']['old_extra'] = {'clock': float('nan')}
        with closing(sqlite3.connect(self.store.root / 'delivery.sqlite3')) as db, db:
            db.execute("UPDATE delivery SET record=? WHERE surface_id='surface'", (json.dumps(record).encode(),))
        original = self._head()
        runtime.delivery_revision = prior.delivery_revision
        with self.store.pre_input('surface', runtime, identity=proof['identity']) as pending:
            with self.assertRaisesRegex(PreInputRecoveryError, 'invalid native identity'):
                pending.persist()
            with self.assertRaisesRegex(PreInputRecoveryError, 'does not exactly match'):
                pending.recover_input_not_sent(proof)
        self.assertEqual(self._head(), original)
        self.assertEqual(runtime.codex_input_not_sent, {})
        other = core.TargetRuntime(send_attempt_id='unrelated', delivery_status='sending')
        self.store.persist('other', other)
        self.assertEqual(other.delivery_revision, 1)
        with closing(sqlite3.connect(self.store.root / 'delivery.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT revision FROM delivery WHERE surface_id='other'").fetchone(), (1,))
        self.assertEqual(self.store._phase, 'running')
        self.assertIsNone(self.store.database_error)

    def test_malformed_private_origin_does_not_stop_unrelated_surface_writer(self):
        for case, origin in (('missing', None), ('null', None), ('list', []),
                             ('text', 'origin'), ('number', 1), ('incomplete', {})):
            with self.subTest(case=case):
                self._database_statement("DELETE FROM delivery WHERE surface_id='surface'")
                runtime, proof = self._pending_input()
                private = dict(failed_turn_id='turn', failed_at=10.0)
                if case != 'missing':
                    private['origin'] = origin
                runtime.codex_private_check = private
                proof['private_check'] = private
                prior = core.TargetRuntime.from_dict(runtime.to_dict())
                prior.delivery_status, prior.codex_input_phase = 'retryable', 'input_not_sent'
                prior.codex_input_not_sent = proof
                self.store.persist('surface', prior)
                original = self._head()
                runtime.delivery_revision = prior.delivery_revision
                with self.store.pre_input('surface', runtime, identity=proof['identity']) as pending:
                    with self.assertRaisesRegex(PreInputRecoveryError, 'original native binding'):
                        pending.persist()
                    with self.assertRaisesRegex(PreInputRecoveryError, 'original native binding'):
                        pending.recover_input_not_sent(proof)
                self.assertEqual(self._head(), original)
                self.assertEqual(runtime.codex_input_not_sent, {})
                other_id = 'unrelated-' + case
                other = core.TargetRuntime(send_attempt_id=other_id, delivery_status='sending')
                self.store.persist(other_id, other)
                self.assertEqual(other.delivery_revision, 1)
                with closing(sqlite3.connect(self.store.root / 'delivery.sqlite3')) as db:
                    self.assertEqual(db.execute('SELECT revision FROM delivery WHERE surface_id=?',
                                                (other_id,)).fetchone(), (1,))
                self.assertEqual(self.store._phase, 'running')
                self.assertIsNone(self.store.database_error)

    def test_recovery_rejects_changed_complete_head_or_identity(self):
        for damage in ('changed_io', 'unknown', 'other_attempt', 'unreadable', 'process_identity'):
            with self.subTest(damage=damage):
                self._database_statement('DELETE FROM delivery')
                self.store.faults.clear()
                runtime, proof = self._pending_input()
                with self.store.pre_input('surface', runtime, identity=proof['identity']) as pending:
                    with mock.patch.object(self.store, '_commit_batch', side_effect=self._lost_commit_ack):
                        with self.assertRaises(RuntimeError):
                            pending.persist()
                    before = self._head()
                    record = json.loads(before[1])
                    if damage == 'changed_io':
                        record['runtime']['send_io_started_at'] = 12
                    elif damage == 'unknown':
                        record['runtime']['delivery_status'] = 'unknown'
                    elif damage == 'other_attempt':
                        record['runtime']['send_attempt_id'] = 'someone-else'
                    elif damage == 'process_identity':
                        proof = {**proof, 'identity': {**proof['identity'], 'pid': 456}}
                    if damage != 'process_identity':
                        data = b'broken-json' if damage == 'unreadable' else json.dumps(record).encode()
                        with closing(sqlite3.connect(self.store.root / 'delivery.sqlite3')) as db, db:
                            db.execute("UPDATE delivery SET record=? WHERE surface_id='surface'", (data,))
                    original = self._head()
                    if damage == 'process_identity':
                        self.assertEqual(original, before)
                    with self.assertRaises(RuntimeError):
                        pending.recover_input_not_sent(proof)
                    self.assertEqual(self._head(), original)
                    self.assertEqual(runtime.codex_input_not_sent, {})
                    self.assertNotEqual(runtime.delivery_status, 'retryable')

    def test_equal_revision_durable_head_overrules_uncommitted_snapshot_proof(self):
        runtime, proof = self._pending_input()
        self.store.persist('surface', runtime)
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    record = json.loads(self._head()[1])
                    for key in ('codex_input_not_sent', 'codex_input_phase'):
                        del record['runtime'][key]
                    with closing(sqlite3.connect(self.store.root / 'delivery.sqlite3')) as db, db:
                        db.execute("UPDATE delivery SET record=? WHERE surface_id='surface'", (json.dumps(record).encode(),))
                snapshot = core.TargetRuntime.from_dict(runtime.to_dict())
                snapshot.codex_input_not_sent, snapshot.codex_input_phase = proof, 'input_not_sent'
                snapshot.delivery_status = 'retryable'
                snapshot.observed_reason = 'newer independent observation'
                recovered = {'surface': snapshot}
                DeliveryStore(self.store.root).restore(recovered, core.TargetRuntime)
                self.assertEqual(snapshot.delivery_status, 'sending')
                self.assertEqual(snapshot.codex_input_not_sent, {})
                self.assertEqual(snapshot.codex_input_phase, '' if legacy else 'text_pending')
                self.assertEqual(snapshot.observed_reason, 'newer independent observation')

    def test_file_replace_followed_by_error_requires_exact_pending_head(self):
        store = DeliveryStore(self.store.root / 'file-pre-input')
        runtime, proof = self._pending_input()
        write = store._write_file

        def lost_ack(path, data):
            write(path, data)
            raise OSError('head replaced before acknowledgement was lost')

        with store.pre_input('surface', runtime, identity=proof['identity']) as pending:
            with mock.patch.object(store, '_write_file', side_effect=lost_ack):
                with self.assertRaisesRegex(OSError, 'head replaced'):
                    pending.persist()
            self.assertEqual(runtime.delivery_revision, 0)
            pending.recover_input_not_sent(proof)
        self.assertEqual(runtime.delivery_revision, 2)
        restored = {}
        DeliveryStore(store.root).restore(restored, core.TargetRuntime)
        self.assertEqual(restored['surface'].codex_input_not_sent, proof)


if __name__ == '__main__':
    unittest.main()

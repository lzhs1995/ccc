"""Actual SQLite durability and writer failure boundaries for native input."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

import cmux_codex_watch as core
from ccc_delivery import DeliveryStore
from tests.test_watch import FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload
from tests.native_failure_fixture import bind_native_failure


class DeliveryJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = DeliveryStore(self.directory.name)
        self.store.start()
        self.addCleanup(self.store.close)

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


if __name__ == '__main__':
    unittest.main()

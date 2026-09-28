"""Real thread/file/SQLite concurrency; no cmux terminals or upstream requests.

The transport boundary below is a fixture. Native/UI timing is a separate
acceptance gate and must not be inferred from these regression tests.
"""
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures


class ImmediateBatchTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def threaded(self, *, keep_first=False):
        self.worker._slot_pool = RealThreadPoolExecutor(50)
        self.entered, self.entry_times = [], []
        entered_lock, registration_lock = threading.Lock(), threading.Lock()
        all_entered, release, first = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(first.set)
        original = self.client.new_codex_surface
        began = time.perf_counter()
        def create(*args):
            import shlex
            tokens = shlex.split(args[-1])
            index = int(tokens[tokens.index('--index') + 1])
            with entered_lock:
                self.entered.append(index)
                self.entry_times.append(time.perf_counter() - began)
                if len(self.entered) == 50:
                    all_entered.set()
            if not release.wait(10):
                raise AssertionError('test did not release creation barrier')
            if keep_first and index == 0 and not first.wait(10):
                raise AssertionError('test did not release slow slot')
            # The legacy fixture models separate shell environments by
            # patching os.environ. Serialize only that test-only section.
            with registration_lock:
                return original(*args)
        self.client.new_codex_surface = create
        return all_entered, release, first

    def until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.now += .03
            self.worker.step()
            time.sleep(.005)
        self.assertTrue(predicate(), batch.counts(self.worker.job))

    def test_all_fifty_enter_creation_before_any_ack_and_keep_every_intent(self):
        all_entered, release, _ = self.threaded()
        self.assertTrue(self.worker.step())
        self.assertTrue(all_entered.wait(10), self.entered)
        saved = core.load_json(self.worker.path, {})
        self.assertEqual(set(self.entered), set(range(50)))
        self.assertEqual([s['phase'] for s in saved['slots']], ['creating'] * 50)
        self.assertEqual(len({s['launch_id'] for s in saved['slots']}), 50)
        self.assertFalse((self.root / 'batch-capacity.lock').exists())
        self.assertFalse((self.root / 'batch-capacity.json').exists())
        release.set()
        self.until(lambda: batch.counts(self.worker.job)['started'] == 50)
        self.assertEqual(len(self.client.calls), 50)
        self.assertEqual(len(self.client.sent), 50)
        self.assertEqual(len(set(self.client.sent)), 50)

    def test_slow_first_create_does_not_hold_other_forty_nine_prompts(self):
        all_entered, release, first = self.threaded(keep_first=True)
        self.worker.step()
        self.assertTrue(all_entered.wait(10))
        release.set()
        self.until(lambda: len(self.client.sent) == 49)
        self.assertIn(0, self.worker._inflight)
        self.assertEqual(self.worker.job['slots'][0]['phase'], 'creating')
        self.assertNotIn('surface_id', self.worker.job['slots'][0])
        first.set()
        self.until(lambda: batch.counts(self.worker.job)['started'] == 50)
        self.assertEqual(len(self.client.calls), 50)

    def test_repeated_steps_cannot_duplicate_an_inflight_create(self):
        all_entered, release, _ = self.threaded()
        self.worker.step()
        self.assertTrue(all_entered.wait(10))
        for _ in range(5):
            self.now += .03
            self.worker.step()
        self.assertEqual(len(self.entered), 50)
        release.set()
        self.until(lambda: batch.counts(self.worker.job)['started'] == 50)

    def test_existing_capacity_file_and_held_capacity_lock_do_not_throttle(self):
        core.atomic_write_json(self.root / 'batch-capacity.json', {'last_start': self.now, 'last_job': 'other'})
        with core.FileLock(self.root / 'batch-capacity.lock'):
            self.worker.step()
        self.assertEqual(len(self.client.calls), 50)

    def test_pause_during_durable_create_intent_dispatches_nothing(self):
        slot = self.worker.job['slots'][0]
        original = self.worker.save
        def save():
            original()
            if slot['phase'] == 'creating':
                self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
        with patch.object(self.worker, 'save', side_effect=save):
            self.worker._create(slot)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(slot['phase'], 'pending')
        self.assertIn('create_not_sent_at', slot)

    def test_pause_or_draft_after_first_prompt_intent_sends_nothing(self):
        self.worker.job['slots'] = self.worker.job['slots'][:1]
        self.worker.step()
        slot = self.worker.job['slots'][0]
        original = self.worker.save
        for change in ('pause', 'draft'):
            with self.subTest(change=change):
                self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=False))
                self.client.frame_options = {}
                def save():
                    original()
                    if slot['phase'] == 'submitting':
                        if change == 'pause':
                            self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
                        else:
                            self.client.frame_options = {'composer': 'busy'}
                with patch.object(self.worker, 'save', side_effect=save):
                    self.worker._advance(slot)
                self.assertEqual(self.client.sent, [])
                self.assertEqual(slot['phase'], 'created')
                self.assertNotIn('submit_at', slot)
                self.assertIn('submit_not_sent_at', slot)

    def test_native_access_is_explicit_short_check_without_gateway_or_name_wait(self):
        result = batch.start(self.config, str(uuid.uuid4()), launch=False, native_access=True)
        job = core.load_json(batch.job_path(self.config, result['job_id']), {})
        self.assertEqual(job['native_access_policy'], 'direct-native-v1')
        self.assertEqual(job['check_retry_policy'], 'fixed-check-v1')
        self.assertEqual(job['initial_prompt'], batch.PROMPT)
        self.assertEqual(batch.startup_mode(job, self.config), 'private_check')
        self.assertEqual(len(job['slots']), 50)
        self.assertNotIn('name_policy', job)
        self.assertFalse(any(key.startswith('access_') for key in job))
        self.assertFalse((batch.job_path(self.config, job['id']).parent / 'access.json').exists())
        args = batch.native_launch_argv(self.config, job, 0)
        self.assertEqual(args[0], '/test/native/codex')
        self.assertFalse(any('127.0.0.1' in arg for arg in args))

    def test_native_access_does_not_reinterpret_an_old_b_job(self):
        before = self.worker.path.read_bytes()
        with self.assertRaises(RuntimeError):
            batch.start(self.config, self.wid, launch=False, native_access=True)
        self.assertEqual(self.worker.path.read_bytes(), before)

    def test_new_b_has_no_cost_saving_name_barrier(self):
        self.assertNotIn('name_policy', self.worker.job)
        self.initial_name = ''
        self.worker.step()
        self.worker.step()
        self.assertEqual(len(self.client.sent), 50)
        self.assertEqual(self.client.rename_sent, [])

    def test_configuration_generation_change_vetoes_cached_authority(self):
        first = self.worker._configuration()
        self.assertTrue(batch.allowed(first, self.worker.job))
        self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
        second = self.worker._configuration()
        self.assertFalse(batch.allowed(second, self.worker.job))
        self.assertFalse(batch.allowed(self.worker._configuration(), self.worker.job))

    def test_configuration_changed_during_load_is_never_cached(self):
        original = self.store.load
        def change():
            value = original()
            changed = copy.deepcopy(value)
            changed['workspace_rules'][0]['paused'] = True
            core.atomic_write_json(self.config, changed)
            return value
        with patch.object(self.worker.store, 'load', side_effect=change):
            with self.assertRaisesRegex(RuntimeError, '代际'):
                self.worker._configuration()
        self.assertIsNone(self.worker._config_snapshot)


class ConcurrentSQLiteTests(unittest.TestCase):
    def test_fifty_writers_are_independent_and_legacy_database_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'original'
            source.mkdir()
            with closing(sqlite3.connect(source / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE backfill_state (id INTEGER, status TEXT)')
                db.execute("INSERT INTO backfill_state VALUES (1, 'complete')")
                db.execute('CREATE TABLE records (slot INTEGER)')
                db.execute('INSERT INTO records VALUES (-1)')
            original = (source / 'state_5.sqlite').read_bytes()
            config, jid = root / 'app' / 'config.json', str(uuid.uuid4())
            legacy = batch.job_path(config, jid).parent / 'native-db' / 'state_5.sqlite'
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b'old live native database must remain byte-identical')
            with patch.dict(os.environ, CODEX_HOME=str(source), CODEX_SQLITE_HOME=str(source)):
                batch.prepare_sqlite_home(config, jid)
                def write_slot(index):
                    directory = batch.prepare_slot_sqlite_home(config, jid, index)
                    path = directory / 'state_5.sqlite'
                    with closing(sqlite3.connect(path, timeout=0)) as db, db:
                        db.execute('INSERT INTO records VALUES (?)', (index,))
                        rows = db.execute('SELECT slot FROM records ORDER BY slot').fetchall()
                    batch.prepare_slot_sqlite_home(config, jid, index)
                    return path.stat().st_ino, rows
                with RealThreadPoolExecutor(50) as pool:
                    result = list(pool.map(write_slot, range(50)))
            self.assertEqual(len({inode for inode, rows in result}), 50)
            for index, (_, rows) in enumerate(result):
                self.assertEqual(rows, [(-1,), (index,)])
            self.assertEqual((source / 'state_5.sqlite').read_bytes(), original)
            self.assertEqual(legacy.read_bytes(), b'old live native database must remain byte-identical')


if __name__ == '__main__':
    unittest.main()

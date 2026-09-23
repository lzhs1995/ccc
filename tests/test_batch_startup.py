"""Do not count an exited CLI or an onboarding menu as a running batch slot."""
import json
from contextlib import closing
import os
from pathlib import Path
import shlex
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import ccc_workspace_batch as batch
import ccc_codex_queue as native
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures
from tests.test_watch import grid_payload, span


def failure_frame(sid, draft=''):
    lines = ["Codex couldn't start because another Codex process is using its local data.",
             'ERROR: failed to initialize sqlite local db: (code: 5) database is locked',
             'user@host /tmp % ' + draft]
    frame = grid_payload([])
    grid = frame['render_grid']
    grid.update(surface_id=sid, row_spans=[span(i, 0, line) for i, line in enumerate(lines)],
                cursor={'row': 2, 'column': len(lines[-1]), 'visible': True})
    return frame


class BatchStartupTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def failed(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        sid = slot['surface_id']
        self.client.bindings.clear()
        self.client.states[sid] = {'kind': 'unknown'}
        self.worker.processes[sid] = {'agent_kind': 'shell', 'agent_pids': [],
                                      'process_snapshot_present': True}
        self.now += 1
        return slot

    def test_native_sqlite_is_per_batch_without_replacing_codex_home(self):
        self.worker.step()
        first = self.worker.job['slots'][0]
        self.now += 1
        second = self.worker.job['slots'][1]
        self.worker._create(second)
        values = []
        for slot in (first, second):
            command = self.worker._launch_command(slot)
            tokens = shlex.split(command)
            self.assertNotIn('CODEX_HOME', command)
            self.assertEqual(tokens[-3:-1], ['codex', '-c'])
            path = Path(json.loads(tokens[-1].split('=', 1)[1]))
            self.assertTrue(path.is_dir())
            values.append(path)
        self.assertEqual(*values)
        self.assertNotEqual(values[0], batch.sqlite_home(self.config, str(uuid.uuid4()), 0))

    def test_db_failure_relaunches_same_surface_once_and_proves_first_task(self):
        slot = self.failed()
        sid = slot['surface_id']
        recovered = False
        original_replay = self.client.replay
        def replay(wid, surface):
            return original_replay(wid, surface) if recovered else failure_frame(surface)
        def restart(window, surface, command):
            nonlocal recovered
            self.assertEqual((window, surface), (self.window, sid))
            self.assertEqual(core.load_json(self.worker.path, {})['slots'][0]['phase'], 'restarting')
            tokens = shlex.split(command)
            with patch.dict(os.environ, CMUX_SURFACE_ID=sid, CMUX_WORKSPACE_ID=self.wid):
                batch.register(self.config, self.job['job_id'], 0, tokens[tokens.index('--launch-id')+1])
            session = str(uuid.uuid4())
            path = self.root / (session + '.jsonl')
            path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': session}}) + '\n')
            self.client.bindings[session] = {'surfaceId': sid, 'workspaceId': self.wid, 'transcriptPath': str(path)}
            self.client.states[sid] = {'kind': 'unknown', 'session_id': session, 'pid': 777}
            recovered = True
            raise core.CmuxError('lost restart acknowledgement')
        with patch.object(self.client, 'replay', side_effect=replay), \
                patch.object(self.client, 'respawn_surface', side_effect=restart, create=True) as respawn:
            self.worker._advance(slot)
            self.worker._advance(slot)
            self.worker._advance(slot)
        respawn.assert_called_once()
        self.assertEqual(slot['phase'], 'confirmed')
        self.assertEqual(self.client.calls, [sid])
        self.assertEqual(self.client.sent, [sid])
        self.assertFalse(core.batch_start_hold(self.store.load()['workspace_rules'][0], sid))

    def test_stale_receipt_never_replays_an_ambiguous_restart(self):
        slot = self.failed()
        with patch.object(self.client, 'replay', return_value=failure_frame(slot['surface_id'])), \
                patch.object(self.client, 'respawn_surface', side_effect=core.CmuxError('timeout'), create=True) as restart:
            for _ in range(4):
                self.worker._advance(slot)
        restart.assert_called_once()
        self.assertEqual(slot['phase'], 'restart_unknown')
        self.assertEqual(self.client.sent, [])

    def test_existing_session_or_missing_process_inventory_forbids_restart(self):
        slot = self.failed()
        sid = slot['surface_id']
        with patch.object(self.client, 'replay', return_value=failure_frame(sid)), \
                patch.object(self.client, 'respawn_surface', create=True) as restart:
            self.worker.processes[sid]['process_snapshot_present'] = False
            self.worker._advance(slot)
            self.worker.processes[sid]['process_snapshot_present'] = True
            self.client.bindings['original'] = {'surfaceId': sid}
            self.worker._advance(slot)
            self.client.bindings.clear()
            slot['native_seen_session_id'] = 'original'
            self.worker._advance(slot)
        restart.assert_not_called()

    def test_draft_or_pool_pause_forbids_restart(self):
        slot = self.failed()
        with patch.object(self.client, 'replay', return_value=failure_frame(slot['surface_id'], 'echo keep me')), \
                patch.object(self.client, 'respawn_surface', create=True) as restart:
            self.worker._advance(slot)
        restart.assert_not_called()
        self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
        with patch.object(self.client, 'replay', return_value=failure_frame(slot['surface_id'])), \
                patch.object(self.client, 'respawn_surface', create=True) as restart:
            self.worker._advance(slot)
        restart.assert_not_called()

    def test_unresolved_initialization_does_not_keep_all_four_permits(self):
        for slot in self.worker.job['slots'][:4]:
            slot.update(phase='create_unknown', created_at=self.now - 31)
        self.worker.save()
        self.assertTrue(self.worker._reserve_start(self.worker.job['slots'][4]))
        self.assertTrue(all(s['phase'] == 'create_unknown' for s in self.worker.job['slots'][:4]))

    def test_delayed_older_bootstrap_cannot_overwrite_recovery_receipt(self):
        slot = self.failed()
        path = self.worker.path.parent / 'surface-0.json'
        old = path.read_bytes()
        slot['launch_id'] = str(uuid.uuid4())
        self.worker.save()
        with patch.dict(os.environ, CMUX_SURFACE_ID=slot['surface_id'], CMUX_WORKSPACE_ID=self.wid):
            with self.assertRaisesRegex(RuntimeError, 'stale batch launch'):
                batch.register(self.config, self.job['job_id'], 0, '')
        self.assertEqual(path.read_bytes(), old)

    def test_exhausted_pty_queues_without_creating_empty_tabs(self):
        self.worker.pty_probe = lambda: False
        for _ in range(3):
            self.worker.step()
            self.now += 1
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.worker.job['status'], 'waiting')
        self.assertIn('PTY', self.worker.job['error'])
        self.worker.pty_probe = lambda: True
        self.worker.step()
        self.assertEqual(len(self.client.calls), 1)

    def test_no_pty_surface_is_reused_after_capacity_returns(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        sid = slot['surface_id']
        (self.worker.path.parent / 'surface-0.json').unlink()
        self.client.states[sid] = {'kind': 'unknown'}
        self.client.bindings.clear()
        self.now += 5
        frame = grid_payload(['Your system cannot allocate any more pty devices.'])
        frame['render_grid']['surface_id'] = sid
        self.worker.pty_probe = lambda: False
        with patch.object(self.client, 'replay', return_value=frame), \
                patch.object(self.client, 'respawn_surface', create=True) as restart:
            self.worker._advance(slot)
            self.assertEqual(slot['phase'], 'pty_wait')
            restart.assert_not_called()
            self.worker.pty_probe = lambda: True
            self.worker._advance(slot)
            self.worker._advance(slot)
        restart.assert_called_once()
        self.assertEqual(restart.call_args.args[1], sid)
        self.assertEqual(self.client.calls, [sid])
        self.assertEqual(slot['phase'], 'restart_unknown')

    def test_a_waiting_pool_cannot_keep_other_pool_at_head_of_queue(self):
        batch.start(self.config, str(uuid.uuid4()), launch=False)
        core.atomic_write_json(self.config.parent / 'batch-capacity.json', {
            'last_start': self.now - 2, 'last_job': self.job['job_id']})
        self.assertTrue(self.worker._reserve_start(self.worker.job['slots'][0]))

    def test_closed_surface_releases_permit_without_replacement(self):
        self.worker.step()
        first = self.worker.job['slots'][0]
        original = first['surface_id']
        self.client.calls.clear()  # The user closed this tab, not its workspace.
        self.now += 6
        self.worker.step()
        self.assertEqual(first['phase'], 'surface_closed')
        self.assertEqual(first['surface_id'], original)
        self.assertEqual(len(self.client.calls), 1)  # Next original pending slot.
        self.assertNotEqual(self.client.calls[0], original)


class DirectBatchProcessTests(unittest.TestCase):
    def label(self, *, reused=False, truncated=False, foreign=False):
        shell = [10, 20]
        identity = [shell, [11, 20] if reused else shell]
        def children(kind, pid, buffer, size):
            buffer[0] = 22
            return size if truncated else 4
        def info(pid, kind, offset, out, size):
            value = out._obj
            value.pid, value.ppid, value.name, value.status = 22, 11, b'codex', 2
            return size
        with patch.object(native, 'batch_shell_identity', side_effect=identity), \
                patch.object(native, '_proc_listpids', side_effect=children), \
                patch.object(native, '_proc_pidinfo', side_effect=info), \
                patch.object(native, 'process_placement_start', return_value=None if foreign else 30):
            return native.batch_child_label(11, shell, {'surface_id': 'surface', 'workspace_id': 'pool'})

    def test_pinned_shell_discovers_codex_without_system_top(self):
        label = self.label()
        self.assertEqual(label['agent_pids'], [22])
        self.assertEqual(label['agent_kind'], 'codex')

    def test_pid_reuse_truncated_children_and_foreign_session_fail_closed(self):
        for case in ('reused', 'truncated', 'foreign'):
            with self.subTest(case=case):
                self.assertIsNone(self.label(**{case: True}))

    def test_upgrade_retires_only_matching_older_batch_helper(self):
        job = {'id': str(uuid.uuid4()), 'worker_version': batch.WORKER_VERSION-1, 'worker_pid': 12345}
        good = '/usr/bin/python3 /release/ccc_workspace_batch.py run --config /config.json --job ' + job['id']
        for command, expected in [(good, True), ('/Applications/codex', False),
                                  (good.replace(job['id'], str(uuid.uuid4())), False),
                                  ('/usr/bin/python3 /acceptance/live_batch.py', False)]:
            with self.subTest(command=command), \
                    patch.object(batch.subprocess, 'run', return_value=SimpleNamespace(stdout=command)), \
                    patch.object(batch.os, 'kill') as kill:
                self.assertEqual(batch.retire_old_worker(job), expected)
                self.assertEqual(kill.call_count, int(expected))


class NativeMetadataSeedTests(unittest.TestCase):
    def test_native_complete_metadata_is_copied_without_logs_or_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'native'
            source.mkdir()
            with closing(sqlite3.connect(source / 'state_5.sqlite')) as db, db:
                db.execute('CREATE TABLE backfill_state (id INTEGER, status TEXT)')
                db.execute("INSERT INTO backfill_state VALUES (1, 'complete')")
                db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
                db.execute("INSERT INTO threads VALUES ('old', '/original/rollout.jsonl')")
            (source / 'logs_2.sqlite').write_bytes(b'never copy busy multi-GB logs')
            (source / 'thread_history_1.sqlite').write_bytes(b'never copy history index')
            config, jid = root / 'ccc/config.json', str(uuid.uuid4())
            with patch.dict(os.environ, CODEX_SQLITE_HOME=str(source)), \
                    patch.object(Path, 'read_text', side_effect=FileNotFoundError):
                batch.prepare_sqlite_home(config, jid)
                target = batch.sqlite_home(config, jid, 0)
                with closing(sqlite3.connect(target / 'state_5.sqlite')) as db, db:
                    self.assertEqual(db.execute('SELECT * FROM threads').fetchall(), [('old', '/original/rollout.jsonl')])
                    self.assertEqual(db.execute('SELECT status FROM backfill_state').fetchone(), ('complete',))
                    db.execute("INSERT INTO threads VALUES ('new', '/new/rollout.jsonl')")
                batch.prepare_sqlite_home(config, jid)
            with closing(sqlite3.connect(target / 'state_5.sqlite')) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM threads').fetchone(), (2,))
            self.assertFalse((target / 'logs_2.sqlite').exists())
            self.assertFalse((target / 'thread_history_1.sqlite').exists())


if __name__ == '__main__':
    unittest.main()

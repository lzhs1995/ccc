import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

SPEC = importlib.util.spec_from_file_location('batch_log_cleanup', Path(__file__).parents[1] / 'tools/batch_log_cleanup.py')
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name).resolve()
        self.job_id = str(uuid.uuid4())
        self.workspace_id = str(uuid.uuid4())
        self.surface_id = str(uuid.uuid4())
        self.directory = self.app / 'workspace-batches' / self.job_id
        self.db = self.directory / 'native-db'
        self.db.mkdir(parents=True)
        self.job = {'id': self.job_id, 'workspace_id': self.workspace_id, 'status': 'complete',
                    'config_path': str(self.app / 'config.json'), 'worker_pid': 100001,
                    'slots': [{'pid': 100002, 'surface_id': self.surface_id, 'session_id': str(uuid.uuid4())}]}
        (self.app / 'config.json').write_text('{}')
        (self.app / 'config.lock').touch()
        (self.directory / 'worker.lock').touch()
        (self.directory / 'job.json').write_text(json.dumps(self.job))
        for name in ('logs_2.sqlite', 'logs_2.sqlite-wal', 'logs_2.sqlite-shm',
                     'state_5.sqlite', 'state_5.sqlite-wal', 'queue_1.sqlite'):
            (self.db / name).write_bytes(b'preserve-or-delete-test')
        for path in self.app.rglob('*'):
            if path.is_file():
                os.utime(path, (time.time() - 172800, time.time() - 172800))
        self.world = dict(workspaces=set(), surfaces=set(), processes={}, opened=[], observed=time.monotonic())
        self.observer = lambda root: dict(self.world, observed=time.monotonic())
        self.addCleanup(patch.stopall)
        patch.object(cleanup, 'JANITOR', self.app / 'janitor').start()

    def preview(self):
        return cleanup.preview(self.app, observer=self.observer)

    def apply(self, plan):
        return cleanup.apply(plan, self.app / 'receipt.jsonl', observer=self.observer)

    def test_preview_changes_no_batch_bytes_or_stat(self):
        before = {str(p): (p.read_bytes(), cleanup.stamp(p)) for p in self.app.rglob('*') if p.is_file()}
        plan = self.preview()
        after = {str(p): (p.read_bytes(), cleanup.stamp(p)) for p in self.app.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(len(plan['candidates']), 1)

    def test_delete_only_logs_preserve_state_job_and_locks(self):
        result = self.apply(self.preview())
        self.assertEqual(result['deleted_files'], 3)
        self.assertEqual((self.db / 'state_5.sqlite-wal').read_bytes(), b'preserve-or-delete-test')
        self.assertTrue((self.db / 'queue_1.sqlite').exists())
        self.assertEqual(json.loads((self.directory / 'job.json').read_text()), self.job)
        self.assertTrue((self.directory / 'worker.lock').exists())

    def test_active_sources_each_block(self):
        for key, value in [('workspaces', {self.workspace_id}), ('surfaces', {self.surface_id}),
                           ('processes', {100002: 'codex'}), ('processes', {999: 'codex ' + self.job_id}),
                           ('opened', [str(self.db / 'state_5.sqlite')])]:
            with self.subTest(key=key, value=value):
                with patch.dict(self.world, {key: value}):
                    self.assertEqual(self.preview()['candidates'], [])

    def test_live_change_after_preview_blocks_apply(self):
        plan = self.preview()
        self.world['workspaces'].add(self.workspace_id)
        self.assertEqual(self.apply(plan)['deleted_files'], 0)

    def test_symlink_at_apply_blocks(self):
        plan = self.preview()
        path = self.db / 'logs_2.sqlite'
        path.unlink(); path.symlink_to(self.db / 'state_5.sqlite')
        self.assertEqual(self.apply(plan)['deleted_files'], 0)
        self.assertTrue((self.db / 'state_5.sqlite').exists())

    def test_added_log_blocks_group(self):
        plan = self.preview()
        (self.db / 'logs_3.sqlite').write_bytes(b'new')
        self.assertEqual(self.apply(plan)['deleted_files'], 0)

    def test_changed_inode_blocks(self):
        plan = self.preview()
        path = self.db / 'logs_2.sqlite'
        path.rename(self.db / 'old.log'); path.write_bytes(b'replacement')
        self.assertEqual(self.apply(plan)['deleted_files'], 0)

    def test_configuration_change_blocks(self):
        plan = self.preview()
        (self.app / 'config.json').write_text('{"changed":true}')
        self.assertEqual(self.apply(plan)['deleted_files'], 0)

    def test_worker_lock_held_blocks(self):
        with cleanup.locked(self.directory / 'worker.lock'):
            self.assertEqual(self.preview()['candidates'], [])

    def test_recent_nonterminal_and_unknown_config_block(self):
        for field, value in [('status', 'running'), ('status', 'needs_attention'), ('config_path', '/elsewhere')]:
            with self.subTest(field=field):
                data = dict(self.job, **{field: value})
                p = self.directory / 'job.json'; p.write_text(json.dumps(data))
                os.utime(p, (time.time() - 172800,) * 2)
                self.assertEqual(self.preview()['candidates'], [])

    def test_subdirectory_layouts(self):
        for layout in ('template', 'slots/0'):
            target = self.db / layout; target.mkdir(parents=True)
            p = target / 'logs_2.sqlite'; p.write_bytes(b'log')
            os.utime(p, (time.time() - 172800,) * 2)
        plan = self.preview()
        self.assertEqual(len(plan['candidates'][0]['files']), 5)
        self.assertEqual(self.apply(plan)['deleted_files'], 5)

    def test_observation_error_never_clears_candidate(self):
        plan = self.preview()
        with patch.object(cleanup, 'observe', side_effect=RuntimeError('unavailable')) as observer:
            result = cleanup.apply(plan, self.app / 'receipt.jsonl', observer=observer)
        self.assertEqual(result['deleted_files'], 0)

    def test_receipt_collision_preserves_all(self):
        plan = self.preview(); (self.app / 'receipt.jsonl').write_text('old')
        with self.assertRaises(FileExistsError):
            self.apply(plan)
        self.assertTrue((self.db / 'logs_2.sqlite').exists())

    def test_stale_plan_and_unsafe_retention_rejected(self):
        for key, value in [('created_at', time.time() - 3600), ('source_sha256', 'wrong'),
                           ('idle_hours', -1), ('idle_hours', float('nan'))]:
            with self.subTest(key=key):
                plan = self.preview(); plan[key] = value
                with self.assertRaises(ValueError):
                    self.apply(plan)

    def test_disabled_blocks(self):
        plan = self.preview(); cleanup.JANITOR.mkdir(); (cleanup.JANITOR / 'DISABLED').touch()
        self.assertEqual(self.apply(plan)['deleted_files'], 0)

    def test_query_failure_and_missing_topology_refused(self):
        with patch.object(cleanup.subprocess, 'run') as run:
            run.return_value.returncode = 1
            run.return_value.stderr = ''
            with self.assertRaises(RuntimeError):
                cleanup.run(['/bin/ps'])
        for value in ({}, {'windows': []}, {'windows': [{'workspaces': [{}]}]}):
            with self.subTest(tree=value):
                with self.assertRaises((ValueError, KeyError)):
                    cleanup.topology(value)

    def test_owner_arrives_during_intent_is_rechecked(self):
        plan = self.preview(); original_emit = cleanup.emit
        def emit(handle, value):
            original_emit(handle, value)
            if value['event'] == 'delete_intent':
                self.world['opened'].append(str(self.db / 'logs_2.sqlite'))
        with patch.object(cleanup, 'emit', emit):
            self.assertEqual(self.apply(plan)['deleted_files'], 0)

    def test_parent_replaced_after_final_stat_refused_by_dirfd(self):
        plan = self.preview(); real_stamp = cleanup.stamp; original_emit = cleanup.emit
        intent = [False]; moved = [False]
        def emit(handle, value):
            original_emit(handle, value)
            if value['event'] == 'delete_intent':
                intent[0] = True
        def stamp(path, **kwargs):
            value = real_stamp(path, **kwargs)
            if intent[0] and path == self.db / 'logs_2.sqlite' and not moved[0]:
                moved[0] = True
                self.db.rename(self.app / 'moved-db')
                self.db.symlink_to(self.app / 'moved-db')
            return value
        with patch.object(cleanup, 'emit', emit), patch.object(cleanup, 'stamp', stamp):
            self.assertEqual(self.apply(plan)['deleted_files'], 0)
        self.assertTrue((self.app / 'moved-db/logs_2.sqlite').exists())

    def test_disabled_schedule_never_observes_or_deletes(self):
        policy = self.app / 'policy.json'; policy.write_text('{"enabled":false}')
        with patch.object(cleanup, 'preview', side_effect=AssertionError('must not scan')):
            self.assertEqual(cleanup.scheduled(policy)['event'], 'disabled')

    def test_schedule_requires_valid_guard_and_exact_source(self):
        cleanup.JANITOR.mkdir()
        home = cleanup.JANITOR / 'ccc-batch-logs'; home.mkdir(); (home / 'history').mkdir()
        (home / 'sweep.lock').touch()
        (cleanup.JANITOR / 'config.env').write_text('MODE=apply\n')
        (cleanup.JANITOR / 'guard-state.json').write_text(json.dumps({
            'observed_at': cleanup.datetime.datetime.now(cleanup.datetime.timezone.utc).isoformat(),
            'health': 'healthy'}))
        policy = {'version': 1, 'enabled': True, 'app': str(self.app),
                  'source_sha256': cleanup.digest(Path(cleanup.__file__).read_bytes()),
                  'idle_hours': 168, 'pressure_idle_hours': 24, 'min_free_gib': 10, 'max_jobs': 32}
        path = home / 'policy.json'; path.write_text(json.dumps(policy))
        with patch.object(cleanup, 'APP', self.app):
            (cleanup.JANITOR / 'DISABLED').touch()
            self.assertEqual(cleanup.scheduled(path)['deleted_files'], 0)
            (cleanup.JANITOR / 'DISABLED').unlink()
            with patch.object(cleanup, 'preview', return_value=self.preview()) as preview:
                with patch.object(cleanup, 'apply', return_value={'deleted_files': 0}) as apply:
                    cleanup.scheduled(path)
            self.assertEqual(preview.call_count, 1)
            self.assertEqual(apply.call_count, 1)
            policy['source_sha256'] = 'changed'; path.write_text(json.dumps(policy))
            with self.assertRaises(ValueError):
                cleanup.scheduled(path)


if __name__ == '__main__':
    unittest.main()

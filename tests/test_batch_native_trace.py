"""Per-original-process diagnostics; no native execution or external requests."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
import uuid
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_batch_argv_initial as argv_fixtures
from tests import test_workspace_batch as fixtures


class NativeTraceTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp
    prepare = argv_fixtures.ArgvInitialTests.prepare

    def test_default_argv_unchanged_and_opt_in_only_adds_one_log_override(self):
        self.prepare()
        plain = batch.native_launch_argv(self.config, self.worker.job, 0)
        self.assertIsNone(batch.native_trace_directory(self.config, self.worker.job, 0))
        self.worker.job['native_trace_policy'] = batch.NATIVE_TRACE_POLICY
        traced = batch.native_launch_argv(self.config, self.worker.job, 0)
        override = next(i for i, arg in enumerate(traced) if arg.startswith('log_dir='))
        self.assertEqual(traced[override-1], '-c')
        self.assertEqual(traced[:override-1] + traced[override+1:], plain)
        self.assertEqual(traced[-1], batch.PROMPT)
        directory = Path(json.loads(traced[override].split('=', 1)[1]))
        self.assertFalse(directory.is_relative_to(batch.working_directory(self.config, self.worker.job['id'], 0)))
        self.assertEqual(directory.name, self.slot['launch_id'])
        self.worker.job['slots'][1]['launch_id'] = str(uuid.uuid4())
        self.assertNotEqual(directory, batch.native_trace_directory(self.config, self.worker.job, 1))

    def test_invalid_policy_launch_and_symlink_destination_are_rejected(self):
        self.prepare()
        self.worker.job['native_trace_policy'] = 'unknown'
        with self.assertRaises(RuntimeError):
            batch.native_launch_argv(self.config, self.worker.job, 0)
        self.worker.job['native_trace_policy'] = batch.NATIVE_TRACE_POLICY
        original = self.slot['launch_id']
        self.slot['launch_id'] = '../escape'
        with self.assertRaises((ValueError, RuntimeError)):
            batch.native_launch_argv(self.config, self.worker.job, 0)
        self.slot['launch_id'] = original
        (self.worker.path.parent/'native-trace').symlink_to(self.root)
        with self.assertRaises(RuntimeError):
            batch.native_launch_argv(self.config, self.worker.job, 0)

    def test_start_persists_policy_only_for_explicit_new_native_job(self):
        original = batch.job_path(self.config, self.job['job_id']).read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'original batch preserved'):
            batch.start(self.config, self.wid, client=self.client, launch=False,
                        private_check=True, _native_trace=True)
        self.assertEqual(batch.job_path(self.config, self.job['job_id']).read_bytes(), original)
        self.wid = str(uuid.uuid4())
        result = batch.start(self.config, self.wid, client=self.client, launch=False,
                             private_check=True, _native_trace=True)
        path = batch.job_path(self.config, result['job_id'])
        self.assertEqual(core.load_json(path, {})['native_trace_policy'], batch.NATIVE_TRACE_POLICY)
        again = batch.start(self.config, self.wid, client=self.client, launch=False, private_check=True)
        self.assertEqual(again['job_id'], result['job_id'])
        self.assertEqual(core.load_json(path, {})['native_trace_policy'], batch.NATIVE_TRACE_POLICY)

    def test_unsupported_modes_never_create_trace_job(self):
        for kwargs in ({}, {'access_check':True}, {'private_check':True, '_native_trace':'yes'}):
            kwargs.setdefault('_native_trace', True)
            with self.subTest(kwargs=kwargs), self.assertRaises(RuntimeError):
                batch.start(self.config, self.wid, client=self.client, launch=False, **kwargs)

    def test_both_exec_guards_reject_policy_or_directory_identity_changes(self):
        self.prepare()
        self.event_path.unlink()  # prepare() models a post-exec transcript; this case starts before exec.
        self.worker.job['native_trace_policy'] = batch.NATIVE_TRACE_POLICY
        self.worker.save()
        observed = {}
        def capture(config, job_id, index, record, argv, final_guard, final_authorized):
            self.assertEqual(record['argv'], argv)
            observed.update(record=record, guard=final_guard, authorized=final_authorized)
            for guard in (final_guard, final_authorized):
                self.assertTrue(guard())
                original = self.worker.path.read_bytes()
                changed = json.loads(original)
                changed.pop('native_trace_policy')
                core.atomic_write_json(self.worker.path, changed)
                self.assertFalse(guard())
                self.worker.path.write_bytes(original)
                self.assertTrue(guard())
                directory = Path(record['native_trace_directory'])
                self.assertEqual(argv.count('log_dir='+json.dumps(str(directory))), 1)
                for component in (directory.parent.parent, directory.parent, directory):
                    component.chmod(0o777)
                    self.assertFalse(guard())
                    component.chmod(0o700)
                    self.assertTrue(guard())
                (directory/'preexisting.log').write_text('not this launch')
                self.assertFalse(guard())
                (directory/'preexisting.log').unlink()
                retained = directory.with_name(directory.name+'-retained')
                directory.rename(retained)
                directory.mkdir(mode=0o700)
                self.assertFalse(guard())
                directory.rmdir()
                retained.rename(directory)
        with patch.dict(os.environ, {'CMUX_SURFACE_ID':self.slot['surface_id'], 'CMUX_WORKSPACE_ID':self.wid}), \
                patch('ccc_guard_scope.birth', return_value=[1234,5678]), \
                patch.object(batch, '_bootstrap_client', return_value=self.client), \
                patch('ccc_batch_guard.native_binary', return_value=sys.executable), \
                patch.object(batch, '_exec_claimed_argv', side_effect=capture), \
                patch.object(os, 'execv') as execute:
            batch._launch_initial_registered(self.config, self.worker.job, 0,
                self.slot['launch_id'], batch.native_launch_argv(self.config, self.worker.job, 0))
        execute.assert_not_called()
        self.assertEqual(observed['record']['native_trace_policy'], batch.NATIVE_TRACE_POLICY)
        self.assertEqual(len(observed['record']['native_trace_identity']), 3)


if __name__ == '__main__':
    unittest.main()

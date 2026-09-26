"""A new B task is short; existing delivery proofs keep their original text."""
import os
import unittest
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures


class BatchPromptPolicyTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def test_new_batch_sends_only_its_persisted_short_prompt(self):
        self.assertEqual(self.worker.job['initial_prompt'], batch.PROMPT)
        self.assertNotEqual(batch.PROMPT, batch.LEGACY_PROMPT)
        self.worker.job['slots'] = self.worker.job['slots'][:1]
        self.worker.save()
        for _ in range(3):
            self.now += 1
            self.worker.step()
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.worker.job['slots'][0]['phase'], 'confirmed')

    def test_old_job_without_prompt_policy_keeps_original_submission_and_proof(self):
        self.worker.job.pop('initial_prompt')
        self.worker.job.pop('cwd_policy')
        self.worker.job.pop('name_policy')
        self.worker.job['slots'] = self.worker.job['slots'][:1]
        self.worker.save()
        self.assertEqual(batch.job_prompt(self.worker.job), 'show me u power')
        for _ in range(3):
            self.now += 1
            self.worker.step()
        slot = self.worker.job['slots'][0]
        self.assertEqual(slot['phase'], 'confirmed')
        self.assertEqual(len(self.client.sent), 1)
        self.assertNotIn('--cd', batch.native_launch_argv(self.config, self.worker.job, 0))

    def test_prompt_policy_is_not_replaced_when_resuming_an_old_job(self):
        self.worker.job.pop('initial_prompt')
        self.worker.save()
        resumed = batch.start(self.config, self.wid, client=self.client, launch=False)
        self.assertEqual(resumed['job_id'], self.worker.job['id'])
        self.assertNotIn('initial_prompt', core.load_json(self.worker.path, {}))

    def test_existing_entry_resumes_an_opted_in_job_without_changing_its_policy(self):
        saved = core.load_json(self.worker.path, {})
        resumed = batch.start(self.config, self.wid, client=self.client, launch=False)
        self.assertEqual(resumed['job_id'], saved['id'])
        self.assertEqual(resumed['startup_mode'], 'private_check')
        self.assertEqual(core.load_json(self.worker.path, {})['initial_prompt'], batch.PROMPT)

    def test_unknown_prompt_policy_is_never_silently_reinterpreted(self):
        for value in ('operator task', '', None, 7):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                batch.job_prompt({'initial_prompt': value})

    def test_short_bootstrap_executes_native_after_registering_the_same_slot(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        with patch.dict(os.environ, CMUX_SURFACE_ID=slot['surface_id'], CMUX_WORKSPACE_ID=self.wid), \
                patch.object(batch.os, 'execv') as execute:
            batch.launch_registered(self.config, self.worker.job['id'], 0, slot['launch_id'])
        argv = execute.call_args.args[1]
        self.assertEqual(execute.call_args.args[0], '/test/native/codex')
        self.assertEqual(argv[0], '/test/native/codex')
        self.assertIn('--cd', argv)
        self.assertNotIn('--remote', argv)
        self.assertNotIn(batch.PROMPT, argv)
        self.assertEqual(len(self.client.sent), 0)

    def test_stale_launch_or_pause_prevents_exec_and_does_not_rearm(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        with patch.dict(os.environ, CMUX_SURFACE_ID=slot['surface_id'], CMUX_WORKSPACE_ID=self.wid), \
                patch.object(batch.os, 'execv') as execute:
            with self.assertRaises(RuntimeError):
                batch.launch_registered(self.config, self.worker.job['id'], 0, 'different-launch')
            self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
            with self.assertRaises(RuntimeError):
                batch.launch_registered(self.config, self.worker.job['id'], 0, slot['launch_id'])
        execute.assert_not_called()
        self.assertTrue(self.store.load()['workspace_rules'][0]['paused'])


class ExistingBatchModeTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp
    finish = fixtures.WorkspaceBatchTests.finish
    private_check = False

    def test_default_B_keeps_all_50_slots_original_prompt_and_inherited_directory(self):
        for key in ('cwd_policy', 'name_policy', 'initial_prompt'):
            self.assertNotIn(key, self.worker.job)
        self.assertEqual(batch.job_prompt(self.worker.job), 'show me u power')
        self.assertNotIn('--cd', batch.native_launch_argv(self.config, self.worker.job, 0))
        counts = self.finish()
        self.assertEqual(counts['started'], 50)
        self.assertEqual(counts['total'], 50)
        self.assertEqual(len(self.client.sent), 50)
        self.assertEqual(self.client.rename_sent, [])
        self.assertFalse((self.worker.path.parent / 'work').exists())

    def test_opt_in_cannot_rewrite_an_existing_unfinished_default_batch(self):
        original_id = self.worker.job['id']
        result = batch.start(self.config, self.wid, client=self.client, launch=False, private_check=True)
        self.assertEqual(result['job_id'], original_id)
        self.assertEqual(result['startup_mode'], 'existing')
        saved = core.load_json(self.worker.path, {})
        self.assertNotIn('cwd_policy', saved)
        self.assertEqual(batch.job_prompt(saved), batch.LEGACY_PROMPT)

    def test_default_cli_keeps_existing_behavior_and_experiment_requires_flag(self):
        parser = core.build_parser()
        self.assertFalse(parser.parse_args(['batch-workspace', self.wid]).private_check)
        self.assertTrue(parser.parse_args(['batch-workspace', self.wid, '--private-check']).private_check)

    def test_non_boolean_mode_is_rejected_without_changing_the_job(self):
        before = self.worker.path.read_bytes()
        for value in ('true', 1, None):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                batch.start(self.config, self.wid, client=self.client, launch=False, private_check=value)
        self.assertEqual(self.worker.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()

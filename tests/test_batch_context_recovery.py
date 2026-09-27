"""B startup authorization must survive current native context envelopes."""
import copy
import json
import os
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_batch_authorization_recovery as recovery
from tests import test_workspace_batch as fixtures


CONTEXT = ('# AGENTS.md instructions\n\n<INSTRUCTIONS>local startup rules</INSTRUCTIONS>\n'
           '<environment_context><cwd>/fixture</cwd></environment_context>')


class BatchContextRecoveryTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp
    submitted = recovery.BatchAuthorizationTests.submitted
    rows = recovery.BatchAuthorizationTests.rows
    append = recovery.BatchAuthorizationTests.append

    def legacy_block(self, slot):
        with patch.object(batch, '_startup_context', return_value=False):
            self.assertFalse(self.worker._confirm(slot))
        proof = slot['confirmation']
        self.assertEqual(proof['blocked'], 'different user prompt')
        proof.pop('context_parser_version', None)
        self.worker.save()
        return copy.deepcopy(proof)

    def restored(self):
        worker = batch.BatchWorker(self.config, self.job['job_id'], client=self.client, queue=self.client)
        self.addCleanup(worker.cache.close)
        return worker, worker.job['slots'][0]

    def test_pathless_native_context_releases_only_its_startup_hold(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot, CONTEXT))
        self.worker._advance(slot)
        self.assertEqual(slot['phase'], 'confirmed')
        self.assertFalse(self.store.load()['workspace_rules'][0]['batch_start_holds'])
        self.assertEqual(self.client.sent, [])

    def test_old_saved_false_block_is_rechecked_without_replaying_prompt(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot, CONTEXT))
        previous = self.legacy_block(slot)
        worker, slot = self.restored()
        worker._advance(slot, confirmation_only=True)
        self.assertEqual(slot['phase'], 'confirmed')
        self.assertFalse(self.store.load()['workspace_rules'][0]['batch_start_holds'])
        self.assertEqual(slot['confirmation']['legacy_context_revalidation']['previous'], previous)
        self.assertEqual(self.client.sent, [])

    def test_recheck_preserves_manual_pause_and_exclusion(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot, CONTEXT))
        self.legacy_block(slot)
        def manual(config):
            rule = config['workspace_rules'][0]
            rule['paused'] = True
            rule['excluded_surface_ids'] = [slot['surface_id']]
            rule['excluded_surface_reasons'] = {slot['surface_id']: {'reason': 'operator'}}
        self.store.mutate(manual)
        worker, slot = self.restored()
        worker._advance(slot, confirmation_only=True)
        rule = self.store.load()['workspace_rules'][0]
        self.assertEqual(slot['phase'], 'confirmed')
        self.assertTrue(rule['paused'])
        self.assertEqual(rule['excluded_surface_ids'], [slot['surface_id']])
        self.assertEqual(rule['excluded_surface_reasons'][slot['surface_id']], {'reason': 'operator'})
        self.assertEqual(self.client.sent, [])

    def test_large_legacy_recheck_is_incremental_and_survives_worker_restart(self):
        slot = self.submitted()
        context = CONTEXT.replace('local startup rules', '中文' * 24000)
        self.append(slot, self.rows(slot, context))
        self.legacy_block(slot)
        worker, slot = self.restored()
        with patch.object(batch, 'CONFIRM_READ_BYTES', 8191):
            self.assertFalse(worker._confirm(slot))
            worker.save()
            worker, slot = self.restored()
            for _ in range(40):
                if worker._confirm(slot):
                    break
        self.assertTrue(slot['confirmation'].get('confirmed'))
        self.assertIn('legacy_context_revalidation', slot['confirmation'])
        self.assertEqual(self.client.sent, [])

    def test_recheck_waits_for_original_prompt_appended_after_context(self):
        slot = self.submitted()
        rows = self.rows(slot, CONTEXT)
        self.append(slot, rows[:-1])
        self.legacy_block(slot)
        worker, slot = self.restored()
        self.assertFalse(worker._confirm(slot))
        worker.save()
        self.append(slot, rows[-1:])
        worker, slot = self.restored()
        self.assertTrue(worker._confirm(slot))

    def test_invalid_saved_recheck_cannot_grant_a_startup_hold(self):
        slot = self.submitted()
        rows = self.rows(slot, CONTEXT)
        self.append(slot, rows[:-1])
        self.legacy_block(slot)
        worker, slot = self.restored()
        self.assertFalse(worker._confirm(slot))
        old = copy.deepcopy(slot['confirmation'])
        self.append(slot, rows[-1:])
        cursor = old['context_recheck']
        invalid = [None, [], {}, *({**cursor, **change} for change in (
            {'origin': {}}, {'session_id': 'another'}, {'expected_task_id': 'another'},
            {'context_parser_version': 0}, {'identity': [0, 0]}, {'offset': -1},
            {'offset': '0'}, {'offset': False}, {'confirmed': True}))]
        for value in invalid:
            with self.subTest(cursor=value):
                slot['confirmation'] = {**copy.deepcopy(old), 'context_recheck': copy.deepcopy(value)}
                worker._advance(slot, confirmation_only=True)
                self.assertNotEqual(slot['phase'], 'confirmed')
                self.assertIn('context_recheck_error', slot['confirmation'])
                self.assertTrue(core.batch_start_hold(self.store.load()['workspace_rules'][0], slot['surface_id']))
        self.assertEqual(self.client.sent, [])

    def test_real_operator_prompt_is_not_reinterpreted_as_startup_context(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot, CONTEXT + '\nDo this different task'))
        self.legacy_block(slot)
        worker, slot = self.restored()
        self.assertFalse(worker._confirm(slot))
        self.assertTrue(core.batch_start_hold(self.store.load()['workspace_rules'][0], slot['surface_id']))
        with patch.object(Path, 'open', side_effect=AssertionError('unchanged rejected proof reread')):
            self.assertFalse(worker._confirm(slot))

    def test_explicit_user_event_with_context_like_text_stays_an_operator_task(self):
        slot = self.submitted()
        rows = self.rows(slot)
        rows.insert(1, {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': CONTEXT}})
        self.append(slot, rows)
        self.assertFalse(self.worker._confirm(slot))
        self.assertEqual(slot['confirmation']['blocked'], 'different user prompt')

    def test_old_block_never_survives_as_a_grant_to_another_inode(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot, CONTEXT))
        self.legacy_block(slot)
        path = Path(slot['transcript'])
        data = path.read_bytes()
        path.rename(path.with_suffix('.old'))
        path.write_bytes(data)
        worker, slot = self.restored()
        self.assertFalse(worker._confirm(slot))
        self.assertIn('transcript', slot['confirmation']['blocked'])

    def test_old_block_never_rechecks_a_truncated_transcript(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot, CONTEXT))
        self.legacy_block(slot)
        Path(slot['transcript']).write_text('')
        worker, slot = self.restored()
        self.assertFalse(worker._confirm(slot))
        self.assertIn('truncated', slot['confirmation']['blocked'])

    def test_old_block_never_grants_a_different_original_session(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot, CONTEXT))
        self.legacy_block(slot)
        worker, slot = self.restored()
        slot['session_id'] = str(uuid.uuid4())
        self.assertFalse(worker._confirm(slot))
        self.assertIn('session', slot['confirmation']['blocked'])

    def test_recheck_keeps_original_task_identity(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot, CONTEXT))
        self.legacy_block(slot)
        worker, slot = self.restored()
        slot['confirmation']['task_id'] = 'another-original-task'
        self.assertFalse(worker._confirm(slot))
        self.assertTrue(core.batch_start_hold(self.store.load()['workspace_rules'][0], slot['surface_id']))

    def test_supported_titles_still_require_a_complete_context_envelope(self):
        for text in (CONTEXT, CONTEXT.replace('instructions\n', 'instructions for /project\n')):
            self.assertTrue(batch._startup_context(text))
        for text in (CONTEXT + '\noperator input', CONTEXT.replace('</INSTRUCTIONS>', ''),
                     '# AGENTS.md instructions\nDo something', CONTEXT.replace('instructions\n', 'instructions extra\n')):
            self.assertFalse(batch._startup_context(text))


class BatchHistoryCostTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def test_finished_history_does_not_multiply_config_validation_or_worker_caches(self):
        rules = []
        old_files = {}
        for _ in range(64):
            wid, jid, sid = (str(uuid.uuid4()) for _ in range(3))
            rule = {'workspace_id': wid, 'enabled': True, 'last_batch_id': jid, 'active_batch_id': jid}
            path = batch.job_path(self.config, jid)
            core.atomic_write_json(path, {'id': jid, 'workspace_id': wid, 'status': 'complete',
                                         'slots': [{'index': 0, 'surface_id': sid, 'phase': 'confirmed'}]})
            old_files[path] = path.read_bytes()
            rules.append(rule)
        self.store.mutate(lambda c: c['workspace_rules'].extend(rules))
        reconciler = batch.BatchReconciler(self.config, self.client, launch=False)
        self.addCleanup(lambda: [worker.cache.close() for worker in reconciler.workers.values()])
        original = core.ConfigStore.load
        reads = []
        def counted(store):
            reads.append(store.path)
            return original(store)
        with patch.object(core.ConfigStore, 'load', counted):
            reconciler.cycle()
            reconciler.cycle()
        self.assertLessEqual(len(reads), 6, 'unchanged history must not revalidate the entire config per job')
        self.assertLessEqual(len(reconciler.workers), 1)
        self.assertTrue(all(path.read_bytes() == data for path, data in old_files.items()))
        self.assertEqual(self.client.sent, [])

    def test_panel_reads_only_the_requested_live_workspaces(self):
        self.worker.step()
        wid, jid = str(uuid.uuid4()), str(uuid.uuid4())
        self.store.mutate(lambda c: c['workspace_rules'].append({'workspace_id': wid, 'last_batch_id': jid}))
        config = self.store.load()
        with patch.object(core, 'load_json', wraps=core.load_json) as load:
            result = batch.snapshots(self.config, config, workspace_ids={self.wid})
        self.assertEqual(set(result), {self.wid})
        self.assertEqual(load.call_count, 1)

    def test_config_replacement_invalidates_cache_even_at_same_time_and_size(self):
        reconciler = batch.BatchReconciler(self.config, self.client, launch=False)
        self.assertFalse(reconciler._config()['global_paused'])
        before = self.config.stat()
        raw = self.config.read_text().replace('"global_paused": false', '"global_paused": true ')
        replacement = self.config.with_suffix('.replacement')
        replacement.write_text(raw)
        self.assertEqual(replacement.stat().st_size, before.st_size)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(self.config)
        self.assertTrue(reconciler._config()['global_paused'])

    def test_config_changed_during_validation_is_not_cached(self):
        reconciler = batch.BatchReconciler(self.config, self.client, launch=False)
        load = reconciler.store.load
        def concurrent_edit():
            value = load()
            self.store.mutate(lambda c: c.update(global_paused=True))
            return value
        with patch.object(reconciler.store, 'load', side_effect=concurrent_edit):
            self.assertFalse(reconciler._config()['global_paused'])
        self.assertIsNone(reconciler._config_cache)
        self.assertTrue(reconciler._config()['global_paused'])


if __name__ == '__main__':
    unittest.main()

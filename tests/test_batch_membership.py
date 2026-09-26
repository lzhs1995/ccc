"""Late topology replies must not strand live B slots or cause replacements."""
import copy
import unittest
from unittest.mock import Mock, patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from ccc_scheduling import SnapshotCache, SnapshotClient
from tests import test_workspace_batch as fixtures


class BatchMembershipTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp

    def cached_client(self, tree):
        cache = SnapshotCache()
        self.addCleanup(cache.close)
        cache.get(("tree",), lambda: tree, ttl=100)
        client = SnapshotClient(self.client, cache)
        self.worker.client = client
        return client

    def test_tree_started_before_creation_cannot_close_a_live_slot(self):
        old = copy.deepcopy(self.client.tree())
        self.worker.step()
        slot = self.worker.job['slots'][0]
        sid = slot['surface_id']
        self.worker.job['slots'] = [slot]
        self.now += 6
        client = self.cached_client(old)
        with patch.object(client, 'fresh_tree', wraps=client.fresh_tree) as fresh:
            self.worker.step()
        fresh.assert_called_once()
        self.assertNotEqual(slot['phase'], 'surface_closed')
        self.assertEqual(self.client.calls, [sid])
        self.assertTrue(core.batch_start_hold(self.store.load()['workspace_rules'][0], sid))

    def test_cached_workspace_absence_needs_new_request(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        self.worker.job['slots'] = [slot]
        self.cached_client({'windows': []})
        self.worker.step()
        self.assertNotEqual(self.worker.job['status'], 'workspace_closed')
        self.assertEqual(len(self.client.calls), 1)

    def test_failed_fresh_read_preserves_slot_and_never_replays_creation(self):
        old = copy.deepcopy(self.client.tree())
        self.worker.step()
        slot = self.worker.job['slots'][0]
        self.worker.job['slots'] = [slot]
        self.now += 6
        client = self.cached_client(old)
        with patch.object(client, 'fresh_tree', side_effect=core.CmuxError('read unavailable')):
            self.assertTrue(self.worker.step())
        self.assertEqual(slot['phase'], 'created')
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.sent, [])

    def false_closed(self, *, submitted=False):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        if submitted:
            self.worker._advance(slot)
            self.assertEqual(len(self.client.sent), 1)
        slot.update(phase='surface_closed', error='old topology said absent')
        self.worker.job.update(status='needs_attention', slots=[slot])
        self.worker.save()
        return slot

    def test_original_unsubmitted_slot_can_finish_without_another_tab(self):
        slot = self.false_closed()
        self.worker.step()
        self.worker.step()
        self.assertEqual(slot['phase'], 'confirmed')
        self.assertEqual(self.client.calls, [slot['surface_id']])
        self.assertEqual(self.client.sent, self.client.calls)
        self.assertFalse(self.store.load()['workspace_rules'][0]['batch_start_holds'])

    def test_original_submitted_slot_only_confirms_its_recorded_prompt(self):
        slot = self.false_closed(submitted=True)
        self.worker.step()
        self.assertEqual(slot['phase'], 'confirmed')
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(len(self.client.calls), 1)

    def test_recovery_does_not_override_a_changed_receipt_or_workspace(self):
        slot = self.false_closed()
        path = self.worker.path.parent / 'surface-0.json'
        original = core.load_json(path, {})
        for change in ({'launch_id': 'different'}, {'surface_id': 'different'}, {'workspace_id': 'different'}):
            with self.subTest(change=change):
                core.atomic_write_json(path, {**original, **change})
                self.worker.step()
                self.assertEqual(slot['phase'], 'surface_closed')
                self.assertEqual(self.client.sent, [])
        core.atomic_write_json(path, original)
        with patch.object(self.client, 'tree', return_value={'windows': []}):
            self.worker.step()
        self.assertEqual(slot['phase'], 'surface_closed')

    def test_manual_exclusion_or_pool_pause_still_prevents_original_prompt(self):
        slot = self.false_closed()
        self.store.mutate(lambda c: c['workspace_rules'][0]['excluded_surface_ids'].append(slot['surface_id']))
        self.worker.step()
        self.assertEqual(slot['phase'], 'blocked')
        self.assertEqual(self.client.sent, [])
        slot['phase'] = 'surface_closed'
        self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
        self.worker.step()
        self.assertEqual(slot['phase'], 'surface_closed')
        self.assertEqual(self.client.sent, [])

    def test_reconciler_revives_original_job_but_does_not_create_or_send(self):
        slot = self.false_closed()
        reconciler = batch.BatchReconciler(self.config, self.client)
        self.addCleanup(lambda: [worker.cache.close() for worker in reconciler.workers.values()])
        with patch.object(batch, '_launch') as launch:
            reconciler.cycle()
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1]['id'], self.job['job_id'])
        restored = core.load_json(self.worker.path, {})
        self.assertEqual(restored['slots'][0]['surface_id'], slot['surface_id'])
        self.assertEqual(restored['slots'][0]['phase'], 'created')
        self.assertEqual(self.client.sent, [])
        self.assertEqual(len(self.client.calls), 1)

    def test_reconciler_cannot_leave_live_original_closed_using_a_precreation_cache(self):
        old = copy.deepcopy(self.client.tree())
        slot = self.false_closed()
        client = self.cached_client(old)
        reconciler = batch.BatchReconciler(self.config, client)
        self.addCleanup(lambda: [w.cache.close() for w in reconciler.workers.values()])
        with patch.object(batch, '_launch') as launch:
            reconciler.cycle()
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1]['id'], self.job['job_id'])
        restored = core.load_json(self.worker.path, {})
        self.assertEqual(restored['slots'][0]['phase'], 'created')
        self.assertEqual(restored['slots'][0]['surface_id'], slot['surface_id'])
        self.assertEqual(self.client.sent, [])

    def test_reconciler_failed_fresh_read_preserves_original_closure_and_hold(self):
        self.false_closed()
        client = self.cached_client(self.client.tree())
        reconciler = batch.BatchReconciler(self.config, client)
        self.addCleanup(lambda: [w.cache.close() for w in reconciler.workers.values()])
        old = self.worker.path.read_bytes()
        with patch.object(client, 'fresh_tree', side_effect=core.CmuxError('unavailable')), \
                patch.object(batch, '_launch') as launch:
            reconciler.cycle()
        launch.assert_not_called()
        self.assertEqual(self.worker.path.read_bytes(), old)

    def test_reconciliation_cannot_revive_old_job_between_settlement_and_new_authorization(self):
        slot = self.false_closed()
        old = self.worker.path.read_bytes()
        absent = copy.deepcopy(self.client.tree())
        absent['windows'][0]['workspaces'][0]['panes'][0]['surfaces'] = []
        reader = Mock()
        reader.tree.return_value = absent
        reconciler = batch.BatchReconciler(self.config, self.client)
        self.addCleanup(lambda: [w.cache.close() for w in reconciler.workers.values()])
        mutate = core.ConfigStore.mutate
        checked = []
        def at_authorization(store, callback):
            if callback.__name__ == 'authorize':
                # The old launch receipt and a present surface reappear just
                # after settlement, while the old authorization is still live.
                checked.append(True)
                reconciler.cycle()
            return mutate(store, callback)
        with patch.object(core.ConfigStore, 'mutate', at_authorization), patch.object(batch, '_launch') as launch:
            result = batch.start(self.config, self.wid, client=reader, launch=False)
        self.assertEqual(checked, [True])
        launch.assert_not_called()
        self.assertNotEqual(result['job_id'], self.job['job_id'])
        self.assertEqual(self.worker.path.read_bytes(), old)
        self.assertEqual(self.client.calls, [slot['surface_id']])

    def test_repeat_b_does_not_overwrite_progress_owned_by_running_worker(self):
        self.worker.step()
        self.worker.save()
        previous = copy.deepcopy(self.worker.job)
        updated = {**previous, 'progress_marker': 'written by active worker'}
        load = core.load_json
        first = [True]
        def race(path, *args, **kwargs):
            if path == self.worker.path and first[0]:
                first[0] = False
                core.atomic_write_json(path, updated)
                return previous
            return load(path, *args, **kwargs)
        with core.FileLock(self.worker.path.parent / 'worker.lock'), patch.object(core, 'load_json', race):
            result = batch.start(self.config, self.wid, client=self.client, launch=False)
        self.assertEqual(result['job_id'], self.job['job_id'])
        self.assertEqual(load(self.worker.path, {}).get('progress_marker'), updated['progress_marker'])
        self.assertEqual(len(self.client.calls), 1)

    def test_repeat_b_preserves_progress_completed_before_it_acquires_ownership(self):
        self.worker.step()
        self.worker.save()
        previous = copy.deepcopy(self.worker.job)
        updated = {**previous, 'progress_marker': 'last worker write before unlock'}
        load = core.load_json
        first = [True]
        def race(path, *args, **kwargs):
            if path == self.worker.path and first[0]:
                first[0] = False
                core.atomic_write_json(path, updated)
                return previous
            return load(path, *args, **kwargs)
        with patch.object(core, 'load_json', race):
            result = batch.start(self.config, self.wid, client=self.client, launch=False)
        self.assertEqual(result['job_id'], self.job['job_id'])
        self.assertEqual(load(self.worker.path, {}).get('progress_marker'), updated['progress_marker'])

    def test_final_status_does_not_allow_another_batch_while_original_worker_still_owns_it(self):
        self.worker.job['status'] = 'complete'
        self.worker.save()
        old = self.worker.path.read_bytes()
        with core.FileLock(self.worker.path.parent / 'worker.lock'):
            result = batch.start(self.config, self.wid, client=self.client, launch=False)
        self.assertEqual(result['job_id'], self.job['job_id'])
        self.assertEqual(self.worker.path.read_bytes(), old)

    def test_reconciler_does_not_restore_old_batch_after_its_authorization_snapshot_changes(self):
        self.false_closed()
        reconciler = batch.BatchReconciler(self.config, self.client)
        self.addCleanup(lambda: [w.cache.close() for w in reconciler.workers.values()])
        def changed_authorization(path, config):
            self.store.mutate(lambda c: c['workspace_rules'][0].pop('active_batch_id'))
            return [self.job['job_id']]
        with patch.object(batch, 'relevant_job_ids', side_effect=changed_authorization), \
                patch.object(batch, '_launch') as launch:
            reconciler.cycle()
        launch.assert_not_called()
        restored = core.load_json(self.worker.path, {})
        self.assertEqual(restored['slots'][0]['phase'], 'surface_closed')
        self.assertEqual(self.client.sent, [])

    def test_b_reuses_a_live_falsely_closed_tab_before_allowing_another_batch(self):
        slot = self.false_closed()
        result = batch.start(self.config, self.worker.job['workspace_id'], client=self.client, launch=False)
        self.assertEqual(result['job_id'], self.job['job_id'])
        self.assertEqual(self.client.calls, [slot['surface_id']])
        self.assertEqual(self.client.sent, [])

    def test_explicit_b_can_start_a_new_batch_after_confirmed_closure_without_rewriting_history(self):
        self.false_closed()
        old = self.worker.path.read_bytes()
        tree = copy.deepcopy(self.client.tree())
        tree['windows'][0]['workspaces'][0]['panes'][0]['surfaces'] = []
        with patch.object(self.client, 'tree', return_value=tree):
            result = batch.start(self.config, self.worker.job['workspace_id'], client=self.client, launch=False)
        self.assertNotEqual(result['job_id'], self.job['job_id'])
        self.assertEqual(result['total'], 50)
        self.assertEqual(self.worker.path.read_bytes(), old)
        self.assertEqual(self.client.sent, [])
        # A second click before this batch progresses must reuse the new job.
        again = batch.start(self.config, self.worker.job['workspace_id'], client=self.client, launch=False)
        self.assertEqual(again['job_id'], result['job_id'])

    def test_b_keeps_old_job_on_uncertain_topology_or_pending_delivery(self):
        slot = self.false_closed()
        with patch.object(self.client, 'tree', side_effect=core.CmuxError('unavailable')):
            with patch.object(batch, '_client', return_value=self.client):
                result = batch.start(self.config, self.worker.job['workspace_id'], launch=False)
        self.assertEqual(result['job_id'], self.job['job_id'])
        slot.update(phase='uncertain', submit_at=self.now)
        self.worker.save()
        result = batch.start(self.config, self.worker.job['workspace_id'], client=self.client, launch=False)
        self.assertEqual(result['job_id'], self.job['job_id'])


if __name__ == '__main__':
    unittest.main()

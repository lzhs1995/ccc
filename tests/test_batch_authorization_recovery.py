"""Real startup shapes, durable authorization and bounded fleet creation."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core
from tests import test_workspace_batch as fixtures
BatchFixture = fixtures.BatchFixture


class BatchAuthorizationTests(unittest.TestCase):
    setUp = fixtures.WorkspaceBatchTests.setUp
    finish = fixtures.WorkspaceBatchTests.finish

    def submitted(self):
        self.worker.step()
        slot = self.worker.job['slots'][0]
        with patch.object(self.client, 'send_text'):
            self.worker._advance(slot)
        return slot

    def rows(self, slot, context=None):
        stamp = datetime.fromtimestamp(slot['submit_at'], timezone.utc).isoformat(timespec='milliseconds')
        rows = [{'type': 'event_msg', 'timestamp': stamp, 'payload': {'type': 'task_started', 'turn_id': 'original'}}]
        for text in [context, batch.PROMPT]:
            if text is not None:
                rows.append({'type': 'response_item', 'payload': {'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}})
        return rows

    def append(self, slot, rows):
        with Path(slot['transcript']).open('a') as out:
            out.write('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n')

    def test_combined_agents_and_environment_releases_real_pool_authorization(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot, '# AGENTS.md instructions for /project\n<INSTRUCTIONS>rules</INSTRUCTIONS>\n<environment_context>cwd</environment_context>'))
        self.worker._advance(slot)
        self.assertEqual(slot['phase'], 'confirmed')
        rule = self.store.load()['workspace_rules'][0]
        self.assertEqual(rule['excluded_surface_ids'], [])
        self.assertFalse(rule['batch_start_holds'])
        self.assertEqual(slot['confirmation']['task_id'], 'original')

    def test_large_split_utf8_context_survives_restart_without_rereading(self):
        slot = self.submitted()
        context = '# AGENTS.md instructions for /project\n<INSTRUCTIONS>' + ('中文规则' * 20000) + '</INSTRUCTIONS>\n<environment_context>cwd</environment_context>'
        data = ('\n'.join(json.dumps(row, ensure_ascii=False) for row in self.rows(slot, context)) + '\n').encode()
        path = Path(slot['transcript'])
        with path.open('ab') as out:
            out.write(data[:-3])
        with patch.object(batch, 'CONFIRM_READ_BYTES', 8191):
            for _ in range(80):
                self.assertFalse(self.worker._confirm(slot))
                if slot['confirmation']['offset'] == path.stat().st_size:
                    break
        self.worker.save()
        with patch.object(Path, 'open', side_effect=AssertionError('unchanged transcript was reread')):
            self.assertFalse(self.worker._confirm(slot))
        restored = batch.BatchWorker(self.config, self.job['job_id'], client=self.client, queue=self.client)
        self.addCleanup(restored.cache.close)
        with path.open('ab') as out:
            out.write(data[-3:])
        self.assertTrue(restored._confirm(restored.job['slots'][0]))

    def test_context_with_appended_human_text_and_second_task_are_vetoed(self):
        for mode in ['human', 'task', 'identity']:
            with self.subTest(mode=mode):
                slot = self.submitted()
                rows = self.rows(slot, '# AGENTS.md instructions for /project\n<INSTRUCTIONS>rules</INSTRUCTIONS>\n<environment_context>cwd</environment_context>' + ('\nreal human request' if mode == 'human' else ''))
                if mode == 'task':
                    rows.insert(1, copy.deepcopy(rows[0]))
                    rows[1]['payload']['turn_id'] = 'different'
                if mode == 'identity':
                    slot['session_id'] = str(uuid.uuid4())
                self.append(slot, rows)
                self.assertFalse(self.worker._confirm(slot))
                self.assertTrue(core.batch_start_hold(self.store.load()['workspace_rules'][0], slot['surface_id']))
                # Each independent assertion starts from the same submitted slot.
                slot.pop('confirmation', None)
                slot['transcript_offset'] = Path(slot['transcript']).stat().st_size

    def test_top_timeout_cannot_prevent_confirmation_or_hold_release(self):
        slot = self.submitted()
        self.worker.job['slots'] = [slot]
        self.append(slot, self.rows(slot))
        with patch.object(self.client, 'top_all', side_effect=core.CmuxError('system.top timed out')):
            self.assertFalse(self.worker.step())
        self.assertEqual(self.worker.job['status'], 'complete')
        self.assertFalse(self.store.load()['workspace_rules'][0]['batch_start_holds'])
        self.assertEqual(self.client.sent, [])

    def test_crash_after_proof_before_release_is_idempotently_repaired(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot))
        with patch.object(self.worker, '_release', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                self.worker._advance(slot)
        self.assertEqual(core.load_json(self.worker.path, {})['slots'][0]['phase'], 'confirmed')
        restored = batch.BatchWorker(self.config, self.job['job_id'], client=self.client, queue=self.client)
        self.addCleanup(restored.cache.close)
        restored._advance(restored.job['slots'][0])
        self.assertFalse(self.store.load()['workspace_rules'][0]['batch_start_holds'])
        self.assertEqual(self.client.sent, [])

    def test_historical_legacy_hold_reconciles_without_top_or_overriding_pause(self):
        slot = self.submitted()
        self.append(slot, self.rows(slot))
        self.worker.job['status'] = 'partial'
        self.worker.save()
        sid, jid = slot['surface_id'], self.job['job_id']
        def legacy(c):
            r = c['workspace_rules'][0]
            r.update(paused=True, last_batch_id=str(uuid.uuid4()))
            r.pop('active_batch_id', None)
            r['batch_start_holds'].clear()
            r['excluded_surface_ids'] = [sid, 'manual']
            r['excluded_surface_reasons'] = {sid: f'batch:{jid}:initial', 'manual': {'reason': 'manual exclusion'}}
        self.store.mutate(legacy)
        reconciler = batch.BatchReconciler(self.config, self.client, launch=False)
        with patch.object(self.client, 'top_all', side_effect=AssertionError('no inventory needed')):
            reconciler.cycle()
        for worker in reconciler.workers.values():
            worker.cache.close()
        rule = self.store.load()['workspace_rules'][0]
        self.assertTrue(rule['paused'])
        self.assertEqual(rule['excluded_surface_ids'], ['manual'])
        self.assertNotIn('active_batch_id', rule)
        self.assertEqual(core.load_json(self.worker.path, {})['slots'][0]['phase'], 'confirmed')

    def test_manual_exclusion_written_during_start_is_not_removed(self):
        slot = self.submitted()
        sid = slot['surface_id']
        def exclude(c):
            r = c['workspace_rules'][0]
            r['excluded_surface_ids'].append(sid)
            r.setdefault('excluded_surface_reasons', {})[sid] = {'reason': 'manual exclusion'}
        self.store.mutate(exclude)
        self.append(slot, self.rows(slot))
        self.worker._advance(slot)
        self.assertEqual(self.store.load()['workspace_rules'][0]['excluded_surface_ids'], [sid])

    def test_w_is_fast_idempotent_and_keeps_P_cancellation(self):
        self.store.mutate(lambda c: c['workspace_rules'][0].update(paused=True, batch_cancelled_at=1))
        with patch.object(batch, '_client', side_effect=AssertionError('w must not query top or cmux')):
            for _ in range(2):
                batch.authorize_workspace(self.config, self.wid)
        rule = self.store.load()['workspace_rules'][0]
        self.assertTrue(rule['paused'])
        self.assertEqual(rule['batch_cancelled_at'], 1)
        self.assertEqual(len(self.store.load()['workspace_rules']), 1)

    def test_slow_start_is_not_abandoned_at_25_or_360_seconds(self):
        self.worker.step()
        self.client.frame_options = {'composer': 'human draft'}
        self.now += 600
        self.worker.step()
        self.assertEqual(self.worker.job['slots'][0]['phase'], 'created')
        self.assertIn(self.worker.job['status'], {'running', 'waiting'})
        self.assertEqual(self.client.sent, [])

    def test_closed_workspace_stops_batch_without_replacement(self):
        self.worker.step()
        with patch.object(self.client, 'tree', return_value={'windows': []}):
            self.assertFalse(self.worker.step())
        self.assertEqual(self.worker.job['status'], 'workspace_closed')
        self.assertEqual(len(self.client.calls), 1)

    def test_completed_B_appends_another_50(self):
        self.assertEqual(self.finish()['started'], 50)
        old = self.job['job_id']
        self.job = batch.start(self.config, self.wid, client=self.client, launch=False)
        self.assertNotEqual(old, self.job['job_id'])
        self.worker = batch.BatchWorker(self.config, self.job['job_id'], client=self.client, queue=self.client)
        self.addCleanup(self.worker.cache.close)
        self.assertEqual(self.finish()['started'], 50)
        self.assertEqual(len(self.client.calls), 100)
        self.assertEqual(len(self.client.sent), 100)

    def test_three_pools_share_four_initializing_slots_and_two_starts_per_second(self):
        workers = [self.worker]
        for _ in range(2):
            context = SimpleNamespace(config=self.config, root=self.root, store=self.store,
                wid=str(uuid.uuid4()), window=self.window, pane=str(uuid.uuid4()),
                assertEqual=self.assertEqual, assertNotIn=self.assertNotIn)
            client = BatchFixture(context)
            job = batch.start(self.config, context.wid, client=client, launch=False)
            worker = batch.BatchWorker(self.config, job['job_id'], client=client, queue=client, clock=lambda: self.now)
            context.worker = worker
            self.addCleanup(worker.cache.close)
            workers.append(worker)
        for worker in workers:
            worker.client.frame_options = {'composer': 'human draft'}
        starts = []
        previous = 0
        for _ in range(100):
            for worker in workers:
                worker.step()
            total = sum(len(w.client.calls) for w in workers)
            starts.extend([self.now] * (total - previous))
            previous = total
            self.assertLessEqual(total, 4)
            self.now += .1
        self.assertEqual(previous, 4)
        self.assertTrue(all(b - a >= .5 - 1e-6 for a, b in zip(starts, starts[1:])))
        self.assertTrue(all(s['phase'] != 'blocked' for w in workers for s in w.job['slots']))

    def test_panel_start_hold_is_pool_monitoring_not_pause_even_before_top_classifies_codex(self):
        from cmux_supervisor_tui import SupervisorModel, watch_label
        self.worker.step()
        idle = SimpleNamespace(maybe_refresh=lambda **kwargs: None)
        sessions = SimpleNamespace(maybe_refresh=lambda *args, **kwargs: None, snapshot=lambda: {})
        panel = SupervisorModel(self.config, client=self.client, janitor=idle, stack=idle, collab=idle, sessions=sessions)
        panel.refresh(force=True)
        self.assertEqual(len(panel.candidates), 1)
        self.assertEqual(watch_label(panel.candidates[0]), '整池／启动中')
        self.assertEqual(panel.counts()['paused'], 0)
        self.assertEqual(panel.counts()['watching'], 1)
        panel.close()

    def test_start_hold_vetoes_even_an_explicit_registration_at_input_boundary(self):
        self.worker.step()
        record = core.find_main_surface(self.client.tree(), self.client.calls[0])
        self.store.mutate(lambda c: c['targets'].append({**record, 'source': 'explicit', 'enabled': True, 'paused': False}))
        daemon = core.WatchDaemon(self.config, self.root / 'state.json', client=self.client)
        self.addCleanup(daemon._process_snapshots.close)
        self.assertIsNone(daemon._active_send_target(record))

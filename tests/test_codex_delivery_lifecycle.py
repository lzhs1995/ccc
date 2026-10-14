"""Discovery churn and UI activity cannot rewrite durable delivery history."""
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from unittest import mock

import cmux_codex_watch as core
from ccc_delivery import DeliveryStore
from ccc_provider_retry import ProviderRetryStore
from tests.test_codex_status_chrome import PROVIDER_TOKEN_RATE_LIMIT, status_payload, visible_text
from tests.test_watch import FakeClient, armed_daemon, discovery_fixture, grid_payload, span


class CodexDeliveryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.frame = status_payload(PROVIDER_TOKEN_RATE_LIMIT)
        self.client = FakeClient(self.frame, visible_text(self.frame))
        self.daemon = armed_daemon(self.temp.name, self.client)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.daemon._delivery_store.start()
        self.addCleanup(self.daemon._delivery_store.close)
        self.now = 1000.0
        self.turn = dict(kind='task_complete', session_id='original', turn_id='failed-one',
                         at=200.0, signature=[1, 200, 1], model_provider='synthetic-provider',
                         error={'message': PROVIDER_TOKEN_RATE_LIMIT})
        self.daemon.codex_queue_recovery.current_turn = lambda _: dict(self.turn)
        self.daemon._provider_retry = ProviderRetryStore(self.daemon._provider_retry.path,
            clock=lambda: self.now, jitter=lambda: 0)
        self.daemon._provider_retry.observe('original', 'synthetic-provider', 'failed-one',
            'rate_limit', PROVIDER_TOKEN_RATE_LIMIT, 200.0)
        self.now += 1

    def test_discovery_loss_preserves_sqlite_revision_and_allows_only_new_failed_turn(self):
        tree, top = discovery_fixture()
        tree['windows'][0]['workspaces'][0]['panes'][0]['surfaces'][0]['id'] = 'surface-uuid'
        self.client.tree_data, self.client.top_data = tree, top
        self.daemon._mutate_config(lambda config: config.update(targets=[], workspace_rules=[{
            'workspace_id': 'workspace-uuid', 'enabled': True, 'excluded_surface_ids': []}]))
        self.daemon._refresh_dynamic_targets(self.client, force=True)
        with mock.patch.object(core.time, 'time', return_value=self.now):
            self.daemon.process_once(self.client)
        runtime = self.daemon.runtime['surface-uuid']
        attempt, revision = runtime.send_attempt_id, runtime.delivery_revision
        self.assertEqual(len(self.client.sent), 1)
        self.assertGreater(revision, 0)

        self.client.top_data = {'windows': []}
        self.daemon._refresh_dynamic_targets(self.client, force=True)
        self.assertNotIn('surface-uuid', self.daemon.dynamic_targets)
        self.assertIsNone(self.daemon._active_send_target({'surface_id': 'surface-uuid'}))
        self.assertIs(self.daemon.runtime.get('surface-uuid'), runtime)
        self.client.top_data = top
        self.daemon._refresh_dynamic_targets(self.client, force=True)
        with mock.patch.object(core.time, 'time', return_value=self.now):
            self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual((runtime.send_attempt_id, runtime.delivery_revision), (attempt, revision))

        self.turn.update(turn_id='failed-two', at=1002.0, signature=[1, 400, 2])
        self.now = 1003.0
        self.daemon._provider_retry.observe('original', 'synthetic-provider', 'failed-two',
            'rate_limit', PROVIDER_TOKEN_RATE_LIMIT, 1002.0)
        self.now += 1
        with mock.patch.object(core.time, 'time', return_value=self.now):
            self.daemon.process_once(self.client)
            self.daemon.process_once(self.client)
        self.assertEqual([r[-1] for r in self.client.sent], ['任务请继续', '任务请继续'])
        self.assertNotEqual(runtime.send_attempt_id, attempt)
        self.assertGreater(runtime.delivery_revision, revision)
        recovered = {}
        DeliveryStore(self.daemon._delivery_store.root).restore(recovered, core.TargetRuntime)
        self.assertEqual(recovered['surface-uuid'].send_attempt_id, runtime.send_attempt_id)
        self.assertEqual(recovered['surface-uuid'].codex_sent_turn_key, 'original:failed-two:1002.0')

    def test_stale_zero_input_failure_is_not_confirmed_by_working_or_queue(self):
        original = core.TargetRuntime(send_attempt_id='prior-input', delivery_status='accepted')
        self.daemon._delivery_store.persist('surface-uuid', original)
        self.daemon.process_once(self.client)
        runtime = self.daemon.runtime['surface-uuid']
        self.assertEqual(runtime.delivery_status, 'failed')
        self.assertEqual(runtime.send_io_started_at, 0)
        self.assertEqual(self.client.sent, [])
        error = runtime.last_send_error
        self.assertIn('durable delivery revision is stale', error)
        for kind in ('working', 'queued_followup'):
            self.daemon._reconcile_codex_delivery('surface-uuid', runtime,
                core.ScreenState(kind, message_kind='codex'))
            self.assertEqual(runtime.delivery_status, 'failed')
            self.assertEqual(runtime.delivery_confirmed_at, 0)
            self.assertEqual(runtime.last_send_error, error)
        with closing(sqlite3.connect(self.daemon._delivery_store.root / 'delivery.sqlite3')) as connection:
            record = json.loads(connection.execute('SELECT record FROM delivery WHERE surface_id=?',
                ('surface-uuid',)).fetchone()[0])
        self.assertEqual(record['runtime']['send_attempt_id'], 'prior-input')

    def test_native_progress_preserves_unknown_input_without_fabricating_receipt(self):
        runtime = self.daemon.runtime['surface-uuid'] = core.TargetRuntime(
            send_attempt_id='lost-ack', delivery_status='unknown', codex_input_phase='text_pending',
            codex_sent_turn_key='original:failed-one:200.0', send_started_at=1000,
            last_send_error='ack timed out')
        self.turn.update(kind='task_started', turn_id='new-turn', at=1001.0)
        # A task_started event does not prove that the exact original payload
        # arrived. Keep the pending-input guard while the new turn is active.
        self.assertTrue(self.daemon._reconcile_codex_delivery('surface-uuid', runtime,
            core.ScreenState('idle', message_kind='codex')))
        self.assertEqual(runtime.delivery_status, 'unknown')
        self.assertEqual(runtime.delivery_confirmed_at, 0)
        self.assertEqual(runtime.last_send_error, 'ack timed out')
        self.assertEqual(self.client.sent, [])

    def test_unknown_attempt_does_not_block_a_distinct_native_failed_turn(self):
        with mock.patch.object(core.time, 'time', return_value=1000.0):
            self.daemon.process_once(self.client)
        runtime = self.daemon.runtime['surface-uuid']
        runtime.delivery_status = 'unknown'
        self.turn.update(turn_id='failed-two', at=1002.0, signature=[1, 400, 2])
        self.daemon._provider_retry.observe('original', 'synthetic-provider', 'failed-two',
            'rate_limit', PROVIDER_TOKEN_RATE_LIMIT, 1002.0)
        self.now = 1003.0
        with mock.patch.object(core.time, 'time', return_value=self.now):
            self.daemon.process_once(self.client)
            self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(runtime.delivery_status, 'accepted')
        self.assertEqual(runtime.delivery_confirmed_at, 0)

    def test_failed_goal_persistence_without_native_identity_cannot_authorize_retry(self):
        row = self.frame['render_grid']['cursor']['row']
        self.frame['render_grid']['row_spans'].append(
            span(row + 2, 2, 'GPT-6-Astra · Goal stalled (/goal resume)', 1))
        self.client.payload, self.client.text = self.frame, visible_text(self.frame)
        self.client.resume_codex_goal = lambda wid, sid, **kw: self.client.send(wid, sid, '/goal resume')
        proof = {**self.turn, 'goal_id': 'original-goal'}
        with mock.patch.object(self.daemon, '_current_blocked_goal', return_value=proof):
            with mock.patch.object(self.daemon._delivery_store, 'persist',
                                   side_effect=RuntimeError('transaction unavailable')):
                self.daemon.process_once(self.client)
            runtime = self.daemon.runtime['surface-uuid']
            self.assertEqual(self.client.sent, [])
            self.client.payload = grid_payload([], working=True)
            self.client.text = visible_text(self.client.payload)
            self.daemon.process_once(self.client)
            self.assertEqual(runtime.delivery_status, 'failed')
            self.client.payload, self.client.text = self.frame, visible_text(self.frame)
            self.daemon.process_once(self.client)
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])
        self.assertFalse(runtime.codex_input_not_sent)
        self.assertEqual(runtime.delivery_confirmed_at, 0)


if __name__ == '__main__':
    unittest.main()

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools.standby_retained_activation import ActivationRun, NativeSetup
from tests.test_standby_process_overlap import plan


class ActivationRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.now = 0
        rows = plan()['batches']
        self.pool = SimpleNamespace(order=[r['batch_id'] for r in rows],
            expected={r['batch_id']: r for r in rows}, handles={}, _health=Mock())
        self.owners = {}
        for key in self.pool.order:
            owner = Mock()
            owner.status.return_value = {'state': 'preparing'}
            owner.service.manager.refresh.return_value = dict(
                state='ready', ready_originals=50, required_originals=50)
            self.owners[key] = owner
        def start(key):
            caller = Mock()
            caller.routes.zero.return_value = dict(before_activation_model_requests=0,
                requests=0, action_id=None, observed_since_monotonic_ns=1)
            self.pool.handles[key] = {'runner': SimpleNamespace(
                owner=self.owners[key], caller=caller)}
        self.pool.start = Mock(side_effect=start)
        self.pool.await_open = Mock(return_value={'opened': True})
        self.pool.prepare = Mock()
        self.pool.begin_activation = Mock()
        self.pool.poll_activation = Mock(return_value='confirmation_written')
        self.pool.settle = Mock(side_effect=lambda key: {'batch_id': key})
        self.run = ActivationRun(self.pool, self.root, clock=lambda: self.now)
        self.first = self.pool.order[::2]
        self.second = self.pool.order[1::2]

    def prepared(self):
        self.run.step()
        self.run.step()
        for key in self.first:
            self.owners[key].status.return_value = {'state': 'ready'}

    def confirmed(self):
        self.prepared()
        self.run.step()
        self.now = 3
        self.run.step()
        self.run.step()

    def test_five_workspaces_progress_and_successors_require_settlement(self):
        self.confirmed()
        self.assertEqual([c.args[0] for c in self.pool.start.call_args_list], self.first)
        self.assertEqual(self.pool.prepare.call_count, 5)
        self.assertEqual(self.pool.begin_activation.call_count, 5)
        self.pool.settle.assert_not_called()
        # Confirmation alone leaves the next cohort unadmitted.
        self.run.step()
        self.assertEqual(self.pool.start.call_count, 5)
        for key in self.first:
            self.owners[key].status.return_value = {'state': 'first_tasks_observed'}
        self.run.step()
        self.assertEqual(self.pool.settle.call_count, 5)
        self.assertEqual(self.pool.start.call_count, 10)
        self.run.step()
        for key in self.second:
            self.owners[key].status.return_value = {'state': 'ready'}
        self.run.step()
        self.now = 6
        self.run.step()
        self.run.step()
        for key in self.second:
            self.owners[key].status.return_value = {'state': 'first_tasks_observed'}
        result = self.run.step()
        self.assertTrue(result['all_settled'])
        self.assertFalse(result['full_500_acceptance'])
        self.assertFalse(result['run_terminal'])
        self.assertEqual(self.pool.prepare.call_count, 10)
        self.assertEqual(self.pool.begin_activation.call_count, 10)
        self.assertEqual(self.pool.poll_activation.call_count, 10)
        self.assertEqual(self.pool.settle.call_count, 10)
        self.run.step()
        self.assertEqual(self.pool.settle.call_count, 10)

    def test_nonzero_request_during_idle_denies_all_ui_and_latches(self):
        self.prepared()
        self.run.step()
        self.now = 3
        self.pool.handles[self.first[0]]['runner'].caller.routes.zero.side_effect = (
            lambda i: dict(before_activation_model_requests=int(i == 49),
                           requests=int(i == 49), action_id=None, observed_since_monotonic_ns=1))
        with self.assertRaisesRegex(ValueError, 'zero-request'):
            self.run.step()
        with self.assertRaisesRegex(ValueError, 'outcome unknown'):
            self.run.step()
        self.pool.begin_activation.assert_not_called()

    def test_real_route_zero_evidence_allows_ready_without_requests(self):
        from ccc_standby_routes import RouteObserver
        routes = RouteObserver(['http://127.0.0.1:1/v1'] * 50, allow_local=True)
        try:
            self.prepared()
            handle = self.pool.handles[self.first[0]]
            handle['runner'].caller.routes = routes
            manager, zeros = self.run._ready(handle)
            self.assertEqual(manager['ready_originals'], 50)
            self.assertEqual(len(zeros), 50)
            self.assertTrue(all(z['before_activation_model_requests'] == 0 for z in zeros))
            self.assertEqual(routes.report()['accepted_connections'], 0)
        finally:
            routes.close()

    def test_missing_released_boolean_and_mixed_route_evidence_rejected(self):
        self.prepared()
        handle = self.pool.handles[self.first[0]]
        valid = dict(before_activation_model_requests=0, requests=0,
                     action_id=None, observed_since_monotonic_ns=1)
        for invalid in (True, {}, dict(valid, requests=1), dict(valid, requests=False),
                        dict(valid, action_id='already-released'),
                        dict(valid, observed_since_monotonic_ns=2)):
            with self.subTest(invalid=invalid):
                handle['runner'].caller.routes.zero.side_effect = lambda i: invalid if i == 49 else valid
                with self.assertRaisesRegex(ValueError, 'zero-request'):
                    self.run._ready(handle)

    def test_lost_ready_after_idle_cannot_restart_preparation(self):
        self.prepared()
        self.run.step()
        self.owners[self.first[0]].status.return_value = {'state': 'preparing'}
        with self.assertRaisesRegex(ValueError, 'lost readiness'):
            self.run.step()
        self.assertEqual(self.pool.prepare.call_count, 5)
        self.pool.begin_activation.assert_not_called()

    def test_replaced_route_or_reset_stamp_during_idle_denies_ui(self):
        self.prepared()
        self.run.step()
        caller = self.pool.handles[self.first[0]]['runner'].caller
        original = caller.routes
        replacement = Mock()
        replacement.zero.return_value = dict(original.zero.return_value)
        caller.routes = replacement
        self.now = 3
        with self.assertRaisesRegex(ValueError, 'route observation changed'):
            self.run.step()
        self.pool.begin_activation.assert_not_called()
        caller.routes = original
        original.zero.return_value = dict(original.zero.return_value,
                                         observed_since_monotonic_ns=2)
        with self.assertRaisesRegex(ValueError, 'route observation changed'):
            self.run._ready(self.pool.handles[self.first[0]])

    def test_route_replaced_during_zero_read_denies_ui(self):
        self.prepared()
        caller = self.pool.handles[self.first[0]]['runner'].caller
        original = caller.routes
        valid = dict(original.zero.return_value)
        def read(index):
            if index == 49:
                caller.routes = Mock()
            return valid
        original.zero.side_effect = read
        with self.assertRaisesRegex(ValueError, 'route observation changed'):
            self.run.step()
        self.pool.begin_activation.assert_not_called()

    def test_unknown_spawn_never_replayed(self):
        self.prepared()
        self.run.step()
        self.now = 3
        self.pool.begin_activation.side_effect = OSError('spawn outcome unknown')
        with self.assertRaises(OSError):
            self.run.step()
        with self.assertRaises(ValueError):
            self.run.step()
        self.pool.begin_activation.assert_called_once()

    def test_late_settlement_return_never_admits_successor(self):
        self.confirmed()
        self.owners[self.first[0]].status.return_value = {'state': 'first_tasks_observed'}
        def delayed(key):
            self.now = 601
            return {'batch_id': key}
        self.pool.settle.side_effect = delayed
        with self.assertRaises(TimeoutError):
            self.run.step()
        self.assertEqual(self.pool.start.call_count, 5)
        self.assertEqual(len(self.pool.handles), 5)

    def test_deadline_keeps_original_opening_handle(self):
        self.pool.await_open.return_value = None
        self.run.step()
        handles = dict(self.pool.handles)
        self.now = 600
        with self.assertRaises(TimeoutError):
            self.run.step()
        self.assertEqual(self.pool.handles, handles)
        self.assertEqual(self.pool.start.call_count, 5)
        self.pool.prepare.assert_not_called()

    def test_second_driver_refused_before_first_start(self):
        other = self.root/'other'
        other.mkdir(mode=0o700)
        with self.assertRaisesRegex(ValueError, 'original unstarted pool'):
            ActivationRun(self.pool, other, clock=lambda: self.now)
        self.assertIs(self.pool.activation_run, self.run)
        self.pool.start.assert_not_called()

    def test_record_failure_before_ui_latches_without_replay(self):
        self.prepared()
        self.run.step()
        self.now = 3
        with patch('tools.standby_retained_activation.write_once', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.run.step()
        with self.assertRaisesRegex(ValueError, 'outcome unknown'):
            self.run.step()
        self.pool.begin_activation.assert_not_called()

    def test_setup_order_and_original_handles(self):
        events = []
        def setup(key, handle):
            self.assertIs(handle, self.pool.handles[key])
            events.append(('setup', key))
        self.run.callbacks['prepare'] = setup
        self.pool.prepare.side_effect = lambda key: events.append(('prepare', key))
        self.prepared()
        self.assertEqual(events, [item for key in self.first
            for item in [('setup', key), ('prepare', key)]])
        events.clear()
        self.run.callbacks['ui'] = setup
        self.pool.begin_activation.side_effect = lambda key, path: events.append(('ui', key))
        self.run.step()
        self.now = 3
        self.run.step()
        self.run.step()
        self.assertEqual(events, [item for key in self.first
            for item in [('setup', key), ('ui', key)]])

    def test_prepare_setup_unknown_prevents_prepare_and_replay(self):
        callback = Mock(side_effect=OSError('gate setup outcome unknown'))
        self.run.callbacks['prepare'] = callback
        self.run.step()
        with self.assertRaises(OSError):
            self.run.step()
        with self.assertRaisesRegex(ValueError, 'outcome unknown'):
            self.run.step()
        callback.assert_called_once()
        self.pool.prepare.assert_not_called()

    def test_ui_binding_that_changes_counters_prevents_input(self):
        self.prepared()
        self.run.step()
        def bind(key, handle):
            handle['runner'].caller.routes.zero.return_value = False
        self.run.callbacks['ui'] = bind
        self.now = 3
        with self.assertRaisesRegex(ValueError, 'zero-request'):
            self.run.step()
        self.pool.begin_activation.assert_not_called()

    def test_late_ui_binding_preserves_handle_without_input(self):
        self.prepared()
        self.run.step()
        def bind(key, handle):
            self.now = 601
        self.run.callbacks['ui'] = bind
        self.now = 3
        with self.assertRaises(TimeoutError):
            self.run.step()
        self.pool.begin_activation.assert_not_called()
        self.assertEqual(len(self.pool.handles), 5)

    def native_setup(self):
        from ccc_workspace_batch import PROMPT
        self.pool.specs = {key: (SimpleNamespace(current=Mock(),
            value={'config_path': str(self.root/'config.json'),
                   'upstream_url': 'https://127.0.0.1/'+key}), self.root)
            for key in self.pool.order}
        resources = {key: dict(proxy=SimpleNamespace(lock=threading.Lock(),
            workspace=self.pool.expected[key]['workspace_id'], gate=None),
            provider=Mock(url='https://127.0.0.1/'+key, prompt=PROMPT))
            for key in self.pool.order}
        setup = NativeSetup(self.pool, resources, Mock(side_effect=lambda commands: Mock()))
        setup.transcript_factory = Mock(side_effect=lambda row, path: Mock())
        for key, owner in self.owners.items():
            owner.service.preparation.job = {'id': '12345678-1234-1234-1234-123456789abc'}
            def observe(index, key=key):
                return dict(index=index, job_id='12345678-1234-1234-1234-123456789abc',
                    workspace_id=self.pool.expected[key]['workspace_id'],
                    session_id=key+str(index), pid=1+index+50*self.pool.order.index(key),
                    birth=[100, index], surface_id=key+str(index))
            owner.service.preparation.observe_for_activation.side_effect = observe
        self.run.callbacks = {'prepare': setup.before_prepare, 'ui': setup.before_ui}
        return setup, resources

    def test_native_provider_binding_precedes_original_ui(self):
        setup, resources = self.native_setup()
        self.confirmed()
        for key in self.first:
            self.assertIs(resources[key]['proxy'].gate, setup.gates[key])
            self.assertEqual(resources[key]['provider'].bind.call_count, 50)
            self.assertEqual(len(setup.resolvers[key]), 50)
            setup.gates[key].bind_activation.assert_called_once()
        self.assertEqual(self.pool.begin_activation.call_count, 5)

    def test_partial_provider_binding_latches_without_gate_or_ui(self):
        setup, resources = self.native_setup()
        key = self.first[0]
        resources[key]['provider'].bind.side_effect = [None, OSError('partial bind')]
        self.prepared()
        self.run.step()
        self.now = 3
        with self.assertRaises(OSError):
            self.run.step()
        with self.assertRaisesRegex(ValueError, 'outcome unknown'):
            self.run.step()
        self.assertEqual(resources[key]['provider'].bind.call_count, 2)
        self.assertEqual(len(setup.resolvers[key]), 50)
        setup.gates[key].bind_activation.assert_not_called()
        self.pool.begin_activation.assert_not_called()

    def test_proxy_drift_during_binding_denies_gate_and_ui(self):
        setup, resources = self.native_setup()
        key = self.first[0]
        def changed(*args):
            resources[key]['proxy'].workspace = 'foreign'
        resources[key]['provider'].bind.side_effect = changed
        self.prepared()
        self.run.step()
        self.now = 3
        with self.assertRaisesRegex(ValueError, 'target changed'):
            self.run.step()
        setup.gates[key].bind_activation.assert_not_called()
        self.pool.begin_activation.assert_not_called()

    def test_cross_cohort_original_reuse_denies_second_ui(self):
        setup, resources = self.native_setup()
        first, second = self.first[:2]
        original = self.owners[second].service.preparation.observe_for_activation.side_effect
        def duplicate(index):
            row = original(index)
            row['session_id'] = first+str(index)
            return row
        self.owners[second].service.preparation.observe_for_activation.side_effect = duplicate
        self.prepared()
        self.run.step()
        self.now = 3
        with self.assertRaisesRegex(ValueError, 'duplicate native'):
            self.run.step()
        resources[second]['provider'].bind.assert_not_called()
        self.assertEqual(self.pool.begin_activation.call_count, 1)


if __name__ == '__main__':
    unittest.main()

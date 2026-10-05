import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import standby_multi_owner as subject
import ccc_standby_runner as runner_module
from tests.test_standby_process_overlap import plan, uid


class MultiOwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.manifest = SimpleNamespace(plan=plan(), current=Mock(), register_attempt=Mock(),
            bind=Mock(return_value={'settlement_sha256': 'digest', 'action_id': 'action'}))
        self.specs = []
        for i, row in enumerate(self.manifest.plan['batches']):
            directory = self.root/str(i)
            directory.mkdir(mode=0o700)
            value = dict(invocation_id=uid(10000+i), workspace_id=row['workspace_id'],
                         mode=row['mode'], config_path=str(self.root/('config'+str(i))))
            self.specs.append((row['batch_id'], SimpleNamespace(value=value,
                current=Mock(), path=self.root/('invocation'+str(i)), sha256='sha'+str(i)), directory))
        self.patches = [patch.object(subject, 'RunManifest', return_value=self.manifest),
                        patch.object(subject, 'live_settlement',
                                     return_value=({'action_id': 'action'}, 'digest')),
                        patch.object(subject, 'capture', return_value={'process_overlap_proven': True})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.runs = []
        runs = self.runs
        class Runner:
            def __init__(self, invocation, directory, *, stop):
                self.invocation, self.stop = invocation, stop
                self.owner, self.caller = Mock(), Mock()
            def run(self, *, prepare, emit):
                runs.append(self)
                emit(dict(kind='standby_runner_open', job_id=self.invocation.value['invocation_id'],
                          **self.invocation.value))
                self.stop.wait()
                return 0
        self.runner = Runner
        self.pool = None
        self.addCleanup(self.shutdown)

    def shutdown(self):
        if self.pool is not None:
            result = self.pool.close(timeout=5)
            self.assertFalse(any(row['alive'] for row in result.values()))

    def create(self, specs=None, runner=None):
        self.pool = subject.RetainedOwners(self.root, self.specs if specs is None else specs,
                                          runner_factory=runner or self.runner)
        return self.pool

    def start_settle(self, i):
        key = self.specs[i][0]
        self.pool.start(key)
        self.assertIsNotNone(self.pool.await_open(key))
        self.pool.settle(key)

    def test_ten_owners_retained_until_explicit_close(self):
        pool = self.create()
        self.assertEqual(self.manifest.register_attempt.call_count, 10)
        for i in range(10):
            self.start_settle(i)
        self.assertEqual(len(self.runs), 10)
        self.assertTrue(all(h['thread'].is_alive() for h in pool.handles.values()))
        self.assertTrue(pool.observe_overlap(self.root)['process_overlap_proven'])
        result = pool.close(timeout=5)
        self.assertEqual(len(result), 10)
        self.assertTrue(all(not r['alive'] and r['returncode'] == 0 for r in result.values()))
        with self.assertRaisesRegex(ValueError, 'closing'):
            pool.start(self.specs[0][0])

    def test_successor_requires_original_settlement(self):
        pool = self.create()
        with self.assertRaisesRegex(ValueError, 'prior cohort'):
            pool.start(self.specs[1][0])
        pool.start(self.specs[0][0])
        pool.await_open(self.specs[0][0])
        with self.assertRaisesRegex(ValueError, 'prior cohort'):
            pool.start(self.specs[1][0])
        # Another workspace can make independent progress.
        self.start_settle(2)
        pool.settle(self.specs[0][0])
        self.start_settle(1)

    def test_private_ui_exit_precedes_runner_stop(self):
        pool = self.create()
        key = self.specs[0][0]
        pool.start(key)
        pool.await_open(key)
        handle = pool.handles[key]
        driver = Mock()
        driver.close.return_value = False
        handle['ui_activation'] = driver
        pending = pool.close(timeout=0)
        self.assertTrue(pending[key]['ui_pending'])
        self.assertTrue(handle['thread'].is_alive())
        self.assertFalse(handle['stop'].is_set())
        driver.close.return_value = True
        result = pool.close(timeout=5)
        self.assertFalse(result[key]['alive'])
        self.assertTrue(handle['stop'].is_set())

    def test_duplicate_or_partial_scope_rejected_before_registration(self):
        for specs in (self.specs[:-1], self.specs[:-1]+[self.specs[0]]):
            with self.assertRaises(ValueError):
                self.create(specs)
        self.manifest.register_attempt.assert_not_called()

    def test_foreign_invocation_rejected_before_registration(self):
        self.specs[0][1].value['workspace_id'] = uid(2)
        with self.assertRaisesRegex(ValueError, 'invocation differs'):
            self.create()
        self.manifest.register_attempt.assert_not_called()

    def test_dead_owner_and_changed_settlement_deny_successor(self):
        pool = self.create()
        self.start_settle(0)
        with patch.object(subject, 'live_settlement', return_value=({'action_id': 'other'}, 'new')):
            with self.assertRaisesRegex(ValueError, 'retained owner'):
                pool.start(self.specs[1][0])
        handle = pool.handles[self.specs[0][0]]
        handle['stop'].set()
        handle['thread'].join(5)
        with self.assertRaisesRegex(ValueError, 'exited'):
            pool.start(self.specs[1][0])

    def test_unknown_admission_is_polled_not_restarted(self):
        gate = threading.Event()
        parent = self.runner
        class SlowRunner(parent):
            def run(self, **kwargs):
                gate.wait(5)
                return super().run(**kwargs)
        pool = self.create(runner=SlowRunner)
        key = self.specs[0][0]
        try:
            pool.start(key)
            self.assertIsNone(pool.await_open(key, timeout=0))
            with self.assertRaisesRegex(ValueError, 'already consumed'):
                pool.start(key)
        finally:
            gate.set()
        self.assertIsNotNone(pool.await_open(key))

    def test_pending_close_keeps_original_handle(self):
        gate = threading.Event()
        parent = self.runner
        class SlowClose(parent):
            def run(self, **kwargs):
                result = super().run(**kwargs)
                gate.wait(5)
                return result
        pool = self.create(runner=SlowClose)
        self.start_settle(0)
        key = self.specs[0][0]
        original = pool.handles[key]
        try:
            self.assertTrue(pool.close(timeout=0)[key]['alive'])
            self.assertIs(pool.handles[key], original)
        finally:
            gate.set()
        self.assertFalse(pool.close(timeout=5)[key]['alive'])

    def test_partial_pool_cannot_claim_overlap(self):
        pool = self.create()
        self.start_settle(0)
        with self.assertRaisesRegex(ValueError, 'all ten'):
            pool.observe_overlap(self.root)
        subject.capture.assert_not_called()

    def test_preparation_is_consumed_even_when_outcome_unknown(self):
        pool = self.create()
        key = self.specs[0][0]
        pool.start(key)
        pool.await_open(key)
        owner = pool.handles[key]['runner'].owner
        owner.prepare.side_effect = OSError('native creation outcome unknown')
        with self.assertRaises(OSError):
            pool.prepare(key)
        with self.assertRaisesRegex(ValueError, 'unconsumed'):
            pool.prepare(key)
        owner.prepare.assert_called_once()
        self.assertEqual(pool.handles[key]['preparation_error'], 'OSError')

    def test_preparation_and_bound_ui_integration(self):
        from tools.standby_ui_activation import BoundActivation
        from tools.standby_ui_pty import SupervisorPTY
        pool = self.create()
        key = self.specs[0][0]
        pool.start(key)
        pool.await_open(key)
        workspace = self.specs[0][1].value['workspace_id']
        surface = uid(30000)
        witness = dict(workspace_id=workspace, surface_id=surface,
                       session_id=uid(30001), pid=123, birth=[100, 456])
        owner = pool.handles[key]['runner'].owner
        owner.prepare.return_value = {'state': 'preparing'}
        owner.status.return_value = {'state': 'ready'}
        owner.service.preparation.observe_for_activation.side_effect = lambda _: copy.deepcopy(witness)
        self.assertEqual(pool.prepare(key), {'state': 'preparing'})
        self.assertIsNone(pool.handles[key]['settlement'])
        ui = object.__new__(SupervisorPTY)
        ui.sent, ui.data, ui.action_offset = set(), bytearray(), None
        ui.master = 999
        ui.child = Mock(pid=12345)
        ui.child.poll.return_value = None
        ui.child.terminate.side_effect = lambda: setattr(ui.child.poll, 'return_value', 0)
        ui.close = Mock()
        ui.poll, ui._record = Mock(return_value=None), Mock()
        client = Mock()
        client.tree.return_value = {'windows': [{'workspaces': [dict(id=workspace,
            ref='workspace:1', title='fixture', panes=[dict(ref='pane:2', surfaces=[
                dict(id=surface, ref='surface:3', type='terminal')])])]}]}
        def activation(*args):
            return BoundActivation(*args, ui_factory=lambda *_: ui)
        with patch('tools.standby_ui_activation.BoundActivation', side_effect=activation), \
                patch('tools.standby_ui_activation.connect', return_value=client), \
                patch('tools.standby_ui_pty.os.write', return_value=1) as write:
            driver = pool.begin_activation(key, self.root/'ui')
            self.assertEqual(pool.poll_activation(key), 'waiting_focus')
            ui.data.extend(driver.prefix)
            self.assertEqual(pool.poll_activation(key), 'action_written')
            ui.data.extend((driver.prompt+' [y/N]').encode())
            self.assertEqual(pool.poll_activation(key), 'confirmation_written')
            self.assertEqual(pool.poll_activation(key), 'confirmation_written')
            self.assertEqual(write.call_count, 2)
            with self.assertRaisesRegex(ValueError, 'already consumed'):
                pool.begin_activation(key, self.root/'replacement')
            self.assertIs(pool.handles[key]['ui_activation'], driver)
            self.assertIsNone(pool.handles[key]['settlement'])
            with self.assertRaisesRegex(ValueError, 'prior cohort'):
                pool.start(self.specs[1][0])
            pool.settle(key)
            self.start_settle(1)
        owner.prepare.assert_called_once()

    def test_poll_cannot_create_an_activation(self):
        pool = self.create()
        key = self.specs[0][0]
        pool.start(key)
        pool.await_open(key)
        with self.assertRaisesRegex(ValueError, 'has not begun'):
            pool.poll_activation(key)

    def test_owner_replacement_before_preparation_never_creates_native(self):
        pool = self.create()
        key = self.specs[0][0]
        pool.start(key)
        pool.await_open(key)
        original = pool.handles[key]['runner'].owner
        replacement = Mock()
        pool.handles[key]['runner'].owner = replacement
        with self.assertRaisesRegex(ValueError, 'owner or caller replaced'):
            pool.prepare(key)
        original.prepare.assert_not_called()
        replacement.prepare.assert_not_called()

    def test_caller_replacement_before_ui_never_spawns_supervisor(self):
        pool = self.create()
        key = self.specs[0][0]
        pool.start(key)
        pool.await_open(key)
        pool.handles[key]['runner'].caller = Mock()
        with patch('tools.standby_ui_activation.connect') as connect:
            with self.assertRaisesRegex(ValueError, 'owner or caller replaced'):
                pool.begin_activation(key, self.root/'ui')
        connect.assert_not_called()

    def test_actual_runner_open_and_close_contract(self):
        callers = []
        def factory(invocation, directory, *, stop):
            value = invocation.value
            value.update(lifetime_seconds=60, argv=['/not-executed/codex'],
                         provider='fixture', upstream_url='https://example.invalid', environment={})
            selected = dict(job_id=value['invocation_id'], cohort_id=uid(20000+len(callers)),
                            workspace_id=value['workspace_id'], mode='b',
                            boot_id=runner_module.boot_id(), generation='a'*64)
            owner = Mock()
            owner.service.preparation = SimpleNamespace(selected=selected,
                jobfile=directory/'job.json', config_path=Path(value['config_path']))
            owner.endpoint.spec_path = directory/'owner.json'
            owner.endpoint.sha256 = 'b'*64
            owner.endpoint.resource_report.return_value = {'resources_released': True}
            owner.status.return_value = dict(selected, state='first_tasks_observed',
                                              job_terminal=False, run_terminal=False)
            caller = Mock()
            caller.admit.return_value = owner
            caller.routes.report.return_value = {'resources_released': True}
            caller.sources.resource_report.return_value = {'resources_released': True}
            callers.append(caller)
            return runner_module.Runner(invocation, directory, stop=stop,
                caller_factory=Mock(return_value=caller), client_factory=Mock(return_value=Mock()))
        with patch.object(runner_module, 'source_hashes', return_value={}):
            pool = self.create(runner=factory)
            for i in range(10):
                self.start_settle(i)
            self.assertEqual(len(callers), 10)
            for caller in callers:
                caller.close.assert_not_called()
                caller.admit.return_value.prepare.assert_not_called()
            result = pool.close(timeout=5)
        self.assertTrue(all(not row['alive'] and row['error'] is None for row in result.values()))
        for caller in callers:
            caller.close.assert_called_once()
        for _, _, directory in self.specs:
            closed = json.loads((directory/'runner-closed.json').read_bytes())
            self.assertTrue(closed['all_resources_released'])
            self.assertFalse(closed['native_processes_terminated'])
            self.assertFalse(closed['run_terminal'])


if __name__ == '__main__':
    unittest.main()

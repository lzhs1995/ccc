"""Real interpreter startup/teardown without targets or native input."""
import json
from pathlib import Path
import tempfile
import time
import unittest
import sys
import subprocess
import queue
from unittest.mock import Mock, patch
from types import SimpleNamespace

import cmux_codex_watch as core
from ccc_native_lanes import (AuthorizationCells, InterpreterLane, refresh_native_bindings,
                              _binding_process_current, _binding_matches,
                              _create_lane_daemon, _restore_assignment_runtime, run_native_lane)
from ccc_delivery import DeliveryStore


class NativeLaneTests(unittest.TestCase):
    @unittest.skipUnless(sys.version_info >= (3,14), 'native lanes use Python 3.14 interpreters')
    def test_cleanup_errors_preserve_original_lane_rows_and_propagate_failure(self):
        for failed in ('native', 'scheduler', 'jev', 'processes', 'delivery'):
            with self.subTest(failed=failed):
                closed = []
                services = {name: Mock() for name in
                            ('native', 'scheduler', 'jev', 'processes', 'delivery')}
                def closer(name):
                    def close():
                        closed.append(name)
                        if name == failed:
                            raise RuntimeError('original-' + name + '-failure')
                    return close
                for name, service in services.items():
                    if name == 'jev':
                        service.stop.side_effect = closer(name)
                    else:
                        service.close.side_effect = closer(name)
                runtime = core.TargetRuntime(delivery_status='unknown',
                    send_attempt_id='original-attempt', claude_submit_phase='text_written',
                    claude_submit_write_unknown=True, claude_submit_attempts=2)
                daemon = SimpleNamespace(_delivery_store=services['delivery'],
                    _process_snapshots=services['processes'], config_store=Mock(),
                    codex_queue_recovery=Mock(), _active_send_target=Mock(),
                    _native_retry_needed=Mock(), _native_failed=Mock(),
                    _scheduled_native=Mock(), _scheduled_native_event=Mock(),
                    _record_dispatch=Mock(), _scheduled_error=Mock(),
                    jev_classifier=services['jev'], runtime={'selected': runtime})
                assignment = {'target': {'surface_id': 'selected', 'workspace_id': 'owned'},
                              'cell': 1, 'binding': {}, 'runtime': runtime.to_dict()}
                commands = queue.SimpleQueue()
                commands.put({'type': 'assignments', 'assignments': [assignment]})
                commands.put({'type': 'stop'})
                results = queue.SimpleQueue()
                with patch('ccc_native_lanes._create_lane_daemon', return_value=daemon), \
                        patch('ccc_native_lanes._restore_assignment_runtime'), \
                        patch('ccc_native_lanes._bound_native_turn', return_value={'kind': 'unknown'}), \
                        patch.object(core, 'CmuxViewportSocket'), \
                        patch('ccc_codex_queue.NativeCompletionWatcher', return_value=services['native']), \
                        patch('ccc_scheduling.SurfaceScheduler', return_value=services['scheduler']):
                    with self.assertRaisesRegex(RuntimeError, 'original-' + failed + '-failure'):
                        run_native_lane('{"capabilities": {}}', bytearray([1, 1]), 0, commands, results)
                self.assertEqual(closed, ['native', 'scheduler', 'jev', 'processes', 'delivery'])
                self.assertEqual(results.get_nowait()['type'], 'ready')
                final = results.get_nowait()
                self.assertEqual(final['type'], 'runtime')
                self.assertEqual(final['rows']['selected']['cell'], 1)
                saved = final['rows']['selected']['runtime']
                self.assertEqual(saved['send_attempt_id'], 'original-attempt')
                self.assertEqual(saved['delivery_status'], 'unknown')
                self.assertTrue(saved['claude_submit_write_unknown'])
                self.assertEqual(saved['claude_submit_attempts'], 2)
                self.assertTrue(daemon.stop_requested)

    def test_cleanup_failure_retains_lane_until_original_interpreter_quiesces(self):
        lane = object.__new__(InterpreterLane)
        lane._interpreter = Mock()
        lane._interpreter.close.side_effect = [RuntimeError('threads running'),
                                              RuntimeError('threads running'), None]
        lane._stopped = False
        lane._thread = Mock()
        lane._thread.is_alive.return_value = False
        lane.results = queue.SimpleQueue()
        lane.error = None
        self.assertFalse(lane._finish_close())
        rows = lane.drain()
        self.assertEqual([row['type'] for row in rows], ['failure'])
        self.assertFalse(lane._stopped)
        self.assertIsNotNone(lane._interpreter)
        self.assertEqual([row['type'] for row in lane.drain()], ['stopped'])
        self.assertTrue(lane._stopped)
        self.assertIsNone(lane._interpreter)
        self.assertEqual(lane.drain(), [])

    def test_empty_lane_does_not_read_or_serialize_fleet_history_parent_retains_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, state = root / 'config.json', root / 'state.json'
            core.atomic_write_json(config, core.default_config())
            history = {str(i): core.TargetRuntime(last_send_error='original-' + str(i)).to_dict()
                       for i in range(150)}
            core.atomic_write_json(state, history)
            original = state.read_bytes()
            load = core.load_json
            def no_fleet_read(path, *args, **kwargs):
                self.assertNotEqual(Path(path), state, 'lane read the complete fleet state')
                return load(path, *args, **kwargs)
            with patch.object(core, 'load_json', side_effect=no_fleet_read):
                lane = _create_lane_daemon({'config_path': str(config), 'state_path': str(state)})
            self.addCleanup(lane._process_snapshots.close)
            self.assertEqual(lane.runtime, {})
            self.assertEqual(lane._state_snapshot._entries, {})
            parent = core.WatchDaemon(config, state)
            self.addCleanup(parent._process_snapshots.close)
            self.assertEqual(json.loads(parent._serialize_state()), history)
            self.assertEqual(state.read_bytes(), original)

    def test_new_assignment_restores_newer_and_equal_durable_unknown_before_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(directory)
            store.start()
            self.addCleanup(store.close)
            original = core.TargetRuntime(send_attempt_id='original', delivery_status='unknown',
                codex_sent_turn_key='session:original-turn', send_io_started_at=20)
            store.persist('selected', original)
            store.persist('selected', original)
            for supplied_revision in (1, 2):
                with self.subTest(supplied_revision=supplied_revision):
                    daemon = SimpleNamespace(runtime={}, _delivery_store=DeliveryStore(directory))
                    supplied = core.TargetRuntime(delivery_revision=supplied_revision,
                        send_attempt_id='uncommitted', delivery_status='retryable', send_count=99)
                    _restore_assignment_runtime(daemon, {'target': {'surface_id': 'selected'},
                                                        'runtime': supplied.to_dict()})
                    restored = daemon.runtime['selected']
                    self.assertEqual(restored.delivery_revision, 2)
                    self.assertEqual(restored.delivery_status, 'unknown')
                    self.assertEqual(restored.send_attempt_id, 'original')
                    self.assertEqual(restored.codex_sent_turn_key, 'session:original-turn')
                    self.assertEqual(restored.send_io_started_at, 20)
                    self.assertEqual(restored.send_count, 0)
                    self.assertEqual(set(daemon.runtime), {'selected'})

    def test_reassignment_never_regresses_newer_local_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            newer = core.TargetRuntime(delivery_revision=4, send_attempt_id='newer', delivery_status='unknown')
            daemon = SimpleNamespace(runtime={'selected': newer}, _delivery_store=DeliveryStore(directory))
            supplied = core.TargetRuntime(delivery_revision=2, send_attempt_id='stale')
            _restore_assignment_runtime(daemon, {'target': {'surface_id': 'selected'},
                                                'runtime': supplied.to_dict()})
            self.assertIs(daemon.runtime['selected'], newer)
            self.assertEqual(daemon.runtime['selected'].delivery_status, 'unknown')

    def test_original_lane_rejects_same_second_pid_reuse_and_changed_foreground(self):
        target = {'surface_id': 'surface', 'workspace_id': 'workspace'}
        binding = {'session_id': 'original', 'pid': 7, 'process_start': 100, 'birth': [100, 123]}
        with patch('ccc_codex_queue.process_placement_start', return_value=100), \
                patch('ccc_guard_scope.birth', return_value=[100, 123]) as birth:
            self.assertTrue(_binding_process_current(target, binding))
            birth.return_value = [100, 124]
            self.assertFalse(_binding_process_current(target, binding))
            birth.return_value = None
            self.assertFalse(_binding_process_current(target, binding))
        self.assertTrue(_binding_matches({**binding, 'kind': 'task_started'}, binding))
        for change in ({'birth': [100, 124]}, {'session_id': 'replacement'}, {'kind': 'unknown', 'pid': None}):
            self.assertFalse(_binding_matches({**binding, **change}, binding))

    @unittest.skipUnless(sys.version_info >= (3,14), 'native lanes use Python 3.14 interpreters')
    def test_real_lane_keeps_info_receipts_on_inherited_stderr(self):
        # Stop at construction, before config, sockets or native observation.
        # A fresh interpreter must initialize its own logging, even though
        # logging in the parent process has already been configured.
        child = '''
import logging
import cmux_codex_watch as core
from ccc_native_lanes import run_native_lane
def stop(self, *args):
    logging.getLogger(core.APP_NAME).info('lane-test-state-and-send-receipt')
    raise RuntimeError('construction-stop')
core.WatchDaemon.__init__ = stop
for _ in range(2):
    try:
        run_native_lane('{"config_path":"unused", "state_path":"unused"}', bytes([1]), 0, None, None)
    except RuntimeError as exc:
        assert str(exc) == 'construction-stop'
'''
        script = ("import logging\nfrom concurrent import interpreters\n"
                  "logging.basicConfig(level=logging.INFO)\n"
                  "lane=interpreters.create()\ntry:\n"
                  f"    lane.exec({child!r})\nfinally:\n    lane.close()\n")
        result = subprocess.run([sys.executable, '-B', '-c', script],
                                cwd=Path(core.__file__).parent,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stderr.splitlines()
                 if 'lane-test-state-and-send-receipt' in line]
        self.assertEqual(len(lines), 2, result.stderr)
        for line in lines:
            self.assertRegex(line, r'^\d{4}-\d{2}-\d{2} .* INFO lane-test-state-and-send-receipt$')

    def test_transient_initial_native_read_is_retried_without_rechecking_known_sources(self):
        recovery=Mock()
        target={'surface_id':'new','workspace_id':'owned'}
        existing={'surface_id':'known','workspace_id':'owned'}
        recovery.wakeup_sources.side_effect=[
            [{'surface_id':'known','identity_current':True}],
            [{'surface_id':'known','identity_current':True},{'surface_id':'new','identity_current':True}]]
        refresh_native_bindings(recovery,[target,existing])
        refresh_native_bindings(recovery,[target,existing])
        recovery.current_turn.assert_called_once_with(target)

    def test_revocation_never_reuses_an_old_assignment(self):
        cells=AuthorizationCells(3)
        first=cells.allocate()
        shared=cells.shared()
        cells.revoke(first)
        second=cells.allocate()
        self.assertNotEqual(first,second)
        self.assertEqual((shared[first],shared[second]),(0,1))
        with self.assertRaises(TypeError):
            shared[second]=0
        cells.revoke_all()
        self.assertEqual(shared[second],0)

    def wait_for(self,lane,kind):
        rows=[]
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            rows.extend(lane.drain())
            if any(row['type']==kind for row in rows):
                return rows
            time.sleep(.01)
        self.fail(f'no {kind}: {rows}, error={lane.error}')

    def lane(self,root,cells):
        config=root/'config.json'
        core.atomic_write_json(config,core.default_config())
        return InterpreterLane(Path(core.__file__).parent,
            {'config_path':str(config),'state_path':str(root/'state.json'),
             'capabilities':{'protocol':'cmux-socket','version':2,
                'socket_path':str(root/'no-controller.sock'),'access_mode':'automation','methods':[]}},cells)

    @unittest.skipUnless(sys.version_info >= (3,14), 'native lanes use Python 3.14 interpreters')
    def test_empty_real_interpreter_starts_and_quiesces_without_cmux(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cells=AuthorizationCells()
            lane=self.lane(root,cells)
            try:
                rows=self.wait_for(lane,'ready')
                self.assertFalse(any(r['type']=='failure' for r in rows))
                lane.update([])
            finally:
                lane.close()
            self.assertIsNone(lane.error)
            self.assertEqual(cells.shared()[lane.cell],0)
            self.assertFalse((root/'state.json').exists())
            self.assertTrue(any(r['type']=='stopped' for r in lane.drain()))

    @unittest.skipUnless(sys.version_info >= (3,14), 'native lanes use Python 3.14 interpreters')
    def test_real_interpreter_with_jev_key_starts_and_quiesces_without_cmux(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cells=AuthorizationCells()
            key=root/'typesafe-api-key'
            key.write_text('example-test-key\n')
            key.chmod(0o600)
            lane=self.lane(root,cells)
            try:
                rows=self.wait_for(lane,'ready')
                self.assertFalse(any(r['type']=='failure' for r in rows))
                lane.update([])
            finally:
                lane.close()
            self.assertIsNone(lane.error)
            self.assertEqual(cells.shared()[lane.cell],0)
            self.assertTrue(any(r['type']=='stopped' for r in lane.drain()))

    @unittest.skipUnless(sys.version_info >= (3,14), 'native lanes use Python 3.14 interpreters')
    def test_key_enabled_startup_failure_leaves_no_jev_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=root/'config.json'
            core.atomic_write_json(config,core.default_config())
            key=root/'typesafe-api-key'
            key.write_text('example-test-key\n');key.chmod(0o600)
            settings={'config_path':str(config),'state_path':str(root/'state.json'),
                'capabilities':{'protocol':'cmux-socket','version':2,
                    'socket_path':str(root/'missing.sock'),'access_mode':'automation','methods':[]}}
            child = '''
import ccc_native_lanes as native
import threading
original=native._create_lane_daemon
def create(settings):
    daemon=original(settings)
    assert daemon.jev_classifier is not None
    assert daemon.jev_classifier.thread is None
    def fail():
        raise RuntimeError('original-startup-failure')
    daemon._delivery_store.start=fail
    return daemon
native._create_lane_daemon=create
try:
    native.run_native_lane(SETTINGS,bytes([1]),0,None,None)
except RuntimeError as exc:
    assert str(exc)=='original-startup-failure'
else:
    raise AssertionError('expected startup failure')
assert not any(t.name=='ccc-jev' for t in threading.enumerate())
'''.replace('SETTINGS',repr(json.dumps(settings)))
            script=("from concurrent import interpreters\n"
                    "lane=interpreters.create()\ntry:\n"
                    "    lane.exec("+repr(child)+")\nfinally:\n    lane.close()\n")
            result=subprocess.run([sys.executable,'-B','-c',script],
                cwd=Path(core.__file__).parent,capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)

    @unittest.skipUnless(sys.version_info >= (3,14), 'native lanes use Python 3.14 interpreters')
    def test_malformed_assignment_fails_closed_and_revokes_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            cells=AuthorizationCells()
            lane=self.lane(Path(directory),cells)
            try:
                self.wait_for(lane,'ready')
                lane.update([{'target':{'surface_id':'duplicate'}}]*2)
                self.wait_for(lane,'failure')
            finally:
                lane.close()
            self.assertEqual(cells.shared()[lane.cell],0)
            self.assertIn('fifty unique assignments',lane.error)


if __name__=='__main__':
    unittest.main()

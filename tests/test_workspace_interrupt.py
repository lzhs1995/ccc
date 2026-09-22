"""Pool pause precedes input, survives failure, and dominates every registration."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import cmux_codex_watch as core
import cmux_supervisor_tui as tui
from tests.test_watch import FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload
from tests.test_cmux_viewport_socket import response, send, server


class WorkspaceInterruptTests(unittest.TestCase):
    def test_pool_gate_overrides_explicit_dynamic_and_future_targets_without_changing_them(self):
        config = core.default_config()
        config['workspace_rules'] = [{'workspace_id': 'pool', 'enabled': True, 'paused': True}]
        config['targets'] = [{'surface_id': 'explicit', 'workspace_id': 'pool', 'paused': False},
                             {'surface_id': 'other', 'workspace_id': 'other', 'paused': False}]
        dynamic = [{'surface_id': 'new', 'workspace_id': 'pool', 'paused': False}]
        original = copy.deepcopy(config)
        rows = {t['surface_id']: t for t in core.effective_targets(config, dynamic)}
        self.assertTrue(rows['explicit']['paused'])
        self.assertTrue(rows['new']['paused'])
        self.assertFalse(rows['other']['paused'])
        self.assertEqual(config, original)
        config['workspace_rules'][0]['paused'] = False
        self.assertFalse(any(t.get('paused') for t in core.effective_targets(config, dynamic)))

    def test_pause_is_durable_before_discovery_and_partial_input_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            store = core.ConfigStore(Path(directory) / 'config.json')
            store.mutate(lambda c: c['workspace_rules'].append({'workspace_id': 'pool', 'enabled': True}))
            client = mock.Mock()
            client.viewport_socket = None
            def interrupt(wid, sid):
                self.assertTrue(store.load()['workspace_rules'][0]['paused'])
                self.assertEqual(wid, 'pool')
                if sid == 'failed':
                    raise core.CmuxError('socket unavailable')
            client.interrupt_codex.side_effect = interrupt
            with mock.patch.object(core, 'discover_codex_surfaces', return_value=[
                {'surface_id': 'ok'}, {'surface_id': 'failed'}]):
                result = core.pause_workspace(store, 'pool', client)
            self.assertEqual(result['interrupt_requested'], ['ok'])
            self.assertEqual(result['failed'][0]['surface_id'], 'failed')
            self.assertTrue(store.load()['workspace_rules'][0]['paused'])
            client.tree.side_effect = core.CmuxError('discovery unavailable')
            with self.assertRaises(core.CmuxError):
                core.pause_workspace(store, 'pool', client)
            self.assertTrue(store.load()['workspace_rules'][0]['paused'])

    def test_pool_pause_during_persistence_cancels_send_even_for_explicit_target(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = grid_payload([], error=HIGH_DEMAND_TEXT)
            client = FakeClient(frame, '■ ' + HIGH_DEMAND_TEXT)
            daemon = armed_daemon(directory, client)
            self.addCleanup(daemon._process_snapshots.close)
            target = daemon.config['targets'][0]
            daemon.config_store.mutate(lambda c: c['workspace_rules'].append({
                'workspace_id': target['workspace_id'], 'enabled': True}))
            daemon._reload_config_if_changed()
            original_save = daemon.save
            def pause_during_save(**kwargs):
                original_save(**kwargs)
                daemon.config_store.mutate(lambda c: c['workspace_rules'][0].update(paused=True))
            with mock.patch.object(daemon, 'save', side_effect=pause_during_save):
                daemon.process_once(client)
            self.assertEqual(client.sent, [])

    def test_exclusive_interrupt_waits_for_active_shared_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            acquired = threading.Event()
            def pause():
                with core.workspace_input_lock(path, 'pool'):
                    acquired.set()
            with core.workspace_input_lock(path, 'pool', shared=True):
                worker = threading.Thread(target=pause)
                worker.start()
                self.assertFalse(acquired.wait(.1))
            worker.join(2)
            self.assertTrue(acquired.is_set())

    def test_interrupt_is_one_explicit_escape_without_submit_or_session_exit(self):
        with server(lambda conn, req: send(conn, response(req))) as (transport, requests):
            transport.control_methods = frozenset({'surface.send_key'})
            client = core.CmuxClient(viewport_socket=transport,
                runner=mock.Mock(side_effect=AssertionError('CLI fallback')))
            client.interrupt_codex('pool', 'surface')
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]['method'], 'surface.send_key')
            self.assertEqual(requests[0]['params'], {
                'workspace_id': 'pool', 'surface_id': 'surface', 'key': 'escape'})

    def test_ui_selected_surface_targets_its_workspace(self):
        model = object.__new__(tui.SupervisorModel)
        model.run_cli = mock.Mock()
        candidate = tui.Candidate({'surface_id': 'surface', 'workspace_id': 'pool',
            'workspace_ref': 'workspace:2', 'ref': 'surface:3'}, 'workspace_rule', '', '', 0, False)
        model.mutate_selected(candidate, 'pause_workspace')
        model.run_cli.assert_called_once_with(['pause-workspace', 'pool'])
        self.assertIn('Interrupt', tui.confirm_prompt('pause_workspace', candidate))


if __name__ == '__main__':
    unittest.main()

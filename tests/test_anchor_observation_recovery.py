"""An unanchored frame denies input without permanently unenrolling a client."""
import tempfile
import unittest
from unittest import mock

import cmux_codex_watch as core
from tests.test_watch import FakeClient, armed_daemon, grid_payload


class AnchorObservationRecoveryTests(unittest.TestCase):
    def missing_grid_error(self):
        client = core.CmuxClient('unused')
        client.viewport_socket = mock.Mock(live_native_frames=False)
        client.viewport_socket.request.return_value = {
            'workspace_id': 'workspace-uuid', 'surface_id': 'surface-uuid'}
        with self.assertRaises(core.ViewportUnavailableError) as caught:
            core.Grid.from_rpc(client.replay('workspace-uuid', 'surface-uuid'), 'surface-uuid')
        return caught.exception

    def test_missing_grid_recovers_after_both_reads_without_persisted_exclusion(self):
        for retry in (False, True):
            for dynamic in (False, True):
                with self.subTest(retry=retry, dynamic=dynamic), tempfile.TemporaryDirectory() as directory:
                    client = FakeClient(grid_payload([]), '')
                    daemon = armed_daemon(directory, client)
                    target = daemon.config['targets'][0]
                    if dynamic:
                        target = dict(target, source='workspace_rule', source_workspace_id='workspace-uuid')
                        daemon._mutate_config(lambda config: config.update(workspace_rules=[{
                            'workspace_id': 'workspace-uuid', 'enabled': True,
                            'excluded_surface_ids': [], 'excluded_surface_reasons': {}}]))
                    before = daemon.config_path.read_bytes()
                    failures = ([core.CmuxError('workspace moved')] if retry else []) + [self.missing_grid_error()]
                    with mock.patch.object(daemon, '_observe_target_viewport', side_effect=failures), \
                         mock.patch.object(daemon, '_refresh_workspace', return_value=True):
                        daemon._process_one_target(target, client, None, '')
                    self.assertEqual(client.sent, [])
                    self.assertFalse(target.get('paused'))
                    self.assertEqual(daemon.config_path.read_bytes(), before)
                    self.assertEqual(daemon.runtime['surface-uuid'].state, 'cmux_unavailable')
                    with mock.patch.object(daemon, '_observe_target_viewport', return_value=core.ScreenState('working')) as observe:
                        daemon._process_one_target(target, client, None, '')
                    observe.assert_called_once()
                    self.assertEqual(daemon.runtime['surface-uuid'].state, 'working')
                    self.assertEqual(daemon.config_path.read_bytes(), before)
                    self.assertEqual(client.sent, [])

    def anchor_error(self):
        client = core.CmuxClient('unused')
        client.viewport_socket = mock.Mock(live_native_frames=True)
        client.viewport_socket.request.return_value = {
            'workspace_id': 'workspace-uuid', 'surface_id': 'surface-uuid',
            'render_grid': {'surface_id': 'surface-uuid', 'anchor': 'viewport'}}
        with self.assertRaises(core.IncompatibleError) as caught:
            client.replay('workspace-uuid', 'surface-uuid')
        return caught.exception

    def test_anchor_gap_does_not_persist_pause_on_either_read_path(self):
        for retry in (False, True):
            with self.subTest(retry=retry), tempfile.TemporaryDirectory() as directory:
                client = FakeClient(grid_payload([]), '')
                daemon = armed_daemon(directory, client)
                target = daemon.config['targets'][0]
                before = daemon.config_path.read_bytes()
                failures = ([core.CmuxError('workspace moved')] if retry else []) + [self.anchor_error()]
                with mock.patch.object(daemon, '_observe_target_viewport', side_effect=failures), \
                     mock.patch.object(daemon, '_refresh_workspace', return_value=True):
                    daemon._process_one_target(target, client, None, '')
                self.assertEqual(client.sent, [])
                self.assertFalse(target.get('paused'))
                self.assertEqual(daemon.config_path.read_bytes(), before)
                self.assertEqual(daemon.runtime['surface-uuid'].state, 'viewport_unanchored')
                with mock.patch.object(daemon, '_observe_target_viewport', return_value=core.ScreenState('working')) as observe:
                    daemon.process_once(client)
                self.assertTrue(observe.called)
                self.assertEqual(client.sent, [])

    def test_dynamic_anchor_gap_does_not_write_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), '')
            daemon = armed_daemon(directory, client)
            target = dict(daemon.config['targets'][0], source='workspace_rule',
                          source_workspace_id='workspace-uuid')
            daemon._mutate_config(lambda config: config.update(workspace_rules=[{
                'workspace_id': 'workspace-uuid', 'enabled': True,
                'excluded_surface_ids': [], 'excluded_surface_reasons': {}}]))
            before = daemon.config_path.read_bytes()
            with mock.patch.object(daemon, '_observe_target_viewport', side_effect=self.anchor_error()):
                daemon._process_one_target(target, client, None, '')
            self.assertEqual(daemon.config_path.read_bytes(), before)
            self.assertFalse(target.get('paused'))
            self.assertEqual(client.sent, [])

    def test_user_pause_still_prevents_observation_and_send(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([]), '')
            daemon = armed_daemon(directory, client, extra_targets=[{
                'surface_id': 'surface-uuid', 'workspace_id': 'workspace-uuid',
                'enabled': True, 'paused': True, 'pause_origin': 'user'}])
            with mock.patch.object(daemon, '_observe_target_viewport') as observe:
                daemon.process_once(client)
            observe.assert_not_called()
            self.assertEqual(client.sent, [])

    def test_foreign_frame_still_raises_nontransient_incompatibility(self):
        for field in ('workspace_id', 'surface_id'):
            with self.subTest(field=field):
                client = core.CmuxClient('unused')
                client.viewport_socket = mock.Mock(live_native_frames=True)
                response = {'workspace_id': 'workspace-uuid', 'surface_id': 'surface-uuid',
                            'render_grid': {'anchor': 'viewport'}}
                response[field] = 'foreign'
                client.viewport_socket.request.return_value = response
                with self.assertRaises(core.IncompatibleError) as caught:
                    client.replay('workspace-uuid', 'surface-uuid')
                self.assertNotIsInstance(caught.exception, core.UnanchoredFrameError)
                self.assertNotIsInstance(caught.exception, core.ViewportUnavailableError)
                response.pop('render_grid')
                with self.assertRaises(core.IncompatibleError) as missing:
                    client.replay('workspace-uuid', 'surface-uuid')
                self.assertNotIsInstance(missing.exception, core.ViewportUnavailableError)


if __name__ == '__main__':
    unittest.main()

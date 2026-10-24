"""Shared-daemon discovery, naming and observation must use explicit identities."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cmux_bridge as bridge
import cmux_workspace_guard as guard
import mac_harness as harness
from test_cmux_workspace_guard import snapshots, W, X, C, T


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        identity, tree = snapshots()
        self.snapshot = (identity, tree, {'CMUX_WORKSPACE_ID': W, 'CMUX_SURFACE_ID': C}, {'verified': True})
        self.me = {'workspace_ref': 'workspace:1', 'surface_ref': 'surface:1',
                   'workspace_id': W, 'surface_id': C, 'provider': 'codex',
                   'pane_ref': 'pane:1'}
        bridge._workspace_pins.clear()
        self.addCleanup(bridge._workspace_pins.clear)

    def test_inventory_excludes_daemon_origin_and_global_dock(self):
        foreign = copy.deepcopy(self.snapshot[1]['windows'][0]['workspaces'][0])
        foreign.update(id=X, ref='workspace:15')
        foreign['panes'][0]['surfaces'][0].update(ref='surface:46', id=X)
        self.snapshot[1]['windows'][0]['workspaces'].append(foreign)
        with patch.object(guard, 'caller_snapshot', return_value=self.snapshot), patch.object(bridge, '_run') as run:
            rows = bridge.list_surfaces()
        self.assertEqual([r['ref'] for r in rows], ['surface:1', 'surface:2'])
        self.assertTrue(all(r['workspace_id'] == W for r in rows))
        run.assert_not_called()

    def test_managed_provider_ignores_stale_title(self):
        with patch.object(guard, 'caller_snapshot', return_value=self.snapshot):
            self.assertEqual(bridge.whoami()['provider'], 'codex')

    def test_unknown_inventory_workspace_denies(self):
        with patch.object(guard, 'caller_snapshot', return_value=self.snapshot):
            with self.assertRaises(guard.WorkspaceScopeError):
                bridge.list_surfaces(workspace=X)

    def test_inventory_marks_caller_not_focused_executor(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(harness, '_ensure_root'), \
             patch.object(harness, '_artifact_root', return_value=Path(temp)), \
             patch.object(bridge, 'whoami', return_value=self.me), \
             patch.object(bridge, 'list_surfaces', return_value=[
                 {'ref': 'surface:1', 'title': 'codex', 'selected': False},
                 {'ref': 'surface:2', 'title': 'claude', 'selected': True}]) as listing:
            harness.cmd_surface_inventory(SimpleNamespace(task_id='test', json=True))
            rows = json.loads((Path(temp) / 'surface-inventory.json').read_text())['surfaces_same_workspace']
            self.assertEqual(rows[0]['role'], 'supervisor_candidate')
            self.assertNotIn('role', rows[1])
            listing.assert_called_once_with(workspace=W)

    def test_rename_caller_uses_both_uuids(self):
        with patch.object(bridge, 'whoami', return_value=self.me), patch.object(bridge, '_run') as run:
            bridge.rename_tab('surface:1', 'supervisor', workspace_uuid=W, surface_uuid=C, caller_uuid=C)
            run.assert_called_once_with('rename-tab', '--workspace', W, '--surface', C, '--', 'supervisor')

    def test_rename_caller_drift_denies_without_mutation(self):
        for field in ('workspace_id', 'surface_id'):
            with self.subTest(field=field), patch.object(bridge, 'whoami', return_value={**self.me, field: X}), \
                 patch.object(bridge, '_run') as run:
                with self.assertRaises(guard.WorkspaceScopeError):
                    bridge.rename_tab('surface:1', 'label', workspace_uuid=W, surface_uuid=C, caller_uuid=C)
                run.assert_not_called()

    def test_rename_peer_rechecks_pins_and_denies_drift(self):
        with patch.object(bridge, 'pin_workspace', side_effect=guard.WorkspaceScopeError('drift')) as pin, \
             patch.object(bridge, '_run') as run:
            with self.assertRaises(guard.WorkspaceScopeError):
                bridge.rename_tab('surface:2', 'label', workspace_uuid=W, surface_uuid=T, caller_uuid=C)
            pin.assert_called_once_with('surface:2', workspace_uuid=W, target_uuid=T, caller_uuid=C)
            run.assert_not_called()

    def test_pinned_screen_reads_matching_workspace(self):
        bridge._workspace_pins['surface:2'] = {}
        with patch.object(bridge, 'pin_workspace', return_value={'target_surface_uuid': T, 'workspace_uuid': W}), \
             patch.object(bridge, '_run', return_value='screen') as run:
            self.assertEqual(bridge.read_screen('surface:2', 40), 'screen')
            run.assert_called_once_with('read-screen', '--workspace', W, '--surface', T, '--lines', '40')

    def test_unmapped_read_does_not_use_daemon_workspace(self):
        with patch.object(bridge, 'surface_uuid_map', return_value={}), patch.object(bridge, '_run') as run:
            with self.assertRaises(guard.WorkspaceScopeError):
                bridge.read_screen('surface:2')
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()

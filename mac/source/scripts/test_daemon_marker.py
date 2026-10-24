"""Managed caller ownership must apply to task markers as well as sends."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cmux_workspace_guard as guard
import mac_harness as harness


class MarkerOwnershipTests(unittest.TestCase):
    def test_managed_arm_and_disarm_preserve_daemon_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            foreign = root / 'daemon-origin' / 'foreign.json'
            foreign.parent.mkdir()
            foreign.write_text(json.dumps({'task_id': 'same-task'}))
            original = foreign.read_bytes()
            with patch.object(harness, '_ACTIVE_DIR', root), \
                 patch.dict(os.environ, {'CMUX_WORKSPACE_ID': 'daemon-origin'}), \
                 patch.object(guard.daemon_identity, 'collect', return_value={'client': {}}), \
                 patch.object(guard, 'caller_snapshot', return_value=({}, {}, {'CMUX_WORKSPACE_ID': 'actual-client'})):
                marker = harness.arm_task('same-task', root / 'output', executor='surface:2')
                self.assertEqual(marker['workspace_id'], 'actual-client')
                self.assertEqual(marker['workspace_uuid'], 'actual-client')
                self.assertEqual(len(list((root / 'actual-client').glob('*.json'))), 1)
                self.assertEqual(harness.disarm_task('same-task'), 1)
                self.assertEqual(foreign.read_bytes(), original)
                self.assertEqual(os.environ['CMUX_WORKSPACE_ID'], 'daemon-origin')

    def test_ambiguous_process_cannot_create_marker(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(harness, '_ACTIVE_DIR', Path(temp)), \
             patch.object(guard.daemon_identity, 'collect', side_effect=guard.daemon_identity.IdentityError('ambiguous')):
            with self.assertRaises(guard.daemon_identity.IdentityError):
                harness.arm_task('task', Path(temp) / 'output', executor='surface:2')
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_drift_cannot_disarm(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = Path(temp) / 'client' / 'task.json'
            marker.parent.mkdir()
            marker.write_text('{"task_id":"task"}')
            before = marker.read_bytes()
            with patch.object(harness, '_ACTIVE_DIR', Path(temp)), \
                 patch.object(guard.daemon_identity, 'collect', return_value={'client': {}}), \
                 patch.object(guard, 'caller_snapshot', side_effect=guard.daemon_identity.IdentityError('drift')):
                with self.assertRaises(guard.daemon_identity.IdentityError):
                    harness.disarm_task('task')
            self.assertEqual(marker.read_bytes(), before)

    def test_ordinary_client_keeps_its_workspace(self):
        with patch.dict(os.environ, {'CMUX_WORKSPACE_ID': 'ordinary'}), \
             patch.object(guard.daemon_identity, 'collect', return_value=None), \
             patch.object(guard, 'caller_snapshot') as snapshot:
            self.assertEqual(harness._workspace_key(), 'ordinary')
            snapshot.assert_not_called()


if __name__ == '__main__':
    unittest.main()

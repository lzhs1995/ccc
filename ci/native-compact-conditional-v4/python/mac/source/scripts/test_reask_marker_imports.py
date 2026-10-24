"""A fresh file observer must enforce boundaries without importing transport."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROBE = r'''
import copy
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
forbidden = {'mac_harness', 'cmux_bridge', 'cmux_claude_interruption', 'render_cmux_agent'}
reading = False
def guard(event, args):
    if event == 'import' and args[0].split('.')[0] in forbidden:
        raise AssertionError('transport import: ' + args[0])
    if reading and event == 'open' and isinstance(args[2], int):
        assert not args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
    if reading and event in {'os.mkdir', 'os.rename', 'os.remove', 'os.rmdir',
                             'os.chmod', 'os.chown', 'os.link', 'os.symlink',
                             'os.truncate', 'socket.connect', 'subprocess.Popen',
                             'os.system', 'os.fork', 'os.posix_spawn'}:
        raise AssertionError('observer side effect: ' + event)
sys.addaudithook(guard)

import executor_ready as ready
import executor_reask as reask
import executor_reask_lifecycle as lifecycle

root, scenario = Path(sys.argv[2]).resolve(), sys.argv[3]
ready.state_root = lambda: root / 'state'
lifecycle.ACTIVE_DIR = root / 'active'
original = dict(state=reask.WAITING, supervisor='original-supervisor',
                caller_surface_uuid='original-executor', workspace_uuid='workspace',
                started_epoch=1000., controller={'original': 'unchanged'},
                terminal_pending={'status': 'UNKNOWN', 'nonce': 'unchanged'},
                reply={'body': 'original reply'}, asks=[])
marker = dict(collaboration_id='reservation', task_id='child-task',
              workspace_uuid='workspace', armed_at='1970-01-01T00:16:41+00:00',
              ttl_seconds=1,
              participants=[dict(role='supervisor', surface_uuid='original-supervisor'),
                            dict(role='executor', surface_uuid='original-executor')])
if scenario == 'foreign':
    marker['participants'][0]['surface_uuid'] = 'new-supervisor'
if scenario == 'wrong_workspace':
    marker['workspace_uuid'] = 'different-workspace'
if scenario == 'missing':
    original['paused_tasks'] = [dict(collaboration_id='reservation', task_id='child-task')]
else:
    path = (lifecycle.ACTIVE_DIR / 'workspace.json' if scenario == 'legacy'
            else lifecycle.ACTIVE_DIR / 'workspace' / 'reservation.json')
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(marker))
if scenario == 'stop':
    path = reask.stop_file(original['caller_surface_uuid'])
    path.parent.mkdir(parents=True)
    path.write_text('{}')
if scenario == 'revoked':
    path = root / 'authorization.txt'
    path.write_text('original authorization')
    original['continuation'] = dict(enabled=True, status='ACTIVE', until_epoch=None,
                                    authorization=lifecycle.authorization_pin(path))
    path.write_text('changed authorization')
state = copy.deepcopy(original)
reading = True
lifecycle.observe_formal_tasks(state, 1003., publish=False)
reading = False
assert not forbidden.intersection(sys.modules)
for key in ('controller', 'terminal_pending', 'reply', 'asks'):
    assert state[key] == original[key], key
print(json.dumps(state))
'''


class MarkerImportTests(unittest.TestCase):
    def test_harness_loads_by_path_in_an_isolated_interpreter(self):
        harness = Path(__file__).resolve().with_name('mac_harness.py')
        probe = '''
import importlib.util
from pathlib import Path
import sys
assert str(Path(sys.argv[1]).parent) not in sys.path
spec = importlib.util.spec_from_file_location('isolated_mac_harness', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module._ACTIVE_DIR == Path('/tmp/multi-agent-collaboration/_active')
'''
        with tempfile.TemporaryDirectory(prefix='harness-by-path-') as directory:
            result = subprocess.run(
                [sys.executable, '-B', '-I', '-c', probe, str(harness)],
                cwd=directory, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def observe(self, scenario):
        with tempfile.TemporaryDirectory(prefix='marker-imports-') as directory:
            result = subprocess.run(
                [sys.executable, '-B', '-c', PROBE, str(Path(__file__).parent), directory, scenario],
                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_reservation_and_legacy_layout_without_transport_imports(self):
        for scenario in ('reservation', 'legacy'):
            with self.subTest(scenario=scenario):
                state = self.observe(scenario)
                self.assertEqual(state['state'], 'WAITING_REPLY')
                self.assertEqual(state['paused_tasks'], [dict(
                    collaboration_id='reservation', task_id='child-task')])

    def test_foreign_owner_stop_and_revocation_without_transport_imports(self):
        for scenario, reason in (('foreign', 'FORMAL_OWNER_CHANGED'),
                                 ('stop', 'OPERATOR_STOP'), ('revoked', 'AUTHORIZATION_CHANGED')):
            with self.subTest(scenario=scenario):
                state = self.observe(scenario)
                self.assertEqual(state['state'], 'STOPPED')
                self.assertEqual(state['exit_reason'], reason)

    def test_missing_marker_never_releases_and_foreign_workspace_never_reserves(self):
        state = self.observe('missing')
        self.assertEqual(state['paused_tasks'], [dict(
            collaboration_id='reservation', task_id='child-task')])
        self.assertNotIn('paused_tasks', self.observe('wrong_workspace'))


if __name__ == '__main__':
    unittest.main()

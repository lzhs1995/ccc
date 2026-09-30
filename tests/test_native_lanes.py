"""Real interpreter startup/teardown without targets or native input."""
import json
from pathlib import Path
import tempfile
import time
import unittest
import sys
import subprocess
from unittest.mock import Mock

import cmux_codex_watch as core
from ccc_native_lanes import AuthorizationCells, InterpreterLane, refresh_native_bindings


class NativeLaneTests(unittest.TestCase):
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

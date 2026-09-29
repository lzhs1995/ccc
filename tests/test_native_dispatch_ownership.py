"""Parent/child ownership cannot overlap during normal input or recovery."""
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cmux_codex_watch as core
from ccc_native_lanes import NativeDispatcher
from tests.test_watch import FakeClient, armed_daemon, grid_payload


class FakeLane:
    def __init__(self,source,initial,cells):
        self.cells=cells;self.cell=cells.allocate();self.assignments=[];self.rows=[]
    def update(self,assignments):self.assignments=list(assignments)
    def drain(self):
        rows,self.rows=self.rows,[]
        return rows
    def close(self):
        assert not self.cells.shared()[self.cell]
        assert all(not self.cells.shared()[a['cell']] for a in self.assignments)
        self.rows.append({'type':'stopped'})


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.daemon=armed_daemon(self.temp.name,FakeClient(grid_payload([])))
        self.daemon._start_scheduler()
        self.addCleanup(self.daemon._scheduler.close)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.targets=list(self.daemon.config['targets'])
        self.sid=self.targets[0]['surface_id']
        self.source={'surface_id':self.sid,'workspace_id':self.targets[0]['workspace_id'],
                     'session_id':'original','pid':1,'process_start':1,'identity_current':True}
        self.daemon.codex_queue_recovery.wakeup_sources=lambda _: [dict(self.source)]
        self.daemon.codex_queue_recovery.current_turn=lambda _:dict(self.source)
        self.factory=patch('ccc_native_lanes.InterpreterLane',FakeLane);self.factory.start();self.addCleanup(self.factory.stop)
        self.dispatch=NativeDispatcher(self.daemon,{})
        self.daemon._native_dispatch=self.dispatch
        self.addCleanup(self.dispatch.close)

    def refresh(self,targets=None):
        self.dispatch._next_refresh=0
        self.dispatch.refresh(self.targets if targets is None else targets)

    def test_parent_is_excluded_before_child_can_receive_assignment(self):
        self.refresh()
        self.assertTrue(self.dispatch.owns(self.sid))
        self.assertIsNone(self.daemon._active_send_target(self.targets[0]))
        self.assertEqual(self.dispatch.filter(self.targets,self.targets)[0],[])
        self.assertEqual(len(self.dispatch.lanes[0]['lane'].assignments),1)

    def test_inflight_original_operation_prevents_ownership_transfer(self):
        with patch.object(self.daemon._scheduler,'quiescent',return_value=False):
            self.refresh()
        self.assertFalse(self.dispatch.owns(self.sid))
        self.assertEqual(self.dispatch.lanes,[])
        self.refresh()
        self.assertTrue(self.dispatch.owns(self.sid))

    def test_advisory_binding_cannot_steal_a_replaced_native_process(self):
        self.daemon.codex_queue_recovery.current_turn=lambda _:{'kind':'unknown'}
        self.refresh()
        self.assertFalse(self.dispatch.owns(self.sid))
        self.assertEqual(self.dispatch.lanes,[])

    def test_hookless_working_native_is_discovered_before_any_failed_turn(self):
        bound = threading.Event()
        original = dict(self.source)
        self.daemon.codex_queue_recovery.wakeup_sources = lambda _:[original] if bound.is_set() else []
        def current(_):
            bound.set()
            return {**original,'kind':'task_started'}
        self.daemon.codex_queue_recovery.current_turn = current
        with patch.object(self.daemon._native_process_index,'lookup',return_value={
                'agent_kind':'codex','agent_pids':[1]}):
            self.refresh()
            self.assertTrue(bound.wait(2))
            self.refresh()
        self.assertTrue(self.dispatch.owns(self.sid))

    def test_background_native_discovery_does_not_authorize_changed_identity(self):
        self.daemon.codex_queue_recovery.wakeup_sources = lambda _:[]
        self.daemon.codex_queue_recovery.current_turn = lambda _:{'kind':'unknown'}
        with patch.object(self.daemon._native_process_index,'lookup',return_value={
                'agent_kind':'codex','agent_pids':[1]}):
            self.refresh()
        self.assertFalse(self.dispatch.owns(self.sid))

    def test_removed_target_stays_excluded_until_original_input_quiesces(self):
        self.refresh();owned=self.dispatch.owned[self.sid]
        cell=owned['assignment']['cell'];lane=owned['state']['lane']
        self.refresh([])
        self.assertEqual(self.dispatch.cells.shared()[cell],0)
        self.assertTrue(self.dispatch.owns(self.sid))
        self.assertEqual(lane.assignments,[])
        lane.rows.append({'type':'released','surface_id':self.sid,'cell':cell,'runtime':{}})
        self.refresh([])
        self.assertFalse(self.dispatch.owns(self.sid))

    def test_native_replacement_requires_new_cell_after_old_release(self):
        self.refresh();old=self.dispatch.owned[self.sid]
        cell=old['assignment']['cell'];lane=old['state']['lane']
        self.source.update(pid=2,session_id='replacement')
        self.refresh()
        self.assertEqual(self.dispatch.cells.shared()[cell],0)
        self.assertEqual(self.dispatch.owned[self.sid]['assignment']['binding']['pid'],1)
        lane.rows.append({'type':'released','surface_id':self.sid,'cell':cell,'runtime':{}})
        self.refresh()
        replacement=self.dispatch.owned[self.sid]['assignment']
        self.assertNotEqual(replacement['cell'],cell)
        self.assertEqual(replacement['binding']['pid'],2)

    def test_failed_lane_cannot_release_parent_before_final_receipt(self):
        self.refresh();owned=self.dispatch.owned[self.sid]
        lane=owned['state']['lane'];cell=owned['assignment']['cell']
        lane.rows.append({'type':'failure','error':'worker exception'})
        self.refresh()
        self.assertTrue(self.dispatch.owns(self.sid))
        lane.rows.extend([{'type':'runtime','rows':{self.sid:{'cell':cell,
            'runtime':{'delivery_revision':3,'delivery_status':'unknown','send_attempt_id':'original'}}}},
            {'type':'stopped'}])
        self.refresh([])
        self.assertFalse(self.dispatch.owns(self.sid))
        self.assertEqual(self.daemon.runtime[self.sid].delivery_status,'unknown')
        self.assertEqual(self.daemon.runtime[self.sid].delivery_revision,3)

    def test_stale_lane_publication_cannot_overwrite_new_owner(self):
        self.refresh();owned=self.dispatch.owned[self.sid]
        self.dispatch._merge(self.sid,owned['assignment']['cell']+100,
            {'delivery_revision':100,'delivery_status':'accepted'})
        self.assertNotIn(self.sid,self.daemon.runtime)


if __name__=='__main__':unittest.main()

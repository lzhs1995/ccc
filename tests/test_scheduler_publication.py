"""Cached scheduling generations still revoke stale readers immediately."""
import copy
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cmux_codex_watch as core
from ccc_scheduling import SurfaceScheduler, TreeSnapshot
from tests.test_watch import FakeClient, armed_daemon, grid_payload


class PublicationTests(unittest.TestCase):
    def test_new_pause_publication_revokes_inflight_read_at_same_clock(self):
        entered, release = threading.Event(), threading.Event()
        current_checks, sent = [], []
        def observe(target, current):
            current_checks.append(current)
            entered.set()
            release.wait(2)
            return 'error'
        scheduler = SurfaceScheduler(observe, lambda *_: sent.append(True), clock=lambda: 100)
        target = {'surface_id':'sid', 'workspace_id':'wid'}
        try:
            scheduler.tick([target], generation=1, publication=object())
            self.assertTrue(entered.wait(1))
            scheduler.tick([{**target,'paused':True}], generation=2, publication=object())
            self.assertFalse(current_checks[0]())
            release.set()
            scheduler.close()
            self.assertEqual(sent, [])
        finally:
            release.set()
            scheduler.close()

    def test_hint_supersedes_old_read_even_without_new_publication(self):
        entered, release, event = threading.Event(), threading.Event(), threading.Event()
        checks = []
        def observe(target, current):
            checks.append(current)
            entered.set()
            release.wait(2)
        def react(target, current):
            self.assertTrue(current())
            event.set()
        scheduler = SurfaceScheduler(observe, lambda *_: None, clock=lambda:100,
            event_workers=1, event_handler=react, supersede_reads=True)
        target, token = {'surface_id':'sid','workspace_id':'wid'}, object()
        try:
            scheduler.tick([target], generation=1, publication=token)
            self.assertTrue(entered.wait(1))
            scheduler.request_observation('sid','wid')
            scheduler.tick([target], generation=1, publication=token)
            self.assertTrue(event.wait(1))
            self.assertFalse(checks[0]())
        finally:
            release.set()
            scheduler.close()

    def test_lost_native_coverage_refreshes_without_registration_change(self):
        now, covered, calls = [100.0], [True], []
        scheduler = SurfaceScheduler(lambda *_:calls.append(now[0]), lambda *_:None,
            clock=lambda:now[0], observation_interval=lambda target, base:10 if covered[0] else base)
        target, token = {'surface_id':'sid','workspace_id':'wid'}, object()
        def pump(count):
            deadline=time.monotonic()+1
            while len(calls)<count and time.monotonic()<deadline:
                scheduler.tick([target], generation=1, publication=token)
                scheduler.wakeup.wait(.001)
            self.assertEqual(len(calls),count)
        try:
            pump(1)
            scheduler.tick([target], generation=1, publication=token)
            covered[0]=False
            now[0]=100.051
            pump(2)
        finally:
            scheduler.close()

    def test_daemon_republishes_replaced_config_and_discovered_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon=armed_daemon(directory,FakeClient(grid_payload([])))
            try:
                first, token=daemon._scheduling_targets()
                self.assertIs(daemon._scheduling_targets()[0],first)
                self.assertIs(daemon._scheduling_targets()[1],token)
                daemon.config=copy.deepcopy(daemon.config)
                daemon.config['targets'][0]['paused']=True
                daemon._observation_policy=core.ObservationPolicy(daemon.config)
                _, paused=daemon._scheduling_targets()
                self.assertIsNot(paused,token)
                daemon.dynamic_targets=dict(daemon.dynamic_targets)
                self.assertIsNot(daemon._scheduling_targets()[1],paused)
            finally:
                daemon._process_snapshots.close()
                daemon._delivery_store.close()


class TopologyIndexTests(unittest.TestCase):
    def test_rpc_source_mutation_cannot_change_existing_snapshot_or_forge_index(self):
        raw={'surfaces':[{'id':'dock','ref':'surface:2','dock_scope':'global'}],
             'dock_surface_ids':[]}
        snapshot=TreeSnapshot(raw)
        raw['surfaces'][0].pop('dock_scope')
        self.assertTrue(core.is_dock_surface(snapshot,'dock'))
        self.assertFalse(core.is_dock_surface(TreeSnapshot(raw),'dock'))
        raw['surfaces'][0]['dock_scope']='global'
        self.assertTrue(core.is_dock_surface(raw,'dock'))

    def test_repeated_input_checks_use_the_same_complete_snapshot_index(self):
        snapshot=TreeSnapshot({'surfaces':[{'id':'dock','ref':'surface:2','dock_scope':'global'}]})
        with patch.object(core,'dock_surface_records',side_effect=AssertionError('repeated full topology scan')):
            for _ in range(500):
                self.assertTrue(core.is_dock_surface(snapshot,'dock'))
                self.assertFalse(core.is_dock_surface(snapshot,'main'))


if __name__ == '__main__':
    unittest.main()

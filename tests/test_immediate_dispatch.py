"""Deadline paths keep actual input gates and independent durable evidence."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import cmux_codex_watch as core
from ccc_delivery import DeliveryStore
from ccc_codex_queue import task_snapshot
from ccc_scheduling import CoalescingWriter, SurfaceScheduler
from tests.test_watch import FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload
from tests.native_failure_fixture import bind_native_failure


class ImmediateSchedulerTests(unittest.TestCase):
    def test_native_event_supersedes_its_own_slow_read_without_late_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            held, release = threading.Event(), threading.Event()
            client = FakeClient(grid_payload([], error=HIGH_DEMAND_TEXT), '■ ' + HIGH_DEMAND_TEXT)
            daemon = armed_daemon(directory, client)
            bind_native_failure(daemon, HIGH_DEMAND_TEXT)
            read = client.read_screen
            first = [True]
            def delayed(*args):
                if first[0]:
                    first[0] = False
                    held.set()
                    release.wait(3)
                return read(*args)
            client.read_screen = delayed
            scheduler = daemon._start_scheduler()
            targets = daemon.config['targets']
            try:
                scheduler.tick(targets)
                self.assertTrue(held.wait(1))
                began = time.monotonic()
                scheduler.request_observation('surface-uuid', 'workspace-uuid')
                while not client.sent and time.monotonic() - began < .8:
                    scheduler.tick(targets)
                    time.sleep(.002)
                self.assertEqual(len(client.sent), 1)
                self.assertFalse(release.is_set())
                self.assertLess(time.monotonic() - began, 1)
                release.set()
            finally:
                release.set()
                scheduler.close()
                daemon._process_snapshots.close()
            self.assertEqual(len(client.sent), 1)

    def test_native_failure_bypasses_slow_regular_observation(self):
        held, release, sent = threading.Event(), threading.Event(), threading.Event()
        def observe(target, current):
            if target['surface_id'] == 'slow':
                held.set()
                release.wait(3)
                return None
            return target if current() else None
        def send(target, candidate, current):
            if current():
                sent.set()
        scheduler = SurfaceScheduler(observe, send, observe_workers=1, send_workers=1, event_workers=2)
        targets = [{'surface_id': s, 'workspace_id': 'w'} for s in ('slow', 'fast')]
        try:
            scheduler.tick(targets)
            self.assertTrue(held.wait(1))
            began = time.monotonic()
            scheduler.request_observation('fast', 'w')
            scheduler.tick(targets)
            self.assertTrue(sent.wait(.8), 'native event queued behind slow periodic read')
            self.assertLess(time.monotonic() - began, 1)
            self.assertFalse(release.is_set())
        finally:
            release.set()
            scheduler.close()

    def test_pause_after_native_observation_still_vetoes_input(self):
        observed, release = threading.Event(), threading.Event()
        sent = []
        def observe(target, current):
            observed.set()
            release.wait(2)
            return target
        scheduler = SurfaceScheduler(observe, lambda *args: sent.append(args),
                                     observe_workers=1, event_workers=1)
        targets = [{'surface_id': 's', 'workspace_id': 'w'}]
        try:
            # Establish an idle slot without occupying the periodic worker.
            scheduler.tick([])
            scheduler.observe_workers = 0
            scheduler.tick(targets)
            scheduler.request_observation('s', 'w')
            scheduler.tick(targets)
            self.assertTrue(observed.wait(1))
            scheduler.tick([{**targets[0], 'paused': True}])
            release.set()
        finally:
            release.set()
            scheduler.close()
        self.assertEqual(sent, [])

    def test_fifty_native_events_run_without_eight_worker_send_queue(self):
        targets = [{'surface_id': str(i), 'workspace_id': 'w'} for i in range(50)]
        entered, release = set(), threading.Event()
        lock = threading.Lock()
        def send(target, candidate, current):
            with lock:
                entered.add(target['surface_id'])
            release.wait(2)
        scheduler = SurfaceScheduler(lambda target, current: target, send,
                                     observe_workers=1, send_workers=1, event_workers=50)
        try:
            scheduler.observe_workers = 0
            scheduler.tick(targets)
            for target in targets:
                scheduler.request_observation(target['surface_id'], 'w')
            began = time.monotonic()
            scheduler.tick(targets)
            while len(entered) < 50 and time.monotonic() - began < .9:
                time.sleep(.002)
            self.assertEqual(len(entered), 50)
            self.assertLess(time.monotonic() - began, 1)
        finally:
            release.set()
            scheduler.close()


class DeliveryDurabilityTests(unittest.TestCase):
    def test_individual_intent_does_not_wait_for_blocked_fleet_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([], error=HIGH_DEMAND_TEXT), '■ ' + HIGH_DEMAND_TEXT)
            daemon = armed_daemon(directory, client)
            held, release = threading.Event(), threading.Event()
            def blocked_writer():
                held.set()
                release.wait(3)
            daemon._state_writer = CoalescingWriter(blocked_writer)
            runtime = core.TargetRuntime(send_attempt_id='one', delivery_status='sending', send_started_at=time.time())
            try:
                daemon._state_writer.request(wait=False)
                self.assertTrue(held.wait(1))
                began = time.monotonic()
                daemon._save_delivery('surface-uuid', runtime, True)
                self.assertLess(time.monotonic() - began, .5)
                restored = {}
                daemon._delivery_store.restore(restored, core.TargetRuntime)
                self.assertEqual(restored['surface-uuid'].send_attempt_id, 'one')
                self.assertEqual(restored['surface-uuid'].delivery_status, 'sending')
            finally:
                release.set()
                daemon._state_writer.close()
                daemon._state_writer = None

    def test_corrupt_one_surface_does_not_block_other_surfaces(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(directory)
            bad = Path(directory) / (store.key('bad') + '.json')
            bad.write_text('{broken')
            store.restore({}, core.TargetRuntime)
            self.assertTrue(store.blocked('bad'))
            self.assertFalse(store.blocked('good'))
            store.persist('good', core.TargetRuntime(send_attempt_id='safe'))
            with self.assertRaises(RuntimeError):
                store.persist('bad', core.TargetRuntime(send_attempt_id='unsafe'))

    def test_older_delivery_row_cannot_overwrite_newer_confirmed_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = DeliveryStore(directory)
            old = core.TargetRuntime(send_attempt_id='old', delivery_status='sending')
            store.persist('s', old)
            new = core.TargetRuntime(send_attempt_id='new', delivery_status='confirmed', delivery_revision=3)
            runtime = {'s': new}
            store.restore(runtime, core.TargetRuntime)
            self.assertIs(runtime['s'], new)
            self.assertEqual(new.send_attempt_id, 'new')

    def test_operator_pause_during_individual_intent_prevents_real_send_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(grid_payload([], error=HIGH_DEMAND_TEXT), '■ ' + HIGH_DEMAND_TEXT)
            daemon = armed_daemon(directory, client)
            original = daemon._delivery_store.persist
            def pause_after_write(sid, runtime):
                original(sid, runtime)
                daemon.config_store.mutate(lambda cfg: cfg.update(global_paused=True))
            client.send = mock.Mock()
            with mock.patch.object(daemon._delivery_store, 'persist', side_effect=pause_after_write):
                daemon.process_once(client)
            client.send.assert_not_called()


class SendGenerationTests(unittest.TestCase):
    def test_unrelated_batch_progress_does_not_invalidate_send_identity(self):
        config = core.default_config()
        target = {'surface_id': 's', 'workspace_id': 'w', 'enabled': True}
        config['targets'] = [target]
        config['workspace_rules'] = [{'workspace_id': 'other', 'batch_start_holds': {}}]
        old = core.ObservationPolicy(config).key(target)
        changed = copy.deepcopy(config)
        changed['workspace_rules'][0]['batch_start_holds']['another'] = {'job_id': 'j'}
        self.assertEqual(core.ObservationPolicy(changed).key(target), old)
        changed['targets'][0]['paused'] = True
        self.assertNotEqual(core.ObservationPolicy(changed).key(target), old)

    def test_own_access_binding_change_invalidates_send_identity(self):
        config = core.default_config()
        target = {'surface_id': 's', 'workspace_id': 'w', 'source': 'workspace_rule'}
        config['workspace_rules'] = [{'workspace_id': 'w', 'access_check_slots': {'s': {'job_id': 'a', 'index': 0}}}]
        old = core.ObservationPolicy(config).key(target)
        config['workspace_rules'][0]['access_check_slots']['s']['job_id'] = 'b'
        self.assertNotEqual(core.ObservationPolicy(config).key(target), old)


class NativeSnapshotTests(unittest.TestCase):
    def event(self, kind, turn, **payload):
        return json.dumps({'type': 'event_msg', 'timestamp': datetime.now(timezone.utc).isoformat(),
                           'payload': {'type': kind, 'turn_id': turn, **payload}}) + '\n'

    def test_cache_never_survives_a_new_native_turn_or_replaced_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.jsonl'
            meta = json.dumps({'type': 'session_meta', 'payload': {'id': 'session'}}) + '\n'
            path.write_text(meta + self.event('task_complete', 'old', error={'message': 'failure'}))
            original = task_snapshot(path, 'session')
            original['error']['message'] = 'mutated caller'
            self.assertEqual(task_snapshot(path, 'session')['error']['message'], 'failure')
            with path.open('a') as handle:
                handle.write(self.event('task_started', 'new'))
            self.assertEqual(task_snapshot(path, 'session')['kind'], 'task_started')
            replacement = path.with_suffix('.tmp')
            replacement.write_text(meta + self.event('user_message', 'human'))
            os.replace(replacement, path)
            self.assertEqual(task_snapshot(path, 'session')['turn_id'], 'human')
            self.assertIsNone(task_snapshot(path, 'other-session'))

    def test_tail_expands_when_a_large_non_lifecycle_record_follows_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.jsonl'
            path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'session'}}) + '\n'
                + self.event('task_complete', 'turn', error={'message': 'failure'})
                + json.dumps({'type': 'other', 'data': 'a' * 70000}) + '\n')
            self.assertEqual(task_snapshot(path, 'session')['turn_id'], 'turn')


if __name__ == '__main__':
    unittest.main()

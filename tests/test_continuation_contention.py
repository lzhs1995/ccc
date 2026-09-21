"""Fleet locks must never put unrelated observations behind slow I/O."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import cmux_codex_watch as core
from ccc_scheduling import CoalescingWriter, SnapshotClient
from tests.test_registration_observation import main_tree
from tests.test_watch import FakeClient, armed_daemon, grid_payload, visible_lines


class ContentionTests(unittest.TestCase):
    def make_daemon(self, directory):
        frame = grid_payload([])
        client = FakeClient(frame, "\n".join(visible_lines(frame)))
        daemon = armed_daemon(directory, client)
        self.addCleanup(daemon._process_snapshots.close)
        return daemon, client

    def test_unchanged_config_does_not_wait_for_another_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self.make_daemon(directory)
            done = threading.Event()
            with ThreadPoolExecutor(1) as pool:
                def check():
                    daemon._reload_config_if_changed()
                    done.set()
                with daemon._config_reload_lock:
                    future = pool.submit(check)
                    self.assertTrue(done.wait(1), "unchanged config waited behind a reload lock")
                future.result(2)

    def test_changed_config_still_waits_for_reload_and_applies_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self.make_daemon(directory)
            entered, done = threading.Event(), threading.Event()
            with ThreadPoolExecutor(1) as pool:
                def check():
                    entered.set()
                    daemon._reload_config_if_changed()
                    done.set()
                with daemon._config_reload_lock:
                    daemon.config_store.mutate(lambda c: c["targets"][0].update(paused=True))
                    future = pool.submit(check)
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(done.wait(0.05))
                    self.assertFalse(daemon.config["targets"][0]["paused"])
                future.result(2)
            self.assertTrue(daemon.config["targets"][0]["paused"])

    def test_slow_health_record_copy_does_not_hold_the_fleet_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self.make_daemon(directory)
            target = daemon.config["targets"][0]
            runtime = daemon.runtime["surface-uuid"] = core.TargetRuntime()
            entered, release = threading.Event(), threading.Event()
            original = runtime.to_dict

            def slow_copy():
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test snapshot not released")
                return original()

            with mock.patch.object(runtime, "to_dict", side_effect=slow_copy), ThreadPoolExecutor(2) as pool:
                publishing = pool.submit(daemon._publish_observation_health)
                try:
                    self.assertTrue(entered.wait(1))
                    dispatched = pool.submit(daemon._record_dispatch, target, "observe", 0)
                    dispatched.result(1)
                finally:
                    release.set()
                publishing.result(2)
            self.assertGreater(runtime.observation_started_at, 0)

    def test_claude_exception_persistence_releases_the_fleet_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.make_daemon(directory)
            daemon.runtime["surface-uuid"] = core.TargetRuntime()
            writes = []

            def write():
                # Bound the old deadlock so a regression fails rather than
                # leaving the unittest process with a permanently stuck writer.
                acquired = daemon._runtime_lock.acquire(timeout=1)
                if not acquired:
                    raise RuntimeError("event exception held the writer's fleet lock")
                daemon._runtime_lock.release()
                daemon._save_now()
                writes.append(1)

            writer = daemon._state_writer = CoalescingWriter(write, delay=0)
            try:
                with mock.patch.object(daemon, "_handle_claude_event", side_effect=RuntimeError("bad event")), \
                     mock.patch.object(daemon, "_mark_claude_event"), \
                     self.assertLogs(core.APP_NAME, level="ERROR"):
                    daemon._handle_claude_event_safely(
                        {"surface_id": "surface-uuid", "event_id": "failed-event"}, client)
                self.assertEqual(writes, [1])
                self.assertIn("surface-uuid", json.loads(daemon.state_path.read_text()))
            finally:
                writer.close()
                daemon._state_writer = None

    def test_ordinary_codex_observation_uses_generation_without_disk_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self.make_daemon(directory)
            with mock.patch.object(daemon, "_active_send_target", side_effect=AssertionError("unneeded disk check")):
                daemon._scheduled_observe(daemon.config["targets"][0], lambda: True)
            self.assertEqual(daemon.runtime["surface-uuid"].observed_state, "idle")

    def test_cancelled_observation_cannot_publish_after_its_read_returns(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.make_daemon(directory)
            current = [True]
            original = client.read_screen

            def read(*args):
                current[0] = False
                return original(*args)

            client.read_screen = read
            daemon._scheduled_observe(daemon.config["targets"][0], lambda: current[0])
            self.assertFalse(daemon.runtime["surface-uuid"].observed_at)

    def test_slow_owner_inspection_does_not_block_dispatch_and_rechecks_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self.make_daemon(directory)
            owner = daemon.runtime["foreign"] = core.TargetRuntime(
                claude_session_id="same-session", claude_hook_health="healthy")
            entered, release = threading.Event(), threading.Event()
            def inspect(*args):
                entered.set()
                release.wait(3)
                return True
            with mock.patch.object(core, "_claude_session_owner_is_live", side_effect=inspect), \
                 ThreadPoolExecutor(2) as pool:
                lookup = pool.submit(daemon._foreign_claude_session_owner, "surface-uuid", "same-session")
                try:
                    self.assertTrue(entered.wait(1))
                    dispatch = pool.submit(daemon._record_dispatch, daemon.config["targets"][0], "observe", 0)
                    dispatch.result(1)
                    owner.claude_session_id = "different-session"
                finally:
                    release.set()
                self.assertEqual(lookup.result(2), "")

    def test_cold_process_inventory_finishes_in_maintenance_without_holding_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.make_daemon(directory)
            entered, release = threading.Event(), threading.Event()
            calls = []
            def top(workspace_id):
                calls.append(workspace_id)
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test inventory was not released")
                return {"windows": []}
            client.top = top
            client.tree_data = main_tree()
            client.terminal_diagnostics = lambda: {"terminals": []}
            with mock.patch.object(daemon, "_check_claude_hook_settings"), ThreadPoolExecutor(2) as pool:
                refresh = pool.submit(daemon._refresh_observation_health,
                                      SnapshotClient(client, daemon._process_snapshots))
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertFalse(refresh.done())
                    pool.submit(daemon._record_dispatch, daemon.config["targets"][0], "observe", 0).result(1)
                finally:
                    release.set()
                refresh.result(2)
            self.assertEqual(calls, ["workspace-uuid"])
            self.assertTrue(daemon._observation_metadata["inventory_complete"])

    def test_changed_owner_process_is_not_mistaken_for_proven_stale_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, _ = self.make_daemon(directory)
            owner = daemon.runtime["foreign"] = core.TargetRuntime(
                claude_session_id="same-session", claude_hook_health="healthy",
                claude_process_pid=10, claude_process_generation="old")
            def inspect(*args):
                owner.claude_process_pid = 20
                owner.claude_process_generation = "new"
                return False
            with mock.patch.object(core, "_claude_session_owner_is_live", side_effect=inspect):
                self.assertEqual(daemon._foreign_claude_session_owner("surface-uuid", "same-session"), "foreign")


if __name__ == "__main__":
    unittest.main()

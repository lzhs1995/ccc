"""Real watchdog guards, persistence and health around asynchronous sends."""
import copy
import contextlib
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import ccc_observation as health
import cmux_codex_watch as core
from tests.test_watch import FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload, process_fixture, span, visible_lines


def error_frame():
    return grid_payload([], error=HIGH_DEMAND_TEXT)


class TimeoutClient(FakeClient):
    def __init__(self, *, delivered=False):
        frame = error_frame()
        super().__init__(frame, "\n".join(visible_lines(frame)))
        self.delivered, self.attempts = delivered, 0

    def send(self, workspace_id, surface_id, message):
        self.attempts += 1
        if self.attempts == 1:
            if self.delivered:
                self.payload = grid_payload([], working=True)
                self.text = "\n".join(visible_lines(self.payload))
            try:
                raise subprocess.TimeoutExpired(["cmux", "send"], 8)
            except subprocess.TimeoutExpired as exc:
                raise core.CmuxError("send acknowledgement timed out") from exc
        return super().send(workspace_id, surface_id, message)


class DeliveryTests(unittest.TestCase):
    def test_failed_attempt_persistence_never_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(error_frame())
            daemon = armed_daemon(directory, client)
            target = daemon.config["targets"][0]
            runtime = core.TargetRuntime()
            daemon.runtime["surface-uuid"] = runtime
            state = core.classify_grid(core.Grid.from_rpc(error_frame(), "surface-uuid"))
            with mock.patch.object(daemon, "save", side_effect=OSError("disk unavailable")):
                daemon._handle_state(target, runtime, state, client, send_guard_tree=client.tree())
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.delivery_status, "failed")
            self.assertIn("input not sent", runtime.last_send_error)

    def test_pause_during_durable_write_blocks_the_actual_send(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(error_frame())
            daemon = armed_daemon(directory, client)
            target = daemon.config["targets"][0]
            runtime = core.TargetRuntime()
            daemon.runtime["surface-uuid"] = runtime
            state = core.classify_grid(core.Grid.from_rpc(error_frame(), "surface-uuid"))
            original_save = daemon.save
            def save_and_pause(**kwargs):
                original_save(**kwargs)
                daemon.config_store.mutate(lambda c: c["targets"][0].update(paused=True))
            with mock.patch.object(daemon, "save", side_effect=save_and_pause):
                daemon._handle_state(target, runtime, state, client, send_guard_tree=client.tree())
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.delivery_status, "cancelled")

    def test_late_read_failure_cannot_pause_a_new_registration_generation(self):
        for error in (core.IncompatibleError("old parser failure"), core.CmuxError("old missing surface"),
                      core.GlobalIncompatibleError("old capability failure")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                client = FakeClient(error_frame())
                daemon = armed_daemon(directory, client)
                target = dict(daemon.config["targets"][0])
                def stale_read(*args):
                    daemon.config_store.mutate(lambda c: c["targets"][0].update(paused=True))
                    daemon.config_store.mutate(lambda c: c["targets"][0].update(paused=False))
                    raise error
                client.read_screen = stale_read
                daemon._scheduled_observe(target, lambda: True)
                persisted = json.loads(daemon.config_path.read_text())
                self.assertFalse(persisted["targets"][0].get("paused"))
                self.assertEqual(persisted["mode"], "armed")
                daemon._process_snapshots.close()

    def test_timeout_after_delivery_is_confirmed_by_working_without_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TimeoutClient(delivered=True)
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            runtime = daemon.runtime["surface-uuid"]
            self.assertEqual(runtime.delivery_status, "unknown")
            self.assertEqual(runtime.send_count, 0)
            daemon.process_once(client)
            self.assertEqual(runtime.delivery_status, "confirmed")
            self.assertEqual(runtime.state, "working")
            self.assertEqual(client.attempts, 1)

    def test_unknown_receipt_survives_restart_and_ignores_echo_below_old_error(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TimeoutClient()
            daemon = armed_daemon(directory, client)
            with mock.patch.object(core.time, "time", return_value=1000):
                daemon.process_once(client)
            restarted = core.WatchDaemon(daemon.config_path, daemon.state_path, client=client)
            frame = error_frame()
            row = frame["render_grid"]["cursor"]["row"]
            frame["render_grid"]["row_spans"].append(span(row - 2, 0, "› 任务请继续"))
            client.payload, client.text = frame, "\n".join(visible_lines(frame))
            with mock.patch.object(core.time, "time", return_value=1005):
                restarted.process_once(client)
            self.assertEqual(client.attempts, 1)
            self.assertEqual(restarted.runtime["surface-uuid"].state, "delivery_unknown")

    def test_new_error_after_a_prompt_reopens_retry_after_unknown_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            client = TimeoutClient()
            daemon = armed_daemon(directory, client)
            with mock.patch.object(core.time, "time", return_value=1000):
                daemon.process_once(client)
            frame = grid_payload(["› 任务请继续", ""], error=HIGH_DEMAND_TEXT)
            client.payload, client.text = frame, "\n".join(visible_lines(frame))
            with mock.patch.object(core.time, "time", return_value=1001.1):
                daemon.process_once(client)
            self.assertEqual(client.attempts, 2)
            self.assertEqual(daemon.runtime["surface-uuid"].delivery_status, "accepted")
            self.assertEqual(daemon.runtime["surface-uuid"].last_send_at, 1001.1)

    def test_fresh_send_preflight_observes_working_and_user_input(self):
        for frame in (grid_payload([], working=True), grid_payload([], composer="busy")):
            with self.subTest(frame=frame["render_grid"]["cursor"]), tempfile.TemporaryDirectory() as directory:
                client = FakeClient(frame, "\n".join(visible_lines(frame)))
                daemon = armed_daemon(directory, client)
                target = daemon.config["targets"][0]
                daemon.runtime["surface-uuid"] = core.TargetRuntime()
                candidate = core.classify_grid(core.Grid.from_rpc(error_frame(), "surface-uuid"))
                daemon._scheduled_send(target, candidate, lambda: True)
                daemon._process_snapshots.close()
                self.assertEqual(client.sent, [])

    def test_pause_written_during_preflight_blocks_send_at_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            frame = error_frame()
            client = FakeClient(frame, "\n".join(visible_lines(frame)))
            daemon = armed_daemon(directory, client)
            target = daemon.config["targets"][0]
            daemon.runtime["surface-uuid"] = core.TargetRuntime()
            candidate = core.classify_grid(core.Grid.from_rpc(frame, "surface-uuid"))
            original_read = client.read_screen
            def pause_then_read(*args):
                daemon.config_store.mutate(lambda config: config["targets"][0].update(paused=True))
                return original_read(*args)
            client.read_screen = pause_then_read
            daemon._scheduled_send(target, candidate, lambda: True)
            daemon._process_snapshots.close()
            self.assertEqual(client.sent, [])
            self.assertTrue(daemon.config["targets"][0]["paused"])

    def test_removed_workspace_rule_invalidates_cached_dynamic_target(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(error_frame())
            daemon = armed_daemon(directory, client)
            target = {"surface_id": "dynamic", "workspace_id": "workspace-uuid", "source": "workspace_rule",
                      "source_workspace_id": "workspace-uuid", "enabled": True}
            daemon.dynamic_targets["dynamic"] = target
            self.assertIsNone(daemon._active_send_target(target))

    def test_pane_follow_cannot_bypass_a_workspace_surface_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, FakeClient(error_frame()))
            daemon.config["targets"][0]["pane_id"] = "pane-uuid"
            daemon.config["workspace_rules"] = [{"workspace_id": "workspace-uuid", "enabled": True,
                                                  "excluded_surface_ids": ["dynamic"]}]
            target = {"surface_id": "dynamic", "workspace_id": "workspace-uuid", "pane_id": "pane-uuid",
                      "source": "pane_follow", "enabled": True}
            daemon.dynamic_targets["dynamic"] = target
            self.assertIsNone(daemon._active_send_target(target))

    def test_persisted_resume_clears_a_failed_local_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, FakeClient(error_frame()))
            target = daemon.config["targets"][0]
            daemon._local_paused_surface_ids.add("surface-uuid")
            daemon.config_store.mutate(lambda c: c["targets"][0].update(paused=False))
            self.assertIsNotNone(daemon._active_send_target(target))
            self.assertNotIn("surface-uuid", daemon._local_paused_surface_ids)

    def test_production_scheduler_runs_with_injected_client_for_40_surfaces(self):
        items = [{"surface_id": str(i), "workspace_id": "workspace-uuid", "enabled": True} for i in range(40)]
        frame = error_frame()
        class Client(FakeClient):
            def __init__(self):
                super().__init__(frame, "\n".join(visible_lines(frame)),
                                 top=process_fixture(*[(str(i), "codex") for i in range(40)]))
                self.mutex = threading.Lock()
            def replay(self, workspace_id, surface_id):
                result = copy.deepcopy(frame)
                result["render_grid"]["surface_id"] = surface_id
                return result
            def send(self, *args):
                with self.mutex:
                    super().send(*args)
        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            daemon = armed_daemon(directory, client, extra_targets=items)
            scheduler = daemon._start_scheduler()
            scheduler.clock = lambda: 100
            try:
                deadline = time.monotonic() + 5
                while len(client.sent) < 40 and time.monotonic() < deadline:
                    scheduler.tick(items, generation=daemon._config_mtime_ns)
                    scheduler.wakeup.wait(0.005)
                    scheduler.wakeup.clear()
                self.assertEqual(len(client.sent), 40)
                self.assertEqual(len({row[1] for row in client.sent}), 40)
            finally:
                scheduler.close()
                daemon._process_snapshots.close()

    def test_wrapped_encrypted_content_400_is_visible_and_never_retried(self):
        frame = grid_payload(['■ {"error":{"message":"bad response status code 400",',
                              '"code":"invalid_encryp', 'ted_content"}}'])
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(frame, "\n".join(visible_lines(frame)))
            daemon = armed_daemon(directory, client)
            daemon.process_once(client)
            self.assertEqual(daemon.runtime["surface-uuid"].state, "provider_blocked")
            self.assertEqual(client.sent, [])


class ContinuationHealthTests(unittest.TestCase):
    def test_status_uses_published_inventory_without_cmux_or_process_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, FakeClient(error_frame()))
            output = io.StringIO()
            with mock.patch.object(core, "CmuxClient", side_effect=AssertionError("status performed cmux I/O")), \
                    mock.patch.object(core, "inspect_claude_process", side_effect=AssertionError("status ran ps")), \
                    mock.patch.object(core, "describe_daemon_runtime", return_value={}), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(core.cli(["--config", str(daemon.config_path), "status"]), 0)
            status = json.loads(output.getvalue())
            self.assertEqual(status["claude_hook_coverage"]["status"], "unknown")
            self.assertEqual(status["continuation_health"]["status"], "unknown")

    def test_incomplete_or_stale_inventory_cannot_publish_healthy_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, FakeClient(error_frame()))
            for complete, at in ((False, 100), (True, 10)):
                daemon._observation_metadata = {"inventory_complete": complete, "observed_at": at}
                with mock.patch.object(core.time, "time", return_value=101):
                    daemon._publish_observation_health()
                snapshot = json.loads(daemon.observation_health_path.read_text())
                self.assertEqual(snapshot["claude_hook_coverage"]["status"], "unknown")

    def test_supervisor_displays_delayed_observation_and_unconfirmed_delivery(self):
        import cmux_supervisor_tui as tui
        target = {"surface_id": "s", "workspace_id": "w"}
        for runtime, expected in (({"viewport_checked_at": 100}, "检测延迟"),
                                  ({"viewport_checked_at": 110, "delivery_status": "unknown"}, "投递待验")):
            with mock.patch.object(tui.time, "time", return_value=110):
                fields = tui.continuation_fields(target, runtime)
            candidate = tui.Candidate(record={**target, "ref": "surface:1"}, source="explicit",
                                      state="recoverable_error", error_type="high_demand",
                                      send_count=0, paused=False, agent_kind="codex", **fields)
            self.assertEqual(tui.watch_label(candidate), expected)

    def test_enabled_target_with_old_viewport_is_delayed(self):
        target = {"surface_id": "s", "workspace_id": "w", "enabled": True}
        row = health.continuation_row(target, {"viewport_checked_at": 100, "state": "recoverable_error"}, now=103)
        self.assertEqual(row["status"], "delayed")
        self.assertEqual(row["observation_age_sec"], 3)

    def test_readable_viewport_cannot_hide_send_failure_or_uncertain_receipt(self):
        target = {"surface_id": "s", "workspace_id": "w"}
        for delivery, expected in (("failed", "send_failed"), ("unknown", "delivery_unknown")):
            row = health.continuation_row(target, {"viewport_checked_at": 100, "delivery_status": delivery}, now=101)
            self.assertEqual(row["status"], expected)

    def test_status_recalculates_age_instead_of_reusing_cached_green_verdict(self):
        target = {"surface_id": "s", "workspace_id": "w"}
        config = {"targets": [target], "workspace_rules": []}
        snapshot = {"config_key": core.monitoring_config_key(config), "continuation_health": {"status": "ok"}}
        with mock.patch.object(core.time, "time", return_value=110):
            report = core.continuation_status(config, {"s": {"viewport_checked_at": 100}}, snapshot)
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["counts"]["delayed"], 1)


if __name__ == "__main__":
    unittest.main()

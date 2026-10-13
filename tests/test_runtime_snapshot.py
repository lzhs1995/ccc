"""Health publication must not copy historical runtimes or trim durable state."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import cmux_codex_watch as core


def snapshot_daemon():
    # Exercise publication methods without starting a daemon or native readers.
    daemon = core.WatchDaemon.__new__(core.WatchDaemon)
    daemon.runtime = {}
    daemon._runtime_lock = threading.Lock()
    daemon._liveness_guard = None
    return daemon


class RuntimeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.daemon = snapshot_daemon()
        self.daemon.runtime = {
            "current": core.TargetRuntime(
                observation_timings={"stages": {"samples": [1, 2]}}),
            "archived": core.TargetRuntime(
                delivery_status="unknown", delivery_revision=7,
                codex_input_phase="text_retained", send_attempt_id="original-attempt",
                codex_input_not_sent={"proof": {"intent": "original", "events": [1]}},
                claude_completed_latched=True),
        }

    def test_unfiltered_snapshot_and_save_preserve_all_history(self):
        daemon = self.daemon
        expected = {sid: value.to_dict() for sid, value in daemon.runtime.items()}
        self.assertEqual(daemon._runtime_snapshot(), expected)
        # A filtered health read must not change the later durable publication.
        self.assertEqual(set(daemon._runtime_snapshot(["current"])), {"current"})
        with tempfile.TemporaryDirectory() as directory:
            daemon.state_path = Path(directory) / "state.json"
            daemon._state_save_lock = threading.Lock()
            daemon._last_state_serialized = ""
            daemon._save_now()
            saved = json.loads(daemon.state_path.read_text())
        self.assertEqual(saved, expected)
        self.assertEqual(core.TargetRuntime.from_dict(saved["archived"]).to_dict(),
                         expected["archived"])

    def test_selected_records_are_copied_once_and_missing_ids_are_ignored(self):
        current = self.daemon.runtime["current"]
        archived = self.daemon.runtime["archived"]
        with mock.patch.object(current, "to_dict", wraps=current.to_dict) as copied, \
             mock.patch.object(archived, "to_dict", side_effect=AssertionError("history copied")):
            result = self.daemon._runtime_snapshot(iter(["current", "missing", "current"]))
        self.assertEqual(set(result), {"current"})
        copied.assert_called_once_with()
        self.assertEqual(set(self.daemon.runtime), {"current", "archived"})

    def test_empty_selection_copies_no_history(self):
        with mock.patch.object(core.TargetRuntime, "to_dict", side_effect=AssertionError("history copied")):
            self.assertEqual(self.daemon._runtime_snapshot([]), {})
            self.assertEqual(self.daemon._runtime_snapshot(iter(())), {})

    def test_selected_nested_values_do_not_alias_runtime(self):
        result = self.daemon._runtime_snapshot(["current"])
        copied = result["current"]["observation_timings"]["stages"]["samples"]
        original = self.daemon.runtime["current"].observation_timings["stages"]["samples"]
        copied.append(3)
        self.assertEqual(original, [1, 2])
        original.append(4)
        self.assertEqual(copied, [1, 2, 3])

    def test_membership_snapshot_survives_changes_while_copying_outside_lock(self):
        for selected in (None, ["first", "later", "new"]):
            with self.subTest(selected=selected):
                daemon = snapshot_daemon()
                first = core.TargetRuntime(state="first-original")
                daemon.runtime = {"first": first, "later": core.TargetRuntime(state="later-original")}
                entered, release = threading.Event(), threading.Event()
                original_copy = first.to_dict

                def slow_copy():
                    entered.set()
                    if not release.wait(5):
                        raise TimeoutError("test snapshot was not released")
                    return original_copy()

                with mock.patch.object(first, "to_dict", side_effect=slow_copy), \
                     ThreadPoolExecutor(1) as pool:
                    copying = pool.submit(daemon._runtime_snapshot, selected)
                    try:
                        self.assertTrue(entered.wait(2))
                        acquired = daemon._runtime_lock.acquire(timeout=1)
                        self.assertTrue(acquired, "record copying held the fleet lock")
                        try:
                            del daemon.runtime["later"]
                            daemon.runtime["new"] = core.TargetRuntime(state="new")
                            daemon.runtime["first"] = core.TargetRuntime(state="replacement")
                        finally:
                            daemon._runtime_lock.release()
                    finally:
                        release.set()
                    result = copying.result(2)
                self.assertEqual({sid: row["state"] for sid, row in result.items()},
                                 {"first": "first-original", "later": "later-original"})
                self.assertEqual(set(daemon.runtime), {"first", "new"})


class HealthSnapshotScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        daemon = self.daemon = snapshot_daemon()
        daemon.observation_health_path = Path(temporary.name) / "monitoring-health.json"
        daemon._targets_lock = threading.Lock()
        daemon._metadata_lock = threading.Lock()
        daemon._scheduler = None

        def target(sid, number, **changes):
            return {"surface_id": sid, "workspace_id": "workspace", "ref": f"surface:{number}",
                    "enabled": True, "paused": False, **changes}

        daemon.config = {
            "mode": "dry-run", "poll_interval_sec": 1,
            "targets": [target("active", 1), target("paused", 2, paused=True),
                        target("disabled", 3, enabled=False), target("unobserved", 4)],
            "workspace_rules": [{"workspace_id": "paused-workspace", "paused": True}],
            "discovery_excluded_surface_ids": ["excluded-live"],
        }
        daemon.dynamic_targets = {
            "dynamic": target("dynamic", 5),
            "dynamic-paused": target("dynamic-paused", 6, workspace_id="paused-workspace"),
        }
        targets = core.effective_targets(daemon.config, daemon.dynamic_targets.values())
        self.target_ids = {t["surface_id"] for t in targets}
        records = {t["surface_id"]: t for t in targets}
        records.update({"unregistered-live": target("unregistered-live", 100),
                        "excluded-live": target("excluded-live", 101)})
        labels, inspections = {}, {}
        for pid, sid in enumerate(records, 1):
            kind = "claude" if sid in {"paused", "disabled", "unregistered-live", "excluded-live"} else "codex"
            labels[sid] = {"agent_kind": kind, "agent_pid": pid,
                           "identity_verified_pids": [pid], "process_snapshot_present": True}
            inspections[pid] = {"pid": pid, "generation": f"birth-{pid}", "started_epoch": 1}
            if sid != "unobserved":
                daemon.runtime[sid] = core.TargetRuntime(
                    state="working", viewport_readable=True, viewport_checked_at=100,
                    observation_timings={"stage_ms": {"total": pid}},
                    claude_process_pid=pid, claude_process_generation=f"birth-{pid}",
                    claude_hook_health="healthy", claude_hook_provenance="hook_receipt",
                    claude_session_id=f"session-{pid}", claude_last_hook_at=99)
        daemon.runtime["archived"] = core.TargetRuntime(delivery_status="unknown")
        daemon._observation_metadata = {
            "records": records, "labels": labels, "observed_at": 100,
            "terminals": {sid: {"runtime_surface_ready": True, "ghostty_surface_ptr": "0x1"}
                          for sid in records},
            "owners": {sid: True for sid in records}, "hook_inspections": inspections,
            "inventory_complete": True, "config_key": core.monitoring_config_key(daemon.config),
        }

    def publish(self):
        with mock.patch.object(core.time, "time", return_value=100):
            self.daemon._publish_observation_health()
        return json.loads(self.daemon.observation_health_path.read_text())

    def test_health_copies_only_effective_targets_with_unchanged_report(self):
        full_state = self.daemon._runtime_snapshot()
        with ExitStack() as stack:
            copies = {sid: stack.enter_context(mock.patch.object(value, "to_dict", wraps=value.to_dict))
                      for sid, value in self.daemon.runtime.items()}
            scoped = self.publish()
        self.assertEqual({sid: copied.call_count for sid, copied in copies.items()},
                         {sid: int(sid in self.target_ids) for sid in self.daemon.runtime})
        with mock.patch.object(self.daemon, "_runtime_snapshot", return_value=full_state):
            full = self.publish()
        self.assertEqual(scoped, full)
        self.assertEqual({r["surface_id"] for r in scoped["monitored_targets"]}, self.target_ids)
        continuation = {r["surface_id"]: r for r in scoped["continuation_health"]["targets"]}
        self.assertEqual(set(continuation), self.target_ids)
        for sid in ("paused", "dynamic-paused"):
            self.assertEqual(continuation[sid]["status"], "paused")
        self.assertEqual(continuation["disabled"]["status"], "disabled")
        self.assertEqual(scoped["claude_hook_coverage"]["unregistered"], ["surface:100"])
        self.assertEqual(scoped["claude_hook_coverage"]["excluded_live"], ["surface:101"])

    def test_empty_target_fleet_keeps_unregistered_claude_without_copying_history(self):
        daemon = self.daemon
        daemon.config["targets"] = []
        daemon.dynamic_targets = {}
        daemon._observation_metadata["config_key"] = core.monitoring_config_key(daemon.config)
        with mock.patch.object(core.TargetRuntime, "to_dict", side_effect=AssertionError("history copied")):
            result = self.publish()
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["monitored_targets"], [])
        self.assertEqual(result["continuation_health"]["targets"], [])
        self.assertIn("surface:100", result["claude_hook_coverage"]["unregistered"])
        self.assertEqual(result["claude_hook_coverage"]["excluded_live"], ["surface:101"])

    def test_diagnostic_refresh_skips_history_and_keeps_same_health(self):
        daemon = self.daemon
        original = daemon._observation_metadata
        full_state = daemon._runtime_snapshot()
        client = mock.Mock()
        client.top_all.return_value = {}
        client.terminal_diagnostics.return_value = {
            "terminals": [dict(row, surface_id=sid)
                          for sid, row in original["terminals"].items()]}
        daemon._native_process_index = mock.Mock()
        daemon._native_process_index.snapshot.return_value = {}
        daemon._health_writer = None

        with mock.patch.object(daemon, "_check_claude_hook_settings"), \
             mock.patch.object(core, "main_surface_records", return_value=list(original["records"].values())), \
             mock.patch.object(core, "classify_surface_processes", side_effect=lambda _: dict(original["labels"])), \
             mock.patch.object(daemon, "_inspect_process_cached", side_effect=original["hook_inspections"].get), \
             mock.patch.object(daemon, "_observation_owner_alive", side_effect=lambda row: bool(row.get("claude_process_pid"))), \
             mock.patch.object(daemon, "_request_health_publish"), \
             mock.patch.object(core.time, "time", return_value=100):
            with ExitStack() as stack:
                copies = {sid: stack.enter_context(mock.patch.object(value, "to_dict", wraps=value.to_dict))
                          for sid, value in daemon.runtime.items()}
                daemon._refresh_observation_health(client)
            scoped_metadata = daemon._observation_metadata
            self.assertEqual({sid: copied.call_count for sid, copied in copies.items()},
                             {sid: int(sid in self.target_ids) for sid in daemon.runtime})
            scoped_report = self.publish()
            with mock.patch.object(daemon, "_runtime_snapshot", return_value=full_state):
                daemon._refresh_observation_health(client)
            self.assertEqual(daemon._observation_metadata, scoped_metadata)
            self.assertEqual(self.publish(), scoped_report)
        self.assertEqual(set(scoped_metadata["owners"]), self.target_ids)
        self.assertIn("paused", scoped_metadata["owners"])
        self.assertIn("disabled", scoped_metadata["owners"])


if __name__ == "__main__":
    unittest.main()

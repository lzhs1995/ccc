import copy
import json
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import ccc_observation as observation
import cmux_codex_watch as core
from tests.test_watch import (
    FakeClient, claude_armed_daemon, claude_grid_payload, claude_hook_event,
    process_fixture_with_pid, claude_idle_screen,
)


def main_tree():
    return {"windows": [{"id": "window", "workspaces": [{
        "id": "workspace-uuid", "ref": "workspace:1", "panes": [{
            "id": "pane", "ref": "pane:1", "surfaces": [{
                "id": "surface-uuid", "ref": "surface:1", "type": "terminal",
                "title": "Claude", "is_dock": False,
            }],
        }],
    }]}]}


def claude_processes():
    top = process_fixture_with_pid("surface-uuid", "claude", 1234)
    ws = top["windows"][0]["workspaces"][0]
    ws.update(kind="workspace", id="workspace-uuid")
    process = ws["surfaces"][0]["processes"][0]
    process.update(cmux_surface_id="surface-uuid", cmux_workspace_id="workspace-uuid")
    return top


class RegistrationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        self.client = FakeClient(claude_grid_payload(["● unfinished work"], completed=True),
                                 text=claude_idle_screen(), tree=main_tree(), top=claude_processes())
        self.daemon = claude_armed_daemon(self.directory, self.client, targets=[])
        self.daemon._claude_hook_config_health = {"healthy": True}
        self.target = dict(surface_id="surface-uuid", workspace_id="workspace-uuid", enabled=True, paused=False)
        self.now = time.time()
        self.start = claude_hook_event("original-start", "SessionStart", created_at=self.now - 60)
        self.stop = claude_hook_event("original-stop", created_at=self.now - 2)
        for event in (self.start, self.stop):
            event["agent_pid"] = 1234
        self.events = [self.start, self.stop]
        self.journal()
        self.daemon._handle_claude_event(self.stop, self.client)
        self.assertEqual(self.daemon.claude_event_ledger.status_of("original-stop"), "unmapped")
        self.daemon.config_store.mutate(lambda cfg: cfg["targets"].append(dict(self.target)))
        self.daemon._reload_config_if_changed()
        inspection = dict(pid=1234, started_at="2026-09-13T10:00:00", started_epoch=self.now - 120,
                          generation="verified-generation", legacy_override=False)
        self.patch = mock.patch.object(core, "inspect_claude_process", return_value=inspection)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def journal(self):
        self.daemon.claude_event_inbox.journal_path.write_text(
            "".join(json.dumps(event) + "\n" for event in self.events))

    def reconcile(self):
        return self.daemon._reconcile_registration("surface-uuid", self.client)

    def test_stop_before_registration_resumes_without_a_new_event(self):
        self.reconcile()
        self.assertEqual(len(self.client.sent_text), 1)
        row = self.daemon.claude_event_ledger.events["original-stop"]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["created_at"], self.stop["created_at"])
        self.assertEqual(row["original_rejection"]["status"], "unmapped")
        self.assertEqual(set(self.daemon.claude_event_ledger.events), {"original-stop"})

    def test_repeated_check_and_daemon_restart_do_not_retype(self):
        self.reconcile()
        self.reconcile()
        restarted = core.WatchDaemon(self.daemon.config_path, self.daemon.state_path, client=self.client)
        restarted._reconcile_registration("surface-uuid", self.client)
        self.assertEqual(len(self.client.sent_text), 1)

    def test_later_prompt_even_if_not_yet_handled_supersedes_stop(self):
        prompt = claude_hook_event("later-human", "UserPromptSubmit", created_at=self.now - 1)
        prompt["agent_pid"] = 1234
        self.events.append(prompt)
        self.journal()
        self.reconcile()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.daemon.claude_event_ledger.status_of("original-stop"), "unmapped")

    def test_prompt_arriving_during_preflight_cancels_submission(self):
        read = self.client.read_screen
        def new_prompt(*args):
            event = claude_hook_event("racing-human", "UserPromptSubmit")
            event["agent_pid"] = 1234
            self.events.append(event)
            self.journal()
            return read(*args)
        self.client.read_screen = new_prompt
        self.reconcile()
        self.assertEqual(self.client.sent, [])

    def test_interrupted_handling_can_resume_but_reserved_cannot(self):
        self.daemon.claude_event_ledger.claim_registration(self.stop)
        restarted = core.WatchDaemon(self.daemon.config_path, self.daemon.state_path, client=self.client)
        restarted._claude_hook_config_health = {"healthy": True}
        restarted._reconcile_registration("surface-uuid", self.client)
        self.assertEqual(len(self.client.sent_text), 1)
        self.daemon.claude_event_ledger.mark(self.stop, "reserved")
        self.reconcile()
        self.assertEqual(len(self.client.sent_text), 1)

    def test_unverified_process_and_missing_session_start_do_not_resume(self):
        process = self.client.top_data["windows"][0]["workspaces"][0]["surfaces"][0]["processes"][0]
        process.pop("cmux_surface_id")
        self.reconcile()
        self.assertEqual(self.client.sent, [])
        process["cmux_surface_id"] = "surface-uuid"
        self.events = [self.stop]
        self.journal()
        self.reconcile()
        self.assertEqual(self.client.sent, [])

    def test_pid_reuse_and_expired_stop_do_not_resume(self):
        with mock.patch.object(core, "inspect_claude_process", return_value={
            "pid": 1234, "started_at": "later", "started_epoch": self.now,
            "generation": "new-generation",
        }):
            self.reconcile()
        self.assertEqual(self.client.sent, [])
        self.stop["created_at"] = self.now - 3601
        self.journal()
        self.reconcile()
        self.assertEqual(self.client.sent, [])

    def test_completed_and_paused_events_are_never_reclaimed(self):
        self.stop["completed"] = True
        self.journal()
        self.reconcile()
        self.assertEqual(self.client.sent, [])
        self.stop["completed"] = False
        self.journal()
        self.daemon.claude_event_ledger.mark(self.stop, "ignored_paused")
        self.reconcile()
        self.assertEqual(self.client.sent, [])

    def test_registration_preserves_user_input_and_retry_countdown(self):
        for payload in (claude_grid_payload(composer="busy"),
                        claude_grid_payload(error="API Error: 503 Retrying in 5 seconds… (attempt 3/3)")):
            with self.subTest(payload=payload):
                self.client.payload = payload
                self.reconcile()
                self.assertEqual(self.client.sent, [])

    def test_completed_latch_is_not_reset_by_registration(self):
        runtime = self.daemon.runtime["surface-uuid"]
        runtime.claude_completed_latched = True
        runtime.claude_process_generation = "older-process"
        self.reconcile()
        self.assertTrue(runtime.claude_completed_latched)
        self.assertEqual(self.daemon.claude_event_ledger.status_of("original-stop"), "unmapped")
        self.assertEqual(self.client.sent, [])

    def test_transport_flag_cannot_reclaim_rejected_event(self):
        event = dict(self.stop, _registration_revalidation=True)
        self.daemon._handle_claude_event(event, self.client)
        self.assertEqual(self.daemon.claude_event_ledger.status_of("original-stop"), "unmapped")
        self.assertEqual(self.client.sent, [])

    def test_ledger_process_or_workspace_mismatch_blocks_reclaim(self):
        original = copy.deepcopy(self.daemon.claude_event_ledger.events["original-stop"])
        for field, value in (("workspace_id", "foreign"), ("agent_pid", 4321), ("created_at", self.now - 1)):
            with self.subTest(field=field):
                self.daemon.claude_event_ledger.events["original-stop"] = dict(original, **{field: value})
                self.reconcile()
                self.assertEqual(self.client.sent, [])
                self.assertEqual(self.daemon.claude_event_ledger.claim_registration(self.stop),
                                 core.CLAUDE_HISTORICAL_ID_COLLISION)

    def test_enrollment_waits_for_in_progress_unauthorized_rejection(self):
        entered, release, reconciling = threading.Event(), threading.Event(), threading.Event()

        def reject_original_event():
            with self.daemon._surface_lock("surface-uuid"):
                self.daemon.claude_event_ledger.mark(self.stop, "handling")
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test rejection did not release")
                self.daemon.claude_event_ledger.mark(self.stop, "unmapped", detail="surface is not authorized")

        def reconcile():
            reconciling.set()
            return self.reconcile()

        with ThreadPoolExecutor(max_workers=2) as pool:
            rejection = pool.submit(reject_original_event)
            self.assertTrue(entered.wait(5))
            recovery = pool.submit(reconcile)
            try:
                self.assertTrue(reconciling.wait(5))
                threading.Event().wait(0.05)
                self.assertFalse(recovery.done())
            finally:
                release.set()
            rejection.result(timeout=5)
            recovery.result(timeout=5)
        self.assertEqual(len(self.client.sent_text), 1)


class DiagnosticSchedulingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.client = FakeClient(claude_grid_payload(), tree=main_tree(), top=claude_processes())
        self.daemon = claude_armed_daemon(temporary.name, self.client)
        self.pool = mock.Mock()
        self.pool.submit.side_effect = lambda *args: Future()
        self.daemon._diagnostics_pool = self.pool

    def test_startup_checks_persisted_targets_independently_of_event_workers(self):
        self.assertIsNone(self.daemon._event_worker_pool)
        self.daemon._schedule_diagnostics()
        names = [call.args[0].__name__ for call in self.pool.submit.call_args_list]
        self.assertEqual(names, ["_refresh_observation_health", "_reconcile_registration"])

    def test_config_registration_resume_and_global_gates_schedule_rechecks(self):
        previous = copy.deepcopy(self.daemon.config)
        for change in ("new_target", "unpause", "mode", "global_paused", "claude_enabled"):
            with self.subTest(change=change):
                before, after = copy.deepcopy(previous), copy.deepcopy(previous)
                if change == "new_target":
                    before["targets"] = []
                elif change == "unpause":
                    before["targets"][0]["paused"] = True
                elif change == "mode":
                    before["mode"] = "dry-run"
                else:
                    before[change] = not after[change]
                self.daemon._registration_due.clear()
                self.daemon._queue_registration_checks(before, after)
                self.assertIn("surface-uuid", self.daemon._registration_due)
        after = copy.deepcopy(previous)
        after["targets"][0]["name"] = "only a title change"
        self.daemon._registration_due.clear()
        self.daemon._queue_registration_checks(previous, after)
        self.assertEqual(self.daemon._registration_due, {})

    def test_diagnostics_and_registration_share_a_bounded_four_job_budget(self):
        self.daemon._diagnostics_next_at = time.time() + 30
        self.daemon._registration_due = {f"surface-{i}": 0 for i in range(8)}
        self.daemon._schedule_diagnostics()
        self.assertEqual(self.pool.submit.call_count, 4)
        self.daemon._diagnostics_next_at = 0
        self.daemon._schedule_diagnostics()
        self.assertEqual(self.pool.submit.call_count, 4)
        self.assertIsNone(self.daemon._diagnostics_future)
        next(iter(self.daemon._registration_futures.values())).set_result(False)
        self.daemon._schedule_diagnostics()
        self.assertEqual(self.pool.submit.call_count, 5)
        self.assertEqual(self.pool.submit.call_args.args[0].__name__, "_refresh_observation_health")
        self.assertEqual(len(self.daemon._registration_futures), 3)

    def test_failed_registration_is_retried_without_queueing_another_same_surface(self):
        self.daemon._schedule_diagnostics()
        future = self.daemon._registration_futures["surface-uuid"]
        self.daemon._schedule_diagnostics()
        self.assertIs(self.daemon._registration_futures["surface-uuid"], future)
        future.set_exception(core.CmuxError("temporary I/O failure"))
        self.daemon._schedule_diagnostics()
        self.assertNotIn("surface-uuid", self.daemon._registration_futures)
        self.assertGreater(self.daemon._registration_due["surface-uuid"], time.time())


class ViewportFallbackTests(unittest.TestCase):
    def client(self, payload):
        client = core.CmuxClient("cmux")
        client._run = mock.Mock(side_effect=[
            core.CmuxError("internal_error: Failed to read terminal text"),
            subprocess.CompletedProcess([], 0, json.dumps(payload), ""),
        ])
        return client

    def test_seq_zero_with_grid_recovers_current_viewport(self):
        client = self.client({"seq": 0, "surface_id": "surface-uuid", "workspace_id": "workspace-uuid",
                              **claude_grid_payload(["current viewport"] )})
        self.assertIn("current viewport", client.read_screen("workspace-uuid", "surface-uuid"))
        self.assertEqual(client.last_viewport_source, "terminal.replay")
        for call in client._run.call_args_list:
            self.assertNotIn("--scrollback", call.args[0])
            self.assertNotIn("--lines", call.args[0])

    def test_seq_zero_without_grid_is_not_global_protocol_failure(self):
        payload = {"seq": 0, "surface_id": "surface-uuid", "workspace_id": "workspace-uuid"}
        with self.assertRaises(core.CmuxError):
            self.client(payload).read_screen("workspace-uuid", "surface-uuid")
        with self.assertRaises(core.IncompatibleError) as raised:
            core.Grid.from_rpc(payload, "surface-uuid")
        self.assertNotIsInstance(raised.exception, core.GlobalIncompatibleError)

    def test_foreign_replay_identity_is_rejected(self):
        for field in ("surface_id", "workspace_id"):
            payload = {"seq": 0, field: "foreign", **claude_grid_payload()}
            client = core.CmuxClient("cmux")
            client._run = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
            with self.assertRaises(core.IncompatibleError):
                client.replay("workspace-uuid", "surface-uuid")


class ObservationCoverageTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.target = dict(surface_id="surface-uuid", workspace_id="workspace-uuid")
        self.process = dict(agent_kind="claude", agent_pid=1234, identity_verified_pids=[1234],
                            process_snapshot_present=True)
        self.terminal = dict(runtime_surface_ready=True, ghostty_surface_ptr="0x1234", in_window=False)
        self.runtime = dict(viewport_readable=True, viewport_checked_at=self.now, viewport_source="read-screen")

    def row(self, owner=False):
        return observation.observation_row(self.target, self.target, self.process, self.terminal, self.runtime,
                                           owner_alive=owner, now=self.now, stale_after=30)

    def test_background_surface_with_valid_runtime_is_readable(self):
        self.assertEqual(self.row()["status"], "readable")

    def test_nil_pointer_does_not_become_truthy_readiness(self):
        self.terminal.update(runtime_surface_ready=False, ghostty_surface_ptr="nil")
        self.assertIs(observation.native_runtime_ready(self.terminal), False)
        self.assertEqual(self.row()["status"], "live_unreadable")
        self.process.update(agent_kind="unknown", agent_pid=0, identity_verified_pids=[])
        self.assertEqual(self.row()["status"], "dormant")
        self.assertEqual(self.row(owner=None)["status"], "unknown")

    def test_missing_diagnostics_cannot_prove_dormancy(self):
        self.terminal = {}
        self.runtime.update(viewport_readable=False)
        self.process.update(agent_pid=0, agent_kind="unknown")
        self.assertEqual(self.row()["status"], "unknown")

    def test_foreign_process_and_its_children_are_excluded(self):
        top = claude_processes()
        surface = top["windows"][0]["workspaces"][0]["surfaces"][0]
        surface["processes"][0]["cmux_surface_id"] = "another-surface"
        surface["processes"].append(dict(kind="process", name="claude", pid=1235, ppid=1234))
        result = core.classify_surface_processes(top)["surface-uuid"]
        self.assertEqual(result["agent_pids"], [])
        self.assertEqual(result["identity_conflicts"], 2)

    def test_foreign_workspace_is_excluded_even_with_matching_surface(self):
        top = claude_processes()
        top["windows"][0]["workspaces"][0]["surfaces"][0]["processes"][0]["cmux_workspace_id"] = "foreign"
        self.assertEqual(core.classify_surface_processes(top)["surface-uuid"]["agent_pids"], [])

    def test_unknown_and_stale_cannot_report_ok(self):
        row = self.row()
        report = observation.summarize_observation([row], now=self.now + 31, stale_after=30)
        self.assertEqual(report["status"], "unknown")
        self.assertEqual(report["counts"]["unknown"], 1)
        row.update(status="live_unreadable")
        unknown = dict(row, status="unknown")
        report = observation.summarize_observation([row, unknown], now=self.now, stale_after=30)
        self.assertEqual(report["status"], "degraded")

    def test_paused_is_explicit_and_does_not_hide_other_gaps(self):
        self.target["paused"] = True
        row = self.row()
        self.assertEqual(row["status"], "paused")
        report = observation.summarize_observation([row, dict(row, status="live_unreadable")],
                                                  now=self.now, stale_after=30)
        self.assertEqual(report["status"], "degraded")

    def test_new_config_invalidates_old_health_snapshot(self):
        config = dict(targets=[self.target])
        snapshot = dict(config_key=core.monitoring_config_key(config), rows=[self.row()])
        self.assertEqual(core.observation_status(config, {}, snapshot)["status"], "ok")
        config["targets"].append(dict(surface_id="new", workspace_id="workspace-uuid"))
        self.assertEqual(core.observation_status(config, {}, snapshot)["status"], "unknown")

    def test_partial_or_wrong_workspace_snapshot_cannot_report_healthy(self):
        config = dict(targets=[self.target])
        for rows in ([], [dict(self.row(), workspace_id="foreign")]):
            with self.subTest(rows=rows):
                report = core.observation_status(config, {}, dict(config_key=core.monitoring_config_key(config), rows=rows))
                self.assertEqual(report["status"], "unknown")
                self.assertEqual(report["counts"]["unknown"], 1)

    def test_readiness_uses_live_hook_identity_and_respects_all_enable_gates(self):
        config = dict(mode="armed", claude_enabled=True, global_paused=False)
        coverage = dict(targets=[self.row()])
        runtime = {"surface-uuid": dict(claude_hook_health="healthy", claude_process_pid=1234)}
        hooks = dict(targets=[dict(surface_id="surface-uuid", hook_verified=True)])
        self.assertEqual(core.registration_readiness(config, coverage, runtime)[0]["status"], "unknown")
        self.assertEqual(core.registration_readiness(config, coverage, runtime, hooks)[0]["status"], "ready")
        runtime["surface-uuid"]["claude_process_pid"] = 4321
        self.assertEqual(core.registration_readiness(config, coverage, runtime, hooks)[0]["status"], "unknown")
        runtime["surface-uuid"]["claude_process_pid"] = 1234
        for changed in (dict(mode="dry-run"), dict(global_paused=True), dict(claude_enabled=False)):
            with self.subTest(changed=changed):
                self.assertEqual(core.registration_readiness({**config, **changed}, coverage, runtime, hooks)[0]["status"],
                                 "blocked")
        for kind in ("shell", "unknown", "grok"):
            coverage["targets"][0]["agent_kind"] = kind
            self.assertEqual(core.registration_readiness(config, coverage, runtime, hooks)[0]["status"], "blocked")

    def test_background_diagnostic_publishes_current_coverage_without_input(self):
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(claude_grid_payload(), tree=main_tree(), top=claude_processes())
            client.top_all = lambda: client.top_data
            client.terminal_diagnostics = lambda: {"terminals": [dict(self.terminal, surface_id="surface-uuid")]}
            daemon = claude_armed_daemon(directory, client)
            daemon.runtime["surface-uuid"] = core.TargetRuntime(**self.runtime)
            daemon._refresh_observation_health(client)
            report = core.observation_status(daemon.config, {}, json.loads(daemon.observation_health_path.read_text()))
            self.assertEqual(report["counts"]["readable"], 1)
            self.assertEqual(report["status"], "ok")
            self.assertEqual(client.sent, [])


if __name__ == "__main__":
    unittest.main()

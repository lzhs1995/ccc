"""First-response proof, workspace isolation, actual cancellation and fencing."""
import asyncio
import copy
import json
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import ccc_batch_guard as guard
import cmux_codex_watch as core


def delta(thread="session", turn="turn", text="OK", method="item/agentMessage/delta"):
    return {"method": method, "params": {"threadId": thread, "turnId": turn, "itemId": "model-item", "delta": text}}


class EvidenceTests(unittest.TestCase):
    def test_only_fresh_typed_model_events_are_positive(self):
        for method in guard.MODEL_DELTAS:
            self.assertIsNotNone(guard.model_evidence(delta(method=method), "session", "turn"))
        for event in (delta(thread="old"), delta(turn="old"), delta(text=""), delta(text="  "),
                      delta(method="error"), delta(method="item/commandExecution/outputDelta"),
                      {"method": "turn/started", "params": {"threadId": "session", "turnId": "turn"}},
                      {"method": "response.created", "params": {"threadId": "session", "turnId": "turn"}},
                      {"result": {"thread": {"turns": [delta()]}}}):
            self.assertIsNone(guard.model_evidence(event, "session", "turn"), event)

    def test_raw_model_tools_are_positive_but_tool_results_and_user_quotes_are_not(self):
        p = {"threadId": "session", "turnId": "turn"}
        for kind in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
            event = {"method": "rawResponseItem/completed", "params": {**p, "item": {"type": kind, "id": "model-id"}}}
            self.assertIsNotNone(guard.model_evidence(event, "session", "turn"))
        for item in ({"type": "function_call_output", "id": "x", "output": "OK"},
                     {"type": "message", "role": "user", "content": [{"type": "text", "text": "connected"}]},
                     {"type": "message", "role": "assistant", "content": None}):
            self.assertIsNone(guard.model_evidence({"method": "rawResponseItem/completed", "params": {**p, "item": item}}, "session", "turn"))

    def test_user_shell_and_unsourced_tool_events_are_not_model_success(self):
        for source in ("userShell", None, "startupHook"):
            event = {"method": "item/started", "params": {"threadId": "session", "turnId": "turn",
                "item": {"type": "commandExecution", "id": "x", "command": "echo OK", "source": source}}}
            self.assertIsNone(guard.model_evidence(event, "session", "turn"))
        event["params"]["item"]["source"] = "agent"
        self.assertIsNotNone(guard.model_evidence(event, "session", "turn"))


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        self.wid, self.other = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
        config = core.default_config()
        config.update(mode="armed", global_paused=False, workspace_rules=[])
        for wid in (self.wid, self.other):
            jid = str(uuid.uuid4())
            config["workspace_rules"].append({"workspace_id": wid, "enabled": True,
                "batch_guard": {"version": 1, "origin_job_id": jid}})
            core.atomic_write_json(self.path.parent / "workspace-batches" / jid / "job.json",
                {"id": jid, "workspace_id": wid, "slots": [{"index": 0}]})
        core.atomic_write_json(self.path, config)
        self.locations = {}
        async def membership(wid, sid):
            self.service.inventory = self.locations.copy()
            self.service.inventory_at = time.monotonic()
            return self.locations.get(sid) == wid
        self.service = guard.GuardService(self.path, membership=membership)
        for wid in (self.wid, self.other):
            await self.service.dispatch({"command": "arm", "workspace_id": wid})
        self.pool = self.service.pools[self.wid]

    async def asyncTearDown(self):
        for pool in self.service.pools.values():
            for task in (pool.stop_task, pool.pause_task):
                if task:
                    await task
        if self.service.save_task:
            await self.service.save_task

    def endpoint(self, pool=None, *, ack=True, ignore_term=False):
        pool = pool or self.pool
        sid = str(uuid.uuid4()).upper()
        e = guard.Endpoint(pool, sid, {"cwd": self.temp.name})
        self.locations[sid] = pool.wid
        pool.endpoints[sid] = e
        e.session_id, e.turn_id, e.active, e.native_start = "session", "turn", True, 1
        e.writes = []
        def write(raw):
            message = json.loads(raw)
            e.writes.append(message)
            if ack and message.get("method") == "turn/interrupt":
                asyncio.get_running_loop().call_soon(e.native_message, {"method": "turn/completed", "params": {
                    "threadId": "session", "turn": {"id": "turn", "status": "interrupted"}}})
        def send(sig):
            if not ignore_term or sig == signal.SIGKILL:
                e.native.returncode = -sig
        e.native = SimpleNamespace(pid=2000 + len(self.locations), returncode=None,
            stdin=SimpleNamespace(write=write, is_closing=lambda: False), send_signal=send)
        return e

    async def settled(self):
        until = time.monotonic() + 2
        while self.pool.phase not in {"stopped", "failed"}:
            self.assertLess(time.monotonic(), until)
            await asyncio.sleep(.005)
        await asyncio.sleep(.01)

    async def test_60_surfaces_cancel_in_parallel_and_other_pool_is_unchanged(self):
        peers = [self.endpoint() for _ in range(60)]
        untouched = self.endpoint(self.service.pools[self.other])
        before = copy.deepcopy(core.ConfigStore(self.path).load()["workspace_rules"][1])
        peers[17].native_message(delta())
        await self.settled()
        self.assertEqual(self.pool.phase, "stopped")
        self.assertTrue(self.pool.trip["connected"] and self.pool.trip["within_deadline"])
        self.assertTrue(all(e.stop_proof == "native_interrupted" for e in peers))
        self.assertEqual(untouched.writes, [])
        self.assertTrue(untouched.active)
        self.assertEqual(core.ConfigStore(self.path).load()["workspace_rules"][1], before)
        self.assertFalse(guard.blocked(self.path, self.other))

    async def test_delivery_ack_is_not_completion_and_fallback_kills_only_bound_backend(self):
        stuck = self.endpoint(ack=False, ignore_term=True)
        untouched = self.endpoint(self.service.pools[self.other])
        with patch("ccc_codex_queue.process_placement_start", return_value=1):
            stuck.native_message(delta())
            await self.settled()
        self.assertEqual(stuck.signal_name, "SIGKILL")
        self.assertEqual(stuck.stop_proof, "backend_exited")
        self.assertTrue(self.pool.trip["within_deadline"])
        self.assertIsNone(untouched.native.returncode)
        self.assertGreaterEqual(self.pool.trip["elapsed_ms"], 650)

    async def test_pid_reuse_refuses_force_signal_and_reports_failure(self):
        stuck = self.endpoint(ack=False)
        with patch("ccc_codex_queue.process_placement_start", return_value=2):
            stuck.native_message(delta())
            await self.settled()
        self.assertEqual(self.pool.phase, "failed")
        self.assertIsNone(stuck.signal_name)
        self.assertFalse(self.pool.trip["within_deadline"])

    async def test_surface_moved_out_is_neither_a_trigger_nor_a_stop_target(self):
        moved, live = self.endpoint(), self.endpoint()
        self.locations[moved.sid] = self.other
        moved.native_message(delta())
        await asyncio.sleep(.02)
        self.assertEqual(self.pool.phase, "watching")
        live.native_message(delta())
        await self.settled()
        self.assertEqual(moved.writes, [])
        self.assertTrue(moved.active)
        self.assertFalse(moved.in_scope)

    async def test_latch_rejects_late_turns_and_rearm_ignores_replayed_history(self):
        e = self.endpoint()
        e.front = SimpleNamespace(write=lambda x: None)
        e.native_message(delta())
        await self.settled()
        before = len(e.writes)
        e.front_message({"id": 22, "method": "turn/start", "params": {"threadId": "session"}})
        self.assertEqual(len(e.writes), before)
        core.ConfigStore(self.path).mutate(lambda c: c["workspace_rules"][0].update(paused=False))
        await self.service.dispatch({"command": "arm", "workspace_id": self.wid, "resume": True})
        e.native_message({"id": 123, "result": {"thread": {"id": "session", "turns": [delta()]}}})
        e.native_message(delta())  # no new live turn after rearm
        await asyncio.sleep(.02)
        self.assertEqual(self.pool.phase, "watching")

    async def test_first_model_event_fences_input_before_membership_await(self):
        source, peer = self.endpoint(), self.endpoint()
        source.native_message(delta())
        try:
            peer.front_message({"id": 22, "method": "turn/start", "params": {"threadId": "session"}})
            self.assertEqual(peer.writes, [])
            self.assertFalse(self.pool.open_gate())
        finally:
            await source.evidence_task
        await self.settled()
        self.assertTrue(self.pool.trip["connected"])

    async def test_moved_evidence_releases_input_fence_without_stopping_pool(self):
        moved = self.endpoint()
        self.locations[moved.sid] = self.other
        moved.native_message(delta())
        try:
            self.assertFalse(self.pool.open_gate())
        finally:
            await moved.evidence_task
        self.assertTrue(self.pool.open_gate())
        self.assertIsNone(self.pool.trip)
        self.assertEqual(moved.writes, [])

    async def test_moved_evidence_cannot_release_another_pending_success(self):
        moved, live = self.endpoint(), self.endpoint()
        self.locations[moved.sid] = self.other
        gates = {e.sid: asyncio.Event() for e in (moved, live)}
        original = self.service.membership
        async def membership(wid, sid):
            await gates[sid].wait()
            return await original(wid, sid)
        self.service.membership = membership
        moved.native_message(delta())
        live.native_message(delta())
        try:
            gates[moved.sid].set()
            await moved.evidence_task
            self.assertFalse(self.pool.open_gate())
            self.assertIsNone(self.pool.trip)
        finally:
            gates[live.sid].set()
            await live.evidence_task
        await self.settled()
        self.assertTrue(self.pool.trip["connected"])
        self.assertEqual(moved.writes, [])

    async def test_already_inflight_turn_does_not_replace_pending_success_with_fault(self):
        source, peer = self.endpoint(), self.endpoint()
        ready = asyncio.Event()
        original = self.service.membership
        async def membership(wid, sid):
            if sid == source.sid:
                await ready.wait()
            return await original(wid, sid)
        self.service.membership = membership
        source.native_message(delta())
        peer.active, peer.awaiting_turn = False, True
        peer.native_message({"method": "turn/started", "params": {
            "threadId": "session", "turn": {"id": "turn"}}})
        try:
            await asyncio.sleep(0)
            self.assertIsNone(self.pool.trip)
        finally:
            ready.set()
            await source.evidence_task
        await self.settled()
        self.assertEqual(self.pool.trip["reason"], "first_model_response")

    async def test_moved_surface_keeps_normal_title_and_session_requests(self):
        e = self.endpoint()
        e.expected_session = "session"
        self.locations[e.sid] = self.other
        await self.service.audit()
        e.front_message({"id": 91, "method": "thread/start", "params": {"threadSource": "thread_title"}})
        e.native_message({"method": "thread/started", "params": {"thread": {"id": "title-thread"}}})
        e.front_message({"id": 92, "method": "thread/resume", "params": {"threadId": "another-session"}})
        e.native_message({"id": 92, "result": {"thread": {"id": "another-session"}}})
        self.assertEqual([m["id"] for m in e.writes], [91, 92])
        self.assertEqual(self.pool.phase, "watching")

    async def test_global_dock_is_not_owned_by_the_workspace_displaying_it(self):
        from ccc_guard_scope import records
        dock, live = self.endpoint(), self.endpoint()
        window = str(uuid.uuid4()).upper()
        pane = str(uuid.uuid4()).upper()
        tree = {"windows": [{"id": window, "workspaces": [{"id": self.wid, "panes": [
            {"id": pane, "dock_scope": "global", "surfaces": [{"id": dock.sid, "type": "terminal"}]},
            {"id": pane, "surfaces": [{"id": live.sid, "type": "terminal"}]}]}]}]}
        locations = records(tree)
        self.assertEqual(locations[dock.sid]["workspace_id"], window)
        self.assertEqual(locations[live.sid]["workspace_id"], self.wid)
        self.locations.update({sid: row["workspace_id"] for sid, row in locations.items()})
        dock.native_message(delta())
        await asyncio.sleep(.02)
        self.assertEqual(self.pool.phase, "watching")
        live.native_message(delta())
        await self.settled()
        self.assertTrue(dock.active)
        self.assertEqual(dock.writes, [])
        self.assertFalse(dock.in_scope)
        # Some cmux tree views repeat the same global Dock beneath another
        # workspace. That is still one window-owned surface, not ambiguity.
        tree["windows"][0]["workspaces"].append({"id": self.other, "panes": [
            {"id": pane, "surfaces": [{"id": dock.sid, "type": "terminal", "dock_scope": "global"}]}]})
        self.assertEqual(records(tree)[dock.sid]["workspace_id"], window)

    async def test_title_or_fake_origin_is_not_authorization(self):
        wid = str(uuid.uuid4()).upper()
        core.ConfigStore(self.path).mutate(lambda c: c["workspace_rules"].append({
            "workspace_id": wid, "name": "B 新开50+授权 Anyrouter", "enabled": True,
            "batch_guard": {"version": 1, "origin_job_id": str(uuid.uuid4())}}))
        with self.assertRaisesRegex(RuntimeError, "provenance"):
            await self.service.dispatch({"command": "arm", "workspace_id": wid})

    async def test_malformed_batch_slots_cannot_confer_provenance(self):
        rule = core.ConfigStore(self.path).load()["workspace_rules"][0]
        jid = rule["batch_guard"]["origin_job_id"]
        path = self.path.parent / "workspace-batches" / jid / "job.json"
        job = guard.read_json(path)
        for slots in ([{}], [None], [{"index": 1}], [{"index": True}],
                      [{"index": 0}, {"index": 0}], {"index": 0}, "slots"):
            with self.subTest(slots=slots):
                guard.write_json(path, {**job, "slots": slots})
                self.assertIsNone(guard.provenance(self.path, self.wid))

    async def test_monitor_fault_stops_pool_but_is_not_connection_success(self):
        self.endpoint()
        self.pool.trigger("native_event_failure")
        await self.settled()
        self.assertEqual(self.pool.phase, "stopped")
        self.assertFalse(self.pool.trip["connected"])

    async def test_exited_fork_in_process_snapshot_is_not_a_coverage_failure(self):
        e = self.endpoint()
        stale = {"pid": 41001, "birth": [1, 2], "surface_id": e.sid,
                 "environment_workspace_id": self.wid, "process_start": 1}
        self.service.process_scan = lambda: [stale]
        with patch("ccc_guard_scope.birth", return_value=None):
            await self.service.audit()
        self.assertEqual(self.pool.phase, "watching")
        self.assertIsNone(self.pool.coverage_error)
        self.assertEqual(self.pool.unmanaged, {})
        self.assertTrue(e.active)

    async def test_first_success_includes_unregistered_codex_in_same_pool(self):
        source = self.endpoint()
        sid = str(uuid.uuid4()).upper()
        other_sid = str(uuid.uuid4()).upper()
        self.locations.update({sid: self.wid, other_sid: self.other})
        stray = {"pid": 41001, "birth": [1, 2], "surface_id": sid,
                 "environment_workspace_id": self.wid, "process_start": 1}
        outside = {**stray, "pid": 41002, "surface_id": other_sid, "environment_workspace_id": self.other}
        live = {41001, 41002}
        self.service.process_scan = lambda: [r for r in (stray, outside) if r["pid"] in live]
        signals = []
        def send(record, sig):
            signals.append((record["pid"], sig))
            if sig == signal.SIGTERM:
                live.remove(record["pid"])
            return True
        with patch("ccc_guard_scope.birth", side_effect=lambda pid, **kw: [1, 2] if pid in live else None), \
             patch("ccc_guard_scope.send", side_effect=send):
            source.native_message(delta())
            await self.settled()
        self.assertNotIn(41001, live)
        self.assertIn(41002, live)
        self.assertEqual(self.pool.phase, "stopped")
        self.assertTrue(self.pool.trip["connected"])
        self.assertTrue(all(pid == 41001 for pid, sig in signals))

    async def test_inventory_failure_cannot_report_all_stopped(self):
        self.endpoint()
        with patch.object(self.service, "audit", side_effect=RuntimeError("membership offline")):
            self.pool.trigger("coverage_unavailable")
            await self.settled()
        self.assertEqual(self.pool.phase, "failed")
        self.assertFalse(self.pool.trip["within_deadline"])
        self.assertFalse(self.pool.trip["connected"])

    async def test_malformed_native_result_is_a_fault_not_success(self):
        e = self.endpoint()
        with self.assertRaises(ValueError):
            e.native_message({"id": 123, "result": ["OK"]})
        self.assertEqual(self.pool.phase, "watching")

    async def test_native_tui_null_notification_opt_out_preserves_live_observation(self):
        e = self.endpoint()
        e.front_message({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "codex_tui", "version": "test"},
            "capabilities": {"optOutNotificationMethods": None}}})
        self.assertEqual(e.writes[-1]["params"]["capabilities"]["optOutNotificationMethods"], [])
        self.assertTrue(e.writes[-1]["params"]["capabilities"]["experimentalApi"])

    async def test_optional_native_title_thread_cannot_spend_a_second_model_request(self):
        e = self.endpoint()
        replies = []
        e.front = SimpleNamespace(write=lambda message: replies.append(json.loads(message)))
        e.front_message({"id": 2, "method": "thread/start", "params": {"threadSource": "thread_title"}})
        self.assertEqual(e.writes, [])
        self.assertIn("error", replies[0])
        self.assertEqual(e.session_id, "session")


class LauncherMigrationTests(unittest.TestCase):
    def test_new_arm_creates_private_parent_before_the_workspace_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            wid = str(uuid.uuid4()).upper()
            with patch.object(guard, "_arm", return_value={"phase": "watching"}):
                guard.arm(path, wid)
            self.assertEqual(guard.guard_root(path).stat().st_mode & 0o777, 0o700)
            self.assertEqual(guard.pool_dir(path, wid).stat().st_mode & 0o777, 0o700)

    def test_replaced_rollout_is_not_accepted_as_the_live_original(self):
        import ccc_guard_migration as migration
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "rollout-original.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": str(uuid.uuid4())}}))
            identity = {"device": path.stat().st_dev, "inode": path.stat().st_ino + 1}
            with patch.object(migration, "process_writable_files", return_value={path: identity}):
                with self.assertRaisesRegex(RuntimeError, "unlinked or replaced"):
                    migration.original_session({"pid": 1234}, Path(temp) / "config.json")

    def test_old_supervisor_batch_is_protected_but_paused_pool_stays_closed(self):
        import ccc_codex_launcher as launcher
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            wid, jid = str(uuid.uuid4()).upper(), str(uuid.uuid4())
            config = core.default_config()
            config["workspace_rules"] = [{"workspace_id": wid, "last_batch_id": jid, "enabled": True}]
            core.atomic_write_json(path, config)
            core.atomic_write_json(path.parent / "workspace-batches" / jid / "job.json",
                {"id": jid, "workspace_id": wid, "slots": [{"index": 0}]})
            with patch.dict("os.environ", {"CMUX_WORKSPACE_ID": wid}), \
                 patch.object(launcher, "current_workspace", return_value=wid), \
                 patch.object(guard, "_arm") as arm, patch.object(guard, "launch") as launch:
                launcher.run(path, "/native/codex", [])
                self.assertTrue(guard.provenance(path, wid))
                arm.assert_called_once_with(path, wid)
                launch.assert_called_once()
                guard.write_json(guard.pool_dir(path, wid) / "STOP.json", {"reason": "first_model_response"})
                with self.assertRaisesRegex(RuntimeError, "已停止"):
                    launcher.run(path, "/native/codex", [])
                self.assertEqual(launch.call_count, 1)

    def test_moved_shell_inherited_b_workspace_does_not_restrict_ordinary_destination(self):
        import ccc_codex_launcher as launcher
        wid, other, sid = (str(uuid.uuid4()).upper() for _ in range(3))
        with patch.dict("os.environ", {"CMUX_WORKSPACE_ID": wid, "CMUX_SURFACE_ID": sid}), \
             patch.object(guard, "provenance", side_effect=[{"enabled": True}, None]), \
             patch.object(launcher, "current_workspace", return_value=other), \
             patch.object(guard, "_arm") as arm, patch("os.execv", side_effect=SystemExit) as execute:
            with self.assertRaises(SystemExit):
                launcher.run(Path("config.json"), "/native/codex", ["--search"])
            arm.assert_not_called()
            execute.assert_called_once_with("/native/codex", ["/native/codex", "--search"])

    def test_darwin_environment_ends_before_apple_startup_metadata(self):
        from ccc_guard_scope import parse_arguments
        from tests.test_codex_process_placement import args_payload
        data = args_payload([b"/opt/bin/codex", b"app-server"], [
            b"CMUX_SURFACE_ID=surface", b"CMUX_WORKSPACE_ID=workspace", b"ptr_munge=original"])
        argv, env = parse_arguments(data + b"\0\0ptr_munge=kernel-metadata\0CMUX_SURFACE_ID=foreign\0")
        self.assertEqual(argv, ["/opt/bin/codex", "app-server"])
        self.assertEqual(env["CMUX_SURFACE_ID"], "surface")
        self.assertEqual(env["ptr_munge"], "original")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            parse_arguments(data + b"CMUX_SURFACE_ID=foreign\0")

    def test_migration_preflight_failure_closes_gate_without_stopping_originals(self):
        import ccc_guard_migration as migration
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            wid = str(uuid.uuid4()).upper()
            config = core.default_config()
            config["workspace_rules"] = [{"workspace_id": wid, "enabled": True}]
            core.atomic_write_json(path, config)
            with patch.object(guard, "provenance", return_value={"enabled": True}), \
                 patch.object(migration, "_adopt_workspace", side_effect=RuntimeError("draft unavailable")), \
                 patch.object(guard, "pause") as stop:
                with self.assertRaisesRegex(migration.PreflightPreservationError, "draft unavailable"):
                    migration.adopt_workspace(path, wid)
                self.assertTrue(guard.blocked(path, wid))
                stop.assert_not_called()

    def test_discovery_failures_never_start_guard_or_stop_uncaptured_sessions(self):
        import ccc_guard_migration as migration
        for component in ("tree", "scan"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "config.json"
                wid = str(uuid.uuid4()).upper()
                config = core.default_config()
                config["workspace_rules"] = [{"workspace_id": wid, "enabled": True}]
                core.atomic_write_json(path, config)
                client = SimpleNamespace(tree=lambda: {})
                owner = client if component == "tree" else migration.scope
                with patch.object(guard, "provenance", return_value={"enabled": True}), \
                     patch.object(migration, "cmux_client", return_value=client), \
                     patch.object(owner, component, side_effect=RuntimeError("discovery unavailable")), \
                     patch.object(guard, "request", side_effect=ConnectionRefusedError), \
                     patch.object(guard, "ensure_service") as start, patch.object(guard, "pause") as stop:
                    with self.assertRaisesRegex(migration.PreflightPreservationError, "discovery unavailable"):
                        guard._arm(path, wid)
                    self.assertTrue(guard.blocked(path, wid))
                    start.assert_not_called()
                    stop.assert_not_called()

    def test_failed_fence_write_keeps_preservation_failure_out_of_stop_fallback(self):
        import ccc_guard_migration as migration
        for component in ("marker", "config"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "config.json"
                wid = str(uuid.uuid4()).upper()
                config = core.default_config()
                config["workspace_rules"] = [{"workspace_id": wid, "enabled": True}]
                core.atomic_write_json(path, config)
                owner, name = (guard, "write_json") if component == "marker" else (core.ConfigStore, "mutate")
                with patch.object(guard, "provenance", return_value={"enabled": True}), \
                     patch.object(migration, "_adopt_workspace", side_effect=migration.PreflightPreservationError("unlinked history")), \
                     patch.object(owner, name, side_effect=OSError("read-only fence")), \
                     patch.object(guard, "pause") as stop:
                    with self.assertRaisesRegex(migration.PreflightPreservationError, "read-only fence"):
                        guard._arm(path, wid)
                    stop.assert_not_called()

    def test_setup_failure_after_complete_capture_still_uses_scoped_stop(self):
        import ccc_guard_migration as migration
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            wid, sid = (str(uuid.uuid4()).upper() for _ in range(2))
            config = core.default_config()
            config["workspace_rules"] = [{"workspace_id": wid, "enabled": True}]
            core.atomic_write_json(path, config)
            record = {"pid": 1234, "birth": [1, 2], "surface_id": sid}
            client = SimpleNamespace(tree=lambda: {})
            with patch.object(guard, "provenance", return_value={"enabled": True}), \
                 patch.object(migration.scope, "records", return_value={sid: {"workspace_id": wid, "surface_id": sid}}), \
                 patch.object(migration.scope, "scan", return_value=[record]), \
                 patch.object(migration, "capture", return_value={"resume_session": "original"}), \
                 patch.object(guard, "request", side_effect=ConnectionRefusedError), \
                 patch.object(guard, "ensure_service", side_effect=RuntimeError("guard offline")), \
                 patch.object(guard, "pause") as stop:
                with self.assertRaisesRegex(RuntimeError, "guard offline"):
                    migration.adopt_workspace(path, wid, client=client)
                stop.assert_called_once_with(path, wid, reason="protection_setup_failed")
                records = list(guard.pool_dir(path, wid).glob("migrations/*/" + sid + ".json"))
                self.assertEqual(len(records), 1)
                self.assertEqual(guard.read_json(records[0])["resume_session"], "original")

    def test_unrecoverable_history_preflight_fences_b_without_killing_original(self):
        import ccc_guard_migration as migration
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            wid = str(uuid.uuid4()).upper()
            config = core.default_config()
            config["workspace_rules"] = [{"workspace_id": wid, "enabled": True}]
            core.atomic_write_json(path, config)
            with patch.object(guard, "provenance", return_value={"enabled": True}), \
                 patch.object(migration, "_adopt_workspace", side_effect=migration.PreflightPreservationError("unlinked history")), \
                 patch.object(guard, "request") as request, patch.object(guard, "pause") as stop:
                with self.assertRaisesRegex(migration.PreflightPreservationError, "unlinked history"):
                    guard._arm(path, wid)
                self.assertTrue(guard.blocked(path, wid))
                self.assertTrue(core.workspace_rule_by_id(core.ConfigStore(path).load(), wid)["paused"])
                request.assert_not_called()
                stop.assert_not_called()

    def test_legacy_capture_failure_precedes_guard_start_or_any_stop(self):
        import ccc_guard_migration as migration
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            wid, sid = (str(uuid.uuid4()).upper() for _ in range(2))
            config = core.default_config()
            config["workspace_rules"] = [{"workspace_id": wid, "enabled": True}]
            core.atomic_write_json(path, config)
            record = {"pid": 1234, "birth": [1, 2], "surface_id": sid}
            target = {"workspace_id": wid, "surface_id": sid}
            client = SimpleNamespace(tree=lambda: {})
            with patch.object(guard, "provenance", return_value={"enabled": True}), \
                 patch.object(migration, "cmux_client", return_value=client), \
                 patch.object(migration.scope, "records", return_value={sid: target}), \
                 patch.object(migration.scope, "scan", return_value=[record]), \
                 patch.object(migration, "capture", side_effect=RuntimeError("unlinked history")), \
                 patch.object(guard, "request", side_effect=ConnectionRefusedError) as request, \
                 patch.object(guard, "ensure_service") as start, patch.object(guard, "pause") as stop:
                with self.assertRaisesRegex(migration.PreflightPreservationError, "unlinked history"):
                    guard._arm(path, wid)
                request.assert_called_once_with(path, "status", timeout=.3, workspace_id=wid)
                start.assert_not_called()
                stop.assert_not_called()
                self.assertTrue(guard.blocked(path, wid))

    def test_pause_cannot_hide_unmanaged_targets_or_coverage_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            wid = str(uuid.uuid4()).upper()
            store = core.ConfigStore(path)
            store.mutate(lambda c: c["workspace_rules"].append({"workspace_id": wid}))
            status = {"phase": "failed", "surfaces": {}, "coverage_error": "inventory offline",
                      "stop_targets": [{"surface_id": "legacy", "pid": 123, "active": True,
                                        "interrupt_requested": True, "in_scope": True}]}
            with patch.object(guard, "provenance", return_value={"enabled": True}), \
                 patch.object(guard, "pause", return_value=status):
                result = core.pause_workspace(store, wid, None)
                self.assertEqual(result["interrupt_requested"], ["legacy"])
                self.assertEqual(len(result["failed"]), 2)
                status["stop_targets"] = []
                result = core.pause_workspace(store, wid, None)
                self.assertTrue(result["failed"])

    def test_original_settings_survive_but_original_prompt_is_never_replayed(self):
        from ccc_guard_migration import launch_options
        config, ui = launch_options(["codex", "-c", 'model_provider="anyrouter"', "-m", "gpt-6-astra",
            "--no-alt-screen", "resume", str(uuid.uuid4()), "already submitted prompt"])
        self.assertIn('model_provider="anyrouter"', config)
        self.assertIn('model="gpt-6-astra"', config)
        self.assertEqual(ui, ["--no-alt-screen"])
        self.assertNotIn("already submitted prompt", config + ui)

    def test_unknown_native_options_do_not_silently_change_a_session(self):
        from ccc_guard_migration import launch_options
        with self.assertRaises(RuntimeError):
            launch_options(["codex", "--unknown-setting", "example"])

    def test_ordinary_workspace_executes_original_binary_and_arguments_unchanged(self):
        from ccc_codex_launcher import run
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            core.atomic_write_json(config, core.default_config())
            with patch.dict("os.environ", {"CMUX_WORKSPACE_ID": str(uuid.uuid4()).upper()}), \
                 patch("os.execv", side_effect=SystemExit) as execute:
                with self.assertRaises(SystemExit):
                    run(config, "/Applications/codex", ["resume", "original", "--search"])
            execute.assert_called_once_with("/Applications/codex", ["/Applications/codex", "resume", "original", "--search"])

    def test_launcher_install_keeps_original_native_executable(self):
        from ccc_codex_launcher import install
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = root / "native-codex"
            binary.write_text("original executable")
            entry = root / "codex"
            entry.symlink_to(binary)
            result = install(root / "config.json", destination=entry)
            self.assertEqual(binary.read_text(), "original executable")
            self.assertEqual(result["native_binary"], str(binary.resolve()))
            self.assertEqual(entry.resolve(), (root / "codex-guard").resolve())


class WatchdogTests(unittest.TestCase):
    def test_watchdog_discovers_unregistered_native_and_cannot_hide_inventory_failure(self):
        import ccc_guard_watchdog as watchdog
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            wid, sid = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
            owned = {"generation": "g", "workspaces": [wid], "backends": []}
            record = {"pid": 3456, "birth": [1, 2], "surface_id": sid, "environment_workspace_id": wid}
            live = {3456}
            def send(row, sig):
                live.discard(row["pid"])
                return True
            with patch.object(guard, "provenance", return_value={"enabled": True}), \
                 patch("ccc_guard_scope.scan", side_effect=lambda: [record] if live else []), \
                 patch("ccc_guard_scope.birth", side_effect=lambda p, **kw: [1, 2] if p in live else None), \
                 patch("ccc_guard_scope.send", side_effect=send):
                watchdog.emergency(path, owned, membership=lambda _: {sid: wid})
                state = guard.snapshot(path, wid)
                self.assertEqual(state["phase"], "stopped")
                self.assertEqual(state["trip"]["target_count"], 1)
                self.assertFalse(live)
            with patch.object(guard, "provenance", return_value={"enabled": True}), \
                 patch("ccc_guard_scope.scan", side_effect=RuntimeError("incomplete inventory")):
                watchdog.emergency(path, owned, membership=lambda _: {sid: wid})
                self.assertEqual(guard.snapshot(path, wid)["phase"], "failed")

    def test_watchdog_uses_one_membership_batch_and_excludes_moved_surfaces(self):
        import ccc_guard_watchdog as watchdog
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            wid, other = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
            sid, moved = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
            owned = {"generation": "generation", "backends": [
                {"workspace_id": wid, "surface_id": s, "environment_workspace_id": wid,
                 "pid": p, "birth": [1, p]} for p, s in ((2001, sid), (2002, moved))]}
            live, signals, lookups = {2001, 2002}, [], []
            def membership(_):
                lookups.append(True)
                return {sid: wid, moved: other}
            def send(record, sig):
                signals.append(record["pid"])
                live.remove(record["pid"])
                return True
            with patch("ccc_batch_guard.provenance", return_value={"enabled": True}), \
                 patch("ccc_guard_scope.scan", return_value=[]), \
                 patch("ccc_guard_scope.birth", side_effect=lambda pid, **kw: [1, pid] if pid in live else None), \
                 patch("ccc_guard_scope.send", side_effect=send):
                result = watchdog.emergency(path, owned, membership=membership)
            self.assertEqual(signals, [2001])
            self.assertEqual(len(lookups), 2)
            self.assertFalse(result["connected"])
            self.assertIn(2002, live)


if __name__ == "__main__":
    unittest.main()

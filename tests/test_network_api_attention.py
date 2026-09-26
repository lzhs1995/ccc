import copy
import http.server
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_network_guard as network
from ccc_mihomo import ProbeResult, ResponsesProbe, route
import test_network_guard as base
from test_network_resilience import RoutingController, MANUAL_GROUP


class ApiRetryTests(unittest.TestCase):
    def setUp(self):
        self.config, self.routes = base.fixtures()
        self.ids = [r.id for r in self.routes]
        self.clock = [1000]
        self.engine = network.Engine(self.config, self.routes, now=1000, monotonic=lambda: self.clock[0])

    def light(self, now):
        self.clock[0] = now
        for rid in self.ids:
            self.engine.record(rid, ProbeResult("accessible"), now)

    def failed(self, rid, now, kind="upstream", status=500):
        self.light(now)
        self.engine.reserve_deep(rid, now)
        self.engine.settle_deep(now + 1)
        self.clock[0] = now + 1
        self.engine.record(rid, ProbeResult(kind, status=status, deep=True), now + 1, started_at=now)

    def common_failure(self):
        for rid, now in zip(self.ids, (1000, 1031, 1062)):
            self.failed(rid, now)

    def test_common_upstream_failure_defers_deep_but_not_light_checks(self):
        self.common_failure()
        self.light(1093)
        self.assertIsNone(self.engine.deep_due(1093, set()))
        self.assertTrue(self.engine.light_due(1098, set()))
        self.light(1123)
        self.assertIsNotNone(self.engine.deep_due(1123, set()))
        self.assertTrue(all(not h.quarantined for h in self.engine.health.values()))

    def test_one_path_or_two_failures_do_not_claim_a_common_outage(self):
        for rid_list in ([self.ids[0]] * 4, self.ids[:2]):
            engine = network.Engine(self.config, self.routes, now=1000)
            for index, rid in enumerate(rid_list):
                engine.record(rid, ProbeResult("upstream", 500, deep=True), 1000 + index * 31)
            now = 1000 + len(rid_list) * 31
            for rid in self.ids:
                engine.record(rid, ProbeResult("accessible"), now)
            self.assertIsNotNone(engine.deep_due(now, set()))

    def test_backoff_is_bounded_and_does_not_reset_or_refund_paid_history(self):
        self.common_failure()
        self.assertEqual(self.engine.api.snapshot(1063)["backoff"]["remaining_sec"], 60)
        for index, (now, delay) in enumerate(((1123, 120), (1244, 240), (1485, 300), (1786, 300))):
            self.failed(self.ids[index % len(self.ids)], now)
            self.assertEqual(self.engine.api.snapshot(now + 1)["backoff"]["remaining_sec"], delay)
        self.assertEqual(self.engine.deep_starts, [1000, 1031, 1062, 1123, 1244, 1485, 1786])
        self.assertFalse(any(h.qualified for h in self.engine.health.values()))

    def test_light_success_and_error_hints_cannot_bypass_backoff(self):
        self.common_failure()
        self.engine.hint_at, self.engine.hint_route = 1100, self.ids[0]
        self.light(1100)
        self.assertIsNone(self.engine.deep_due(1100, set()))
        self.assertEqual(self.engine.api.snapshot(1100)["backoff"]["consecutive_failures"], 3)

    def test_retry_recovery_still_requires_full_sse_and_quarantine_gates(self):
        rid = self.ids[0]
        self.engine.record(rid, ProbeResult("blocked"), 900)
        self.common_failure()
        self.light(1123)
        self.engine.record(rid, ProbeResult("healthy", deep=True), 1124, started_at=1123)
        self.assertTrue(self.engine.health[rid].qualified)
        self.assertFalse(self.engine.health[rid].quarantined)
        self.assertEqual(self.engine.api.remaining(1124), 0)
        self.engine.record(rid, ProbeResult("blocked"), 1125)
        self.engine.record(rid, ProbeResult("healthy", deep=True), 1126, started_at=1124)
        self.assertTrue(self.engine.health[rid].quarantined)
        self.assertFalse(self.engine.health[rid].qualified)

    def test_transport_and_local_observer_failures_are_not_shared_api_outages(self):
        for kind in ("timeout", "transport", "truncated", "blocked", "observer_error"):
            state = network.ApiProbeState(1000)
            for i in range(4):
                state.record(self.ids[i], ProbeResult(kind, deep=True), 1000 + i * 31)
            self.assertEqual(state.remaining(1124), 0, kind)
            self.assertEqual(state.snapshot(1124)["outcomes"], {kind: 4})

    def test_changed_error_class_or_old_evidence_restarts_consecutive_count(self):
        self.common_failure()
        state = self.engine.api
        state.record(self.ids[0], ProbeResult("rate_limit", 429, deep=True), 1200)
        self.assertEqual(state.streak, 1)
        self.assertEqual(state.remaining(1200), 0)
        state.record(self.ids[1], ProbeResult("rate_limit", 429, deep=True), 1901)
        self.assertEqual(state.streak, 1)

    def test_backoff_survives_restart_without_refunding_unfinished_reservation(self):
        self.common_failure()
        self.engine.reserve_deep(self.ids[3], 1123)
        saved = copy.deepcopy(self.engine.saved())
        clock = [10]
        restored = network.Engine(self.config, self.routes, saved, now=5000, monotonic=lambda: clock[0])
        restored.record(self.ids[4], ProbeResult("accessible"), 5030)
        clock[0] = 40
        self.assertIsNone(restored.deep_due(5030, set()))
        self.assertEqual(restored.deep_recovery["reservation"], saved["deep_budget"]["pending"])
        clock[0] = 70
        restored.record(self.ids[4], ProbeResult("accessible"), 5060)
        self.assertIsNotNone(restored.deep_due(5060, set()))

    def test_thirty_minutes_of_upstream_failure_retains_bounded_recovery_checks(self):
        for limit in (1, 2):
            self.engine = network.Engine(self.config, self.routes, now=1000, monotonic=lambda: self.clock[0])
            self.engine.policy["deep_per_minute"] = limit
            starts = []
            for now in range(1000, 2801):
                self.light(now)
                rid = self.engine.deep_due(now, set())
                if rid:
                    starts.append(now)
                    self.engine.reserve_deep(rid, now)
                    self.clock[0] = now + .1
                    self.engine.settle_deep(now + .1)
                    self.engine.record(rid, ProbeResult("upstream", 500, deep=True), now + .1)
            self.assertGreaterEqual(len(starts), 7)
            self.assertLessEqual(len(starts), 11, starts)
            self.assertLessEqual(max(b - a for a, b in zip(starts, starts[1:])), 302)
            self.assertGreaterEqual(min(b - a for a, b in zip(starts, starts[1:])), 60 / limit)
            self.assertLessEqual(max(sum(0 <= end - start < 60 for start in starts) for end in starts), limit)

    def test_clock_adjustment_does_not_shorten_or_unbound_live_api_backoff(self):
        self.common_failure()
        self.clock[0] = 1093
        self.assertEqual(self.engine.api.remaining(999999), 30)
        self.assertEqual(self.engine.api.remaining(900), 30)
        self.clock[0] = 1123
        self.assertEqual(self.engine.api.remaining(900), 0)

    def test_invalid_api_state_delays_only_a_bounded_recovery(self):
        clock = [0]
        for invalid in ({"version": 1, "delay": "bad"}, {"version": 1, "kind": []}, ["bad"], {}):
            state = network.ApiProbeState(1000, invalid, monotonic=lambda: clock[0])
            self.assertEqual(state.remaining(1000), 300)
            self.assertEqual(state.snapshot(1000)["backoff"]["recovery"], "invalid_api_state")
            clock[0] += 300
            self.assertEqual(state.remaining(1300), 0)

    def test_malformed_optional_history_cannot_terminate_guard_recovery(self):
        self.common_failure()
        saved = self.engine.api.saved()
        saved["history"].extend([{"at": 1063, "kind": {}, "status": 500}, {"at": 1063, "kind": "upstream", "status": []}])
        restored = network.ApiProbeState(1064, saved)
        self.assertEqual(restored.snapshot(1064)["completed"], 3)
        self.assertEqual(restored.remaining(1064), 60)

    def test_previously_qualified_current_cannot_starve_other_api_checks(self):
        self.engine.current = self.ids[0]
        self.engine.record(self.ids[0], ProbeResult("accessible"), 1000)
        self.engine.record(self.ids[0], ProbeResult("healthy", deep=True), 1001)
        self.failed(self.ids[0], 1200)
        self.light(1231)
        self.assertNotEqual(self.engine.deep_due(1231, set()), self.ids[0])
        self.assertEqual(self.engine.decision(1231), (self.ids[0], "api_attention"))

    def test_common_failure_cannot_change_manual_selection_or_quarantine(self):
        self.config.update(outer_group="Transit-Auto-Select")
        control = RoutingController(self.config, self.routes)
        control.nodes[self.config["outer_group"]]["now"] = MANUAL_GROUP
        publisher = base.Publisher()
        director = network.Director(self.config, self.engine, control, publisher)
        manual = copy.deepcopy(control.nodes[MANUAL_GROUP])
        self.common_failure()
        for now in (1063, 1093, 1123):
            self.assertEqual(director.sync(now)[0], "manual")
        self.assertEqual(control.nodes[MANUAL_GROUP], manual)
        self.assertFalse(any(h.quarantined for h in self.engine.health.values()))


class CredentialSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ccc-probe-binding-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "auth.json"
        self.path.write_text(json.dumps({"OPENAI_API_KEY": "fixture-before"}))
        self.config = {"url": "http://127.0.0.1/v1/responses", "model": "fixture-model",
                       "auth_file": str(self.path), "timeout_sec": 2, "deep_timeout_sec": 2}
        self.item = route("fixture", {"name": "local", "type": "http", "server": "127.0.0.1", "port": 1})

    def server(self):
        calls = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append((self.headers.get("Authorization"), self.rfile.read(int(self.headers["Content-Length"]))))
                body = b'data: {"type":"response.completed","response":{"id":"fixture-response","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"OK"}]}]}}\n\n'
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port, calls

    def test_queued_probe_keeps_account_and_model_used_to_bind_its_contract(self):
        port, calls = self.server()
        probe = ResponsesProbe(self.config, {self.item.id: port})
        self.path.write_text(json.dumps({"OPENAI_API_KEY": "fixture-after"}))
        self.config["model"] = "other-model"
        self.assertEqual(probe.run(self.item, True).kind, "healthy")
        self.assertEqual(calls[0][0], "Bearer fixture-before")
        self.assertEqual(json.loads(calls[0][1])["model"], "fixture-model")

    def test_guard_binding_and_probe_use_one_file_snapshot(self):
        key, contract, legacy = network.probe_binding({"probe": self.config})
        self.path.write_text(json.dumps({"OPENAI_API_KEY": "fixture-after"}))
        self.assertNotEqual(contract, network.contract_digest({"probe": self.config}))
        port, calls = self.server()
        probe = ResponsesProbe(self.config, {self.item.id: port}, credential=key)
        self.assertEqual(probe.run(self.item, True).kind, "healthy")
        self.assertEqual(calls[0][0], "Bearer fixture-before")
        self.assertTrue(legacy)

    def test_unreadable_or_malformed_file_is_local_and_sends_no_request(self):
        port, calls = self.server()
        for raw in ("", "not-json", "null", '{"OPENAI_API_KEY":null}'):
            self.path.write_text(raw)
            result = ResponsesProbe(self.config, {self.item.id: port}).run(self.item, True)
            self.assertEqual(result.kind, "observer_error")
            self.assertEqual(result.stage, "credentials")
        self.assertEqual(calls, [])

    def test_environment_snapshot_does_not_follow_later_process_environment(self):
        self.config.pop("auth_file")
        self.config["auth_env"] = "CCC_FIXTURE_PROBE_KEY"
        port, calls = self.server()
        with mock.patch.dict("os.environ", {"CCC_FIXTURE_PROBE_KEY": "fixture-env-before"}):
            probe = ResponsesProbe(self.config, {self.item.id: port})
        with mock.patch.dict("os.environ", {"CCC_FIXTURE_PROBE_KEY": "fixture-env-after"}):
            probe.run(self.item, True)
        self.assertEqual(calls[0][0], "Bearer fixture-env-before")


class CredentialGuardTests(unittest.TestCase):
    setUp = base.GuardLoopTests.setUp
    patch = base.GuardLoopTests.patch
    stepped_loop = base.GuardLoopTests.stepped_loop
    save_engine = base.GuardLoopTests.save_engine

    def credential_file(self):
        path = self.root / "auth.json"
        path.write_text(json.dumps({"OPENAI_API_KEY": "fixture-before"}))
        self.config["probe"] = {"auth_file": str(path), "model": "fixture-model", "url": "https://anyrouter.test/v1/responses"}
        return path

    def test_temporary_unreadable_credentials_preserve_qualification_and_isolation(self):
        path = self.credential_file()
        snapshots = []
        def setup(clock):
            engine = network.Engine(self.config, self.routes, now=1000)
            for item in self.routes:
                engine.record(item.id, ProbeResult("accessible"), 999)
                engine.record(item.id, ProbeResult("healthy", deep=True), 999.1)
            engine.record(self.routes[1].id, ProbeResult("blocked"), 999.2)
            self.save_engine(engine)
        def tick(clock):
            if clock[0] == 1002:
                path.write_text("{")
            if clock[0] == 1004:
                snapshots.append(copy.deepcopy(self.guard.engine.saved()))
                path.write_text(json.dumps({"OPENAI_API_KEY": "fixture-before"}))
        self.stepped_loop([1000, 1002, 1003, 1004, 1005], setup=setup, on_tick=tick)
        self.assertTrue(snapshots[0]["health"][self.routes[0].id]["qualified"])
        self.assertTrue(snapshots[0]["health"][self.routes[1].id]["quarantined"])
        unavailable = [s for s in self.snapshots if s["at"] in (1002, 1003)]
        self.assertTrue(unavailable)
        self.assertTrue(all(next(r for r in s["routes"] if r["id"] == self.routes[0].id)["qualified"]
                            for s in unavailable))
        self.assertTrue(all(s["automatic"]["state"] == "credential_unavailable" for s in unavailable))
        self.assertFalse(self.guard.credential_error)

    def test_actual_credential_change_discards_old_worker_and_keeps_spent_budget(self):
        path = self.credential_file()
        old_contract = network.contract_digest(self.config)
        def setup(clock):
            engine = network.Engine(self.config, self.routes, now=1000)
            for item in self.routes:
                engine.record(item.id, ProbeResult("accessible"), 999)
            engine.record(self.routes[1].id, ProbeResult("blocked"), 999.1)
            self.save_engine(engine)
        def tick(clock):
            if clock[0] == 1002:
                path.write_text(json.dumps({"OPENAI_API_KEY": "fixture-after"}))
        self.stepped_loop([1000, 1002], setup=setup, on_tick=tick)
        self.assertNotEqual(self.guard.contract, old_contract)
        self.assertGreater(self.guard.discarded_probes, 0)
        self.assertEqual(self.guard.engine.deep_starts, [1000])
        self.assertEqual(self.guard.engine.deep_settled_at, 1002)
        self.assertFalse(any(h.qualified for h in self.guard.engine.health.values()))
        self.assertTrue(self.guard.engine.health[self.routes[1].id].quarantined)
        events = [json.loads(line) for line in (self.root / "events.ndjson").read_text().splitlines()]
        self.assertTrue(any(e["event"] == "probe_contract_changed" for e in events))
        self.assertTrue(all(e["contract"] == old_contract for e in events if e["event"] == "probe"))
        raw = (self.root / "events.ndjson").read_text() + (self.root / "health.json").read_text()
        self.assertNotIn("fixture-before", raw)
        self.assertNotIn("fixture-after", raw)

    def test_status_separates_current_manual_route_from_automatic_api_attention(self):
        self.credential_file()
        self.config["outer_group"] = "Transit-Auto-Select"
        self.controller = RoutingController(self.config, self.routes)
        self.controller.nodes[self.config["outer_group"]]["now"] = MANUAL_GROUP
        self.patch("Controller", return_value=self.controller)
        def setup(clock):
            engine = network.Engine(self.config, self.routes, now=1000)
            for i, item in enumerate(self.routes[:3]):
                engine.record(item.id, ProbeResult("upstream", 500, deep=True), 997 + i)
            self.save_engine(engine)
        self.stepped_loop([1000], setup=setup)
        status = self.snapshots[-1]
        self.assertEqual(status["phase"], "manual")
        self.assertEqual(status["automatic"]["state"], "api_backoff")
        self.assertEqual(status["automatic"]["api"]["outcomes"], {"upstream": 3})
        self.assertEqual(status["automatic"]["ready"], 0)
        self.assertEqual(status["automatic"]["probe"]["model"], "fixture-model")
        self.assertNotIn("fixture-before", json.dumps(status))
        self.assertIn("Clash", status["automatic"]["latency_note"])


if __name__ == "__main__":
    unittest.main()

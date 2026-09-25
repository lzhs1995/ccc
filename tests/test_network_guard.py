import copy
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_mihomo import ProbeResult, route
from ccc_network_guard import Director, Engine, Guard, ProviderServer, singleton, contract_digest


def fixtures():
    config = {"commercial_pools": ["NTHU", "Yeye"], "mode": "manage", "group": "AnyRouter",
              "provider": "verified", "offline_proxy": "Offline", "policy": {}, "service_host": "anyrouter.test"}
    routes = [route(pool, {"name": name, "type": "http", "server": "127.0.0.1", "port": 9000 + index}, priority=priority)
              for index, (pool, name, priority) in enumerate((
                  ("NTHU", "N1", 0), ("NTHU", "N2", 1), ("Yeye", "Y1", 0), ("Yeye", "Y2", 1),
                  ("Tokyo", "us11", 0), ("Tokyo", "us178", 1)))]
    return config, routes


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.config, self.routes = fixtures()
        self.e = Engine(self.config, self.routes)
        self.ids = [r.id for r in self.routes]
        self.e.current = self.ids[0]

    def good(self, rid, now=1000):
        self.e.record(rid, ProbeResult("accessible"), now)
        self.e.record(rid, ProbeResult("healthy", deep=True), now + .1)

    def all_good(self):
        for rid in self.ids:
            self.good(rid)

    def bad(self, rid, now=1001):
        self.e.record(rid, ProbeResult("timeout"), now)
        self.e.record(rid, ProbeResult("timeout"), now + .1)

    def test_light_http_success_is_not_admission(self):
        rid = self.ids[1]
        for _ in range(5):
            self.e.record(rid, ProbeResult("accessible"), 1000)
        self.assertFalse(self.e.ready(rid, 1001))
        self.good(rid)
        self.assertTrue(self.e.ready(rid, 1001))

    def test_deadline_tuning_preserves_api_identity_but_model_changes_do_not(self):
        config = {"probe": {"url": "https://anyrouter.test/v1/responses", "model": "test", "timeout_sec": 5}}
        before = contract_digest(config)
        config["probe"].update(timeout_sec=6, light_timeout_by_pool={"Tokyo": 8})
        self.assertEqual(before, contract_digest(config))
        config["probe"]["model"] = "other"
        self.assertNotEqual(before, contract_digest(config))

    def test_current_commercial_pool_is_sticky(self):
        self.all_good()
        self.e.health[self.ids[2]].elapsed_ms = .1
        self.assertEqual(self.e.decision(1001), (self.ids[0], "healthy"))
        self.e.current, self.e.active_pool = self.ids[2], "Yeye"
        self.assertEqual(self.e.decision(1001), (self.ids[2], "healthy"))

    def test_single_timeout_rechecks_without_flapping(self):
        self.all_good()
        self.e.record(self.ids[0], ProbeResult("timeout"), 1001)
        self.assertEqual(self.e.decision(1002), (self.ids[0], "suspect"))
        self.e.record(self.ids[0], ProbeResult("accessible"), 1003)
        self.assertEqual(self.e.decision(1003), (self.ids[0], "healthy"))

    def test_failure_uses_same_airport_then_other_airport_then_tokyo(self):
        self.all_good()
        self.bad(self.ids[0])
        self.assertEqual(self.e.decision(1002)[0], self.ids[1])
        self.bad(self.ids[1])
        self.assertEqual(self.e.decision(1002)[0], self.ids[2])
        self.bad(self.ids[2])
        self.bad(self.ids[3])
        self.assertEqual(self.e.decision(1002), (self.ids[4], "fallback"))
        self.bad(self.ids[4])
        self.assertEqual(self.e.decision(1002), (self.ids[5], "fallback"))
        self.bad(self.ids[5])
        self.assertEqual(self.e.decision(1002), ("", "network_wait"))

    def test_tokyo_us11_primary_and_commercial_recovery_preempt_fallback(self):
        self.all_good()
        self.e.current, self.e.active_pool = self.ids[5], "Tokyo"
        self.assertEqual(self.e.decision(1001)[0], self.ids[0])
        for rid in self.ids[:4]:
            self.bad(rid)
        self.assertEqual(self.e.decision(1002), (self.ids[4], "fallback"))

    def test_waf_isolation_needs_cooldown_three_lights_and_a_new_complete_sse(self):
        rid = self.ids[0]
        self.good(rid)
        self.e.record(rid, ProbeResult("blocked", status=200), 1001)
        self.e.record(rid, ProbeResult("healthy", deep=True), 1002)
        for stamp in (1003, 1004, 1005, 1062, 1063):
            self.e.record(rid, ProbeResult("accessible"), stamp)
        self.assertTrue(self.e.health[rid].quarantined)
        self.assertIsNone(self.e.deep_due(1063, set()))
        self.e.record(rid, ProbeResult("accessible"), 1064)
        self.assertEqual(self.e.deep_due(1064, set()), rid)
        self.assertFalse(self.e.ready(rid, 1064))
        self.e.record(rid, ProbeResult("healthy", deep=True), 1065)
        self.assertTrue(self.e.ready(rid, 1065))

    def test_account_model_or_global_overload_does_not_quarantine_or_rotate(self):
        self.all_good()
        for kind in ("auth", "rate_limit", "permission", "upstream", "contract"):
            self.e.record(self.ids[0], ProbeResult(kind, deep=True), 1001)
            self.assertFalse(self.e.health[self.ids[0]].quarantined)
            self.assertEqual(self.e.decision(1002), (self.ids[0], "api_attention"))

    def test_billable_probe_budget_survives_restart(self):
        self.good(self.ids[0])
        self.e.reserve_deep(self.ids[0], 1000)
        self.e.reserve_deep(self.ids[1], 1031)
        restored = Engine(self.config, self.routes, self.e.saved(), now=1032)
        restored.record(self.ids[2], ProbeResult("accessible"), 1032)
        self.assertIsNone(restored.deep_due(1033, set()))
        restored.record(self.ids[2], ProbeResult("accessible"), 1062)
        self.assertIsNotNone(restored.deep_due(1062, set()))

    def test_clock_rollback_cannot_erase_consumed_probe_budget(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                engine = Engine(self.config, self.routes, now=995)
                engine.reserve_deep(self.ids[0], 1000)
                engine.reserve_deep(self.ids[1], 1031)
                if restart:
                    engine = Engine(self.config, self.routes, engine.saved(), now=995)
                engine.record(self.ids[2], ProbeResult("accessible"), 995)
                self.assertIsNone(engine.deep_due(995, set()))
                self.assertEqual(engine.deep_starts, [1000, 1031])
                engine.record(self.ids[2], ProbeResult("accessible"), 1040)
                self.assertIsNone(engine.deep_due(1040, set()))
                engine.record(self.ids[2], ProbeResult("accessible"), 1062)
                self.assertEqual(engine.deep_due(1062, set()), self.ids[2])

    def test_probe_minimum_interval_over_one_minute_survives_restart(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                config = copy.deepcopy(self.config)
                config['policy']['deep_min_interval_sec'] = 90
                engine = Engine(config, self.routes, now=1000)
                engine.reserve_deep(self.ids[0], 1000)
                if restart:
                    engine = Engine(config, self.routes, engine.saved(), now=1061)
                engine.record(self.ids[2], ProbeResult('accessible'), 1061)
                self.assertIsNone(engine.deep_due(1061, set()))
                engine.record(self.ids[2], ProbeResult('accessible'), 1089)
                self.assertIsNone(engine.deep_due(1089, set()))
                engine.record(self.ids[2], ProbeResult('accessible'), 1090)
                self.assertEqual(engine.deep_due(1090, set()), self.ids[2])

    def test_stale_or_future_qualification_cannot_admit_a_standby(self):
        self.all_good()
        for h in self.e.health.values():
            h.light_ok_at = 10000
        self.assertFalse(self.e.ready(self.ids[1], 10001))
        saved = self.e.saved()
        saved["health"][self.ids[1]]["deep_ok_at"] = 99999
        restored = Engine(self.config, self.routes, saved, now=10001)
        self.assertFalse(restored.health[self.ids[1]].qualified)

    def test_subscription_replacement_keeps_active_definition_until_switch(self):
        self.all_good()
        self.e.update_inventory(self.routes[1:])
        self.assertIn(self.ids[0], self.e.routes)
        self.e.current = self.ids[1]
        self.e.update_inventory(self.routes[1:])
        self.assertNotIn(self.ids[0], self.e.routes)

    def test_seed_is_consumed_once_and_cannot_erase_later_isolation(self):
        rid = self.ids[0]
        rows = {rid: {"kind": "healthy", "deep": True, "at": 1000}}
        self.e.seed(rows, 1001)
        self.bad(rid)
        self.e.seed(rows, 1002)
        self.assertTrue(self.e.health[rid].quarantined)
        restored = Engine(self.config, self.routes, self.e.saved(), now=1002)
        restored.seed(rows, 1003)
        self.assertTrue(restored.health[rid].quarantined)

    def test_current_light_probe_is_not_suppressed_by_a_long_deep_probe(self):
        self.good(self.ids[0])
        self.e.reserve_deep(self.ids[0], 1001)
        self.assertIn(self.ids[0], self.e.light_due(1003, set()))
        self.assertNotIn(self.ids[0], self.e.light_due(1003, {self.ids[0]}))

    def test_complete_sse_keeps_admission_when_initial_light_has_aged(self):
        rid = self.ids[1]
        self.e.record(rid, ProbeResult("accessible"), 1000)
        self.e.record(rid, ProbeResult("healthy", deep=True), 1040, started_at=1001)
        self.assertTrue(self.e.ready(rid, 1041))
        self.assertEqual(self.e.health[rid].light_ok_at, 1000)
        self.assertEqual(self.e.health[rid].deep_ok_at, 1040)

    def test_late_sse_cannot_erase_a_newer_failure(self):
        rid = self.ids[0]
        self.good(rid)
        self.e.record(rid, ProbeResult("blocked"), 1003)
        for now in (1064, 1065, 1066):
            self.e.record(rid, ProbeResult("accessible"), now)
        self.e.record(rid, ProbeResult("healthy", deep=True), 1067, started_at=1002)
        self.assertTrue(self.e.health[rid].quarantined)
        self.assertFalse(self.e.ready(rid, 1067))
        self.e.record(rid, ProbeResult("healthy", deep=True), 1069, started_at=1068)
        self.assertTrue(self.e.ready(rid, 1069))

    def test_light_success_does_not_hide_repeated_truncated_generations(self):
        rid = self.ids[0]
        self.good(rid)
        self.e.record(rid, ProbeResult("truncated", deep=True), 1002)
        self.e.record(rid, ProbeResult("accessible"), 1003)
        self.assertEqual(self.e.health[rid].failures, 1)
        self.assertEqual(self.e.deep_due(1004, set()), rid)
        self.e.record(rid, ProbeResult("truncated", deep=True), 1005)
        self.assertTrue(self.e.health[rid].quarantined)
        self.e.record(rid, ProbeResult("accessible"), 1006)
        self.assertFalse(self.e.ready(rid, 1006))
        self.assertEqual(self.e.health[rid].deep_failures, 2)


class Publisher:
    def __init__(self):
        self.routes = []
    def set(self, routes):
        changed = routes != self.routes
        self.routes = list(routes)
        return changed


class FakeController:
    def __init__(self, names, now):
        self.names, self.now, self.calls = names, now, []
    def get(self, path):
        return {"now": self.now, "all": self.names}
    def refresh(self, provider):
        self.calls.append(("refresh", provider))
    def select(self, group, name):
        self.calls.append(("select", group, name))
        self.now = name


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.config, self.routes = fixtures()
        self.e = Engine(self.config, self.routes)
        for item in self.routes:
            self.e.record(item.id, ProbeResult("accessible"), 1000)
            self.e.record(item.id, ProbeResult("healthy", deep=True), 1000.1)
        self.controller = FakeController([r.name for r in self.routes] + ["Offline"], self.routes[0].name)
        self.publisher = Publisher()
        self.director = Director(self.config, self.e, self.controller, self.publisher)

    def test_observe_mode_never_changes_the_live_selector_or_provider(self):
        self.config["mode"] = "observe"
        self.assertEqual(self.director.sync(1001)[0], "observe")
        self.assertEqual(self.controller.calls, [])

    def test_startup_provider_refuses_download_until_first_reconciliation(self):
        publisher = ProviderServer(0, 'fixture')
        self.addCleanup(publisher.close)
        url = f'http://127.0.0.1:{publisher.server.server_port}/fixture/proxies'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.assertRaises(urllib.error.HTTPError) as error:
            opener.open(url, timeout=2)
        self.assertEqual(error.exception.code, 503)
        error.exception.close()
        publisher.set([self.routes[0]])
        with opener.open(url, timeout=2) as response:
            self.assertEqual(json.load(response)['proxies'][0]['name'], self.routes[0].name)
        publisher.set([])
        with opener.open(url, timeout=2) as response:
            self.assertEqual(json.load(response)['proxies'], [{'name': 'AR/Offline', 'type': 'reject'}])

    def test_general_catalog_is_separate_from_api_admission(self):
        publisher = ProviderServer(0, "fixture")
        self.addCleanup(publisher.close)
        publisher.set_catalog(self.routes, self.config["commercial_pools"])
        publisher.set([])  # A reconciled empty admission list, not startup.
        base = f"http://127.0.0.1:{publisher.server.server_port}/fixture/"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(base + "proxies", timeout=2) as response:
            self.assertEqual(json.load(response)["proxies"], [{"name": "AR/Offline", "type": "reject"}])
        with opener.open(base + "transit/NTHU", timeout=2) as response:
            self.assertEqual([p["name"] for p in json.load(response)["proxies"]], ["N1", "N2"])
        publisher.set([self.routes[0]])
        with opener.open(base + "proxies", timeout=2) as response:
            self.assertEqual([p["name"] for p in json.load(response)["proxies"]], [self.routes[0].name, "AR/Offline"])

    def test_refresh_failure_does_not_attempt_a_selection(self):
        self.e.record(self.routes[0].id, ProbeResult("blocked"), 1001)
        self.controller.refresh = mock.Mock(side_effect=RuntimeError("unavailable"))
        with self.assertRaises(RuntimeError):
            self.director.sync(1002)
        self.assertEqual(self.controller.now, self.routes[0].name)
        self.assertEqual(self.controller.calls, [])

    def test_selected_bad_route_is_pruned_after_committing_replacement(self):
        self.e.record(self.routes[0].id, ProbeResult("blocked"), 1001)
        self.director.sync(1002)
        self.assertEqual(self.controller.now, self.routes[1].name)
        self.assertIn(self.routes[0], self.publisher.routes)
        self.director.sync(1003)
        self.assertNotIn(self.routes[0], self.publisher.routes)
        self.assertEqual([c[0] for c in self.controller.calls], ["refresh", "select", "refresh"])

    def test_all_failed_uses_explicit_offline_and_stays_network_wait(self):
        for item in self.routes:
            self.e.record(item.id, ProbeResult("blocked"), 1001)
        for now in (1002, 1003, 1004):
            self.assertEqual(self.director.sync(now)[0], "network_wait")
            self.assertEqual(self.controller.now, "Offline")
        self.assertFalse(any("DIRECT" in call for call in self.controller.calls))

    def test_same_selector_cannot_have_two_owners_in_different_state_dirs(self):
        with tempfile.TemporaryDirectory() as root:
            one = {"controller_socket": str(Path(root) / "control.sock"), "group": "test"}
            two = {**one, "state_dir": str(Path(root) / "different")}
            with singleton(one) as sockets:
                with self.assertRaisesRegex(RuntimeError, "another network guard"):
                    with singleton(two):
                        pass
            (sockets / "guard.lock").unlink()
            sockets.rmdir()

    def test_many_session_hints_coalesce_without_starting_probes(self):
        guard = Guard.__new__(Guard)
        guard.config, guard.engine, guard.hint_count = self.config, self.e, 0
        first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.setblocking(False)
        for index in range(30):
            second.send(json.dumps({"service_host": "anyrouter.test", "surface_id": str(index)}).encode())
            guard.consume_hints(first, 1002)
        self.assertEqual(guard.hint_count, 30)
        self.assertEqual(self.e.hint_at, 1002)
        self.assertEqual(self.e.deep_starts, [])


if __name__ == "__main__":
    unittest.main()

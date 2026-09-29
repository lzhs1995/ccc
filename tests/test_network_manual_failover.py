import copy
import concurrent.futures
import datetime
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_mihomo as io
import ccc_network_guard as network
import ccc_network_client as client
import test_network_guard as loop_fixtures
from test_network_guard import fixtures, Publisher


class Link:
    generation = 1
    available = True

    def current(self):
        return self.generation if self.available else None

    def accepts(self, value):
        return self.available and value == self.generation


class Controller:
    def __init__(self, config, routes):
        self.mode, self.calls, self.snapshots, self.on_snapshot = "rule", [], 0, None
        self.provider, self.reads = config["provider"], []
        self.rules = [{"type": "Domain", "payload": config["service_host"], "proxy": config["outer_group"]}]
        self.nodes = {
            "Transit": {"type": "Selector", "now": "AnyRouter", "all": ["AnyRouter"]},
            "AnyRouter": {"type": "Selector", "now": "Manual-A", "all": ["Manual-A", "Manual-B", "Automatic"]},
            "Automatic": {"type": "Selector", "now": config["group"], "all": [config["group"]]},
            config["group"]: {"type": "Selector", "now": routes[0].name,
                              "all": [r.name for r in routes] + [config["offline_proxy"]]},
            "Manual-A": {"name": "Manual-A", "type": "AnyTLS", "id": "manual-a"},
            "Manual-B": {"name": "Manual-B", "type": "Vless", "id": "manual-b"},
        }
        self.provider_nodes = {r.name: {"name": r.name, "type": "AnyTLS", "id": r.id} for r in routes}
        self.nodes.update(self.provider_nodes)

    def get(self, path):
        self.reads.append(path)
        if path == "/configs":
            return {"mode": self.mode}
        if path == "/rules":
            return {"rules": copy.deepcopy(self.rules)}
        if path == "/proxies":
            self.snapshots += 1
            if self.on_snapshot:
                self.on_snapshot(self.snapshots)
            return {"proxies": copy.deepcopy(self.nodes)}
        if path.startswith("/providers/proxies/"):
            parts = path.removeprefix("/providers/proxies/").split("/")
            if len(parts) == 2 and unquote(parts[0]) == self.provider and unquote(parts[1]) in self.provider_nodes:
                return copy.deepcopy(self.provider_nodes[unquote(parts[1])])
            raise io.ControllerError("GET", path, 404)
        name = unquote(path.removeprefix("/proxies/"))
        if name not in self.nodes:
            raise io.ControllerError("GET", path, 404)
        return copy.deepcopy(self.nodes[name])

    def refresh(self, provider):
        self.calls.append(("refresh", provider))

    def select(self, group, name):
        self.calls.append(("select", group, name))
        self.nodes[group]["now"] = name


class ManualTakeoverTests(unittest.TestCase):
    def setUp(self):
        self.config, self.routes = fixtures()
        self.config.update(group="Auto", outer_group="Transit", policy=dict(network.DEFAULTS),
            probe={"validation_mode": "reachability", "url": "https://anyrouter.test/v1/responses", "timeout_sec": 1},
            manual_failover={"enabled": True, "selector": "AnyRouter", "automatic": "Automatic"})
        self.engine = network.Engine(self.config, self.routes, now=1000)
        for item in self.routes:
            self.engine.record(item.id, io.ProbeResult("accessible"), 1000)
        self.control = Controller(self.config, self.routes)
        self.director = network.Director(self.config, self.engine, self.control, Publisher())
        self.link = Link()
        self.clock = mock.patch.object(network.time, "monotonic", return_value=100)
        self.mono = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.director.sync(1001, link=self.link)
        self.manual = self.director.manual

    def result(self, kind="transport", replacement="accessible"):
        binding = self.manual.binding
        def proof(name, chain, outcome):
            return {"name": name, "chain": copy.deepcopy(chain), "started_at": 1001,
                    "completed_at": 1002, "result": {"kind": outcome}}
        candidate = binding.get("candidate")
        return {"manual": proof(binding["selection"], binding["chain"], kind),
                "candidate": proof(candidate["name"], candidate["chain"], replacement)
                    if candidate and replacement is not None else None,
                "started_mono": 99, "completed_mono": 100}

    def failures(self, count=2, kind="transport", replacement="accessible"):
        for _ in range(count):
            self.manual.record(self.result(kind, replacement), self.manual.epoch, 1, self.link)

    def manual_writes(self):
        return [c for c in self.control.calls if c[:2] == ("select", "AnyRouter")]

    def test_healthy_user_choice_is_sticky(self):
        self.failures(4, kind="accessible")
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])
        self.assertEqual(self.manual.snapshot()["state"], "healthy")

    def test_one_failure_does_not_override_user(self):
        self.failures(1)
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_two_actual_failures_and_new_replacement_check_take_over_once(self):
        self.failures()
        self.assertEqual(self.director.sync(1002, link=self.link)[0], "healthy")
        self.assertEqual(self.manual_writes(), [("select", "AnyRouter", "Automatic")])
        self.assertTrue(self.director.effective_route["managed"])
        self.assertTrue(self.manual.last_switch["confirmed"])
        self.director.sync(1003, link=self.link)
        self.assertEqual(len(self.manual_writes()), 1)

    def test_fresh_replacement_is_required(self):
        for replacement in (None, "blocked", "transport", "observer_error"):
            with self.subTest(replacement=replacement):
                self.failures(replacement=replacement)
                self.director.sync(1002, link=self.link)
                self.assertEqual(self.manual_writes(), [])

    def test_stale_probes_never_authorize_takeover(self):
        self.failures()
        self.mono.return_value = 121
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_old_epoch_cannot_supply_new_failure_streak(self):
        result, epoch = self.result(), self.manual.epoch
        self.control.nodes["AnyRouter"]["now"] = "Manual-B"
        self.director.sync(1002, link=self.link)
        self.assertFalse(self.manual.record(result, epoch, 1, self.link))
        self.assertEqual(self.manual.failures, 0)

    def test_user_override_discards_already_completed_failure(self):
        self.failures()
        self.control.nodes["AnyRouter"]["now"] = "Manual-B"
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])
        self.assertEqual(self.manual.failures, 0)

    def test_user_override_during_final_read_cannot_be_overwritten(self):
        self.failures()
        after_observe = self.control.snapshots + 2
        def change(number):
            if number == after_observe:
                self.control.nodes["AnyRouter"]["now"] = "Manual-B"
        self.control.on_snapshot = change
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])
        self.assertEqual(self.control.nodes["AnyRouter"]["now"], "Manual-B")

    def test_reload_same_name_new_proxy_identity_discards_evidence(self):
        self.failures()
        self.control.nodes["Manual-A"]["id"] = "reloaded-object"
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_changed_fixed_dialer_discards_evidence(self):
        self.control.nodes["Manual-A"]["dialer-proxy"] = "Manual-B"
        self.director.sync(1001, link=self.link)
        self.failures()
        self.control.nodes["Manual-B"]["id"] = "new-dialer"
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_changed_replacement_identity_needs_new_check(self):
        self.failures()
        self.control.nodes[self.routes[0].name]["id"] = "new-auto-object"
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_provider_children_absent_from_global_inventory_still_take_over(self):
        for route in self.routes:
            del self.control.nodes[route.name]
        self.director.sync(1002, link=self.link)
        self.failures()
        self.director.sync(1003, link=self.link)
        self.assertEqual(self.manual_writes(), [("select", "AnyRouter", "Automatic")])
        self.assertIn(io.live_proxy_path(self.routes[0].name, self.config["provider"]), self.control.reads)

    def test_conflicting_same_name_global_proxy_cannot_replace_manual_route(self):
        name = self.routes[0].name
        self.control.nodes[name] = {**self.control.provider_nodes[name], "id": "different-global-object"}
        self.director.sync(1002, link=self.link)
        self.assertIsNone(self.manual.binding["candidate"])
        self.failures()
        self.director.sync(1003, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_same_id_with_different_dialer_is_also_ambiguous(self):
        name = self.routes[0].name
        self.control.nodes[name] = {**self.control.provider_nodes[name], "dialer-proxy": "Manual-B"}
        self.director.sync(1002, link=self.link)
        self.assertIsNone(self.manual.binding["candidate"])
        self.failures()
        self.director.sync(1003, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_missing_provider_child_cannot_fall_back_to_same_named_global_proxy(self):
        self.failures()
        self.control.provider_nodes.clear()
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_foreign_provider_response_cannot_authorize_takeover(self):
        self.failures()
        name = self.routes[0].name
        self.control.provider_nodes[name] = {**self.control.provider_nodes[name], "provider-name": "unrelated"}
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def mutate_during_final_provider_lookup(self, mutation):
        self.failures()
        reads = 0
        original = self.control.get
        def get(path):
            nonlocal reads
            if path.startswith("/providers/proxies/"):
                reads += 1
                if reads == 2:
                    mutation()
            return original(path)
        with mock.patch.object(self.control, "get", side_effect=get):
            self.director.sync(1002, link=self.link)
        self.assertGreaterEqual(reads, 2)
        self.assertEqual(self.manual_writes(), [])

    def test_manual_change_during_final_provider_lookup_cannot_be_overwritten(self):
        self.mutate_during_final_provider_lookup(lambda: self.control.nodes["AnyRouter"].update(now="Manual-B"))
        self.assertEqual(self.control.nodes["AnyRouter"]["now"], "Manual-B")

    def test_direct_mode_during_final_provider_lookup_revokes_takeover(self):
        self.mutate_during_final_provider_lookup(lambda: setattr(self.control, "mode", "direct"))

    def test_changed_rule_during_final_provider_lookup_revokes_takeover(self):
        self.mutate_during_final_provider_lookup(lambda: self.control.rules[0].update(proxy="Manual-B"))

    def test_changed_manual_identity_during_final_provider_lookup_revokes_takeover(self):
        self.mutate_during_final_provider_lookup(lambda: self.control.nodes["Manual-A"].update(id="new-proxy-object"))

    def test_manual_change_on_last_selector_read_prevents_put(self):
        self.failures()
        original = self.control.get
        changed = False
        def get(path):
            nonlocal changed
            if path == "/proxies/AnyRouter" and self.control.snapshots >= 4:
                self.control.nodes["AnyRouter"]["now"] = "Manual-B"
                changed = True
            return original(path)
        with mock.patch.object(self.control, "get", side_effect=get):
            self.director.sync(1002, link=self.link)
        self.assertTrue(changed)
        self.assertEqual(self.manual_writes(), [])
        self.assertEqual(self.control.nodes["AnyRouter"]["now"], "Manual-B")

    def slow_final_read(self, path_name):
        self.failures()
        original = self.control.get
        delayed = False
        def get(path):
            nonlocal delayed
            value = original(path)
            if not delayed and path == path_name and self.control.snapshots >= 4:
                self.mono.return_value += 1
                delayed = True
            return value
        with mock.patch.object(self.control, "get", side_effect=get):
            self.director.sync(1002, link=self.link)
        self.assertTrue(delayed)
        self.assertEqual(self.manual_writes(), [])

    def test_slow_final_route_snapshot_cannot_authorize_takeover(self):
        self.slow_final_read("/proxies")

    def test_slow_last_manual_selector_read_cannot_authorize_takeover(self):
        self.slow_final_read("/proxies/AnyRouter")

    def test_changed_automatic_child_cannot_receive_old_evidence(self):
        self.failures()
        self.control.nodes["Auto"]["now"] = self.routes[1].name
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_background_candidate_must_still_be_qualified(self):
        self.failures()
        for rid in self.engine.routes:
            self.engine.record(rid, io.ProbeResult("blocked"), 1002)
        self.director.sync(1003, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_link_generation_change_is_not_route_failure(self):
        self.failures()
        self.link.generation += 1
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])
        self.assertEqual(self.manual.failures, 0)

    def test_observer_or_link_outage_never_takes_over(self):
        self.failures()
        self.director.sync(1002, link=self.link, observer_error="local storage unavailable")
        self.assertEqual(self.manual_writes(), [])
        self.link.available = False
        self.director.sync(1003, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_observer_failure_breaks_consecutive_streak(self):
        self.failures(1)
        self.failures(1, kind="observer_error")
        self.failures(1)
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_disabled_policy_keeps_legacy_manual_behavior(self):
        self.failures()
        self.config["manual_failover"]["enabled"] = False
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_observe_policy_has_no_selector_writes(self):
        self.failures()
        self.config["mode"] = "observe"
        self.director.sync(1002, link=self.link)
        self.assertEqual(self.manual_writes(), [])

    def test_global_direct_or_unrelated_rule_cannot_authorize_writes(self):
        for mode in ("direct", "global", "rule"):
            with self.subTest(mode=mode):
                self.control.mode = mode
                self.control.nodes["GLOBAL"] = self.control.nodes["Transit"]
                if mode == "rule":
                    self.control.rules.insert(0, {"type": "ProcessName", "payload": "codex", "proxy": "AnyRouter"})
                self.director.sync(1002, link=self.link)
                self.assertIsNone(self.manual.binding)
                self.assertEqual(self.manual_writes(), [])

    def test_dynamic_dialer_cannot_be_reported_as_a_fixed_route(self):
        self.control.nodes["Manual-A"]["dialer-proxy"] = "Auto"
        self.director.sync(1002, link=self.link)
        self.assertIsNone(self.manual.binding)

    def test_new_guard_cannot_reuse_old_manual_failure_streak(self):
        self.failures()
        replacement = network.Director(self.config, self.engine, self.control, Publisher())
        replacement.sync(1002, link=self.link)
        self.assertEqual(replacement.manual.failures, 0)
        self.assertEqual(self.manual_writes(), [])

    def test_failed_put_is_not_blindly_replayed(self):
        self.failures()
        real = self.control.select
        def lost(group, name):
            real(group, name)
            raise TimeoutError("ack lost")
        with mock.patch.object(self.control, "select", side_effect=lost):
            self.director.sync(1002, link=self.link)
            self.director.sync(1003, link=self.link)
        self.assertEqual(len(self.manual_writes()), 1)
        self.assertFalse(self.manual.last_switch["confirmed"])


class DelayController:
    def __init__(self, status, *, delay=20, stale=False, missing=False, changed=False, controller_error=None,
                 provider="", name="Manual"):
        self.status, self.delay, self.stale, self.missing = status, delay, stale, missing
        self.changed, self.controller_error = changed, controller_error
        self.node = {"name": name, "type": "AnyTLS", "id": "exact-object", "alive": True, "extra": {}}
        self.provider, self.paths = provider, []
        if provider:
            self.node["provider-name"] = provider
        self.query = None

    def get(self, path):
        self.paths.append(path)
        prefix = io.live_proxy_path(self.node["name"], self.provider)
        endpoint = prefix + ("/healthcheck?" if self.provider else "/delay?")
        if path != prefix and not path.startswith(endpoint):
            raise io.ControllerError("GET", path, 404)
        if path.startswith(endpoint):
            self.query = parse_qs(urlsplit(path).query)
            stamp = time.time() - (100 if self.stale else 0)
            if not self.missing:
                self.node["extra"][self.query["url"][0]] = {"alive": self.status != 403,
                    "history": [{"time": datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).isoformat(),
                                 "delay": self.delay if self.status != 403 else 0}]}
            if self.changed:
                self.node["id"] = "replacement-object"
            if self.controller_error or self.delay == 0:
                raise io.ControllerError("GET", path, self.controller_error or 503)
            return {"delay": self.delay}
        return copy.deepcopy(self.node)


class LiveContractTests(unittest.TestCase):
    def run_probe(self, status, **kwargs):
        control = DelayController(status, **kwargs)
        chain = io.live_proxy_chain("Manual", lambda _: control.node)
        probe = io.LiveReachabilityProbe(control, "https://anyrouter.test/v1/responses", 1)
        return probe.run("Manual", chain), control

    def test_401_429_500_do_not_mean_a_banned_node(self):
        for status in (200, 204, 400, 401, 402, 404, 405, 429, 500, 503, 599):
            with self.subTest(status=status):
                result, control = self.run_probe(status)
                self.assertEqual(result["result"]["kind"], "accessible")
                self.assertEqual(control.query["expected"], ["200-402/404-599"])
                self.assertEqual(set(control.query), {"url", "timeout", "expected"})

    def test_nonzero_delay_and_generic_alive_cannot_hide_403(self):
        result, control = self.run_probe(403)
        self.assertTrue(control.node["alive"])
        self.assertEqual(result["result"]["kind"], "blocked")

    def test_zero_millisecond_controller_503_can_be_a_good_route(self):
        result, _ = self.run_probe(401, delay=0)
        self.assertEqual(result["controller_status"], 503)
        self.assertEqual(result["result"]["kind"], "accessible")

    def test_missing_or_stale_url_evidence_is_observer_failure(self):
        for change in ({"missing": True}, {"stale": True}, {"changed": True}, {"controller_error": 400}):
            with self.subTest(change=change):
                result, _ = self.run_probe(403, **change)
                self.assertEqual(result["result"]["kind"], "observer_error")

    def test_transport_timeout_requires_fresh_failed_url_record(self):
        result, _ = self.run_probe(403, delay=0, controller_error=504)
        self.assertEqual(result["result"]["kind"], "transport")

    def test_live_id_changed_before_dispatch_makes_no_health_request(self):
        control = DelayController(403)
        chain = io.live_proxy_chain("Manual", lambda _: control.node)
        control.node["id"] = "new-object"
        result = io.LiveReachabilityProbe(control, "https://anyrouter.test/", 1).run("Manual", chain)
        self.assertEqual(result["result"]["kind"], "observer_error")
        self.assertIsNone(control.query)

    def test_provider_probe_checks_one_exact_child_without_fanout_or_global_fallback(self):
        name, provider = "Proxy / 台湾", "Pool / eligible"
        control = DelayController(500, provider=provider, name=name)
        chain = io.live_proxy_chain(name, lambda _: control.node)
        probe = io.LiveReachabilityProbe(control, "https://anyrouter.test/v1/responses", 1)
        result = probe.run(name, chain)
        self.assertEqual(result["result"]["kind"], "accessible")
        prefix = io.live_proxy_path(name, provider)
        self.assertEqual([p for p in control.paths if "?" not in p], [prefix, prefix])
        checks = [p for p in control.paths if "?" in p]
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0].startswith(prefix + "/healthcheck?"))
        self.assertEqual(control.query["expected"], ["200-402/404-599"])
        self.assertEqual(set(control.query), {"url", "timeout", "expected"})


class ManualConfigTests(unittest.TestCase):
    def config(self):
        config, _ = fixtures()
        config.update(version=1, group="Auto", outer_group="Transit", state_dir="/tmp/network-state",
            binary="/tmp/mihomo", controller_socket="/tmp/private.sock", interface="lo0",
            publish={"port": 18000, "token": "x" * 32},
            sources=[{"pool": "NTHU", "path": "/tmp/subscription"}],
            probe={"validation_mode": "reachability", "url": "https://anyrouter.test/v1/responses"},
            manual_failover={"enabled": True, "selector": "AnyRouter", "automatic": "Automatic"})
        return config

    def test_activation_rejects_ambiguous_policy_and_dangerous_selector(self):
        for value in (None, [], {"enabled": "true"}, {"enabled": True},
                      {"enabled": True, "selector": "GLOBAL", "automatic": "Automatic"},
                      {"enabled": True, "selector": "Auto", "automatic": "Automatic"},
                      {"enabled": True, "selector": "AnyRouter", "automatic": "DIRECT"},
                      {"enabled": True, "selector": "AnyRouter", "automatic": "Automatic", "force": True}):
            with self.subTest(policy=value), tempfile.TemporaryDirectory() as tmp:
                config = self.config(); config["manual_failover"] = value
                path = Path(tmp) / "config.json"; path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    network.load_config(path)

    def test_enabled_policy_is_anonymous_and_opt_in(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"; path.write_text(json.dumps(config))
            self.assertTrue(network.load_config(path)["manual_failover"]["enabled"])
            config["probe"].update(validation_mode="response", model="test")
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                network.load_config(path)

    def test_summary_shows_actual_manual_protection_before_pool_count(self):
        value = {"phase": "manual", "validation_mode": "reachability",
            "effective_route": {"selection": "Manual-A"},
            "automatic": {"validation_mode": "reachability", "summary": "可达 34/36", "ready": 30},
            "manual_failover": {"enabled": True, "summary": "手选可达 · 故障自动接替已开启"}}
        text = client.summary(value)
        self.assertLess(text.index("故障自动接替"), text.index("可达 34/36"))
        for corrupt in (None, [], "wrong", {"enabled": True, "summary": []}):
            self.assertIsInstance(client.summary({**value, "manual_failover": corrupt}), str)


class ManualGuardLoopTests(unittest.TestCase):
    patch = loop_fixtures.GuardLoopTests.patch
    stepped_loop = loop_fixtures.GuardLoopTests.stepped_loop

    def setUp(self):
        loop_fixtures.GuardLoopTests.setUp(self)
        self.config.update(group="Auto", outer_group="Transit",
            probe={"validation_mode": "reachability", "url": "https://anyrouter.test/v1/responses", "timeout_sec": 1},
            manual_failover={"enabled": True, "selector": "AnyRouter", "automatic": "Automatic"})
        self.controller = Controller(self.config, self.routes)
        self.patch("Controller", return_value=self.controller)
        self.probe.run.side_effect = lambda *_: io.ProbeResult("accessible")

    @staticmethod
    def failed_probe(config, binding, candidate, link, generation):
        def proof(name, chain, kind):
            return {"name": name, "chain": chain, "result": {"kind": kind}}
        return {"manual": proof(binding["selection"], binding["chain"], "transport"),
                "candidate": proof(candidate["name"], candidate["chain"], "accessible") if candidate else None,
                "started_mono": network.time.monotonic(), "completed_mono": network.time.monotonic()}

    def test_real_guard_loop_consumes_proofs_and_performs_one_manual_takeover(self):
        with mock.patch.object(network.ManualFailover, "run_probe", side_effect=self.failed_probe) as probe:
            self.stepped_loop(list(range(1000, 1012)))
        switches = [c for c in self.controller.calls if c[:2] == ("select", "AnyRouter")]
        self.assertEqual(switches, [("select", "AnyRouter", "Automatic")])
        self.assertEqual(probe.call_count, 2)
        self.assertTrue(any(s["manual_failover"]["state"] == "automatic" for s in self.snapshots))
        self.assertLessEqual(max(s["probe_in_flight"] for s in self.snapshots), 4)
        self.assertEqual(self.guard.engine.deep_starts, [])

    def test_pending_manual_worker_does_not_block_heartbeat_or_exceed_four_probes(self):
        pending = []
        class HeldExecutor(loop_fixtures.ImmediateExecutor):
            def submit(executor, function, *args):
                if function is network.ManualFailover.run_probe:
                    future = concurrent.futures.Future()
                    pending.append(future)
                    return future
                return super().submit(function, *args)
        def setup(clock):
            self.patch("concurrent.futures.ThreadPoolExecutor", new=HeldExecutor)
        self.stepped_loop(list(range(1000, 1010)), setup=setup)
        self.assertEqual(len(pending), 1)
        self.assertGreaterEqual(len(self.snapshots), 9)
        self.assertLessEqual(max(s["probe_in_flight"] for s in self.snapshots), 4)
        self.assertEqual(self.guard.engine.deep_starts, [])
        self.assertFalse(any(c[:2] == ("select", "AnyRouter") for c in self.controller.calls))
        pending[0].cancel()


if __name__ == "__main__":
    unittest.main()

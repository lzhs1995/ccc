import copy
import concurrent.futures
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_mihomo import PhysicalLink, ProbeResult, route
import ccc_network_guard as network
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


class ImmediateExecutor:
    """Deterministic completed jobs for clock and stale-result regressions."""
    def __init__(self, **kwargs):
        pass

    def submit(self, function, *args):
        result = concurrent.futures.Future()
        try:
            result.set_result(function(*args))
        except Exception as exc:
            result.set_exception(exc)
        return result

    def shutdown(self, **kwargs):
        pass


class GuardLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ccc-loop-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config, self.routes = fixtures()
        self.config.update(state_dir=str(self.root), controller_socket="/fixture/controller.sock",
                           binary="/fixture/mihomo", interface="en0", probe={},
                           sources=[{"pool": "NTHU", "path": "/fixture/original.json"}],
                           publish={"port": 0, "token": "fixture"},
                           policy={**network.DEFAULTS, "inventory_interval_sec": 1})
        self.publisher = Publisher()
        self.publisher.close = mock.Mock()
        self.publisher.set_catalog = mock.Mock()
        self.controller = FakeController([r.name for r in self.routes] + ["Offline"], self.routes[0].name)
        self.core = types.SimpleNamespace(process=mock.Mock(pid=4242), ports={}, close=mock.Mock())
        self.core.process.poll.return_value = None
        self.probe = mock.Mock()
        self.probe.run.side_effect = lambda item, deep: ProbeResult("healthy" if deep else "accessible", deep=deep)
        self.config_reader = self.patch("load_config", return_value=self.config)
        self.inventory = self.patch("inventory", side_effect=lambda config: list(self.routes))
        self.patch("ProviderServer", return_value=self.publisher)
        self.patch("Controller", return_value=self.controller)
        self.patch("ResponsesProbe", return_value=self.probe)
        self.link_reader = mock.Mock(return_value=("192.0.2.10",))
        self.link = PhysicalLink("fixture0", self.link_reader)
        self.link.sample()
        self.link.start = mock.Mock(side_effect=self.link.sample)
        self.patch("PhysicalLink", return_value=self.link)
        shadow_patch = mock.patch.object(Guard, "new_shadow", return_value=(self.core, list(self.routes)))
        self.shadow = shadow_patch.start()
        self.addCleanup(shadow_patch.stop)
        self.guard = Guard(self.root / "network.json")
        self.snapshots = []
        write = network.atomic_json

        def capture(path, value):
            write(path, value)
            if Path(path).name == "status.json" and value.get("phase") != "stopped":
                self.snapshots.append(value)

        self.patch("atomic_json", side_effect=capture)

    def patch(self, name, **options):
        patch = mock.patch("ccc_network_guard." + name, **options)
        value = patch.start()
        self.addCleanup(patch.stop)
        return value

    def stepped_loop(self, ticks, *, setup=None):
        clock = [ticks[0]]
        self.patch("time", new=types.SimpleNamespace(time=lambda: clock[0]))
        self.patch("concurrent.futures.ThreadPoolExecutor", new=ImmediateExecutor)
        pending = iter(ticks)

        def tick(timeout):
            value = next(pending, None)
            if value is None:
                return True
            clock[0] = value
            return False

        self.guard.stop = types.SimpleNamespace(wait=tick)
        if setup:
            setup(clock)
        self.guard._run(self.root)
        return clock

    def test_slow_inventory_cannot_block_results_routing_or_heartbeat(self):
        self.config["policy"]["inventory_interval_sec"] = .01
        entered, release, advanced = threading.Event(), threading.Event(), threading.Event()
        reads = 0

        def inventory(config):
            nonlocal reads
            reads += 1
            if reads > 1:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("fixture did not release subscription")
            return list(self.routes)

        self.inventory.side_effect = inventory
        original_get = self.controller.get

        def get(path):
            if entered.is_set():
                advanced.set()
            return original_get(path)

        self.controller.get = get
        errors = []

        def run():
            try:
                self.guard._run(self.root)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(3), "periodic subscription read did not begin")
            before = len(self.snapshots)
            self.assertTrue(advanced.wait(3), "routing stopped behind subscription parsing")
            deadline = time.monotonic() + 3
            while len(self.snapshots) <= before and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertGreater(len(self.snapshots), before, "heartbeat stopped behind subscription parsing")
            self.assertTrue(any(h.deep_ok_at for h in self.guard.engine.health.values()),
                            "completed probes were not collected during subscription parsing")
        finally:
            release.set()
            self.guard.stop.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_delayed_collection_keeps_worker_completion_time(self):
        self.stepped_loop([1000, 1100])
        health = self.guard.engine.health[self.routes[0].id]
        self.assertEqual(health.light_ok_at, 1000)
        self.assertEqual(self.guard.engine.deep_starts, [],
                         "an old light result must not authorize a new paid probe")

    def test_deep_reservation_uses_time_after_local_config_work(self):
        dispatched = []

        def setup(clock):
            engine = Engine(self.config, self.routes, now=1000)
            engine.record(self.routes[0].id, ProbeResult("accessible"), 999)
            network.atomic_json(self.root / "health.json", {**engine.saved(),
                                "contract": contract_digest(self.config),
                                "routes": [r.record() for r in self.routes]})

            def config_read(path):
                clock[0] = 1007
                return self.config

            def probe(item, deep):
                if deep:
                    dispatched.append(clock[0])
                return ProbeResult("healthy" if deep else "accessible", deep=deep)

            self.config_reader.side_effect = config_read
            self.probe.run.side_effect = probe

        self.stepped_loop([1000], setup=setup)
        self.assertEqual(dispatched, [1007])
        self.assertEqual(self.guard.engine.deep_starts, dispatched)

    def test_deep_reservation_uses_time_after_result_collection(self):
        dispatched = []

        def setup(clock):
            record = Engine.record

            def delayed_record(engine, *args, **kwargs):
                clock[0] = 1007
                return record(engine, *args, **kwargs)

            patch = mock.patch.object(Engine, "record", new=delayed_record)
            patch.start()
            self.addCleanup(patch.stop)

            def probe(item, deep):
                if deep:
                    dispatched.append(clock[0])
                return ProbeResult("healthy" if deep else "accessible", deep=deep)

            self.probe.run.side_effect = probe

        self.stepped_loop([1000, 1001], setup=setup)
        self.assertEqual(dispatched, [1007])
        self.assertEqual(self.guard.engine.deep_starts, dispatched)

    def test_completed_light_failure_precedes_old_deep_success(self):
        item = self.routes[0]
        engine = Engine(self.config, self.routes, now=1006)
        engine.current = item.id
        engine.record(item.id, ProbeResult("blocked"), 936)
        for stamp in (997, 998, 999):
            engine.record(item.id, ProbeResult("accessible"), stamp)
        engine.reserve_deep(item.id, 1000)
        network.atomic_json(self.root / "health.json", {**engine.saved(),
                            "contract": contract_digest(self.config),
                            "routes": [r.record() for r in self.routes]})
        # The deep request entered the queue first, but its success completed
        # after a newer light failure. Both are collected in the same tick.
        for deep, began, completed, kind in ((True, 1000, 1005, "healthy"),
                                              (False, 1001, 1002, "timeout")):
            job = concurrent.futures.Future()
            job.set_result((ProbeResult(kind, deep=deep), completed))
            self.guard.jobs[job] = (item.id, deep, self.core, began, contract_digest(self.config), self.link.current())
        self.stepped_loop([1006])
        health = self.guard.engine.health[item.id]
        self.assertTrue(health.quarantined, "an old SSE erased an intervening failure")
        self.assertFalse(health.qualified)
        self.assertEqual(health.deep_ok_at, 0)
        self.assertEqual(health.failure_at, 1002)
        self.assertEqual(self.guard.engine.deep_starts, [1000])

    def test_unchanged_inventory_recovers_without_hiding_a_known_outage(self):
        engine = Engine(self.config, self.routes, now=1000)
        for item in self.routes:
            engine.record(item.id, ProbeResult("blocked"), 999)
        network.atomic_json(self.root / "health.json", {**engine.saved(),
                            "contract": contract_digest(self.config),
                            "routes": [r.record() for r in self.routes]})
        self.inventory.side_effect = [list(self.routes), subprocess.TimeoutExpired("fixture", 5),
                                      list(self.routes), list(self.routes)]
        self.probe.run.side_effect = lambda item, deep: ProbeResult("observer_error", deep=deep)
        self.stepped_loop([1000, 1002, 1004, 1006, 1008])
        failures = [s for s in self.snapshots if "subscription refresh failed" in s.get("error", "")]
        self.assertTrue(failures)
        self.assertTrue(all(s["phase"] == "network_wait" for s in failures))
        self.assertEqual(self.guard.inventory_error, "")
        self.assertEqual(self.guard.shadow_error, "")
        self.assertEqual(self.guard.phase, "network_wait")

    def test_inventory_completion_from_old_config_cannot_replace_live_routes(self):
        updated = copy.deepcopy(self.config)
        updated["sources"][0]["path"] = "/fixture/updated.json"
        self.config_reader.side_effect = [self.config, self.config, updated]
        extra = route("NTHU", {"name": "old-config-only", "type": "http", "server": "127.0.0.1", "port": 9009})
        self.inventory.side_effect = [list(self.routes), [*self.routes, extra], list(self.routes)]
        self.stepped_loop([1000, 1002, 1004])
        self.assertEqual(set(self.guard.engine.routes), {r.id for r in self.routes})
        self.assertEqual(self.shadow.call_count, 1)

    def test_subscription_recovery_does_not_clear_a_probe_core_error(self):
        reads = 0

        def inventory(config):
            nonlocal reads
            reads += 1
            if reads == 2:
                raise subprocess.TimeoutExpired("fixture", 5)
            if reads == 3:
                self.guard.shadow_error = "isolated probe core unavailable: fixture"
            return list(self.routes)

        self.inventory.side_effect = inventory
        self.stepped_loop([1000, 1002, 1004, 1006, 1008])
        self.assertFalse(self.snapshots[-1].get("inventory_error"))
        self.assertEqual(self.guard.shadow_error, "isolated probe core unavailable: fixture")
        self.assertEqual(self.guard.phase, "observer_fault")

    def test_startup_parser_timeout_can_use_matching_saved_inventory(self):
        engine = Engine(self.config, self.routes, now=1000)
        network.atomic_json(self.root / "health.json", {**engine.saved(),
                            "contract": contract_digest(self.config),
                            "routes": [r.record() for r in self.routes]})
        self.inventory.side_effect = [subprocess.TimeoutExpired("fixture", 5), list(self.routes)]
        self.stepped_loop([1000, 1002, 1004])
        self.assertEqual(set(self.guard.engine.routes), {r.id for r in self.routes})
        self.assertEqual(self.guard.inventory_error, "")

    def save_engine(self, engine):
        network.atomic_json(self.root / "health.json", {**engine.saved(),
                            "contract": contract_digest(self.config),
                            "routes": [r.record() for r in self.routes]})

    def test_cross_generation_sse_cannot_admit_or_refund_its_reservation(self):
        item = self.routes[0]
        engine = Engine(self.config, self.routes, now=1000)
        engine.current = item.id
        engine.record(item.id, ProbeResult("blocked"), 936)
        for stamp in (997, 998, 999):
            engine.record(item.id, ProbeResult("accessible"), stamp)
        engine.reserve_deep(item.id, 1000)
        self.save_engine(engine)
        old_generation = self.link.current()
        job = concurrent.futures.Future()
        job.set_result((ProbeResult("healthy", deep=True, stage="response_body"), 1001))
        self.guard.jobs[job] = (item.id, True, self.core, 1000, contract_digest(self.config), old_generation)
        self.link_reader.return_value = ()
        self.link.sample()
        self.link_reader.return_value = ("192.0.2.10",)
        self.link.sample()
        self.stepped_loop([1002])
        health = self.guard.engine.health[item.id]
        self.assertTrue(health.quarantined)
        self.assertFalse(health.qualified)
        self.assertEqual(health.deep_ok_at, 0)
        self.assertEqual(health.deep_attempt_at, 1000)
        self.assertEqual(self.guard.engine.deep_starts, [1000])
        self.assertEqual(self.guard.discarded_probes, 1)
        event = self.guard.last_probe_event
        self.assertFalse(event["accepted"])
        self.assertEqual(event["stage"], "response_body")
        self.assertEqual(event["discard_reason"], "physical_interface_generation_changed")

    def test_cross_generation_failures_cannot_isolate_previously_verified_routes(self):
        item = self.routes[0]
        engine = Engine(self.config, self.routes, now=1000)
        engine.current = item.id
        engine.record(item.id, ProbeResult("accessible"), 998)
        engine.record(item.id, ProbeResult("healthy", deep=True), 999)
        engine.reserve_deep(item.id, 1000)
        self.save_engine(engine)
        old_generation = self.link.current()
        for deep, kind in ((True, "timeout"), (False, "blocked")):
            job = concurrent.futures.Future()
            job.set_result((ProbeResult(kind, deep=deep, stage="response_headers"), 1001))
            self.guard.jobs[job] = (item.id, deep, self.core, 1000, contract_digest(self.config), old_generation)
        self.link_reader.return_value = ("192.0.2.11",)
        self.link.sample()
        self.stepped_loop([1002])
        health = self.guard.engine.health[item.id]
        self.assertTrue(health.qualified)
        self.assertFalse(health.quarantined)
        self.assertEqual(health.failures, 0)
        self.assertEqual(health.deep_ok_at, 999)
        self.assertEqual(self.guard.discarded_probes, 2)
        self.assertEqual(self.guard.engine.deep_starts, [1000])

    def test_unavailable_interface_preserves_current_provider_and_all_prior_evidence(self):
        engine = Engine(self.config, self.routes, now=1000)
        engine.current = self.routes[0].id
        for item in self.routes:
            engine.record(item.id, ProbeResult("blocked"), 999)
        engine.reserve_deep(self.routes[0].id, 1000)
        self.save_engine(engine)
        prior_health = copy.deepcopy(engine.saved()["health"])
        self.link_reader.return_value = ()
        self.link.sample()
        self.stepped_loop([1001, 1002, 1003])
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.publisher.routes, [])
        self.probe.run.assert_not_called()
        self.assertEqual(self.guard.director.actual_name, self.routes[0].name)
        self.assertEqual(self.guard.phase, "observer_fault")
        self.assertEqual(self.guard.engine.saved()["health"], prior_health)
        self.assertEqual(self.guard.engine.deep_starts, [1000])
        self.assertFalse(self.snapshots[-1]["physical_link"]["available"])

    def test_unavailable_interface_does_not_hide_an_already_selected_offline(self):
        self.controller.now = "Offline"
        self.link_reader.return_value = ()
        self.link.sample()
        self.stepped_loop([1000, 1001])
        self.assertEqual(self.guard.phase, "network_wait")
        self.assertEqual(self.controller.calls, [])
        self.probe.run.assert_not_called()

    def test_link_loss_between_persistent_reservation_and_dispatch_keeps_budget_spent(self):
        engine = Engine(self.config, self.routes, now=1000)
        engine.current = self.routes[0].id
        engine.record(self.routes[0].id, ProbeResult("accessible"), 999)
        self.save_engine(engine)
        save = self.guard.save

        def saved_then_down():
            save()
            if self.guard.engine.deep_starts:
                self.link_reader.return_value = ()
                self.link.sample()

        self.guard.save = saved_then_down
        self.stepped_loop([1000, 1001])
        self.probe.run.assert_not_called()
        self.assertEqual(self.guard.engine.deep_starts, [1000])
        self.assertEqual(self.guard.engine.health[self.routes[0].id].deep_attempt_at, 1000)
        saved = json.loads((self.root / "health.json").read_text())
        self.assertEqual(saved["deep_starts"], [1000])
        self.assertGreaterEqual(self.guard.discarded_probes, 1)

    def test_probe_stages_and_bounded_journal_preserve_failure_diagnostics(self):
        item = self.routes[0]
        engine = Engine(self.config, self.routes, now=1000)
        engine.record(item.id, ProbeResult("timeout", detail="probe deadline exceeded", stage="tls"), 1000)
        self.assertEqual(engine.health[item.id].light_stage, "tls")
        self.assertEqual(engine.health[item.id].light_detail, "probe deadline exceeded")
        (self.root / "events.ndjson").write_text("x" * (2 * 1024 * 1024))
        self.guard.journal({"event": "fixture", "stage": "tls"})
        self.assertEqual((self.root / "events.1.ndjson").stat().st_size, 2 * 1024 * 1024)
        self.assertEqual(json.loads((self.root / "events.ndjson").read_text()), {"event": "fixture", "stage": "tls"})

    def test_network_launchagent_uses_interactive_resource_class_only(self):
        with mock.patch.object(Path, "home", return_value=self.root), \
             mock.patch.object(network.subprocess, "run"), \
             mock.patch("cmux_codex_watch._bootstrap_runtime_service"):
            installed = network.install(self.root / "network.json", source=self.root / "ccc_network_guard.py")
        payload = network.plistlib.loads(Path(installed["plist"]).read_bytes())
        self.assertEqual(payload["ProcessType"], "Interactive")
        self.assertEqual(payload["ProgramArguments"][-2:], ["--config", str((self.root / "network.json").resolve())])


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

    def test_link_changes_during_controller_read_hold_all_publication(self):
        reader = mock.Mock(return_value=("192.0.2.10",))
        link = PhysicalLink("fixture0", reader)
        link.sample()
        get = self.controller.get
        def down(path):
            reader.return_value = ()
            link.sample()
            return get(path)
        self.controller.get = down
        self.assertEqual(self.director.sync(1001, link=link)[0], "observer_fault")
        self.assertEqual(self.controller.calls, [])
        self.assertEqual(self.publisher.routes, [])

    def test_link_loss_during_refresh_cannot_commit_a_route_switch(self):
        self.e.record(self.routes[0].id, ProbeResult("blocked"), 1001)
        reader = mock.Mock(return_value=("192.0.2.10",))
        link = PhysicalLink("fixture0", reader)
        link.sample()
        refresh = self.controller.refresh
        def down(provider):
            refresh(provider)
            reader.return_value = ()
            link.sample()
        self.controller.refresh = down
        self.assertEqual(self.director.sync(1002, link=link)[0], "observer_fault")
        self.assertEqual(self.controller.now, self.routes[0].name)
        self.assertEqual(self.controller.calls, [("refresh", "verified")])

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

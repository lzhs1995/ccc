import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_mihomo import ControllerError, ProbeResult, effective_service_route
from ccc_network_guard import Director, Engine
from test_network_guard import fixtures, Publisher
from tools.network_profile import build_profile, manual_name, MANUAL_GROUP


class RoutingController:
    def __init__(self, config, routes):
        self.config, self.mode = config, "rule"
        self.rules = [{"type": "Domain", "payload": config["service_host"], "proxy": config["outer_group"]}]
        self.nodes = {config["outer_group"]: {"type": "Selector", "now": config["group"],
                                             "all": [config["group"], MANUAL_GROUP]},
                      config["group"]: {"type": "Selector", "now": routes[0].name,
                                        "all": [r.name for r in routes] + [config["offline_proxy"]]},
                      MANUAL_GROUP: {"type": "Selector", "now": "America B1", "all": ["America B1"]},
                      "America B1": {"type": "AnyTLS"},
                      "GLOBAL": {"type": "Selector", "now": "America B1", "all": ["America B1"]}}
        self.calls = []
        self.on_refresh = None

    def get(self, path):
        from urllib.parse import unquote
        if path == "/configs":
            return {"mode": self.mode}
        if path == "/rules":
            return {"rules": copy.deepcopy(self.rules)}
        name = unquote(path.removeprefix("/proxies/"))
        if name not in self.nodes:
            raise ControllerError("GET", path, 404)
        return copy.deepcopy(self.nodes[name])

    def refresh(self, provider):
        self.calls.append(("refresh", provider))
        if self.on_refresh:
            self.on_refresh()

    def select(self, group, name):
        if group != self.config["group"]:
            raise AssertionError("guard attempted to overwrite the user selector")
        self.calls.append(("select", group, name))
        self.nodes[group]["now"] = name


class IndependentManualRoutingTests(unittest.TestCase):
    def setUp(self):
        self.config, self.routes = fixtures()
        self.config.update(outer_group="Transit-Auto-Select", fallback={"pool": "Tokyo"},
                           publish={"port": 17891, "token": "fixture-token-for-profile-only"})
        self.engine = Engine(self.config, self.routes)
        self.engine.current = self.routes[0].id
        for item in self.routes:
            self.engine.record(item.id, ProbeResult("accessible"), 1000)
            self.engine.record(item.id, ProbeResult("healthy", deep=True), 1000.1)
        self.controller = RoutingController(self.config, self.routes)
        self.publisher = Publisher()
        self.director = Director(self.config, self.engine, self.controller, self.publisher)

    def isolate_all(self):
        for item in self.routes:
            self.engine.record(item.id, ProbeResult("blocked"), 1001)

    def test_full_quarantine_never_removes_or_overrides_manual_choice(self):
        self.controller.nodes[self.config["outer_group"]]["now"] = MANUAL_GROUP
        before = copy.deepcopy(self.controller.nodes[MANUAL_GROUP])
        self.isolate_all()
        for now in (1002, 1003, 1004):
            phase, _ = self.director.sync(now)
            self.assertEqual(phase, "manual")
            self.assertFalse(self.director.effective_route["managed"])
            self.assertEqual(self.controller.nodes[MANUAL_GROUP], before)
            self.assertEqual(self.controller.nodes[self.config["outer_group"]]["now"], MANUAL_GROUP)
        self.assertEqual(self.controller.nodes[self.config["group"]]["now"], "Offline")
        self.assertEqual(self.publisher.routes, [])

    def test_global_emergency_profile_without_guard_group_is_manual(self):
        self.controller.mode = "global"
        del self.controller.nodes[self.config["group"]]
        self.isolate_all()
        phase, _ = self.director.sync(1002, observer_error="shadow observer unavailable")
        self.assertEqual(phase, "manual")
        self.assertEqual(self.director.effective_route["selection"], "America B1")
        self.assertEqual(self.controller.calls, [])

    def test_unrelated_rules_cannot_claim_outage_or_edit_same_named_group(self):
        self.controller.rules.insert(0, {"type": "ProcessName", "payload": "codex", "proxy": "America B1"})
        self.isolate_all()
        self.assertEqual(self.director.sync(1002)[0], "inactive")
        self.assertFalse(self.director.effective_route["managed"])
        self.assertEqual(self.controller.calls, [])

    def test_observer_fault_cannot_publish_or_select_offline(self):
        self.publisher.routes = [self.routes[0]]
        self.isolate_all()
        phase, _ = self.director.sync(1002, observer_error="ENOSPC")
        self.assertEqual(phase, "observer_fault")
        self.assertEqual(self.publisher.routes, [self.routes[0]])
        self.assertEqual(self.controller.calls, [])

    def test_user_override_during_refresh_is_reflected_before_status(self):
        self.isolate_all()
        self.controller.on_refresh = lambda: self.controller.nodes[self.config["outer_group"]].update(now=MANUAL_GROUP)
        self.assertEqual(self.director.sync(1002)[0], "manual")
        self.assertFalse(self.director.effective_route["managed"])

    def test_failed_automatic_refresh_does_not_hide_effective_manual_route(self):
        self.controller.nodes[self.config["outer_group"]]["now"] = MANUAL_GROUP
        self.isolate_all()
        def fail():
            raise TimeoutError("publisher unavailable")
        self.controller.on_refresh = fail
        self.assertEqual(self.director.sync(1002)[0], "manual")
        self.assertIn("TimeoutError", self.director.reconciliation_error)
        self.assertFalse(self.director.effective_route["managed"])
        self.assertEqual(self.controller.nodes[MANUAL_GROUP]["now"], "America B1")

    def test_stale_evidence_keeps_current_route_without_offline(self):
        self.assertEqual(self.director.sync(10000)[0], "checking")
        self.assertIn(self.routes[0], self.publisher.routes)
        self.assertFalse(any(c[0] == "select" for c in self.controller.calls))

    def test_global_selector_can_explicitly_choose_automatic_route(self):
        self.controller.mode = "global"
        self.controller.nodes["GLOBAL"]["now"] = self.config["outer_group"]
        self.assertTrue(effective_service_route(self.controller, self.config)["managed"])

    def test_manual_catalog_is_inline_complete_and_independent_of_provider(self):
        profile = {"proxies": [{"name": "Existing extra", "type": "ss", "server": "127.0.0.1", "port": 9999}],
                   "proxy-groups": [{"name": self.config["outer_group"], "type": "select", "proxies": [],
                                     "use": [self.config["provider"]], "filter": "^AR/"}],
                   "proxy-providers": {}, "tun": {"enable": False},
                   "rules": ["DOMAIN,other.test,DIRECT", "DOMAIN,anyrouter.test,Transit-Auto-Select", "MATCH,DIRECT"]}
        before = copy.deepcopy(profile)
        result = build_profile(profile, self.config, self.routes, default_id=self.routes[2].id,
                               extra_manual=["Existing extra"])
        self.assertEqual(profile, before)
        groups = {g["name"]: g for g in result["proxy-groups"]}
        self.assertEqual(groups[MANUAL_GROUP]["proxies"][0], manual_name(self.routes[2]))
        self.assertNotIn("use", groups[MANUAL_GROUP])
        self.assertEqual(set(groups[MANUAL_GROUP]["proxies"]), {manual_name(r) for r in self.routes} | {"Existing extra"})
        self.assertEqual(groups[self.config["outer_group"]]["proxies"][:2], [MANUAL_GROUP, self.config["group"]])
        self.assertNotIn("use", groups[self.config["outer_group"]])
        self.assertNotIn("filter", groups[self.config["outer_group"]])
        result["proxy-providers"].clear()  # Detector/publisher unavailable.
        nodes = {p["name"]: p for p in json.loads(json.dumps(result))["proxies"]}
        for item in self.routes:
            self.assertEqual({**nodes[manual_name(item)], "name": item.name}, item.proxies[-1])
        self.assertEqual(result["tun"], before["tun"])
        self.assertEqual(result["rules"][0], "DOMAIN,anyrouter.test,Transit-Auto-Select")


if __name__ == "__main__":
    unittest.main()

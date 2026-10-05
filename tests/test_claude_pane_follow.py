import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import cmux_codex_watch as w
from tests.test_watch import FakeClient


class ClaudePaneFollowTests(unittest.TestCase):
    def setUp(self):
        self.seed = dict(surface_id="old", workspace_id="w", pane_id="p", enabled=True,
                         follow_agents=["codex", "claude"])
        self.config = {"targets": [self.seed], "workspace_rules": []}
        records = [("new", "p", "claude"), ("codex", "p", "codex"),
                   ("shell", "p", "zsh"), ("other", "q", "claude")]
        self.tree = {"windows": [{"workspaces": [{"id": "w", "ref": "workspace:1", "panes": [
            {"id": pane, "ref": "pane:1", "surfaces": [
                {"id": sid, "ref": "surface:"+str(i+1), "type": "terminal", "title": agent}
                for i, (sid, p, agent) in enumerate(records) if p == pane]}
            for pane in ("p", "q")]}]}]}
        self.top = {"windows": [{"workspaces": [{"surfaces": [
            {"kind": "surface", "id": sid, "ref": "surface:"+str(i+1), "processes": [
                {"kind": "process", "name": agent, "path": "/usr/bin/"+agent}]}
            for i, (sid, pane, agent) in enumerate(records)]}]}]}
        self.client = FakeClient({}, tree=self.tree, top=self.top)
        self.target = dict(surface_id="new", workspace_id="w", pane_id="p", source="pane_follow",
                           follow_agent="claude", source_workspace_id="w", enabled=True, paused=False)

    def ids(self):
        return {r["surface_id"] for r in w.discover_pane_follow_targets(self.client, self.config)}

    def test_opt_in_discovers_new_claude_and_existing_codex(self):
        self.assertEqual(self.ids(), {"new", "codex"})
        found = next(r for r in w.discover_pane_follow_targets(self.client, self.config) if r["surface_id"] == "new")
        self.assertEqual(found["follow_agent"], "claude")

    def test_no_opt_in_preserves_codex_only(self):
        self.seed.pop("follow_agents")
        self.assertEqual(self.ids(), {"codex"})

    def test_paused_disabled_and_wrong_workspace_seed(self):
        for field, value in (("paused", True), ("enabled", False), ("workspace_id", "elsewhere")):
            with self.subTest(field=field):
                old = copy.deepcopy(self.seed)
                self.seed[field] = value
                self.assertEqual(self.ids(), set())
                self.seed.clear(); self.seed.update(old)

    def test_excluded_and_explicit_paused_child_not_sent(self):
        self.config["workspace_rules"] = [{"workspace_id": "w", "excluded_surface_ids": ["new"]}]
        self.assertEqual(self.ids(), {"codex"})
        self.config["workspace_rules"] = []
        self.config["targets"].append({**self.target, "paused": True})
        actual = next(r for r in w.effective_targets(self.config, [self.target]) if r["surface_id"] == "new")
        self.assertTrue(actual["paused"])

    def test_workspace_pause_remains_effective(self):
        self.config["workspace_rules"] = [{"workspace_id": "w", "paused": True}]
        actual = w.effective_targets(self.config, [self.target])[0:]
        self.assertTrue(all(t["paused"] for t in actual))

    def test_publication_rechecks_agent_opt_in(self):
        self.assertTrue(w.dynamic_target_authorized(self.config, self.target))
        self.seed["follow_agents"] = ["codex"]
        self.assertFalse(w.dynamic_target_authorized(self.config, self.target))

    def test_policy_generation_changes_on_opt_in_revocation(self):
        before = w.ObservationPolicy(w.validate_config(self.config)).key(self.target)
        self.seed["follow_agents"] = ["codex"]
        self.assertNotEqual(before, w.ObservationPolicy(w.validate_config(self.config)).key(self.target))

    def test_unknown_agent_cannot_inherit(self):
        self.assertFalse(w.dynamic_target_authorized(self.config, {**self.target, "follow_agent": "unknown"}))

    def test_bad_follow_agents_rejected(self):
        for value in (True, "claude", ["unknown"], [1], None):
            with self.subTest(value=value):
                self.seed["follow_agents"] = value
                with self.assertRaises(RuntimeError): w.validate_config(self.config)

    def test_send_gate_rechecks_revoked_agent_opt_in(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"config.json"
            w.atomic_write_json(path, w.validate_config(self.config))
            daemon = w.WatchDaemon(path, Path(td)/"state.json", client=self.client)
            daemon.dynamic_targets = {"new": self.target}
            self.assertIsNotNone(daemon._active_send_target(self.target))
            daemon.config_store.mutate(lambda c: c["targets"][0].update(follow_agents=["codex"]))
            self.assertIsNone(daemon._active_send_target(self.target))

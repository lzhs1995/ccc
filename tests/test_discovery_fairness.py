"""Batch bookkeeping must not starve other authorized terminals."""
import copy
import tempfile
import unittest

import cmux_codex_watch as core
from ccc_scheduling import SnapshotCache, SnapshotClient, SurfaceScheduler
from tests.test_watch import FakeClient, armed_daemon, discovery_fixture, grid_payload, visible_lines


class DiscoveryFairnessTests(unittest.TestCase):
    def daemon(self, directory):
        frame = grid_payload([])
        client = FakeClient(frame, "\n".join(visible_lines(frame)))
        daemon = armed_daemon(directory, client)
        self.addCleanup(daemon._process_snapshots.close)
        return daemon, client

    def test_config_churn_does_not_reset_the_waiting_readers_position(self):
        now, seen = [100.0], []
        scheduler = SurfaceScheduler(
            lambda target, current: seen.append(target["surface_id"]),
            lambda *_: self.fail("observation must not send"),
            observe_workers=1, clock=lambda: now[0])
        targets = [{"surface_id": str(i), "workspace_id": "w"} for i in range(12)]
        try:
            for generation in range(1, 14):
                scheduler.wakeup.clear()
                scheduler.tick(targets, generation=generation)
                self.assertTrue(scheduler.wakeup.wait(2))
                now[0] += .25
            self.assertEqual(set(seen[:12]), {str(i) for i in range(12)})
        finally:
            scheduler.close()

    def test_fleet_pause_merge_scans_rules_once_and_preserves_explicit_precedence(self):
        class Rules(list):
            scans = 0
            def __iter__(self):
                self.scans += 1
                return super().__iter__()
        rules = Rules([{'workspace_id': f'w-{i}', 'paused': i == 4} for i in range(318)])
        targets = [{'surface_id': str(i), 'workspace_id': f'w-{i % 318}',
                    'ref': f'surface:{i}', 'enabled': True} for i in range(843)]
        explicit = {**targets[0], 'enabled': False}
        merged = core.effective_targets({'targets': [explicit], 'workspace_rules': rules}, targets)
        self.assertEqual(rules.scans, 1)
        self.assertEqual(len(merged), 843)
        self.assertFalse(merged[0]['enabled'])
        self.assertEqual({r['surface_id'] for r in merged if r.get('paused')}, {'4', '322', '640'})
        self.assertFalse(any(t.get('paused') for t in targets))

    def test_unrelated_registration_during_read_keeps_the_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.daemon(directory)
            target = dict(daemon.config["targets"][0])
            original = client.read_screen

            def read(*args):
                daemon._mutate_config(lambda config: config["workspace_rules"].append({
                    "workspace_id": "other-workspace", "enabled": True,
                    "excluded_surface_ids": [], "active_batch_id": "another-batch"}))
                return original(*args)

            client.read_screen = read
            daemon._scheduled_observe(target, lambda: True)
            self.assertEqual(daemon.runtime[target["surface_id"]].observed_state, "idle")
            self.assertEqual(client.sent, [])

    def test_other_batch_slot_hold_does_not_cancel_this_surface_read(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.daemon(directory)
            target = dict(daemon.config["targets"][0])
            daemon._mutate_config(lambda config: config["workspace_rules"].append({
                "workspace_id": target["workspace_id"], "enabled": True,
                "excluded_surface_ids": [], "batch_start_holds": {}}))
            original = client.read_screen

            def read(*args):
                daemon._mutate_config(lambda config: config["workspace_rules"][0]["batch_start_holds"].update({
                    "other-surface": {"job_id": "new-job", "created_at": 100}}))
                return original(*args)

            client.read_screen = read
            daemon._scheduled_observe(target, lambda: True)
            self.assertEqual(daemon.runtime[target["surface_id"]].observed_state, "idle")
            self.assertEqual(client.sent, [])

    def test_actual_pause_during_read_still_invalidates_the_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.daemon(directory)
            target = dict(daemon.config["targets"][0])
            original = client.read_screen

            def read(*args):
                daemon._mutate_config(lambda config: config["targets"][0].update(paused=True))
                return original(*args)

            client.read_screen = read
            daemon._scheduled_observe(target, lambda: True)
            self.assertFalse(daemon.runtime[target["surface_id"]].observed_at)
            self.assertEqual(client.sent, [])

    def prepare_discovery(self, directory):
        daemon, client = self.daemon(directory)
        tree, top = discovery_fixture()
        client.tree_data, client.top_data = tree, top
        daemon._mutate_config(lambda config: config.update(targets=[], workspace_rules=[{
            "workspace_id": "workspace-uuid", "enabled": True,
            "excluded_surface_ids": []}]))
        return daemon, client

    def test_batch_progress_during_discovery_does_not_discard_new_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.prepare_discovery(directory)
            original = client.top

            def top(workspace_id):
                daemon._mutate_config(lambda config: config["workspace_rules"][0].update(
                    active_batch_id="new-batch"))
                return original(workspace_id)

            client.top = top
            daemon._refresh_dynamic_targets(client, force=True)
            self.assertIn("codex-a", daemon.dynamic_targets)

    def test_exclusion_during_discovery_cannot_restore_the_excluded_target(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.prepare_discovery(directory)
            original = client.top

            def top(workspace_id):
                daemon._mutate_config(lambda config: config["workspace_rules"][0].update(
                    excluded_surface_ids=["codex-a"]))
                return original(workspace_id)

            client.top = top
            daemon._refresh_dynamic_targets(client, force=True)
            self.assertNotIn("codex-a", daemon.dynamic_targets)

    def test_partial_process_enumeration_keeps_existing_observation_and_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.prepare_discovery(directory)
            daemon._refresh_dynamic_targets(client, force=True)
            runtime = daemon.runtime["codex-a"] = core.TargetRuntime(codex_sent_turn_key="original-turn")
            client.top_data = {"windows": [], "sample": {"enumeration_complete": False}}
            daemon._refresh_dynamic_targets(client, force=True)
            self.assertIn("codex-a", daemon.dynamic_targets)
            self.assertIs(daemon.runtime.get("codex-a"), runtime)
            self.assertEqual(runtime.codex_sent_turn_key, "original-turn")
            # A confirmed terminal closure still removes it, even while the
            # independent process enumeration remains partial.
            client.tree_data = {"windows": []}
            daemon._refresh_dynamic_targets(client, force=True)
            self.assertNotIn("codex-a", daemon.dynamic_targets)

    def test_partial_enumeration_neither_enrolls_unknowns_nor_ignores_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.prepare_discovery(directory)
            daemon._refresh_dynamic_targets(client, force=True)
            client.top_data = {"windows": [], "sample": {"enumeration_complete": False}}
            daemon._mutate_config(lambda config: config["workspace_rules"][0].update(
                excluded_surface_ids=["codex-a"]))
            daemon._refresh_dynamic_targets(client, force=True)
            self.assertEqual(daemon.dynamic_targets, {})

    def test_changed_cmux_endpoint_rejects_old_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, client = self.prepare_discovery(directory)
            original = client.top

            def top(workspace_id):
                daemon._mutate_config(lambda config: config.update(cmux_path="/another/cmux"))
                return original(workspace_id)

            client.top = top
            daemon._refresh_dynamic_targets(client, force=True)
            self.assertEqual(daemon.dynamic_targets, {})

    def test_many_workspace_rules_share_one_collection_and_one_classification(self):
        tree, _ = discovery_fixture()
        # Construct distinct UUIDs/refs; scope must survive many rules and a
        # collector slower than the normal cache TTL.
        template = tree["windows"][0]["workspaces"][0]
        workspaces, tops, rules = [], [], []
        for i in range(100):
            wid, sid = f"w-{i}", f"s-{i}"
            workspace = copy.deepcopy(template)
            workspace.update(id=wid, ref=f"workspace:{i}")
            workspace["panes"][0]["surfaces"] = [{"id": sid, "ref": f"surface:{i}", "type": "terminal"}]
            workspaces.append(workspace)
            tops.append({"id": wid, "surfaces": [{"kind": "surface", "id": sid,
                "ref": f"surface:{i}", "processes": [{"kind": "process", "name": "codex", "pid": 1000 + i}]}]})
            rules.append({"workspace_id": wid, "enabled": True})
        now, calls = [100.0], []

        class Client:
            def tree(self):
                return {"windows": [{"workspaces": workspaces}]}

            def top_all(self):
                calls.append(1)
                now[0] += 6
                return {"windows": [{"workspaces": tops}], "sample": {"enumeration_complete": False}}

        cache = SnapshotCache(clock=lambda: now[0])
        try:
            client = SnapshotClient(Client(), cache)
            found = core.discover_rule_targets(client, {"workspace_rules": rules})
            self.assertEqual({r["surface_id"] for r in found}, {f"s-{i}" for i in range(100)})
            self.assertEqual(calls, [1])
            self.assertFalse(client.top("w-0")["sample"]["enumeration_complete"])
        finally:
            cache.close()


if __name__ == "__main__":
    unittest.main()

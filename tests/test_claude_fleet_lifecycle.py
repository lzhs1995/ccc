import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import cmux_codex_watch as w
from tests.test_claude_fleet_discovery import client_for as discovery_client

def client_for(rows):
    client = discovery_client(rows)
    # CLI find_surface consumes controller refs, unlike the discovery-only fixture.
    for i, ws in enumerate(client.tree_data["windows"][0]["workspaces"]):
        ws["ref"] = f"workspace:{i+1}"
    return client

class FleetLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/"config.json"
        self.config = w.validate_config(dict(mode="armed", claude_enabled=True, claude_auto_discover=True))
        w.atomic_write_json(self.path, self.config)
        self.client = client_for([("w1","p1","c1","claude"),("w2","p2","c2","claude"),
                                  ("w2","p3","x","codex"),("w2","p4","s","zsh")])
        self.store = w.ConfigStore(self.path)

    def cli(self, *args):
        out=io.StringIO()
        with mock.patch.object(w,"CmuxClient",return_value=self.client), contextlib.redirect_stdout(out):
            self.assertEqual(w.cli(["--config",str(self.path),*args]),0)
        return json.loads(out.getvalue())

    def daemon(self):
        return w.WatchDaemon(self.path,self.path.parent/"state.json",client=self.client)

    def found(self):
        config=self.store.load()
        return {t["surface_id"]:t for t in w.effective_targets(config,
            w.discover_claude_targets(self.client, config))}

    def test_startup_enable_race_reloads_new_policy(self):
        self.store.mutate(lambda c:c.update(claude_auto_discover=False))
        original=w.WatchDaemon._load_runtime
        def race(d):
            runtime=original(d)
            d.config_store.mutate(lambda c:c.update(claude_auto_discover=True))
            return runtime
        with mock.patch.object(w.WatchDaemon,"_load_runtime",race):
            d=self.daemon()
        d._reload_config_if_changed()
        d._refresh_dynamic_targets(self.client,force=True)
        self.assertEqual(set(d.dynamic_targets),{"c1","c2"})

    def test_startup_revoke_race_rejects_send(self):
        original=w.WatchDaemon._load_runtime
        def race(d):
            runtime=original(d)
            d.config_store.mutate(lambda c:c.update(claude_auto_discover=False))
            return runtime
        with mock.patch.object(w.WatchDaemon,"_load_runtime",race):
            d=self.daemon()
        d._refresh_dynamic_targets(self.client,force=True)
        t=dict(surface_id="c1",workspace_id="w1")
        self.assertIsNone(d._active_send_target(t))

    def test_runtime_reports_loaded_fleet_flag(self):
        d=self.daemon();d._write_daemon_runtime()
        metadata=json.loads(d.daemon_runtime_path.read_bytes())
        self.assertTrue(metadata["claude_auto_discover"])
        self.assertEqual(metadata["loaded_config_mtime_ns"], d._config_mtime_ns)

    def test_effective_cli_includes_global_claude(self):
        self.assertEqual({t["surface_id"] for t in self.cli("effective")},{"c1","c2"})

    def test_track_accepts_claude_without_unsafe_waiver(self):
        self.cli("track-surface","c1")
        self.assertEqual(w.target_by_id(self.store.load(),"c1")["surface_id"],"c1")

    def test_track_still_rejects_shell(self):
        with self.assertRaises(RuntimeError):self.cli("track-surface","s")

    def test_pause_resume_dynamic_across_restart(self):
        self.cli("pause","surface:1")
        d=self.daemon();d._refresh_dynamic_targets(self.client,force=True)
        t=self.found()["c1"]
        self.assertTrue(t["paused"]);self.assertIsNone(d._active_send_target(t))
        self.cli("resume","surface:1")
        d._reload_config_if_changed()
        self.assertIsNotNone(d._active_send_target(t))
        self.assertEqual(self.store.load()["targets"][0].get("follow_agents"),[])

    def test_saved_dynamic_pause_never_becomes_unconditional_grant(self):
        self.cli("pause","c1");self.cli("resume","c1")
        self.store.mutate(lambda c:c.update(claude_auto_discover=False))
        d=self.daemon();self.assertIsNone(d._active_send_target(self.found()["c1"]))

    def test_remove_dynamic_survives_rediscovery_restart(self):
        self.cli("remove","c1")
        d=self.daemon();d._refresh_dynamic_targets(self.client,force=True)
        self.assertNotIn("c1",d.dynamic_targets);self.assertIn("c2",d.dynamic_targets)

    def test_untrack_without_workspace_rule(self):
        result=self.cli("untrack-surface","c1")
        self.assertEqual(result["surface_id"],"c1");self.assertNotIn("c1",self.found())

    def test_remove_explicit_cannot_reappear_as_auto(self):
        self.cli("add","c1");self.cli("remove","c1")
        self.assertNotIn("c1",self.found())

    def test_include_reauthorizes_only_requested_uuid(self):
        self.cli("exclude","c1");self.cli("exclude","c2")
        self.assertEqual(self.found(),{})
        self.cli("include","c1");self.assertEqual(set(self.found()),{"c1"})

    def test_track_clears_optout_without_touching_other_surfaces(self):
        self.cli("untrack-surface","c1");self.cli("untrack-surface","c2")
        self.cli("track-surface","c1")
        self.assertEqual(set(self.found()),{"c1"})

    def test_optout_survives_move(self):
        self.cli("untrack-surface","c1")
        self.client=client_for([("w3","p9","c1","claude")])
        self.assertNotIn("c1",self.found())

    def test_stale_send_candidate_respects_untrack(self):
        d=self.daemon();d._refresh_dynamic_targets(self.client,force=True)
        t=d.dynamic_targets["c1"]
        self.cli("untrack-surface","c1")
        self.assertIsNone(d._active_send_target(t))

    def test_exclude_cannot_be_bypassed_by_explicit(self):
        self.cli("add","c1");self.cli("exclude","c1")
        d=self.daemon();self.assertIsNone(d._active_send_target(w.target_by_id(d.config,"c1")))

    def test_optout_lists_validated(self):
        for key in ("discovery_excluded_surface_ids","claude_excluded_workspace_ids"):
            for bad in ("c1",{},[42],[""]):
                with self.subTest(key=key,bad=bad),self.assertRaises(RuntimeError):
                    w.validate_config({**self.config,key:bad})

    def test_authorization_fingerprint_includes_fleet_gates(self):
        before=w.monitoring_config_key(self.config)
        for key,val in (("claude_auto_discover",False),("claude_enabled",False),
                        ("discovery_excluded_surface_ids",["c1"]),
                        ("claude_excluded_workspace_ids",["w1"])):
            self.assertNotEqual(before,w.monitoring_config_key({**self.config,key:val}))

    def test_workspace_untrack_cannot_enable_global_fallback(self):
        self.store.mutate(lambda c:c["workspace_rules"].append(dict(workspace_id="w1",enabled=True)))
        self.cli("untrack-workspace","w1")
        self.assertNotIn("c1",self.found())

    def test_health_exposes_omitted_live_claude(self):
        records=w.main_surface_records(self.client.tree())
        labels=w.classify_surface_processes(self.client.top("w1"))
        report=w.claude_hook_coverage_from_inventory({},records,labels,{},lambda pid:{})
        self.assertEqual(report["unregistered"],["surface:1","surface:2"])
        self.assertEqual(report["status"],"degraded")

    def test_reauthorize_workspace_keeps_pause_and_surface_exclusions(self):
        import ccc_workspace_batch as batch
        self.store.mutate(lambda c:c.update(claude_excluded_workspace_ids=["w1","w2"],
            discovery_excluded_surface_ids=["c1"],
            workspace_rules=[dict(workspace_id="w1",paused=True,excluded_surface_ids=["c1"])]))
        batch.authorize_workspace(self.path,"w1",client=self.client)
        c=self.store.load()
        self.assertEqual(c["claude_excluded_workspace_ids"],["w2"])
        self.assertTrue(c["workspace_rules"][0]["paused"])
        self.assertEqual(c["discovery_excluded_surface_ids"],["c1"])
        self.assertEqual(w.discover_all_targets(self.client,c),[])

    def test_excluded_seed_cannot_authorize_sibling(self):
        self.client=client_for([("w1","p1","c1","claude"),("w1","p1","c2","claude")])
        self.store.mutate(lambda c:c.update(claude_auto_discover=False,
            targets=[dict(surface_id="c1",workspace_id="w1",pane_id="p1",follow_agents=["claude"])],
            discovery_excluded_surface_ids=["c1"]))
        self.assertEqual(w.discover_all_targets(self.client,self.store.load()),[])

    def test_health_distinguishes_optout_from_omission(self):
        self.store.mutate(lambda c:c.update(discovery_excluded_surface_ids=["c1"],claude_excluded_workspace_ids=["w2"]))
        report=w.claude_hook_coverage_from_inventory({},w.main_surface_records(self.client.tree()),
            w.classify_surface_processes(self.client.top("w1")),{},lambda pid:{},self.store.load())
        self.assertEqual(report["unregistered"],[])
        self.assertEqual(set(report["excluded_live"]),{"surface:1","surface:2"})

    def test_panel_uses_same_inventory_without_extra_rpc(self):
        import cmux_supervisor_tui as tui
        from tests.test_supervisor import _DiscoveryClient, _StubJanitor
        client=_DiscoveryClient()
        client.tree=mock.Mock(wraps=client.tree)
        client.top_all=mock.Mock(wraps=client.top_all)
        self.store.mutate(lambda c:c["workspace_rules"].append(dict(workspace_id="workspace-11")))
        stub=_StubJanitor()
        model=tui.SupervisorModel(self.path,client=client,janitor=stub,stack=stub,collab=stub,
            sessions=mock.Mock(snapshot=lambda:{}))
        with mock.patch.object(model.network,"snapshot",return_value={}), \
             mock.patch.object(w.ClaudeHookSettingsManager,"inspect",return_value={}):
            model.refresh(force=True)
        self.assertTrue(model.online,model.error)
        client.tree.assert_called_once();client.top_all.assert_called_once()
        row=next(r for r in model.candidates if r.agent_kind=="claude")
        self.assertEqual(row.source,"claude_auto")
        self.assertNotEqual(tui.watch_kind(row),"untracked")
        self.assertFalse(tui.is_idling(row))
        self.assertIn("p 暂停",tui.selected_action_hint(row))
        calls=[];model.run_cli=lambda args:calls.append(args)
        for action in ("pause","resume","remove","untrack_workspace"):
            model.mutate_selected(row,action)
        self.assertEqual([r[0] for r in calls],["pause","resume","remove","untrack-workspace"])

    def test_panel_dynamic_pause_and_optout_visible(self):
        import cmux_supervisor_tui as tui
        from tests.test_supervisor import _DiscoveryClient, _StubJanitor
        client=_DiscoveryClient();stub=_StubJanitor()
        model=tui.SupervisorModel(self.path,client=client,janitor=stub,stack=stub,collab=stub,
            sessions=mock.Mock(snapshot=lambda:{}))
        self.store.mutate(lambda c:c.update(discovery_excluded_surface_ids=["surface-60"]))
        with mock.patch.object(model.network,"snapshot",return_value={}), \
             mock.patch.object(w.ClaudeHookSettingsManager,"inspect",return_value={}):
            model.refresh(force=True)
        row=next(r for r in model.candidates if r.agent_kind=="claude")
        self.assertEqual(row.source,"workspace_excluded");self.assertTrue(row.paused)
        self.store.mutate(lambda c:c.update(discovery_excluded_surface_ids=[],targets=[dict(
            surface_id="surface-60",workspace_id="workspace-11",source_workspace_id="workspace-11",
            source="claude_auto",follow_agent="claude",paused=True,follow_agents=[])]))
        with mock.patch.object(model.network,"snapshot",return_value={}), \
             mock.patch.object(w.ClaudeHookSettingsManager,"inspect",return_value={}):
            model.refresh(force=True)
        row=next(r for r in model.candidates if r.agent_kind=="claude")
        self.assertEqual(row.source,"claude_auto");self.assertEqual(tui.watch_kind(row),"paused")

    def test_panel_add_claude_needs_no_non_codex_override(self):
        import cmux_supervisor_tui as tui
        model=object.__new__(tui.SupervisorModel);calls=[];model.run_cli=lambda args:calls.append(args)
        row=tui.Candidate(dict(surface_id="c1",workspace_id="w1",ref="surface:1",workspace_ref="workspace:1"),
            "untracked","unknown","",0,False,agent_kind="claude")
        self.assertNotIn("不是 Codex",tui.confirm_prompt("add",row))
        model.mutate_selected(row,"add")
        self.assertNotIn("--allow-non-codex",calls[0])

    def test_effective_state_respects_exclusion_and_revoked_dynamic_grant(self):
        self.cli("track-surface","c1")
        self.cli("exclude","c1")
        c=self.store.load()
        self.assertTrue(next(t for t in w.effective_targets(c,[]) if t["surface_id"]=="c1")["paused"])
        self.cli("pause","c2");self.cli("resume","c2")
        self.store.mutate(lambda c:c.update(claude_auto_discover=False))
        self.assertTrue(next(t for t in w.effective_targets(self.store.load(),[]) if t["surface_id"]=="c2")["paused"])

    def test_panel_workspace_optout_keeps_separate_explicit_grant(self):
        import cmux_supervisor_tui as tui
        from tests.test_supervisor import _DiscoveryClient, _StubJanitor
        stub=_StubJanitor()
        model=tui.SupervisorModel(self.path,client=_DiscoveryClient(),janitor=stub,stack=stub,collab=stub,
            sessions=mock.Mock(snapshot=lambda:{}))
        self.store.mutate(lambda c:c.update(claude_excluded_workspace_ids=["workspace-11"]))
        with mock.patch.object(model.network,"snapshot",return_value={}), \
             mock.patch.object(w.ClaudeHookSettingsManager,"inspect",return_value={}):
            model.refresh(force=True)
        row=next(r for r in model.candidates if r.agent_kind=="claude")
        self.assertTrue(row.paused)
        self.assertIn("w 重新授权",tui.selected_action_hint(row))
        with self.assertRaisesRegex(RuntimeError,"单路恢复不会解除"):
            model.mutate_selected(row,"resume")
        self.store.mutate(lambda c:c.update(targets=[dict(surface_id="surface-60",workspace_id="workspace-11")]))
        with mock.patch.object(model.network,"snapshot",return_value={}), \
             mock.patch.object(w.ClaudeHookSettingsManager,"inspect",return_value={}):
            model.refresh(force=True)
        row=next(r for r in model.candidates if r.agent_kind=="claude")
        self.assertFalse(row.paused);self.assertEqual(row.source,"explicit")

if __name__=="__main__":unittest.main()

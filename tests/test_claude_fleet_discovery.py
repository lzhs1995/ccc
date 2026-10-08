import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import cmux_codex_watch as w
from tests.test_watch import FakeClient


def client_for(rows):
    workspaces = []
    tops = []
    for wid in dict.fromkeys(r[0] for r in rows):
        panes = []
        for pane in dict.fromkeys(r[1] for r in rows if r[0] == wid):
            panes.append(dict(id=pane, surfaces=[dict(id=sid, ref=f"surface:{i+1}", type="terminal")
                for i, (ww, pp, sid, agent) in enumerate(rows) if ww == wid and pp == pane]))
        workspaces.append(dict(id=wid, panes=panes))
        tops.append(dict(id=wid, surfaces=[dict(kind="surface", id=sid, ref=f"surface:{i+1}",
            processes=[dict(kind="process", name=agent, path="/usr/bin/"+agent)])
            for i, (ww, pp, sid, agent) in enumerate(rows) if ww == wid]))
    return FakeClient({}, tree={"windows": [{"workspaces": workspaces}]},
                      top={"windows": [{"workspaces": tops}]})


class FleetDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.rows = [("w1", "p1", "c1", "claude"), ("w1", "p1", "c2", "claude"),
                     ("w1", "p2", "c3", "claude"), ("w2", "p3", "c4", "claude"),
                     ("w2", "p3", "codex", "codex"), ("w2", "p3", "shell", "zsh")]
        self.client = client_for(self.rows)
        self.config = w.validate_config(dict(claude_auto_discover=True, claude_enabled=True,
                                             mode="armed", targets=[], workspace_rules=[]))

    def targets(self):
        return w.discover_claude_targets(self.client, self.config)

    def test_every_workspace_pane_and_tab_without_seed(self):
        self.assertEqual({t['surface_id'] for t in self.targets()}, {'c1','c2','c3','c4'})

    def test_legacy_default_and_independent_gates(self):
        self.assertFalse(w.default_config()['claude_auto_discover'])
        for flag in ('claude_enabled', 'claude_auto_discover'):
            config = {**self.config, flag: False}
            self.assertEqual(w.discover_claude_targets(self.client, config), [])

    def test_flag_is_strict_boolean(self):
        for val in (1, 'true', None, []):
            with self.subTest(val=val), self.assertRaises(RuntimeError):
                w.validate_config({**self.config, 'claude_auto_discover': val})

    def test_explicit_paused_or_disabled_always_wins(self):
        found = self.targets()
        self.config['targets'] = [dict(found[0], paused=True), dict(found[1], enabled=False)]
        self.assertEqual({t['surface_id'] for t in self.targets()}, {'c3','c4'})
        combined = {t['surface_id']: t for t in w.effective_targets(self.config, found)}
        self.assertTrue(combined['c1']['paused'])
        self.assertFalse(combined['c2']['enabled'])

    def test_workspace_paused_disabled_exclusion_hold_and_manager(self):
        for rule in ({'paused': True}, {'enabled': False}, {'excluded_surface_ids':['c1','c2','c3']},
                     {'batch_start_holds':{s:{'job_id':'j'} for s in ('c1','c2','c3')}}):
            with self.subTest(rule=rule):
                self.config['workspace_rules'] = [dict(workspace_id='w1', **rule)]
                self.assertEqual({t['surface_id'] for t in self.targets()}, {'c4'})
        self.config['workspace_rules'] = []
        self.config['manager_surface_id'] = 'c1'
        self.assertNotIn('c1', {t['surface_id'] for t in self.targets()})

    def test_old_workspace_rule_stays_codex_only(self):
        self.config['workspace_rules'] = [dict(workspace_id='w2', enabled=True)]
        self.assertEqual({t['surface_id'] for t in w.discover_rule_targets(self.client,self.config)}, {'codex'})

    def test_new_tab_and_workspace_follow_next_pass(self):
        self.client = client_for(self.rows + [('w3','p8','new','claude')])
        self.assertIn('new', {t['surface_id'] for t in self.targets()})

    def test_scope_revoked_and_wrong_agent_workspace_denied(self):
        target = self.targets()[0]
        for bad in ({'follow_agent':'codex'}, {'source_workspace_id':'elsewhere'}):
            self.assertFalse(w.dynamic_target_authorized(self.config, {**target, **bad}))
        self.config['claude_auto_discover'] = False
        self.assertFalse(w.dynamic_target_authorized(self.config, target))

    def test_policy_changes_on_revocation(self):
        t = self.targets()[0]
        before = w.ObservationPolicy(self.config).key(t)
        self.config['claude_auto_discover'] = False
        self.assertNotEqual(before, w.ObservationPolicy(self.config).key(t))

    def daemon(self, root):
        path = Path(root)/'config.json'
        w.atomic_write_json(path, self.config)
        return w.WatchDaemon(path, Path(root)/'state.json', client=self.client)

    def test_live_send_gate_rechecks_revocation(self):
        with tempfile.TemporaryDirectory() as root:
            d = self.daemon(root)
            d._refresh_dynamic_targets(self.client, force=True)
            t = d.dynamic_targets['c1']
            self.assertIsNotNone(d._active_send_target(t))
            d.config_store.mutate(lambda c: c.update(claude_auto_discover=False))
            self.assertIsNone(d._active_send_target(t))

    def test_first_discovery_reconciles_registration_once(self):
        with tempfile.TemporaryDirectory() as root:
            d = self.daemon(root)
            d._refresh_dynamic_targets(self.client, force=True)
            self.assertEqual(set(d._registration_due), {'c1','c2','c3','c4'})
            d._registration_due.clear()
            d._refresh_dynamic_targets(self.client, force=True)
            self.assertEqual(d._registration_due, {})

    def test_rediscovery_and_restart_preserve_delivery_history(self):
        with tempfile.TemporaryDirectory() as root:
            d = self.daemon(root)
            d._refresh_dynamic_targets(self.client, force=True)
            r = d.runtime.setdefault('c1', w.TargetRuntime())
            r.send_count = 3
            r.claude_submit_phase = 'enter_sent'
            r.claude_submit_event_id = 'original-event'
            r.claude_completed_latched = True
            d._refresh_dynamic_targets(client_for([]), force=True)
            self.assertIs(d.runtime['c1'], r)
            d._refresh_dynamic_targets(self.client, force=True)
            d.save()
            restarted = w.WatchDaemon(d.config_path, Path(root)/'state.json', client=self.client)
            restarted._refresh_dynamic_targets(self.client, force=True)
            self.assertEqual(restarted.runtime['c1'].send_count, 3)
            self.assertEqual(restarted.runtime['c1'].claude_submit_event_id, 'original-event')
            self.assertTrue(restarted.runtime['c1'].claude_completed_latched)

    def test_moved_tab_needs_new_discovery_and_new_workspace_gate(self):
        with tempfile.TemporaryDirectory() as root:
            d = self.daemon(root)
            d._refresh_dynamic_targets(self.client, force=True)
            old = copy.deepcopy(d.dynamic_targets['c1'])
            moved = client_for([('w3','p4','c1','claude')])
            self.assertFalse(d._refresh_workspace(old, moved))
            self.assertEqual(old['workspace_id'], 'w1')
            d._refresh_dynamic_targets(moved, force=True)
            self.assertEqual(d.dynamic_targets['c1']['workspace_id'], 'w3')
            self.assertIsNone(d._active_send_target(old))

    def test_codex_replacement_cannot_use_claude_scope(self):
        with tempfile.TemporaryDirectory() as root:
            d = self.daemon(root)
            t = self.targets()[0]
            self.assertFalse(d._codex_turn_ready(t, w.TargetRuntime(),
                w.ScreenState('codex_error', message_kind='codex')))
            d._refresh_dynamic_targets(client_for([('w1','p1','c1','codex')]), force=True)
            self.assertNotIn('c1', d.dynamic_targets)


    def test_pane_follow_cannot_bypass_workspace_denial(self):
        self.config['targets'] = [dict(self.targets()[0], follow_agents=['claude'])]
        for rule in ({'paused': True}, {'enabled': False}, {'batch_start_holds':{'c2':{'job_id':'j'}}}):
            with self.subTest(rule=rule):
                self.config['workspace_rules'] = [dict(workspace_id='w1', **rule)]
                found = w.discover_pane_follow_targets(self.client, self.config)
                self.assertFalse(any(t['surface_id']=='c2' and w.dynamic_target_authorized(self.config,t) for t in found))

    def test_automatic_isolation_persists_across_restart_and_can_resume(self):
        for method in ('_mark_target_incompatible', '_pause_missing_or_error'):
            with self.subTest(method=method), tempfile.TemporaryDirectory() as root:
                d = self.daemon(root)
                d._refresh_dynamic_targets(self.client, force=True)
                t = d.dynamic_targets['c1']
                with mock.patch.object(d, '_notify'):
                    getattr(d, method)(t, w.TargetRuntime(), 'incompatible original surface')
                d2 = w.WatchDaemon(d.config_path, d.state_path, client=self.client)
                d2._refresh_dynamic_targets(self.client, force=True)
                self.assertNotIn('c1', d2.dynamic_targets)
                paused = w.target_by_id(d2.config, 'c1')
                self.assertTrue(paused['paused'])
                self.assertTrue(paused['enabled'])  # ordinary resume clears pause
                self.assertIsNone(d2._active_send_target(t))

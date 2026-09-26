"""Slow GUI inventory cannot hide independently verified local Codex UUIDs."""
import copy
import threading
import unittest
from unittest.mock import patch

import ccc_guard_scope as scope
from ccc_native_processes import NativeProcessIndex, local_processes
from ccc_scheduling import SnapshotCache, SnapshotClient
import cmux_codex_watch as core
from tests.test_watch import discovery_fixture, FakeClient, armed_daemon, grid_payload, visible_lines
from tests import test_codex_process_session as process_fixtures


class NativeDiscoveryTests(unittest.TestCase):
    def row(self, sid="codex-a", wid="workspace-uuid", pid=101):
        return {"surface_id": sid, "environment_workspace_id": wid,
                "pid": pid, "birth": [100, 200], "remote": False, "backend": False}

    def index(self, rows):
        index = NativeProcessIndex(loader=lambda: rows, clock=lambda: 100)
        index.refresh()
        return index

    def test_process_index_expires_and_requires_exact_workspace_and_unique_process(self):
        now = [100.0]
        rows = [self.row(), self.row("ambiguous", pid=102), self.row("ambiguous", pid=103)]
        index = NativeProcessIndex(loader=lambda: rows, clock=lambda: now[0])
        self.assertIsNone(index.snapshot())
        index.refresh()
        target = {"surface_id": "codex-a", "workspace_id": "workspace-uuid"}
        self.assertEqual(index.lookup(target)["agent_pids"], [101])
        self.assertIsNone(index.lookup({**target, "workspace_id": "moved"}))
        self.assertIsNone(index.lookup({**target, "surface_id": "ambiguous"}))
        now[0] = 106
        self.assertIsNone(index.snapshot())
        self.assertIsNone(index.lookup(target))

    def test_slow_or_failed_refresh_does_not_block_readers_or_publish_fresh_ownership(self):
        entered, release = threading.Event(), threading.Event()
        now = [100.0]
        def load():
            entered.set()
            release.wait(2)
            return [self.row()]
        index = NativeProcessIndex(loader=load, clock=lambda: now[0])
        index.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertIsNone(index.snapshot())
            now[0] = 106
        finally:
            release.set()
            index.close()
        self.assertIsNone(index.snapshot())
        index.loader = lambda: (_ for _ in ()).throw(OSError("inventory unavailable"))
        index.refresh()
        self.assertIsNone(index.snapshot())

    def test_native_filter_rejects_remote_noninteractive_and_changed_birth(self):
        row = self.row()
        for argv in (["codex", "--remote", "ws://host"], ["codex", "--remote=ws://host"],
                     ["codex", "exec", "prompt"], ["codex", "app-server"], ["codex", "--version"]):
            with self.subTest(argv=argv), patch.object(scope, "scan", return_value=[row]), \
                    patch.object(scope, "arguments", return_value=(argv, {})), \
                    patch.object(scope, "birth", return_value=row["birth"]):
                self.assertEqual(local_processes(), [])
        with patch.object(scope, "scan", return_value=[row]), \
                patch.object(scope, "arguments", return_value=(["/native/codex", "resume", "session"], {})), \
                patch.object(scope, "birth", return_value=row["birth"]) as birth:
            self.assertEqual(local_processes(), [row])
            birth.return_value = [100, 201]
            self.assertEqual(local_processes(), [])

    def test_700_surfaces_discover_while_the_single_gui_snapshot_is_blocked(self):
        tree, _ = discovery_fixture()
        template = tree["windows"][0]["workspaces"][0]
        workspaces, rules, native = [], [], []
        for w in range(14):
            wid = f"w-{w}"
            workspace = copy.deepcopy(template)
            workspace.update(id=wid, ref=f"workspace:{w}")
            workspace["panes"][0]["surfaces"] = []
            for i in range(50):
                sid = f"s-{w}-{i}"
                workspace["panes"][0]["surfaces"].append({"id": sid, "ref": f"surface:{w*50+i}", "type": "terminal"})
                native.append(self.row(sid, wid, 1000 + w * 50 + i))
            workspaces.append(workspace)
            rules.append({"workspace_id": wid, "enabled": True, "excluded_surface_ids": [f"s-{w}-0"]})
        entered, release = threading.Event(), threading.Event()
        class Client:
            calls = 0
            def tree(self):
                return {"windows": [{"workspaces": workspaces}]}
            def top_all(self):
                self.calls += 1
                entered.set()
                release.wait(3)
                raise core.CmuxError("GUI timeout")
        index = self.index(native)
        cache, base = SnapshotCache(), Client()
        try:
            client = SnapshotClient(base, cache)
            snapshot = core.DiscoverySnapshot(client, index.snapshot())
            found = core.discover_rule_targets(snapshot, {"workspace_rules": rules})
            self.assertTrue(entered.wait(1))
            self.assertFalse(release.is_set())
            self.assertEqual({t["surface_id"] for t in found}, {r["surface_id"] for r in native if not r["surface_id"].endswith("-0")})
            self.assertTrue(all(snapshot.incomplete(rule["workspace_id"]) for rule in rules))
            self.assertEqual(base.calls, 1)
        finally:
            release.set()
            cache.close()

    def test_fresh_conflicting_gui_owner_and_wrong_workspace_are_not_overridden(self):
        tree, top = discovery_fixture()
        class Client:
            def tree(self):
                return tree
            def top_all(self):
                return top
        # A shell or Claude in the tree must not be enrolled from a stale hint.
        native = self.index([self.row("shell"), self.row("claude"), self.row("helper", "other")])
        snapshot = core.DiscoverySnapshot(Client(), native.snapshot())
        found = snapshot.codex_surfaces("workspace-uuid")
        self.assertEqual({t["surface_id"] for t in found}, {"codex-a"})


class NativeFirstBindingTests(unittest.TestCase):
    setUp = process_fixtures.ProcessSessionTests.setUp
    run_command = process_fixtures.ProcessSessionTests.run_command

    def test_first_original_binding_does_not_wait_for_gui_and_still_checks_placement(self):
        frame = grid_payload([])
        client = FakeClient(frame, '\n'.join(visible_lines(frame)))
        daemon = armed_daemon(self.root, client)
        self.addCleanup(daemon._process_snapshots.close)
        daemon._native_process_index = NativeProcessIndex(loader=lambda: [{
            'surface_id': 's', 'environment_workspace_id': 'w', 'pid': 123}], clock=lambda: 100)
        daemon._native_process_index.refresh()
        entered, release = threading.Event(), threading.Event()
        def top(*args):
            entered.set()
            release.wait(3)
            raise core.CmuxError('GUI timeout')
        client.top = top
        snapshot = SnapshotClient(client, daemon._process_snapshots)
        self.queue.process_lookup = lambda target: daemon._candidate_process_label(target, snapshot)
        try:
            with patch('ccc_codex_queue.subprocess.run', side_effect=self.run_command):
                result = self.queue.current_turn(self.target)
                self.assertEqual((result['kind'], result['session_id'], result['pid']),
                                 ('task_complete', 'original', 123))
                self.assertTrue(entered.wait(1))
                self.assertFalse(release.is_set())
                self.command = self.command.replace('CMUX_SURFACE_ID=s', 'CMUX_SURFACE_ID=foreign')
                self.assertEqual(self.queue.current_turn(self.target), {'kind': 'unknown'})
            self.assertEqual(client.sent, [])
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()

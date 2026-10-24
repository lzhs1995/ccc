"""Missing GUI observations must not turn live agents into idle registrations."""
import copy
import stat
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ccc_guard_scope as scope
import ccc_native_processes as native
import cmux_supervisor_tui as tui
from ccc_inventory import InventoryUnavailable


WINDOW = "00000000-0000-4000-8000-000000000001"
WORKSPACE = "00000000-0000-4000-8000-000000000002"
PANE = "00000000-0000-4000-8000-000000000003"
SURFACE = "00000000-0000-4000-8000-000000000004"


def tree():
    return {"windows": [{"id": WINDOW, "workspaces": [{
        "id": WORKSPACE, "ref": "workspace:1000020000", "panes": [{
            "id": PANE, "ref": "pane:1000020001", "surfaces": [{
                "id": SURFACE, "ref": "surface:1000020006", "type": "terminal"}]}]}]}]}


def process():
    return {"pid": 123, "birth": (100, 200), "agent_kind": "codex",
            "pgid": 123, "tpgid": 123, "tdev": 77, "surface_id": SURFACE,
            "environment_workspace_id": WORKSPACE, "argv_fingerprint": "original"}


class DeviceEntry:
    name = "ttys001"

    def stat(self, *, follow_symlinks):
        assert follow_symlinks is False
        return SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=77)


class Devices:
    def __enter__(self):
        return iter([DeviceEntry()])

    def __exit__(self, *args):
        pass


def native_fixture(rows=None):
    rows = [process()] if rows is None else rows
    stack = ExitStack()
    stack.enter_context(patch.object(scope, "scan", return_value=rows))
    stack.enter_context(patch.object(native, "display_process", return_value=process()))
    stack.enter_context(patch.object(native, "_display_tty_device", return_value=77))
    stack.enter_context(patch("os.scandir", return_value=Devices()))
    return stack


class NativeDisplayRecoveryTests(unittest.TestCase):
    def records(self):
        inventory = tree()
        return native.display_surface_records(inventory, tui.core.main_surface_records(inventory))

    def test_missing_tty_joins_uuid_foreground_device_and_generation_without_mutating_tree(self):
        records = self.records()
        before = copy.deepcopy(records)
        with native_fixture():
            recovered, labels = tui.native_display_labels(records, {}, {})
        self.assertEqual(records, before)
        self.assertEqual(recovered[0]["native_display_target"]["tty"], "ttys001")
        self.assertEqual(labels[SURFACE]["agent_pids"], [123])
        self.assertEqual(labels[SURFACE]["native_identity"]["process"], process())

    def test_ambiguous_process_foreground_birth_and_foreign_surface_are_not_adopted(self):
        cases = [
            [process(), {**process(), "pid": 124}],
            [{**process(), "tpgid": 124}],
            [{**process(), "birth": (100, 201)}],
            [{**process(), "surface_id": WINDOW}],
        ]
        for rows in cases:
            with self.subTest(rows=rows), native_fixture(rows):
                _, labels = tui.native_display_labels(self.records(), {}, {})
                self.assertNotIn(SURFACE, labels)

    def test_fresh_gui_shell_observation_is_not_overwritten_by_native_hint(self):
        records = self.records()
        records[0]["native_display_target"]["tty"] = "ttys001"
        known = {SURFACE: {"agent_kind": "shell", "agent_pids": []}}
        with patch.object(scope, "scan") as scan:
            _, labels = tui.native_display_labels(records, known, {})
        self.assertEqual(labels, known)
        scan.assert_not_called()

    def test_failed_native_scan_keeps_identity_unknown(self):
        with patch.object(scope, "scan", side_effect=RuntimeError("incomplete libproc read")):
            records, labels = tui.native_display_labels(self.records(), {}, {})
        self.assertEqual(labels, {})
        self.assertFalse(records[0]["native_display_target"]["tty"])


class QuietSource:
    def maybe_refresh(self, *args, **kwargs):
        pass

    def snapshot(self):
        return {}


class MissingTop:
    def tree(self):
        return tree()

    def top_all(self):
        raise tui.core.CmuxError("GUI process inventory timeout")


class CachedTopology:
    def get(self, *args, **kwargs):
        raise InventoryUnavailable("scan pending")

    def peek(self, name, *, max_age):
        return tree() if name == "tree" else None


class SupervisorRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        quiet = QuietSource()
        self.model = tui.SupervisorModel(Path(tmp.name) / "config.json", client=MissingTop(),
            janitor=quiet, sessions=quiet, stack=quiet, collab=quiet)
        self.addCleanup(self.model.close)
        self.model.store.load = lambda: {"workspace_rules": [{"workspace_id": WORKSPACE}]}
        self.model._refresh_batch_snapshots = lambda: None
        runtime = {"surfaces": {SURFACE: {"state": "working", "send_count": 4}}}
        for target, value in (("cmux_supervisor_tui.core.load_json", runtime),
                              ("cmux_supervisor_tui.core.ClaudeHookSettingsManager.inspect", {})):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_top_failure_still_publishes_current_native_process_and_running_state(self):
        with native_fixture():
            self.model.refresh(force=True)
        self.assertTrue(self.model.online, self.model.error)
        self.assertEqual(len(self.model.candidates), 1)
        row = self.model.candidates[0]
        self.assertEqual(row.agent_kind, "codex")
        self.assertEqual(row.record["display_location"], "w#1/p#1/s#1")
        self.assertFalse(tui.is_idling(row))

    def test_cached_tree_does_not_require_cached_top_to_recover(self):
        self.model._inventory = CachedTopology()
        with native_fixture():
            self.model.refresh(force=True)
        self.assertTrue(self.model.online, self.model.error)
        self.assertEqual(self.model.candidates[0].agent_kind, "codex")

    def test_missing_tree_does_not_invent_new_surface_or_claim_online(self):
        self.model._inventory = CachedTopology()
        self.model._inventory.peek = lambda *args, **kwargs: None
        with native_fixture(), patch.object(scope, "scan") as scan:
            self.model.refresh(force=True)
        self.assertFalse(self.model.online)
        self.assertEqual(self.model.candidates, [])
        scan.assert_not_called()


class MonitorCountTests(unittest.TestCase):
    def candidate(self, kind, *, source="workspace_rule", state="working", paused=False):
        return tui.Candidate({"surface_id": SURFACE, "workspace_id": WORKSPACE},
            source, state, "", 0, paused, agent_kind=kind)

    def test_fifty_agents_and_one_unknown_terminal_are_not_fifty_one_monitors_or_idle(self):
        counts = tui.group_counts([self.candidate("codex") for _ in range(50)] +
                                  [self.candidate("unknown", state="unknown")])
        self.assertEqual((counts["pool"], counts["identity_pending"], counts["idling"], counts["all"]),
                         (50, 1, 0, 51))

    def test_unknown_registration_is_separate_from_true_idle_and_paused(self):
        for source in ("explicit", "workspace_rule", "workspace_non_codex"):
            with self.subTest(source=source):
                counts = tui.group_counts([self.candidate("unknown", source=source, state="unknown")])
                self.assertEqual((counts["watching"], counts["pool"], counts["idling"], counts["identity_pending"]),
                                 (0, 0, 0, 1))
                row = self.candidate("unknown", source=source, state="unknown")
                self.assertEqual(tui.watch_label(row), "待检测")
        idle = tui.group_counts([self.candidate("shell", state="idle")])
        self.assertEqual((idle["pool"], idle["idling"]), (0, 1))
        paused = tui.group_counts([self.candidate("codex", paused=True)])
        self.assertEqual((paused["pool"], paused["paused"]), (0, 1))

    def test_running_recoverable_and_delivery_unknown_states_are_retained(self):
        for state in ("working", "delivery_unknown", "awaiting_transition", "recoverable_error"):
            with self.subTest(state=state):
                row = self.candidate("codex", state=state)
                counts = tui.group_counts([row])
                self.assertEqual((counts["pool"], counts["idling"], row.state), (1, 0, state))


if __name__ == "__main__":
    unittest.main()

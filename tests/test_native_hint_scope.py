"""Single-surface advisory reads keep native generations and group ambiguity."""
import unittest
import uuid
from unittest.mock import Mock, patch

import cmux_codex_watch as core
import ccc_native_processes as native


class NativeHintScopeTests(unittest.TestCase):
    def setUp(self):
        surfaces = [{"id": str(uuid.uuid4()).upper(), "ref": f"surface:{i + 1}",
                     "type": "terminal", "tty": "ttys001"} for i in range(8)]
        self.workspace = str(uuid.uuid4()).upper()
        self.tree = {"windows": [{"id": str(uuid.uuid4()).upper(), "workspaces": [
            {"id": self.workspace, "panes": [{"id": str(uuid.uuid4()).upper(),
                                             "surfaces": surfaces}]}]}]}
        self.records = core.main_surface_records(self.tree)
        self.assertEqual(len(self.records), len(surfaces))
        self.processes = [{"pid": 100 + i, "birth": (1, i), "agent_kind": "claude",
                           "surface_id": surface["id"], "pgid": 100 + i,
                           "tpgid": 100 + i, "tdev": 7,
                           "environment_workspace_id": self.workspace}
                          for i, surface in enumerate(surfaces)]
        self.live = {row["pid"]: dict(row) for row in self.processes}
        self.daemon = core.WatchDaemon.__new__(core.WatchDaemon)
        self.daemon._native_agent_processes = Mock(side_effect=lambda **_: self.processes)
        self.client = Mock()
        self.client.tree.return_value = self.tree
        device = patch.object(native, "_display_tty_device", return_value=7)
        device.start()
        self.addCleanup(device.stop)
        reader = patch.object(native, "display_process", side_effect=self.live.get)
        self.reader = reader.start()
        self.addCleanup(reader.stop)

    def hint(self, record=None, labels=None):
        return self.daemon._native_process_hint(record or self.records[0], self.client,
                                               labels or {}, {})

    def test_fleet_queries_verify_each_requested_process_once(self):
        for record, row in zip(self.records, self.processes):
            result = self.hint(record)
            self.assertEqual(result["agent_pid"], row["pid"])
            self.assertEqual(result["native_identity"]["process"], row)
        self.assertEqual(self.reader.call_count, len(self.processes))

    def test_current_target_generation_is_still_checked_on_every_query(self):
        self.assertEqual(self.hint()["agent_kind"], "claude")
        row = self.processes[0]
        self.live[row["pid"]] = dict(row, birth=(1, 99))
        self.assertEqual(self.hint().get("agent_kind", "unknown"), "unknown")
        self.assertEqual([call.args[0] for call in self.reader.call_args_list],
                         [row["pid"], row["pid"]])

    def test_same_target_duplicate_cannot_be_hidden_by_scoping(self):
        duplicate = dict(self.processes[0], pid=999, pgid=999, tpgid=999)
        self.processes.append(duplicate)
        self.live[999] = duplicate
        self.assertEqual(self.hint().get("agent_kind", "unknown"), "unknown")

    def test_existing_gui_conflict_remains_authoritative(self):
        sid = self.records[0]["surface_id"]
        conflict = {"agent_kind": "unknown", "identity_conflicts": ["gui conflict"]}
        self.assertEqual(self.hint(labels={sid: conflict}), conflict)
        self.reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()

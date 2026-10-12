"""A lane must release a lost native source without trusting GUI hints."""
import unittest
from unittest.mock import patch

from tests import test_native_dispatch_ownership as ownership


class MissingNativeSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ownership.OwnershipTests(
            "test_parent_is_excluded_before_child_can_receive_assignment")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.mock_exact_process()
        self.fixture.source["birth"] = [1, 100]
        self.fixture.daemon.codex_queue_recovery.current_turn = lambda target: {
            **self.fixture.source, "kind": "task_started"}

    def admit(self):
        self.fixture.refresh()
        self.assertTrue(self.fixture.dispatch.owns(self.fixture.sid))
        return self.fixture.dispatch.owned[self.fixture.sid]

    def lose_source(self):
        daemon = self.fixture.daemon
        daemon.codex_queue_recovery.wakeup_sources = lambda targets: []
        # NativeProcessIndex is an advisory discovery source. A missing hint
        # must not be treated as proof of either liveness or exit.
        return patch.object(daemon._native_process_index, "lookup", return_value=None)

    def release(self, owned):
        lane = owned["state"]["lane"]
        lane.rows.append({"type": "released", "surface_id": self.fixture.sid,
                          "cell": owned["assignment"]["cell"], "runtime": {}})
        self.fixture.refresh()
        self.assertFalse(self.fixture.dispatch.owns(self.fixture.sid))
        self.assertEqual(self.fixture.dispatch.filter(self.fixture.targets, object())[0],
                         self.fixture.targets)

    def test_dead_source_revokes_cell_then_returns_parent_after_release(self):
        owned = self.admit()
        with self.lose_source(), patch("ccc_codex_queue.process_placement_start",
                                      return_value=None), \
             patch("ccc_guard_scope.scan",
                   side_effect=AssertionError("no global process scan")):
            self.fixture.refresh()
            self.assertEqual(self.fixture.dispatch.cells.shared()[owned["assignment"]["cell"]], 0)
            # The parent remains excluded until the old lane's final receipt.
            self.assertTrue(self.fixture.dispatch.owns(self.fixture.sid))
            self.assertEqual(owned["state"]["lane"].assignments, [])
            self.release(owned)

    def test_replacement_process_revokes_old_cell_without_global_scan(self):
        owned = self.admit()
        with self.lose_source(), patch("ccc_codex_queue.process_placement_start",
                                      return_value=2), \
             patch("ccc_guard_scope.scan",
                   side_effect=AssertionError("no global process scan")):
            self.fixture.refresh()
            self.assertEqual(self.fixture.dispatch.cells.shared()[owned["assignment"]["cell"]], 0)
            self.assertTrue(self.fixture.dispatch.owns(self.fixture.sid))
            self.release(owned)

    def test_live_source_gap_keeps_old_cell_when_fresh_identity_matches(self):
        owned = self.admit()
        with self.lose_source(), patch("ccc_codex_queue.process_placement_start",
                                      return_value=1), \
             patch("ccc_guard_scope.birth", return_value=[1, 100]), \
             patch("ccc_guard_scope.scan",
                   side_effect=AssertionError("no global process scan")):
            for _ in range(3):
                self.fixture.refresh()
                self.assertEqual(self.fixture.dispatch.cells.shared()[owned["assignment"]["cell"]], 1)
                self.assertTrue(self.fixture.dispatch.owns(self.fixture.sid))

    def test_probe_exception_keeps_ownership_until_next_resolved_check(self):
        owned = self.admit()
        with self.lose_source(), patch("ccc_codex_queue.process_placement_start",
                                      side_effect=OSError("inspection unavailable")), \
             patch("ccc_guard_scope.scan",
                   side_effect=AssertionError("no global process scan")):
            self.fixture.refresh()
            self.assertEqual(self.fixture.dispatch.cells.shared()[owned["assignment"]["cell"]], 1)
            self.assertTrue(self.fixture.dispatch.owns(self.fixture.sid))
        with self.lose_source(), patch("ccc_codex_queue.process_placement_start",
                                      return_value=None):
            self.fixture.refresh()
            self.assertEqual(self.fixture.dispatch.cells.shared()[owned["assignment"]["cell"]], 0)
            self.release(owned)

    def test_same_pid_same_second_reuse_revokes_precise_birth_binding(self):
        owned = self.admit()
        with self.lose_source(), patch("ccc_codex_queue.process_placement_start",
                                      return_value=1), \
             patch("ccc_guard_scope.birth", return_value=[1, 101]), \
             patch("ccc_guard_scope.scan",
                   side_effect=AssertionError("no global process scan")):
            self.fixture.refresh()
            self.assertEqual(self.fixture.dispatch.cells.shared()[owned["assignment"]["cell"]], 0)
            self.assertTrue(self.fixture.dispatch.owns(self.fixture.sid))
            self.release(owned)


if __name__ == "__main__":
    unittest.main()

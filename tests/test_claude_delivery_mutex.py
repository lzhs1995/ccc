"""CCC shares the real MAC locks and never submits a partial composer."""
import copy
import fcntl
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import cmux_codex_watch as w
from tests import test_claude_provider_guard as provider_guard
from tests.test_watch import (FakeClient, armed_daemon, bind_native_failure,
                              claude_grid_payload, claude_wrapped_composer_payload,
                              grid_payload, span)


class ClaudeDeliveryMutexTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.locks = self.root / "mac-deliveries-v1"
        self.locks.mkdir()
        patcher = mock.patch.object(w, "DELIVERY_LOCK_ROOT", self.locks)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.serial = 0

    def case(self, *, pending=False):
        self.serial += 1
        folder = self.root / str(self.serial)
        folder.mkdir()
        return provider_guard.ProviderGuardTests().case(folder, pending=pending)

    def lock_paths(self, surface="surface-uuid"):
        # Public MAC deliveries-v1 naming contract, independent of CCC helpers.
        return (
            self.locks / ("pane-" + hashlib.sha256(b"WORKSPACE-UUID:PANE-UUID").hexdigest() + ".lock"),
            self.locks / ("target-" + hashlib.sha256(surface.upper().encode()).hexdigest() + ".lock"),
        )

    def assert_locks_free(self, paths=None):
        for path in paths or self.lock_paths():
            with path.open("a+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def assert_locks_held(self):
        for path in self.lock_paths():
            with path.open("a+") as handle, self.assertRaises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_codex_high_demand_recovers_while_claude_delivery_locks_are_held(self):
        error = "We're currently experiencing high demand, which may cause temporary errors."
        for path in self.lock_paths():
            with self.subTest(lock=path.name), tempfile.TemporaryDirectory() as folder:
                client = FakeClient(grid_payload([], error=error), "■ " + error)
                daemon = armed_daemon(folder, client)
                bind_native_failure(daemon, error)
                with path.open("a+") as held, mock.patch.object(
                        daemon, "_acquire_claude_delivery",
                        wraps=daemon._acquire_claude_delivery) as acquire:
                    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    daemon.process_once(client)
                    daemon.process_once(client)
                    self.assertEqual(len(client.sent), 1)
                    acquire.assert_not_called()

    def test_idle_claude_without_submit_transaction_does_not_take_delivery_lock(self):
        daemon, target, runtime, _, client = self.case()
        state = w.ScreenState("empty", message_kind="claude")
        with mock.patch.object(daemon, "_acquire_claude_delivery",
                               wraps=daemon._acquire_claude_delivery) as acquire:
            self.assertFalse(daemon._reconcile_claude_submit(target, runtime, state, client))
            self.assertFalse(daemon._recover_orphan_watchdog_submit(target, runtime, state, client))
            acquire.assert_not_called()

    def test_mac_pane_and_target_locks_exclude_every_input_entrypoint(self):
        for locked_path in self.lock_paths():
            for route in ("event", "enter", "orphan", "reconcile"):
                with self.subTest(lock=locked_path.name, route=route):
                    d, t, r, e, c = self.case(pending=route in {"enter", "reconcile"})
                    before = (list(c.sent_text), list(c.sent_keys), r.claude_submit_phase,
                              r.claude_submit_attempts, r.claude_submit_event_id)
                    with locked_path.open("a+") as held:
                        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        if route == "event":
                            self.assertFalse(d._send_claude_event(e, t, r, c)[0])
                        elif route == "enter":
                            self.assertFalse(d._send_claude_enter(t, r, c, reason="late", input_check=lambda: True))
                        else:
                            state = w.ScreenState("composer_busy", watchdog_echo=True)
                            method = d._recover_orphan_watchdog_submit if route == "orphan" else d._reconcile_claude_submit
                            self.assertTrue(method(t, r, state, c))
                    self.assertEqual((c.sent_text, c.sent_keys, r.claude_submit_phase,
                                      r.claude_submit_attempts, r.claude_submit_event_id), before)
                    self.assert_locks_free()

    def test_paste_and_one_enter_hold_both_mac_locks(self):
        d, t, r, e, c = self.case()
        raw_text, raw_key = c.send_text, c.send_key
        writes = []

        def write(raw, kind, *args):
            self.assert_locks_held()
            writes.append(kind)
            return raw(*args)

        provider_guard.ProviderGuardTests().guard_transport(c)
        with mock.patch.object(c, "send_text", side_effect=lambda *a: write(raw_text, "paste", *a)), \
             mock.patch.object(c, "send_key", side_effect=lambda *a: write(raw_key, "enter", *a)):
            sent, reason = d._send_claude_event(e, t, r, c)
        self.assertTrue(sent, reason)
        self.assertEqual(writes, ["paste", "enter"])
        self.assertEqual((len(c.sent_text), len(c.sent_keys), r.send_count), (1, 1, 1))
        self.assert_locks_free()

    def test_another_thread_cannot_enter_a_sibling_tab_during_delivery(self):
        d, t, r, _, c = self.case(pending=True)
        other_target = dict(t, surface_id="second-surface")
        d.config["targets"].append(other_target)
        other_runtime = copy.deepcopy(r)
        d.runtime[other_target["surface_id"]] = other_runtime
        tree = copy.deepcopy(c.tree_data)
        surfaces = tree["windows"][0]["workspaces"][0]["panes"][0]["surfaces"]
        surfaces.append({"id": "second-surface", "ref": "surface:2", "type": "terminal"})
        c.tree_data = tree
        payload = claude_wrapped_composer_payload()
        payload["render_grid"]["surface_id"] = "second-surface"
        other = FakeClient(payload, tree=tree)
        outcomes, errors = [], []

        def contender():
            try:
                outcomes.append(d._send_claude_enter(other_target, other_runtime, other,
                                                     reason="sibling", input_check=lambda: True))
            except BaseException as exc:
                errors.append(exc)

        raw_key = c.send_key

        def key(*args):
            worker = threading.Thread(target=contender)
            worker.start()
            worker.join(2)
            self.assertFalse(worker.is_alive(), "delivery lock must not block the worker")
            self.assertEqual(errors, [])
            self.assertEqual(outcomes, [False])
            self.assertEqual(other.sent_keys, [])
            return raw_key(*args)

        with mock.patch.object(c, "send_key", side_effect=key):
            self.assertTrue(d._send_claude_enter(t, r, c, reason="first", input_check=lambda: True))
        self.assertTrue(d._send_claude_enter(other_target, other_runtime, other,
                                             reason="after_release", input_check=lambda: True))
        self.assertEqual(len(other.sent_keys), 1)

    def test_first_acquisition_rejects_replaced_lock_inode(self):
        raw_flock = fcntl.flock
        for replace_index in (0, 1):
            with self.subTest(replace_index=replace_index):
                d, t, r, _, c = self.case(pending=True)
                calls, displaced = [], []

                def lock(handle, flags):
                    result = raw_flock(handle, flags)
                    calls.append(flags)
                    if len(calls) == replace_index + 1:
                        path = self.lock_paths()[replace_index]
                        old = path.with_suffix(f".old-{self.serial}")
                        path.rename(old)
                        path.touch()
                        displaced.append(old)
                    return result

                before = (r.claude_submit_attempts, r.claude_submit_event_id, r.claude_submit_phase)
                with mock.patch.object(w.fcntl, "flock", side_effect=lock):
                    self.assertFalse(d._send_claude_enter(t, r, c, reason="race", input_check=lambda: True))
                self.assertTrue(displaced)
                self.assertEqual(c.sent_keys, [])
                self.assertEqual((r.claude_submit_attempts, r.claude_submit_event_id, r.claude_submit_phase), before)
                self.assert_locks_free([*self.lock_paths(), *displaced])

    def test_inode_is_rechecked_at_both_connected_write_boundaries(self):
        for stage in ("text", "enter"):
            with self.subTest(stage=stage):
                d, t, r, e, c = self.case()
                displaced = []

                def replace(kind):
                    if kind == stage:
                        path = self.lock_paths()[0]
                        old = path.with_suffix(f".boundary-{self.serial}")
                        path.rename(old)
                        path.touch()
                        displaced.append(old)

                provider_guard.ProviderGuardTests().guard_transport(c, before=replace)
                self.assertFalse(d._send_claude_event(e, t, r, c)[0])
                self.assertEqual(len(displaced), 1)
                self.assertEqual(len(c.sent_text), int(stage == "enter"))
                self.assertEqual(c.sent_keys, [])
                self.assert_locks_free([*self.lock_paths(), *displaced])

    def test_transport_exception_releases_locks_without_resetting_unknown_budget(self):
        d, t, r, _, c = self.case(pending=True)
        r.claude_submit_attempts = 2
        r.claude_orphan_enter_count = 2
        with mock.patch.object(c, "send_key", side_effect=w.UncertainDeliveryError("lost key ACK")) as key:
            self.assertFalse(d._send_claude_enter(t, r, c, reason="lost", input_check=lambda: True))
            self.assert_locks_free()
            self.assertTrue(r.claude_submit_write_unknown)
            original = (r.claude_submit_event_id, r.claude_submit_attempts, r.claude_orphan_enter_count)
            self.assertFalse(d._send_claude_enter(t, r, c, reason="unknown", input_check=lambda: True))
            d._reconcile_claude_submit(t, r, w.ScreenState("composer_busy", watchdog_echo=True), c)
            self.assertEqual((r.claude_submit_event_id, r.claude_submit_attempts, r.claude_orphan_enter_count), original)
            self.assertEqual(key.call_count, 1)
        self.assert_locks_free()

    def test_visible_draft_below_cursor_prevents_paste(self):
        d, t, r, e, c = self.case()
        payload = claude_grid_payload(cursor_visible=True)
        grid = payload["render_grid"]
        grid["row_spans"].append(span(grid["cursor"]["row"] + 1, 2, "foreign draft", 0))
        c.payload = payload
        state = w.classify_claude_grid(w.Grid.from_rpc(payload, t["surface_id"]))
        self.assertEqual(state.kind, "composer_busy")
        self.assertFalse(d._send_claude_event(e, t, r, c)[0])
        self.assertEqual((c.sent_text, c.sent_keys), ([], []))

    def test_watchdog_prefix_does_not_own_the_rest_of_a_composer(self):
        for shape in ("foreign_below_cursor", "folded", "cropped"):
            with self.subTest(shape=shape):
                d, t, r, _, c = self.case(pending=True)
                payload = claude_wrapped_composer_payload()
                grid = payload["render_grid"]
                if shape == "cropped":
                    # The viewport ends inside the box: no closing border, no footer.
                    grid["row_spans"] = [s for s in grid["row_spans"] if s["row"] <= grid["cursor"]["row"]]
                else:
                    text = "foreign task notice" if shape == "foreign_below_cursor" else "[Pasted text #2 +4 lines]"
                    grid["row_spans"].append(span(grid["cursor"]["row"] + 1, 2, text, 0))
                c._submit_echo = False
                c.payload = payload
                self.assertFalse(d._send_claude_enter(t, r, c, reason=shape, input_check=lambda: True))
                self.assertEqual(c.sent_keys, [])
                self.assertEqual(r.claude_submit_attempts, 0)

    def test_complete_composer_must_be_stable_across_two_fresh_reads(self):
        d, t, r, _, c = self.case(pending=True)
        first = claude_wrapped_composer_payload()
        second = copy.deepcopy(first)
        second["render_grid"]["columns"] += 1
        with mock.patch.object(c, "replay", side_effect=[first, second]) as replay:
            self.assertFalse(d._send_claude_enter(t, r, c, reason="resized", input_check=lambda: True))
        self.assertEqual(replay.call_count, 2)
        self.assertEqual(c.sent_keys, [])


if __name__ == "__main__":
    unittest.main()

"""Selected-row durability, including actual Claude response-loss recovery."""
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import ccc_state_snapshot as snapshot
import cmux_codex_watch as watch
from tests.test_state_snapshot import copied_clone
from tests import test_claude_delivery_recovery as recovery


class PartialSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.json"
        for patcher in (mock.patch.object(snapshot, "_clone_function", return_value=object()),
                        mock.patch.object(snapshot, "_clone", side_effect=copied_clone)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.writer = snapshot.AtomicJSONSnapshot(self.path)
        self.old = {"archived": {"unknown": True, "attempt": "original"},
                    "live": {"phase": "text_written", "budget": 1}}
        self.writer.write(self.old)

    def read(self):
        return json.loads(self.path.read_text())

    def test_warm_update_encodes_only_selected_row_and_keeps_history(self):
        changes = {"live": {"phase": "enter_sent", "budget": 1}}
        with mock.patch.object(self.writer, "_encode", wraps=self.writer._encode) as encoded:
            result = self.writer.update(changes)
        self.assertEqual(encoded.call_args_list, [mock.call(changes)])
        self.assertEqual(result["changed_records"], 1)
        self.assertEqual(self.read(), {**self.old, **changes})

    def test_cold_update_and_foreign_replacement_keep_original_rows(self):
        self.writer = snapshot.AtomicJSONSnapshot(self.path)
        self.writer.update({"live": {"phase": "unknown"}})
        self.assertEqual(self.read()["archived"], self.old["archived"])
        foreign = self.path.with_suffix(".foreign")
        foreign.write_text('{"foreign":{"unknown":true}}')
        os.replace(foreign, self.path)
        self.writer.update({"live": {"phase": "none"}})
        self.assertEqual(self.read(), {"foreign": {"unknown": True}, "live": {"phase": "none"}})

    def test_malformed_and_non_regular_source_are_not_overwritten(self):
        self.path.write_text("{broken")
        with self.assertRaises(ValueError):
            self.writer.update({"live": {}})
        self.assertEqual(self.path.read_text(), "{broken")
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            self.writer.update({"live": {}})

    def test_deleted_source_cannot_drop_archived_unknown_records(self):
        self.path.unlink()
        with self.assertRaisesRegex(RuntimeError, "disappeared"):
            self.writer.update({"live": {"phase": "enter_sent"}})
        self.assertFalse(self.path.exists())
        self.writer.write(self.old)  # Full publication may restore known history.
        self.writer.update({"live": {"phase": "unknown"}})
        self.assertEqual(self.read()["archived"], self.old["archived"])

    def test_complete_publication_repairs_malformed_source_before_update(self):
        self.path.write_text("{broken")
        with self.assertRaises(ValueError):
            self.writer.update({"live": {"phase": "enter_sent"}})
        self.writer.write(self.old)
        changes = {"live": {"phase": "unknown", "budget": 1}}
        self.writer.update(changes)
        self.assertEqual(self.read(), {**self.old, **changes})

    def test_each_fsync_failure_invalidates_cache_and_requires_both_barriers(self):
        for failure_at in (1, 2):
            with self.subTest(failure_at=failure_at):
                self.writer.write(self.old)
                actual, calls = os.fsync, []
                def fail(fd):
                    calls.append(fd)
                    if len(calls) == failure_at:
                        raise OSError(errno.EIO, "sync failed")
                    return actual(fd)
                changes = {"live": {"phase": "unknown", "budget": 1}}
                with mock.patch.object(snapshot.os, "fsync", side_effect=fail):
                    with self.assertRaises(OSError):
                        self.writer.update(changes)
                self.assertIsNone(self.writer._identity)
                with mock.patch.object(snapshot.os, "fsync", wraps=actual) as synced:
                    self.assertNotEqual(self.writer.update(changes)["mode"], "unchanged")
                self.assertEqual(synced.call_count, 2)
                self.assertEqual(self.read(), {**self.old, **changes})

    def test_change_after_file_sync_does_not_overwrite_foreign_writer(self):
        actual = os.fsync
        changed = False
        def replace_source(fd):
            nonlocal changed
            actual(fd)
            if not changed:
                changed = True
                foreign = self.path.with_suffix(".foreign")
                foreign.write_text('{"foreign":{"attempt":"keep"}}')
                os.replace(foreign, self.path)
        with mock.patch.object(snapshot.os, "fsync", side_effect=replace_source):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                self.writer.update({"live": {"phase": "enter_sent"}})
        self.assertEqual(self.read(), {"foreign": {"attempt": "keep"}})

    def test_unsupported_clone_still_publishes_complete_durable_json(self):
        with mock.patch.object(snapshot, "_clone", return_value=False):
            self.assertEqual(self.writer.update({"live": {"phase": "unknown"}})["mode"], "compact")
        self.assertEqual(self.read()["archived"], self.old["archived"])


class ClaudeSurfacePersistenceTests(unittest.TestCase):
    def setup_case(self, directory):
        return recovery.DeliveryRecoveryTests().setup_case(directory)

    def test_surface_barrier_never_waits_for_a_new_fleet_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, target, runtime, event, client = self.setup_case(directory)
            runtime.claude_submit_event_id = event["event_id"]
            runtime.claude_submit_native_identity = {"session_id": "original", "process_generation": "original"}
            runtime.claude_submit_phase = "text_written"
            runtime.claude_submit_write_unknown = True
            runtime.claude_orphan_enter_count = 1
            daemon._state_writer = mock.Mock()
            daemon._state_writer.request.side_effect = AssertionError("fleet barrier")
            daemon._state_snapshot.write({"archive": {"attempt": "keep", "unknown": True}})
            daemon._save_claude_surface(target["surface_id"], runtime)
            saved = json.loads(daemon.state_path.read_text())
            self.assertEqual(saved["archive"], {"attempt": "keep", "unknown": True})
            self.assertEqual(saved[target["surface_id"]], runtime.to_dict())
            daemon._state_writer.request.assert_not_called()

    def test_surface_barrier_waits_for_inflight_full_write_without_losing_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, target, runtime, event, client = self.setup_case(directory)
            entered, release = threading.Event(), threading.Event()
            original = daemon._state_snapshot.write
            failures = []
            def slow_write(value):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError("test did not release the full writer")
                return original(value)
            def full_write():
                try:
                    daemon._save_now()
                except BaseException as exc:
                    failures.append(exc)
            with mock.patch.object(daemon._state_snapshot, "write", side_effect=slow_write):
                writer = threading.Thread(target=full_write)
                writer.start()
                self.assertTrue(entered.wait(2))
                runtime.claude_submit_write_unknown = True
                runtime.claude_orphan_enter_count = 1
                timer = threading.Timer(0.15, release.set)
                timer.start()
                start = time.perf_counter()
                try:
                    daemon._save_claude_surface(target["surface_id"], runtime)
                finally:
                    release.set()
                    writer.join(2)
                    timer.join(2)
                elapsed = time.perf_counter() - start
            self.assertFalse(writer.is_alive())
            self.assertEqual(failures, [])
            # An in-flight full write still delays a surface barrier. This
            # bounded fixture does not establish a production latency limit.
            self.assertGreaterEqual(elapsed, 0.12)
            self.assertLess(elapsed, 2)
            self.assertEqual(json.loads(daemon.state_path.read_text())[target["surface_id"]], runtime.to_dict())

    def test_lost_enter_response_is_durable_without_an_extra_full_save(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon, target, runtime, event, client = self.setup_case(directory)
            daemon.claude_event_ledger.mark(event, "reserved")
            runtime.claude_submit_event_id = event["event_id"]
            runtime.claude_submit_phase = "text_written"
            runtime.claude_submit_since = time.time() - 100
            client.send_text(target["workspace_id"], target["surface_id"], watch.CLAUDE_MESSAGE)
            client.send_key = mock.Mock(side_effect=watch.CmuxError("response lost after input"))
            self.assertFalse(daemon._send_claude_enter(target, runtime, client, reason="late", input_check=lambda: True))
            restarted = watch.WatchDaemon(daemon.config_path, daemon.state_path, client=client)
            retained = restarted.runtime[target["surface_id"]]
            self.assertTrue(retained.claude_submit_write_unknown)
            self.assertEqual(retained.claude_submit_event_id, event["event_id"])
            state = watch.ScreenState("composer_busy", message_kind="claude", watchdog_echo=True)
            self.assertTrue(restarted._reconcile_claude_submit(target, retained, state, client))
            self.assertEqual(client.send_key.call_count, 1)
            self.assertEqual(retained.send_count, 0)


if __name__ == "__main__":
    unittest.main()

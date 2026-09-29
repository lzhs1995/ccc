from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from tests.test_janitor import JanitorTestCase, SRC, UUIDS

spec = importlib.util.spec_from_file_location("janitor_maintenance", SRC / "janitor_maintenance.py")
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


class MaintenanceTests(JanitorTestCase):
    def setUp(self):
        super().setUp()
        self.box.set_config(MODE="apply", QUARANTINE_KEEP_HOURS=3)
        values = {"HOME": self.box.home, "JD": self.box.jd, "CM": self.box.cm,
                  "Q": self.box.quarantine, "STAGING": self.box.staging}
        self.patches = [patch.object(maintenance, k, v) for k, v in values.items()]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()

    def test_three_hour_sealed_boundary(self):
        before = self.box.batch("before", sealed_ago_h=2.9)
        due = self.box.batch("due", sealed_ago_h=3.1)
        result = maintenance.expire()
        self.assertTrue(before.exists())
        self.assertFalse(due.exists())
        self.assertEqual(result["expired_batches"], 1)

    def test_mixed_candidate_ranking_needs_no_per_file_processes(self):
        first, second = self.box.jd / 'first.txt', self.box.jd / 'second.txt'
        newer, older = self.box.cm / 'newer.sb-file', self.box.staging / UUIDS[0]
        newer.write_text('newer')
        older.mkdir(parents=True, exist_ok=True)
        os.utime(newer, (2000, 2000))
        os.utime(older, (1000, 1000))
        missing = self.box.cm / 'gone.sb-file'
        first.write_text(f'{newer}\n{missing}\n')
        second.write_text(f'{older}\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(maintenance.subprocess, 'run',
                side_effect=AssertionError('per-file subprocess')):
            maintenance.rank_candidates([first, second])
        self.assertEqual(output.getvalue().splitlines(),
                         [f'0\t{missing}', f'1000\t{older}', f'2000\t{newer}'])

    def test_exact_expiry_boundary_uses_seal_not_directory_mtime(self):
        batch = self.box.batch("boundary", sealed_ago_h=0)
        sealed = json.loads((batch / maintenance.META).read_text())["sealed_at_epoch"]
        with patch.object(maintenance.time, "time", return_value=sealed + 10800 - 1):
            maintenance.expire()
        self.assertTrue(batch.exists())
        with patch.object(maintenance.time, "time", return_value=sealed + 10800):
            maintenance.expire()
        self.assertFalse(batch.exists())

    def test_invalid_incomplete_and_linked_batches_fail_closed(self):
        incomplete = self.box.batch(".incomplete-crash", sealed_ago_h=4)
        missing = self.box.batch("missing", sealed_ago_h=4, metadata=False)
        bad = self.box.batch("bad", sealed_ago_h=4)
        data = json.loads((bad / maintenance.META).read_text())
        data["schema_version"] = 7
        (bad / maintenance.META).write_text(json.dumps(data))
        linked = self.box.quarantine / "linked"
        linked.symlink_to(self.box.published, target_is_directory=True)
        result = maintenance.expire()
        self.assertEqual(result["expired_batches"], 0)
        self.assertTrue(all(p.exists() for p in (incomplete, missing, bad, linked)))

    def test_referenced_and_open_batches_are_not_deleted(self):
        batch = self.box.batch("referenced", sealed_ago_h=4)
        self.box.set_live_ids([UUIDS[0]])
        self.assertEqual(maintenance.expire()["expired_batches"], 0)
        self.box.set_live_ids(["00000000-0000-0000-0000-000000000000"])
        payload = batch / UUIDS[0] / "payload"
        with payload.open("w") as held:
            held.write("in use")
            held.flush()
            self.assertEqual(maintenance.expire()["expired_batches"], 0)
        self.assertTrue(batch.exists())

    def test_immediate_scope_does_not_delete_future_quarantine(self):
        original = self.box.batch("original", sealed_ago_h=0.2)
        scope = maintenance.capture()
        path = self.box.jd / "scope.json"
        maintenance.atomic_json(path, scope)
        future = self.box.batch("future", sealed_ago_h=0.1)
        result = maintenance.expire(immediate_manifest=path)
        self.assertEqual(result["expired_batches"], 1)
        self.assertFalse(original.exists())
        self.assertTrue(future.exists())

    def test_scope_does_not_bless_a_changed_existing_batch(self):
        batch = self.box.batch("changed", sealed_ago_h=0.2)
        path = self.box.jd / "scope.json"
        maintenance.atomic_json(path, maintenance.capture())
        (batch / UUIDS[0] / "later").write_text("new")
        self.assertEqual(maintenance.expire(immediate_manifest=path)["expired_batches"], 0)
        self.assertTrue(batch.exists())

    def test_expiry_preserves_last_sweep_and_refreshes_quarantine_status(self):
        self.box.run_guard()
        self.box.run_janitor()
        before = (self.box.jd / "janitor-state.json").read_bytes()
        self.box.batch("old", sealed_ago_h=4)
        maintenance.expire()
        self.assertEqual((self.box.jd / "janitor-state.json").read_bytes(), before)
        status = self.box.ctl_json()
        self.assertEqual(status["quarantine"]["batch_count"], 0)
        self.assertEqual(status["quarantine"]["keep_hours"], 3)
        self.assertEqual(status["expiry"]["expired_batches"], 1)

    def test_mutex_pause_and_guard_trip_refuse_expiry(self):
        batch = self.box.batch("old", sealed_ago_h=4)
        for sentinel in ("DISABLED", "GUARD_TRIPPED", ".janitor.mutex"):
            with self.subTest(sentinel=sentinel):
                path = self.box.jd / sentinel
                path.mkdir() if sentinel.endswith("mutex") else path.touch()
                with self.assertRaises(maintenance.Refused):
                    maintenance.expire()
                self.assertTrue(batch.exists())
                path.rmdir() if path.is_dir() else path.unlink()

    def test_drain_is_finite_batched_and_preserves_new_candidates(self):
        self.box.set_config(MAX_ITEMS_PER_RUN=2)
        originals = self.box.aged_sb(5)
        real_capture = maintenance.capture
        future = self.box.cm / "future.sb-extra"

        def capture_then_new_file():
            result = real_capture()
            future.write_text("new candidate outside the snapshot")
            old = time.time() - 7200
            os.utime(future, (old, old))
            return result

        with patch.object(maintenance, "capture", side_effect=capture_then_new_file), \
                patch.dict(os.environ, {"CMUX_JANITOR_TEST_SETTLE_SEC": "0"}):
            result = maintenance.drain(purge_now=True)
        self.assertEqual(result["phase"], "complete")
        self.assertEqual((result["total"], result["processed"], result["disposed"]), (5, 5, 5))
        self.assertEqual(result["expired_items"], 5)
        self.assertFalse(any(p.exists() for p in originals))
        self.assertTrue(future.exists())
        metrics = [json.loads(line) for line in (self.box.jd / "metrics.jsonl").read_text().splitlines()]
        self.assertEqual([x["selected"] for x in metrics], [2, 2, 1])


if __name__ == "__main__":
    unittest.main()

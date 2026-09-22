"""Phase 1 janitor/guard/ctl contract tests.

Every assertion here was written against observed behaviour of the scripts in
``janitor/src``, not from memory: the first draft of this file failed 17 times
because it guessed the owner-file format, the log wording, and the shape of a
live store.  When a probe finds nothing, suspect the probe.

Everything runs against a throwaway ``HOME``.  Nothing may touch the real
``~/.cmuxterm`` or ``~/.config/cmux-janitor``; ``SandboxTests`` asserts that.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "janitor" / "src"
PYTHON = "/opt/homebrew/bin/python3.14"

# 【label 前缀必须和被测代码同源派生，不能写死账号名】
# 写死 `com.<某个账号>` 有两个问题：一是发布源码会泄露账号名；二是这些常量的
# **唯一作用**是反向断言「沙箱操作没有溢出到真实域」，而「真实域」就是
# janitor 自己算出来的那些 label。写死的字面量在换账号的机器上会指向一组
# 根本不存在的 label，于是「从未 bootout 真实 label」这条断言变成空转。
LABEL_PREFIX = f"com.{os.environ.get('USER') or Path.home().name or 'user'}"

# A staging dir only counts as a candidate when its name is UUID-shaped; the
# janitor's own -regex gate is what these have to satisfy.  The pool must stay
# comfortably larger than measure_selected()'s 64-item budget: at 59 names a
# test asking for 70 candidates silently got 59 and stayed `exact`, so the
# precision assertions were testing the fixture, not the script.
UUIDS = [f"{i:08X}-1111-2222-3333-444444444444" for i in range(1, 201)]


class Sandbox:
    """A temporary HOME with the janitor installed into it."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="janitor-box-"))
        self.home = self.root / "home"
        self.jd = self.home / ".config" / "cmux-janitor"
        self.cm = self.home / ".cmuxterm"
        self.staging = self.cm / "agent-turn-diff-baseline-snapshots-staging"
        self.published = self.cm / "agent-turn-diff-baseline-snapshots"
        self.quarantine = self.home / ".cmuxterm-janitor-quarantine"
        for path in (self.jd, self.staging, self.published, self.home / ".config" / "cmux",
                     self.home / ".cmux" / "hooks"):
            path.mkdir(parents=True, exist_ok=True)

        self.live = self.cm / "agent-turn-diff-baselines.json"
        self.lock = self.cm / "agent-turn-diff-baselines.json.lock"
        # A store with zero extractable ids is a parse gap, and the janitor
        # deliberately fails closed on it (ABORT-STAGING).  Seed one real id so
        # the normal path is exercised; tests that want the gap set it directly.
        self.set_live_ids(["00000000-0000-0000-0000-000000000000"])
        self.lock.write_text("", encoding="utf-8")

        for path in SRC.iterdir():
            if path.is_file():
                target = self.jd / path.name
                target.write_bytes(path.read_bytes())
                target.chmod(0o755)
        # Guard R3 requires >=150 published snapshots; give it a healthy count.
        for i in range(160):
            (self.published / f"pub-{i:04d}").mkdir()

    # ---- fixture helpers ----

    def set_live_ids(self, ids: list[str]) -> None:
        body = {"entries": [{"untrackedSnapshotId": i} for i in ids]}
        self.live.write_text(json.dumps(body), encoding="utf-8")

    def set_config(self, **overrides: object) -> None:
        text = (self.jd / "config.env").read_text(encoding="utf-8")
        lines = []
        for line in text.splitlines():
            key = line.split("=", 1)[0].strip()
            if key in overrides:
                lines.append(f"{key}={overrides.pop(key)}")
            else:
                lines.append(line)
        for key, value in overrides.items():
            lines.append(f"{key}={value}")
        (self.jd / "config.env").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def aged_staging(self, count: int, *, minutes: int = 120) -> list[Path]:
        made = []
        old = time.time() - minutes * 60
        for name in UUIDS[:count]:
            directory = self.staging / name
            directory.mkdir(exist_ok=True)
            (directory / "payload.txt").write_text("x" * 128, encoding="utf-8")
            os.utime(directory / "payload.txt", (old, old))
            os.utime(directory, (old, old))
            made.append(directory)
        return made

    def young_staging(self, count: int) -> list[Path]:
        made = []
        for name in UUIDS[count:count * 2]:
            directory = self.staging / name
            directory.mkdir(exist_ok=True)
            (directory / "payload.txt").write_text("y", encoding="utf-8")
            made.append(directory)
        return made

    def aged_sb(self, count: int, *, minutes: int = 120) -> list[Path]:
        made = []
        old = time.time() - minutes * 60
        for index in range(count):
            path = self.cm / f"agent-turn-diff-baselines.json.sb-{index:05d}"
            path.write_text("z" * 64, encoding="utf-8")
            os.utime(path, (old, old))
            made.append(path)
        return made

    def batch(self, name: str, *, sealed_ago_h: float, items: int = 1,
              metadata: bool = True) -> Path:
        directory = self.quarantine / name
        directory.mkdir(parents=True, exist_ok=True)
        for i in range(items):
            (directory / f"{UUIDS[i]}").mkdir(exist_ok=True)
        sealed = time.time() - sealed_ago_h * 3600
        if metadata:
            (directory / ".janitor-batch.json").write_text(json.dumps({
                "schema_version": 1,
                "run_id": f"fixture-{name}",
                "created_at_epoch": int(sealed) - 5,
                "sealed_at_epoch": int(sealed),
                "sealed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(sealed)),
                "item_count": items,
            }), encoding="utf-8")
        os.utime(directory, (sealed, sealed))
        return directory

    # ---- runners ----

    def _env(self, **extra: str) -> dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        # The script reads CMUX_JANITOR_TEST_SETTLE_SEC (cmux-janitor.sh:573).
        # An earlier draft set CMUX_JANITOR_SETTLE_SEC, which the script never
        # looks at, so every run paid the 3s production settle in silence.
        env["CMUX_JANITOR_TEST_SETTLE_SEC"] = "0"
        env.update(extra)
        return env

    def run_janitor(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/bin/bash", str(self.jd / "cmux-janitor.sh"), *args],
                              capture_output=True, text=True, env=self._env(**extra))

    def run_guard(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/bin/bash", str(self.jd / "guard.sh"), *args],
                              capture_output=True, text=True, env=self._env())

    def run_ctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([PYTHON, str(self.jd / "cmux-janitorctl"), *args],
                              capture_output=True, text=True, env=self._env())

    def run_uninstall(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/bin/bash", str(self.jd / "uninstall.sh"), *args],
                              capture_output=True, text=True, env=self._env())

    # ---- observations ----

    def log(self) -> str:
        path = self.jd / "janitor.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def guard_log(self) -> str:
        path = self.jd / "guard.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def state(self) -> dict:
        return json.loads((self.jd / "janitor-state.json").read_text(encoding="utf-8"))

    def guard_state(self) -> dict:
        return json.loads((self.jd / "guard-state.json").read_text(encoding="utf-8"))

    def ctl_json(self) -> dict:
        return json.loads(self.run_ctl("status", "--json").stdout)

    def batches(self) -> list[str]:
        if not self.quarantine.exists():
            return []
        return sorted(p.name for p in self.quarantine.iterdir() if p.is_dir())

    def run_id(self) -> str | None:
        """The run_id in the published state, or None when there is no state."""

        path = self.jd / "janitor-state.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("run_id")
        except (OSError, ValueError):
            return None

    def wait_for_run(self, before: str | None, *, timeout: float = 60.0) -> bool:
        """Block until a detached run publishes a new state document.

        ``ctl run --manual`` launches the sweep with ``start_new_session=True``
        and returns as soon as the child is spawned, so asserting on the log
        straight after it returns is a race.

        The marker is a change in ``run_id``, not a log substring: the script
        calls ``publish_state`` only at its terminal points (cmux-janitor.sh:249
        error, :488 and :541 early exits, :675 full run) and ``RUN_ID`` carries
        the pid (:223), so a new value means one more run finished.  The final
        summary line would miss the two early-exit paths, which never emit
        ``safety_complete=``.
        """

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.run_id() not in (None, before):
                return True
            time.sleep(0.1)
        return False

    def tree_snapshot(self) -> dict[str, tuple[int, int, int]]:
        """Every path under the managed trees with inode, mtime_ns and size."""
        snap: dict[str, tuple[int, int, int]] = {}
        for root in (self.cm, self.quarantine):
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                st = path.lstat()
                snap[str(path)] = (st.st_ino, st.st_mtime_ns, st.st_size)
        return snap


class JanitorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.box = Sandbox()

    def tearDown(self) -> None:
        subprocess.run(["/bin/rm", "-rf", str(self.box.root)], check=False)


class PreviewContractTests(JanitorTestCase):
    def snapshot(self):
        result = {}
        for path in sorted(self.box.home.rglob("*")):
            info = path.lstat()
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            except PermissionError:
                digest = "unreadable"
            result[str(path.relative_to(self.box.home))] = (
                info.st_ino, info.st_mode, info.st_mtime_ns, digest,
            )
        return result

    def preview(self, *, ctl=False, expected_rc=0):
        before = self.snapshot()
        result = (self.box.run_ctl("preview", "--json") if ctl else
                  self.box.run_janitor("--preview", "--json"))
        self.assertEqual(result.returncode, expected_rc, result.stderr)
        self.assertEqual(before, self.snapshot(), "preview changed persistent files")
        doc = json.loads(result.stdout)
        self.assertTrue(doc["read_only"])
        return doc

    def test_cap_and_live_protection_use_final_not_raw_count(self):
        self.box.set_config(MODE="apply", MAX_ITEMS_PER_RUN=1)
        self.box.aged_sb(2)
        self.box.aged_staging(3)
        self.box.set_live_ids([UUIDS[0]])
        counts = self.preview()["counts"]
        self.assertEqual((counts["raw_sb"], counts["raw_staging"]), (2, 3))
        self.assertEqual((counts["protected_directories"], counts["eligible"]), (1, 4))
        self.assertEqual((counts["selected"], counts["would_dispose"]), (1, 1))
        self.assertEqual(counts["would_dispose_sb"] + counts["would_dispose_staging"], 1)

    def test_paused_preview_reports_unknown_without_writing(self):
        (self.box.jd / "DISABLED").write_text("operator pause\n")
        doc = self.preview()
        self.assertEqual(doc["reason"], "disabled")
        self.assertIsNone(doc["counts"])
        self.assertFalse(doc["scanned"])

    def test_missing_store_reports_unknown_without_creating_directories(self):
        self.box.cm.rename(self.box.root / "absent-cmux")
        doc = self.preview()
        self.assertEqual(doc["reason"], "store_missing")
        self.assertIsNone(doc["counts"])

    def test_moving_item_is_not_disposable(self):
        self.box.aged_sb(1)
        # An injected second sample differs, while all real managed bytes stay fixed.
        script = self.box.jd / "cmux-janitor.sh"
        source = script.read_text()
        old = 'stamp_of() { "$STAT" -f%m "$1" 2>/dev/null || echo x; }'
        self.assertIn(old, source)
        script.write_text(source.replace(old, 'stamp_of() { echo x; }'))
        counts = self.preview()["counts"]
        self.assertEqual((counts["selected"], counts["would_dispose"], counts["skipped_moving"]), (1, 0, 1))

    def test_failed_enumeration_is_unknown_not_an_empty_candidate_set(self):
        script = self.box.jd / "cmux-janitor.sh"
        source = script.read_text()
        self.assertIn("FIND=/usr/bin/find", source)
        script.write_text(source.replace("FIND=/usr/bin/find", "FIND=/usr/bin/false"))
        doc = self.preview(expected_rc=1)
        self.assertEqual(doc["reason"], "candidate_scan_unverified")
        self.assertIsNone(doc["counts"])

    def test_invalid_config_is_nonzero_and_read_only(self):
        self.box.set_config(USE_QUARANTINE=0)
        doc = self.preview(expected_rc=1)
        self.assertEqual(doc["reason"], "invalid_config")
        self.assertIsNone(doc["counts"])

    def test_empty_preview_counts_expiry_in_separate_units(self):
        self.box.set_config(MODE="apply", QUARANTINE_KEEP_HOURS=48)
        self.box.batch("old", sealed_ago_h=72, items=3)
        self.box.batch("fresh", sealed_ago_h=1, items=1)
        doc = self.preview()
        self.assertEqual(doc["counts"]["would_dispose"], 0)
        self.assertEqual(doc["quarantine"], {
            "batch_count": 2, "would_expire_batches": 1, "would_expire_items": 3})

    def test_existing_ambiguous_or_stale_mutex_is_never_reclaimed(self):
        mutex = self.box.jd / ".janitor.mutex"
        mutex.mkdir()
        for content in (None, "corrupt", '{"pid":99999999,"process_start":"dead"}'):
            if content is not None:
                (mutex / "owner").write_text(content)
            os.utime(mutex, (1, 1))
            with self.subTest(content=content):
                doc = self.preview()
                self.assertEqual(doc["reason"], "busy")
                self.assertIsNone(doc["counts"])

    def test_fresh_content_is_selected_but_not_disposable(self):
        self.box.set_config(MODE="apply")
        directory = self.box.aged_staging(1)[0]
        os.utime(directory / "payload.txt", None)
        counts = self.preview()["counts"]
        self.assertEqual((counts["selected"], counts["would_dispose"], counts["skipped_fresh"]), (1, 0, 1))

    def test_open_handle_is_selected_but_not_disposable(self):
        self.box.set_config(MODE="apply")
        path = self.box.aged_sb(1)[0]
        with path.open("rb"):
            counts = self.preview()["counts"]
        self.assertEqual((counts["selected"], counts["would_dispose"], counts["skipped_held"]), (1, 0, 1))

    def test_missing_live_store_blocks_only_staging(self):
        self.box.aged_staging(1)
        self.box.aged_sb(1)
        self.box.live.unlink()
        doc = self.preview()
        self.assertEqual(doc["reason"], "live_store_missing")
        self.assertFalse(doc["safety_complete"])
        self.assertEqual(doc["counts"]["would_dispose_staging"], 0)
        self.assertEqual(doc["counts"]["would_dispose_sb"], 1)

    def test_unreadable_live_store_is_not_an_empty_store(self):
        self.box.aged_staging(1)
        self.box.live.chmod(0)
        try:
            if os.access(self.box.live, os.R_OK):
                self.skipTest("caller can read mode-000 files")
            doc = self.preview()
            self.assertEqual(doc["reason"], "live_store_unreadable")
            self.assertEqual(doc["counts"]["would_dispose"], 0)
        finally:
            self.box.live.chmod(0o600)

    def test_empty_live_store_is_a_verified_no_reference_state(self):
        self.box.aged_staging(1)
        self.box.live.write_bytes(b"")
        doc = self.preview()
        self.assertTrue(doc["safety_complete"])
        self.assertEqual(doc["counts"]["would_dispose_staging"], 1)

    def test_nonempty_unparseable_live_store_fails_closed(self):
        self.box.aged_staging(1)
        self.box.live.write_text("unparseable")
        doc = self.preview()
        self.assertFalse(doc["safety_complete"])
        self.assertEqual(doc["counts"]["would_dispose"], 0)

    def test_controller_preview_uses_synchronous_read_only_script(self):
        self.box.set_config(MODE="apply", MAX_ITEMS_PER_RUN=1)
        self.box.aged_sb(2)
        self.assertEqual(self.preview(ctl=True)["counts"]["would_dispose"], 1)

    def test_controller_propagates_invalid_config_status(self):
        self.box.set_config(MODE="invalid")
        self.assertEqual(self.preview(ctl=True, expected_rc=1)["reason"], "invalid_config")

    def test_json_without_preview_is_usage_error_and_does_not_run(self):
        before = self.snapshot()
        result = self.box.run_janitor("--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(before, self.snapshot())


class ConfigContractTests(JanitorTestCase):
    def test_authoritative_config_declares_only_keep_hours(self):
        text = (SRC / "config.env").read_text(encoding="utf-8")
        self.assertIn("QUARANTINE_KEEP_HOURS=3", text)
        # The retired name may be documented in a comment but never assigned.
        # The fail-closed check is anchored (cmux-janitor.sh:98 greps for
        # ^[[:space:]]*QUARANTINE_RETAIN_HOURS=), so a '#'-prefixed mention
        # cannot trip it; asserting the bare substring was stricter than the
        # real contract and rejected the file's own explanatory comment.
        for line in text.splitlines():
            self.assertFalse(
                re.match(r"^\s*QUARANTINE_RETAIN_HOURS=", line),
                f"retired key assigned, not merely documented: {line!r}")

    def test_retired_retention_key_fails_closed(self):
        self.box.set_config(QUARANTINE_RETAIN_HOURS=24)
        self.box.aged_staging(1)
        result = self.box.run_janitor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("ABORT config invalid", self.box.log())
        self.assertIn("QUARANTINE_RETAIN_HOURS", self.box.log())
        self.assertEqual(self.box.batches(), [], "nothing may move on a bad config")

    def test_illegal_values_fail_closed_without_touching_files(self):
        for value in ("-1", "0", "abc", "48.5", ""):
            with self.subTest(keep_hours=value):
                box = Sandbox()
                try:
                    box.set_config(QUARANTINE_KEEP_HOURS=value)
                    box.aged_staging(1)
                    before = box.tree_snapshot()
                    result = box.run_janitor()
                    self.assertEqual(result.returncode, 1, f"KEEP_HOURS={value!r}")
                    self.assertIn("ABORT config invalid", box.log())
                    self.assertEqual(before, box.tree_snapshot())
                finally:
                    subprocess.run(["/bin/rm", "-rf", str(box.root)], check=False)

    def test_keep_hours_48_is_what_expiry_actually_uses(self):
        self.box.set_config(QUARANTINE_KEEP_HOURS=48)
        # The original defect: config said 48, the script read a different name
        # and used 24.  A batch at 30h must therefore survive.
        self.box.batch("20260827-000000", sealed_ago_h=30)
        self.box.aged_staging(1)
        self.box.run_janitor()
        self.assertIn("20260827-000000", self.box.batches())
        self.assertEqual(self.box.state()["quarantine"]["keep_hours"], 48)

    def test_batch_past_48h_is_expired(self):
        self.box.batch("20260826-000000", sealed_ago_h=50)
        self.box.aged_staging(1)
        self.box.run_janitor()
        self.assertNotIn("20260826-000000", self.box.batches())
        self.assertGreaterEqual(self.box.state()["counts"]["expired_batches"], 1)


class DryModeTests(JanitorTestCase):
    def test_dry_makes_no_change_to_target_tree_or_quarantine(self):
        self.box.set_config(MODE="dry")
        self.box.aged_staging(3)
        self.box.aged_sb(2)
        self.box.batch("20260826-000000", sealed_ago_h=99)  # past any retention
        before = self.box.tree_snapshot()

        result = self.box.run_janitor()
        self.assertEqual(result.returncode, 0)
        after = self.box.tree_snapshot()

        self.assertEqual(before, after, "dry mode mutated the managed trees")
        self.assertIn("20260826-000000", self.box.batches(),
                      "dry mode expired a quarantine batch")

    def test_dry_reports_would_counts_and_zero_disposed(self):
        self.box.set_config(MODE="dry")
        self.box.aged_staging(3)
        self.box.batch("20260826-000000", sealed_ago_h=99)
        self.box.run_janitor()

        counts = self.box.state()["counts"]
        self.assertEqual(counts["disposed"], 0)
        self.assertEqual(counts["expired_batches"], 0)
        self.assertEqual(counts["expired_items"], 0)
        self.assertEqual(counts["would_dispose"], 3)
        self.assertGreaterEqual(counts["would_expire_batches"], 1)
        self.assertIn("disposed=0", self.box.log())

    def test_dry_creates_no_quarantine_directory_when_absent(self):
        self.box.set_config(MODE="dry")
        self.box.aged_staging(2)
        self.box.run_janitor()
        self.assertFalse(self.box.quarantine.exists(),
                         "dry mode created a quarantine dir (guard R7 would trip)")

    def test_preview_apply_is_read_only_before_disposal(self):
        """Preview must not dispose candidates or expire quarantine batches."""
        self.box.set_config(MODE="apply")
        self.box.aged_staging(2)
        self.box.aged_sb(1)
        self.box.batch("20260826-000000", sealed_ago_h=99)
        before = self.box.tree_snapshot()

        result = self.box.run_janitor("--preview", "--verbose")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(before, self.box.tree_snapshot(),
                         "apply preview mutated a managed tree")
        self.assertIn("20260826-000000", self.box.batches(),
                      "apply preview expired a quarantine batch")
        self.assertIn("Janitor Preview", result.stdout)
        self.assertIn("Would Dispose", result.stdout)
        for name in ("janitor-state.json", "metrics.jsonl", "janitor.log"):
            self.assertFalse((self.box.jd / name).exists(),
                             f"preview created side-effect file {name}")


class GlobalCapTests(JanitorTestCase):
    def test_two_kinds_share_one_global_cap(self):
        self.box.set_config(MAX_ITEMS_PER_RUN=10)
        self.box.aged_staging(12)
        self.box.aged_sb(12)
        self.box.run_janitor()
        counts = self.box.state()["counts"]
        self.assertEqual(counts["selected"], 10)
        self.assertEqual(counts["disposed"], 10,
                         "the cap must span both kinds, not apply per kind")

    def test_selection_is_oldest_first(self):
        self.box.set_config(MAX_ITEMS_PER_RUN=2)
        old = self.box.aged_staging(1, minutes=600)[0]
        mid = self.box.aged_staging(2, minutes=400)[-1]
        young = self.box.aged_staging(3, minutes=200)[-1]
        self.box.run_janitor()
        self.assertFalse(old.exists(), "oldest candidate was not selected")
        self.assertFalse(mid.exists(), "second-oldest candidate was not selected")
        self.assertTrue(young.exists(), "newest candidate should have waited")

    def test_cap_of_one_still_progresses(self):
        self.box.set_config(MAX_ITEMS_PER_RUN=1)
        self.box.aged_staging(4)
        self.box.run_janitor()
        self.assertEqual(self.box.state()["counts"]["selected"], 1)


class SafetyVersusPrecisionTests(JanitorTestCase):
    def test_safety_complete_true_on_a_clean_run(self):
        self.box.aged_staging(2)
        self.box.run_janitor()
        self.assertTrue(self.box.state()["safety_complete"])

    def test_parse_gap_sets_safety_complete_false_and_disposes_nothing(self):
        # Non-empty store from which zero ids extract == cannot prove a staging
        # dir is unreferenced.  Must fail closed.
        self.box.live.write_text('{"entries": [{"other": "value"}]}', encoding="utf-8")
        staged = self.box.aged_staging(3)
        self.box.run_janitor()
        state = self.box.state()
        self.assertFalse(state["safety_complete"])
        self.assertEqual(state["counts"]["disposed"], 0)
        for directory in staged:
            self.assertTrue(directory.exists())
        self.assertIn("ABORT-STAGING", self.box.log())

    def test_estimated_bytes_never_block_disposal(self):
        # Byte totals are display-only.  Past the measurement budget the total
        # is flagged estimated, and disposal must still happen (R2-1).
        self.box.set_config(MAX_ITEMS_PER_RUN=80)
        self.box.aged_staging(70)
        self.box.run_janitor()
        state = self.box.state()
        self.assertEqual(state["selected_bytes"]["precision"], "estimated")
        self.assertTrue(state["safety_complete"])
        self.assertEqual(state["counts"]["disposed"], 70,
                         "estimated bytes must not gate disposal")

    def test_small_run_reports_exact_bytes(self):
        self.box.aged_staging(2)
        self.box.run_janitor()
        selected = self.box.state()["selected_bytes"]
        self.assertEqual(selected["precision"], "exact")
        self.assertGreater(selected["value"], 0)

    def test_live_referenced_staging_is_protected(self):
        keep = self.box.aged_staging(2)[0]
        self.box.set_live_ids([keep.name])
        self.box.run_janitor()
        self.assertTrue(keep.exists(), "a referenced snapshot was disposed")
        self.assertGreaterEqual(self.box.state()["counts"]["protected_ids"], 1)

    def test_young_and_non_uuid_staging_are_not_candidates(self):
        young = self.box.young_staging(2)
        stray = self.box.staging / "not-a-uuid"
        stray.mkdir()
        old = time.time() - 7200
        os.utime(stray, (old, old))
        self.box.aged_staging(1)
        self.box.run_janitor()
        for directory in young:
            self.assertTrue(directory.exists())
        self.assertTrue(stray.exists(), "a non-UUID directory was treated as junk")


class SealedBatchTests(JanitorTestCase):
    def test_apply_seals_batch_with_verifiable_metadata(self):
        self.box.aged_staging(2)
        self.box.run_janitor()
        batches = self.box.batches()
        self.assertEqual(len(batches), 1)
        meta_path = self.box.quarantine / batches[0] / ".janitor-batch.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta["item_count"], 2)
        self.assertIn("sealed_at", meta)
        self.assertGreaterEqual(meta["sealed_at_epoch"], meta["created_at_epoch"])

    def test_no_incomplete_batch_survives_a_clean_run(self):
        self.box.aged_staging(2)
        self.box.run_janitor()
        leftovers = [p.name for p in self.box.quarantine.iterdir()
                     if p.name.startswith(".incomplete-")]
        self.assertEqual(leftovers, [])

    def test_batch_without_metadata_is_never_expired(self):
        self.box.batch("20260101-000000", sealed_ago_h=999, metadata=False)
        self.box.aged_staging(1)
        self.box.run_janitor()
        self.assertIn("20260101-000000", self.box.batches(),
                      "a batch with no verifiable metadata must not be expired")

    def test_batch_with_corrupt_metadata_is_never_expired(self):
        directory = self.box.batch("20260102-000000", sealed_ago_h=999)
        (directory / ".janitor-batch.json").write_text("{not json", encoding="utf-8")
        self.box.aged_staging(1)
        self.box.run_janitor()
        self.assertIn("20260102-000000", self.box.batches())

    def test_sealed_at_survives_a_directory_touch(self):
        # uninstall.sh moving items out bumps the directory mtime.  Expiry must
        # follow recorded sealed_at, not the mtime, so a touched old batch still
        # expires and a touched young one still does not.
        directory = self.box.batch("20260826-010101", sealed_ago_h=99)
        os.utime(directory, None)  # now
        self.box.aged_staging(1)
        self.box.run_janitor()
        self.assertNotIn("20260826-010101", self.box.batches(),
                         "expiry must use sealed_at, not the directory mtime")

    def test_next_expiry_is_sealed_at_plus_keep_hours(self):
        self.box.aged_staging(1)
        self.box.run_janitor()
        quarantine = self.box.state()["quarantine"]
        oldest = time.strptime(quarantine["oldest_sealed_at"], "%Y-%m-%dT%H:%M:%SZ")
        nxt = time.strptime(quarantine["next_expiry_at"], "%Y-%m-%dT%H:%M:%SZ")
        delta = time.mktime(nxt) - time.mktime(oldest)
        self.assertAlmostEqual(delta, 3 * 3600, delta=90)


class MutexTests(JanitorTestCase):
    def _own_start(self) -> str:
        out = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())],
                             capture_output=True, text=True).stdout
        return " ".join(out.split())

    def _owner(self, mutex: Path, *, pid: int, start: str, run_id: str) -> None:
        # Matches write_mutex_owner(): JSON with pid/process_start/run_id.
        (mutex / "owner").write_text(json.dumps({
            "pid": pid, "process_start": start, "run_id": run_id,
        }) + "\n", encoding="utf-8")

    def test_live_owner_is_busy_no_matter_how_old(self):
        mutex = self.box.jd / ".janitor.mutex"
        mutex.mkdir()
        self._owner(mutex, pid=os.getpid(), start=self._own_start(), run_id="other")
        old = time.time() - 90 * 60
        os.utime(mutex, (old, old))
        self.box.aged_staging(1)
        result = self.box.run_janitor()
        self.assertEqual(result.returncode, 0)
        self.assertIn("SKIP another run in progress", self.box.log())
        self.assertIn("live owner", self.box.log())
        self.assertTrue(mutex.exists(), "a live owner's mutex was stolen")
        self.assertEqual(self.box.batches(), [], "work ran while another run held the lock")

    def test_dead_owner_is_reclaimed(self):
        mutex = self.box.jd / ".janitor.mutex"
        mutex.mkdir()
        self._owner(mutex, pid=99999999, start="Thu Jan 1 00:00:00 2026", run_id="dead")
        old = time.time() - 90 * 60
        os.utime(mutex, (old, old))
        self.box.aged_staging(1)
        self.box.run_janitor()
        self.assertIn("cleared stale mutex", self.box.log())
        self.assertIn("is dead", self.box.log())
        self.assertEqual(self.box.state()["counts"]["disposed"], 1)

    def test_pid_reuse_with_different_start_is_reclaimed(self):
        mutex = self.box.jd / ".janitor.mutex"
        mutex.mkdir()
        # Our pid is alive, but the recorded start stamp is not ours: this is
        # exactly the pid-reuse case a bare kill -0 would misread as live.
        self._owner(mutex, pid=os.getpid(), start="Thu Jan 1 00:00:00 1990",
                    run_id="reused")
        old = time.time() - 90 * 60
        os.utime(mutex, (old, old))
        self.box.aged_staging(1)
        self.box.run_janitor()
        self.assertIn("cleared stale mutex", self.box.log())
        self.assertIn("reused", self.box.log())

    def test_missing_owner_inside_grace_is_busy(self):
        mutex = self.box.jd / ".janitor.mutex"
        mutex.mkdir()  # fresh: covers the crash window after mkdir
        self.box.aged_staging(1)
        result = self.box.run_janitor()
        self.assertEqual(result.returncode, 0)
        self.assertIn("SKIP another run in progress", self.box.log())
        self.assertIn("inside", self.box.log())
        self.assertTrue(mutex.exists())
        self.assertEqual(self.box.batches(), [])

    def test_missing_owner_past_grace_is_reclaimed(self):
        mutex = self.box.jd / ".janitor.mutex"
        mutex.mkdir()
        old = time.time() - 10 * 60  # grace is 5 min
        os.utime(mutex, (old, old))
        self.box.aged_staging(1)
        self.box.run_janitor()
        self.assertIn("cleared stale mutex", self.box.log())
        self.assertIn("owner unknown", self.box.log())

    def test_corrupt_owner_past_grace_is_reclaimed(self):
        mutex = self.box.jd / ".janitor.mutex"
        mutex.mkdir()
        (mutex / "owner").write_text("garbage, not json", encoding="utf-8")
        old = time.time() - 10 * 60
        os.utime(mutex, (old, old))
        self.box.aged_staging(1)
        self.box.run_janitor()
        self.assertIn("cleared stale mutex", self.box.log())

    def test_mutex_is_released_after_a_normal_run(self):
        self.box.aged_staging(1)
        self.box.run_janitor()
        self.assertFalse((self.box.jd / ".janitor.mutex").exists())

    def test_trap_does_not_delete_a_successors_lock(self):
        # release_mutex() only removes a mutex whose owner run_id is still ours.
        script = (SRC / "cmux-janitor.sh").read_text(encoding="utf-8")
        self.assertIn('[ "$held" = "$RUN_ID" ]', script)


class GuardTests(JanitorTestCase):
    def _baseline(self) -> dict[str, str]:
        text = (self.box.jd / "guard.baseline").read_text(encoding="utf-8")
        out = {}
        for line in text.splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                out[key.strip()] = value.strip()
        return out

    def test_first_run_writes_schema_2_baseline(self):
        result = self.box.run_guard()
        self.assertEqual(result.returncode, 0)
        base = self._baseline()
        self.assertEqual(base["BASE_SCHEMA"], "2")
        self.assertEqual(base["BASE_MODE"], "apply")
        self.assertTrue(base["BASE_Q_FINGERPRINT"])

    def test_healthy_check_is_silent_and_publishes_state(self):
        self.box.run_guard()
        result = self.box.run_guard()
        self.assertEqual(result.returncode, 0)
        state = self.box.guard_state()
        self.assertEqual(state["health"], "healthy")
        # The document carries a single `reason` string, not a violations list
        # (guard.sh:180); a healthy check leaves it empty.
        self.assertEqual(state["reason"], "")
        self.assertTrue(state["mode_matches_baseline"])
        self.assertTrue(state["quarantine_fingerprint_matches"])
        self.assertFalse((self.box.jd / "GUARD_TRIPPED").exists())

    def test_mode_drift_trips_and_writes_disabled(self):
        self.box.run_guard()
        self.box.set_config(MODE="dry")
        result = self.box.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.box.jd / "GUARD_TRIPPED").exists())
        self.assertTrue((self.box.jd / "DISABLED").exists(),
                        "a trip must stop the janitor, not merely report")
        self.assertIn("R6", (self.box.jd / "GUARD_TRIPPED").read_text(encoding="utf-8"))

    def test_dry_with_unchanged_quarantine_does_not_trip(self):
        # The old R7 tripped merely because a quarantine dir existed, which made
        # a safe apply->dry rollback impossible.
        self.box.batch("20260827-120000", sealed_ago_h=2)
        self.box.set_config(MODE="dry")
        self.box.run_guard()  # baseline in dry, with quarantine present
        result = self.box.run_guard()
        self.assertEqual(result.returncode, 0, self.box.guard_log())
        self.assertFalse((self.box.jd / "GUARD_TRIPPED").exists())

    def test_dry_with_changed_quarantine_trips(self):
        self.box.batch("20260827-120000", sealed_ago_h=2)
        self.box.set_config(MODE="dry")
        self.box.run_guard()
        self.box.batch("20260827-130000", sealed_ago_h=1)  # movement under dry
        result = self.box.run_guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("R7", (self.box.jd / "GUARD_TRIPPED").read_text(encoding="utf-8"))

    def test_schema_1_baseline_refuses_to_judge_without_tripping(self):
        self.box.run_guard()
        base = self.box.jd / "guard.baseline"
        base.write_text("\n".join([
            "BASE_LOCK_SIZE=0", "BASE_LOCK_MTIME=1", "BASE_PUB_COUNT=160",
            "BASE_CFG_COUNT=0", "BASE_HOOKS_COUNT=0", "BASE_MODE=apply",
        ]) + "\n", encoding="utf-8")
        result = self.box.run_guard()
        self.assertEqual(result.returncode, 3)
        self.assertFalse((self.box.jd / "GUARD_TRIPPED").exists(),
                         "an operator problem must not be reported as a violation")
        self.assertEqual(self.box.guard_state()["health"], "schema_mismatch")

    def test_plain_rearm_refuses_mode_drift(self):
        self.box.run_guard()
        self.box.set_config(MODE="dry")
        result = self.box.run_guard("--rearm")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._baseline()["BASE_MODE"], "apply",
                         "plain --rearm must not silently bless drift")

    def test_accept_mode_requires_disabled(self):
        self.box.run_guard()
        self.box.set_config(MODE="dry")
        result = self.box.run_guard("--rearm", "--accept-mode", "dry")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DISABLED", result.stdout + result.stderr)

    def test_accept_mode_completes_a_deliberate_switch(self):
        self.box.run_guard()
        (self.box.jd / "DISABLED").touch()
        self.box.set_config(MODE="dry")
        result = self.box.run_guard("--rearm", "--accept-mode", "dry")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        base = self._baseline()
        self.assertEqual(base["BASE_MODE"], "dry")
        self.assertEqual(base["BASE_SCHEMA"], "2")
        self.assertTrue(base["BASE_Q_FINGERPRINT"])
        self.assertTrue((self.box.jd / "DISABLED").exists(),
                        "rearm must never clear the kill switch")

    def test_accept_mode_must_match_config(self):
        self.box.run_guard()
        (self.box.jd / "DISABLED").touch()
        self.box.set_config(MODE="dry")
        result = self.box.run_guard("--rearm", "--accept-mode", "apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._baseline()["BASE_MODE"], "apply")

    def test_argument_matrix_rejects_unknown_and_misplaced_flags(self):
        cases = [
            (["--bogus"], "unknown argument"),
            (["--accept-mode"], "needs dry|apply"),
            (["--accept-mode", "maybe"], "must be dry or apply"),
            (["--accept-mode", "dry"], "only valid with --rearm"),
            (["--status", "--accept-mode", "dry"], "only valid with --rearm"),
        ]
        for argv, needle in cases:
            with self.subTest(argv=argv):
                result = self.box.run_guard(*argv)
                self.assertEqual(result.returncode, 2)
                self.assertIn(needle, result.stdout + result.stderr)

    def test_tripped_guard_stays_tripped(self):
        self.box.run_guard()
        self.box.set_config(MODE="dry")
        self.box.run_guard()
        before = (self.box.jd / "GUARD_TRIPPED").read_text(encoding="utf-8")
        self.box.run_guard()
        self.assertEqual(before, (self.box.jd / "GUARD_TRIPPED").read_text(encoding="utf-8"))
        self.assertEqual(self.box.guard_state()["health"], "tripped")

    def test_guard_fails_closed_when_its_directory_is_unusable(self):
        env = dict(os.environ)
        env["HOME"] = "/nonexistent-guard-home"
        result = subprocess.run(["/bin/bash", str(self.box.jd / "guard.sh")],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 3,
                         "an unwritable guard dir must not report health")


class KillSwitchTests(JanitorTestCase):
    def test_disabled_blocks_scheduled_runs_at_gate_zero(self):
        (self.box.jd / "DISABLED").touch()
        self.box.aged_staging(3)
        before = self.box.tree_snapshot()
        result = self.box.run_janitor("--scheduled")
        self.assertEqual(result.returncode, 0)
        self.assertIn("SKIP disabled-by-user", self.box.log())
        self.assertEqual(before, self.box.tree_snapshot())
        self.assertFalse((self.box.jd / ".janitor.mutex").exists(),
                         "GATE 0 must precede the mutex")

    def test_disabled_blocks_manual_runs_too(self):
        (self.box.jd / "DISABLED").touch()
        self.box.aged_staging(3)
        before = self.box.tree_snapshot()
        result = self.box.run_janitor("--manual")
        self.assertEqual(result.returncode, 0)
        self.assertIn("trigger=manual", self.box.log())
        self.assertEqual(before, self.box.tree_snapshot())

    def test_disabled_does_not_expire_quarantine(self):
        (self.box.jd / "DISABLED").touch()
        self.box.batch("20260101-000000", sealed_ago_h=999)
        self.box.run_janitor()
        self.assertIn("20260101-000000", self.box.batches())

    def test_unknown_trigger_is_rejected(self):
        result = self.box.run_janitor("--inspect")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stdout + result.stderr)

    def test_no_inspect_trigger_exists(self):
        script = (SRC / "cmux-janitor.sh").read_text(encoding="utf-8")
        for line in script.splitlines():
            if line.strip().startswith("#"):
                continue
            self.assertNotIn("TRIGGER=inspect", line)


QUARANTINE_AGGREGATE_FIELDS = [
    "age_sec", "batch_count", "bytes", "keep_hours", "next_expiry_at", "observed_at", "oldest_sealed_at", "stale",
]


def assert_status_is_bounded(case, payload, batch_names, home=None):
    """Assert the status document leaks no batch inventory.

    Structure-aware on purpose.  The earlier version asserted each batch name was
    absent from the *entire* raw document, which failed ~10% of runs (measured: 2
    of 20) on a payload that leaked nothing: a batch is named ``%Y%m%d-%H%M%S``
    and the public ``run_id`` is ``%Y%m%d-%H%M%S-<pid>``, so a batch sealed in the
    same second as the run started makes the name a legitimate *prefix* of the run
    id.  The run id is public by design and its timestamp is its own.

    What actually constitutes a leak is structure: a batch list, a path, or any
    batch name inside the bounded quarantine object.  Those are checked here, and
    absolute paths are still checked across the whole document.
    """

    quarantine = payload["quarantine"]
    case.assertLessEqual(set(quarantine), set(QUARANTINE_AGGREGATE_FIELDS))
    case.assertLessEqual({"batch_count", "bytes", "keep_hours", "next_expiry_at", "oldest_sealed_at"}, set(quarantine))
    case.assertNotIn("batches", quarantine)

    # Only scalars plus the documented ``bytes`` measure.  A list is how a batch
    # inventory would arrive; a nested dict is how a path map would.
    for key, value in quarantine.items():
        if key == "bytes":
            case.assertEqual(sorted(value), ["precision", "value"])
            continue
        case.assertNotIsInstance(
            value, (list, dict), f"quarantine.{key} must be a scalar, got {type(value).__name__}")

    # No batch name anywhere inside the bounded object.
    bounded_raw = json.dumps(quarantine)
    for name in batch_names:
        case.assertNotIn(name, bounded_raw, f"batch name {name} leaked into the quarantine object")

    # No absolute path anywhere in the document.
    raw = json.dumps(payload)
    if home is not None:
        case.assertNotIn(str(home), raw)
    case.assertNotIn("/Users/", raw)
    case.assertNotIn("/.cmuxterm-janitor-quarantine", raw)


class CtlTests(JanitorTestCase):
    def test_status_json_is_bounded_and_leaks_no_names(self):
        self.box.aged_staging(2)
        self.box.run_janitor()
        self.box.run_guard()
        raw = self.box.run_ctl("status", "--json").stdout
        payload = json.loads(raw)

        assert_status_is_bounded(self, payload, self.box.batches(), home=self.box.home)
        self.assertNotIn(str(self.box.home), raw)
        self.assertNotIn("/Users/", raw)

        # The run id stays the documented shape, which is what makes the
        # prefix collision above harmless rather than an unbounded field.
        run_id = payload["janitor"]["run_id"]
        if run_id is not None:
            self.assertRegex(run_id, r"^\d{8}-\d{6}-\d+$")

    def test_a_run_id_sharing_a_batch_timestamp_is_not_a_leak(self):
        """Deterministic fixture for the flake this replaced.

        Same-second seal: the batch name is a prefix of the run id.  No sleeping,
        no real janitor run -- the exact colliding payload, every time.
        """

        payload = {
            "janitor": {"run_id": "20260829-042344-41615", "trigger": "scheduled"},
            "quarantine": {
                "batch_count": 1,
                "bytes": {"value": 12288, "precision": "exact"},
                "oldest_sealed_at": "2026-08-28T22:53:44Z",
                "next_expiry_at": "2026-08-30T22:53:44Z",
                "keep_hours": 48,
            },
        }
        # The colliding name must be accepted.
        assert_status_is_bounded(self, payload, ["20260829-042344"])
        self.assertIn("20260829-042344", payload["janitor"]["run_id"])

    def test_an_actual_batch_inventory_still_fails(self):
        """The other half: the relaxed check must still catch a real leak."""

        def payload_with(quarantine_extra):
            return {
                "janitor": {"run_id": "20260829-042344-41615", "trigger": "scheduled"},
                "quarantine": {
                    "batch_count": 1,
                    "bytes": {"value": 12288, "precision": "exact"},
                    "oldest_sealed_at": "2026-08-28T22:53:44Z",
                    "next_expiry_at": "2026-08-30T22:53:44Z",
                    "keep_hours": 48,
                    **quarantine_extra,
                },
            }

        # An extra ``batches`` list is the canonical leak.
        with self.assertRaises(AssertionError):
            assert_status_is_bounded(
                self, payload_with({"batches": ["20260829-042344"]}), ["20260829-042344"])

        # A differently-named list is still a list.
        with self.assertRaises(AssertionError):
            assert_status_is_bounded(
                self, payload_with({"inventory": ["20260829-042344"]}), ["20260829-042344"])

        # A batch name smuggled into a scalar aggregate field.
        with self.assertRaises(AssertionError):
            assert_status_is_bounded(
                self, payload_with({"oldest_sealed_at": "20260829-042344"}), ["20260829-042344"])

        # An absolute quarantine path anywhere in the document.
        leaky = payload_with({})
        leaky["janitor"]["error"] = "/Users/someone/.cmuxterm-janitor-quarantine/20260829-042344"
        with self.assertRaises(AssertionError):
            assert_status_is_bounded(self, leaky, ["20260829-042344"])

    def test_status_reports_paused_and_staleness(self):
        self.box.aged_staging(1)
        self.box.run_janitor()
        (self.box.jd / "DISABLED").touch()
        payload = self.box.ctl_json()
        self.assertTrue(payload["control"]["paused"])
        self.assertIsNotNone(payload["janitor"]["age_sec"])
        # Human output is the default; there is no --format flag (only --json
        # and --pretty exist), and passing one would exit 2 on argparse.
        self.assertIn("已暂停", self.box.run_ctl("status").stdout)

    def test_missing_janitor_dir_fails_closed(self):
        env = dict(os.environ)
        env["HOME"] = "/nonexistent-ctl-home"
        result = subprocess.run([PYTHON, str(self.box.jd / "cmux-janitorctl"),
                                 "status", "--json"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 3)
        # The fail-closed envelope goes to stderr so it can never be mistaken
        # for a valid status document on stdout (cmux-janitorctl:475).
        self.assertIn("fail_closed", result.stderr)

    def test_bad_json_state_is_invalid_not_zero(self):
        self.box.aged_staging(1)
        self.box.run_janitor()
        (self.box.jd / "janitor-state.json").write_text(
            '{"counts": {"eligible": NaN}}', encoding="utf-8")
        janitor = self.box.ctl_json()["janitor"]
        self.assertTrue(janitor["invalid"])
        self.assertIsNone(janitor["counts"]["eligible"],
                          "an unreadable number must not render as 0")

    def test_out_of_range_numbers_become_none(self):
        self.box.aged_staging(1)
        self.box.run_janitor()
        for bad in ("true", "-5", '"12"', "null"):
            with self.subTest(value=bad):
                (self.box.jd / "janitor-state.json").write_text(
                    '{"counts": {"eligible": %s}, "selected_bytes": {"value": %s,'
                    ' "precision": "exact"}}' % (bad, bad), encoding="utf-8")
                janitor = self.box.ctl_json()["janitor"]
                self.assertIsNone(janitor["counts"]["eligible"])
                self.assertIsNone(janitor["selected_bytes"]["value"])
                self.assertEqual(janitor["selected_bytes"]["precision"], "unknown")

    def test_precision_survives_the_round_trip(self):
        self.box.set_config(MAX_ITEMS_PER_RUN=80)
        self.box.aged_staging(70)
        self.box.run_janitor()
        self.assertEqual(
            self.box.ctl_json()["janitor"]["selected_bytes"]["precision"], "estimated")

    def test_pause_and_resume_round_trip(self):
        self.box.run_guard()
        self.assertEqual(self.box.run_ctl("pause").returncode, 0)
        self.assertTrue((self.box.jd / "DISABLED").exists())
        self.assertEqual(self.box.run_ctl("resume").returncode, 0)
        self.assertFalse((self.box.jd / "DISABLED").exists())

    def test_resume_refuses_while_guard_tripped(self):
        self.box.run_guard()
        self.box.run_ctl("pause")
        (self.box.jd / "GUARD_TRIPPED").write_text("TRIPPED test\n", encoding="utf-8")
        result = self.box.run_ctl("resume")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.box.jd / "DISABLED").exists(),
                        "resume must not resurrect a tripped janitor")

    def test_resume_refuses_on_mode_baseline_mismatch(self):
        self.box.run_guard()
        self.box.run_ctl("pause")
        self.box.set_config(MODE="dry")  # config now disagrees with BASE_MODE
        result = self.box.run_ctl("resume")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.box.jd / "DISABLED").exists())

    def test_resume_refuses_on_stale_guard_state(self):
        self.box.run_guard()
        self.box.run_ctl("pause")
        state = self.box.jd / "guard-state.json"
        old = time.time() - 3600
        os.utime(state, (old, old))
        payload = json.loads(state.read_text(encoding="utf-8"))
        payload["observed_at"] = "2026-01-01T00:00:00Z"
        state.write_text(json.dumps(payload), encoding="utf-8")
        result = self.box.run_ctl("resume")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.box.jd / "DISABLED").exists())

    def test_manual_run_refused_while_paused(self):
        self.box.run_guard()
        self.box.run_ctl("pause")
        self.box.aged_staging(2)
        before = self.box.tree_snapshot()
        result = self.box.run_ctl("run", "--manual")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(before, self.box.tree_snapshot())

    def test_manual_run_executes_when_healthy(self):
        self.box.run_guard()
        self.box.aged_staging(2)
        before = self.box.run_id()
        result = self.box.run_ctl("run", "--manual")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.box.wait_for_run(before),
                        "detached manual run never published a state document")
        self.assertIn("trigger=manual", self.box.log())
        self.assertEqual(self.box.state()["counts"]["disposed"], 2)

    def test_ctl_never_rearms_guard(self):
        source = (SRC / "cmux-janitorctl").read_text(encoding="utf-8")
        for line in source.splitlines():
            if "--rearm" in line and not line.lstrip().startswith("#"):
                self.assertIn('"', line,
                              "--rearm may only appear inside operator guidance text")

    def test_argument_errors_exit_two(self):
        for argv in ([], ["bogus"], ["run"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.box.run_ctl(*argv).returncode, 2)

    def test_metrics_are_bounded_and_pathless(self):
        self.box.set_config(MAX_ITEMS_PER_RUN=1)
        for _ in range(3):
            self.box.aged_staging(2)
            self.box.run_janitor()
        metrics = self.box.jd / "metrics.jsonl"
        self.assertTrue(metrics.exists())
        lines = [l for l in metrics.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertLessEqual(len(lines), 2048)
        for line in lines:
            record = json.loads(line)
            self.assertIn("run_id", record)
            self.assertNotIn("/Users/", line)
            self.assertNotIn(str(self.box.home), line)


class UninstallRestoreTests(JanitorTestCase):
    def test_metadata_is_not_restored_into_cmuxterm(self):
        self.box.aged_staging(2)
        self.box.run_janitor()
        self.assertEqual(len(self.box.batches()), 1)

        result = self.box.run_uninstall()
        self.assertIn("SKIP (batch metadata)", result.stdout)
        self.assertFalse((self.box.cm / ".janitor-batch.json").exists(),
                         "batch metadata leaked into ~/.cmuxterm")
        self.assertFalse((self.box.cm / "batch.json").exists())

    def test_staging_items_are_restored_to_staging(self):
        made = self.box.aged_staging(2)
        names = [d.name for d in made]
        self.box.run_janitor()
        for directory in made:
            self.assertFalse(directory.exists())
        self.box.run_uninstall()
        for name in names:
            self.assertTrue((self.box.staging / name).exists(),
                            f"{name} was not restored to staging")

    def test_incomplete_batch_is_left_in_place(self):
        incomplete = self.box.quarantine / ".incomplete-testrun"
        incomplete.mkdir(parents=True)
        (incomplete / UUIDS[0]).mkdir()
        result = self.box.run_uninstall()
        self.assertIn("SKIP (incomplete batch)", result.stdout)
        self.assertTrue((incomplete / UUIDS[0]).exists())
        self.assertTrue(self.box.quarantine.exists(),
                        "quarantine deleted while an unsealed batch remained")

    def test_same_name_target_is_stranded_not_overwritten(self):
        made = self.box.aged_staging(1)
        name = made[0].name
        self.box.run_janitor()
        # cmux recreated the same snapshot id after quarantine.
        recreated = self.box.staging / name
        recreated.mkdir(parents=True, exist_ok=True)
        (recreated / "fresh.txt").write_text("keep me", encoding="utf-8")

        result = self.box.run_uninstall()
        self.assertIn("SKIP (target exists)", result.stdout)
        self.assertEqual((recreated / "fresh.txt").read_text(encoding="utf-8"), "keep me")
        self.assertTrue(self.box.quarantine.exists(),
                        "quarantine removed despite a stranded item")

    def test_dry_run_changes_nothing(self):
        self.box.aged_staging(2)
        self.box.run_janitor()
        before = self.box.tree_snapshot()
        result = self.box.run_uninstall("--dry-run")
        self.assertIn("DRY RUN complete", result.stdout)
        self.assertEqual(before, self.box.tree_snapshot())

    def test_uninstall_refuses_to_bootout_a_label_it_did_not_install(self):
        # 2026-08-29T06:39:50Z: this very test file, running under a throwaway
        # HOME, booted the PRODUCTION janitor and guard out of launchd. A launchd
        # label is machine-global and `bootout gui/<uid>/<label>` derives its
        # scope from the label plus the real uid -- neither comes from $HOME, so
        # the sandbox never contained it. The Mac ran unprotected until noticed.
        #
        # Every label this sandbox could see is by construction "not ours" (the
        # sandbox never bootstraps anything), so the script must always refuse.
        result = self.box.run_uninstall()
        self.assertNotIn("DO:    launchctl bootout", result.stdout,
                         "uninstall.sh issued a real bootout from a sandbox HOME")
        # And the refusal must be visible rather than silent, whenever a label of
        # that name happens to be loaded on this machine.
        for label in (f"{LABEL_PREFIX}.cmux-janitor-guard", f"{LABEL_PREFIX}.cmux-janitor"):
            loaded = subprocess.run(
                ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True, text=True).returncode == 0
            if loaded:
                self.assertIn("SKIP-FOREIGN-LABEL", result.stdout,
                              f"{label} is loaded but uninstall.sh did not say it skipped it")

    def test_uninstall_keeps_the_scope_guard(self):
        # Structural, not behavioural: the test above cannot fail on a machine
        # where neither label is loaded, so it would not catch someone deleting
        # the guard. This one reads the script and fails if the bootout calls
        # stop being gated on label_is_ours.
        text = (SRC / "uninstall.sh").read_text(encoding="utf-8")
        self.assertIn("label_is_ours()", text, "the scope guard was removed")
        gated = [line for line in text.splitlines()
                 if line.strip().startswith("if label_is_ours ")]
        self.assertEqual(len(gated), 3,
                         "all bootout blocks must be gated on label_is_ours")
        for line in text.splitlines():
            stripped = line.strip()
            if "launchctl bootout" in stripped and not stripped.startswith(("#", "act ", "say ")):
                self.assertIn('"$DRY" = "0"', stripped,
                              f"ungated bootout call: {stripped}")


class SandboxTests(JanitorTestCase):
    def test_tests_never_reference_the_real_home(self):
        text = Path(__file__).read_text(encoding="utf-8")
        # Assembled from fragments on purpose: spelling the paths out here would
        # plant the very literals this test scans for, and the first draft did
        # exactly that -- it failed by finding its own source line.
        real = Path.home()
        for suffix in (".cmuxterm", ".config/cmux-janitor", ".cmuxterm-janitor-quarantine"):
            self.assertNotIn(str(real / suffix), text)

    def test_sandbox_home_is_isolated(self):
        self.assertNotEqual(str(self.box.home), os.path.expanduser("~"))
        self.assertTrue(str(self.box.home).startswith(tempfile.gettempdir()))


class BoundaryInvariantTests(unittest.TestCase):
    """Execution plane and control plane must stay separate (R4 conditions)."""

    def test_watcher_does_not_import_or_call_the_janitor(self):
        source = (REPO / "cmux_codex_watch.py").read_text(encoding="utf-8")
        for needle in ("cmux-janitorctl", "cmux-janitor.sh", "janitor-state.json",
                       "cmuxterm-janitor-quarantine"):
            self.assertNotIn(needle, source)

    def test_watcher_never_touches_the_cmux_data_tree(self):
        source = (REPO / "cmux_codex_watch.py").read_text(encoding="utf-8")
        for needle in ("agent-turn-diff-baseline-snapshots-staging",
                       "agent-turn-diff-baselines.json"):
            self.assertNotIn(needle, source)

    def test_janitor_and_guard_do_not_depend_on_ccc(self):
        for name in ("cmux-janitor.sh", "guard.sh"):
            source = (SRC / name).read_text(encoding="utf-8")
            for needle in ("cmux_codex_watch", "cmux_supervisor_tui", "ccc"):
                self.assertNotIn(needle, source, f"{name} referenced {needle}")

    def test_tui_cannot_delete_or_move_cmux_files(self):
        source = (REPO / "cmux_supervisor_tui.py").read_text(encoding="utf-8")
        for needle in ("shutil.rmtree", "os.remove", "os.unlink", ".unlink(", "shutil.move"):
            self.assertNotIn(needle, source,
                             "the TUI must never dispose of files itself")

    def test_the_deployed_tree_holds_only_real_artifacts(self):
        """``janitor/src`` is copied verbatim to production, so nothing else may live there.

        Importing ``cmux-janitorctl`` from a probe makes CPython drop a
        ``__pycache__`` beside the source, and the deploy step copies whatever it
        finds.  It is gitignored, so neither ``git status`` nor a review notices;
        it appeared twice in this task from my own probes.  Asserting the
        inventory turns "remember to clean up" into something that fails loudly,
        which is the only version that survives a tired operator.
        """

        # For public release, the source tree contains only templates.
        # Production deployments have rendered plists with actual account names.
        # This test must pass in both contexts.
        base_artifacts = {
            "cmux-janitor.sh", "guard.sh", "status.sh", "uninstall.sh",
            "cmux-janitorctl", "config.env", "janitor_maintenance.py", "expire.sh",
            "ENABLE.command", "DISABLE.command",
            "com.__LABEL_PREFIX__.cmux-janitor.plist.template",
            "com.__LABEL_PREFIX__.cmux-janitor-guard.plist.template",
        }
        # Rendered plists are present in production but not in source/staging
        rendered_plists = {
            f"{LABEL_PREFIX}.cmux-janitor.plist",
            f"{LABEL_PREFIX}.cmux-janitor-guard.plist"
        }

        actual = {path.name for path in SRC.iterdir()}
        # Accept either source tree (templates only) or production tree (templates + rendered)
        self.assertTrue(
            actual == base_artifacts or actual == (base_artifacts | rendered_plists),
            f"janitor/src must contain deployable artifacts. Got: {actual - base_artifacts - rendered_plists}"
        )
        stray = [path.name for path in SRC.iterdir() if not path.is_file()]
        self.assertEqual(stray, [], "no directories may sit in the deployed tree")


class CountProjectionTests(JanitorTestCase):
    """The public contract must carry the counts the janitor actually published.

    R2 P1-1: ``cmux-janitor.sh`` publishes counts under a nested ``counts``
    object, and the controller was reading them from the top level.  Every count
    therefore projected as ``None`` after a perfectly good run, and the panel
    rendered ``?`` for the whole candidate funnel.

    The R1 tests did not catch this because they only asserted that *malformed*
    input projects as ``None`` — which it did, for the wrong reason.  These tests
    assert the positive direction: a valid run must project real numbers.
    """

    # Every public count name, so a key that stops being projected fails here
    # rather than silently reading as "not measured".
    PUBLIC_COUNTS = (
        "raw_sb", "raw_staging", "protected", "eligible", "selected",
        "would_dispose", "disposed", "would_expire_batches",
        "would_expire_items", "expired_batches", "expired_items",
        "skipped_moving", "skipped_fresh", "skipped_held", "skipped_outside",
    )

    def test_a_real_run_projects_every_count_as_a_number(self):
        self.box.aged_staging(3)
        self.box.run_janitor()

        published = self.box.state()["counts"]
        projected = self.box.ctl_json()["janitor"]["counts"]

        for name in self.PUBLIC_COUNTS:
            with self.subTest(count=name):
                self.assertIn(name, projected)
                self.assertIsNotNone(
                    projected[name],
                    f"{name} projected as None from a valid nested state")

        # The projection must equal what was published, not merely be non-None.
        self.assertEqual(projected["disposed"], published["disposed"])
        self.assertEqual(projected["selected"], published["selected"])
        self.assertEqual(projected["eligible"], published["eligible"])
        self.assertEqual(projected["disposed"], 3)

    def test_protected_reads_the_published_protected_ids(self):
        # The script's key is `protected_ids`; the public name is `protected`.
        # Reading `protected` from the nested object would still have yielded
        # None after the nesting fix, so the mapping is asserted directly.
        keep = self.box.aged_staging(2)[0]
        self.box.set_live_ids([keep.name])
        self.box.run_janitor()

        published = self.box.state()["counts"]["protected_ids"]
        self.assertGreaterEqual(published, 1)
        self.assertEqual(self.box.ctl_json()["janitor"]["counts"]["protected"],
                         published)

    def test_zero_stays_zero_and_is_not_confused_with_unmeasured(self):
        # A run in apply mode disposes, so would_dispose is a real 0.  "Nothing
        # would be disposed" and "the count could not be read" are different
        # facts and must not share a representation.
        self.box.aged_staging(1)
        self.box.run_janitor()

        projected = self.box.ctl_json()["janitor"]["counts"]
        self.assertEqual(self.box.state()["counts"]["would_dispose"], 0)
        self.assertEqual(projected["would_dispose"], 0)
        self.assertIsNotNone(projected["would_dispose"])

    def test_malformed_counts_container_projects_every_key_as_none(self):
        self.box.aged_staging(1)
        self.box.run_janitor()
        observed = self.box.state()["observed_at"]

        for shape in ('"text"', "[]", "12", "null", "{}"):
            with self.subTest(counts=shape):
                (self.box.jd / "janitor-state.json").write_text(
                    '{"observed_at": "%s", "counts": %s}' % (observed, shape),
                    encoding="utf-8")
                projected = self.box.ctl_json()["janitor"]["counts"]
                # The key set is fixed regardless of input shape, so a consumer
                # never has to guess whether a missing key means zero.
                self.assertEqual(sorted(projected), sorted(self.PUBLIC_COUNTS))
                for name in self.PUBLIC_COUNTS:
                    self.assertIsNone(projected[name], name)

    def test_one_bad_count_does_not_poison_its_neighbours(self):
        self.box.aged_staging(1)
        self.box.run_janitor()
        observed = self.box.state()["observed_at"]

        for bad in ("true", "-5", '"12"', "null", "1e999"):
            with self.subTest(eligible=bad):
                (self.box.jd / "janitor-state.json").write_text(
                    '{"observed_at": "%s", "counts": {"eligible": %s,'
                    ' "selected": 9, "protected_ids": 4}}' % (observed, bad),
                    encoding="utf-8")
                projected = self.box.ctl_json()["janitor"]["counts"]
                self.assertIsNone(projected["eligible"], bad)
                self.assertEqual(projected["selected"], 9, bad)
                self.assertEqual(projected["protected"], 4, bad)

    def test_unparseable_document_is_invalid_and_reports_no_counts(self):
        self.box.aged_staging(1)
        self.box.run_janitor()

        for text in ('{"counts": {"eligible": NaN}}', "[1,2,3]", "not json"):
            with self.subTest(document=text[:20]):
                (self.box.jd / "janitor-state.json").write_text(text, encoding="utf-8")
                janitor = self.box.ctl_json()["janitor"]
                self.assertTrue(janitor["invalid"])
                self.assertFalse(janitor["present"])
                self.assertTrue(all(value is None
                                    for value in janitor["counts"].values()))


class MetricsRetentionTests(JanitorTestCase):
    """One retention key, honoured by both the shell and the controller.

    R2 P1-2: ``config.env`` declared ``METRICS_KEEP_LINES`` while the shell
    consumed ``METRICS_MAX_LINES`` and the controller hard-coded its own limit.
    Editing the configured value changed nothing anywhere — the same shape of
    defect as ``QUARANTINE_RETAIN_HOURS`` vs ``QUARANTINE_KEEP_HOURS``.
    """

    def _seed_metrics(self, count: int) -> Path:
        metrics = self.box.jd / "metrics.jsonl"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        metrics.write_text("".join(
            '{"observed_at":"%s","run_id":"seed-%d"}\n' % (stamp, i)
            for i in range(count)), encoding="utf-8")
        return metrics

    def _metric_lines(self) -> int:
        path = self.box.jd / "metrics.jsonl"
        if not path.exists():
            return 0
        return len([l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()])

    def test_config_declares_only_the_canonical_keys(self):
        text = (SRC / "config.env").read_text(encoding="utf-8")
        self.assertIn("METRICS_KEEP_LINES=", text)
        self.assertIn("METRICS_KEEP_DAYS=", text)
        for line in text.splitlines():
            self.assertNotIn("METRICS_MAX_LINES", line,
                             "the retired name must not appear, even commented")

    def test_shell_consumes_the_configured_key_not_an_internal_alias(self):
        """The shell must read ``$METRICS_KEEP_LINES``, never ``$METRICS_MAX_LINES``.

        The test is about *expansion*, not about the name appearing at all: the
        retired name is still required as a literal in the fail-closed detection
        list, and an earlier version of this assertion flagged that very line —
        it would have forced the removal of the code that refuses the old key.
        """

        script = (SRC / "cmux-janitor.sh").read_text(encoding="utf-8")
        reading_retired = [line for line in script.splitlines()
                           if "$METRICS_MAX_LINES" in line
                           or "${METRICS_MAX_LINES" in line]
        self.assertEqual(reading_retired, [],
                         "the shell must not expand the retired name")
        # The canonical name is what actually drives pruning.
        self.assertTrue(
            any("$METRICS_KEEP_LINES" in line or "${METRICS_KEEP_LINES" in line
                for line in script.splitlines()),
            "the shell must expand METRICS_KEEP_LINES")
        # And the retired name must still be detected, as a literal.
        self.assertIn("METRICS_MAX_LINES:METRICS_KEEP_LINES", script)

    def test_configured_line_limit_actually_prunes_in_the_shell(self):
        self.box.set_config(METRICS_KEEP_LINES=4)
        # Each run appends exactly one record, so eight runs must leave four.
        for _ in range(8):
            self.box.run_janitor()
        self.assertEqual(self._metric_lines(), 4)

    def test_changing_the_limit_changes_the_shell_result(self):
        # Proves the number is read rather than coincidentally equal to a default.
        self.box.set_config(METRICS_KEEP_LINES=3)
        for _ in range(6):
            self.box.run_janitor()
        self.assertEqual(self._metric_lines(), 3)

        self.box.set_config(METRICS_KEEP_LINES=9)
        for _ in range(9):
            self.box.run_janitor()
        self.assertEqual(self._metric_lines(), 9)

    def test_controller_prunes_to_the_configured_limit(self):
        self.box.set_config(METRICS_KEEP_LINES=5)
        self._seed_metrics(20)
        result = self.box.run_ctl("compact-metrics")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        # The applied limits are reported, so a caller can prove which ran.
        self.assertEqual(payload["keep_lines"], 5)
        self.assertEqual(payload["kept"], 5)
        self.assertEqual(self._metric_lines(), 5)

    def test_controller_limit_is_not_hard_coded(self):
        self._seed_metrics(30)
        self.box.set_config(METRICS_KEEP_LINES=12)
        self.box.run_ctl("compact-metrics")
        self.assertEqual(self._metric_lines(), 12)

    def test_shell_refuses_the_retired_alias(self):
        self.box.set_config(METRICS_MAX_LINES=100)
        self.box.aged_staging(1)
        result = self.box.run_janitor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("ABORT config invalid", self.box.log())
        self.assertIn("METRICS_MAX_LINES", self.box.log())
        self.assertEqual(self.box.batches(), [],
                         "nothing may move while the config is ambiguous")

    def test_controller_refuses_the_retired_alias_without_pruning(self):
        self.box.set_config(METRICS_MAX_LINES=100)
        self._seed_metrics(20)
        result = self.box.run_ctl("compact-metrics")
        self.assertEqual(result.returncode, 3)
        self.assertIn("fail_closed", result.stderr)
        self.assertIn("METRICS_MAX_LINES", result.stderr)
        self.assertEqual(self._metric_lines(), 20,
                         "a refused compaction must not delete a line")

    def test_illegal_limits_fail_closed_in_both_components(self):
        for value in ("0", "-5", "abc", "12.5", "1e999", ""):
            with self.subTest(value=value):
                self.box.set_config(METRICS_KEEP_LINES=value)
                self._seed_metrics(20)

                # Controller: refuses, and deletes nothing.
                result = self.box.run_ctl("compact-metrics")
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn("fail_closed", result.stderr)
                self.assertEqual(self._metric_lines(), 20)

                # Shell: same verdict on the same file, so the two agree.
                shell = self.box.run_janitor()
                self.assertEqual(shell.returncode, 1, value)
                self.assertIn("METRICS_KEEP_LINES", self.box.log())

    def test_absent_key_falls_back_to_the_documented_default(self):
        text = (self.box.jd / "config.env").read_text(encoding="utf-8")
        kept = [line for line in text.splitlines()
                if not line.strip().startswith("METRICS_KEEP_LINES")]
        (self.box.jd / "config.env").write_text("\n".join(kept) + "\n", encoding="utf-8")

        self._seed_metrics(20)
        result = self.box.run_ctl("compact-metrics")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["keep_lines"], 2048)
        # 20 < 2048, so nothing is pruned; the default is a bound, not a target.
        self.assertEqual(self._metric_lines(), 20)

    def test_keep_days_is_validated_too(self):
        self.box.set_config(METRICS_KEEP_DAYS="0")
        self._seed_metrics(5)
        result = self.box.run_ctl("compact-metrics")
        self.assertEqual(result.returncode, 3)
        self.assertIn("METRICS_KEEP_DAYS", result.stderr)
        self.assertEqual(self._metric_lines(), 5)

    def test_duplicate_definitions_are_refused(self):
        path = self.box.jd / "config.env"
        path.write_text(path.read_text(encoding="utf-8") + "METRICS_KEEP_LINES=7\n",
                        encoding="utf-8")
        result = self.box.run_janitor()
        self.assertEqual(result.returncode, 1)
        self.assertIn("defined 2 times", self.box.log())


class DuplicateConfigKeyTests(JanitorTestCase):
    """A duplicated canonical key must stop both components, not one (R3 P1).

    The shell refuses a key defined twice before it sources config.env, because
    ``.`` would take the last assignment and that is not necessarily the one the
    operator believes is in effect.  The controller used to read the same file
    happily and prune with a silently chosen limit, so one config produced two
    different safety decisions -- "abort the run" in the shell and "delete
    records" in the controller.
    """

    CANONICAL_KEYS = ("QUARANTINE_KEEP_HOURS", "METRICS_KEEP_LINES", "METRICS_KEEP_DAYS")

    def _duplicate(self, key: str, first: object, second: object,
                   box: "Sandbox | None" = None) -> None:
        """Append a second assignment of ``key`` so the file defines it twice."""

        path = (box or self.box).jd / "config.env"
        text = path.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines()
                 if not line.strip().startswith(f"{key}=")]
        lines += [f"{key}={first}", f"{key}={second}"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _seed_metrics(self, count: int = 20,
                      box: "Sandbox | None" = None) -> Path:
        metrics = (box or self.box).jd / "metrics.jsonl"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        metrics.write_text("".join(
            '{"observed_at":"%s","run_id":"seed-%d"}\n' % (stamp, i)
            for i in range(count)), encoding="utf-8")
        return metrics

    def test_shell_and_ctl_agree_on_every_canonical_key(self):
        """Each key gets a fresh sandbox, so one abort cannot mask the next.

        A duplicated key is a whole-config verdict: whichever key carries it,
        the shell must abort and ctl must fail closed on the *same* file.
        """

        for key in self.CANONICAL_KEYS:
            with self.subTest(key=key):
                box = Sandbox()
                self.addCleanup(subprocess.run,
                                ["/bin/rm", "-rf", str(box.root)], check=False)
                metrics = self._seed_metrics(20, box=box)
                self._duplicate(key, 5, 9, box=box)
                before = metrics.read_bytes()

                shell = box.run_janitor()
                self.assertEqual(shell.returncode, 1, "the shell must abort")
                self.assertIn(f"{key} defined 2 times", box.log())

                ctl = box.run_ctl("compact-metrics")
                self.assertEqual(ctl.returncode, 3,
                                 "ctl must fail closed, not prune on a guessed limit")
                self.assertIn("defined 2 times", ctl.stderr)

                # The point of the fix: refusing must not touch the file.
                self.assertEqual(metrics.read_bytes(), before,
                                 "metrics.jsonl must be byte-identical after a refusal")

    def test_refusal_leaves_metrics_byte_identical(self):
        metrics = self._seed_metrics(20)
        before = metrics.read_bytes()
        before_stat = metrics.stat()
        self._duplicate("METRICS_KEEP_LINES", 5, 5)  # even identical values are ambiguous

        result = self.box.run_ctl("compact-metrics")
        self.assertEqual(result.returncode, 3)
        after_stat = metrics.stat()
        self.assertEqual(metrics.read_bytes(), before)
        self.assertEqual(after_stat.st_ino, before_stat.st_ino,
                         "a refusal must not replace the file either")
        self.assertEqual(after_stat.st_size, before_stat.st_size)

    def test_identical_duplicate_values_are_still_refused(self):
        # `KEY=5` twice is unambiguous in value but still ambiguous in intent:
        # the shell counts assignments, not values, and ctl must match it rather
        # than being cleverer.
        self._duplicate("METRICS_KEEP_LINES", 5, 5)
        self.assertEqual(self.box.run_ctl("compact-metrics").returncode, 3)
        self.assertEqual(self.box.run_janitor().returncode, 1)

    def test_commented_duplicate_does_not_count(self):
        # `#` is not whitespace, so neither grep -cE '^[[:space:]]*KEY=' nor the
        # Python counterpart may count a commented line.  Getting this wrong
        # would make a documented example break the janitor.
        path = self.box.jd / "config.env"
        text = path.read_text(encoding="utf-8")
        path.write_text(text + "# METRICS_KEEP_LINES=99\n", encoding="utf-8")
        self.assertEqual(self.box.run_ctl("compact-metrics").returncode, 0)
        self.assertEqual(self.box.run_janitor().returncode, 0)

    def test_indented_duplicate_does_count(self):
        # Leading whitespace is ignored by both, because sourcing honours it.
        path = self.box.jd / "config.env"
        text = path.read_text(encoding="utf-8")
        path.write_text(text + "   METRICS_KEEP_LINES=99\n", encoding="utf-8")
        self.assertEqual(self.box.run_ctl("compact-metrics").returncode, 3)
        self.assertEqual(self.box.run_janitor().returncode, 1)

    def test_status_still_answers_while_config_is_ambiguous(self):
        """Diagnosis must survive the refusal that stops mutation.

        ``status`` is how an operator finds out *why* sweeping stopped.  Making
        it fail closed too would leave the TUI with nothing to display but
        "cannot read", which is strictly less information than the janitor's own
        published error.
        """

        self.box.aged_staging(1)
        self.box.run_janitor()          # a healthy run, so state exists
        self._duplicate("METRICS_KEEP_LINES", 5, 9)
        self.box.run_janitor()          # now aborts and publishes the reason

        payload = self.box.ctl_json()
        self.assertEqual(payload["janitor"]["phase"], "error")
        self.assertIn("defined 2 times", payload["janitor"]["error"])
        self.assertFalse(payload["janitor"]["safety_complete"])

    def test_duplicate_key_blocks_disposal_entirely(self):
        staged = self.box.aged_staging(3)
        self._duplicate("QUARANTINE_KEEP_HOURS", 48, 24)
        before = self.box.tree_snapshot()
        self.assertEqual(self.box.run_janitor().returncode, 1)
        for directory in staged:
            self.assertTrue(directory.exists())
        self.assertEqual(before, self.box.tree_snapshot())
        self.assertEqual(self.box.batches(), [])


class MetricsAgePruningBoundaryTests(JanitorTestCase):
    """METRICS_KEEP_DAYS is manual-only, and that must be stated, not implied.

    R3 required an explicit choice: either wire an automatic caller or document
    the limitation.  The documented choice is manual-only, on the grounds that
    the line bound already gives an unconditional size guarantee and the age
    bound only governs how far back history reaches.
    """

    def _seed(self, ancient: int, fresh: int) -> Path:
        metrics = self.box.jd / "metrics.jsonl"
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 400 * 86400))
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rows = ['{"observed_at":"%s","run_id":"ancient-%d"}' % (old, i) for i in range(ancient)]
        rows += ['{"observed_at":"%s","run_id":"fresh-%d"}' % (now, i) for i in range(fresh)]
        metrics.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return metrics

    def test_scheduled_runs_enforce_lines_only_never_age(self):
        metrics = self._seed(ancient=3, fresh=0)
        self.box.set_config(METRICS_KEEP_LINES=50, METRICS_KEEP_DAYS=1)
        self.box.run_janitor()
        text = metrics.read_text(encoding="utf-8")
        # 400-day-old records survive a scheduled run: only the line bound runs.
        self.assertIn("ancient-0", text)
        self.assertEqual(text.count("ancient-"), 3)

    def test_manual_compaction_is_what_enforces_age(self):
        metrics = self._seed(ancient=3, fresh=2)
        self.box.set_config(METRICS_KEEP_DAYS=1)
        result = self.box.run_ctl("compact-metrics")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = metrics.read_text(encoding="utf-8")
        self.assertNotIn("ancient-", text)
        self.assertIn("fresh-0", text)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["keep_days"], 1)
        self.assertEqual(payload["dropped"], 3)

    def test_no_launchd_job_invokes_compaction(self):
        """The claim in config.env has to match the plists.

        If someone later adds an automatic caller, this fails and the comment
        must be rewritten in the same commit -- which is the point.
        """

        # The deployed tree contains rendered plists with actual account names.
        # The source tree (for public release) contains only templates.
        # This test runs against the source tree, so it looks for templates.
        template_names = [
            "com.__LABEL_PREFIX__.cmux-janitor.plist.template",
            "com.__LABEL_PREFIX__.cmux-janitor-guard.plist.template"
        ]

        for name in template_names:
            template_path = SRC / name
            if template_path.exists():
                plist = template_path.read_text(encoding="utf-8")
                self.assertNotIn("compact-metrics", plist)

        # Also check rendered plists if they exist (production deployment)
        for name in (f"{LABEL_PREFIX}.cmux-janitor.plist", f"{LABEL_PREFIX}.cmux-janitor-guard.plist"):
            rendered_path = SRC / name
            if rendered_path.exists():
                plist = rendered_path.read_text(encoding="utf-8")
                self.assertNotIn("compact-metrics", plist)

        script = (SRC / "cmux-janitor.sh").read_text(encoding="utf-8")
        self.assertNotIn("compact-metrics", script)

    def test_config_documents_the_manual_only_boundary(self):
        text = (SRC / "config.env").read_text(encoding="utf-8")
        self.assertIn("compact-metrics", text,
                      "the manual-only command must be named where the key is declared")
        # The retired wording claimed both bounds prune on every run.
        self.assertNotIn("超过任一即裁剪", text)
        self.assertIn("不会自动发生", text)


class LegacyBatchMigrationTests(JanitorTestCase):
    """Batches sealed before two-phase sealing shipped must become expirable.

    ``expire_quarantine`` fails closed on a batch with no
    ``.janitor-batch.json`` (cmux-janitor.sh:479-482).  That default is right --
    an unknown seal time must never be guessed -- but it means every batch
    created by the old janitor can *never* expire.  Production is in that state:
    19 batches, ~30 GB, ``oldest_sealed_at: null``.  Deploying without a
    migration step would pin that storage forever while the storage page
    truthfully reported 0 expirable batches.
    """

    def _legacy(self, when_ago_h: float, *, items: int = 1,
                mtime_ago_h: float | None = None) -> Path:
        """A batch shaped exactly like the old janitor left them.

        The name is ``date '+%Y%m%d-%H%M%S'`` at seal time and the mtime is the
        sealing ``mv``, so both encode the same moment.  ``mtime_ago_h`` lets a
        test drive the two apart, which is the case the migration must refuse.
        """

        sealed = time.time() - when_ago_h * 3600
        name = time.strftime("%Y%m%d-%H%M%S", time.localtime(sealed))
        directory = self.box.quarantine / name
        directory.mkdir(parents=True, exist_ok=True)
        for i in range(items):
            (directory / UUIDS[i]).mkdir(exist_ok=True)
        stamp = sealed if mtime_ago_h is None else time.time() - mtime_ago_h * 3600
        os.utime(directory, (stamp, stamp))
        return directory

    def _migrate(self, *flags: str) -> subprocess.CompletedProcess[str]:
        return self.box.run_ctl("migrate-batches", *flags)

    def _payload(self, result: subprocess.CompletedProcess[str]) -> dict:
        return json.loads(result.stdout)

    # ---- the defect this exists to fix -------------------------------------

    def test_legacy_batch_cannot_expire_before_migration(self):
        """The starting condition, asserted rather than assumed."""

        self._legacy(when_ago_h=400)
        self.box.set_config(QUARANTINE_KEEP_HOURS=48)
        self.box.run_janitor()

        self.assertEqual(len(self.box.batches()), 1,
                         "a 400h-old batch survives because it has no seal time")
        self.assertIn("SKIP-NO-BATCH-META", self.box.log())
        self.assertIsNone(self.box.ctl_json()["quarantine"]["oldest_sealed_at"])

    def test_migration_makes_an_unexpirable_batch_expire(self):
        """End to end: the whole point of the command.

        This is the assertion that actually matters -- not that metadata was
        written, but that the shell subsequently disposes of the batch.  A
        migration that writes a file the expirer still rejects would pass every
        narrower test while changing nothing.
        """

        self._legacy(when_ago_h=400)
        self.box.set_config(QUARANTINE_KEEP_HOURS=48)
        self.box.run_guard()                       # baseline, so re-arm is possible
        self.assertEqual(self.box.run_ctl("pause").returncode, 0)
        self.assertEqual(self._migrate("--apply").returncode, 0)
        # Metadata changed the quarantine fingerprint, so the guard must be
        # re-armed before the janitor may resume.
        self.assertEqual(self.box.run_guard("--rearm").returncode, 0)
        self.assertEqual(self.box.run_ctl("resume").returncode, 0)

        before = self.box.run_id()
        self.box.run_janitor()
        self.assertNotEqual(self.box.run_id(), before, "the sweep must have run")

        self.assertEqual(self.box.batches(), [], "the batch must now expire")
        self.assertIn("EXPIRED batch=", self.box.log())
        self.assertNotIn("SKIP-NO-BATCH-META",
                         self.box.log().rsplit("EXPIRED", 1)[-1])

    # ---- gates -------------------------------------------------------------

    def test_dry_run_is_the_default_and_writes_nothing(self):
        self._legacy(when_ago_h=400)
        self._legacy(when_ago_h=300)
        before = self.box.tree_snapshot()

        result = self._migrate()
        payload = self._payload(result)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(payload["applied"], "report-only must be the default")
        self.assertEqual(payload["migrated"], 2)
        self.assertEqual(self.box.tree_snapshot(), before,
                         "a dry report must not change one inode or mtime")

    def test_apply_refuses_while_the_janitor_is_running(self):
        """Pause first: writing into a batch a live sweep is filling races the seal."""

        self._legacy(when_ago_h=400)
        before = self.box.tree_snapshot()

        result = self._migrate("--apply")
        self.assertEqual(result.returncode, 3)
        self.assertIn("requires the janitor paused", result.stderr)
        self.assertEqual(self.box.tree_snapshot(), before)

    def test_apply_refuses_while_a_sweep_holds_the_mutex(self):
        self._legacy(when_ago_h=400)
        self.box.run_ctl("pause")
        (self.box.jd / ".janitor.mutex").mkdir(parents=True, exist_ok=True)
        before = self.box.tree_snapshot()

        result = self._migrate("--apply")
        self.assertEqual(result.returncode, 3)
        self.assertIn("mutex", result.stderr)
        self.assertEqual(self.box.tree_snapshot(), before)

    # ---- what it refuses to reconstruct ------------------------------------

    def test_name_and_mtime_disagreement_is_left_for_a_human(self):
        """Two timestamps that contradict each other are not evidence."""

        self._legacy(when_ago_h=400, mtime_ago_h=10)   # 390h apart, tolerance 24h
        self.box.run_ctl("pause")

        payload = self._payload(self._migrate("--apply"))
        self.assertEqual(payload["migrated"], 0)
        self.assertEqual(payload["skipped"], 1)
        reason = payload["skipped_detail"][0]["reason"]
        self.assertIn("disagree", reason)
        batch = self.box.quarantine / self.box.batches()[0]
        self.assertFalse((batch / ".janitor-batch.json").exists())

    def test_unparseable_directory_name_is_skipped(self):
        odd = self.box.quarantine / "not-a-batch-name"
        odd.mkdir(parents=True)
        (odd / UUIDS[0]).mkdir()
        self.box.run_ctl("pause")

        payload = self._payload(self._migrate("--apply"))
        self.assertEqual(payload["migrated"], 0)
        self.assertIn("not %Y%m%d-%H%M%S", payload["skipped_detail"][0]["reason"])
        self.assertFalse((odd / ".janitor-batch.json").exists())

    def test_incomplete_batch_is_never_touched(self):
        """An .incomplete-* batch is a sweep in progress, not a sealed one."""

        incomplete = self.box.quarantine / ".incomplete-20260828-010101-999"
        incomplete.mkdir(parents=True)
        (incomplete / UUIDS[0]).mkdir()
        self.box.run_ctl("pause")

        payload = self._payload(self._migrate("--apply"))
        self.assertEqual(payload["migrated"], 0)
        self.assertEqual(payload["skipped"], 1)
        # Exact equality, not ``assertIn("incomplete", ...)``.  The fallback
        # reason for an unparseable name interpolates the batch name, and the
        # name itself contains the word "incomplete" -- so a substring check
        # passes whether or not the explicit skip exists.  A mutant that deleted
        # the skip survived that assertion; this one distinguishes the two.
        self.assertEqual(payload["skipped_detail"][0]["reason"], "incomplete batch",
                         "the skip must classify it as incomplete, not merely "
                         "reject it as an unparseable name")
        self.assertFalse((incomplete / ".janitor-batch.json").exists())

    def test_corrupt_metadata_is_preserved_not_overwritten(self):
        """The expirer already fails closed here; replacing it destroys evidence."""

        batch = self._legacy(when_ago_h=400)
        meta = batch / ".janitor-batch.json"
        meta.write_text("{ this is not json", encoding="utf-8")
        original = meta.read_bytes()
        self.box.run_ctl("pause")

        payload = self._payload(self._migrate("--apply"))
        self.assertEqual(payload["migrated"], 0)
        self.assertIn("unreadable", payload["skipped_detail"][0]["reason"])
        self.assertEqual(meta.read_bytes(), original)

    def test_already_sealed_batches_are_reported_not_rewritten(self):
        native = self.box.batch("20260826-010101", sealed_ago_h=99)
        original = (native / ".janitor-batch.json").read_bytes()
        self.box.run_ctl("pause")

        payload = self._payload(self._migrate("--apply"))
        self.assertEqual(payload["already_sealed"], 1)
        self.assertEqual(payload["migrated"], 0)
        self.assertEqual((native / ".janitor-batch.json").read_bytes(), original,
                         "a natively sealed batch must not be rewritten")

    # ---- reconstruction semantics ------------------------------------------

    def test_reconstruction_prefers_the_later_timestamp(self):
        """A wrong guess must delay expiry, never bring it forward.

        Both timestamps are inside tolerance, so the batch migrates; the chosen
        seal time has to be the later one, because expiring early destroys data
        that was still inside its retention window.
        """

        self._legacy(when_ago_h=100, mtime_ago_h=90)   # 10h apart, within 24h
        self.box.run_ctl("pause")

        payload = self._payload(self._migrate("--apply"))
        self.assertEqual(payload["migrated"], 1)
        record = payload["batches"][0]
        self.assertEqual(record["evidence"], "mtime",
                         "mtime is the later of the two here")
        # ~90h, not ~100h: the later timestamp won.
        self.assertLess(record["age_hours"], 95)
        self.assertGreater(record["age_hours"], 85)

    def test_written_metadata_is_accepted_by_the_shell_parser(self):
        """Mirror of ``batch_sealed_epoch``: grep, then is_uint.

        The shell does not parse JSON, so JSON validity is not the contract.
        Asserting Python can read it back would test the wrong parser.
        """

        self._legacy(when_ago_h=400)
        self.box.run_ctl("pause")
        self._migrate("--apply")

        meta = self.box.quarantine / self.box.batches()[0] / ".janitor-batch.json"
        text = meta.read_text(encoding="utf-8")
        found = re.search(r'"sealed_at_epoch"\s*:\s*([0-9]+)', text)
        self.assertIsNotNone(found, "the shell's grep must match")
        self.assertTrue(found.group(1).isdigit(), "and is_uint must accept it")

    def test_migrated_metadata_records_that_it_was_reconstructed(self):
        self._legacy(when_ago_h=400)
        self.box.run_ctl("pause")
        self._migrate("--apply")

        meta = self.box.quarantine / self.box.batches()[0] / ".janitor-batch.json"
        record = json.loads(meta.read_text(encoding="utf-8"))
        self.assertEqual(record["migrated_from"], "legacy-unsealed",
                         "a reader must be able to tell reconstructed from observed")
        self.assertIn("migration_evidence", record)
        self.assertEqual(record["sealed_at_epoch"], record["created_at_epoch"])

    def test_migration_is_idempotent(self):
        self._legacy(when_ago_h=400)
        self.box.run_ctl("pause")
        first = self._payload(self._migrate("--apply"))
        self.assertEqual(first["migrated"], 1)

        meta = self.box.quarantine / self.box.batches()[0] / ".janitor-batch.json"
        after_first = meta.read_bytes()

        second = self._payload(self._migrate("--apply"))
        self.assertEqual(second["migrated"], 0)
        self.assertEqual(second["already_sealed"], 1)
        self.assertEqual(meta.read_bytes(), after_first,
                         "a second run must not rewrite what the first wrote")

    def test_absent_quarantine_is_not_an_error(self):
        # Nothing to migrate is a normal state, not a failure.
        payload = self._payload(self._migrate("--apply"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["quarantine"], "absent")
        self.assertEqual(payload["migrated"], 0)

    # ---- interaction with the guard ---------------------------------------

    def test_migration_moves_the_quarantine_fingerprint(self):
        """Which is why the command says to re-arm, and why the test proves it.

        The guard hashes batch name+inode+mtime (guard.sh:106-123).  Writing
        metadata changes the mtime, so a migration necessarily moves the
        fingerprint.  Under dry mode that is an R7 violation and the guard trips
        -- correctly, since it cannot tell a migration from an unexplained
        change.  The deploy order therefore has to be pause, migrate, re-arm,
        resume, and the returned note says so.
        """

        # Case A: the mtime already equals the reconstructed seal time, which is
        # what every production batch looks like.  Nothing moves, so claiming a
        # re-arm is needed would be a false instruction.
        batch = self._legacy(when_ago_h=400)
        mtime_before = int(batch.stat().st_mtime)
        self.box.set_config(MODE="dry")
        self.box.run_guard()                       # baseline under dry
        self.box.run_ctl("pause")

        payload = self._payload(self._migrate("--apply"))
        self.assertFalse(payload["fingerprint_changed"])
        self.assertEqual(payload["fingerprint_changed_batches"], [])
        self.assertIn("unchanged", payload["guard_note"])
        self.assertIn("no re-arm needed", payload["guard_note"])
        self.assertEqual(int(batch.stat().st_mtime), mtime_before,
                         "the batch mtime must be restored, not bumped")

        # And the guard agrees: an unchanged fingerprint is not an R7 violation,
        # so a migration that moves nothing does not trip it.
        self.box.run_guard()
        self.assertFalse((self.box.jd / "GUARD_TRIPPED").exists(),
                         "an unchanged fingerprint must not trip the guard")

    def test_a_migration_that_moves_an_mtime_says_so_and_trips_the_guard(self):
        """The other half: when the name is the later stamp, the mtime does move.

        ``max(name, mtime)`` picks the name, the restore therefore shifts the
        directory mtime, and the fingerprint changes.  Under dry that *is* an R7
        violation, so here the note must say re-arm -- the same note that must
        stay silent in the common case above.
        """

        name = "20260811-213000"
        stamp = time.mktime(time.strptime(name, "%Y%m%d-%H%M%S"))
        batch = self.box.quarantine / name
        batch.mkdir(parents=True, exist_ok=True)
        (batch / UUIDS[0]).mkdir(exist_ok=True)
        # Deliberately older than the name, and well inside the 24h tolerance.
        os.utime(batch, (stamp - 3600, stamp - 3600))

        self.box.set_config(MODE="dry")
        self.box.run_guard()
        self.box.run_ctl("pause")

        payload = self._payload(self._migrate("--apply"))
        self.assertTrue(payload["fingerprint_changed"])
        self.assertEqual(payload["fingerprint_changed_batches"], [name])
        self.assertIn("re-arm", payload["guard_note"])
        self.assertEqual(int(batch.stat().st_mtime), int(stamp),
                         "sealed_at must win, since it is the later stamp")

        self.box.run_guard()
        self.assertTrue((self.box.jd / "GUARD_TRIPPED").exists(),
                        "a real quarantine change under dry must trip")
        # The documented recovery works.
        rearm = self.box.run_guard("--rearm")
        self.assertEqual(rearm.returncode, 0, rearm.stdout + rearm.stderr)
        self.assertFalse((self.box.jd / "GUARD_TRIPPED").exists())

    def test_ctl_migration_never_executes_the_guard(self):
        """Re-arming is a human act; a tool that did it would defeat the trip.

        Asserted against the *call graph*, not against the text: the migration
        legitimately mentions ``guard.sh --rearm`` in the note it prints to tell
        an operator what to do next, and an earlier version of this test flagged
        exactly that string -- it would have forced the removal of the
        instruction rather than of any behaviour.
        """

        import ast

        source = (SRC / "cmux-janitorctl").read_text(encoding="utf-8")
        tree = ast.parse(source)
        spawns: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = ast.unparse(node.func)
            if target not in {"subprocess.run", "subprocess.Popen", "os.system",
                              "os.execv", "os.spawnv"}:
                continue
            spawns.append(ast.unparse(node.args[0]) if node.args else target)
        joined = " ".join(spawns)
        self.assertNotIn("GUARD_SH", joined,
                         "ctl must never execute guard.sh")
        self.assertNotIn("guard", joined.lower().replace("guard_state", ""),
                         "ctl must never execute anything guard-related")
        # It does spawn exactly two things, and both are expected.
        self.assertTrue(any("launchctl" in item for item in spawns))
        self.assertTrue(any("JANITOR_SH" in item for item in spawns))


class LiveStoreTriageTests(JanitorTestCase):
    """J1（2026-09-01）：LIVE 缺失 ≠ 空文件 ≠ 不可解析，三态分治。

    缺失意味着"无法知道还有什么被引用"（挂载错位、迁移中、store 被移走），
    必须 fail closed；存在的 0 字节 store 是一个可验证的"没有活跃引用"的
    正面声明；非空但提取不出 id 是解析缺口，同样 fail closed。修复前缺失
    和空文件共享同一个分支，把"不知道"当成了"没有"。
    """

    def test_missing_live_store_aborts_staging(self):
        self.box.aged_staging(3)
        self.box.live.unlink()
        before = sorted(p.name for p in self.box.staging.iterdir())
        self.box.run_janitor("--manual")
        self.assertIn("ABORT-STAGING live store missing", self.box.log())
        self.assertEqual(sorted(p.name for p in self.box.staging.iterdir()), before,
                         "no staging dir may move when the store is missing")
        self.assertFalse(self.box.state()["safety_complete"])

    def test_empty_live_store_is_a_real_no_references_claim(self):
        made = self.box.aged_staging(2)
        self.box.live.write_text("", encoding="utf-8")
        self.box.run_janitor("--manual")
        for directory in made:
            self.assertFalse(directory.exists(),
                             "an existing 0-byte store means nothing is referenced")
        self.assertTrue(self.box.state()["safety_complete"])

    def test_unparseable_live_store_still_aborts(self):
        # 既有行为钉死：非空但 0 个 id = 解析缺口，fail closed。
        self.box.aged_staging(2)
        self.box.live.write_text('{"entries": "corrupted-no-ids-here"}',
                                 encoding="utf-8")
        before = sorted(p.name for p in self.box.staging.iterdir())
        self.box.run_janitor("--manual")
        self.assertIn("extracted 0 snapshot ids", self.box.log())
        self.assertEqual(sorted(p.name for p in self.box.staging.iterdir()), before)

    def test_missing_live_store_does_not_block_sb_cleanup(self):
        # .sb-* 原子写孤儿有独立的安全规则，不依赖 live store 的引用表。
        made = self.box.aged_sb(2)
        self.box.live.unlink()
        self.box.run_janitor("--manual")
        for path in made:
            self.assertFalse(path.exists(), ".sb-* cleanup is independent of LIVE")


class QuarantineMandatoryTests(JanitorTestCase):
    """J2（2026-09-01）：处置必须可回滚，直接 rm -rf 路径已废除。

    USE_QUARANTINE 非 1 时 validate_config fail closed，整轮不做任何处置；
    dispose() 的直删分支被拒绝分支替换；guard 的 R9 把该键漂移视为违规。
    """

    def test_use_quarantine_zero_fails_closed_without_touching_files(self):
        self.box.set_config(USE_QUARANTINE=0)
        self.box.aged_staging(2)
        self.box.aged_sb(1)
        before = self.box.tree_snapshot()
        result = self.box.run_janitor("--manual")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ABORT config invalid", self.box.log())
        self.assertIn("USE_QUARANTINE=0 (direct delete) is retired", self.box.log())
        self.assertEqual(before, self.box.tree_snapshot(),
                         "a refused config must not change a single byte")

    def test_use_quarantine_garbage_fails_closed(self):
        for value in ("2", "yes", "", "true"):
            with self.subTest(value=value):
                box = Sandbox()
                try:
                    box.set_config(USE_QUARANTINE=value)
                    box.aged_staging(1)
                    result = box.run_janitor()
                    self.assertEqual(result.returncode, 1, f"USE_QUARANTINE={value!r}")
                    self.assertIn("ABORT config invalid", box.log())
                finally:
                    subprocess.run(["/bin/rm", "-rf", str(box.root)], check=False)

    def test_dispose_has_no_direct_rm_branch_left(self):
        # 源码级钉死：dispose() 里不再存在对候选路径的直接 rm -rf。
        # expire_quarantine 对隔离批次的到期 rm 不在此列（那是隔离区的出口）。
        text = (SRC / "cmux-janitor.sh").read_text(encoding="utf-8")
        dispose_body = text.split("dispose() {", 1)[1].split("\n}", 1)[0]
        self.assertNotIn('"$RM" -rf', dispose_body)
        self.assertIn("REFUSE-DELETE", dispose_body)

    def test_guard_trips_on_use_quarantine_drift(self):
        self.assertEqual(self.box.run_guard().returncode, 0, "baseline run")
        self.box.set_config(USE_QUARANTINE=0)
        result = self.box.run_guard()
        self.assertEqual(result.returncode, 1, "drift must trip")
        self.assertIn("R9 USE_QUARANTINE", self.box.guard_log())
        self.assertTrue((self.box.jd / "DISABLED").exists(),
                        "a tripped guard must stop the janitor")

    def test_guard_rearm_refuses_a_non_one_value(self):
        self.box.run_guard()
        self.box.set_config(USE_QUARANTINE=0)
        self.box.run_guard()  # trips
        result = self.box.run_guard("--rearm")
        self.assertEqual(result.returncode, 1)
        self.assertIn("USE_QUARANTINE", result.stdout + result.stderr)

    def test_old_baseline_without_the_key_defaults_to_one(self):
        # 旧基线缺 BASE_USE_QUARANTINE 键时按 1 处理——1 是 janitor 唯一
        # 接受的值，所以旧基线只可能是在 1 下取的；不得因升级而误跳闸。
        self.box.run_guard()
        base = self.box.jd / "guard.baseline"
        lines = [line for line in base.read_text(encoding="utf-8").splitlines()
                 if not line.startswith("BASE_USE_QUARANTINE=")]
        base.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = self.box.run_guard()
        self.assertEqual(result.returncode, 0, "legacy baseline must stay healthy")
        self.assertFalse((self.box.jd / "DISABLED").exists())


if __name__ == "__main__":
    unittest.main()

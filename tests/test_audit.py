import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ccc_session_audit as audit_core  # noqa: E402


class AuditHelperTests(unittest.TestCase):
    def test_pollution_count_excludes_legitimate_human_exact_prompt(self):
        ledger = {
            "legitimate": {"status": "human_prompt", "detail": "human_exact_prompt"},
            "ordinary": {"status": "human_prompt", "detail": "human_prompt"},
            "regression": {"status": "human_prompt", "detail": "ambiguous_exact_prompt"},
            "other": {"status": "sent", "detail": "ambiguous_exact_prompt"},
        }
        self.assertEqual(audit_core.pollution_count(ledger), 1)

    def test_log_cursor_detects_append_truncate_and_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "watch.log"
            path.write_text("old\n", encoding="utf-8")
            _, cursor, transition = audit_core.read_new_log(path, {})
            self.assertEqual(transition, "baseline")

            with path.open("a", encoding="utf-8") as handle:
                handle.write("new\n")
            text, cursor, transition = audit_core.read_new_log(path, cursor)
            self.assertEqual((text, transition), ("new\n", "advanced"))

            with path.open("r+b") as handle:
                handle.truncate(0)
                handle.write(b"after truncate\n")
            text, cursor, transition = audit_core.read_new_log(path, cursor)
            self.assertEqual(transition, "truncated")
            self.assertEqual(text, "after truncate\n")

            rotated = path.with_suffix(".log.1")
            path.replace(rotated)
            path.write_text("after rotate\n", encoding="utf-8")
            text, _, transition = audit_core.read_new_log(path, cursor)
            self.assertEqual(transition, "rotated")
            self.assertEqual(text, "after rotate\n")


class SyntheticSendAccountingTests(unittest.TestCase):
    """The two probe errors that produced wrong answers on live data."""

    @staticmethod
    def _line(stamp, body):
        return f"2026-08-27 {stamp},000 WARNING {body}"

    def test_fallback_echo_is_not_counted_as_a_real_hook(self):
        # A synthetic send emits its own `sent=claude_hook source=fallback` line
        # carrying the SAME event id.  Counting it would make every synthetic
        # send look like a healthy Hook followed it.
        text = "\n".join([
            self._line("13:48:54", "surface=AAAA1111 sent=claude_hook_gap event=abc polls=2 attempt=1"),
            self._line("13:48:54", "surface=AAAA1111 sent=claude_hook event=abc source=fallback latency=0.9s"),
        ])
        events = audit_core.parse_log_events(text)
        self.assertEqual(len(events["gap_sends"]), 1)
        self.assertEqual(events["real_hooks"], [], "fallback echo must not be a real hook")
        # as_of must clear the window, otherwise this is legitimately `pending`
        # rather than `absent` -- the distinction Fix 2 exists to preserve.
        observation = audit_core.post_send_hook_observation(
            events["gap_sends"], events["real_hooks"],
            as_of=events["gap_sends"][0]["at"] + 3600,
        )
        self.assertEqual(observation["post_send_hook_absent"], 1)
        self.assertEqual(observation["post_send_hook_observed"], 0)
        self.assertEqual(observation["post_send_hook_pending"], 0)

    def test_socket_sourced_hook_after_send_is_observed_but_not_a_verdict(self):
        text = "\n".join([
            self._line("13:48:54", "surface=AAAA1111 sent=claude_hook_gap event=abc polls=2 attempt=1"),
            self._line("13:49:30", "surface=AAAA1111 sent=claude_hook event=zzz source=socket latency=1.1s"),
        ])
        events = audit_core.parse_log_events(text)
        self.assertEqual(len(events["real_hooks"]), 1)
        observation = audit_core.post_send_hook_observation(
            events["gap_sends"], events["real_hooks"],
            as_of=events["real_hooks"][0]["at"] + 3600,
        )
        self.assertEqual(observation["post_send_hook_observed"], 1)
        # The field names must stay observational; no caller may read validity.
        self.assertNotIn("send_valid", observation)
        self.assertNotIn("valid", observation)
        self.assertIn("observation only", observation["note"])

    def test_absent_attempt_field_is_unknown_not_one(self):
        text = "\n".join([
            self._line("06:01:00", "surface=BBBB2222 sent=claude_hook_gap event=e1 polls=2"),
            self._line("06:02:00", "surface=BBBB2222 sent=claude_hook_gap event=e2 polls=2 attempt=1"),
        ])
        events = audit_core.parse_log_events(text)
        attempts = [send["attempt"] for send in events["gap_sends"]]
        self.assertEqual(attempts, [None, 1], "missing attempt must not default to 1")
        aftermath_input = dict(events)
        aftermath_input["repairs"] = [{"at": events["gap_sends"][0]["at"] - 10, "line": "x"}]
        entries = audit_core.repair_aftermath(aftermath_input)
        self.assertEqual(entries[0]["attempt_distribution"]["attempt_unknown"], 1)
        self.assertEqual(entries[0]["attempt_distribution"]["attempt_1"], 1)

    def test_many_attempt_one_sends_still_counted_as_high_hourly_volume(self):
        # Every line reads attempt=1 because each is a fresh episode.  The
        # per-episode bound cannot see the total; the hourly count must.
        lines = [
            self._line(f"06:{minute:02d}:00",
                       f"surface=CCCC3333 sent=claude_hook_gap event=e{minute} polls=2 attempt=1")
            for minute in range(0, 24, 2)
        ]
        events = audit_core.parse_log_events("\n".join(lines))
        self.assertEqual(len(events["gap_sends"]), 12)
        self.assertTrue(all(s["attempt"] == 1 for s in events["gap_sends"]))
        rates = audit_core.gap_rate_by_surface_hour(events["gap_sends"])
        self.assertEqual(list(rates.values()), [12])
        self.assertGreaterEqual(max(rates.values()), audit_core.GAP_RATE_WARN_PER_HOUR)

    def test_repair_aftermath_reports_correlated_never_confirmed(self):
        text = "\n".join([
            self._line("13:47:59", "Claude Hook configuration auto-repaired backup=/tmp/x.json"),
            self._line("13:48:54", "surface=AAAA1111 sent=claude_hook_gap event=e1 polls=2 attempt=1"),
            self._line("13:50:44", "surface=BBBB2222 sent=claude_hook_gap event=e2 polls=2 attempt=1"),
        ])
        entries = audit_core.repair_aftermath(audit_core.parse_log_events(text))
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["gap_sends"], 2)
        self.assertEqual(entry["surface_count"], 2)
        self.assertEqual(entry["causal_status"], "correlated")
        self.assertNotEqual(entry["causal_status"], "confirmed")
        self.assertAlmostEqual(entry["seconds_to_first_gap"], 55.0, places=0)


class PostSendWindowTests(unittest.TestCase):
    """An unelapsed window must stay undecided instead of reading as absent."""

    SEND_AT = 1_800_000_000.0

    def _sends(self):
        return [{"at": self.SEND_AT, "surface": "AAAA1111", "event": "e1", "attempt": 1}]

    def test_unelapsed_window_is_pending_not_absent(self):
        window = audit_core.POST_SEND_HOOK_WINDOW_SEC
        observation = audit_core.post_send_hook_observation(
            self._sends(), [], as_of=self.SEND_AT + window - 1,
        )
        self.assertEqual(observation["post_send_hook_pending"], 1)
        self.assertEqual(observation["post_send_hook_absent"], 0)
        self.assertEqual(observation["post_send_hook_observed"], 0)
        self.assertEqual(
            observation["per_surface"]["AAAA1111"],
            {"observed": 0, "pending": 1, "absent": 0},
        )

    def test_absent_requires_the_window_to_have_fully_elapsed(self):
        window = audit_core.POST_SEND_HOOK_WINDOW_SEC
        observation = audit_core.post_send_hook_observation(
            self._sends(), [], as_of=self.SEND_AT + window + 1,
        )
        self.assertEqual(observation["post_send_hook_absent"], 1)
        self.assertEqual(observation["post_send_hook_pending"], 0)

    def test_real_hook_inside_window_is_observed_regardless_of_as_of(self):
        hooks = [{"at": self.SEND_AT + 30, "surface": "AAAA1111", "source": "socket"}]
        # Even scored the instant after the Hook, observed wins over pending:
        # the answer is already known and does not need the full window.
        observation = audit_core.post_send_hook_observation(
            self._sends(), hooks, as_of=self.SEND_AT + 31,
        )
        self.assertEqual(observation["post_send_hook_observed"], 1)
        self.assertEqual(observation["post_send_hook_pending"], 0)
        self.assertEqual(observation["post_send_hook_absent"], 0)


class IncidentGroupingTests(unittest.TestCase):
    """One slot re-arming N times is one finding, not N findings."""

    BASE = 1_800_000_000.0

    def _text(self, rows):
        lines = []
        for offset, body in rows:
            stamp = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.BASE + offset),
            )
            lines.append(f"{stamp},000 WARNING {body}")
        return "\n".join(lines)

    def _merge(self, rows, *, as_of_offset, previous=None):
        events = audit_core.parse_log_events(self._text(rows))
        return audit_core.merge_incident_groups(
            events["incidents"], previous or {}, as_of=self.BASE + as_of_offset,
        )

    def test_repeated_expiry_of_one_event_is_a_single_group(self):
        groups = self._merge([
            (0, "surface=8B863C9F Claude stop deferred event=1361e706 reason=x superseded=-"),
            (900, "surface=8B863C9F Claude deferred stop expired event=1361e706 "
                  "waited_sec=900 retrying=true continuing_to_monitor=true"),
            (1800, "surface=8B863C9F Claude deferred stop expired event=1361e706 "
                   "waited_sec=903 retrying=true continuing_to_monitor=true"),
            (2700, "surface=8B863C9F Claude deferred stop expired event=1361e706 "
                   "waited_sec=901 retrying=true continuing_to_monitor=true"),
            (3600, "surface=8B863C9F Claude deferred stop released event=1361e706 "
                   "reason=sent waited_sec=900 final_status=sent"),
        ], as_of_offset=3700)
        self.assertEqual(len(groups), 1, f"expected one slot, got {sorted(groups)}")
        group = next(iter(groups.values()))
        self.assertEqual(group["expiry_count"], 3)
        self.assertEqual(group["rearm_count"], 3)
        self.assertEqual(group["identity"], "event")
        self.assertTrue(group["release_observed"])
        self.assertEqual(group["final_status"], "sent")

    def test_held_sec_is_not_masked_by_the_rearm_reset(self):
        # waited_sec restarts on every re-arm, so its maximum understates a long
        # hold: 903s observed against a 3600s span.
        groups = self._merge([
            (0, "surface=8B863C9F Claude stop deferred event=aa reason=x superseded=-"),
            (900, "surface=8B863C9F Claude deferred stop expired event=aa "
                  "waited_sec=900 retrying=true continuing_to_monitor=true"),
            (1800, "surface=8B863C9F Claude deferred stop expired event=aa "
                   "waited_sec=903 retrying=true continuing_to_monitor=true"),
            (3600, "surface=8B863C9F Claude deferred stop released event=aa "
                   "reason=sent waited_sec=900 final_status=sent"),
        ], as_of_offset=3700)
        group = next(iter(groups.values()))
        self.assertEqual(group["held_sec"], 3600.0)
        self.assertEqual(group["max_waited_sec"], 903.0)
        self.assertGreater(group["held_sec"], group["max_waited_sec"] * 3)
        self.assertFalse(group["held_sec_is_lower_bound"])

    def test_missing_start_yields_lower_bound_and_missing_release_is_not_a_verdict(self):
        # The cursor may have already consumed the start line.  The span is then
        # a floor, and an unseen release is "not observed", never "not released".
        groups = self._merge([
            (900, "surface=8B863C9F Claude deferred stop expired event=bb "
                  "waited_sec=900 retrying=true continuing_to_monitor=true"),
            (1800, "surface=8B863C9F Claude deferred stop expired event=bb "
                   "waited_sec=902 retrying=true continuing_to_monitor=true"),
        ], as_of_offset=2000)
        group = next(iter(groups.values()))
        self.assertTrue(group["held_sec_is_lower_bound"])
        self.assertFalse(group["release_observed"])
        self.assertEqual(group["held_sec"], 1100.0)

    def test_absent_retrying_field_still_counts_as_a_rearm(self):
        # The older log format had no ``retrying=``.  Reading absence as "false"
        # would drop those re-arms and undercount a pathological slot.
        groups = self._merge([
            (900, "surface=8B863C9F Claude deferred stop expired event=cc "
                  "waited_sec=902 continuing_to_monitor=true"),
            (1800, "surface=8B863C9F Claude deferred stop expired event=cc "
                   "waited_sec=903 retrying=true continuing_to_monitor=true"),
        ], as_of_offset=2000)
        group = next(iter(groups.values()))
        self.assertEqual(group["expiry_count"], 2)
        self.assertEqual(group["rearm_count"], 2, "absent retrying= must not be read as false")
        self.assertEqual(group["rearm_legacy_format"], 1)

    def test_healthy_deferrals_are_not_retained_but_pathological_ones_are(self):
        # 872 of 878 observed starts released within seconds.  Keeping a group per
        # start would grow the state file ~100x while carrying no information.
        rows = [
            (0, "surface=NORMAL01 Claude stop deferred event=ok1 reason=x superseded=-"),
            (3, "surface=NORMAL01 Claude deferred stop released event=ok1 "
                "reason=sent waited_sec=3 final_status=sent"),
            (10, "surface=NORMAL02 Claude stop deferred event=ok2 reason=x superseded=-"),
            (12, "surface=NORMAL02 Claude deferred stop released event=ok2 "
                 "reason=sent waited_sec=2 final_status=sent"),
            (20, "surface=SICK0001 Claude stop deferred event=bad reason=x superseded=-"),
            (920, "surface=SICK0001 Claude deferred stop expired event=bad "
                  "waited_sec=900 retrying=true continuing_to_monitor=true"),
        ]
        groups = self._merge(rows, as_of_offset=1000)
        kinds = sorted(group["event"] for group in groups.values())
        self.assertEqual(kinds, ["bad"], f"only pathological slots retained, got {kinds}")

    def test_groups_accumulate_across_runs_and_prune_past_retention(self):
        first = self._merge([
            (900, "surface=8B863C9F Claude deferred stop expired event=dd "
                  "waited_sec=900 retrying=true continuing_to_monitor=true"),
        ], as_of_offset=1000)
        second = self._merge([
            (1800, "surface=8B863C9F Claude deferred stop expired event=dd "
                   "waited_sec=902 retrying=true continuing_to_monitor=true"),
        ], as_of_offset=2000, previous=first)
        group = next(iter(second.values()))
        self.assertEqual(group["expiry_count"], 2, "counts must survive an incremental run")
        stale = audit_core.merge_incident_groups(
            [], second, as_of=self.BASE + audit_core.INCIDENT_RETAIN_SEC + 10_000,
        )
        self.assertEqual(stale, {}, "groups past the retention window must be pruned")


class AuditIntegrationTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.logs = self.root / "logs"
        self.audit = self.root / "audit"
        self.app.mkdir()
        self.logs.mkdir()
        self.source = self.root / "cmux_codex_watch.py"
        self.source.write_text("print('release')\n", encoding="utf-8")
        self.config = {
            "mode": "armed",
            "global_paused": False,
            "claude_enabled": True,
            "targets": [{
                "surface_id": "surface-uuid",
                "ref": "surface:43",
                "paused": False,
            }],
        }
        self._write_json(self.app / "config.json", self.config)
        self.log_path = self.logs / "watch.log"
        self.log_path.write_text("baseline\n", encoding="utf-8")
        os.utime(self.log_path, (self.NOW, self.NOW))
        self._write_json(self.app / "daemon-runtime.json", {
            "pid": 123,
            "started_at": self.NOW - 100,
            "source_sha256": audit_core.sha256_of(self.source),
            "config_sha256": audit_core.sha256_of(self.app / "config.json"),
        })
        self._write_json(self.app / "claude-event-ledger.json", {"events": {
            "legitimate": {"status": "human_prompt", "detail": "human_exact_prompt"},
            "regression": {"status": "human_prompt", "detail": "ambiguous_exact_prompt"},
            "old-generation": {
                "status": "deferred_generation_changed",
                "handled_at": self.NOW - 99_999,
            },
        }})
        self._write_json(self.app / "state.json", {
            "surface-uuid": {
                "state": "composer_busy",
                "claude_hook_health": "healthy",
                "claude_last_hook_at": self.NOW - 2 * 3600,
                "claude_context_status": "normal",
            },
        })
        self.patch = mock.patch.multiple(
            audit_core,
            APP_DIR=self.app,
            LOG_DIR=self.logs,
            AUDIT_DIR=self.audit,
            AUDIT_STATE=self.audit / "audit-state.json",
            AUDIT_JOURNAL=self.audit / "audit.jsonl",
            AUDIT_ACKNOWLEDGEMENTS=self.audit / "acknowledgements.json",
            SOURCE_PATH=self.source,
            EXPECTED_TARGETS=1,
            EXPECTED_PAUSED=0,
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def _tree_fingerprint(self):
        return {str(p.relative_to(self.root)): (
            p.stat().st_ino, p.stat().st_mtime_ns,
            audit_core.sha256_of(p) if p.is_file() else None,
        ) for p in self.root.rglob("*")}

    def test_read_only_does_not_create_audit_directory(self):
        before = self._tree_fingerprint()
        result = audit_core.audit(now=self.NOW, live_pids=[123], read_only=True)
        self.assertTrue(result["read_only"])
        self.assertEqual(before, self._tree_fingerprint())
        self.assertFalse(self.audit.exists())

    def test_read_only_does_not_advance_existing_cursor_or_journal(self):
        audit_core.audit(now=self.NOW, live_pids=[123])
        with self.log_path.open("a") as handle:
            handle.write("new observation\n")
        before = self._tree_fingerprint()
        audit_core.audit(now=self.NOW + 10, live_pids=[123], read_only=True)
        self.assertEqual(before, self._tree_fingerprint())

    def test_json_cli_reports_missing_and_malformed_inputs_without_writes(self):
        path = self.app / "state.json"
        for body in (None, "not-json", "[]", "null"):
            with self.subTest(body=body):
                if body is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(body)
                before = self._tree_fingerprint()
                with mock.patch.object(audit_core, "daemon_processes", return_value=[123]), \
                     mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    rc = audit_core.main(["--json", "--read-only"])
                payload = json.loads(out.getvalue())
                self.assertEqual(rc, 2)
                self.assertIn("state.json", payload["input_errors"])
                self.assertEqual(before, self._tree_fingerprint())

    def test_json_cli_error_is_structured_and_does_not_persist(self):
        self._write_json(self.app / "daemon-runtime.json", {"pid": "invalid"})
        before = self._tree_fingerprint()
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = audit_core.main(["--json", "--read-only"])
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(out.getvalue())["error"], "ValueError")
        self.assertEqual(before, self._tree_fingerprint())

    def test_json_flag_does_not_implicitly_disable_default_publication(self):
        with mock.patch.object(audit_core, "daemon_processes", return_value=[123]), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            audit_core.main(["--json"])
        self.assertFalse(json.loads(out.getvalue())["read_only"])
        self.assertTrue((self.audit / "audit-state.json").exists())
        self.assertTrue((self.audit / "audit.jsonl").exists())

    def test_unknown_option_rejected_before_any_audit(self):
        with mock.patch.object(audit_core, "audit") as audited, \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as raised:
                audit_core.main(["--jsno"])
        self.assertEqual(raised.exception.code, 2)
        audited.assert_not_called()

    @staticmethod
    def _write_json(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def _freshen_log(self, now):
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"tick {now}\n")
        os.utime(self.log_path, (now, now))

    def test_hook_silence_is_independent_of_oscillating_runtime_state(self):
        first = audit_core.audit(now=self.NOW, live_pids=[123])
        key = "surface:surface-uuid:hook_silent"
        self.assertIn(key, first["conditions"])
        self.assertEqual(first["pollution"], 1)
        self.assertFalse(any("human_exact_prompt" in item for item in first["alerts"]))
        episode = first["conditions"][key]["episode_id"]

        state = json.loads((self.app / "state.json").read_text(encoding="utf-8"))
        state["surface-uuid"]["state"] = "claude_hook_waiting"
        self._write_json(self.app / "state.json", state)
        later = self.NOW + 5 * 3600
        self._freshen_log(later)
        second = audit_core.audit(now=later, live_pids=[123])
        self.assertEqual(second["conditions"][key]["episode_id"], episode)
        self.assertEqual(second["conditions"][key]["severity"], "severe")
        self.assertEqual(second["conditions"][key]["observations"], 2)

    def test_explicit_acknowledgement_demotes_but_does_not_hide_condition(self):
        audit_core.audit(now=self.NOW, live_pids=[123])
        key = "surface:surface-uuid:hook_silent"
        self.audit.mkdir(exist_ok=True)
        self._write_json(self.audit / "acknowledgements.json", {
            key: {"reason": "upstream session intentionally idle"},
        })
        later = self.NOW + 60
        self._freshen_log(later)
        result = audit_core.audit(now=later, live_pids=[123])
        self.assertTrue(result["conditions"][key]["acknowledged"])
        self.assertFalse(any(f"key={key}" in item for item in result["alerts"]))
        self.assertTrue(any(f"key={key}" in item for item in result["notes"]))

    def test_completed_episode_is_not_misreported_as_hook_silence(self):
        state_path = self.app / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["surface-uuid"].update({
            # Deliberately leave a non-completed viewport state: the audit must
            # use the lifecycle latch rather than binding back to runtime.state.
            "state": "composer_busy",
            "claude_completed_latched": True,
            "claude_last_event_status": "completed",
        })
        self._write_json(state_path, state)
        result = audit_core.audit(now=self.NOW, live_pids=[123])
        self.assertNotIn("surface:surface-uuid:hook_silent", result["conditions"])

    def _set_targets(self, specs):
        """specs: list of (ref, paused, health, last_hook_at)."""
        config = json.loads((self.app / "config.json").read_text(encoding="utf-8"))
        state = json.loads((self.app / "state.json").read_text(encoding="utf-8"))
        config["targets"] = []
        state = {}
        for index, (ref, paused, health, last_hook) in enumerate(specs):
            surface_id = f"uuid-{index}"
            config["targets"].append({
                "surface_id": surface_id, "ref": ref, "paused": paused,
            })
            state[surface_id] = {
                "state": "composer_busy",
                "claude_hook_health": health,
                "claude_last_hook_at": last_hook,
                "claude_context_status": "normal",
                "claude_completed_latched": True,
            }
        self._write_json(self.app / "config.json", config)
        self._write_json(self.app / "state.json", state)

    def test_distribution_covers_every_target_including_paused(self):
        self._set_targets([
            ("surface:1", False, "healthy", self.NOW - 60),
            ("surface:2", False, "missing", 0.0),
            ("surface:3", False, "legacy_override", 0.0),
            ("surface:4", False, "unverified", 0.0),
            ("surface:5", True, "unverified", 0.0),
            ("surface:6", True, "healthy", self.NOW - 60),
        ])
        result = audit_core.audit(now=self.NOW, live_pids=[123])
        distribution = result["hook_distribution"]
        self.assertEqual(sum(distribution.values()), 6,
                         "distribution must account for all targets, not a subset")
        self.assertEqual(distribution["healthy"], 1)
        self.assertEqual(distribution["missing"], 1)
        self.assertEqual(distribution["legacy_override"], 1)
        self.assertEqual(distribution["unverified"], 1)
        self.assertEqual(distribution["paused:unverified"], 1)
        self.assertEqual(distribution["paused:healthy"], 1)
        rendered = audit_core._format_distribution(result)
        self.assertIn("paused:unverified=1", rendered)

    def test_never_hooked_unverified_does_not_alert_but_after_hook_does(self):
        self._set_targets([
            ("surface:idle-a", False, "unverified", 0.0),
            ("surface:idle-b", False, "unverified", 0.0),
        ])
        result = audit_core.audit(now=self.NOW, live_pids=[123])
        self.assertEqual(len(result["unverified_never_hooked"]), 2)
        self.assertEqual(result["unverified_previously_hooked"], [])
        self.assertNotIn("hook:unverified_after_hook", result["conditions"])

        # Same label, but this one HAD a Hook: protection was lost.
        self._set_targets([
            ("surface:idle-a", False, "unverified", 0.0),
            ("surface:regressed", False, "unverified", self.NOW - 7200),
        ])
        later = self.NOW + 60
        self._freshen_log(later)
        result = audit_core.audit(now=later, live_pids=[123])
        self.assertEqual(result["unverified_previously_hooked"], ["surface:regressed"])
        self.assertIn("hook:unverified_after_hook", result["conditions"])
        self.assertTrue(any("protection lost" in item for item in result["alerts"]))

    def test_hourly_gap_volume_accumulates_across_incremental_runs(self):
        # The log cursor is incremental.  If per-hour counts were derived from a
        # single tail they would reset every run and never reach a threshold.
        self._set_targets([("surface:1", False, "healthy", self.NOW - 60)])
        audit_core.audit(now=self.NOW, live_pids=[123])
        half = audit_core.GAP_RATE_WARN_PER_HOUR // 2 + 1
        for batch in range(2):
            with self.log_path.open("a", encoding="utf-8") as handle:
                for index in range(half):
                    stamp = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(self.NOW + batch * 60 + index),
                    )
                    handle.write(
                        f"{stamp},000 WARNING surface=SURF0001 "
                        f"sent=claude_hook_gap event=b{batch}i{index} polls=2 attempt=1\n"
                    )
            moment = self.NOW + 120 + batch * 120
            os.utime(self.log_path, (moment, moment))
            result = audit_core.audit(now=moment, live_pids=[123])
        total = sum(
            entry["sends"] for entry in result["gap_rate_hotspots"]
            if entry["surface"] == "SURF0001"
        )
        self.assertGreaterEqual(total, audit_core.GAP_RATE_WARN_PER_HOUR)
        self.assertTrue(
            any(key.startswith("gap_rate:SURF0001|") for key in result["conditions"]),
            f"expected hourly volume condition, got {sorted(result['conditions'])}",
        )
        self.assertTrue(any("per-episode retry bounds do not limit this" in item
                            for item in result["alerts"]))

    def _append_gap_lines(self, surface, count, at, *, tag="g"):
        """Append *count* synthetic-send lines stamped at *at*."""

        with self.log_path.open("a", encoding="utf-8") as handle:
            for index in range(count):
                stamp = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(at + index),
                )
                handle.write(
                    f"{stamp},000 WARNING surface={surface} "
                    f"sent=claude_hook_gap event={tag}{index} polls=2 attempt=1\n"
                )
        os.utime(self.log_path, (at + count, at + count))

    @staticmethod
    def _hour_of(stamp):
        return time.strftime("%Y-%m-%d %H:00", time.localtime(stamp))

    def test_open_hour_stays_active_then_retires_on_rollover_without_realerting(self):
        # A bucket for the hour in progress can still gain sends, so escalation
        # is meaningful there.  It is also inherently short-lived: an hour is
        # current for at most an hour, well under the 6h severe threshold.
        self._set_targets([("surface:1", False, "healthy", self.NOW - 60)])
        audit_core.audit(now=self.NOW, live_pids=[123])
        self._append_gap_lines("SURF0001", audit_core.GAP_RATE_WARN_PER_HOUR + 1, self.NOW)
        key = f"gap_rate:SURF0001|{self._hour_of(self.NOW)}"

        first = audit_core.audit(now=self.NOW + 60, live_pids=[123])
        self.assertIn(key, first["conditions"])
        self.assertEqual(first["conditions"][key]["severity"], "warning")

        # Roll past the hour: same fact, now frozen.  It must retire quietly.
        later = self.NOW + 2 * 3600
        self._freshen_log(later)
        second = audit_core.audit(now=later, live_pids=[123])
        self.assertNotIn(key, second["conditions"], "closed bucket must not stay active")
        self.assertIn(f"SURF0001|{self._hour_of(self.NOW)}", second["gap_rate_reported"])
        self.assertFalse(
            [item for item in second["alerts"] if key in item],
            "rollover into a closed hour is not a new finding",
        )

    def test_closed_hour_alerts_once_then_never_escalates(self):
        self._set_targets([("surface:1", False, "healthy", self.NOW - 60)])
        audit_core.audit(now=self.NOW, live_pids=[123])
        # Stamped an hour back, so the bucket is already closed when first seen.
        closed_at = self.NOW - 3600
        self._append_gap_lines("SURF0002", audit_core.GAP_RATE_WARN_PER_HOUR + 3, closed_at)
        os.utime(self.log_path, (self.NOW, self.NOW))
        key = f"gap_rate:SURF0002|{self._hour_of(closed_at)}"

        first = audit_core.audit(now=self.NOW + 60, live_pids=[123])
        self.assertIn(key, first["conditions"], "a late discovery must still be reported")
        self.assertEqual(len([item for item in first["alerts"] if key in item]), 1)

        # 30 hours on, the age-based severity would have made this critical.
        much_later = self.NOW + 30 * 3600
        self._freshen_log(much_later)
        third = audit_core.audit(now=much_later, live_pids=[123])
        self.assertNotIn(key, third["conditions"])
        self.assertFalse([item for item in third["alerts"] if key in item])
        self.assertTrue(
            any("gap_rate history" in note for note in third["notes"]),
            "retired buckets stay visible as a rolling note",
        )

    def _seed_state(self, payload):
        self.audit.mkdir(parents=True, exist_ok=True)
        self._write_json(self.audit / "audit-state.json", payload)

    def test_prefix_gap_rate_conditions_migrate_without_a_second_alert(self):
        closed_hour = self._hour_of(self.NOW - 3600)
        bucket = f"SURF0003|{closed_hour}"
        # Written by a build that had no gap_rate_reported at all.
        self._seed_state({
            "conditions": {
                f"gap_rate:{bucket}": {
                    "detail": "SURF0003: 13 synthetic sends",
                    "first_seen_at": self.NOW - 7200,
                    "episode_id": "aabbccdd",
                    "severity": "warning",
                    "observations": 4,
                },
            },
        })
        result = audit_core.audit(now=self.NOW, live_pids=[123])
        self.assertIn(bucket, result["gap_rate_reported"])
        self.assertTrue(result["gap_rate_reported"][bucket]["carried_over"])
        self.assertEqual(result["gap_rate_migrated"], [bucket])
        self.assertNotIn(f"gap_rate:{bucket}", result["conditions"])
        self.assertFalse([item for item in result["alerts"] if bucket in item])

    def test_migration_runs_once_and_leaves_the_current_hour_active(self):
        current_hour = self._hour_of(self.NOW)
        closed_hour = self._hour_of(self.NOW - 3600)
        self._seed_state({
            "conditions": {
                f"gap_rate:SURF0004|{closed_hour}": {"detail": "closed", "severity": "warning"},
                f"gap_rate:SURF0004|{current_hour}": {"detail": "open", "severity": "warning"},
            },
        })
        first = audit_core.audit(now=self.NOW, live_pids=[123])
        self.assertEqual(first["gap_rate_migrated"], [f"SURF0004|{closed_hour}"])
        self.assertNotIn(
            f"SURF0004|{current_hour}", first["gap_rate_reported"],
            "an hour still in progress must not be pre-adopted as history",
        )

        later = self.NOW + 120
        self._freshen_log(later)
        second = audit_core.audit(now=later, live_pids=[123])
        self.assertEqual(
            second["gap_rate_migrated"], [],
            "the migration branch is gated on the key being absent and must not rerun",
        )

    def test_as_of_follows_log_coverage_not_the_wall_clock(self):
        # The log trails real time.  Scoring the post-send window against ``now``
        # would call a send "no Hook arrived" when the log simply does not reach
        # that far yet.
        self._set_targets([("surface:1", False, "healthy", self.NOW - 60)])
        audit_core.audit(now=self.NOW, live_pids=[123])
        send_at = self.NOW + 10
        self._append_gap_lines("SURF0005", 1, send_at)
        now = send_at + audit_core.POST_SEND_HOOK_WINDOW_SEC + 50
        os.utime(self.log_path, (now, now))

        result = audit_core.audit(now=now, live_pids=[123])
        self.assertEqual(result["log_covers_until"], send_at)
        self.assertEqual(result["as_of"], send_at)
        self.assertLess(result["as_of"], now)
        post = result["post_send_hook"]
        self.assertEqual(post["post_send_hook_pending"], 1)
        self.assertEqual(
            post["post_send_hook_absent"], 0,
            "an uncovered window must not be scored as absent",
        )

    def test_provenance_keeps_running_disk_and_start_config_distinct(self):
        meta = json.loads((self.app / "daemon-runtime.json").read_text(encoding="utf-8"))
        meta["source_sha256"] = "a" * 64
        meta["config_sha256"] = "b" * 64
        self._write_json(self.app / "daemon-runtime.json", meta)
        result = audit_core.audit(now=self.NOW, live_pids=[123])
        self.assertEqual(result["running_source_sha"], "a" * 64)
        self.assertEqual(result["disk_source_sha"], audit_core.sha256_of(self.source))
        self.assertEqual(result["daemon_start_config_sha"], "b" * 64)
        self.assertEqual(
            result["disk_config_sha"], audit_core.sha256_of(self.app / "config.json"),
        )
        self.assertIn("daemon:source_drift", result["conditions"])
        self.assertTrue(any("config bytes changed since daemon start" in n for n in result["notes"]))

    def test_generation_changed_is_terminal_but_old_provisional_is_escalated(self):
        ledger_path = self.app / "claude-event-ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["events"]["still-parked"] = {
            "status": "deferred_working",
            "handled_at": self.NOW - 7200,
        }
        self._write_json(ledger_path, ledger)
        result = audit_core.audit(now=self.NOW, live_pids=[123])
        detail = result["conditions"]["ledger:provisional_deferred"]["detail"]
        self.assertIn("still-pa", detail)
        self.assertNotIn("old-gene", detail)


if __name__ == "__main__":
    unittest.main()

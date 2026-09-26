"""A failed measurement is not proof that an immutable safety fact changed."""
import json
import os
import sys
import unittest
from pathlib import Path

from tests.test_janitor import JanitorTestCase


class GuardObservationTests(JanitorTestCase):
    def fake_tool(self, key, text):
        path = self.box.root / ("fake-" + key.lower())
        path.write_text("#!/bin/bash\n" + text + "\n")
        path.chmod(0o755)
        script = self.box.jd / "guard.sh"
        original = script.read_text()
        lines = [f'{key}="{path}"' if line.startswith(key + "=") else line
                 for line in original.splitlines()]
        script.write_text("\n".join(lines) + "\n")
        return original

    def test_config_grep_fork_failure_no_longer_invents_r9(self):
        self.box.run_guard()
        self.fake_tool("GREP", 'case "$*" in *USE_QUARANTINE*) exit 2;; esac\nexec /usr/bin/grep "$@"')
        result = self.box.run_guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.box.jd / "GUARD_TRIPPED").exists())
        self.assertEqual(self.box.guard_state()["health"], "healthy")

    def test_stat_failure_holds_cleanup_without_latching_a_false_violation(self):
        self.box.run_guard()
        before = (self.box.jd / "guard.baseline").read_bytes()
        source = self.fake_tool("STAT", 'exit 2')
        result = self.box.run_guard()
        self.assertEqual(result.returncode, 3)
        self.assertEqual((self.box.jd / "guard.baseline").read_bytes(), before)
        self.assertFalse((self.box.jd / "GUARD_TRIPPED").exists())
        self.assertFalse((self.box.jd / "DISABLED").exists())
        self.assertTrue((self.box.jd / "GUARD_UNAVAILABLE").exists())
        self.assertEqual(self.box.guard_state()["health"], "observation_error")
        self.box.aged_sb(1)
        artifacts = self.box.tree_snapshot()
        self.assertEqual(self.box.run_janitor("--manual").returncode, 0)
        self.assertEqual(self.box.tree_snapshot(), artifacts)
        self.assertEqual(self.box.run_ctl("run", "--manual").returncode, 1)
        self.assertEqual(self.box.run_ctl("resume").returncode, 1)
        self.assertNotEqual(self.box.run_ctl("expire").returncode, 0)
        # A successful later check releases only its temporary gate.
        (self.box.jd / "DISABLED").write_text("operator pause")
        (self.box.jd / "guard.sh").write_text(source)
        self.assertEqual(self.box.run_guard().returncode, 0)
        self.assertFalse((self.box.jd / "GUARD_UNAVAILABLE").exists())
        self.assertEqual((self.box.jd / "DISABLED").read_text(), "operator pause")

    def test_status_failure_is_read_only_and_initial_failure_never_baselines(self):
        self.fake_tool("STAT", "exit 2")
        result = self.box.run_guard("--status")
        self.assertEqual(result.returncode, 3)
        for name in ("guard-state.json", "guard.baseline", "GUARD_UNAVAILABLE", "GUARD_TRIPPED"):
            self.assertFalse((self.box.jd / name).exists(), name)
        self.assertEqual(self.box.run_guard().returncode, 3)
        self.assertFalse((self.box.jd / "guard.baseline").exists())

    def test_unreadable_config_never_rearms_or_deletes_a_prior_trip(self):
        self.box.run_guard()
        self.box.set_config(USE_QUARANTINE=0)
        self.box.run_guard()
        trip = (self.box.jd / "GUARD_TRIPPED").read_bytes()
        (self.box.jd / "config.env").rename(self.box.jd / "config.saved")
        self.assertEqual(self.box.run_guard("--rearm").returncode, 3)
        self.assertEqual((self.box.jd / "GUARD_TRIPPED").read_bytes(), trip)
        self.assertTrue((self.box.jd / "DISABLED").exists())

    def test_actual_deleted_key_still_trips_after_a_successful_read(self):
        self.box.run_guard()
        config = self.box.jd / "config.env"
        config.write_text("\n".join(line for line in config.read_text().splitlines()
                                    if not line.startswith("USE_QUARANTINE=")) + "\n")
        self.assertEqual(self.box.run_guard().returncode, 1)
        self.assertIn("R9", (self.box.jd / "GUARD_TRIPPED").read_text())

    def test_publishing_failure_never_releases_the_temporary_gate(self):
        self.box.run_guard()
        (self.box.jd / "GUARD_UNAVAILABLE").write_text("unavailable")
        self.fake_tool("MKTEMP", "exit 1")
        self.assertEqual(self.box.run_guard().returncode, 3)
        self.assertTrue((self.box.jd / "GUARD_UNAVAILABLE").exists())

    def test_latched_original_reason_remains_visible_in_controller(self):
        self.box.run_guard()
        self.box.set_config(USE_QUARANTINE=0)
        self.box.run_guard()
        self.box.set_config(USE_QUARANTINE=1)
        self.box.run_guard()
        result = self.box.ctl_json()
        self.assertTrue(result["control"]["guard_tripped"])
        self.assertEqual(len(result["guard"]["violations"]), 1)
        self.assertIn("R9", result["guard"]["violations"][0])

    def test_successful_first_status_does_not_create_a_baseline(self):
        before = sorted(p.name for p in self.box.jd.iterdir())
        self.assertEqual(self.box.run_guard("--status").returncode, 3)
        self.assertEqual(sorted(p.name for p in self.box.jd.iterdir()), before)

    def test_failed_baseline_replacement_preserves_prior_baseline_and_trip(self):
        self.box.run_guard()
        self.box.set_config(USE_QUARANTINE=0)
        self.box.run_guard()
        before = (self.box.jd / "guard.baseline").read_bytes()
        trip = (self.box.jd / "GUARD_TRIPPED").read_bytes()
        self.box.set_config(USE_QUARANTINE=1)
        self.fake_tool("MV", "exit 2")
        self.assertEqual(self.box.run_guard("--rearm").returncode, 3)
        self.assertEqual((self.box.jd / "guard.baseline").read_bytes(), before)
        self.assertEqual((self.box.jd / "GUARD_TRIPPED").read_bytes(), trip)
        self.assertTrue((self.box.jd / "GUARD_UNAVAILABLE").exists())

    def test_failed_publication_creates_a_gate_even_without_a_prior_failure(self):
        self.box.run_guard()
        self.fake_tool("DATE", "exit 2")
        self.assertEqual(self.box.run_guard().returncode, 3)
        self.assertTrue((self.box.jd / "GUARD_UNAVAILABLE").exists())
        self.assertEqual(self.box.run_ctl("resume").returncode, 1)

    def test_genuinely_missing_mode_is_not_baselined_or_rearmed(self):
        self.box.run_guard()
        config = self.box.jd / "config.env"
        config.write_text("\n".join(line for line in config.read_text().splitlines()
                                    if not line.startswith("MODE=")) + "\n")
        self.assertEqual(self.box.run_guard().returncode, 1)
        self.assertIn("R6", (self.box.jd / "GUARD_TRIPPED").read_text())
        self.assertEqual(self.box.run_guard("--rearm").returncode, 1)


if __name__ == "__main__":
    unittest.main()

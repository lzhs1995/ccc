"""Distinguish producer observation deadlines from bounded snapshot transport."""
import copy
import unittest
from unittest import mock

import cmux_codex_watch as core


class PublishedContinuationTests(unittest.TestCase):
    def setUp(self):
        self.target = {"surface_id": "s", "workspace_id": "w", "enabled": True}
        self.config = {"targets": [self.target], "workspace_rules": [], "poll_interval_sec": 1}
        self.runtime = {"viewport_checked_at": 100, "state": "working", "observation_cadence_sec": 1}
        self.snapshot = {
            "config_key": core.monitoring_config_key(self.config), "observed_at": 101.8,
            "rows": [{"surface_id": "s", "workspace_id": "w", "status": "readable",
                      "observed_at": 100, "viewport_checked_at": 100}],
        }

    def report(self, now, *, runtime=None, snapshot=None, config=None):
        with mock.patch.object(core.time, "time", return_value=now):
            return core.continuation_status(config or self.config, {"s": runtime or self.runtime},
                                            snapshot or self.snapshot)["targets"][0]

    def test_transport_age_does_not_invent_a_producer_deadline_miss(self):
        result = self.report(102.5)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observation_age_sec"], 2.5)

    def test_stopped_publisher_expires_instead_of_leaving_a_green_result(self):
        self.assertEqual(self.report(103.8)["status"], "ok")
        self.assertEqual(self.report(103.801)["status"], "delayed")
        self.assertEqual(self.report(140)["status"], "delayed")

    def test_producer_deadline_miss_is_never_hidden_by_a_fresh_publication(self):
        self.snapshot["observed_at"] = 102.1
        self.assertEqual(self.report(102.2)["status"], "delayed")

    def test_future_missing_malformed_or_foreign_publication_cannot_extend_deadline(self):
        for value in (None, False, "101.8", float("nan"), float("inf"), 102.6, 99):
            with self.subTest(value=value):
                self.snapshot["observed_at"] = value
                self.assertEqual(self.report(102.5)["status"], "delayed")
        for field, value in (("surface_id", "foreign"), ("workspace_id", "foreign"),
                             ("observed_at", 0), ("viewport_checked_at", 99),
                             ("status", "live_unreadable")):
            snapshot = copy.deepcopy(self.snapshot)
            snapshot["observed_at"] = 101.8
            snapshot["rows"][0][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.report(102.5, snapshot=snapshot)["status"], "delayed")

    def test_changed_authorization_cannot_reuse_the_old_publication(self):
        config = copy.deepcopy(self.config)
        config["targets"][0]["workspace_id"] = "new-workspace"
        self.assertEqual(self.report(102.5, config=config)["status"], "delayed")

    def test_fresh_delivery_failure_and_unknown_receipt_still_take_effect(self):
        for delivery, expected in (("failed", "send_failed"), ("unknown", "delivery_unknown"),
                                   ("sending", "delivery_unknown")):
            with self.subTest(delivery=delivery):
                runtime = {**self.runtime, "delivery_status": delivery, "send_started_at": 100}
                self.assertEqual(self.report(102.5, runtime=runtime)["status"], expected)
        runtime = {**self.runtime, "state": "cmux_unavailable"}
        self.assertEqual(self.report(102.5, runtime=runtime)["status"], "unavailable")

    def test_new_state_observation_is_not_rebased_to_an_unrelated_snapshot(self):
        runtime = {**self.runtime, "viewport_checked_at": 101}
        self.assertEqual(self.report(103.5, runtime=runtime)["status"], "delayed")
        runtime["viewport_checked_at"] = 102.4
        self.assertEqual(self.report(102.5, runtime=runtime)["status"], "ok")

    def test_independent_atomic_writers_keep_the_newest_verified_observation(self):
        self.snapshot["rows"][0]["viewport_checked_at"] = 101
        result = self.report(102.5)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["viewport_checked_at"], 101)
        self.assertEqual(result["observation_age_sec"], 1.5)
        runtime = {**self.runtime, "delivery_status": "failed"}
        self.assertEqual(self.report(102.5, runtime=runtime)["status"], "send_failed")
        for value in (False, "101", float("nan"), float("inf"), 99, 103):
            with self.subTest(value=value):
                self.snapshot["rows"][0]["viewport_checked_at"] = value
                self.assertEqual(self.report(102.5)["status"], "delayed")


if __name__ == "__main__":
    unittest.main()

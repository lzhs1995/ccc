"""Bounded observation must never turn a timeout or a queue into reception."""
import unittest
from unittest.mock import Mock, patch

import cmux_confirmation as confirmation


class ConfirmationTests(unittest.TestCase):
    def test_invalid_windows_fail_before_observation(self):
        for value in (-1, 60.001, float("nan"), float("inf"), True, "30"):
            with self.subTest(value=value):
                observe = Mock()
                with self.assertRaisesRegex(ValueError, "CONFIRMATION_WINDOW_INVALID"):
                    confirmation.observe_receipt(observe, value)
                observe.assert_not_called()

    def test_defaults_and_zero_reconcile_do_one_read(self):
        self.assertEqual(confirmation.wait_budget(), 30)
        observe = Mock(return_value={"confirmed": False, "state": "NATIVE_PENDING"})
        result = confirmation.observe_receipt(observe, reconcile_only=True)
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(result["confirmation_observation"]["requested_seconds"], 0)
        self.assertEqual(result["confirmation_observation"]["stop_reason"], "SINGLE_OBSERVATION")

    def test_stalled_clock_and_sleep_still_terminate(self):
        observe = Mock(return_value={"confirmed": False, "state": "NATIVE_PENDING"})
        with patch.object(confirmation.time, "monotonic", return_value=0), patch.object(
                confirmation.time, "sleep") as sleep:
            result = confirmation.observe_receipt(observe, 2)
        self.assertFalse(result["confirmed"])
        self.assertEqual(observe.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result["confirmation_observation"]["stop_reason"], "OBSERVATION_COUNT_EXHAUSTED")

    def test_slow_io_reports_actual_duration_without_second_observation(self):
        clock = [0.0]
        def observe():
            clock[0] += 12
            return {"confirmed": False}
        with patch.object(confirmation.time, "monotonic", side_effect=lambda: clock[0]):
            result = confirmation.observe_receipt(observe, 2)
        self.assertEqual(result["confirmation_observation"]["elapsed_seconds"], 12)
        self.assertEqual(result["confirmation_observation"]["observation_count"], 1)
        self.assertEqual(result["confirmation_observation"]["stop_reason"], "OBSERVATION_WINDOW_EXHAUSTED")

    def test_queue_can_stop_terminal_observation_without_claiming_receipt(self):
        observe = Mock(return_value={"confirmed": False, "state": "NATIVE_QUEUED"})
        result = confirmation.observe_receipt(observe, 30, stop_when_queued=True)
        self.assertFalse(result["confirmed"])
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(result["confirmation_observation"]["stop_reason"], "QUEUED_NOT_RECEIVED")

    def test_reconcile_can_observe_native_record_after_queued_record(self):
        observe = Mock(side_effect=[{"confirmed": False, "state": "NATIVE_QUEUED"},
                                    {"confirmed": True, "state": "NATIVE_RECEIVED"}])
        with patch.object(confirmation.time, "sleep"):
            result = confirmation.observe_receipt(observe, 2, reconcile_only=True)
        self.assertTrue(result["confirmed"])
        self.assertEqual(observe.call_count, 2)

    def test_observer_failure_keeps_details_and_actual_read_count(self):
        error = OSError("original transcript unavailable")
        error.details = {"original_attempt": "unchanged"}
        with self.assertRaises(OSError) as caught:
            confirmation.observe_receipt(Mock(side_effect=error), 0)
        details = caught.exception.details
        self.assertEqual(details["original_attempt"], "unchanged")
        self.assertEqual(details["confirmation_observation"]["observation_count"], 1)
        self.assertEqual(details["confirmation_observation"]["stop_reason"], "OBSERVATION_ERROR")


if __name__ == "__main__":
    unittest.main()

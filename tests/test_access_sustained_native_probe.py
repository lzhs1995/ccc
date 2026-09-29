"""Acceptance must not release success before exercising every real-run gate."""
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.access_sustained_native_probe import SustainedNativeProbe


class SustainedNativeProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.probe = SustainedNativeProbe(Path(self.tmp.name))
        self.probe.first_request_at = 100
        self.probe.reconnects = {'native:5': {'number': 5}}
        self.server = SimpleNamespace(condition=threading.Condition(), requests=[None] * 1250)
        self.failed = {str(i): {'turn_id': 'first'} for i in range(50)}
        self.current = {str(i): {'turn_id': 'continued'} for i in range(50)}

    def test_all_fifty_original_sessions_must_really_have_a_new_turn(self):
        self.current['49'] = {'turn_id': 'first'}
        with patch('tools.access_sustained_native_probe.time.monotonic', return_value=251):
            self.probe.maybe_release(self.server, self.current, self.failed)
            self.assertFalse(self.probe.allow_success.is_set())
            self.current['49'] = {'turn_id': 'continued'}
            self.probe.maybe_release(self.server, self.current, self.failed)
        self.assertTrue(self.probe.allow_success.is_set())
        self.assertEqual(self.probe.evidence()['continued_original_sessions'], 50)

    def test_request_count_alone_cannot_replace_duration_or_native_reconnect(self):
        with patch('tools.access_sustained_native_probe.time.monotonic', return_value=249):
            self.probe.maybe_release(self.server, self.current, self.failed)
        self.assertFalse(self.probe.allow_success.is_set())
        self.probe.reconnects = {'native:4': {'number': 4}}
        with patch('tools.access_sustained_native_probe.time.monotonic', return_value=251):
            self.probe.maybe_release(self.server, self.current, self.failed)
        self.assertFalse(self.probe.allow_success.is_set())

    def test_reconnect_and_elapsed_time_cannot_replace_real_http_count(self):
        self.server.requests = [None] * 1000
        with patch('tools.access_sustained_native_probe.time.monotonic', return_value=251):
            self.probe.maybe_release(self.server, self.current, self.failed)
        self.assertFalse(self.probe.allow_success.is_set())
        self.assertTrue(self.probe.reject())
        with self.assertRaises(AssertionError):
            self.probe.evidence()


if __name__ == '__main__':
    unittest.main()

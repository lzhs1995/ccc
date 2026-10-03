"""An absent request must not hide a read failure or invent a credential."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import cmux_supervisor_tui as tui
import test_supervisor_request_key as fixtures


class RequestObservationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeRequestKeyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_empty_directory_is_absent_not_a_global_key(self):
        result = self.fixture.observe()
        self.assertEqual(result.api_key_observation_status, "absent")
        self.assertEqual(result.api_key_observed, "")
        candidate = SimpleNamespace(session=result)
        self.assertEqual(tui.Candidate.api_key_text.fget(candidate), "暂无请求记录")
        detail = "".join(tui.api_key_detail_lines(candidate, 89))
        self.assertIn("未采集到该会话", detail)
        self.assertNotIn(result.api_key_config, detail)

    def test_permission_failure_is_not_absence(self):
        with patch.object(Path, "iterdir", side_effect=PermissionError("private")):
            result = self.fixture.observe()
        self.assertEqual(result.api_key_observation_status, "unverified")
        self.assertIn("PermissionError", result.api_key_observation_note)

    def test_malformed_record_is_not_absence(self):
        path = self.fixture.write()
        path.write_text("not json")
        result = self.fixture.observe()
        self.assertEqual(result.api_key_observation_status, "unverified")
        self.assertIn("JSONDecodeError", result.api_key_observation_note)
        self.assertEqual(result.api_key_observed, "")

    def test_unbound_record_is_not_absence(self):
        self.fixture.write()
        with patch("ccc_request_key_binding.connected_writer", return_value=False):
            result = self.fixture.observe()
        self.assertEqual(result.api_key_observation_status, "unverified")
        self.assertIn("身份核验", result.api_key_observation_note)

    def test_process_exit_during_empty_scan_is_not_absence(self):
        result = self.fixture.observe(births=[self.fixture.born, None])
        self.assertEqual(result.api_key_observation_status, "unverified")
        self.assertEqual(result.api_key_observed, "")

    def test_next_request_changes_absent_to_actual_key(self):
        result = self.fixture.observe()
        self.assertEqual(result.api_key_observation_status, "absent")
        self.fixture.write(key="fake-request-not-global")
        result = self.fixture.observe()
        self.assertEqual(result.api_key_observation_status, "observed")
        self.assertEqual(result.api_key_observed, "fake-request-not-global")
        self.assertIn("实际认证请求头", result.api_key_observation_note)

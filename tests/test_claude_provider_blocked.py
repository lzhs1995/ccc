"""Native provider failures, durable input guards and safe task boundaries."""
import json
import tempfile
import unittest
from pathlib import Path

from claude_ccc_protocol import (
    STOP_FAILURE_METADATA, append_event, build_event, classify_stop_failure,
)


RPM_ERROR = "⏺ API Error: Request rejected (429) · group requests-per-minute limit exceeded"


class ProviderFailureClassificationTests(unittest.TestCase):
    def test_enum_and_response_body_both_contribute(self):
        cases = [
            ("authentication_failed", "HTTP 503", "claude_auth"),
            ("oauth_org_not_allowed", "", "claude_auth"),
            ("cloud_credential_error", "", "claude_auth"),
            ("billing_error", "", "claude_billing"),
            ("account_on_hold", "", "claude_billing"),
            ("model_not_found", "", "claude_model_unavailable"),
            ("invalid_request", "Prompt is too long", "claude_request_invalid"),
            ("invalid_request", "request identifier 503", "claude_request_invalid"),
            ("unknown", "HTTP 405 Method Not Allowed", "claude_request_invalid"),
            ("server_error", "API Error: 403 API Key 所属分组已删除", "claude_auth"),
            ("server_error", "HTTP 403 预扣费失败：余额不足", "claude_billing"),
            ("unknown", {"error": {"status": 403, "message": "API Key 所属分组已删除"}}, "claude_auth"),
            ("", "API Error: 401", "claude_auth"),
            ("", "API Error: 403", "claude_auth"),
            ("unknown", "API Error: model does not exist", "claude_model_unavailable"),
        ]
        for error, details, expected in cases:
            with self.subTest(error=error, details=details):
                result = classify_stop_failure(error, details)
                self.assertEqual(result["error_kind"], expected)
                self.assertTrue(result["provider_blocked"])

    def test_real_transient_failures_remain_retryable(self):
        cases = [
            ("rate_limit", "", "claude_429"),
            ("rate_limit_error", "", "claude_429"),
            ("unknown", RPM_ERROR, "claude_429"),
            ("server_error", {"error": {"message": RPM_ERROR}}, "claude_429"),
            ("invalid_request", RPM_ERROR, "claude_429"),
            ("", "\x1b[31m" + RPM_ERROR + "\x1b[0m", "claude_429"),
            ("", "Request rejected (\n429) · group requests-per-minute\nlimit exceeded", "claude_429"),
            ("unknown", "group requests-per-minute limit exceeded", "claude_429"),
            ("overloaded", "", "claude_503"),
            ("server_error", "HTTP 503", "claude_503"),
            ("", "503 private upstream details", "claude_503"),
            ("server_error", "HTTP 500 internal server error", "claude_api"),
            ("unknown", "stream disconnected", "claude_stream"),
        ]
        for error, details, expected in cases:
            with self.subTest(error=error, details=details):
                result = classify_stop_failure(error, details)
                self.assertEqual(result["error_kind"], expected)
                self.assertFalse(result["provider_blocked"])

    def test_body_is_not_persisted_and_journal_keeps_metadata(self):
        raw = "HTTP 403 API Key 所属分组已删除 private-response-secret"
        event = build_event({"hook_event_name": "StopFailure", "session_id": "session-a",
                             "error": "server_error", "error_details": raw},
                            {"CMUX_SURFACE_ID": "surface-a", "CMUX_WORKSPACE_ID": "workspace-a"})
        self.assertEqual(event["error_kind"], "claude_auth")
        self.assertEqual(event["stop_failure_error"], "server_error")
        self.assertNotIn("private-response-secret", json.dumps(event))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            append_event(event, path)
            restored = json.loads(path.read_text())
            for key in STOP_FAILURE_METADATA:
                self.assertEqual(restored[key], event[key])


if __name__ == "__main__":
    unittest.main()

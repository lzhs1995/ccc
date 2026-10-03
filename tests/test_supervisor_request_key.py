"""Read native request records without mistaking shared configuration for auth."""
import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cmux_supervisor_tui as tui

SID = "01a0e7c1-dbf1-7c23-a186-ce9af424ed55"
OTHER = "01a0e96a-68dd-7210-8ef0-d83c89dca4d8"


class NativeRequestKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.directory.chmod(0o700)
        self.born = [int(time.time()) - 60, 0]
        self.epoch = str(uuid.uuid4())

    def write(self, sid=SID, key="fake-a", **changes):
        data = dict(schema=1, observer_epoch=self.epoch, pid=123,
                    thread_id=sid, purpose="request_attempt", transport="http",
                    observed_at_ms=int(time.time() * 1000),
                    authorization="Bearer " + key, api_key=None,
                    endpoint="https://example.invalid/v1/responses")
        data.update(changes)
        path = self.directory / f"{data['observer_epoch']}-{sid}-{data['purpose']}.json"
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def observe(self, sid=SID, births=None, env=None):
        result = tui.SessionResult(status="ok", agent_kind="codex", session_id=sid, pid=123,
                                   api_key_config="fake-global-current")
        with patch("ccc_guard_scope.birth", side_effect=births, return_value=self.born), \
             patch("ccc_guard_scope.arguments", return_value=(["codex", "app-server"], env or {
                 "CODEX_CREDENTIAL_OBSERVATIONS_DIR": str(self.directory)})):
            tui.observe_request_api_key(result, self.directory)
        return result

    def test_persistent_marker_uses_writer_home_and_actual_record(self):
        home = self.directory
        self.directory = home / "credential-observations"
        self.directory.mkdir(mode=0o700)
        env = {"CODEX_HOME": str(home)}
        self.write()
        self.assertEqual(self.observe(env=env).api_key_observed, "")
        marker = self.directory / "enabled-v1"
        marker.write_bytes(b"ccc-request-credentials-v1\n")
        marker.chmod(0o600)
        self.assertEqual(self.observe(env=env).api_key_observed, "fake-a")
        self.assertEqual(self.observe(env={"CODEX_HOME": str(home / "wrong")}).api_key_observed, "")
        self.assertEqual(self.observe(env={**env, "CODEX_CREDENTIAL_OBSERVATIONS_DIR": "/wrong"}).api_key_observed, "")
        marker.chmod(0o644)
        self.assertEqual(self.observe(env=env).api_key_observed, "")
        marker.unlink()
        target = home / "marker"
        target.write_bytes(b"ccc-request-credentials-v1\n")
        target.chmod(0o600)
        marker.symlink_to(target)
        self.assertEqual(self.observe(env=env).api_key_observed, "")

    def test_two_threads_use_different_request_keys_not_shared_config(self):
        self.write()
        self.write(OTHER, "fake-b")
        for sid, expected in ((SID, "fake-a"), (OTHER, "fake-b")):
            result = self.observe(sid)
            self.assertEqual(result.api_key_observed, expected)
            candidate = SimpleNamespace(session=result)
            self.assertEqual(tui.Candidate.api_key_text.fget(candidate), expected)
            self.assertIn("最近实际请求", "".join(tui.api_key_detail_lines(candidate, 30)))
            self.assertNotIn(expected, repr(result))

    def test_unrelated_observations_do_not_hide_mixed_thread_keys(self):
        for index in range(5000):
            (self.directory / f"unrelated-{index}-request_attempt.json").touch()
        self.write(SID, "fake-a")
        self.write(OTHER, "fake-b")
        self.assertEqual(
            [self.observe(sid).api_key_observed for sid in (SID, OTHER)],
            ["fake-a", "fake-b"],
        )

    def test_one_thread_key_changes_without_changing_other_thread_display(self):
        self.write(SID, "fake-a")
        self.write(OTHER, "fake-b")
        first = self.observe(SID)
        second = self.observe(OTHER)
        self.assertEqual((first.api_key_observed, second.api_key_observed),
                         ("fake-a", "fake-b"))

        # A new request replaces only this thread's observation. Refresh the
        # existing UI objects, as the live collector does, in reverse order.
        self.write(SID, "fake-c")
        with patch("ccc_guard_scope.birth", return_value=self.born), \
             patch("ccc_guard_scope.arguments", return_value=(["codex", "app-server"], {
                 "CODEX_CREDENTIAL_OBSERVATIONS_DIR": str(self.directory)})):
            for result in (second, first):
                result.api_key_config = "fake-new-global"
                tui.observe_request_api_key(result, self.directory)
        self.assertEqual((first.api_key_observed, second.api_key_observed),
                         ("fake-c", "fake-b"))
        for result, expected in ((first, "fake-c"), (second, "fake-b")):
            self.assertEqual(tui.Candidate.api_key_text.fget(
                SimpleNamespace(session=result)), expected)

    def test_reused_pid_or_dead_writer_is_rejected(self):
        self.write()
        self.assertEqual(self.observe(births=[None]).api_key_observed, "")
        self.assertEqual(self.observe(births=[self.born, self.born, [self.born[0], 1]]).api_key_observed, "")
        self.write(observed_at_ms=(self.born[0] - 1) * 1000)
        self.assertEqual(self.observe().api_key_observed, "")

    def test_request_before_cli_restart_is_explicitly_historical(self):
        self.write(pid=124, observed_at_ms=(self.born[0] + 10) * 1000)
        def birth(pid, **kwargs):
            return [self.born[0] + 20, 0] if pid == 123 else self.born
        with patch("ccc_request_key_binding.connected_writer", return_value=True):
            result = self.observe(births=birth)
        self.assertEqual(result.api_key_observed, "fake-a")
        self.assertIn("早于当前 CLI 启动", result.api_key_observation_note)
        self.assertTrue(result.api_key_observation_historical)
        self.assertIn("历史请求", tui.Candidate.api_key_text.fget(SimpleNamespace(session=result)))
        self.assertNotIn("fake-a", tui.Candidate.api_key_text.fget(SimpleNamespace(session=result)))
        self.assertIn("尚未观察到本次启动后的请求", result.api_key_observation_note)
        self.assertIn("早于当前 CLI 启动", "".join(tui.api_key_detail_lines(
            SimpleNamespace(session=result), 89)))

        # A later actual request must replace the historical label on the same
        # object; changing the shared disk key alone must never do so.
        self.write(pid=124, key="fake-new", observed_at_ms=(self.born[0] + 30) * 1000)
        with patch("ccc_guard_scope.birth", side_effect=birth), \
             patch("ccc_guard_scope.arguments", return_value=(["codex", "app-server"], {
                 "CODEX_CREDENTIAL_OBSERVATIONS_DIR": str(self.directory)})), \
             patch("ccc_request_key_binding.connected_writer", return_value=True):
            tui.observe_request_api_key(result, self.directory)
        self.assertFalse(result.api_key_observation_historical)
        self.assertEqual(tui.Candidate.api_key_text.fget(SimpleNamespace(session=result)), "fake-new")

    def test_historical_marker_clears_when_current_writer_disappears(self):
        result = tui.SessionResult(status="ok", agent_kind="codex", session_id=SID, pid=123,
                                   api_key_observed="fake-old", api_key_observation_historical=True)
        with patch("ccc_guard_scope.birth", return_value=None):
            tui.observe_request_api_key(result, self.directory)
        self.assertFalse(result.api_key_observation_historical)
        self.assertEqual(tui.Candidate.api_key_text.fget(SimpleNamespace(session=result)), "未核实")

    def test_request_after_cli_restart_is_not_marked_historical(self):
        self.write()
        result = self.observe()
        self.assertEqual(result.api_key_observed, "fake-a")
        self.assertNotIn("早于当前 CLI 启动", result.api_key_observation_note)

    def test_other_backend_path_or_ambiguous_backends_are_rejected(self):
        self.write()
        self.assertEqual(self.observe(env={"CODEX_CREDENTIAL_OBSERVATIONS_DIR":"/wrong"}).api_key_observed, "")
        self.write(observer_epoch=str(uuid.uuid4()), pid=124)
        with patch("ccc_request_key_binding.connected_writer", return_value=True):
            self.assertEqual(self.observe().api_key_observed, "")

    def test_warmup_cannot_override_request_and_no_global_fallback(self):
        self.write(purpose="warmup", authorization="Bearer fake-warmup")
        self.assertEqual(self.observe().api_key_observed, "")
        self.write()
        self.assertEqual(self.observe().api_key_observed, "fake-a")

    def test_public_file_symlink_or_malformed_data_is_rejected(self):
        path = self.write()
        path.chmod(0o644)
        self.assertEqual(self.observe().api_key_observed, "")
        path.unlink()
        target = self.directory / "other"
        target.write_text("{}")
        path.symlink_to(target)
        self.assertEqual(self.observe().api_key_observed, "")
        path.unlink()
        self.write(authorization="Bearer fake-a\ninvalid")
        self.assertEqual(self.observe().api_key_observed, "")

    def test_mismatched_two_auth_headers_are_not_guessed(self):
        self.write(api_key="different-key")
        self.assertEqual(self.observe().api_key_observed, "")

    def test_invalid_schema_and_record_types_do_not_escape_worker(self):
        for changes in ({"schema": True}, {"pid": True}, {"pid": "123"}):
            self.write(**changes)
            self.assertEqual(self.observe().api_key_observed, "")
        path = self.write()
        path.write_text("[]")
        self.assertEqual(self.observe().api_key_observed, "")

    def test_observation_is_cleared_when_writer_evidence_disappears(self):
        self.write()
        result = self.observe()
        self.assertEqual(result.api_key_observed, "fake-a")
        with patch("ccc_guard_scope.birth", return_value=None):
            tui.observe_request_api_key(result, self.directory)
        self.assertEqual((result.api_key_observed, result.api_key_observation_note), ("", ""))

    def test_same_thread_from_unconnected_backend_is_not_displayed(self):
        self.write(pid=124)
        with patch("ccc_request_key_binding.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="p123\nf1\ntunix\nd0x1\nn->0x2\np124\nf2\ntunix\nd0x3\nn->0x4\n")):
            self.assertEqual(self.observe().api_key_observed, "")

    def test_disconnect_before_publication_clears_key(self):
        self.write(pid=124)
        with patch("ccc_request_key_binding.connected_writer", side_effect=[True, False]):
            self.assertEqual(self.observe().api_key_observed, "")

    def test_bound_writer_without_key_still_makes_identity_ambiguous(self):
        self.write()
        self.write(observer_epoch=str(uuid.uuid4()), pid=124, authorization=None,
                   credential_scope="opaque_redirect")
        with patch("ccc_request_key_binding.connected_writer", return_value=True):
            self.assertEqual(self.observe().api_key_observed, "")

    def test_redirect_record_clears_key_even_if_stale_header_is_present(self):
        self.write(credential_scope="opaque_redirect")
        result = self.observe()
        self.assertEqual(result.api_key_observed, "")
        self.assertIn("重定向", result.api_key_observation_note)
        self.assertNotIn("尚无原生请求记录", "".join(tui.api_key_detail_lines(
            SimpleNamespace(session=result), 80)))

    def test_request_without_auth_clears_key_and_reports_record(self):
        self.write(authorization=None)
        result = self.observe()
        self.assertEqual(result.api_key_observed, "")
        self.assertIn("最近请求", result.api_key_observation_note)


if __name__ == "__main__":
    unittest.main()

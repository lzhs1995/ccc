import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_network_client import NetworkClient, configured_host, error_host


class NetworkClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "network.json"
        self.path.write_text(json.dumps({"state_dir": str(self.root), "service_host": "anyrouter.test"}))
        self.options = {"enabled": True, "config_path": str(self.path)}
        self.client = NetworkClient()
        self.target = {"surface_id": "surface-a", "workspace_id": "workspace-a"}
        self.turn = {"kind": "task_complete", "session_id": "original", "pid": 42, "process_start": 100,
                     "error": {"message": "error sending request for url (https://anyrouter.test/v1/responses)"}}
        for name, value in (("birth", [100, 1]), ("arguments", (["codex"],
                {"CMUX_SURFACE_ID": "surface-a", "CMUX_WORKSPACE_ID": "workspace-a"}))):
            patch = mock.patch("ccc_guard_scope." + name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        self.status()

    def status(self, phase="network_wait", at=None, mode="manage"):
        value = {"version": 1, "service_host": "anyrouter.test", "mode": mode,
                 "phase": phase, "at": time.time() if at is None else at}
        (self.root / "status.json").write_text(json.dumps(value))
        self.client.cache = (0, "", {})

    def test_disabled_feature_has_no_io(self):
        with mock.patch("ccc_network_client.bounded_json", side_effect=AssertionError("must not read")):
            self.assertFalse(self.client.verdict({"enabled": False}, self.target, self.turn)["blocked"])

    def test_only_matching_service_gets_network_wait(self):
        self.assertTrue(self.client.verdict(self.options, self.target, self.turn)["blocked"])
        other = {**self.turn, "error": {"message": "error for url (https://api.openai.com/v1/responses)"}}
        verdict = self.client.verdict(self.options, self.target, other)
        self.assertFalse(verdict["blocked"])
        self.assertEqual(verdict["service_host"], "api.openai.com")

    def test_unknown_binding_does_not_pause_other_providers(self):
        with mock.patch("ccc_network_client.configured_host", return_value=""):
            verdict = self.client.verdict(self.options, self.target, {**self.turn, "error": {"message": "network error"}})
        self.assertEqual(verdict["phase"], "unbound")
        self.assertFalse(verdict["blocked"])

    def test_missing_stale_future_or_observe_snapshot_does_not_claim_outage(self):
        for stamp, mode in ((time.time() - 11, "manage"), (time.time() + 50, "manage"), (time.time(), "observe")):
            self.status(at=stamp, mode=mode)
            self.assertFalse(self.client.verdict(self.options, self.target, self.turn)["blocked"])
        (self.root / "status.json").write_text("partial")
        self.client.cache = (0, "", {})
        self.assertFalse(self.client.verdict(self.options, self.target, self.turn)["blocked"])

    def test_error_url_requires_an_exact_host(self):
        fake = {"error": {"message": "url (https://anyrouter.test.evil.invalid/v1/responses)"}}
        self.assertEqual(error_host(fake), "anyrouter.test.evil.invalid")
        self.assertFalse(self.client.verdict(self.options, self.target, fake)["blocked"])

    def test_reference_links_do_not_bind_a_native_request_host(self):
        messages = ("See https://anyrouter.test/v1/responses for help",
                    "Documentation url: https://anyrouter.test/help")
        for message in messages:
            with self.subTest(message=message):
                turn = {**self.turn, "error": {"message": message}}
                self.assertEqual(error_host(turn), "")
                with mock.patch("ccc_network_client.configured_host", return_value="api.openai.com"):
                    self.assertFalse(self.client.verdict(self.options, self.target, turn)["blocked"])

    def test_native_request_endpoint_is_distinct_from_response_body_links(self):
        turn = {**self.turn, "error": {"message": "unexpected status 503 Service Unavailable: "
                "see https://support.test/help, url: https://anyrouter.test/v1/responses, request id: fixture"}}
        self.assertEqual(error_host(turn), "anyrouter.test")
        with mock.patch("ccc_network_client.configured_host", return_value=""):
            self.assertTrue(self.client.verdict(self.options, self.target, turn)["blocked"])

    def test_native_error_url_cannot_bypass_local_process_ownership(self):
        env = {"CMUX_SURFACE_ID": "surface-a", "CMUX_WORKSPACE_ID": "workspace-a"}
        for argv, context in ((["codex", "--remote=ws://localhost:9999"], env),
                              (["codex", "--remote", "ws://localhost:9999"], env),
                              (["codex"], {**env, "CMUX_WORKSPACE_ID": "workspace-b"})):
            with self.subTest(argv=argv, context=context), \
                    mock.patch("ccc_guard_scope.arguments", return_value=(argv, context)):
                self.assertFalse(self.client.verdict(self.options, self.target, self.turn)["blocked"])
        with mock.patch("ccc_guard_scope.birth", side_effect=[[100, 1], [100, 2]]):
            self.assertFalse(self.client.verdict(self.options, self.target, self.turn)["blocked"])

    def process_config(self, extra_args=(), *, config_mtime=99, birth_change=False,
                       named_profiles=None, profile_mtime=99, extra_config="",
                       replace_during_read=False):
        codex_dir = self.root / "codex"
        codex_dir.mkdir(exist_ok=True)
        path = codex_dir / "config.toml"
        path.write_text(extra_config + 'model_provider="custom"\n[model_providers.custom]\nbase_url="https://anyrouter.test/v1"\n')
        os.utime(path, (config_mtime, config_mtime))
        for name, content in (named_profiles or {}).items():
            profile_path = codex_dir / (name + ".config.toml")
            profile_path.write_text(content)
            os.utime(profile_path, (profile_mtime, profile_mtime))
        argv = ["codex", *extra_args]
        env = {"CODEX_HOME": str(codex_dir), "CMUX_SURFACE_ID": "surface-a", "CMUX_WORKSPACE_ID": "workspace-a"}
        checks = 0

        def process_birth(*args, **kwargs):
            nonlocal checks
            checks += 1
            if checks == 2 and replace_during_read:
                replacement = path.with_suffix(".replacement")
                replacement.write_text(path.read_text().replace("anyrouter.test", "other.test"))
                os.utime(replacement, (config_mtime, config_mtime))
                os.replace(replacement, path)
            return [101, 2] if checks == 2 and birth_change else [100, 1]

        with mock.patch("ccc_guard_scope.birth", side_effect=process_birth), \
                mock.patch("ccc_guard_scope.arguments", return_value=(argv, env)):
            return configured_host({**self.turn, "error": None, "model_provider": "custom"}, self.target)

    @unittest.skipUnless(sys.version_info >= (3, 11), "Python 3.10 binds through native error URLs")
    def test_startup_overrides_and_config_drift_are_respected(self):
        self.assertEqual(self.process_config(), "anyrouter.test")
        self.assertEqual(self.process_config(("-c", 'model_providers.custom.base_url="https://other.test/v1"')), "other.test")
        self.assertEqual(self.process_config(config_mtime=102), "")
        self.assertEqual(self.process_config(birth_change=True), "")

    @unittest.skipUnless(sys.version_info >= (3, 11), "Python 3.10 binds through native error URLs")
    def test_current_native_named_profile_files_override_base_provider(self):
        profiles = {"other": '[model_providers.custom]\nbase_url="https://other.test/v1"\n'}
        for args in (("-p", "other"), ("--profile=other",), ("-pother",)):
            with self.subTest(args=args):
                self.assertEqual(self.process_config(args, named_profiles=profiles), "other.test")
        self.assertEqual(self.process_config(("-p", "other"), named_profiles=profiles,
                                             profile_mtime=100.5), "")

    @unittest.skipUnless(sys.version_info >= (3, 11), "Python 3.10 binds through native error URLs")
    def test_inline_remote_endpoint_cannot_use_local_provider_configuration(self):
        self.assertEqual(self.process_config(("--remote=ws://127.0.0.1:9999",)), "")

    @unittest.skipUnless(sys.version_info >= (3, 11), "Python 3.10 binds through native error URLs")
    def test_config_changed_within_startup_second_is_not_original_configuration(self):
        self.assertEqual(self.process_config(config_mtime=100.5), "")

    @unittest.skipUnless(sys.version_info >= (3, 11), "Python 3.10 binds through native error URLs")
    def test_prompt_after_option_terminator_is_not_a_provider_override(self):
        self.assertEqual(self.process_config(("--", '-cmodel_providers.custom.base_url="https://other.test/v1"')), "anyrouter.test")

    @unittest.skipUnless(sys.version_info >= (3, 11), "Python 3.10 binds through native error URLs")
    def test_replaced_configuration_cannot_bind_the_previous_service(self):
        self.assertEqual(self.process_config(replace_during_read=True), "")

    @unittest.skipUnless(sys.version_info >= (3, 11), "Python 3.10 binds through native error URLs")
    def test_legacy_and_named_profiles_are_not_conflated(self):
        profiles = {"other": '[model_providers.custom]\nbase_url="https://other.test/v1"\n'}
        self.assertEqual(self.process_config(named_profiles=profiles, extra_config='profile="other"\n'), "")
        self.assertEqual(self.process_config(("-pother",), named_profiles=profiles,
                                             extra_config='profiles={other={model_provider="custom"}}\n'), "")


class NetworkContinuationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_codex_status_chrome import status_payload, visible_text, ERRORS
        from tests.test_watch import FakeClient, armed_daemon
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        payload = status_payload("high_demand")
        self.client = FakeClient(payload, visible_text(payload))
        self.daemon = armed_daemon(self.tmp.name, self.client)
        self.addCleanup(self.daemon._process_snapshots.close)
        self.daemon._mutate_config(lambda c: c.update(network_guard={"enabled": True, "config_path": str(Path(self.tmp.name) / "network.json")}))
        self.turn = {"kind": "task_complete", "session_id": "original", "turn_id": "one", "at": 100,
                     "error": {"message": ERRORS["high_demand"]}}
        self.daemon.codex_queue_recovery.current_turn = mock.Mock(return_value=self.turn)
        self.verdict = mock.Mock(return_value={"blocked": True, "phase": "network_wait", "service_host": "anyrouter.test"})
        self.daemon.network.verdict = self.verdict

    def recover(self):
        self.verdict.return_value = {"blocked": False, "phase": "healthy", "service_host": "anyrouter.test"}

    def test_wait_and_recovery_preserve_failed_turn_deduplication(self):
        self.daemon.process_once(self.client)
        runtime = self.daemon.runtime["surface-uuid"]
        self.assertEqual(self.client.sent, [])
        self.assertEqual(runtime.state, "network_wait")
        self.assertEqual(runtime.send_count, 0)
        self.recover()
        self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)
        runtime.awaiting, runtime.last_send_at = False, 0
        self.daemon.process_once(self.client)
        self.assertEqual(len(self.client.sent), 1)

    def test_network_recovery_cannot_override_manual_pause(self):
        self.daemon.process_once(self.client)
        self.daemon._mutate_config(lambda c: c.update(global_paused=True))
        self.recover()
        self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

    def test_network_recovery_cannot_revive_a_completed_or_new_native_turn(self):
        self.daemon.process_once(self.client)
        self.recover()
        for replacement in ({**self.turn, "error": None}, {"kind": "task_started", "session_id": "new"}):
            self.daemon.codex_queue_recovery.current_turn.return_value = replacement
            self.daemon.process_once(self.client)
        self.assertEqual(self.client.sent, [])

    def test_b_stop_remains_an_independent_earlier_gate(self):
        target = self.daemon.config["targets"][0]
        with mock.patch("ccc_batch_guard.blocked", return_value=True):
            self.assertIsNone(self.daemon._active_send_target(target))
        self.verdict.assert_not_called()


if __name__ == "__main__":
    unittest.main()

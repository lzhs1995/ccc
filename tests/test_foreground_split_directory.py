"""Mirror native foreground-at-home and request-directory override semantics."""
import json
import unittest
from unittest.mock import patch

import ccc_client_thread_observation as binding
import cmux_supervisor_tui as tui
import test_client_thread_observation as fixtures


class SplitDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ForegroundTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.requests = f.home / 'request-headers'
        self.requests.mkdir(mode=0o700)
        self.env = {'CODEX_HOME': str(f.home), 'CODEX_CLIENT_THREAD_OBSERVER': '1',
                    'CODEX_CREDENTIAL_OBSERVATIONS_DIR': str(self.requests)}
        mocker = patch.object(binding.scope, 'arguments',
                             return_value=(['codex', 'resume', f.old], self.env))
        mocker.start()
        self.addCleanup(mocker.stop)
        f.publish()
        request = self.requests / f'{f.old}-{f.new}-request_attempt.json'
        request.write_text(json.dumps(dict(schema=1, observer_epoch=f.old, pid=f.pid,
            thread_id=f.new, purpose='request_attempt', transport='http',
            observed_at_ms=3000, authorization='Bearer fake-split-directory')))
        request.chmod(0o600)

    def test_native_home_selection_and_separate_request_directory(self):
        result = self.fixture.resolve()
        self.assertEqual(result.session_id, self.fixture.new)
        tui.observe_request_api_key(result)
        self.assertEqual(result.api_key_observed, 'fake-split-directory')

    def test_request_override_does_not_bypass_foreground_marker(self):
        (self.fixture.directory / 'enabled-v1').unlink()
        self.assertEqual(self.fixture.resolve().status, 'unknown')

    def test_request_directory_cannot_supply_foreground_selection(self):
        self.fixture.path.rename(self.requests / self.fixture.path.name)
        self.assertEqual(self.fixture.resolve().status, 'unknown')

    def test_missing_client_home_does_not_read_panel_home(self):
        f = self.fixture
        with patch.object(binding.scope, 'arguments', return_value=(['codex'], {
                'CODEX_CLIENT_THREAD_OBSERVER': '1'})), patch.object(
                    binding.Path, 'home', return_value=f.home.parent):
            self.assertEqual(f.read()[0], 'invalid')

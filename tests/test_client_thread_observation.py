import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ccc_client_thread_observation as binding
import cmux_supervisor_tui as tui


class ForegroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.directory = self.home / 'credential-observations'
        self.directory.mkdir(mode=0o700)
        marker = self.directory / 'enabled-v1'
        marker.write_bytes(b'ccc-request-credentials-v1\n')
        marker.chmod(0o600)
        self.pid = 912345
        self.path = self.directory / f'client-{self.pid}-thread.json'
        self.old = '01a0c2c0-e9af-7f01-b946-f9ebbb51d901'
        self.new = '01a0c2c0-27ac-7cc3-b050-f124fadc342c'
        self.data = dict(schema=1, purpose='client_foreground_thread', pid=self.pid,
                         client_epoch=self.old, thread_id=self.new, published_at_ms=2000)
        binding._seen.clear()
        self.addCleanup(binding._seen.clear)
        for name, value in [('birth', [1, 0]), ('arguments', (['codex', 'resume', self.old], {'CODEX_HOME': str(self.home)}))]:
            mocker = patch.object(binding.scope, name, return_value=value)
            mocker.start()
            self.addCleanup(mocker.stop)

    def publish(self):
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)

    def read(self):
        return binding.read_foreground(self.pid, tui.request_observation_directory_matches)

    def resolve(self):
        return tui.resolve_surface_session('codex', [self.pid],
            {self.pid: {'command': f'codex resume {self.old}'}}, {}, now=3)

    def test_current_selection_overrides_startup_resume(self):
        self.publish()
        result = self.resolve()
        self.assertEqual((result.status, result.session_id, result.tier), ('ok', self.new, 'codex-foreground'))
        self.assertIsNotNone(result.foreground_evidence)

    def test_null_and_lost_record_never_restore_old_argv(self):
        self.data['thread_id'] = None
        self.publish()
        self.assertEqual(self.resolve().status, 'unknown')
        self.path.unlink()
        self.assertEqual(self.resolve().status, 'unknown')

    def test_pid_reuse_and_malformed_record_rejected(self):
        self.data['published_at_ms'] = 999
        self.publish()
        self.assertEqual(self.read()[0], 'invalid')
        self.path.write_text('{}')
        self.assertEqual(self.read()[0], 'invalid')

    def test_symlink_and_removed_marker_rejected(self):
        self.publish()
        saved = self.directory / 'saved'
        self.path.rename(saved)
        self.path.symlink_to(saved)
        self.assertEqual(self.read()[0], 'invalid')
        self.path.unlink()
        saved.rename(self.path)
        (self.directory / 'enabled-v1').unlink()
        self.assertEqual(self.read()[0], 'invalid')

    def test_selection_change_changes_publication_evidence(self):
        self.publish()
        first = self.read()
        self.data['thread_id'] = self.old
        self.publish()
        second = self.read()
        self.assertEqual((first[0], second[0]), ('ok', 'ok'))
        self.assertNotEqual(first[2], second[2])

    def test_process_change_during_read_rejected(self):
        self.publish()
        with patch.object(binding.scope, 'birth', side_effect=[[1, 0], [2, 0]]):
            self.assertEqual(self.read()[0], 'invalid')

    def test_legacy_absence_is_not_native_evidence(self):
        self.assertEqual(self.read(), ('absent', None, None))
        self.assertIsNone(self.resolve().foreground_evidence)

    def test_reused_pid_does_not_inherit_another_process_observer_requirement(self):
        self.publish()
        self.assertEqual(self.read()[0], 'ok')
        self.path.unlink()
        with patch.object(binding.scope, 'birth', return_value=[3, 0]):
            self.assertEqual(self.read(), ('absent', None, None))

    def test_same_generation_lost_record_stays_invalid(self):
        self.publish()
        self.assertEqual(self.read()[0], 'ok')
        self.path.unlink()
        self.assertEqual(self.read(), ('invalid', None, None))

    def test_unmeasured_birth_cannot_restore_startup_session(self):
        self.publish()
        self.assertEqual(self.resolve().session_id, self.new)
        self.path.unlink()
        with patch.object(binding.scope, 'birth', return_value=None):
            self.assertEqual(self.read(), ('invalid', None, None))
            self.assertEqual(self.resolve().status, 'unknown')
        # Recovery of the original identity does not authorize argv fallback.
        self.assertEqual(self.resolve().status, 'unknown')
        self.publish()
        self.assertEqual(self.resolve().session_id, self.new)

    def test_unmeasured_birth_without_history_is_not_legacy_absence(self):
        with patch.object(binding.scope, 'birth', return_value=None):
            self.assertEqual(self.read(), ('invalid', None, None))
            self.assertEqual(self.resolve().status, 'unknown')

    def test_reused_pid_managed_client_still_requires_its_own_record(self):
        self.publish()
        self.assertEqual(self.read()[0], 'ok')
        self.path.unlink()
        env = {'CODEX_HOME': str(self.home), 'CODEX_CLIENT_THREAD_OBSERVER': '1'}
        with patch.object(binding.scope, 'birth', return_value=[3, 0]), \
             patch.object(binding.scope, 'arguments', return_value=(['codex'], env)):
            self.assertEqual(self.read(), ('invalid', None, None))

    def test_managed_launch_never_falls_back_when_publication_missing(self):
        env = {'CODEX_HOME': str(self.home), 'CODEX_CLIENT_THREAD_OBSERVER': '1'}
        with patch.object(binding.scope, 'arguments', return_value=(['codex', 'resume', self.old], env)):
            self.assertEqual(self.read(), ('invalid', None, None))
            self.publish()
            self.assertEqual(self.read()[0], 'ok')
            self.path.unlink()
            binding._seen.clear()  # A fresh panel must retain the same guarantee.
            self.assertEqual(self.read(), ('invalid', None, None))
            self.assertEqual(self.resolve().status, 'unknown')

    def test_unknown_observer_version_does_not_use_startup_session(self):
        env = {'CODEX_HOME': str(self.home), 'CODEX_CLIENT_THREAD_OBSERVER': '2'}
        with patch.object(binding.scope, 'arguments', return_value=(['codex', 'resume', self.old], env)):
            self.assertEqual(self.read(), ('invalid', None, None))

    def test_another_process_publication_during_key_read_clears_key(self):
        self.publish()
        request = self.directory / f'{self.old}-{self.new}-request_attempt.json'
        request.write_text(json.dumps(dict(schema=1, observer_epoch=self.old, pid=self.pid,
            thread_id=self.new, purpose='request_attempt', transport='http',
            observed_at_ms=3000, authorization='Bearer fake-session-key', api_key=None)))
        request.chmod(0o600)
        result = self.resolve()
        other = self.pid + 1
        result.foreground_observations += ((other, ('absent', None, None)),)
        original = binding.read_foreground
        def changed(pid, *args, **kwargs):
            if pid == other:
                return ('ok', self.old, ('new-selection',))
            return original(pid, *args, **kwargs)
        with patch.object(binding, 'read_foreground', changed):
            tui.observe_request_api_key(result, self.directory)
        self.assertEqual(result.api_key_observed, '')
        self.assertIn('当前会话改变', result.api_key_observation_note)

    def test_key_publication_rechecks_current_selection(self):
        import ccc_request_key_binding as connection
        self.publish()
        request = self.directory / f'{self.old}-{self.new}-request_attempt.json'
        request.write_text(json.dumps(dict(schema=1, observer_epoch=self.old, pid=self.pid,
            thread_id=self.new, purpose='request_attempt', transport='http',
            observed_at_ms=3000, authorization='Bearer fake-session-key', api_key=None)))
        request.chmod(0o600)
        result = self.resolve()
        tui.observe_request_api_key(result, self.directory)
        self.assertEqual(result.api_key_observed, 'fake-session-key')
        original = connection.connected_writer
        calls = 0
        def switch(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.data['thread_id'] = self.old
                self.publish()
            return original(*args, **kwargs)
        with patch.object(connection, 'connected_writer', switch):
            tui.observe_request_api_key(result, self.directory)
        self.assertEqual(calls, 2)
        self.assertEqual(result.api_key_observed, '')
        self.assertIn('当前会话改变', result.api_key_observation_note)

    def test_request_directory_uses_client_home_not_panel_home(self):
        self.publish()
        request = self.directory / f'{self.old}-{self.new}-request_attempt.json'
        request.write_text(json.dumps(dict(schema=1, observer_epoch=self.old, pid=self.pid,
            thread_id=self.new, purpose='request_attempt', transport='http',
            observed_at_ms=3000, authorization='Bearer fake-client-only')))
        request.chmod(0o600)
        result = self.resolve()
        with patch.object(Path, 'home', return_value=self.home / 'different-panel-home'):
            tui.observe_request_api_key(result)
        self.assertEqual(result.api_key_observed, 'fake-client-only')

    def test_request_directory_missing_client_home_never_uses_panel_home(self):
        self.publish()
        result = self.resolve()
        result.api_key_observed = 'old-key'
        with patch.object(binding.scope, 'arguments', return_value=(['codex'], {})):
            tui.observe_request_api_key(result)
        self.assertEqual(result.api_key_observed, '')


if __name__ == '__main__':
    unittest.main()

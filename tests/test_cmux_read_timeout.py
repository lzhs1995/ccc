import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import cmux_codex_watch as watch
from tests.test_watch import FakeClient, armed_daemon, grid_payload


def read_timeout():
    runner = mock.Mock(side_effect=subprocess.TimeoutExpired(['cmux', 'read-screen'], 8))
    try:
        watch.CmuxClient(runner=runner).read_screen('workspace-uuid', 'surface-uuid')
    except watch.CmuxError as exc:
        return exc
    raise AssertionError('timeout was not raised')


class ReadTimeoutTests(unittest.TestCase):
    def check_transient(self, operation, after_refresh=False, failure=None):
        payload = grid_payload(['previous output'], error='http_405')
        client = FakeClient(payload, '■ unexpected status 405 Method Not Allowed')
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, client)
            config_path = Path(directory)/'config.json'
            before = config_path.read_bytes()
            runtime = watch.TargetRuntime(episode_id='keep', send_count=8, last_send_at=123, awaiting=True,
                                          claude_completed_latched=True,
                                          claude_session_id='keep-session',
                                          delivery_status='unknown', send_attempt_id='keep-attempt')
            daemon.runtime['surface-uuid'] = runtime
            calls = 0

            def fail(*args):
                nonlocal calls
                calls += 1
                if after_refresh and calls == 1:
                    raise watch.CmuxError('workspace moved')
                raise failure if failure is not None else read_timeout()

            with mock.patch.object(client, operation, side_effect=fail), \
                 mock.patch.object(daemon, '_refresh_workspace', return_value=True):
                daemon.process_once(client)
            self.assertEqual(config_path.read_bytes(), before)
            self.assertFalse(daemon.config['targets'][0]['paused'])
            self.assertEqual(runtime.state, 'cmux_unavailable')
            self.assertEqual((runtime.episode_id, runtime.send_count, runtime.last_send_at), ('keep', 8, 123))
            self.assertTrue(runtime.awaiting)
            self.assertTrue(runtime.claude_completed_latched)
            self.assertEqual(runtime.claude_session_id, 'keep-session')
            self.assertEqual((runtime.delivery_status, runtime.send_attempt_id), ('unknown', 'keep-attempt'))
            self.assertEqual(client.sent, [])
            client.text = 'Working (0s • esc to interrupt)'
            with mock.patch.object(client, 'read_screen', return_value=client.text) as fresh_read:
                daemon.process_once(client)
                fresh_read.assert_called_once()
            self.assertEqual(client.sent, [])

    def test_socket_transport_failures_keep_initial_and_refreshed_reads_monitored(self):
        for detail in (
            'cmux read-screen --workspace failed: Error: Failed to write to socket (Broken pipe, errno 32)',
            'cmux --json rpc failed: Error: Failed to write to socket (Broken pipe, errno 32)',
            'cmux --json rpc failed: Error: No live cmux socket found. Tried:\n  /tmp/cmux.sock',
            'cmux --json rpc failed: Error: Connection reset by peer',
        ):
            for operation in ('read_screen', 'replay'):
                for after_refresh in (False, True):
                    with self.subTest(detail=detail, operation=operation, after_refresh=after_refresh):
                        self.check_transient(operation, after_refresh, watch.CmuxError(detail))

    def test_wrapped_connection_error_keeps_the_target_monitored(self):
        failure = watch.CmuxError('viewport transport interrupted')
        failure.__cause__ = ConnectionResetError('connection interrupted')
        self.check_transient('read_screen', failure=failure)

    def test_identity_errors_are_not_hidden_by_transport_text(self):
        for detail in (
            'surface not found',
            'not_found: Terminal surface not found',
            'invalid_params: Surface is not a terminal',
        ):
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as directory:
                client = FakeClient(grid_payload(['previous output']))
                daemon = armed_daemon(directory, client)
                failure = watch.CmuxError(detail + '; previous read: Broken pipe')
                with mock.patch.object(client, 'read_screen', side_effect=failure), \
                     mock.patch.object(daemon, '_refresh_workspace', return_value=False):
                    daemon.process_once(client)
                config = json.loads((Path(directory) / 'config.json').read_text())
                self.assertTrue(config['targets'][0]['paused'])
                self.assertEqual(client.sent, [])
    def test_initial_and_refreshed_read_timeout_remain_monitored(self):
        for after_refresh in (False, True):
            with self.subTest(after_refresh=after_refresh):
                self.check_transient('read_screen', after_refresh)

    def test_initial_and_refreshed_replay_timeout_remain_monitored(self):
        for after_refresh in (False, True):
            with self.subTest(after_refresh=after_refresh):
                self.check_transient('replay', after_refresh)

    def test_read_screen_internal_renderer_error_remains_monitored(self):
        client = FakeClient(grid_payload(['previous output']))
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, client)
            config_path = Path(directory) / 'config.json'
            before = config_path.read_bytes()
            with mock.patch.object(
                client,
                'read_screen',
                side_effect=watch.CmuxError(
                    'cmux read-screen --workspace failed: '
                    'Error: internal_error: Failed to read terminal text'
                ),
            ):
                daemon.process_once(client)
            self.assertEqual(config_path.read_bytes(), before)
            self.assertFalse(daemon.config['targets'][0]['paused'])
            self.assertEqual(daemon.runtime['surface-uuid'].state, 'cmux_unavailable')
            self.assertEqual(client.sent, [])

    def test_replay_command_error_after_refresh_remains_monitored(self):
        client = FakeClient(grid_payload(['previous output']))
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, client)
            config_path = Path(directory) / 'config.json'
            before = config_path.read_bytes()
            calls = 0

            def fail(*args):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise watch.CmuxError('workspace moved')
                raise watch.CmuxError(
                    "Command '['cmux', '--json', 'rpc', 'terminal.replay'] "
                    'timed out'
                )

            with mock.patch.object(client, 'read_screen', side_effect=fail), \
                    mock.patch.object(daemon, '_refresh_workspace', return_value=True):
                daemon.process_once(client)
            self.assertGreaterEqual(calls, 2)
            self.assertEqual(config_path.read_bytes(), before)
            self.assertFalse(daemon.config['targets'][0]['paused'])
            self.assertEqual(daemon.runtime['surface-uuid'].state, 'cmux_unavailable')

    def test_generic_missing_surface_error_still_isolates(self):
        client = FakeClient(grid_payload(['previous output']))
        with tempfile.TemporaryDirectory() as directory:
            daemon = armed_daemon(directory, client)
            with mock.patch.object(client, 'read_screen', side_effect=watch.CmuxError('surface not found')), \
                 mock.patch.object(daemon, '_refresh_workspace', return_value=False):
                daemon.process_once(client)
            config = json.loads((Path(directory)/'config.json').read_text())
            self.assertTrue(config['targets'][0]['paused'])
            self.assertEqual(client.sent, [])


if __name__ == '__main__':
    unittest.main()

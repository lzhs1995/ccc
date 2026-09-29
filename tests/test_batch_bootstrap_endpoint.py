"""Batch discovery reuse with real local sockets; no native processes."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

import ccc_workspace_batch as batch
import cmux_codex_watch as core


class BootstrapEndpointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ccc-endpoint-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)/'controller.sock'
        self.calls = []
        calls = self.calls
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                calls.append(request['method'])
                result = {'windows': [{'workspaces': [{'id': request['params']['workspace_id'], 'panes': []}]}]}
                self.wfile.write((json.dumps({'id':request['id'], 'ok':True, 'result':result})+'\n').encode())
        self.server = Server(str(self.path), Handler)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.01})
        thread.start()
        def close():
            self.server.shutdown(); self.server.server_close(); thread.join()
        self.addCleanup(close)
        self.config = {'cmux_path':'/original/cmux'}
        transport = core.CmuxViewportSocket()
        transport.configure({'protocol':'cmux-socket', 'version':2, 'socket_path':str(self.path),
                             'access_mode':'automation', 'methods':['system.tree']})
        self.endpoint = batch._bootstrap_endpoint(core.CmuxClient('/original/cmux', viewport_socket=transport), self.config)
        self.job = {'bootstrap_endpoint':self.endpoint}

    def test_fifty_bootstraps_use_only_original_tree_rpc_without_capabilities_cli(self):
        def read(_):
            return batch._bootstrap_client(self.config, self.job).workspace_tree('workspace')
        with patch.object(core.CmuxClient, 'capabilities', side_effect=AssertionError('rediscovery')), ThreadPoolExecutor(10) as pool:
            results = list(pool.map(read, range(50)))
        self.assertEqual(len(results), 50)
        self.assertEqual(self.calls, ['system.tree']*50)

    def test_replaced_socket_cannot_be_reused(self):
        self.path.rename(self.path.with_suffix('.old'))
        with socket.socket(socket.AF_UNIX) as replacement:
            replacement.bind(str(self.path))
            self.assertFalse(batch._bootstrap_endpoint_current(self.endpoint, self.config))
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                batch._bootstrap_client(self.config, self.job)
        self.assertEqual(self.calls, [])

    def test_changed_binary_rejects_original_discovery(self):
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            batch._bootstrap_client({'cmux_path':'/different/cmux'}, self.job)

    def test_missing_socket_rejects_without_fallback(self):
        self.path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            batch._bootstrap_client(self.config, self.job)

    def test_bootstrap_client_has_no_input_capability_or_cli_fallback(self):
        client = batch._bootstrap_client(self.config, self.job)
        with self.assertRaisesRegex(RuntimeError, 'no CLI fallback'):
            client.send_text('workspace', 'surface', 'text')
        self.assertEqual(self.calls, [])

    def test_legacy_job_keeps_original_discovery(self):
        with patch.object(batch, '_client', return_value='legacy') as create:
            self.assertEqual(batch._bootstrap_client(self.config, {}), 'legacy')
            create.assert_called_once_with(self.config)

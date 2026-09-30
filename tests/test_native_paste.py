"""Original daemon path, native provenance fixture and real local socket."""
import json
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

import cmux_codex_watch as core
from tests import test_native_enter as native_fixture


class NativePasteTests(unittest.TestCase):
    def exercise(self, fault=None):
        fixture = native_fixture.NativeEnterTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        temporary = tempfile.TemporaryDirectory(prefix='ccc-paste-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        rows = []
        pending_commits = []
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while line := self.rfile.readline():
                    req = json.loads(line)
                    method = req['method']
                    rows.append(method)
                    result = {'workspace_id':fixture.wid, 'surface_id':fixture.sid}
                    if method == 'system.tree': result = fixture.client.tree()
                    elif method == 'terminal.replay': result.update(fixture.client.payload)
                    elif method == 'terminal.paste':
                        assert pending_commits == ['paste_submit_pending']
                        assert fixture.runtime.codex_input_phase == 'paste_submit_pending'
                        assert req['params']['submit_key'] == 'enter'
                        if fault == 'eof': return
                        result.update(delivery='queued' if fault=='queued' else 'delivered', submitted=True)
                        if fault == 'legacy_ack': result.pop('delivery')
                        if fault == 'partial': result.update(submitted=False, submit_error='input_queue_full')
                        if fault == 'missing_id': result.pop('surface_id')
                    else: raise AssertionError(method)
                    self.wfile.write((json.dumps({'id': 'wrong' if fault=='id' and method=='terminal.paste' else req['id'],
                                                 'ok':True, 'result':result})+'\n').encode())
        server = Server(str(Path(temporary.name)/'c.sock'), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01})
        thread.start()
        def close():
            server.shutdown(); server.server_close(); thread.join()
        self.addCleanup(close)
        transport = core.CmuxViewportSocket(max_connections=1)
        transport.configure({'protocol':'cmux-socket', 'version':2, 'socket_path':str(server.server_address),
            'access_mode':'automation', 'methods':['system.tree','terminal.paste','surface.send_text','surface.send_key']})
        def no_cli(*a, **kw): raise AssertionError('unexpected CLI fallback')
        client = core.CmuxClient(runner=no_cli, viewport_socket=transport)
        persist = fixture.daemon._save_delivery
        def save(*a, **kw):
            if fixture.runtime.delivery_status == 'sending':
                if fault == 'persist_failure':
                    raise OSError('injected durable intent failure')
                pending_commits.append(fixture.runtime.codex_input_phase)
            result = persist(*a, **kw)
            if fault=='pause' and fixture.runtime.codex_input_phase=='paste_submit_pending':
                fixture.daemon.config['global_paused'] = True
            return result
        with patch.object(fixture.daemon, '_save_delivery', side_effect=save):
            fixture.daemon._handle_state(fixture.target, fixture.runtime, fixture.state, client,
                                         send_guard_tree=fixture.client.tree())
        self.assertNotIn('surface.send_text', rows)
        self.assertNotIn('surface.send_key', rows)
        self.assertEqual(rows.count('terminal.paste'), 0 if fault in {'pause','persist_failure'} else 1)
        if fault in {'eof','id','partial','missing_id'}:
            self.assertEqual(fixture.runtime.delivery_status, 'unknown')
            self.assertTrue(fixture.daemon._reconcile_codex_delivery(fixture.sid, fixture.runtime, fixture.state))
            self.assertEqual(rows.count('terminal.paste'),1)
        elif fault=='persist_failure':
            self.assertEqual(fixture.runtime.delivery_status,'failed')
            self.assertEqual(pending_commits, [])
        elif fault!='pause':
            self.assertEqual(fixture.runtime.delivery_status,'accepted')

    def test_single_paste_submit(self): self.exercise()
    def test_actual_native_ack_without_optional_delivery_field(self): self.exercise('legacy_ack')
    def test_queued_ack_requires_submitted_true(self): self.exercise('queued')
    def test_lost_ack_never_replays(self): self.exercise('eof')
    def test_wrong_ack_id_never_replays(self): self.exercise('id')
    def test_partial_submit_never_supplements(self): self.exercise('partial')
    def test_missing_target_ack_is_unknown(self): self.exercise('missing_id')
    def test_pause_during_persistence_prevents_paste(self): self.exercise('pause')
    def test_failed_initial_intent_prevents_all_input(self): self.exercise('persist_failure')

"""Official socket control avoids CLI resolution, with no ambiguous resend."""
import tempfile
import errno
import socket
import os
from pathlib import Path
import threading
import unittest
import uuid
from unittest import mock

import cmux_codex_watch as core
from tests.test_cmux_viewport_socket import response, send, server
from tests.test_watch import FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload


class ControlSocketTests(unittest.TestCase):
    def client(self, transport):
        transport.control_methods = frozenset({"system.tree", "system.top", "surface.send_text", "surface.send_key"})
        return core.CmuxClient(viewport_socket=transport,
            runner=mock.Mock(side_effect=AssertionError("unexpected CLI call")))

    def test_tree_and_top_are_single_scoped_rpc_calls(self):
        def handle(connection, request):
            result = {"windows": []}
            if request["method"] == "system.top":
                result["include_processes"] = request["params"].get("include_processes", False)
            send(connection, {"id": request["id"], "ok": True, "result": result})
        with server(handle) as (transport, requests):
            client = self.client(transport)
            self.assertEqual(client.tree(), {"windows": []})
            self.assertEqual(client.top_all(), {"windows": [], "include_processes": True})
            self.assertEqual(client.top("workspace"), {"windows": [], "include_processes": True})
            self.assertEqual([(r["method"], r["params"]) for r in requests], [
                ("system.tree", {"all": True}), ("system.top", {"all": True, "include_processes": True}),
                ("system.top", {"workspace_id": "workspace", "include_processes": True})])

    def test_native_live_frame_requests_zero_history_and_skips_text_prefilter(self):
        def handle(connection, request):
            frame = grid_payload([], error=HIGH_DEMAND_TEXT)
            frame['render_grid'].update(surface_id='surface', anchor='screen')
            send(connection, response(request, **frame))
        with server(handle) as (transport, requests):
            client = self.client(transport)
            transport.live_native_frames = True
            text, grid = client.read_viewport('workspace', 'surface', structured=False)
            self.assertIsNotNone(grid)
            self.assertIn(HIGH_DEMAND_TEXT, text)
            self.assertEqual([(r['method'], r['params']) for r in requests], [('terminal.replay', {
                'workspace_id':'workspace','surface_id':'surface','anchor':'screen','max_scrollback_rows':0})])
            client.runner.assert_not_called()

    def test_live_frame_missing_anchor_and_history_requests_fail_closed(self):
        with server(lambda c,r:send(c,response(r, **grid_payload([])))) as (transport, requests):
            client = self.client(transport)
            transport.live_native_frames = True
            with self.assertRaises(core.IncompatibleError):client.replay('workspace','surface')
            for rows in (None, 1, 200, False):
                with self.assertRaises(ValueError):transport.request('terminal.replay', {
                    'workspace_id':'workspace','surface_id':'surface','anchor':'screen','max_scrollback_rows':rows},timeout=1)
            self.assertEqual(len(requests),1)
            client.runner.assert_not_called()

    def test_top_without_processes_is_unavailable_not_an_empty_agent_inventory(self):
        def handle(connection, request):
            send(connection, {"id": request["id"], "ok": True,
                              "result": {"windows": [], "include_processes": False}})
        with server(handle) as (transport, _):
            with self.assertRaisesRegex(core.CmuxError, "omitted requested processes"):
                self.client(transport).top_all()

    def test_send_preserves_explicit_identity_and_terminal_enter(self):
        with server(lambda connection, request: send(connection, response(request))) as (transport, requests):
            self.client(transport).send("workspace", "surface", core.MESSAGE)
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0]["method"], "surface.send_text")
            self.assertEqual(requests[0]["params"], {"workspace_id": "workspace", "surface_id": "surface",
                                                   "text": core.MESSAGE})
            self.assertEqual(requests[1]["params"], {"workspace_id":"workspace", "surface_id":"surface", "key":"enter"})

    def test_draft_uses_one_socket_request_without_appending_enter(self):
        with server(lambda connection, request: send(connection, response(request))) as (transport, requests):
            client = self.client(transport)
            client.send_text("workspace", "surface", core.MESSAGE)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["params"], {"workspace_id":"workspace",
                "surface_id":"surface", "text":core.MESSAGE})
            client.runner.assert_not_called()

    def test_draft_lost_ack_is_not_repeated_via_cli(self):
        with server(lambda *_:None) as (transport, requests):
            client = self.client(transport)
            with self.assertRaises(core.UncertainDeliveryError):
                client.send_text("workspace", "surface", core.MESSAGE)
            self.assertEqual(len(requests), 1)
            client.runner.assert_not_called()

    def test_enter_and_own_queue_edit_use_single_scoped_key_request(self):
        for action,key in ((lambda c:c.send_key('workspace','surface','enter'),'enter'),
                           (lambda c:c.edit_codex_queued_prompt('workspace','surface'),'alt+up')):
            with self.subTest(key=key):
                with server(lambda connection, request: send(connection,response(request))) as (transport,requests):
                    client=self.client(transport);transport.control_methods|={'surface.send_key'}
                    action(client)
                    self.assertEqual(len(requests),1)
                    self.assertEqual(requests[0]['method'],'surface.send_key')
                    self.assertEqual(requests[0]['params'],{'workspace_id':'workspace','surface_id':'surface','key':key})
                    client.runner.assert_not_called()
                with server(lambda *_:None) as (transport,requests):
                    client=self.client(transport);transport.control_methods|={'surface.send_key'}
                    with self.assertRaises(core.UncertainDeliveryError):action(client)
                    self.assertEqual(len(requests),1)
                    client.runner.assert_not_called()

    def test_missing_or_bad_ack_never_falls_back_or_retries(self):
        for broken in ("id", "workspace_id", "surface_id", "eof", "json", "ok"):
            with self.subTest(broken=broken):
                def handle(connection, request):
                    if broken == "eof":
                        return
                    if broken == "json":
                        connection.sendall(b"bad-json\n")
                        return
                    result = response(request)
                    if broken in {"workspace_id", "surface_id"}:
                        result["result"][broken] = "other"
                    else:
                        result[broken] = False
                    send(connection, result)
                with server(handle) as (transport, requests):
                    client = self.client(transport)
                    with self.assertRaises(core.UncertainDeliveryError):
                        client.send("workspace", "surface", core.MESSAGE)
                    self.assertEqual(len(requests), 1)
                    client.runner.assert_not_called()

    def test_socket_timeout_is_uncertain_and_has_no_fallback(self):
        release = threading.Event()
        def handle(connection, request):
            release.wait(1)
            send(connection, response(request))
        with server(handle) as (transport, requests):
            client = self.client(transport)
            try:
                with self.assertRaises(core.UncertainDeliveryError):
                    client._control_rpc("surface.send_text", {"workspace_id": "workspace",
                        "surface_id": "surface", "text": core.MESSAGE + "\n"}, timeout=0.02)
            finally:
                release.set()
            client.runner.assert_not_called()
            self.assertEqual(len(requests), 1)

    def test_uncertain_control_send_reaches_daemon_delivery_ledger(self):
        from tests.native_failure_fixture import bind_native_failure
        payload = grid_payload([], error=HIGH_DEMAND_TEXT)
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "■ " + HIGH_DEMAND_TEXT)
            client.send = mock.Mock(side_effect=core.UncertainDeliveryError("lost socket acknowledgement"))
            daemon = armed_daemon(directory, client)
            bind_native_failure(daemon, HIGH_DEMAND_TEXT)
            with mock.patch.object(core.time, "time", return_value=1000):
                daemon.process_once(client)
                daemon.process_once(client)
            self.assertEqual(daemon.runtime["surface-uuid"].delivery_status, "unknown")
            self.assertEqual(client.send.call_count, 1)

    def test_control_requires_advertised_automation_support(self):
        transport = core.CmuxViewportSocket()
        transport.configure({"protocol": "cmux-socket", "version": 2, "socket_path": "/tmp/not-used",
                             "access_mode": "password", "methods": ["surface.send_text"]})
        self.assertEqual(transport.control_methods, frozenset())

    def test_clean_batch_retains_initial_input_and_interactive_shell(self):
        sid, wid, pane, window = (str(uuid.uuid4()) for _ in range(4))
        def handle(connection, request):
            send(connection, {"id": request["id"], "ok": True,
                              "result": {"surface_id": sid, "workspace_id": wid,
                                         "pane_id": pane, "window_id": window}})
        with server(handle) as (transport, requests):
            client = self.client(transport)
            transport.control_methods |= {"surface.create"}
            self.assertEqual(client.new_codex_surface(window, wid, pane, 'bootstrap', clean_shell=True), sid)
            self.assertEqual(requests[0]['params']['initial_command'], '/bin/zsh -f -i')
            self.assertEqual(requests[0]['params']['initial_input'], 'bootstrap\r')
            self.assertEqual(len(requests), 1)
            client.runner.assert_not_called()

    def test_batch_create_uses_initial_input_and_pinned_socket_identity(self):
        sid, wid, pane, window = (str(uuid.uuid4()) for _ in range(4))
        def handle(connection, request):
            send(connection, {"id": request["id"], "ok": True,
                              "result": {"surface_id": sid, "workspace_id": wid,
                                         "pane_id": pane, "window_id": window}})
        with server(handle) as (transport, requests):
            client = self.client(transport)
            transport.control_methods |= {"surface.create"}
            self.assertEqual(client.new_codex_surface(window, wid, pane, "python bootstrap.py"), sid)
            self.assertEqual(requests[0]["method"], "surface.create")
            self.assertEqual(requests[0]["params"], {
                "window_id": window, "workspace_id": wid, "pane_id": pane,
                "type": "terminal", "placement": "workspace", "focus": False,
                "initial_input": "python bootstrap.py\r"})
            client.runner.assert_not_called()

    def test_create_lost_or_conflicting_ack_never_creates_again_via_cli(self):
        sid, wid, pane, window = (str(uuid.uuid4()) for _ in range(4))
        for broken in ("eof", "id", "workspace", "surface", "dock", "pane"):
            with self.subTest(broken=broken):
                def handle(connection, request):
                    if broken == "eof":
                        return
                    result = {"surface_id": sid, "workspace_id": wid, "pane_id": pane}
                    if broken == "workspace": result["workspace_id"] = str(uuid.uuid4())
                    if broken == "surface": result["surface_id"] = "surface:9"
                    if broken == "dock": result["dock_surface_id"] = sid
                    if broken == "pane": result["pane_id"] = str(uuid.uuid4())
                    send(connection, {"id": "wrong" if broken == "id" else request["id"],
                                      "ok": True, "result": result})
                with server(handle) as (transport, requests):
                    client = self.client(transport)
                    transport.control_methods |= {"surface.create"}
                    with self.assertRaises(core.UncertainDeliveryError):
                        client.new_codex_surface(window, wid, pane, "python bootstrap.py")
                    self.assertEqual(len(requests), 1)
                    client.runner.assert_not_called()

    def test_local_accept_queue_pressure_retries_connect_but_sends_once(self):
        original = socket.socket.connect
        attempts = []
        def connect(connection, path):
            attempts.append(path)
            if len(attempts) <= 3:
                raise ConnectionRefusedError(errno.ECONNREFUSED, 'local accept queue full')
            return original(connection,path)
        with server(lambda connection, request: send(connection,response(request))) as (transport,requests):
            with mock.patch.object(socket.socket,'connect',connect):
                self.client(transport).send('workspace','surface',core.MESSAGE)
            self.assertEqual(len(attempts),5)
            self.assertEqual(len(requests),2)
            self.assertEqual(requests[0]['params']['text'],core.MESSAGE)

    def test_missing_socket_does_not_enter_connect_retry(self):
        connection=mock.Mock()
        connection.connect.side_effect=ConnectionRefusedError(errno.ECONNREFUSED,'gone')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConnectionRefusedError):
                core._connect_local_socket(connection,str(Path(directory)/'absent'),lambda:1)
        self.assertEqual(connection.connect.call_count,1)

    def test_replaced_socket_is_not_retried_again(self):
        connection=mock.Mock()
        connection.connect.side_effect=ConnectionRefusedError(errno.ECONNREFUSED,'queue full')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'original.sock'
            first,second=socket.socket(socket.AF_UNIX),socket.socket(socket.AF_UNIX)
            try:
                first.bind(str(path))
                def replace(_):
                    os.unlink(path)
                    second.bind(str(path))
                with mock.patch.object(core.time,'sleep',replace),self.assertRaises(ConnectionRefusedError):
                    core._connect_local_socket(connection,str(path),lambda:1)
                self.assertEqual(connection.connect.call_count,1)
            finally:
                first.close();second.close()

    def test_connect_retry_respects_original_deadline(self):
        with server(lambda *_:None) as (transport,requests):
            connection=mock.Mock()
            connection.connect.side_effect=ConnectionRefusedError(errno.ECONNREFUSED,'queue full')
            remaining=mock.Mock(side_effect=[1,1,TimeoutError('deadline')])
            with self.assertRaises(TimeoutError):
                core._connect_local_socket(connection,transport.path,remaining)
            self.assertEqual(connection.connect.call_count,1)
            self.assertEqual(requests,[])


if __name__ == "__main__":
    unittest.main()

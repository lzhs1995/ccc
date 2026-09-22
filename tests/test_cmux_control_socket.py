"""Official socket control avoids CLI resolution, with no ambiguous resend."""
import tempfile
import threading
import unittest
from unittest import mock

import cmux_codex_watch as core
from tests.test_cmux_viewport_socket import response, send, server
from tests.test_watch import FakeClient, HIGH_DEMAND_TEXT, armed_daemon, grid_payload


class ControlSocketTests(unittest.TestCase):
    def client(self, transport):
        transport.control_methods = frozenset({"system.tree", "system.top", "surface.send_text"})
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

    def test_top_without_processes_is_unavailable_not_an_empty_agent_inventory(self):
        def handle(connection, request):
            send(connection, {"id": request["id"], "ok": True,
                              "result": {"windows": [], "include_processes": False}})
        with server(handle) as (transport, _):
            with self.assertRaisesRegex(core.CmuxError, "omitted requested processes"):
                self.client(transport).top_all()

    def test_send_preserves_explicit_identity_and_ordered_newline(self):
        with server(lambda connection, request: send(connection, response(request))) as (transport, requests):
            self.client(transport).send("workspace", "surface", core.MESSAGE)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["method"], "surface.send_text")
            self.assertEqual(requests[0]["params"], {"workspace_id": "workspace", "surface_id": "surface",
                                                   "text": core.MESSAGE + "\n"})

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
        payload = grid_payload([], error=HIGH_DEMAND_TEXT)
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient(payload, "■ " + HIGH_DEMAND_TEXT)
            client.send = mock.Mock(side_effect=core.UncertainDeliveryError("lost socket acknowledgement"))
            daemon = armed_daemon(directory, client)
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


if __name__ == "__main__":
    unittest.main()

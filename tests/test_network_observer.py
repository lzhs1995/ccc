import http.server
import ipaddress
import ctypes
import socket
import ssl
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_mihomo import PhysicalLink, ProbeResult, ResponsesProbe, _IfAddrs, physical_ipv4, route
from ccc_network_guard import Engine


class LocalProbeFailureTests(unittest.TestCase):
    def probe(self, port, timeout=.2):
        item = route("test", {"name": "test", "type": "http", "server": "127.0.0.1", "port": 1})
        config = {"url": "https://example.invalid/v1/responses", "model": "test-model",
                  "allow_unauthenticated_test": True, "timeout_sec": timeout, "deep_timeout_sec": timeout}
        return item, ResponsesProbe(config, {item.id: port})

    def server(self, mode):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_CONNECT(self):
                if mode == "reject":
                    self.send_error(502)
                elif mode == "slow_headers":
                    time.sleep(1)
                else:
                    self.send_response(200)
                    self.end_headers()
                    time.sleep(1)
            def log_message(self, *_):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def test_closed_local_listener_is_not_remote_node_failure(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        item, probe = self.probe(port)
        for deep in (False, True):
            with self.subTest(deep=deep):
                result = probe.run(item, deep)
                self.assertEqual(result.kind, "observer_error")
                self.assertEqual(result.stage, "observer_connect")

    def test_local_setup_delay_does_not_quarantine_a_remote_exit(self):
        item, probe = self.probe(1, timeout=.02)
        context = ssl.create_default_context()
        def delayed_context():
            time.sleep(.04)
            return context
        with mock.patch("ccc_mihomo.ssl.create_default_context", side_effect=delayed_context):
            result = probe.run(item)
        self.assertEqual(result.kind, "observer_error")
        self.assertEqual(result.stage, "setup")

    def test_remote_connect_rejection_remains_a_route_failure(self):
        item, probe = self.probe(self.server("reject"))
        result = probe.run(item)
        self.assertEqual(result.kind, "transport")
        self.assertEqual(result.stage, "tunnel")

    def test_connect_headers_and_remote_tls_obey_total_deadline(self):
        for mode, stage in (("slow_headers", "tunnel"), ("slow_tls", "tls")):
            with self.subTest(mode=mode):
                item, probe = self.probe(self.server(mode))
                started = time.monotonic()
                result = probe.run(item)
                self.assertEqual(result.kind, "timeout")
                self.assertEqual(result.stage, stage)
                self.assertLess(time.monotonic() - started, .6)


class PhysicalLinkTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform in {"darwin", "linux"}, "native getifaddrs platforms")
    def test_native_snapshot_reads_only_physical_ipv4_addresses(self):
        self.assertEqual(physical_ipv4("ccc-missing-fixture"), ())
        for _, name in socket.if_nameindex():
            for value in physical_ipv4(name):
                address = ipaddress.ip_address(value)
                self.assertEqual(address.version, 4)
                self.assertFalse(address.is_loopback)
                self.assertFalse(address.is_unspecified)
                self.assertFalse(address.is_link_local)

    @unittest.skipUnless(sys.platform in {"darwin", "linux"}, "native getifaddrs platforms")
    def test_dhcp_self_assigned_addresses_are_not_a_working_physical_link(self):
        addresses = ("0.0.0.0", "127.0.0.1", "169.254.8.42", "192.168.5.12", "10.1.2.3")
        buffers, entries = [], []
        for value in addresses:
            family = bytes((16, socket.AF_INET)) if sys.platform == "darwin" else socket.AF_INET.to_bytes(2, sys.byteorder)
            buffers.append(ctypes.create_string_buffer(family + b"\0\0" + socket.inet_aton(value)))
            entries.append(_IfAddrs(name=b"fixture0", flags=0x41,
                                   address=ctypes.cast(buffers[-1], ctypes.c_void_p)))
        for left, right in zip(entries, entries[1:]):
            left.next = ctypes.pointer(right)
        library = mock.Mock()
        def populate(output):
            ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(_IfAddrs)))[0] = ctypes.pointer(entries[0])
            return 0
        library.getifaddrs.side_effect = populate
        with mock.patch("ccc_mihomo.ctypes.CDLL", return_value=library):
            self.assertEqual(physical_ipv4("fixture0"), ("10.1.2.3", "192.168.5.12"))
            entries[2].next = ctypes.POINTER(_IfAddrs)()
            self.assertEqual(physical_ipv4("fixture0"), ())
        self.assertEqual(library.freeifaddrs.call_count, 2)

    def test_link_down_and_recovery_invalidate_old_success_and_failure(self):
        reader = mock.Mock(return_value=("192.0.2.10",))
        link = PhysicalLink("test0", reader)
        first = link.sample()
        self.assertTrue(link.accepts(first))
        self.assertEqual(link.sample(), first)
        reader.return_value = ()
        self.assertIsNone(link.sample())
        self.assertFalse(link.accepts(first))
        reader.return_value = ("192.0.2.10",)
        recovered = link.sample()
        self.assertNotEqual(first, recovered)
        self.assertFalse(link.accepts(first))
        self.assertTrue(link.accepts(recovered))
        transitions = link.changes()
        self.assertEqual([event["available"] for event in transitions], [True, False, True])
        self.assertEqual([event["generation"] for event in transitions], [1, 2, 3])
        self.assertEqual(link.changes(), [])

    def test_address_change_also_invalidates_in_flight_evidence(self):
        reader = mock.Mock(return_value=("192.0.2.10",))
        link = PhysicalLink("test0", reader)
        first = link.sample()
        reader.return_value = ("192.0.2.11",)
        self.assertNotEqual(first, link.sample())
        self.assertFalse(link.accepts(first))

    def test_link_changes_are_seen_while_the_director_is_busy(self):
        down_seen = threading.Event()
        state = [("192.0.2.10",)]
        def reader(_):
            if not state[0]:
                down_seen.set()
            return state[0]
        link = PhysicalLink("test0", reader)
        link.start(interval=.01)
        self.addCleanup(link.close)
        first = link.sample()
        state[0] = ()
        self.assertTrue(down_seen.wait(timeout=1))
        state[0] = ("192.0.2.10",)
        recovered = link.sample()
        self.assertNotEqual(first, recovered)
        self.assertFalse(link.accepts(first))
        self.assertTrue(link.accepts(recovered))

    def test_inspection_failure_is_an_observer_failure(self):
        reader = mock.Mock(return_value=("192.0.2.10",))
        link = PhysicalLink("test0", reader)
        first = link.sample()
        reader.side_effect = OSError("fixture")
        self.assertIsNone(link.sample())
        self.assertIn("inspection failed", link.detail)
        self.assertFalse(link.accepts(first))


class RecoverySchedulingTests(unittest.TestCase):
    def setUp(self):
        pools = ("NTHU", "NTHU", "NTHU", "Yeye", "Yeye", "Tokyo", "Tokyo")
        self.routes = [route(pool, {"name": "route-" + str(i), "type": "http",
                                  "server": "127.0.0.1", "port": i + 1000}, priority=i)
                       for i, pool in enumerate(pools)]
        self.engine = Engine({"commercial_pools": ["NTHU", "Yeye"], "policy": {}}, self.routes)
        self.ids = [r.id for r in self.routes]
        self.engine.current = self.ids[0]
        for rid in self.ids:
            self.engine.record(rid, ProbeResult("accessible"), 1000)
            self.engine.record(rid, ProbeResult("healthy", deep=True), 1000.1)

    def test_recovery_probes_prefer_accessible_candidate_in_missing_pool(self):
        first, second = self.ids[3:5]
        for rid in (first, second):
            self.engine.record(rid, ProbeResult("timeout"), 1001)
            self.engine.record(rid, ProbeResult("timeout"), 1002)
        self.engine.record(first, ProbeResult("timeout"), 1063)
        self.engine.record(second, ProbeResult("accessible"), 1063)
        self.assertIn(second, self.engine.standbys(1064))
        self.assertNotIn(first, self.engine.standbys(1064))
        self.assertFalse(self.engine.ready(second, 1064))
        self.assertTrue(self.engine.health[second].quarantined)

    def test_overdue_inventory_route_cannot_starve_behind_slow_hot_probes(self):
        for rid in self.ids:
            health = self.engine.health[rid]
            health.light_attempt_at = health.light_at = health.light_ok_at = 1104
        cold = self.ids[2]
        health = self.engine.health[cold]
        health.light_attempt_at = health.light_at = health.light_ok_at = 1000
        self.assertNotIn(cold, self.engine.standbys(1110))
        due = self.engine.light_due(1110, {self.ids[0]})
        self.assertEqual(due[0], cold)
        self.assertEqual(self.engine.light_due(1110, set())[0], self.ids[0])


if __name__ == "__main__":
    unittest.main()

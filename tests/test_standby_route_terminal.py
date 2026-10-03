"""Actual local resource observations, without provider requests."""
import socket
import threading
import time
import unittest
from urllib.parse import urlsplit

from ccc_standby_routes import RouteObserver


class RouteTerminalTests(unittest.TestCase):
    def route(self):
        route = RouteObserver(['http://127.0.0.1:9/v1'], allow_local=True)
        self.addCleanup(route.close)
        return route

    def until(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.005)
        self.fail('local resources did not reach expected state')

    def test_closed_flag_alone_is_not_resource_terminal(self):
        route = self.route()
        with route._lock:
            route._closed = True
        try:
            report = route.report()
            self.assertTrue(report['closed'])
            self.assertFalse(report['resources_released'])
            self.assertFalse(report['resources']['listener_closed'])
        finally:
            with route._lock:
                route._closed = False
        route.close()
        self.assertTrue(route.report()['resources_released'])

    def test_partial_request_shutdown_releases_actual_socket_and_worker(self):
        route = self.route()
        endpoint = urlsplit(route.urls[0])
        client = socket.create_connection((endpoint.hostname, endpoint.port), timeout=2)
        self.addCleanup(client.close)
        client.sendall(b'GET ')
        self.until(lambda: route.report()['resources']['worker_threads_alive'] == 1)
        before = route.report()
        self.assertEqual(before['pending_connections'], 1)
        self.assertEqual(before['resources']['downstream_connections'], 1)
        self.assertFalse(before['resources_released'])
        route.close()
        self.until(lambda: route.report()['resources_released'])
        after = route.report()
        self.assertEqual(after['resources']['worker_threads_alive'], 0)
        self.assertEqual(after['resources']['downstream_connections'], 0)
        self.assertEqual(after['pending_connections'], 0)
        self.assertEqual(client.recv(1), b'')

    def test_lingering_tracked_worker_prevents_terminal_after_listener_close(self):
        route = self.route()
        release = threading.Event()
        worker = threading.Thread(target=release.wait, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(release.set)
        route._track_worker(worker)
        route.close()
        self.assertFalse(route.report()['resources_released'])
        self.assertEqual(route.report()['resources']['worker_threads_alive'], 1)
        release.set()
        worker.join(2)
        self.assertTrue(route.report()['resources_released'])


if __name__ == '__main__':
    unittest.main()

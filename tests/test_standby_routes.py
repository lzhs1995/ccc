import contextlib
from concurrent.futures import ThreadPoolExecutor
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import unittest
import uuid
from urllib.parse import urlsplit

from ccc_standby_routes import RouteObserver


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.headers.get('Upgrade') == 'websocket':
            self.send_response_only(101)
            self.send_header('Connection', 'Upgrade')
            self.send_header('Upgrade', 'websocket')
            self.end_headers()
            self.wfile.write(b'first-frame')
            self.wfile.flush()
            data = self.rfile.read(4)
            self.wfile.write(data)
            self.wfile.flush()
            self.close_connection = True
            return
        self.server.seen.append((self.command, self.path, dict(self.headers), b''))
        self.send_response_only(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'{}')

    def do_POST(self):
        if self.headers.get('Transfer-Encoding') == 'chunked':
            body = b''
            while True:
                size = int(self.rfile.readline().strip(), 16)
                if not size:
                    self.rfile.readline()
                    break
                body += self.rfile.read(size)
                self.rfile.read(2)
        else:
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.server.seen.append((self.command, self.path, dict(self.headers), body))
        if self.path.endswith('/redirect'):
            self.send_response_only(307)
            self.send_header('Location', 'https://example.invalid/no-follow')
            self.send_header('Content-Length', '0')
            self.end_headers()
        elif self.path.endswith('/stream'):
            self.send_response_only(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            for part in [b'data: one\n\n', b'data: two\n\n']:
                self.wfile.write(f'{len(part):x}\r\n'.encode() + part + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
        else:
            self.send_response_only(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class RoutesTest(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.daemon_threads = True
        self.upstream.seen = []
        self.thread = threading.Thread(target=self.upstream.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.routes = RouteObserver([f'http://127.0.0.1:{self.upstream.server_port}/v1'] * 50,
                                    allow_local=True, timeout=2)
        self.addCleanup(self.close)

    def close(self):
        self.routes.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.thread.join()

    def request(self, index=0, *, method='POST', suffix='/responses', body=b'{"input":"keep original"}', headers=None, chunked=False):
        url = urlsplit(self.routes.urls[index])
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
        try:
            connection.request(method, url.path + suffix, body=body, headers=headers or {}, encode_chunked=chunked)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def raw(self, payload):
        url = urlsplit(self.routes.urls[0])
        connection = socket.create_connection((url.hostname, url.port), timeout=3)
        try:
            connection.sendall(payload.replace(b'PATH', url.path.encode()))
            data = b''
            while part := connection.recv(65536):
                data += part
            return data
        finally:
            connection.close()

    def test_standby_rejects_and_permanently_counts_title_or_model(self):
        self.assertEqual(self.routes.zero(0)['before_activation_model_requests'], 0)
        self.assertEqual(self.request(body=b'{"title":true}')[0], 503)
        self.assertEqual(self.upstream.seen, [])
        self.assertEqual(self.routes.report()['slots'][0]['before_activation'], 1)
        with self.assertRaises(ValueError):
            self.routes.zero(0)
        with self.assertRaises(ValueError):
            self.routes.release(str(uuid.uuid4()))

    def test_catalog_is_not_a_model_request(self):
        self.assertEqual(self.request(method='GET', suffix='/models?version=1', body=None)[0], 200)
        self.assertEqual(self.routes.zero(0)['before_activation_model_requests'], 0)
        self.assertEqual(self.upstream.seen[0][1], '/v1/models?version=1')

    def test_lookalike_catalog_is_not_exempt(self):
        for suffix in ['/other/models', '/models/infer', '/models/../responses']:
            self.assertEqual(self.request(method='GET', suffix=suffix, body=None)[0], 503)
        self.assertFalse(self.upstream.seen)

    def test_original_body_authorization_and_response_preserved(self):
        self.routes.release(str(uuid.uuid4()))
        body = b'{"model":"original", "input":[1,2], "tools":[{"type":"x"}]}'
        status, headers, response = self.request(body=body, headers={'Authorization': 'Bearer test-only', 'X-Original': 'same'})
        self.assertEqual((status, response), (200, body))
        command, path, headers, seen = self.upstream.seen[0]
        self.assertEqual((command, path, seen), ('POST', '/v1/responses', body))
        self.assertEqual(headers['Authorization'], 'Bearer test-only')
        self.assertEqual(headers['X-Original'], 'same')
        self.assertEqual(self.routes.report()['slots'][0]['forwarded'], 1)

    def test_stream_preserves_events(self):
        self.routes.release(str(uuid.uuid4()))
        status, headers, data = self.request(suffix='/stream')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'text/event-stream')
        self.assertEqual(data, b'data: one\n\ndata: two\n\n')

    def test_chunked_request_preserves_body(self):
        self.routes.release(str(uuid.uuid4()))
        self.assertEqual(self.request(body=iter([b'one', b'two']), chunked=True)[2], b'onetwo')

    def test_redirect_cannot_send_native_outside_observer(self):
        self.routes.release(str(uuid.uuid4()))
        status, headers, _ = self.request(suffix='/redirect')
        self.assertEqual(status, 502)
        self.assertNotIn('Location', headers)
        self.assertEqual(len(self.upstream.seen), 1)

    def test_websocket_initial_and_later_frames_preserved(self):
        self.routes.release(str(uuid.uuid4()))
        url = urlsplit(self.routes.urls[0])
        connection = socket.create_connection((url.hostname, url.port), timeout=3)
        self.addCleanup(connection.close)
        connection.sendall(f'GET {url.path}/responses HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n'.encode())
        reader = connection.makefile('rb')
        self.addCleanup(reader.close)
        self.assertIn(b'101', reader.readline())
        while reader.readline() != b'\r\n':
            pass
        self.assertEqual(reader.read(11), b'first-frame')
        connection.sendall(b'ping')
        self.assertEqual(reader.read(4), b'ping')

    def test_long_request_counted_before_414(self):
        result = self.raw(b'POST PATH/' + b'x' * 65536 + b' HTTP/1.1\r\nHost: localhost\r\n\r\n')
        self.assertIn(b'414', result.split(b'\r\n')[0])
        with self.assertRaises(ValueError):
            self.routes.zero(0)
        self.assertEqual(self.routes.report()['slots'][0]['requests'], 1)

    def test_bad_headers_counted_before_431(self):
        self.assertIn(b'431', self.raw(b'POST PATH/responses HTTP/1.1\r\n' + b'X: ' + b'x' * 65536 + b'\r\n\r\n').split(b'\r\n')[0])
        self.assertEqual(self.routes.report()['slots'][0]['requests'], 1)
        with self.assertRaises(ValueError):
            self.routes.release(str(uuid.uuid4()))

    def test_unknown_request_invalidates_zero_claim(self):
        self.assertIn(b'404', self.raw(b'GET /foreign HTTP/1.1\r\nHost: localhost\r\n\r\n').split(b'\r\n')[0])
        with self.assertRaises(ValueError):
            self.routes.zero(0)

    def test_pending_socket_cannot_be_called_zero(self):
        url = urlsplit(self.routes.urls[0])
        connection = socket.create_connection((url.hostname, url.port), timeout=3)
        self.addCleanup(connection.close)
        # A subsequent completed connection proves the listener accepted the
        # preceding socket, without relying on a timing sleep.
        self.request(method='GET', suffix='/models', body=None)
        with self.assertRaises(ValueError):
            self.routes.zero(0)

    def test_fifty_parallel_catalogs_keep_slot_counts(self):
        barrier = threading.Barrier(50)
        def request(index):
            barrier.wait()
            return self.request(index, method='GET', suffix='/models', body=None)[0]
        with ThreadPoolExecutor(max_workers=50) as pool:
            self.assertEqual(list(pool.map(request, range(50))), [200] * 50)
        self.assertEqual([r['requests'] for r in self.routes.report()['slots']], [1] * 50)
        self.assertEqual(self.routes.zero(49)['before_activation_model_requests'], 0)

    def test_close_and_action_replacement_refused(self):
        action = str(uuid.uuid4())
        self.routes.release(action)
        self.routes.release(action)
        with self.assertRaises(ValueError):
            self.routes.release(str(uuid.uuid4()))
        self.routes.close()
        with self.assertRaises(ValueError):
            self.routes.zero(0)

    def test_endpoint_rejects_credentials_remote_http_and_query(self):
        for endpoint in ['http://example.com', 'https://u:p@example.com',
                         'https://example.com?token=value', 'https://example.com/#x']:
            with self.assertRaises(ValueError):
                RouteObserver([endpoint])


if __name__ == '__main__':
    unittest.main()

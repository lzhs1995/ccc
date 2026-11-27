"""Observe the original model route without rewriting prompts or responses.

Each slot gets a private loopback URL mapped to its original provider URL.
Before activation, model requests are counted and rejected locally. Catalog
GETs may pass; they do not invoke a model. After release, HTTP bodies, SSE and
WebSocket bytes pass through unchanged. No request body or credential is logged.
This observes only explicitly routed providers, not arbitrary plugin traffic.
"""
from __future__ import annotations

import contextlib
import base64
import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit


LINE_LIMIT = 65536
BLOCK = 65536


def _endpoint(url, *, local=False):
    parsed = urlsplit(url)
    if (parsed.scheme not in ('https', 'http') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or '\\' in url
            or any(ord(c) <= 32 for c in url)
            or (parsed.scheme != 'https' and
                not (local and parsed.hostname in ('localhost', '127.0.0.1', '::1')))):
        raise ValueError('original HTTPS provider URL required')
    parsed.port  # Validate the port before binding a listener.
    return parsed


class RouteObserver:
    """One cohort's continuously running, non-restartable route observer.

    `routes` is an ordered list of the exact upstream URL for every slot.
    `release` is called by the owner only after durable activation consumption.
    An attempted preactivation inference permanently prevents a zero proof.
    """
    def __init__(self, routes, *, allow_local=False, timeout=60.0, proxy_url=None, ca_file=None):
        if not routes or type(timeout) not in (int, float) or not 0 < timeout <= 3600:
            raise ValueError('provider routes and bounded timeout required')
        self._urls = tuple(routes)
        self._routes = tuple(_endpoint(url, local=allow_local) for url in routes)
        self._timeout = timeout
        self._proxy = urlsplit(proxy_url) if proxy_url else None
        if self._proxy and (self._proxy.scheme != 'http' or not self._proxy.hostname
                            or self._proxy.query or self._proxy.fragment
                            or self._proxy.path not in ('', '/')):
            raise ValueError('selected upstream proxy requires an HTTP CONNECT URL')
        self._ssl = ssl.create_default_context(cafile=ca_file)
        self._token = secrets.token_hex(24)
        self._lock = threading.RLock()
        self._closed = self._failed = False
        self._action = None
        self._connections = set()
        self._workers = set()
        self._upstreams = {}
        self._pending = 0
        self._accepted = 0
        self._records = [dict(requests=0, model_attempts=0, before_activation=0,
                              forwarded=0, failures=0) for _ in routes]
        self._unattributed = 0
        self._created_ns = time.monotonic_ns()
        owner = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 128

            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(owner._timeout)
                with owner._lock:
                    owner._connections.add(connection)
                    owner._accepted += 1
                    owner._pending += 1
                return connection, address

            def close_request(self, request):
                super().close_request(request)
                with owner._lock:
                    owner._connections.discard(request)

            def process_request_thread(self, request, client_address):
                owner._track_worker(threading.current_thread())
                super().process_request_thread(request, client_address)

            def handle_error(self, request, address):
                # Never allow an unhandled parser/transport error to support
                # a zero-request claim; stderr must not contain headers.
                with owner._lock:
                    owner._failed = True

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def handle(self):
                # One HTTP request per downstream connection. Normal HTTP
                # responses advertise close; upgraded sockets stay a tunnel.
                # Count before parse_request, including the 414/431 branches.
                resolved = False
                counted = False
                self.response_started = False
                try:
                    self.close_connection = True
                    self.raw_requestline = self.rfile.readline(LINE_LIMIT + 1)
                    if not self.raw_requestline:
                        return
                    parts = self.raw_requestline.split()
                    index = owner._index(parts[1]) if len(parts) >= 2 else None
                    with owner._lock:
                        if index is None:
                            owner._unattributed += 1
                        else:
                            owner._records[index]['requests'] += 1
                            counted = True
                    if len(self.raw_requestline) > LINE_LIMIT:
                        self.requestline = self.request_version = self.command = ''
                        self.send_error(414)
                        owner._bad(index)
                        return
                    if not self.parse_request():
                        owner._bad(index)
                        return
                    if index is None:
                        self.send_error(404)
                        return
                    self.request_framing = owner._framing(self.headers)
                    path = owner._path(self.path, index)
                    catalog = self.command == 'GET' and urlsplit(path).path.rstrip('/').endswith('/models')
                    # Only the provider's exact catalog path is non-model.
                    catalog = catalog and urlsplit(path).path.rstrip('/') == (
                        owner._routes[index].path.rstrip('/') + '/models')
                    # A catalog exception permits only an ordinary empty GET,
                    # never an upgraded tunnel or a body-bearing operation.
                    catalog = (catalog and not self.headers.get('Upgrade')
                        and 'upgrade' not in self.headers.get('Connection', '').lower()
                        and not self.headers.get('Transfer-Encoding')
                        and self.headers.get('Content-Length', '0') == '0')
                    with owner._lock:
                        if not catalog:
                            record = owner._records[index]
                            record['model_attempts'] += 1
                            if owner._action is None:
                                record['before_activation'] += 1
                        allowed = (not owner._closed and not owner._failed
                                   and (catalog or owner._action is not None))
                        owner._pending -= 1
                        resolved = True
                    if not allowed:
                        self.send_error(503, 'standby model route is not released')
                        return
                    owner._forward(self, index, path)
                except (OSError, ValueError, http.client.HTTPException):
                    owner._bad(index if counted else None)
                    if not self.response_started:
                        with contextlib.suppress(OSError):
                            self.send_error(502, 'upstream transport failed')
                finally:
                    if not resolved:
                        with owner._lock:
                            owner._pending -= 1
                    self.close_connection = True

        self._server = Server(('127.0.0.1', 0), Handler)
        self._server_identity = self._server.socket.fileno()
        self._thread = threading.Thread(target=self._serve, name='ccc-standby-routes', daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            self._server.serve_forever(poll_interval=.05)
        finally:
            with self._lock:
                if not self._closed:
                    self._failed = True

    def _track_worker(self, thread):
        with self._lock:
            self._workers = {worker for worker in self._workers if worker.is_alive()}
            self._workers.add(thread)

    def _index(self, target):
        try:
            path = target.decode('ascii')
        except UnicodeError:
            return None
        match = re.match(r'^/' + self._token + r'/(0|[1-9][0-9]*)(?:/|\?|$)', path)
        if not match:
            return None
        index = int(match[1])
        return index if 0 <= index < len(self._routes) else None

    def _path(self, target, index):
        prefix = f'/{self._token}/{index}'
        suffix = target[len(prefix):]
        if not suffix or suffix.startswith('?'):
            suffix = '/' + suffix
        if not suffix.startswith('/') or '\r' in suffix or '\n' in suffix:
            raise ValueError('invalid routed request path')
        return self._routes[index].path.rstrip('/') + suffix

    def _bad(self, index):
        with self._lock:
            if index is None:
                self._failed = True
            else:
                self._records[index]['failures'] += 1
                # A malformed request cannot disappear from standby evidence.
                if self._action is None:
                    self._records[index]['before_activation'] += 1

    def _connect(self, index):
        route = self._routes[index]
        owner = self

        class HTTPS(http.client.HTTPSConnection):
            def connect(connection):
                http.client.HTTPConnection.connect(connection)
                # Register the TLS socket before its blocking handshake. A
                # concurrent close must interrupt raw CONNECT and TLS reads.
                connection.sock = connection._context.wrap_socket(connection.sock,
                    server_hostname=connection._tunnel_host or connection.host,
                    do_handshake_on_connect=False)
                owner._track(connection, connection.sock)
                connection.sock.do_handshake()

        if self._proxy:
            if route.scheme != 'https':
                raise ValueError('upstream proxy requires HTTPS provider')
            connection = HTTPS(self._proxy.hostname, self._proxy.port or 80,
                                                     timeout=self._timeout, context=self._ssl)
            headers = {}
            if self._proxy.username is not None:
                from urllib.parse import unquote
                auth = unquote(self._proxy.username) + ':' + unquote(self._proxy.password or '')
                headers['Proxy-Authorization'] = 'Basic ' + base64.b64encode(auth.encode()).decode()
            connection.set_tunnel(route.hostname, route.port or 443, headers=headers)
        else:
            cls = HTTPS if route.scheme == 'https' else http.client.HTTPConnection
            connection = cls(route.hostname, route.port, timeout=self._timeout,
                       **({'context': self._ssl} if route.scheme == 'https' else {}))
        with self._lock:
            self._live_locked()
            self._upstreams[connection] = set()
        create = connection._create_connection
        def connect_socket(*args, **kwargs):
            stream = create(*args, **kwargs)
            self._track(connection, stream)
            return stream
        connection._create_connection = connect_socket
        return connection

    def _track(self, connection, stream):
        with self._lock:
            try:
                self._live_locked()
                self._upstreams[connection].add(stream)
            except BaseException:
                stream.close()
                raise

    @staticmethod
    def _framing(headers):
        lengths = headers.get_all('Content-Length', [])
        encodings = headers.get_all('Transfer-Encoding', [])
        if (len(lengths) > 1 or len(encodings) > 1 or (lengths and encodings)
                or (lengths and not re.fullmatch(r'[0-9]+', lengths[0]))
                or (encodings and encodings[0].lower() != 'chunked')):
            raise ValueError('ambiguous request framing')
        return lengths, encodings

    def _forward(self, handler, index, path):
        upgraded = handler.headers.get('Upgrade', '').lower() == 'websocket'
        lengths, encodings = handler.request_framing
        connection = self._connect(index)
        try:
            connection.putrequest(handler.command, path, skip_host=True, skip_accept_encoding=True)
            route = self._routes[index]
            host = route.hostname if ':' not in route.hostname else '[' + route.hostname + ']'
            connection.putheader('Host', host + (f':{route.port}' if route.port else ''))
            for key, value in handler.headers.items():
                if key.lower() not in {'host', 'proxy-authorization', 'proxy-connection', 'connection'}:
                    connection.putheader(key, value)
            connection.putheader('Connection', 'Upgrade' if upgraded else 'close')
            connection.endheaders()
            if encodings:
                while True:
                    line = handler.rfile.readline(LINE_LIMIT + 1)
                    if len(line) > LINE_LIMIT or not line.endswith(b'\r\n'):
                        raise ValueError('invalid request chunk')
                    raw_size = line.split(b';', 1)[0].strip()
                    if not re.fullmatch(b'[0-9a-fA-F]+', raw_size):
                        raise ValueError('invalid request chunk size')
                    size = int(raw_size, 16)
                    connection.send(line)
                    if not size:
                        while True:
                            trailer = handler.rfile.readline(LINE_LIMIT + 1)
                            if len(trailer) > LINE_LIMIT or not trailer.endswith(b'\r\n'):
                                raise ValueError('invalid request trailer')
                            connection.send(trailer)
                            if trailer == b'\r\n':
                                break
                        break
                    self._copy_exact(handler.rfile, connection.send, size)
                    ending = handler.rfile.read(2)
                    if ending != b'\r\n':
                        raise ValueError('invalid request chunk ending')
                    connection.send(ending)
            elif lengths:
                self._copy_exact(handler.rfile, connection.send, int(lengths[0]))
            with self._lock:
                self._records[index]['forwarded'] += 1
            upstream_socket = connection.sock
            response = connection.getresponse()
            # Never give native a Location it could follow outside this
            # observer. Redirects require explicit routing support first.
            if 300 <= response.status < 400 and response.status != 304:
                raise ValueError('upstream redirect would escape observed route')
            if response.status == 101 and not upgraded:
                raise ValueError('unsolicited protocol upgrade')
            handler.response_started = True
            handler.send_response_only(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in {'connection', 'transfer-encoding'}:
                    handler.send_header(key, value)
            handler.send_header('Connection', 'Upgrade' if response.status == 101 else 'close')
            handler.end_headers()
            handler.wfile.flush()
            if response.status == 101:
                self._tunnel(handler, response, upstream_socket)
            elif handler.command != 'HEAD':
                while data := response.read1(BLOCK):
                    handler.wfile.write(data)
                    handler.wfile.flush()
        finally:
            connection.close()
            with self._lock:
                sockets = self._upstreams.pop(connection, ())
            for stream in sockets:
                stream.close()

    @staticmethod
    def _copy_exact(reader, send, remaining):
        while remaining:
            data = reader.read(min(BLOCK, remaining))
            if not data:
                raise ValueError('truncated request body')
            send(data)
            remaining -= len(data)

    def _tunnel(self, handler, response, upstream):
        # Buffered readers preserve bytes received together with the upgrade
        # headers. Bidirectional pumps preserve every frame and half-close.
        def pump(reader, target):
            try:
                while data := reader.read1(BLOCK):
                    target.sendall(data)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    target.shutdown(socket.SHUT_WR)
        thread = threading.Thread(target=pump, args=(handler.rfile, upstream), daemon=True)
        with self._lock:
            thread.start()
            self._track_worker(thread)
        pump(response.fp, handler.connection)
        with contextlib.suppress(OSError):
            handler.connection.shutdown(socket.SHUT_RD)
        thread.join(timeout=self._timeout)
        if thread.is_alive():
            raise ValueError('upgraded route did not close')

    def _live_locked(self):
        if (self._closed or self._failed or not self._thread.is_alive()
                or self._server.socket.fileno() != self._server_identity):
            self._failed = True
            raise ValueError('original model route observer unavailable')

    def current(self):
        with self._lock:
            self._live_locked()
            return {'created_monotonic_ns': self._created_ns,
                    'routes_sha256': hashlib.sha256(json.dumps(self._urls).encode()).hexdigest(),
                    'local_urls_sha256': hashlib.sha256(json.dumps(self.urls).encode()).hexdigest()}

    @property
    def urls(self):
        return tuple(f'http://127.0.0.1:{self._server.server_port}/{self._token}/{i}'
                     for i in range(len(self._routes)))

    def zero(self, index):
        self.current()
        with self._lock:
            self._live_locked()
            if type(index) is not int or not 0 <= index < len(self._records):
                raise ValueError('invalid model route slot')
            record = self._records[index]
            if ((self._action is None and self._pending) or self._unattributed
                    or record['before_activation']):
                raise ValueError('standby route has pending or nonzero request evidence')
            return {'before_activation_model_requests': 0,
                    'observed_since_monotonic_ns': self._created_ns,
                    'requests': record['requests'], 'action_id': self._action}

    def release(self, action_id):
        from ccc_native_standby import identifier
        identifier(action_id)
        self.current()
        with self._lock:
            self._live_locked()
            if self._action is not None:
                if self._action != action_id:
                    raise ValueError('model routes already bound to another action')
                return
            if self._pending or self._unattributed or any(r['before_activation'] for r in self._records):
                raise ValueError('nonzero standby route cannot be released')
            self._action = action_id

    def report(self):
        with self._lock:
            self._workers = {worker for worker in self._workers if worker.is_alive()}
            resources = {
                'listener_closed': self._server.socket.fileno() == -1,
                'server_thread_alive': self._thread.is_alive(),
                'worker_threads_alive': len(self._workers),
                'downstream_connections': len(self._connections),
                'upstream_connections': len(self._upstreams),
                'upstream_sockets': sum(len(streams) for streams in self._upstreams.values()),
            }
            released = (self._closed and resources['listener_closed']
                        and not resources['server_thread_alive']
                        and not resources['worker_threads_alive']
                        and not resources['downstream_connections']
                        and not resources['upstream_connections']
                        and not resources['upstream_sockets'] and self._pending == 0)
            return {'action_id': self._action, 'closed': self._closed, 'failed': self._failed,
                    'resources': resources, 'resources_released': released,
                    'pending_connections': self._pending, 'accepted_connections': self._accepted,
                    'unattributed_requests': self._unattributed, 'slots': copy.deepcopy(self._records)}

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            connections = tuple(self._connections) + tuple(
                stream for streams in self._upstreams.values() for stream in streams)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

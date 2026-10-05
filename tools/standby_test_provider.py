"""Loopback-only HTTPS fixture; native lifecycle, never request counts, ends failure.

This is an acceptance fixture, not a production provider. Bind each original
session transcript before activation. It does not send terminal input.
"""
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import ssl
import subprocess
import threading
import time
import uuid


def recovered_turn(path, session_id):
    """Only a new task after an original terminal rate-limit failure qualifies."""
    failed = set()
    current = None
    active = None
    started = set()
    bound = False
    total = 0
    complete_rows = 0
    with Path(path).open('rb') as stream:
        for raw in stream:
            total += len(raw)
            if total > 32 * 1024 * 1024:
                raise ValueError('transcript exceeds fixture budget')
            if not raw.endswith(b'\n'):
                break  # Writer may still be appending the final row.
            complete_rows += 1
            row = json.loads(raw)
            if row.get('type') == 'session_meta':
                if row.get('payload', {}).get('id') != session_id:
                    raise ValueError('transcript session mismatch')
                bound = True
            if row.get('type') != 'event_msg':
                continue
            event = row.get('payload', {})
            turn = event.get('turn_id')
            if event.get('type') == 'task_complete':
                if not turn or turn != active:
                    raise ValueError('terminal does not match active original turn')
                error = event.get('error') or {}
                if (turn and isinstance(error, dict)
                        and (error.get('codex_error_info') == 'rate_limit_exceeded'
                             or 'rate limit exceeded' in str(error.get('message', '')).lower())):
                    failed.add(turn)
                current = None
                active = None
            elif event.get('type') == 'task_started':
                if not turn or active is not None or turn in started:
                    raise ValueError('overlapping or repeated original turn')
                active = turn
                started.add(turn)
                current = turn if failed and turn not in failed else None
            elif event.get('type') == 'turn_aborted':
                raise ValueError('original turn aborted')
    if not bound:
        if not complete_rows:
            return None  # Newly opened rollout; no identity or recovery proof yet.
        raise ValueError('transcript session binding absent')
    return current


class _Server(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True
    request_queue_size = 128

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        try:
            return self.tls.wrap_socket(connection, server_side=True), address
        except BaseException:
            connection.close()
            raise


class _Response(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def do_POST(self):
        fixture = self.server.fixture
        diagnostic = {}
        try:
            if self.path != '/v1/responses' or self.headers.get('Transfer-Encoding'):
                raise ValueError('unexpected endpoint/framing')
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 8 * 1024 * 1024:
                raise ValueError('request size outside fixture bound')
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('incomplete body')
            encoding = self.headers.get('Content-Encoding', 'identity')
            if encoding == 'gzip':
                with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
                    raw = compressed.read(8 * 1024 * 1024 + 1)
            elif encoding != 'identity':
                raise ValueError('unsupported encoding')
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError('expanded body exceeds bound')
            body = json.loads(raw)
            diagnostic['body_sha256'] = hashlib.sha256(raw).hexdigest()
            users = [r for r in body.get('input', []) if r.get('role') == 'user']
            text = '\n'.join(p.get('text', '') for p in users[-1].get('content', [])
                             if p.get('type') == 'input_text') if users else ''
            sid = self.headers.get('thread-id')
            # Keep attribution without storing request text or authorization.
            try:
                diagnostic['session_id'] = str(uuid.UUID(sid))
            except (ValueError, TypeError, AttributeError):
                diagnostic['session_id'] = None
            with fixture.lock:
                transcript = fixture.sessions.get(sid)
                diagnostic.update(session_bound=transcript is not None,
                                  prompt_matches=text == fixture.prompt)
                if transcript is None:
                    diagnostic['refusal_reason'] = 'unbound_session'
                    raise ValueError('unbound session')
                if text != fixture.prompt:
                    diagnostic['refusal_reason'] = 'unexpected_prompt'
                    raise ValueError('unexpected prompt')
                fixture.counts[sid] += 1
                if fixture.counts[sid] > 128:
                    raise ValueError('per-session request budget exhausted')
            if callable(transcript):
                transcript = transcript()
            # A native may send its first request before persisting rollout.
            # Absence cannot authorize success or terminal input.
            turn = recovered_turn(transcript, sid) if transcript is not None else None
            fixture.record(session_id=sid, recovered_turn=turn,
                           body_sha256=hashlib.sha256(raw).hexdigest())
            response = dict(id='resp_'+uuid.uuid4().hex, object='response',
                            status='in_progress', output=[])
            events = [dict(type='response.created', response=response)]
            if not turn:
                # Codex prepends "rate limit exceeded: " to this SSE error.
                # Use the actual provider banner so the acceptance fixture
                # reaches CCC's strict terminal classifier without broadening
                # production matching to arbitrary mentions of rate limits.
                events.append(dict(type='response.failed', response={**response,
                    'status': 'failed', 'error': {'code': 'rate_limit_exceeded',
                    'message': ('Your requests to gpt-6-astra for gpt-6-astra '
                                'in eastus2 have exceeded token rate limit.')}}))
            else:
                item = dict(id='msg_'+uuid.uuid4().hex, type='message', role='assistant',
                            status='completed', content=[dict(type='output_text', text='OK', annotations=[])])
                events.extend([
                    dict(type='response.output_item.added', output_index=0,
                         item={**item, 'status': 'in_progress', 'content': []}),
                    dict(type='response.output_text.delta', item_id=item['id'], output_index=0,
                         content_index=0, delta='OK'),
                    dict(type='response.output_item.done', output_index=0, item=item),
                    dict(type='response.completed', response={**response, 'status': 'completed', 'output': [item]})])
            data = ''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            fixture.record(error=type(exc).__name__+': '+str(exc), **diagnostic)
            self.send_error(400, 'fixture refused request')


class LocalProvider:
    def __init__(self, directory, prompt):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, exist_ok=False)
        self.prompt = prompt
        self.lock = threading.Lock()
        self.sessions, self.counts = {}, {}
        self.cert, key = self.directory/'certificate.pem', self.directory/'key.pem'
        command = ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                   '-keyout', str(key), '-out', str(self.cert), '-days', '1',
                   '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1']
        result = subprocess.run(command, capture_output=True, timeout=20)
        if result.returncode:
            raise RuntimeError('local certificate creation failed: '+result.stderr.decode(errors='replace'))
        key.chmod(0o600)
        self.cert.chmod(0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, key)
        self.server = _Server(('127.0.0.1', 0), _Response)
        self.server.fixture = self
        self.server.tls = context
        self.url = 'https://127.0.0.1:%d/v1' % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def bind(self, session_id, transcript):
        uuid.UUID(session_id)
        path = transcript() if callable(transcript) else Path(transcript).resolve(strict=True)
        # Bind before any activated task; old failed sessions cannot satisfy
        # this experiment's recovery condition.
        if path is not None:
            recovered_turn(path, session_id)
            with Path(path).open() as stream:
                if any(json.loads(line).get('payload', {}).get('type') == 'task_started'
                       for line in stream if line.endswith('\n')):
                    raise ValueError('fixture must bind before first task')
        with self.lock:
            if session_id in self.sessions or len(self.sessions) >= 50:
                raise ValueError('duplicate/excess fixture session')
            self.sessions[session_id] = transcript if callable(transcript) else path
            self.counts[session_id] = 0

    def record(self, **row):
        with self.lock:
            with (self.directory/'requests.jsonl').open('a') as stream:
                stream.write(json.dumps(dict(at=time.time(), monotonic_ns=time.monotonic_ns(), **row))+'\n')
                stream.flush()
                os.fsync(stream.fileno())

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

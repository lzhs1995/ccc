"""Exact R8 synthetic fixture and CLI judgment functions; no host dependencies."""
import hashlib
import http.server
import json
import threading
import time

QUIET_SECONDS = 20
IDLE_MS = 18000
EXPECTED = 'PARTIAL_LOCAL_PASSING_COMPLETE'
CODEX = None  # Set only to the digest-verified official client at execution.

def require(ok, why):
    if not ok:
        raise RuntimeError(why)

def frame(event):
    return ('event: ' + event['type'] + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode()

def prefix():
    item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'in_progress',
            'phase': 'final_answer', 'content': []}
    return b''.join(frame(e) for e in [
        {'type': 'response.created', 'response': {'id': 'resp_fixture', 'status': 'in_progress', 'output': []}},
        {'type': 'response.output_item.added', 'output_index': 0, 'item': item},
        {'type': 'response.content_part.added', 'item_id': 'msg_fixture', 'output_index': 0, 'content_index': 0,
         'part': {'type': 'output_text', 'text': '', 'annotations': []}},
        {'type': 'response.output_text.delta', 'item_id': 'msg_fixture', 'output_index': 0,
         'content_index': 0, 'delta': 'PARTIAL_'}])

def half_frame():
    whole = frame({'type': 'response.output_text.delta', 'item_id': 'msg_fixture', 'output_index': 0,
                   'content_index': 0, 'delta': 'LOCAL_PASSING_COMPLETE'})
    split = whole.index(b'LOCAL_PASSING_COMPLETE') + 5
    return whole[:split], whole[split:]

def finish(include_delta):
    item = {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
            'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': EXPECTED, 'annotations': []}]}
    events = []
    if include_delta:
        events.append({'type': 'response.output_text.delta', 'item_id': 'msg_fixture', 'output_index': 0,
                       'content_index': 0, 'delta': 'LOCAL_PASSING_COMPLETE'})
    events.extend([
        {'type': 'response.output_item.done', 'output_index': 0, 'item': item},
        {'type': 'response.completed', 'response': {'id': 'resp_fixture', 'status': 'completed',
         'output': [item], 'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}])
    return b''.join(frame(e) for e in events)

class Fake(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.active_handlers = 0
        self.handlers_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        with self.handlers_lock: self.active_handlers += 1
        try: super().process_request(request, address)
        except BaseException:
            with self.handlers_lock: self.active_handlers -= 1
            raise

    def process_request_thread(self, request, address):
        try: super().process_request_thread(request, address)
        finally:
            with self.handlers_lock: self.active_handlers -= 1

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == '/sandbox-positive':
            body = b'sandbox-positive-control'
        elif self.path.endswith('/models'):
            body = b'{"object":"list","data":[{"id":"gpt-6-astra","object":"model"}]}'
        else:
            self.send_error(404); return
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        size = int(self.headers.get('Content-Length', 0))
        if self.path != '/v1/responses' or not 0 < size <= 1024 * 1024:
            self.send_error(400); return
        raw = self.rfile.read(size)
        case = self.headers.get('Authorization', '').removeprefix('Bearer synthetic-')
        if case not in {'silent', 'comments', 'half-frame'}:
            self.send_error(403); return
        record = {'case': case, 'start': time.monotonic(), 'request_bytes': size,
                  'request_sha256': hashlib.sha256(raw).hexdigest(), 'upstream_ping_events': 0,
                  'comments_sent': 0, 'completed_sent': False}
        self.server.records.append(record)
        self.close_connection = True
        try:
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close'); self.end_headers()
            self.wfile.write(prefix()); self.wfile.flush()
            record['first_real_delta_sent'] = time.monotonic()
            if case == 'half-frame':
                self.wfile.write(half_frame()[0]); self.wfile.flush()
            until = time.monotonic() + QUIET_SECONDS
            while time.monotonic() < until and not self.server.stop.is_set():
                if case == 'comments':
                    self.wfile.write(b': fixture-comment\n\n'); self.wfile.flush(); record['comments_sent'] += 1
                self.server.stop.wait(min(1, max(0, until - time.monotonic())))
            if self.server.stop.is_set():
                record['stopped_by_owned_cleanup'] = True; return
            if case == 'half-frame': self.wfile.write(half_frame()[1])
            self.wfile.write(finish(case != 'half-frame')); self.wfile.flush()
            record['completed_sent'] = True
            record['completed_at'] = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            record['caller_closed'] = True
        finally:
            record['finished_at'] = time.monotonic()

def codex_command(private, gateway, case):
    args = [str(CODEX), 'exec', '--ephemeral', '--ignore-user-config', '--ignore-rules',
            '--skip-git-repo-check', '--sandbox', 'read-only', '--json', '--color', 'never', '-C', str(private)]
    cfg = {'model_provider': 'fixture', 'model': 'fixture-' + case + '/gpt-6-astra',
           'log_dir': str(private / 'codex-log'), 'sqlite_home': str(private / 'codex-sqlite'),
           'check_for_update_on_startup': False,
           'model_providers.fixture.name': 'Isolated artifact fixture',
           'model_providers.fixture.base_url': f'http://127.0.0.1:{gateway}/v1',
           'model_providers.fixture.wire_api': 'responses',
           'model_providers.fixture.requires_openai_auth': False,
           'model_providers.fixture.supports_websockets': False,
           'model_providers.fixture.http_headers': {'Authorization': 'Bearer magpie'},
           'model_providers.fixture.stream_idle_timeout_ms': IDLE_MS,
           'model_providers.fixture.stream_max_retries': 0,
           'model_providers.fixture.request_max_retries': 0}
    for key, value in cfg.items():
        # TOML inline table differs from JSON object syntax.
        value = '{ Authorization = "Bearer magpie" }' if isinstance(value, dict) else json.dumps(value)
        args.extend(['-c', key + '=' + value])
    return args + ['Local synthetic transport check only. Return the fixture answer. Do not call tools.']

def judge(returncode, stdout, stderr, records, expected_success):
    events = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    completed = [x for x in events if x.get('type') == 'turn.completed']
    forbidden = {'command_execution', 'mcp_tool_call', 'web_search', 'file_change', 'function_call'}
    require(not any(x.get('item', {}).get('type') in forbidden for x in events), 'unexpected tool activity')
    require(len(records) == 1, 'exactly one fake supplier request required')
    record = records[0]
    require('first_real_delta_sent' in record and record['upstream_ping_events'] == 0, 'invalid fixture sequence')
    if expected_success:
        require(returncode == 0 and len(completed) == 1, 'Codex did not complete the turn')
        messages = [x.get('item', {}).get('text') for x in events
                    if x.get('type') == 'item.completed' and x.get('item', {}).get('type') == 'agent_message']
        require(EXPECTED in messages and record['completed_sent'], 'exact complete assistant answer missing')
        require(record['completed_at'] - record['first_real_delta_sent'] >= QUIET_SECONDS - .1,
                'quiet interval shorter than required')
    else:
        require(returncode != 0 and not completed, 'baseline unexpectedly completed')
        require('idle timeout waiting for SSE' in stdout + stderr, 'baseline failed for a different reason')
    return {'passed': True, 'expected_success': expected_success, 'codex_turn_completed': len(completed),
            'returncode': returncode, 'supplier_requests': len(records), 'supplier_completed_sent': record['completed_sent']}

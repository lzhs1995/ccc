"""Bounded Responses transport for explicitly selected, disposable B checks.

No terminal scan, keystroke, signal, or model history is used for admission.
Only the fixed check below crosses the upstream connection. Native tools,
skills, title prompts and tool-result continuations are never forwarded.
"""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import functools
import hmac
import ipaddress
import json
import re
import ssl
import time
from urllib.parse import urlsplit
import uuid

from ccc_access_budget import AccessBudget, AdmissionClosed

HEADER_LIMIT = 16 * 1024
NATIVE_BODY_LIMIT = 1024 * 1024
RESPONSE_LIMIT = 256 * 1024
CHECK_INPUT = 'Reply exactly OK.'
CHECK_INSTRUCTIONS = 'Return OK. Do not call tools.'

# These fields determine framing or interpretation. Even identical duplicate
# values are rejected. Other upstream fields are never forwarded to native.
SINGLE_RESPONSE_HEADERS = frozenset({
    'content-length', 'transfer-encoding', 'content-type', 'content-encoding', 'connection',
})
SAFE_PROTOCOL_REASONS = frozenset({
    'HTTP header too large', 'ambiguous HTTP header', 'invalid HTTP header value',
    'ambiguous HTTP body framing', 'invalid HTTP chunk', 'HTTP trailers too large',
    'HTTP body too large', 'truncated HTTP chunk', 'invalid HTTP chunk ending',
    'invalid HTTP content length', 'truncated HTTP body',
    'native request must have bounded body framing',
    'proxy did not establish the configured connection',
    'HTTPS proxy checks require Python 3.11 or newer',
    'no complete upstream response', 'invalid upstream output',
    'tool or unsupported output blocked before native execution',
    'unsupported response content', 'empty assistant output', 'no complete assistant answer',
    'invalid upstream usage', 'upstream did not honor the requested output limit',
    'invalid upstream response status', 'upstream ignored identity encoding',
    'upstream did not return Responses SSE', 'invalid Responses event',
    'upstream turn did not complete exactly once',
    'upstream stream ended before a full response',
})


class ProtocolFault(ValueError):
    pass


async def read_head(reader, *, response=False):
    raw = await reader.readuntil(b'\r\n\r\n')
    if len(raw) > HEADER_LIMIT:
        raise ProtocolFault('HTTP header too large')
    lines = raw[:-4].decode('latin-1').split('\r\n')
    headers = {}
    for line in lines[1:]:
        key, separator, value = line.partition(':')
        key = key.lower()
        if not separator or not re.fullmatch(r'[a-z0-9!#$%&*+.^_`|~-]+', key):
            raise ProtocolFault('ambiguous HTTP header')
        value = value.strip(' \t')
        if any((ord(char) < 32 and char != '\t') or ord(char) == 127 for char in value):
            raise ProtocolFault('invalid HTTP header value')
        if key in headers:
            if not response or key in SINGLE_RESPONSE_HEADERS:
                raise ProtocolFault('ambiguous HTTP header')
            # Set-Cookie must not be comma-combined. We do not keep cookies or
            # pass any of them onward; validate every occurrence, retain only
            # the first in this private parser result. Repeated non-framing
            # metadata such as Vary/Via cannot affect body interpretation.
            if key != 'set-cookie':
                headers[key] += ', ' + value
            continue
        headers[key] = value
    return lines[0], headers


def error_detail(exc, stage):
    """Bounded diagnostics made only from local constants, never server text."""
    if isinstance(exc, ProtocolFault):
        reason = str(exc) if str(exc) in SAFE_PROTOCOL_REASONS else 'HTTP protocol validation failed'
        kind = 'ProtocolFault'
    elif isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        reason, kind = 'operation timed out', 'TimeoutError'
    elif isinstance(exc, asyncio.IncompleteReadError):
        reason, kind = 'connection ended before the expected bytes arrived', 'IncompleteReadError'
    elif isinstance(exc, asyncio.LimitOverrunError):
        reason, kind = 'HTTP framing exceeded the read limit', 'LimitOverrunError'
    elif isinstance(exc, OSError):
        reason, kind = 'connection or local storage operation failed', 'OSError'
    else:
        reason, kind = 'local request validation or processing failed', 'ValidationError'
    return {'stage': stage, 'type': kind, 'reason': reason}


async def body_chunks(reader, headers, limit, *, require_length=False):
    total = 0
    transfer, length = headers.get('transfer-encoding'), headers.get('content-length')
    if transfer is not None:
        if transfer.lower() != 'chunked' or length is not None:
            raise ProtocolFault('ambiguous HTTP body framing')
        while True:
            line = await reader.readuntil(b'\r\n')
            value = line[:-2].split(b';', 1)[0]
            if len(line) > 128 or not re.fullmatch(b'[0-9a-fA-F]+', value):
                raise ProtocolFault('invalid HTTP chunk')
            count = int(value, 16)
            if count == 0:
                trailers = 0
                while True:
                    trailer = await reader.readuntil(b'\r\n')
                    trailers += len(trailer)
                    if trailers > HEADER_LIMIT:
                        raise ProtocolFault('HTTP trailers too large')
                    if trailer == b'\r\n':
                        return
            total += count
            if total > limit:
                raise ProtocolFault('HTTP body too large')
            # A complete SSE event can precede the end of a large HTTP chunk.
            # Deliver available bytes immediately; waiting for padding would
            # delay the admission gate even though success is already here.
            remaining = count
            while remaining:
                data = await reader.read(min(remaining, 65536))
                if not data:
                    raise ProtocolFault('truncated HTTP chunk')
                remaining -= len(data)
                yield data
            if await reader.readexactly(2) != b'\r\n':
                raise ProtocolFault('invalid HTTP chunk ending')
    elif length is not None:
        if not re.fullmatch(r'[0-9]+', length) or int(length) > limit:
            raise ProtocolFault('invalid HTTP content length')
        remaining = int(length)
        while remaining:
            data = await reader.read(min(remaining, 65536))
            if not data:
                raise ProtocolFault('truncated HTTP body')
            remaining -= len(data)
            yield data
    else:
        if require_length:
            raise ProtocolFault('native request must have bounded body framing')
        while True:
            data = await reader.read(65536)
            if not data:
                return
            total += len(data)
            if total > limit:
                raise ProtocolFault('HTTP body too large')
            yield data


@functools.lru_cache(maxsize=1)
def default_tls_context():
    # Trust-store loading must not block the event loop for every connection.
    return ssl.create_default_context()


@dataclass(frozen=True)
class Upstream:
    base_url: str
    model: str
    proxy_url: str = ''
    allow_loopback: bool = False
    timeout: float = 120
    tls_context: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        parsed = urlsplit(self.base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
            raise ValueError('invalid upstream URL')
        local = False
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme != 'https' and not (self.allow_loopback and local and parsed.scheme == 'http'):
            raise ValueError('upstream must use HTTPS; HTTP is only for an explicit loopback fixture')
        if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,128}', self.model):
            raise ValueError('invalid check model')
        if not 1 <= self.timeout <= 600:
            raise ValueError('invalid upstream timeout')
        if parsed.scheme == 'https' and self.tls_context is None:
            object.__setattr__(self, 'tls_context', default_tls_context())
        if self.proxy_url:
            proxy = urlsplit(self.proxy_url)
            if (proxy.scheme != 'http' or not proxy.hostname or proxy.path not in ('', '/')
                    or proxy.query or proxy.fragment):
                raise ValueError('only an explicit HTTP CONNECT proxy is supported')

    @property
    def parsed(self):
        return urlsplit(self.base_url)

    @property
    def authority(self):
        parsed = self.parsed
        host = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
        return host + (':' + str(parsed.port) if parsed.port else '')

    def request(self, policy, headers):
        # Do not merge the native JSON. In particular input.additional_tools,
        # MCP schemas, history and native title instructions have no path here.
        body = json.dumps({
            'model': self.model, 'input': CHECK_INPUT, 'instructions': CHECK_INSTRUCTIONS,
            'max_output_tokens': policy.max_output_tokens,
            'reasoning': {'effort': 'low'}, 'stream': True, 'store': False,
            'tools': [], 'tool_choice': 'none', 'parallel_tool_calls': False,
        }, separators=(',', ':')).encode()
        assert len(body) < 1024
        fields = {
            'Host': self.authority, 'Content-Type': 'application/json',
            'Content-Length': str(len(body)), 'Accept': 'text/event-stream',
            'Accept-Encoding': 'identity', 'Connection': 'close',
        }
        # Credentials stay in memory and go only to the configured upstream.
        # They are never included in status, journal or diagnostic records.
        for key in ('authorization', 'api-key', 'x-api-key'):
            if key in headers:
                fields[key] = headers[key]
        path = self.parsed.path.rstrip('/') + '/responses'
        head = 'POST ' + path + ' HTTP/1.1\r\n'
        head += ''.join(key + ': ' + value + '\r\n' for key, value in fields.items())
        return head.encode('latin-1') + b'\r\n' + body

    async def connect(self):
        parsed = self.parsed
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        context = self.tls_context if parsed.scheme == 'https' else None
        if not self.proxy_url:
            return await asyncio.open_connection(parsed.hostname, port, ssl=context,
                server_hostname=parsed.hostname if context else None, limit=RESPONSE_LIMIT)
        proxy = urlsplit(self.proxy_url)
        reader, writer = await asyncio.open_connection(proxy.hostname, proxy.port or 80, limit=RESPONSE_LIMIT)
        try:
            destination = ('[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname) + ':' + str(port)
            fields = {'Host': destination}
            if proxy.username is not None:
                from urllib.parse import unquote
                credentials = unquote(proxy.username) + ':' + unquote(proxy.password or '')
                fields['Proxy-Authorization'] = 'Basic ' + base64.b64encode(credentials.encode()).decode()
            writer.write(('CONNECT ' + destination + ' HTTP/1.1\r\n' +
                ''.join(key + ': ' + value + '\r\n' for key, value in fields.items()) + '\r\n').encode())
            await writer.drain()
            line, _ = await read_head(reader, response=True)
            if not re.fullmatch(r'HTTP/1\.\d 200(?: .*)?', line):
                raise ProtocolFault('proxy did not establish the configured connection')
            if context:
                if not hasattr(writer, 'start_tls'):
                    raise ProtocolFault('HTTPS proxy checks require Python 3.11 or newer')
                await writer.start_tls(context, server_hostname=parsed.hostname)
            return reader, writer
        except BaseException:
            writer.close()
            raise


def completed_answer(response, output_limit):
    if (not isinstance(response, dict) or response.get('status') != 'completed'
            or not isinstance(response.get('id'), str) or not response['id']
            or not isinstance(response.get('output'), list) or response.get('error') is not None):
        raise ProtocolFault('no complete upstream response')
    messages = []
    for item in response['output']:
        if not isinstance(item, dict):
            raise ProtocolFault('invalid upstream output')
        if item.get('type') == 'reasoning':
            continue
        if (item.get('type') != 'message' or item.get('role') != 'assistant'
                or item.get('status') not in (None, 'completed')
                or not isinstance(item.get('id'), str) or not item['id']
                or not isinstance(item.get('content'), list)):
            raise ProtocolFault('tool or unsupported output blocked before native execution')
        parts = item['content']
        if not parts or any(not isinstance(part, dict) or part.get('type') != 'output_text'
                            or not isinstance(part.get('text'), str) for part in parts):
            raise ProtocolFault('unsupported response content')
        if not any(part['text'].strip() for part in parts):
            raise ProtocolFault('empty assistant output')
        messages.append({'id': item['id'], 'type': 'message', 'role': 'assistant', 'status': 'completed',
                         'content': [{'type': 'output_text', 'text': part['text'], 'annotations': []}
                                     for part in parts]})
    if not messages:
        raise ProtocolFault('no complete assistant answer')
    result = {'id': response['id'], 'object': 'response', 'status': 'completed', 'output': messages}
    usage = response.get('usage')
    if usage is not None:
        if (not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                for key in ('input_tokens', 'output_tokens', 'total_tokens'))):
            raise ProtocolFault('invalid upstream usage')
        if usage['output_tokens'] > output_limit:
            raise ProtocolFault('upstream did not honor the requested output limit')
        result['usage'] = {key: usage[key] for key in ('input_tokens', 'output_tokens', 'total_tokens')}
    return result


def response_events(response):
    started = {**response, 'status': 'in_progress', 'output': []}
    started.pop('usage', None)
    events = [{'type': 'response.created', 'response': started}]
    for index, item in enumerate(response['output']):
        events.append({'type': 'response.output_item.done', 'output_index': index, 'item': item})
    events.append({'type': 'response.completed', 'response': response})
    return b''.join(('event: ' + event['type'] + '\ndata: ' +
        json.dumps(event, separators=(',', ':')) + '\n\n').encode() for event in events)


async def read_response(reader, output_limit, *, on_complete=None):
    line, headers = await read_head(reader, response=True)
    if not re.fullmatch(r'HTTP/1\.\d [1-5]\d\d(?: .*)?', line):
        raise ProtocolFault('invalid upstream response status')
    status = int(line.split(' ', 2)[1])
    if headers.get('content-encoding', 'identity') != 'identity':
        raise ProtocolFault('upstream ignored identity encoding')
    if status != 200:
        # Do not propagate server-generated instructions or endpoint names.
        # The caller records a real rejected HTTP attempt independently of usage.
        async for _ in body_chunks(reader, headers, RESPONSE_LIMIT):
            pass
        return status, None
    if headers.get('content-type', '').split(';', 1)[0].lower() != 'text/event-stream':
        raise ProtocolFault('upstream did not return Responses SSE')
    pending = b''
    completed = None
    generated_content = False
    async for chunk in body_chunks(reader, headers, RESPONSE_LIMIT):
        pending += chunk
        pending = pending.replace(b'\r\n', b'\n')
        while b'\n\n' in pending:
            event, pending = pending.split(b'\n\n', 1)
            values = [line[5:].lstrip(b' ') for line in event.split(b'\n') if line.startswith(b'data:')]
            if not values or values == [b'[DONE]']:
                continue
            value = json.loads(b'\n'.join(values))
            if not isinstance(value, dict):
                raise ProtocolFault('invalid Responses event')
            kind = value.get('type', '')
            if kind.startswith(('response.output_', 'response.content_part.')):
                generated_content = True
            if kind == 'response.failed' and not generated_content:
                failure = value.get('response')
                error = failure.get('error') if isinstance(failure, dict) else None
                if (isinstance(failure, dict) and failure.get('status') == 'failed'
                        and not failure.get('output') and isinstance(error, dict)
                        and error.get('code') in {'server_error', 'rate_limit_exceeded', 'overloaded'}
                        and re.search(r'high demand|overload|rate.limit|too many requests',
                                      str(error.get('message', '')), re.I)):
                    # A complete explicit congestion rejection is retryable
                    # within the same finite HTTP budget. Generated deltas or
                    # a truncated stream never get reclassified this way.
                    return (429 if error.get('code') == 'rate_limit_exceeded' else 503), None
            if kind in ('response.completed', 'response.incomplete', 'response.failed', 'error'):
                if kind != 'response.completed' or completed is not None:
                    raise ProtocolFault('upstream turn did not complete exactly once')
                completed = completed_answer(value.get('response'), output_limit)
                # Close here, within the parser task. Returning through
                # wait_for would yield and let a queued dispatch pass first.
                if on_complete is not None:
                    on_complete(completed)
                return status, completed
    raise ProtocolFault('upstream stream ended before a full response')


class BatchChannel:
    """A single bounded job. No global lock, discovery, or per-request process."""

    def __init__(self, budget: AccessBudget, upstream: Upstream, tokens, *, sessions=None,
                 binding_loader=None, cohort_timeout=120, dispatch_check=None):
        if len(tokens) != 50 or len(set(tokens)) != 50 or any(not isinstance(t, str) or len(t) < 32 for t in tokens):
            raise ValueError('fifty distinct private slot capabilities are required')
        self.budget, self.upstream, self.tokens = budget, upstream, tuple(tokens)
        self.sessions, self.binding_loader = dict(sessions or {}), binding_loader
        self.cohort_timeout = cohort_timeout
        self.dispatch_check = dispatch_check
        self.waiting = {}
        # Every new transport owner must form a real fifty-connection cohort.
        # A previous process's reservations alone do not prove concurrency.
        self.first_wave_sent = False
        self.dispatched_numbers = set()
        self.wave_fault = ''
        self.last_error = None
        self.slot_results = {}
        self.metrics = {'received': 0, 'forwarded': 0, 'denied': 0, 'complete': 0, 'rejected': 0,
                        'uncertain': 0, 'active': 0, 'peak_active': 0, 'dispatch_times': [],
                        'completion_gate_times': []}

    def authorize(self, slot, token):
        return 0 <= slot < 50 and hmac.compare_digest(self.tokens[slot], token)

    def check_admission(self, slot, session_id):
        if self.wave_fault:
            raise AdmissionClosed(self.wave_fault)
        if self.dispatch_check is not None and not self.dispatch_check():
            raise AdmissionClosed('this batch is no longer authorized to start checks')
        self.budget.check_admission(slot, session_id)

    def snapshot(self):
        value = self.budget.snapshot()
        return {**value, 'fault': value['fault'] or self.wave_fault,
                'forwarded': self.metrics['forwarded'], 'complete': self.metrics['complete'],
                'last_error': dict(self.last_error) if self.last_error else None,
                'slot_results': {str(slot): dict(result) for slot, result in self.slot_results.items()}}

    def record_result(self, reservation, outcome, detail=None):
        prior = self.slot_results.get(reservation.slot, {})
        if (detail is None and prior.get('attempt') == reservation.number
                and prior.get('outcome') == outcome):
            return
        # The parser's success gate is irreversible, including when a later
        # native delivery or storage operation fails.
        if prior.get('outcome') != 'complete':
            self.slot_results[reservation.slot] = {
                'outcome': outcome, 'attempt': reservation.number, 'updated_at': time.time(),
                **({'error': dict(detail)} if detail else {}),
            }
        if detail:
            self.last_error = {**detail, 'slot': reservation.slot, 'at': time.time()}

    async def check_session(self, slot, session_id, storage):
        if slot not in self.sessions and self.binding_loader is not None:
            self.sessions[slot] = await storage(self.binding_loader, slot)
        if self.sessions.get(slot) != session_id:
            raise AdmissionClosed('this is not the registered main native session')

    def dispatch_now(self, ticket):
        reservation, writer, wire, client_reader, client_writer, ready = ticket
        if self.wave_fault or client_writer.is_closing() or client_reader.at_eof():
            raise AdmissionClosed(self.wave_fault or 'native client disconnected before dispatch')
        if self.dispatch_check is not None and not self.dispatch_check():
            raise AdmissionClosed('this batch is no longer authorized to start checks')
        self.budget.begin_dispatch(reservation)
        # Both calls run on this event loop, with no await between the gate and
        # request bytes. Every asynchronous connect/queue wait is already over.
        self.dispatched_numbers.add(reservation.number)
        self.record_result(reservation, 'in_flight')
        self.metrics['forwarded'] += 1
        self.metrics['active'] += 1
        self.metrics['peak_active'] = max(self.metrics['peak_active'], self.metrics['active'])
        self.metrics['dispatch_times'].append(time.monotonic())
        if self.budget.policy.attempt_mode == 'sustained' and len(self.metrics['dispatch_times']) > 512:
            del self.metrics['dispatch_times'][:-512]
        writer.write(wire)
        ready.set_result(None)

    def fail_wave(self, reason):
        if self.first_wave_sent:
            return
        self.wave_fault = reason
        waiting, self.waiting = self.waiting, {}
        for ticket in waiting.values():
            if not ticket[-1].done():
                ticket[-1].set_exception(AdmissionClosed(reason))

    async def dispatch(self, reservation, writer, wire, client_reader, client_writer):
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        ticket = (reservation, writer, wire, client_reader, client_writer, ready)
        if self.first_wave_sent:
            self.dispatch_now(ticket)
            return
        if self.wave_fault:
            raise AdmissionClosed(self.wave_fault)
        self.waiting[reservation.slot] = ticket
        if len(self.waiting) == 50:
            tickets, self.waiting = self.waiting, {}
            if ((self.dispatch_check is not None and not self.dispatch_check())
                    or any(t[3].at_eof() or t[4].is_closing() or t[-1].done() for t in tickets.values())):
                self.waiting = tickets
                self.fail_wave('the fifty-slot cohort is no longer fully connected and authorized')
                return await ready
            # Fifty completed TCP/TLS connects form the first cohort. Write all
            # fifty before yielding to receive any upstream response.
            self.first_wave_sent = True
            for pending in tickets.values():
                try:
                    self.dispatch_now(ticket=pending)
                except BaseException as exc:
                    if not pending[-1].done():
                        pending[-1].set_exception(exc)
        try:
            await asyncio.wait_for(ready, self.cohort_timeout)
        except asyncio.TimeoutError:
            self.fail_wave('fifty native checks did not become ready before the deadline')
            if ready.done() and not ready.cancelled():
                ready.exception()
            raise AdmissionClosed(self.wave_fault)
        except asyncio.CancelledError:
            self.fail_wave('a native check was cancelled before the fifty-slot cohort was ready')
            if ready.done() and not ready.cancelled():
                ready.exception()
            raise


class Gateway:
    """One asyncio loop can serve independent 50-slot workspaces."""

    def __init__(self, channels=None, *, channel_loader=None, workers=4, health_token=''):
        self.channels = dict(channels or {})
        self.channel_loader = channel_loader
        self.loading = {}
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='ccc-access-storage')
        self.server = None
        self.tasks = set()
        self.health_token = health_token

    async def storage(self, function, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(self.pool, functools.partial(function, *args, **kwargs))

    async def channel(self, job):
        if job in self.channels:
            return self.channels[job]
        if self.channel_loader is None:
            raise AdmissionClosed('unknown access job')
        if job not in self.loading:
            async def load():
                channel = await self.storage(self.channel_loader, job)
                self.channels[job] = channel
                return channel
            self.loading[job] = asyncio.create_task(load())
        try:
            channel = await asyncio.shield(self.loading[job])
            self.channels[job] = channel
            return channel
        finally:
            if self.loading.get(job) is not None and self.loading[job].done():
                self.loading.pop(job, None)

    async def start(self, port=0):
        self.server = await asyncio.start_server(self.accept, '127.0.0.1', port,
                                                 limit=NATIVE_BODY_LIMIT, backlog=2048)
        return self.server.sockets[0].getsockname()[1]

    async def reply(self, writer, status, body, content_type='application/json'):
        writer.write(('HTTP/1.1 ' + str(status) + ' CCC Access\r\nContent-Type: ' + content_type +
                      '\r\nContent-Length: ' + str(len(body)) + '\r\nConnection: close\r\n\r\n').encode() + body)
        await writer.drain()

    async def reject(self, writer, status, message):
        await self.reply(writer, status, json.dumps({'error': {'message': message,
            'type': 'ccc_access_check', 'code': 'ccc_access_closed' if status == 409 else 'ccc_access_error'}}).encode())

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        channel = reservation = upstream_writer = None
        client_monitor = None
        dispatched = finished = False
        outcome = 'cancelled_before_dispatch'
        completed_response = None
        stage = 'native_request'
        try:
            line, headers = await asyncio.wait_for(read_head(reader), 15)
            if self.health_token and hmac.compare_digest(line, 'GET /health/' + self.health_token + ' HTTP/1.1'):
                await self.reply(writer, 200, b'{"ready":true}')
                return
            match = re.fullmatch(r'POST /([0-9a-f-]{36})/([0-9]{1,2})/([A-Za-z0-9_-]{32,128})/v1/responses HTTP/1\.\d', line)
            if not match:
                await self.reject(writer, 404, 'Only a registered batch Responses check is supported.')
                return
            job, slot, token = match.group(1), int(match.group(2)), match.group(3)
            uuid.UUID(job)
            channel = await self.channel(job)
            if not channel.authorize(slot, token):
                await self.reject(writer, 403, 'This native slot is not authorized.')
                return
            session_id = headers.get('thread-id', '')
            uuid.UUID(session_id)
            channel.metrics['received'] += 1
            stage = 'native_binding'
            await channel.check_session(slot, session_id, self.storage)
            channel.check_admission(slot, session_id)
            # Drop native context without deserializing its potentially huge
            # tools/history tree or allocating a copy per concurrent request.
            async def discard():
                async for _ in body_chunks(reader, headers, NATIVE_BODY_LIMIT, require_length=True):
                    pass
            stage = 'native_body'
            await asyncio.wait_for(discard(), 20)
            async def monitor_client():
                # Reqwest cancellation closes this local HTTP exchange. It
                # must close its upstream socket without waiting for a model,
                # storage, a terminal scan or a CCC interrupt operation.
                try:
                    await reader.read(1)
                except (OSError, RuntimeError):
                    pass
                task.cancel()
            client_monitor = asyncio.create_task(monitor_client())
            wire = channel.upstream.request(channel.budget.policy, headers)
            stage = 'budget_reservation'
            reservation = await self.storage(channel.budget.reserve, slot, session_id)
            stage = 'upstream_connect'
            upstream_reader, upstream_writer = await asyncio.wait_for(channel.upstream.connect(), 20)
            stage = 'cohort_dispatch'
            await channel.dispatch(reservation, upstream_writer, wire, reader, writer)
            dispatched = True
            outcome = 'uncertain'
            await upstream_writer.drain()
            def complete(response):
                nonlocal outcome, completed_response
                observed = time.monotonic()
                channel.budget.note_success(reservation, response['id'])
                channel.record_result(reservation, 'complete')
                completed_response = response
                outcome = 'complete'
                channel.metrics['completion_gate_times'].append({'response_observed': observed,
                    'gate_closed': time.monotonic(), 'reservation': reservation.number})
            stage = 'upstream_response'
            status, response = await asyncio.wait_for(read_response(upstream_reader,
                channel.budget.policy.max_output_tokens, on_complete=complete), channel.upstream.timeout)
            if response is None:
                outcome = 'rejected'
                channel.record_result(reservation, outcome, {'stage': 'upstream_response',
                    'type': 'UpstreamRejected', 'reason': 'API check rejected with HTTP ' + str(status),
                    'http_status': status})
                stage = 'budget_completion'
                await self.storage(channel.budget.finish, reservation, outcome)
                finished = True
                channel.metrics['rejected'] += 1
                # Preserve congestion as a retryable native error; no upstream
                # body, tool content, credentials or guessed success is relayed.
                message = ('We are currently experiencing high demand. ' if status in (429, 500, 502, 503, 504) else '')
                stage = 'native_response'
                await self.reject(writer, status, message + 'API check rejected with HTTP ' + str(status) + '.')
            else:
                stage = 'budget_completion'
                await self.storage(channel.budget.finish, reservation, outcome,
                                   response_id=response['id'], usage=response.get('usage'))
                finished = True
                channel.metrics['complete'] += 1
                stage = 'native_response'
                await self.reply(writer, 200, response_events(response), 'text/event-stream')
        except asyncio.CancelledError:
            if channel:
                channel.fail_wave('native request cancelled before the initial cohort completed')
                channel.metrics['uncertain'] += int(dispatched or (reservation is not None
                    and reservation.number in channel.dispatched_numbers))
            raise
        except AdmissionClosed as exc:
            if channel:
                channel.metrics['denied'] += 1
            try:
                await self.reject(writer, 409, str(exc))
            except (OSError, RuntimeError):
                pass
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError,
                asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            detail = error_detail(exc, stage)
            if channel:
                dispatched = dispatched or (reservation is not None
                    and reservation.number in channel.dispatched_numbers)
                channel.metrics['uncertain'] += int(dispatched)
                if reservation is not None:
                    channel.record_result(reservation, outcome if dispatched else 'cancelled_before_dispatch', detail)
                else:
                    channel.last_error = {**detail, 'at': time.time()}
                if not channel.first_wave_sent:
                    channel.fail_wave('first-wave setup failed; no partial substitute for fifty checks')
            try:
                await self.reject(writer, 502, 'The bounded API check did not complete (' +
                    detail['type'] + '; ' + detail['stage'] + '): ' + detail['reason'] + '.')
            except (OSError, RuntimeError):
                pass
        finally:
            # Transport cancellation comes before potentially slow fsync.
            # The durable reservation already prevents a crash from refunding
            # this attempt while its conservative outcome is being stored.
            if client_monitor:
                client_monitor.cancel()
            if upstream_writer:
                upstream_writer.close()
            writer.close()
            if reservation is not None and reservation.number in channel.dispatched_numbers:
                dispatched = True
                if outcome == 'cancelled_before_dispatch':
                    outcome = 'uncertain'
            if reservation is not None and not finished:
                channel.record_result(reservation, outcome)
                try:
                    # note_success is irreversible; a client/storage error
                    # after completion cannot downgrade it into a retry.
                    if outcome == 'complete':
                        await self.storage(channel.budget.finish, reservation, 'complete',
                                           response_id=completed_response['id'], usage=completed_response.get('usage'))
                    else:
                        await self.storage(channel.budget.finish, reservation, outcome)
                except (OSError, ValueError, RuntimeError):
                    pass  # Pending durable record bars unsafe recovery.
            if dispatched:
                channel.metrics['active'] -= 1
                channel.dispatched_numbers.discard(reservation.number)
            self.tasks.discard(task)

    async def close(self):
        if self.server:
            self.server.close()
        for task in tuple(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
        if self.server:
            await self.server.wait_closed()
        if self.loading:
            await asyncio.gather(*tuple(self.loading.values()), return_exceptions=True)
        self.pool.shutdown(wait=True)
        for channel in self.channels.values():
            channel.budget.close()

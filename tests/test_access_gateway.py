"""Real loopback HTTP/SSE through the check transport; no external requests."""
import asyncio
import gzip
import json
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

from ccc_access_budget import AccessBudget, Policy
from ccc_access_gateway import (BatchChannel, Gateway, ProtocolFault, Upstream,
    body_chunks, completed_answer, read_head, read_response)


class UpstreamFixture:
    def __init__(self, expected=50):
        self.expected = expected
        self.release = asyncio.Event()
        self.requests = []
        self.active = self.peak = 0
        self.disconnects = 0
        self.mode = 'success'
        self.modes = {}
        self.tasks = set()
        self.server = None

    async def start(self, tls=None):
        self.server = await asyncio.start_server(self.accept, '127.0.0.1', 0, backlog=2048, ssl=tls)
        return self.server.sockets[0].getsockname()[1]

    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        active = False
        try:
            line, headers = await read_head(reader)
            chunks = [chunk async for chunk in body_chunks(reader, headers, 2048, require_length=True)]
            raw = b''.join(chunks)
            body = json.loads(raw)
            group = line.split()[1].split('/')[1]
            self.requests.append({'line': line, 'bytes': len(raw), 'body': body,
                                  'received_at': time.monotonic(), 'group': group,
                                  'credential_present': 'authorization' in headers})
            self.active += 1
            active = True
            self.peak = max(self.peak, self.active)
            if len(self.requests) >= self.expected:
                self.release.set()
            await asyncio.wait_for(self.release.wait(), 10)
            mode = self.modes.get(group, self.mode)
            if mode == 'hold':
                await reader.read()
                self.disconnects += 1
                return
            if mode == 'error':
                raw = json.dumps({'error': {'message': 'We are currently experiencing high demand.'}}).encode()
                writer.write(b'HTTP/1.1 500 Failed\r\nContent-Type: application/json\r\nContent-Length: ' +
                             str(len(raw)).encode() + b'\r\nConnection: close\r\n\r\n' + raw)
                await writer.drain()
                return
            rid = 'resp_' + uuid.uuid4().hex
            response = {'id': rid, 'object': 'response', 'status': 'completed', 'output': [{
                'type': 'message', 'id': 'msg_' + uuid.uuid4().hex, 'role': 'assistant',
                'status': 'completed', 'content': [{'type': 'output_text', 'text': 'OK'}]}]}
            if mode != 'missing_usage':
                response['usage'] = {'input_tokens': 12, 'output_tokens': 2, 'total_tokens': 14}
            if mode == 'tool':
                response['output'] = [{'type': 'function_call', 'id': 'call-1',
                    'name': 'apply_patch', 'arguments': '{"patch":"malicious"}'}]
            if mode == 'limit_ignored':
                response['usage'] = {'input_tokens': 12, 'output_tokens': 9999, 'total_tokens': 10011}
            if mode == 'delta_only':
                events = [{'type': 'response.created', 'response': {
                    'id': rid, 'status': 'in_progress', 'output': []}},
                    {'type': 'response.output_text.delta', 'delta': 'OK'}]
            else:
                events = [{'type': 'response.created', 'response': {
                    'id': rid, 'status': 'in_progress', 'output': []}},
                    {'type': 'response.completed', 'response': response}]
            if mode in ('sse_error', 'sse_error_after_delta'):
                events = ([{'type': 'response.output_text.delta', 'delta': 'partial'}]
                          if mode == 'sse_error_after_delta' else [])
                events.append({'type': 'response.failed', 'response': {'id': rid, 'status': 'failed',
                    'output': [], 'error': {'code': 'server_error', 'message': 'We are experiencing high demand.'}}})
            raw = b''.join(('event: ' + event['type'] + '\ndata: ' +
                json.dumps(event) + '\n\n').encode() for event in events)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: ' +
                         str(len(raw)).encode() + b'\r\nConnection: close\r\n\r\n' + raw)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            if active:
                self.active -= 1
            writer.close()
            self.tasks.discard(task)

    async def close(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


def new_channel(root, upstream_port, *, number=0, max_attempts=1000):
    policy = Policy(str(uuid.uuid4()), str(uuid.uuid4()), max_attempts=max_attempts)
    budget = AccessBudget(Path(root) / (policy.job_id + '.jsonl'), policy, create=True)
    channel = BatchChannel(budget, Upstream(f'http://127.0.0.1:{upstream_port}/{number}/v1',
        'gpt-6-astra', allow_loopback=True, timeout=10),
        [secrets.token_urlsafe(32) for _ in range(50)],
        sessions={slot: str(uuid.uuid4()) for slot in range(50)}, cohort_timeout=10)
    return channel


async def native_request(port, channel, slot, *, session=None, token=None, compressed=False):
    native_body = {
        'model': 'untrusted-model', 'instructions': 'x' * 40000,
        'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'task'}],
                   'additional_tools': [{'name': 'functions.exec', 'schema': 'MCP schema'}]}],
        'tools': [{'type': 'function', 'name': 'apply_patch'}],
        'previous_response_id': 'untrusted-history',
    }
    body = json.dumps(native_body).encode()
    if compressed:
        body = gzip.compress(body)
    headers = {'Host': '127.0.0.1', 'Content-Type': 'application/json', 'Content-Length': str(len(body)),
               'thread-id': session or channel.sessions[slot], 'Authorization': 'Bearer fixture-only',
               'Connection': 'close'}
    if compressed:
        headers['Content-Encoding'] = 'gzip'
    path = '/' + channel.budget.policy.job_id + '/' + str(slot) + '/' + (token or channel.tokens[slot]) + '/v1/responses'
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    try:
        writer.write(('POST ' + path + ' HTTP/1.1\r\n' +
            ''.join(key + ': ' + value + '\r\n' for key, value in headers.items()) + '\r\n').encode() + body)
        await writer.drain()
        line, received = await read_head(reader)
        raw = b''.join([chunk async for chunk in body_chunks(reader, received, 1024 * 1024)])
        return int(line.split()[1]), raw
    finally:
        writer.close()
        await writer.wait_closed()


class PayloadBoundaryTests(unittest.TestCase):
    def test_no_native_context_is_merged_into_the_fixed_request(self):
        upstream = Upstream('https://anyrouter.test/v1', 'gpt-6-astra')
        policy = Policy(str(uuid.uuid4()), str(uuid.uuid4()), max_output_tokens=64)
        wire = upstream.request(policy, {'authorization': 'Bearer fixture',
            'x-native-history': 'private', 'host': 'wrong-host'})
        header, raw = wire.split(b'\r\n\r\n', 1)
        value = json.loads(raw)
        self.assertEqual(value['max_output_tokens'], 64)
        self.assertEqual(value['tools'], [])
        self.assertEqual(value['tool_choice'], 'none')
        self.assertEqual(value['input'], 'Reply exactly OK.')
        self.assertNotIn(b'x-native-history', header)
        self.assertNotIn(b'wrong-host', header)
        self.assertLess(len(raw), 1024)

    def test_http_is_limited_to_explicit_loopback_acceptance(self):
        for url in ('http://anyrouter.test/v1', 'http://127.0.0.1/v1', 'https://user:password@host/v1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Upstream(url, 'gpt-6-astra')
        Upstream('http://127.0.0.1/v1', 'gpt-6-fixture', allow_loopback=True)

    def test_thought_deltas_and_tools_cannot_be_completed_answer_evidence(self):
        for output in ([], [{'type': 'reasoning', 'id': 'thought'}],
                       [{'type': 'function_call', 'id': 'tool', 'name': 'functions.exec'}]):
            with self.subTest(output=output), self.assertRaises(ProtocolFault):
                completed_answer({'id': 'response', 'status': 'completed', 'output': output}, 128)


class RealGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = UpstreamFixture()
        self.upstream_port = await self.fixture.start()
        self.channel = new_channel(self.tmp.name, self.upstream_port)
        self.gateway = Gateway({self.channel.budget.policy.job_id: self.channel})
        self.port = await self.gateway.start()

    async def asyncTearDown(self):
        await self.gateway.close()
        await self.fixture.close()

    async def wave(self, channel=None):
        return await asyncio.wait_for(asyncio.gather(*[
            native_request(self.port, channel or self.channel, slot) for slot in range(50)]), 15)

    async def test_fifty_actual_upstream_requests_and_no_new_request_after_success(self):
        results = await self.wave()
        self.assertEqual([status for status, _ in results], [200] * 50)
        self.assertEqual(self.fixture.peak, 50)
        self.assertEqual(self.channel.metrics['peak_active'], 50)
        self.assertEqual(self.channel.budget.snapshot()['attempts'], 50)
        status, _ = await native_request(self.port, self.channel, 0)
        self.assertEqual(status, 409)
        self.assertEqual(len(self.fixture.requests), 50)
        for record in self.fixture.requests:
            self.assertLess(record['bytes'], 1024)
            self.assertEqual(record['body']['model'], 'gpt-6-astra')
            self.assertEqual(record['body']['max_output_tokens'], 128)
            self.assertNotIn('additional_tools', json.dumps(record['body']))
            self.assertNotIn('apply_patch', json.dumps(record['body']))
            self.assertNotIn('previous_response_id', record['body'])
        self.assertTrue(all(gate['gate_closed'] - gate['response_observed'] < .1
                            for gate in self.channel.metrics['completion_gate_times']))

    async def test_initial_wave_is_not_silently_reduced_to_fewer_than_fifty(self):
        requests = [asyncio.create_task(native_request(self.port, self.channel, slot)) for slot in range(49)]
        await asyncio.sleep(.1)
        self.assertEqual(len(self.fixture.requests), 0)
        requests.append(asyncio.create_task(native_request(self.port, self.channel, 49, compressed=True)))
        results = await asyncio.wait_for(asyncio.gather(*requests), 15)
        self.assertTrue(all(status == 200 for status, _ in results))
        self.assertEqual(self.fixture.peak, 50)

    async def test_failed_initial_cohort_stays_closed_without_consuming_more_attempts(self):
        self.channel.cohort_timeout = .05
        first = await native_request(self.port, self.channel, 0)
        self.assertEqual(first[0], 409)
        self.assertTrue(self.channel.snapshot()['fault'])
        self.assertFalse(self.channel.first_wave_sent)
        attempts = self.channel.budget.snapshot()['attempts']
        self.assertEqual(attempts, 1)
        later = await self.wave()
        self.assertTrue(all(status == 409 for status, _ in later))
        self.assertEqual(self.channel.budget.snapshot()['attempts'], attempts)
        self.assertEqual(self.fixture.requests, [])

    async def test_paused_batch_is_rejected_before_reserving_or_connecting(self):
        self.channel.dispatch_check = lambda: False
        result = await native_request(self.port, self.channel, 0)
        self.assertEqual(result[0], 409)
        self.assertEqual(self.channel.budget.snapshot()['attempts'], 0)
        self.assertEqual(self.fixture.requests, [])

    async def test_missing_usage_cannot_create_a_second_round(self):
        self.fixture.mode = 'missing_usage'
        results = await self.wave()
        self.assertTrue(all(status == 200 for status, _ in results))
        self.assertEqual((await native_request(self.port, self.channel, 0))[0], 409)
        self.assertEqual(len(self.fixture.requests), 50)

    async def test_independent_native_title_thread_is_rejected_before_spending(self):
        status, _ = await native_request(self.port, self.channel, 0, session=str(uuid.uuid4()))
        self.assertEqual(status, 409)
        self.assertEqual(self.channel.budget.snapshot()['attempts'], 0)
        self.assertEqual(len(self.fixture.requests), 0)

    async def test_invalid_capability_never_spends(self):
        status, _ = await native_request(self.port, self.channel, 0, token='x' * 40)
        self.assertEqual(status, 403)
        self.assertEqual(self.channel.budget.snapshot()['attempts'], 0)

    async def test_tool_output_never_reaches_native_or_authorizes_more_requests(self):
        self.fixture.mode = 'tool'
        results = await self.wave()
        self.assertTrue(all(status == 502 and b'apply_patch' not in raw for status, raw in results))
        self.assertIsNone(self.channel.budget.snapshot()['first_complete'])
        self.assertEqual((await native_request(self.port, self.channel, 0))[0], 409)
        self.assertEqual(len(self.fixture.requests), 50)

    async def test_requested_output_limit_is_not_silently_removed_on_failure(self):
        self.fixture.mode = 'limit_ignored'
        results = await self.wave()
        self.assertTrue(all(status == 502 for status, _ in results))
        self.assertIsNone(self.channel.budget.snapshot()['first_complete'])
        self.assertEqual(len(self.fixture.requests), 50)
        self.assertEqual((await native_request(self.port, self.channel, 0))[0], 409)

    async def test_deltas_without_completion_do_not_mark_access_success(self):
        self.fixture.mode = 'delta_only'
        results = await self.wave()
        self.assertTrue(all(status == 502 for status, _ in results))
        self.assertIsNone(self.channel.budget.snapshot()['first_complete'])

    async def test_explicit_sse_congestion_rejection_can_retry_within_the_http_budget(self):
        self.fixture.mode = 'sse_error'
        results = await self.wave()
        self.assertTrue(all(status == 503 for status, _ in results))
        self.assertEqual(self.channel.budget.snapshot()['blocked_slots'], [])
        self.fixture.mode = 'error'
        self.assertEqual((await native_request(self.port, self.channel, 0))[0], 500)
        self.assertEqual(self.channel.budget.snapshot()['attempts'], 51)

    async def test_failure_after_generated_content_does_not_authorize_another_attempt(self):
        self.fixture.mode = 'sse_error_after_delta'
        results = await self.wave()
        self.assertTrue(all(status == 502 for status, _ in results))
        self.assertEqual((await native_request(self.port, self.channel, 0))[0], 409)
        self.assertEqual(len(self.fixture.requests), 50)

    async def test_http_rejections_still_consume_the_finite_attempt_budget(self):
        self.fixture.mode = 'error'
        limited = new_channel(self.tmp.name, self.upstream_port, number=1, max_attempts=50)
        self.gateway.channels[limited.budget.policy.job_id] = limited
        results = await self.wave(limited)
        self.assertTrue(all(status == 500 for status, _ in results))
        self.assertEqual((await native_request(self.port, limited, 0))[0], 409)
        self.assertEqual(len(self.fixture.requests), 50)

    async def test_success_in_one_workspace_does_not_close_another_workspace(self):
        self.fixture.expected = 100
        self.fixture.modes['1'] = 'error'
        other = new_channel(self.tmp.name, self.upstream_port, number=1)
        self.gateway.channels[other.budget.policy.job_id] = other
        first, second = await asyncio.gather(self.wave(), self.wave(other))
        self.assertTrue(all(status == 200 for status, _ in first))
        self.assertTrue(all(status == 500 for status, _ in second))
        self.assertEqual((await native_request(self.port, self.channel, 0))[0], 409)
        self.assertEqual((await native_request(self.port, other, 0))[0], 500)
        self.assertEqual(len(self.fixture.requests), 101)

    async def test_slow_completion_fsync_does_not_delay_closing_queued_requests(self):
        original = self.channel.budget._append
        release = threading.Event()
        def append(event):
            if event['kind'] == 'finished':
                release.wait(5)
            original(event)
        self.channel.budget._append = append
        wave = asyncio.create_task(self.wave())
        try:
            deadline = time.monotonic() + 5
            while not self.channel.budget.snapshot()['first_complete'] and time.monotonic() < deadline:
                await asyncio.sleep(.005)
            self.assertIsNotNone(self.channel.budget.snapshot()['first_complete'])
            # All four storage workers may be blocked. Denial still requires
            # neither a disk worker nor a CCC inventory scan.
            status, _ = await asyncio.wait_for(native_request(self.port, self.channel, 0), .5)
            self.assertEqual(status, 409)
            self.assertEqual(len(self.fixture.requests), 50)
        finally:
            release.set()
            await wave

    async def test_disconnected_first_wave_member_does_not_trigger_partial_dispatch(self):
        request = asyncio.create_task(native_request(self.port, self.channel, 0))
        while len(self.channel.waiting) != 1:
            await asyncio.sleep(.005)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await asyncio.sleep(.01)
        results = await asyncio.wait_for(asyncio.gather(*[
            native_request(self.port, self.channel, slot) for slot in range(1, 50)]), 10)
        self.assertTrue(all(status == 409 for status, _ in results))
        self.assertEqual(len(self.fixture.requests), 0)

    async def held_wave(self):
        self.fixture.mode = 'hold'
        requests = [asyncio.create_task(native_request(self.port, self.channel, slot)) for slot in range(50)]
        until = time.monotonic() + 5
        while len(self.fixture.requests) < 50 and time.monotonic() < until:
            await asyncio.sleep(.005)
        self.assertEqual(len(self.fixture.requests), 50)
        return requests

    async def wait_for_upstream_disconnects(self):
        until = time.monotonic() + .5
        while self.fixture.disconnects < 50 and time.monotonic() < until:
            await asyncio.sleep(.005)
        self.assertEqual(self.fixture.disconnects, 50)

    async def test_client_cancellation_closes_all_its_in_flight_upstream_sockets(self):
        requests = await self.held_wave()
        for request in requests:
            request.cancel()
        await asyncio.gather(*requests, return_exceptions=True)
        await self.wait_for_upstream_disconnects()

    async def test_transport_closes_before_cancel_outcome_fsync_finishes(self):
        requests = await self.held_wave()
        original = self.channel.budget._append
        release = threading.Event()
        def append(event):
            if event['kind'] == 'finished':
                release.wait(5)
            original(event)
        self.channel.budget._append = append
        try:
            for handler in tuple(self.gateway.tasks):
                handler.cancel()
            await self.wait_for_upstream_disconnects()
            self.assertTrue(self.gateway.tasks, 'slow storage should still be finishing')
        finally:
            release.set()
            await asyncio.gather(*requests, return_exceptions=True)

    async def test_gateway_shutdown_cancels_handlers_before_waiting_for_server_close(self):
        requests = await self.held_wave()
        await asyncio.wait_for(self.gateway.close(), 2)
        await asyncio.gather(*requests, return_exceptions=True)
        await self.wait_for_upstream_disconnects()


class StreamingGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_event_closes_gate_before_http_padding_and_task_handoff(self):
        response = {'id': 'real-response', 'status': 'completed', 'output': [{
            'id': 'real-message', 'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': 'OK'}]}]}
        event = ('data: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n\n').encode()
        for framing in ('chunked', 'content-length'):
            with self.subTest(framing=framing):
                reader = asyncio.StreamReader()
                header = (b'Transfer-Encoding: chunked' if framing == 'chunked' else b'Content-Length: 65536')
                prefix = b'10000\r\n' if framing == 'chunked' else b''
                reader.feed_data(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n' + header + b'\r\n\r\n' + prefix + event)
                seen = []
                task = asyncio.create_task(read_response(reader, 128, on_complete=lambda _: seen.append('closed')))
                await asyncio.sleep(0)
                # No remaining 64 KiB padding, EOF or timeout has arrived.
                self.assertEqual(seen, ['closed'])
                self.assertEqual((await asyncio.wait_for(task, .2))[1]['id'], 'real-response')


def tls_contexts(root):
    root = Path(root)
    config = root / 'openssl.cnf'
    config.write_text('[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n'
                      '[dn]\nCN=localhost\n[ext]\nsubjectAltName=IP:127.0.0.1,DNS:localhost\n')
    cert, key = root / 'cert.pem', root / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-keyout', str(key), '-out', str(cert), '-config', str(config)],
                   check=True, capture_output=True)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cafile=str(cert))
    return server, client


class ConnectFixture:
    def __init__(self, destination):
        self.destination = destination
        self.connections = 0
        self.tasks = set()
    async def start(self):
        self.server = await asyncio.start_server(self.accept, '127.0.0.1', 0, backlog=2048)
        return self.server.sockets[0].getsockname()[1]
    async def accept(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        upstream_writer = None
        try:
            line, _ = await read_head(reader)
            if line != f'CONNECT 127.0.0.1:{self.destination} HTTP/1.1':
                raise AssertionError('CONNECT escaped its loopback fixture')
            upstream_reader, upstream_writer = await asyncio.open_connection('127.0.0.1', self.destination)
            self.connections += 1
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            await writer.drain()
            async def copy(source, dest):
                while True:
                    data = await source.read(65536)
                    if not data:
                        dest.close()
                        return
                    dest.write(data)
                    await dest.drain()
            await asyncio.gather(copy(reader, upstream_writer), copy(upstream_reader, writer))
        except (OSError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            if upstream_writer:
                upstream_writer.close()
            self.tasks.discard(task)
    async def close(self):
        self.server.close()
        await self.server.wait_closed()
        for task in tuple(self.tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


@unittest.skipIf(not hasattr(asyncio.StreamWriter, 'start_tls'), 'HTTP CONNECT TLS requires Python 3.11+')
class RealTLSGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifty_verified_tls_sessions_through_connect_proxy(self):
        with tempfile.TemporaryDirectory() as root:
            server_tls, client_tls = tls_contexts(root)
            fixture = UpstreamFixture()
            port = await fixture.start(server_tls)
            proxy = ConnectFixture(port)
            proxy_port = await proxy.start()
            channel = new_channel(root, port)
            channel.upstream = Upstream(f'https://127.0.0.1:{port}/0/v1', 'gpt-6-astra',
                proxy_url=f'http://127.0.0.1:{proxy_port}', tls_context=client_tls, timeout=10)
            gateway = Gateway({channel.budget.policy.job_id: channel})
            gateway_port = await gateway.start()
            try:
                result = await asyncio.wait_for(asyncio.gather(*[
                    native_request(gateway_port, channel, slot) for slot in range(50)]), 20)
                self.assertEqual([status for status, _ in result], [200] * 50)
                self.assertEqual(fixture.peak, 50)
                self.assertEqual(proxy.connections, 50)
            finally:
                await gateway.close()
                await proxy.close()
                await fixture.close()


if __name__ == '__main__':
    unittest.main()

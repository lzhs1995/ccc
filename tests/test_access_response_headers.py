"""Response-header interoperability and safe diagnostics; local HTTP only."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from ccc_access_gateway import Gateway, ProtocolFault, Upstream, read_head, read_response
from test_access_gateway import UpstreamFixture, native_request, new_channel


COOKIES = b'Set-Cookie: first=server-private; Path=/\r\nSet-Cookie: second=server-private; Path=/\r\n'


def stream(raw):
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()
    return reader


def response(status, body, kind='text/event-stream', extra=COOKIES):
    return (f'HTTP/1.1 {status} Fixture\r\nContent-Type: {kind}\r\nContent-Length: {len(body)}\r\n'.encode()
            + extra + b'Connection: close\r\n\r\n' + body)


def answer():
    return {'id': 'resp_headers', 'status': 'completed', 'output': [
        {'id': 'msg_headers', 'type': 'message', 'role': 'assistant', 'status': 'completed',
         'content': [{'type': 'output_text', 'text': 'OK'}]}],
        'usage': {'input_tokens': 3, 'output_tokens': 1, 'total_tokens': 4}}


def event(value):
    return b'data: ' + json.dumps(value).encode() + b'\n\n'


class ResponseHeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_sse_with_two_cookies_closes_gate_exactly_once(self):
        seen = []
        code, result = await read_response(stream(response(200, event(
            {'type': 'response.completed', 'response': answer()}))), 128, on_complete=seen.append)
        self.assertEqual(code, 200)
        self.assertEqual(result['id'], 'resp_headers')
        self.assertEqual(seen, [result])
        self.assertNotIn('server-private', json.dumps(result))

    async def test_http_overload_with_two_cookies_remains_retryable(self):
        seen = []
        raw = response(500, b'{"error":"high demand"}', 'application/json')
        self.assertEqual(await read_response(stream(raw), 128, on_complete=seen.append), (500, None))
        self.assertEqual(seen, [])

    async def test_sse_overload_with_two_cookies_remains_retryable(self):
        body = event({'type': 'response.failed', 'response': {'id': 'resp_rejected',
            'status': 'failed', 'output': [], 'error': {'code': 'server_error', 'message': 'high demand'}}})
        self.assertEqual(await read_response(stream(response(200, body)), 128), (503, None))

    async def test_html_with_two_cookies_still_fails_with_specific_reason(self):
        with self.assertRaisesRegex(ProtocolFault, 'upstream did not return Responses SSE'):
            await read_response(stream(response(200, b'<html>challenge</html>', 'text/html')), 128)

    async def test_non_framing_list_headers_may_repeat_in_a_response(self):
        extra = COOKIES + b'Vary: Accept-Encoding\r\nvary: Origin\r\nVia: edge-one\r\nVia: edge-two\r\n'
        result = await read_response(stream(response(200, event(
            {'type': 'response.completed', 'response': answer()}), extra=extra)), 128)
        self.assertEqual(result[0], 200)

    async def test_duplicate_framing_and_singleton_response_headers_still_fail(self):
        for extra in (b'Content-Length: 0\r\n', b'Transfer-Encoding: chunked\r\n',
                      b'Content-Type: text/html\r\n',
                      b'Content-Encoding: identity\r\nContent-Encoding: gzip\r\n',
                      b'Connection: keep-alive\r\n'):
            with self.subTest(extra=extra), self.assertRaises(ProtocolFault):
                await read_response(stream(response(200, event(
                    {'type': 'response.completed', 'response': answer()}), extra=extra)), 128)

    async def test_repeated_cookie_with_control_character_is_rejected(self):
        extra = b'Set-Cookie: safe=1\r\nSet-Cookie: unsafe=\x00\r\n'
        with self.assertRaisesRegex(ProtocolFault, 'invalid HTTP header value'):
            await read_response(stream(response(200, b'', extra=extra)), 128)

    async def test_native_request_headers_remain_strict(self):
        for name in ('Authorization', 'thread-id', 'Content-Length', 'Set-Cookie', 'Vary'):
            raw = f'POST / HTTP/1.1\r\n{name}: one\r\n{name}: two\r\n\r\n'.encode()
            with self.subTest(name=name), self.assertRaises(ProtocolFault):
                await read_head(stream(raw))

    async def test_connect_proxy_response_also_accepts_two_cookies(self):
        received = []
        async def proxy(reader, writer):
            try:
                received.append((await read_head(reader))[0])
                writer.write(b'HTTP/1.1 200 Connection Established\r\n' + COOKIES + b'\r\n'
                             + response(200, event({'type': 'response.completed', 'response': answer()})))
                await writer.drain()
            finally:
                writer.close()
        server = await asyncio.start_server(proxy, '127.0.0.1', 0)
        try:
            port = server.sockets[0].getsockname()[1]
            upstream = Upstream('http://127.0.0.1:9999/v1', 'fixture',
                                proxy_url=f'http://127.0.0.1:{port}', allow_loopback=True)
            reader, writer = await upstream.connect()
            try:
                self.assertEqual((await read_response(reader, 128))[0], 200)
            finally:
                writer.close()
                await writer.wait_closed()
            self.assertEqual(received, ['CONNECT 127.0.0.1:9999 HTTP/1.1'])
        finally:
            server.close()
            await server.wait_closed()


class CookieFixture(UpstreamFixture):
    async def accept(self, reader, writer):
        original = writer.write
        def write(raw):
            if self.mode == 'html':
                original(response(200, b'<html>challenge server-private</html>', 'text/html'))
            else:
                original(raw.replace(b'\r\n', b'\r\n' + COOKIES, 1))
        writer.write = write
        await super().accept(reader, writer)


class RealCookieGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = CookieFixture()
        port = await self.fixture.start()
        self.channel = new_channel(self.tmp.name, port)
        self.gateway = Gateway({self.channel.budget.policy.job_id: self.channel})
        self.port = await self.gateway.start()

    async def asyncTearDown(self):
        await self.gateway.close()
        await self.fixture.close()

    async def wave(self):
        return await asyncio.wait_for(asyncio.gather(*[
            native_request(self.port, self.channel, slot) for slot in range(50)]), 15)

    async def test_real_fifty_cookie_responses_complete_and_close_new_admissions(self):
        self.assertEqual([code for code, _ in await self.wave()], [200] * 50)
        self.assertEqual(self.fixture.peak, 50)
        self.assertEqual(self.channel.snapshot()['blocked_slots'], [])
        self.assertTrue(self.channel.snapshot()['first_complete'])
        self.assertEqual((await native_request(self.port, self.channel, 0))[0], 409)
        self.assertEqual(len(self.fixture.requests), 50)

    async def test_real_fifty_cookie_rejections_do_not_poison_the_budget(self):
        for mode, code in (('error', 500), ('sse_error', 503)):
            self.fixture.mode = mode
            self.assertEqual([status for status, _ in await self.wave()], [code] * 50)
            self.assertEqual(self.channel.snapshot()['blocked_slots'], [])
        self.assertEqual(self.channel.snapshot()['attempts'], 100)
        self.assertFalse(self.channel.snapshot()['first_complete'])

    async def test_real_html_failure_is_diagnosable_and_does_not_replay(self):
        self.fixture.mode = 'html'
        results = await self.wave()
        # Error responses close the socket before durable budget finalization.
        # Observe the ledger only after every accepted handler has finished.
        await asyncio.wait_for(asyncio.gather(*tuple(self.gateway.tasks)), 15)
        self.assertEqual([code for code, _ in results], [502] * 50)
        detail = self.channel.snapshot()['last_error']
        self.assertEqual(detail['stage'], 'upstream_response')
        self.assertEqual(detail['type'], 'ProtocolFault')
        self.assertEqual(detail['reason'], 'upstream did not return Responses SSE')
        self.assertEqual(self.channel.snapshot()['slot_results']['0']['error']['reason'], detail['reason'])
        self.assertNotIn('server-private', json.dumps(detail))
        self.assertTrue(all(b'upstream did not return Responses SSE' in raw for _, raw in results))
        self.assertTrue(all(b'server-private' not in raw for _, raw in results))
        self.assertEqual(self.channel.snapshot()['blocked_slots'], list(range(50)))
        self.assertEqual((await native_request(self.port, self.channel, 0))[0], 409)
        self.assertEqual(len(self.fixture.requests), 50)

    async def test_non_protocol_exception_text_is_never_exposed(self):
        async def fail():
            raise OSError('Bearer fixture-secret must not appear in diagnostics')
        object.__setattr__(self.channel.upstream, 'connect', fail)
        code, raw = await native_request(self.port, self.channel, 0)
        self.assertEqual(code, 502)
        detail = self.channel.snapshot()['last_error']
        self.assertEqual(detail['stage'], 'upstream_connect')
        self.assertNotIn('fixture-secret', json.dumps(detail))
        self.assertNotIn(b'fixture-secret', raw)

    async def test_unknown_protocol_exception_reason_is_not_forwarded(self):
        async def fail():
            raise ProtocolFault('Cookie fixture-private must not appear in diagnostics')
        object.__setattr__(self.channel.upstream, 'connect', fail)
        code, raw = await native_request(self.port, self.channel, 0)
        self.assertEqual(code, 502)
        self.assertNotIn(b'fixture-private', raw)
        self.assertEqual(self.channel.snapshot()['last_error']['reason'], 'HTTP protocol validation failed')


if __name__ == '__main__':
    unittest.main()

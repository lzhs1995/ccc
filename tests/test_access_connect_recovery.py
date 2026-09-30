"""Connection setup failures must not permanently poison a sustained N pool."""
import asyncio
import errno
import json
from pathlib import Path
import secrets
import ssl
import tempfile
import threading
import time
import unittest
import uuid

from ccc_access_budget import AccessBudget, Policy
from ccc_access_gateway import BatchChannel, Gateway, Upstream, error_detail
from ccc_access_service import continuation_decision
from tests.test_access_gateway import UpstreamFixture, native_request


class SustainedConnectRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = UpstreamFixture()
        upstream_port = await self.fixture.start()
        self.policy = Policy(str(uuid.uuid4()), str(uuid.uuid4()),
                             max_attempts=None, attempt_mode='sustained')
        self.budget = AccessBudget(Path(self.tmp.name) / 'journal.jsonl', self.policy, create=True)
        self.channel = BatchChannel(self.budget,
            Upstream(f'http://127.0.0.1:{upstream_port}/0/v1', 'fixture', allow_loopback=True),
            [secrets.token_urlsafe(32) for _ in range(50)],
            sessions={i: str(uuid.uuid4()) for i in range(50)}, cohort_timeout=3)
        self.gateway = Gateway({self.policy.job_id: self.channel})
        self.port = await self.gateway.start()
        self.requests = []

    async def asyncTearDown(self):
        for task in self.requests:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.requests, return_exceptions=True)
        await self.gateway.close()
        await self.fixture.close()

    def request(self, slot):
        task = asyncio.create_task(native_request(self.port, self.channel, slot))
        self.requests.append(task)
        return task

    async def settle(self):
        deadline = time.monotonic() + 3
        while self.budget.snapshot()['in_flight'] and time.monotonic() < deadline:
            await asyncio.sleep(.005)
        self.assertEqual(self.budget.snapshot()['in_flight'], 0)

    def decision(self, slot):
        value = {**self.channel.snapshot(), 'authorized': True, 'updated_at': time.time()}
        return continuation_decision({'job_id': self.policy.job_id, 'index': slot},
                                     value, self.policy.workspace_id)

    def fail_next_connect(self, exc):
        connect = self.channel.upstream.connect
        failed = False
        async def once():
            nonlocal failed
            if not failed:
                failed = True
                raise exc
            return await connect()
        object.__setattr__(self.channel.upstream, 'connect', once)

    async def test_one_reset_retries_original_slot_while_other_49_wait_without_sending(self):
        self.fail_next_connect(ConnectionResetError(errno.ECONNRESET, 'fixture-private'))
        tasks = [self.request(i) for i in range(50)]
        done, _ = await asyncio.wait(tasks, timeout=3, return_when=asyncio.FIRST_COMPLETED)
        self.assertEqual(len(done), 1)
        failed = next(iter(done))
        slot = tasks.index(failed)
        code, raw = failed.result()
        self.assertEqual(code, 503)
        self.assertNotIn(b'fixture-private', raw)
        self.assertEqual(self.fixture.requests, [])
        self.assertFalse(self.channel.snapshot()['fault'])
        self.assertTrue(self.decision(slot)['allowed'])
        retry = self.request(slot)
        results = await asyncio.wait_for(asyncio.gather(
            *[t for t in tasks if t is not failed], retry), 5)
        self.assertEqual([status for status, _ in results], [200] * 50)
        await self.settle()
        self.assertEqual(self.fixture.peak, 50)
        self.assertEqual(self.budget.snapshot()['attempts'], 51)
        self.assertEqual(self.channel.metrics['forwarded'], 50)
        self.assertEqual(len(self.fixture.requests), 50)

    async def test_waiting_deadline_can_reform_same_fifty_without_resetting_attempts(self):
        self.channel.cohort_timeout = .04
        self.assertEqual((await self.request(0))[0], 503)
        await self.settle()
        self.assertFalse(self.channel.snapshot()['fault'])
        self.assertTrue(self.decision(0)['allowed'])
        self.assertEqual(self.fixture.requests, [])
        prefix = self.budget.path.read_bytes()
        self.channel.cohort_timeout = 3
        results = await asyncio.wait_for(asyncio.gather(*[self.request(i) for i in range(50)]), 5)
        self.assertEqual([code for code, _ in results], [200] * 50)
        self.assertEqual(self.budget.snapshot()['attempts'], 51)
        self.assertTrue(self.budget.path.read_bytes().startswith(prefix))
        self.assertEqual(self.fixture.peak, 50)

    async def test_cancelled_waiter_does_not_cancel_other_native_sessions(self):
        first = self.request(0)
        deadline = time.monotonic() + 2
        while not self.channel.waiting and time.monotonic() < deadline:
            await asyncio.sleep(.005)
        self.assertEqual(len(self.channel.waiting), 1)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await self.settle()
        self.assertFalse(self.channel.snapshot()['fault'])
        results = await asyncio.wait_for(asyncio.gather(*[self.request(i) for i in range(50)]), 5)
        self.assertEqual([code for code, _ in results], [200] * 50)
        self.assertEqual(self.fixture.peak, 50)

    async def test_later_connection_timeout_is_retryable_but_no_success_gate_is_reopened(self):
        self.fixture.mode = 'error'
        results = await asyncio.wait_for(asyncio.gather(*[self.request(i) for i in range(50)]), 5)
        self.assertEqual([code for code, _ in results], [500] * 50)
        self.fail_next_connect(TimeoutError())
        self.assertEqual((await self.request(0))[0], 503)
        self.assertTrue(self.decision(0)['allowed'])
        self.fixture.mode = 'success'
        self.assertEqual((await self.request(0))[0], 200)
        self.assertEqual((await self.request(0))[0], 409)
        self.assertEqual(len(self.fixture.requests), 51)
        self.assertEqual(self.budget.snapshot()['attempts'], 52)

    async def test_local_storage_failure_is_not_mislabeled_as_a_connection_retry(self):
        def fail(_):
            raise OSError(errno.ENOSPC, 'fixture-private')
        self.budget._append = fail
        code, _ = await self.request(0)
        self.assertEqual(code, 502)
        self.assertFalse(self.decision(0)['allowed'])
        self.assertEqual(self.fixture.requests, [])

    async def test_upstream_fin_before_cohort_is_not_counted_as_a_forwarded_request(self):
        closed = asyncio.Event()
        async def upstream(reader, writer):
            if not closed.is_set():
                writer.close()
                await writer.wait_closed()
                closed.set()
                return
            await self.fixture.accept(reader, writer)
        server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        object.__setattr__(self.channel.upstream, 'base_url',
                           f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/0/v1')
        try:
            first = self.request(0)
            await asyncio.wait_for(closed.wait(), 2)
            await asyncio.sleep(.05)
            others = [self.request(i) for i in range(1, 50)]
            self.assertEqual((await asyncio.wait_for(first, 2))[0], 503)
            self.assertEqual(len(self.fixture.requests), 0)
            self.assertEqual(self.channel.snapshot()['forwarded'], 0)
            replacement = self.request(0)
            results = await asyncio.wait_for(asyncio.gather(*others, replacement), 5)
            self.assertEqual([code for code, _ in results], [200] * 50)
            self.assertEqual(len(self.fixture.requests), 50)
            self.assertEqual(self.channel.snapshot()['forwarded'], 50)
            self.assertEqual(self.budget.snapshot()['attempts'], 51)
        finally:
            server.close()
            await server.wait_closed()

    async def test_retry_response_waits_for_failed_attempt_to_finish_persisting(self):
        self.fail_next_connect(ConnectionRefusedError(errno.ECONNREFUSED, 'fixture-private'))
        append = self.budget._append
        entered, release = threading.Event(), threading.Event()
        def slow(event):
            if event['kind'] == 'finished':
                entered.set()
                release.wait(3)
            append(event)
        self.budget._append = slow
        request = self.request(0)
        try:
            deadline = time.monotonic() + 2
            while not entered.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(.005)
            self.assertTrue(entered.is_set())
            self.assertFalse(request.done())
            self.assertEqual(self.fixture.requests, [])
        finally:
            release.set()
        self.assertEqual((await request)[0], 503)
        self.assertEqual(self.budget.snapshot()['in_flight'], 0)

    async def test_new_fiftieth_connection_with_eof_cannot_release_first_49(self):
        accepted = 0
        async def upstream(reader, writer):
            nonlocal accepted
            accepted += 1
            if accepted == 50:
                writer.close()
                await writer.wait_closed()
            else:
                await self.fixture.accept(reader, writer)
        server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        object.__setattr__(self.channel.upstream, 'base_url',
                           f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/0/v1')
        connect, connecting = self.channel.upstream.connect, 0
        async def delayed_last():
            nonlocal connecting
            connecting += 1
            number = connecting
            reader, writer = await connect()
            if number == 50:
                await asyncio.sleep(.05)
                self.assertTrue(reader.at_eof())
            return reader, writer
        object.__setattr__(self.channel.upstream, 'connect', delayed_last)
        try:
            requests = [self.request(i) for i in range(50)]
            done, _ = await asyncio.wait(requests, timeout=2, return_when=asyncio.FIRST_COMPLETED)
            self.assertEqual(len(done), 1)
            first = next(iter(done))
            self.assertEqual(first.result()[0], 503)
            self.assertEqual(self.fixture.requests, [])
            self.assertEqual(self.channel.metrics['forwarded'], 0)
            retry = self.request(requests.index(first))
            results = await asyncio.wait_for(asyncio.gather(
                *[r for r in requests if r is not first], retry), 5)
            self.assertEqual([code for code, _ in results], [200] * 50)
            self.assertEqual(len(self.fixture.requests), 50)
        finally:
            server.close()
            await server.wait_closed()

    async def test_certificate_error_is_visible_without_disabling_verification_or_auto_retry(self):
        self.fail_next_connect(ssl.SSLCertVerificationError(1, 'fixture-private'))
        code, raw = await self.request(0)
        self.assertEqual(code, 502)
        self.assertNotIn(b'fixture-private', raw)
        state = self.decision(0)
        self.assertFalse(state['allowed'])
        self.assertEqual(state['error']['type'], 'TLSVerificationError')


class ConnectionDiagnosticTests(unittest.TestCase):
    def test_errno_is_retained_without_server_or_credential_text(self):
        detail = error_detail(ConnectionResetError(errno.ECONNRESET, 'Bearer fixture-private'),
                              'upstream_connect')
        self.assertEqual(detail.get('errno'), errno.ECONNRESET)
        self.assertNotIn('fixture-private', json.dumps(detail))


if __name__ == '__main__':
    unittest.main()

"""Actual local Unix socket writes exercise the ledger/transport boundary."""
import copy
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from unittest import mock

import cmux_codex_watch as core
from ccc_standby_transport import send_initial
from tests import test_native_standby as ledger_fixture
from tests.test_cmux_viewport_socket import server, send, response


class StandbyTransportTests(unittest.TestCase):
    def setUp(self):
        self.state = ledger_fixture.StandbyLedgerTests()
        self.state.setUp()
        self.addCleanup(self.state.doCleanups)
        self.state.ready()
        self.state.activate()

    def client(self, transport):
        transport.control_methods = frozenset({'terminal.paste', 'system.tree'})
        return core.CmuxClient(viewport_socket=transport,
            runner=mock.Mock(side_effect=AssertionError('no real cmux or CLI fallback')))

    def deliver(self, client, index=0, **changes):
        return self.state.deliver(index, send=partial(send_initial, client), **changes)

    def test_one_atomic_paste_with_original_target_and_no_replay(self):
        with server(lambda c, r: send(c, response(r, submitted=True))) as (transport, requests):
            client = self.client(transport)
            self.assertTrue(self.deliver(client))
            self.assertFalse(self.deliver(client))
            self.assertEqual(len(requests), 1)
            row = self.state.rows[0]
            self.assertEqual(requests[0]['method'], 'terminal.paste')
            self.assertEqual(requests[0]['params'], dict(workspace_id=row['workspace_id'],
                surface_id=row['surface_id'], text='fixed prompt', submit_key='enter'))
            client.runner.assert_not_called()

    def test_pid_reuse_during_connection_is_rejected_before_actual_write(self):
        connect = core._connect_local_socket
        def changed(*args):
            connect(*args)
            self.state.rows[0]['birth'][1] += 1
        with server(lambda *_: self.fail('unexpected input')) as (transport, requests):
            client = self.client(transport)
            with mock.patch.object(core, '_connect_local_socket', side_effect=changed):
                with self.assertRaises(core.CmuxError):
                    self.deliver(client)
            self.assertFalse(requests)
            self.assertFalse(self.deliver(client))
            self.assertTrue((self.state.path / 'invalidated.json').exists())

    def test_permission_change_during_connection_is_rejected_before_actual_write(self):
        connect = core._connect_local_socket
        allowed = [True]
        def changed(*args):
            connect(*args)
            allowed[0] = False
        with server(lambda *_: self.fail('unexpected input')) as (transport, requests):
            client = self.client(transport)
            with mock.patch.object(core, '_connect_local_socket', side_effect=changed):
                with self.assertRaises(core.CmuxError):
                    self.deliver(client, authorized=lambda i: allowed[0])
            self.assertFalse(requests)

    def test_ack_loss_is_consumed_without_retry_or_fallback(self):
        with server(lambda *_: None) as (transport, requests):
            client = self.client(transport)
            with self.assertRaises(core.UncertainDeliveryError):
                self.deliver(client)
            self.assertFalse(self.deliver(client))
            self.assertEqual(len(requests), 1)
            client.runner.assert_not_called()

    def test_missing_paste_support_fails_before_any_input(self):
        with server(lambda *_: self.fail('unexpected input')) as (transport, requests):
            client = self.client(transport)
            transport.control_methods = frozenset()
            with self.assertRaises(core.InputNotSentError):
                self.deliver(client)
            self.assertFalse(self.deliver(client))
            self.assertFalse(requests)
            client.runner.assert_not_called()

    def test_one_ack_wait_does_not_hold_other_slots_write_lock(self):
        entered, release = threading.Event(), threading.Event()
        def handle(connection, request):
            if request['params']['surface_id'] == self.state.rows[0]['surface_id']:
                entered.set()
                release.wait(3)
            send(connection, response(request, submitted=True))
        with server(handle) as (transport, requests), ThreadPoolExecutor(2) as pool:
            client = self.client(transport)
            slow = pool.submit(self.deliver, client)
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(pool.submit(self.deliver, client, 1).result(1))
                self.assertFalse(slow.done())
            finally:
                release.set()
            self.assertTrue(slow.result(2))
            self.assertEqual(len(requests), 2)

    def test_guard_reads_use_the_admitted_connection_before_write(self):
        following = []
        def handle(connection, request):
            self.assertEqual(request['method'], 'system.tree')
            send(connection, dict(id=request['id'], ok=True, result={'windows': []}))
            data = bytearray()
            while b'\n' not in data:
                data.extend(connection.recv(65536))
            paste = json.loads(data)
            following.append(paste)
            send(connection, response(paste, submitted=True))
        with server(handle) as (transport, requests):
            client = self.client(transport)
            calls = []
            def observe(index):
                calls.append(index)
                if len(calls) == 3:  # The guard entry after socket connection.
                    self.assertEqual(client.tree(), {'windows': []})
                return copy.deepcopy(self.state.rows[index])
            self.assertTrue(self.deliver(client, observe=observe))
            self.assertEqual(len(requests), 1)
            self.assertEqual([r['method'] for r in following], ['terminal.paste'])
            self.assertIsNone(transport._connection_local.read_rpc)

    def test_identity_and_authorization_are_checked_after_waiting_for_write_lock(self):
        for changed_field in ('birth', 'authorization'):
            with self.subTest(changed_field=changed_field):
                if changed_field == 'authorization':
                    self.state.doCleanups()
                    self.setUp()
                reached = threading.Event()
                real_lock = self.state.ledger._write_lock
                class GatedLock:
                    def __enter__(self):
                        reached.set()
                        return real_lock.__enter__()
                    def __exit__(self, *args):
                        return real_lock.__exit__(*args)
                self.state.ledger._write_lock = GatedLock()
                allowed = [True]
                with server(lambda *_: self.fail('input after identity changed')) as (transport, requests), \
                        ThreadPoolExecutor(1) as pool:
                    client = self.client(transport)
                    real_lock.acquire()
                    pending = pool.submit(self.deliver, client, authorized=lambda i: allowed[0])
                    try:
                        self.assertTrue(reached.wait(1))
                        if changed_field == 'birth':
                            self.state.rows[0]['birth'][1] += 1
                        else:
                            allowed[0] = False
                    finally:
                        real_lock.release()
                    with self.assertRaises(core.CmuxError):
                        pending.result(2)
                    self.assertFalse(requests)
                    self.assertTrue((self.state.path / 'invalidated.json').exists())


if __name__ == '__main__':
    unittest.main()

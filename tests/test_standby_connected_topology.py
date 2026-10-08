"""An admitted writer must not wait on readers needing its connection slot."""
import contextlib
import json
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import cmux_codex_watch as core
from ccc_standby_prepare import FreshTopology, PreparationOwner
from tests.test_cmux_viewport_socket import response, send, server


class ConnectedTopologyTests(unittest.TestCase):
    def exercise(self, change=None):
        wid, sid = str(uuid.uuid4()), str(uuid.uuid4())
        entered = threading.Event()
        allowed = [True]
        requests = []
        tree = {'windows': [{'workspaces': [{'id': wid, 'panes': [
            {'surfaces': [{'id': sid, 'ref': 'surface:1', 'type': 'terminal'}]}]}]}]}

        def handle(connection, request):
            while True:
                requests.append(request)
                if request['method'] == 'system.tree':
                    if change == 'permission':
                        allowed[0] = False
                    if change == 'move':
                        tree['windows'][0]['workspaces'][0]['panes'][0]['surfaces'] = []
                    send(connection, dict(id=request['id'], ok=True, result=tree))
                else:
                    self.assertEqual(request['method'], 'terminal.paste')
                    send(connection, response(request, submitted=True))
                data = bytearray()
                while b'\n' not in data:
                    chunk = connection.recv(65536)
                    if not chunk:
                        return
                    data.extend(chunk)
                request = json.loads(data)

        class BoundedClient(core.CmuxClient):
            def _control_rpc(self, method, params, **kwargs):
                return super()._control_rpc(method, params, timeout=.5, **kwargs)

        with server(handle) as (transport, _), ThreadPoolExecutor(1) as pool:
            transport._connection_slots = threading.BoundedSemaphore(1)
            transport.control_methods = frozenset({'system.tree', 'terminal.paste'})
            client = BoundedClient(viewport_socket=transport,
                runner=Mock(side_effect=AssertionError('no external controller')))

            def topology():
                entered.set()
                return client.workspace_tree(wid)

            owner = SimpleNamespace(client=client, _topology=FreshTopology(topology),
                _connected_topology=FreshTopology(lambda: client.workspace_tree(wid)),
                job={'workspace_id': wid, 'slots': [{}]}, _current=lambda: None,
                _permission=lambda *_: allowed[0], _failed=threading.Event())
            waiting = []

            def authorization():
                # This callback runs after the sole connection is admitted.
                # The coalesced reader starts but cannot acquire that slot.
                waiting.append(pool.submit(owner._topology))
                self.assertTrue(entered.wait(1))
                self.assertFalse(waiting[0].done())
                return PreparationOwner._authorization(owner, 0,
                    surface_id=sid, connected=client)

            @contextlib.contextmanager
            def guard():
                if not authorization():
                    raise core.InputNotSentError('authorization revoked')
                yield

            with self.assertRaises(core.CmuxError) if change else contextlib.nullcontext():
                client._control_rpc('terminal.paste', dict(workspace_id=wid,
                    surface_id=sid, text='test', submit_key='enter'), write_guard=guard)
            self.assertEqual(waiting[0].result(2), tree)
            self.assertIsNone(transport._connection_local.read_rpc)
            self.assertEqual(sum(r['method'] == 'terminal.paste' for r in requests),
                             0 if change else 1)
            client.runner.assert_not_called()

    def test_admitted_guard_bypasses_reader_waiting_for_its_connection(self):
        self.exercise()

    def test_permission_revocation_during_connected_read_prevents_paste(self):
        self.exercise('permission')

    def test_surface_move_during_connected_read_prevents_paste(self):
        self.exercise('move')


if __name__ == '__main__':
    unittest.main()

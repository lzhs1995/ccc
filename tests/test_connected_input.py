import json
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
import cmux_codex_watch as core

class ConnectedInputTests(unittest.TestCase):
    def exercise(self, fault=None):
        with tempfile.TemporaryDirectory(prefix='ccc-ci-', dir='/tmp') as root:
            rows=[]; connections=[]
            class Server(socketserver.ThreadingUnixStreamServer):
                daemon_threads=True
            class Handler(socketserver.StreamRequestHandler):
                def handle(self):
                    connections.append(1)
                    while True:
                        line=self.rfile.readline()
                        if not line:return
                        req=json.loads(line);rows.append(req['method'])
                        params=req['params']
                        if req['method']=='terminal.replay':
                            if fault=='eof':return
                            result={'workspace_id':params['workspace_id'], 'surface_id':params['surface_id'],
                                    'render_grid':{'surface_id':params['surface_id'],'anchor':'screen'}}
                            if fault=='identity':result['workspace_id']='other'
                        else:result={'workspace_id':params['workspace_id'],'surface_id':params['surface_id']}
                        response={'id':'wrong' if fault=='id' else req['id'],'ok':True,'result':result}
                        self.wfile.write((json.dumps(response)+'\n').encode())
            path=str(Path(root)/'c.sock');server=Server(path,Handler)
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
            transport=core.CmuxViewportSocket(max_connections=1);transport.path=path
            transport.control_methods=frozenset({'surface.send_key','surface.send_text'})
            client=core.CmuxClient(viewport_socket=transport)
            def check():
                client.replay('w','s',live=True)
                return fault!='deny'
            try:
                with client.input_guard(check):
                    if fault:
                        with self.assertRaises(core.CmuxError):client.send_key('w','s','enter')
                    else:client.send_key('w','s','enter')
                self.assertEqual(rows,['terminal.replay']+([] if fault else ['surface.send_key']))
                self.assertEqual(len(connections),1)
                self.assertIsNone(getattr(transport._connection_local,'read_rpc',None))
                self.assertTrue(transport._connection_slots.acquire(blocking=False))
                transport._connection_slots.release()
            finally:
                server.shutdown();server.server_close();thread.join()
    def test_same_connection_read_then_single_input(self):self.exercise()
    def test_wrong_id_never_inputs(self):self.exercise('id')
    def test_eof_never_inputs(self):self.exercise('eof')
    def test_wrong_identity_never_inputs(self):self.exercise('identity')
    def test_final_denial_never_inputs(self):self.exercise('deny')

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import cmux_codex_watch as core


class ControllerAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.path = str(Path(self.temporary.name)/'control.sock')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen()
        self.addCleanup(self.server.close)

    def test_processes_share_capacity(self):
        code = ('import json,time,sys; from cmux_codex_watch import controller_admission; '
                '\nwith controller_admission(sys.argv[1],time.monotonic()+5,capacity=2):'
                '\n start=time.monotonic(); time.sleep(.06); print(json.dumps([start,time.monotonic()]))')
        processes = [subprocess.Popen([sys.executable,'-B','-c',code,self.path], stdout=subprocess.PIPE,
                                     text=True) for _ in range(8)]
        rows = []
        try:
            for process in processes:
                output, _ = process.communicate(timeout=10)
                self.assertEqual(process.returncode,0)
                rows.append(json.loads(output))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        events = sorted([(a,1) for a,b in rows]+[(b,-1) for a,b in rows])
        active = maximum = 0
        for _,change in events:
            active += change
            maximum = max(maximum,active)
        self.assertEqual(maximum,2)
        self.assertEqual(active,0)

    def test_crashed_holder_releases_capacity(self):
        code = ('import sys,time; from cmux_codex_watch import controller_admission; '
                '\nwith controller_admission(sys.argv[1],time.monotonic()+5,capacity=1):'
                '\n print("held",flush=True); time.sleep(30)')
        process = subprocess.Popen([sys.executable,'-B','-c',code,self.path],stdout=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'held')
            with self.assertRaises(core.InputNotSentError):
                with core.controller_admission(self.path,time.monotonic()+.03,capacity=1):
                    self.fail('over-admitted')
            process.kill()
            process.wait(timeout=5)
            with core.controller_admission(self.path,time.monotonic()+1,capacity=1):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()

    def test_expired_deadline_does_not_enter(self):
        with self.assertRaises(core.InputNotSentError):
            with core.controller_admission(self.path,time.monotonic()-1):
                self.fail('expired admission entered')


if __name__ == '__main__':
    unittest.main()

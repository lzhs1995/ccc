"""Private real Python children, local files and adversarial RPC responses."""
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import ccc_codex_queue as queue
import ccc_standby_inventory as inventory


class InventoryProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ccc-inventory-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.children = []

    def fake(self, body, *, timeout=1, handshake=True, high_fds=False):
        if high_fds:
            if os.name != 'posix':
                self.skipTest('POSIX high pipe descriptors')
            import fcntl
            import resource
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            if soft != resource.RLIM_INFINITY and soft < 2060:
                self.skipTest('requires existing FD limit >= 2060; never raises it')
        script = self.root / 'worker.py'
        prelude = 'import json, os, sys, time\n'
        if handshake:
            prelude += "print(json.dumps({'protocol':1,'ready':os.getpid()}), flush=True)\nrequest=json.loads(sys.stdin.readline())\n"
        script.write_text(prelude + body)
        real = subprocess.Popen
        def spawn(_args, **kwargs):
            child = real([sys.executable, '-I', '-S', '-B', str(script)], **kwargs)
            self.children.append(child)
            if high_fds:
                for name, mode in [('stdin', 'wb'), ('stdout', 'rb')]:
                    stream = getattr(child, name)
                    fd = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD_CLOEXEC, 2048)
                    setattr(child, name, os.fdopen(fd, mode, buffering=0))
                    stream.close()
            return child
        with patch.object(inventory.subprocess, 'Popen', side_effect=spawn) as launch:
            reader = inventory.ProcessInventoryReader(timeout=timeout)
        self.addCleanup(reader.close)
        return reader, launch

    def test_live_fresh_add_remove_inode_and_options(self):
        if sys.platform != 'darwin':
            self.skipTest('real Darwin libproc')
        reader = inventory.ProcessInventoryReader()
        self.addCleanup(reader.close)
        pid = reader._child.pid
        file = self.root / 'writer.jsonl'
        with file.open('wb') as handle:
            info = os.fstat(handle.fileno())
            expected = {'device': info.st_dev, 'inode': info.st_ino}
            self.assertEqual(reader(os.getpid(), identities=True)[file], expected)
            self.assertIn(file, reader(os.getpid(), writer_identity_only=True))
        self.assertNotIn(file, reader(os.getpid(), identities=True))
        self.assertEqual(reader._child.pid, pid)
        reader.close()
        with self.assertRaises(OSError):
            reader(os.getpid())

    def test_private_child_does_not_inherit_original_writer(self):
        if sys.platform != 'darwin':
            self.skipTest('real Darwin libproc')
        file = self.root / 'never-inherit.jsonl'
        with file.open('wb') as handle:
            os.set_inheritable(handle.fileno(), True)
            reader = inventory.ProcessInventoryReader()
            self.addCleanup(reader.close)
            actual = queue.process_writable_files(reader._child.pid, identities=True)
            self.assertNotIn(file, actual)

    def test_wrong_sequence_pid_flags_and_protocol_close_channel(self):
        for field, value in [('sequence', 2), ('pid', 456), ('writer_identity_only', True), ('protocol', 2)]:
            with self.subTest(field=field):
                reader, launched = self.fake(f"request[{field!r}]={value!r}\nprint(json.dumps({{'request':request,'files':[]}}), flush=True)\ntime.sleep(10)\n")
                with self.assertRaisesRegex(OSError, 'identity mismatch'):
                    reader(123)
                self.assertIsNone(reader._child)
                with self.assertRaises(OSError):
                    reader(123)
                self.assertEqual(launched.call_count, 1)

    def test_malformed_partial_or_extra_frame_never_returns_inventory(self):
        for payload in [b'{\n', b'{', b'{}\n{}\n']:
            with self.subTest(payload=payload):
                reader, _ = self.fake(f"os.write(1,{payload!r})\ntime.sleep(10)\n", timeout=.15)
                with self.assertRaises((OSError, ValueError)):
                    reader(123)
                self.assertIsNone(reader._child)

    def test_timeout_kills_child_without_retry(self):
        reader, launched = self.fake('time.sleep(10)\n', timeout=.15)
        child = reader._child
        with self.assertRaises(TimeoutError):
            reader(123)
        self.assertIsNotNone(child.poll())
        self.assertIsNone(reader._child)
        self.assertEqual(launched.call_count, 1)

    def test_exit_without_reply_is_terminal(self):
        reader, _ = self.fake('sys.exit(7)\n')
        child = reader._child
        with self.assertRaises(OSError):
            reader(123)
        self.assertIsNotNone(child.poll())

    def test_cancel_inflight_read_and_repeat_close(self):
        reader, _ = self.fake('time.sleep(10)\n')
        entered, errors = threading.Event(), []
        original = reader._send
        def sending(*args):
            original(*args)
            entered.set()
        reader._send = sending
        def observe():
            try:
                reader(123)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=observe)
        thread.start()
        self.assertTrue(entered.wait(1))
        child = reader._child
        reader.close()
        thread.join(1)
        reader.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OSError)
        self.assertIsNotNone(child.poll())

    def test_two_callers_have_independent_ordered_requests(self):
        reader, _ = self.fake("""for _ in range(2):
 print(json.dumps({'request':request,'files':[['/fresh-'+str(request['sequence']),1,request['sequence']]]}), flush=True)
 request=json.loads(sys.stdin.readline())
""")
        results, errors = [], []
        def observe():
            try:
                results.append(reader(123, identities=True))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=observe) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual({next(iter(r)) for r in results}, {Path('/fresh-1'), Path('/fresh-2')})

    def test_invalid_paths_inodes_duplicate_and_result_shape_rejected(self):
        invalid = [[['relative',1,2]], [['/nul\x00',1,2]], [['/a',True,1]],
                   [['/a',1,-1]], [['/a',1,2],['/a',1,3]], {}, [['/a',1]]]
        for files in invalid:
            with self.subTest(files=files):
                reader, _ = self.fake(f"print(json.dumps({{'request':request,'files':{files!r}}}), flush=True)\ntime.sleep(10)\n")
                with self.assertRaises(OSError):
                    reader(123, identities=True)

    def test_late_response_rejected_before_return(self):
        reader, _ = self.fake("print(json.dumps({'request':request,'files':[]}), flush=True)\ntime.sleep(10)\n")
        original = reader._receive
        def delayed(deadline):
            result = original(deadline)
            reader.timeout = .01
            time.sleep(.06)
            return result
        reader.timeout = .05
        reader._receive = delayed
        with self.assertRaises(TimeoutError):
            reader(123)

    def test_native_negative_preserves_type_errno_diagnostics_and_next_fresh_read(self):
        root = str(Path(inventory.__file__).resolve().parent)
        reader, _ = self.fake(f"""sys.path.insert(0, {root!r})
from ccc_standby_inventory import _worker
from ccc_codex_queue import IncompleteVnodeRead, VnodeInventoryChanged
def read(pid, **kwargs):
 if pid == 123: raise IncompleteVnodeRead(9, 'short vnode')
 if pid == 124:
  error = VnodeInventoryChanged('changed')
  error.inventory_change = {{'changed_count':1}}
  raise error
 return {{}}
_worker(read, sys.stdin.buffer, sys.stdout.buffer)
""", handshake=False)
        child = reader._child
        with self.assertRaises(queue.IncompleteVnodeRead) as caught:
            reader(123)
        self.assertEqual(caught.exception.errno, errno.EBADF)
        with self.assertRaises(queue.VnodeInventoryChanged) as caught:
            reader(124)
        self.assertEqual(caught.exception.inventory_change, {'changed_count': 1})
        self.assertEqual(reader(125, identities=True), {})
        self.assertIs(reader._child, child)

    def test_worker_refuses_replayed_sequence_without_read(self):
        request = {'protocol':1, 'sequence':1, 'pid':123, 'writer_identity_only':True}
        line = json.dumps(request).encode() + b'\n'
        reads = []
        def read(*args, **kwargs):
            reads.append((args, kwargs))
            return {}
        with self.assertRaises(OSError):
            inventory._worker(read, io.BytesIO(line + line), io.BytesIO())
        self.assertEqual(len(reads), 1)

    def test_worker_death_before_next_call_cannot_return_last_result(self):
        reader, _ = self.fake("print(json.dumps({'request':request,'files':[['/old',1,2]]}), flush=True)\ntime.sleep(10)\n")
        self.assertIn(Path('/old'), reader(123))
        reader._child.kill(); reader._child.wait(timeout=1)
        with self.assertRaises(OSError):
            reader(123)

    def test_response_integer_and_boolean_types_are_not_interchangeable(self):
        for field, value in [('protocol', True), ('protocol', 1.0),
                             ('sequence', True), ('pid', 123.0),
                             ('writer_identity_only', 0)]:
            with self.subTest(field=field, value=value):
                reader, _ = self.fake(f"request[{field!r}]={value!r}\nprint(json.dumps({{'request':request,'files':[]}}), flush=True)\ntime.sleep(10)\n")
                with self.assertRaisesRegex(OSError, 'identity mismatch'):
                    reader(123)
                self.assertIsNone(reader._child)

    def test_constructor_rejects_mistyped_handshake_and_cleans_child(self):
        for protocol, pid in [('True', 'os.getpid()'), ('1.0', 'os.getpid()'),
                              ('1', 'float(os.getpid())')]:
            with self.subTest(protocol=protocol, pid=pid):
                with self.assertRaisesRegex(OSError, 'handshake'):
                    self.fake(f"print(json.dumps({{'protocol':{protocol},'ready':{pid}}}), flush=True)\ntime.sleep(10)\n", handshake=False)
                child = self.children[-1]
                self.assertIsNotNone(child.poll())
                self.assertTrue(child.stdin.closed and child.stdout.closed)

    def test_constructor_timeout_and_spawn_failure_do_not_leave_child(self):
        with self.assertRaises(TimeoutError):
            self.fake('time.sleep(10)\n', timeout=.15, handshake=False)
        child = self.children[-1]
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed and child.stdout.closed)
        with patch.object(inventory.subprocess, 'Popen', side_effect=OSError('spawn refused')):
            with self.assertRaisesRegex(OSError, 'spawn refused'):
                inventory.ProcessInventoryReader()

    def test_close_reaps_and_closes_pipes_when_terminate_races_exit(self):
        reader, _ = self.fake('time.sleep(10)\n')
        child = reader._child
        terminate = child.terminate
        def raced():
            terminate()
            raise ProcessLookupError('child exited during signal')
        with patch.object(child, 'terminate', side_effect=raced):
            reader.close()
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed and child.stdout.closed)
        self.assertIsNone(reader._child)

    def test_parent_eof_exits_worker_without_read(self):
        reader = inventory.ProcessInventoryReader()
        self.addCleanup(reader.close)
        child = reader._child
        child.stdin.close()
        self.assertEqual(child.wait(timeout=1), 0)
        with self.assertRaises(OSError):
            reader(123)

    def test_reply_size_limit_closes_channel_without_partial_result(self):
        reader, _ = self.fake("print(json.dumps({'request':request,'files':[['/'+('x'*512),1,2]]}), flush=True)\ntime.sleep(10)\n")
        with patch.object(inventory, 'MAX_REPLY', 128):
            with self.assertRaisesRegex(OSError, 'too large'):
                reader(123)
        self.assertIsNone(reader._child)


class InventoryHighFDTests(unittest.TestCase):
    """Real high-numbered worker pipes without opening thousands of files."""
    setUp = InventoryProcessTests.setUp
    fake = InventoryProcessTests.fake

    def worker(self, body, *, handshake=True, timeout=2):
        reader, launch = self.fake(body, handshake=handshake, timeout=timeout, high_fds=True)
        self.assertGreaterEqual(reader._child.stdin.fileno(), 2048)
        self.assertGreaterEqual(reader._child.stdout.fileno(), 2048)
        return reader, launch

    def assert_reaped(self, reader, child):
        self.assertIsNone(reader._child)
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed and child.stdout.closed)

    def block_writes(self, reader):
        total = 0
        while True:
            try:
                total += os.write(reader._child.stdin.fileno(), b'x' * 4096)
            except BlockingIOError:
                break
            self.assertLess(total, 16 * 1024 * 1024, 'pipe never blocked')
        self.assertGreater(total, 0)

    def test_high_fd_handshake_and_fresh_worker_roundtrips(self):
        root = str(Path(inventory.__file__).resolve().parent)
        observed = self.root / 'observed'
        observed.write_text('one')
        reader, launch = self.worker(f"""sys.path.insert(0, {root!r})
from pathlib import Path
from ccc_standby_inventory import _worker
path = Path({str(observed)!r})
def read(pid, **kwargs):
 if not path.exists(): return {{}}
 info = path.stat()
 return {{path: {{'device': info.st_dev, 'inode': info.st_ino}}}}
_worker(read, sys.stdin.buffer, sys.stdout.buffer)
""", handshake=False)
        child = reader._child
        info = observed.stat()
        self.assertEqual(reader(os.getpid(), identities=True),
                         {observed: {'device': info.st_dev, 'inode': info.st_ino}})
        observed.unlink()
        self.assertEqual(reader(os.getpid(), identities=True), {})
        self.assertEqual(reader._sequence, 2)
        self.assertEqual(launch.call_count, 1)
        reader.close()
        self.assert_reaped(reader, child)

    def test_high_fd_read_deadline_closes_channel(self):
        reader, launch = self.worker('time.sleep(10)\n')
        child = reader._child
        reader.timeout = .15
        with self.assertRaisesRegex(TimeoutError, 'deadline elapsed'):
            reader(123)
        self.assert_reaped(reader, child)
        self.assertEqual(launch.call_count, 1)
        with self.assertRaisesRegex(OSError, 'closed'):
            reader(123)

    def test_high_fd_blocked_write_deadline_closes_channel(self):
        reader, launch = self.worker("print(json.dumps({'protocol':1,'ready':os.getpid()}), flush=True)\ntime.sleep(10)\n", handshake=False)
        child = reader._child
        self.block_writes(reader)
        reader.timeout = .15
        with self.assertRaisesRegex(TimeoutError, 'deadline elapsed'):
            reader(123)
        self.assert_reaped(reader, child)
        self.assertEqual(launch.call_count, 1)

    def cancel_wait(self, *, writing):
        reader, launch = self.worker("print(json.dumps({'protocol':1,'ready':os.getpid()}), flush=True)\ntime.sleep(10)\n", handshake=False)
        child = reader._child
        if writing:
            self.block_writes(reader)
        waiting, errors = threading.Event(), []
        original = reader._wait
        def wait(stream, deadline, **kwargs):
            if kwargs.get('writing', False) == writing:
                waiting.set()
            return original(stream, deadline, **kwargs)
        def observe():
            try:
                reader(123)
            except BaseException as exc:
                errors.append(exc)
        reader._wait = wait
        thread = threading.Thread(target=observe)
        thread.start()
        self.addCleanup(thread.join, 2)
        try:
            self.assertTrue(waiting.wait(1))
        finally:
            reader.close()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OSError)
        self.assertIn('closed', str(errors[0]))
        self.assert_reaped(reader, child)
        self.assertEqual(launch.call_count, 1)

    def test_high_fd_read_cancellation_reaps_worker(self):
        self.cancel_wait(writing=False)

    def test_high_fd_write_cancellation_reaps_worker(self):
        self.cancel_wait(writing=True)

    def test_high_fd_startup_deadline_reaps_worker(self):
        with self.assertRaisesRegex(TimeoutError, 'deadline elapsed'):
            self.worker('time.sleep(10)\n', handshake=False, timeout=.3)
        child = self.children[-1]
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed and child.stdout.closed)


if __name__ == '__main__':
    unittest.main()

"""Associate observed credentials with the CLI's actual local backend."""
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ccc_request_key_binding import connected_writer, unix_pairs


RAW = "p12\nf7\ntunix\nd0x123\nn->0xabc\np34\nf9\ntunix\nd0xabc\nn->0x123\n"


class KeyBindingTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin libproc')
    def test_named_accepted_connection_across_processes(self):
        import os
        from pathlib import Path
        with tempfile.TemporaryDirectory(prefix='ccc-key-socket-') as temp:
            path = str(Path(temp) / 'named.sock')
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(path)
            listener.listen(1)
            listener.settimeout(5)
            child = subprocess.Popen([sys.executable, '-B', '-c',
                'import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); print("ready",flush=True); sys.stdin.read()',
                path], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            accepted = None
            try:
                accepted, _ = listener.accept()
                self.assertEqual(child.stdout.readline(), b'ready\n')
                with patch('ccc_guard_scope.birth', return_value=[1, 0]):
                    self.assertTrue(connected_writer(child.pid, os.getpid(), [1, 0], [1, 0]))
                    accepted.close()
                    self.assertFalse(connected_writer(child.pid, os.getpid(), [1, 0], [1, 0]))
            finally:
                if accepted is not None:
                    accepted.close()
                listener.close()
                child.communicate(timeout=5)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin libproc')
    def test_native_peer_disappears_and_partial_read_rejected(self):
        with patch('ccc_guard_scope.birth', return_value=[1, 0]):
            with patch('ccc_request_key_binding.native_unix_pairs', side_effect=[
                    {(1, 2)}, {(2, 1)}, {(1, 3)}, {(2, 1)}]):
                self.assertFalse(connected_writer(12, 34, [1, 0], [1, 0]))
            with patch('ccc_request_key_binding.native_unix_pairs', side_effect=OSError('short')):
                self.assertFalse(connected_writer(12, 34, [1, 0], [1, 0]))

    def test_reciprocal_pair_and_same_process(self):
        with patch("ccc_guard_scope.birth", return_value=[1, 0]):
            self.assertTrue(connected_writer(12, 34, [1, 0], [1, 0],
                runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=RAW)))
            self.assertTrue(connected_writer(12, 12, [1, 0], [1, 0]))

    def test_wrong_peer_not_just_shared_socket_name(self):
        for text in (RAW.replace("n->0x123", "n->0x456"),
                     RAW.replace("n->0xabc", "n/private/tmp/shared.sock"),
                     RAW.replace("tunix", "tREG"), RAW.replace("p34", "p56")):
            with self.subTest(text=text), patch("ccc_guard_scope.birth", return_value=[1, 0]):
                self.assertFalse(connected_writer(12, 34, [1, 0], [1, 0],
                    runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=text)))

    def test_process_replaced_during_socket_read(self):
        with patch("ccc_guard_scope.birth", side_effect=[[1, 0], [1, 0], [2, 0]]):
            self.assertFalse(connected_writer(12, 34, [1, 0], [1, 0],
                runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=RAW)))

    def test_lsof_failure_and_zero_endpoints(self):
        self.assertEqual(unix_pairs(RAW.replace("0x123", "0x0")), {})
        with patch("ccc_guard_scope.birth", return_value=[1, 0]):
            self.assertFalse(connected_writer(12, 34, [1, 0], [1, 0],
                runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout=RAW)))

    @unittest.skipUnless(sys.platform == "darwin", "Darwin lsof socket addresses")
    def test_real_private_socket_pair_across_processes(self):
        # Real kernel endpoints, no Codex session, auth, or network request.
        import os
        local, remote = socket.socketpair()
        child = subprocess.Popen([sys.executable, "-B", "-c",
            "import sys; print('ready',flush=True); sys.stdin.read()"],
            pass_fds=(remote.fileno(),), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        remote.close()
        try:
            self.assertEqual(child.stdout.readline(), b"ready\n")
            with patch("ccc_guard_scope.birth", return_value=[1, 0]):
                self.assertTrue(connected_writer(os.getpid(), child.pid, [1, 0], [1, 0]))
                local.close()
                self.assertFalse(connected_writer(os.getpid(), child.pid, [1, 0], [1, 0]))
        finally:
            local.close()
            child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()

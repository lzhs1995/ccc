"""有界证据 I/O 的独立行为回归；所有输入均为真实临时文件。"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import cmux_evidence_io as evidence


class _ReadBoundary:
    """保留真实文件描述符，仅在 read 边界注入确定性的文件变化。"""

    def __init__(self, stream, before_read=None, after_read=None, sizes=None):
        self.stream = stream
        self.before_read = before_read
        self.after_read = after_read
        self.sizes = sizes

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def fileno(self):
        return self.stream.fileno()

    def read(self, size=-1):
        if self.sizes is not None:
            self.sizes.append(size)
        if self.before_read:
            self.before_read()
        raw = self.stream.read(size)
        if self.after_read:
            self.after_read()
        return raw


class EvidenceIOTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evidence-io-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "evidence.bin"
        self.path.write_bytes(b"original evidence\n")

    @contextmanager
    def boundary(self, *, before_read=None, after_read=None, sizes=None):
        real_fdopen = os.fdopen

        def wrapped(*args, **kwargs):
            return _ReadBoundary(real_fdopen(*args, **kwargs),
                                 before_read, after_read, sizes)

        with patch.object(evidence.os, "fdopen", side_effect=wrapped):
            yield

    def assert_special_file_rejected_promptly(self, path):
        # 直接约束读取进程；若 O_NONBLOCK 回归，超时会杀死该进程，测试不会挂住。
        code = (
            "import sys\n"
            "import cmux_evidence_io as evidence\n"
            "try:\n"
            "    evidence.snapshot(sys.argv[1])\n"
            "except (OSError, ValueError):\n"
            "    raise SystemExit(0)\n"
            "raise SystemExit('special file was accepted')\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code, str(path)],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_snapshot_returns_exact_bytes_and_file_identity(self):
        before = self.path.stat()
        raw, identity = evidence.snapshot(self.path)
        self.assertEqual(raw, b"original evidence\n")
        self.assertEqual(identity, (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        ))

    def test_empty_and_binary_regular_files_are_preserved(self):
        for raw in (b"", b"\x00\xff\r\nbinary\x00"):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.assertEqual(evidence.read_bytes(self.path), raw)

    def test_accepts_exactly_eight_mib(self):
        raw = b"x" * (8 * 1024 * 1024)
        self.path.write_bytes(raw)
        self.assertEqual(evidence.read_bytes(self.path), raw)

    def test_rejects_over_eight_mib_before_reading(self):
        with self.path.open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
        sizes = []
        with self.boundary(sizes=sizes), self.assertRaises(ValueError):
            evidence.read_bytes(self.path)
        self.assertEqual(sizes, [])

    def test_custom_and_zero_limits_are_enforced(self):
        self.path.write_bytes(b"abcd")
        self.assertEqual(evidence.read_bytes(self.path, limit=4), b"abcd")
        with self.assertRaises(ValueError):
            evidence.read_bytes(self.path, limit=3)
        self.path.write_bytes(b"")
        self.assertEqual(evidence.read_bytes(self.path, limit=0), b"")

    def test_relative_path_is_rejected_before_open(self):
        real_open = os.open
        with patch.object(evidence.os, "open", wraps=real_open) as opened:
            with self.assertRaises(ValueError):
                evidence.snapshot(Path("evidence.bin"))
        opened.assert_not_called()

    def test_symlink_to_regular_file_is_rejected(self):
        link = self.root / "linked.bin"
        link.symlink_to(self.path)
        with self.assertRaises((OSError, ValueError)):
            evidence.snapshot(link)
        self.assertEqual(self.path.read_bytes(), b"original evidence\n")

    def test_directory_missing_path_and_socket_are_rejected(self):
        for path in (self.root, self.root / "missing.bin"):
            with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                evidence.snapshot(path)
        endpoint = self.root / "socket"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(endpoint))
            self.assert_special_file_rejected_promptly(endpoint)

    def test_fifo_is_rejected_without_a_writer(self):
        fifo = self.root / "unopened.fifo"
        os.mkfifo(fifo)
        self.assert_special_file_rejected_promptly(fifo)

    def test_same_bytes_inode_replacement_after_read_is_rejected(self):
        replacement = self.root / "replacement.bin"
        replacement.write_bytes(self.path.read_bytes())
        with self.boundary(after_read=lambda: os.replace(replacement, self.path)):
            with self.assertRaises(ValueError):
                evidence.snapshot(self.path)

    def test_append_after_read_is_rejected(self):
        def append():
            with self.path.open("ab") as stream:
                stream.write(b"new bytes")
        with self.boundary(after_read=append), self.assertRaises(ValueError):
            evidence.snapshot(self.path)

    def test_same_size_rewrite_after_read_is_rejected(self):
        before = self.path.stat()

        def rewrite():
            self.path.write_bytes(b"x" * before.st_size)
            os.utime(self.path, ns=(before.st_atime_ns,
                                   before.st_mtime_ns + 1_000_000_000))

        with self.boundary(after_read=rewrite), self.assertRaises(ValueError):
            evidence.snapshot(self.path)

    def test_truncate_before_read_is_rejected(self):
        with self.boundary(before_read=lambda: self.path.write_bytes(b"")):
            with self.assertRaises(ValueError):
                evidence.snapshot(self.path)

    def test_growth_after_stat_still_reads_only_limit_plus_one(self):
        self.path.write_bytes(b"a" * 16)
        sizes = []
        with self.boundary(
            before_read=lambda: self.path.write_bytes(b"b" * 64), sizes=sizes,
        ), self.assertRaises(ValueError):
            evidence.snapshot(self.path, limit=32)
        self.assertEqual(sizes, [33])

    def test_symlink_swap_after_read_is_rejected(self):
        destination = self.root / "destination.bin"
        destination.write_bytes(self.path.read_bytes())

        def swap():
            self.path.unlink()
            self.path.symlink_to(destination)

        with self.boundary(after_read=swap), self.assertRaises(ValueError):
            evidence.snapshot(self.path)

    def test_unlink_after_read_is_rejected(self):
        with self.boundary(after_read=self.path.unlink):
            with self.assertRaises((OSError, ValueError)):
                evidence.snapshot(self.path)

    def test_attempt_paths_are_sorted_local_and_ignore_other_names(self):
        journal = self.root / "attempts"
        journal.mkdir()
        expected = [journal / "attempt-0001.json", journal / "attempt-0002.json"]
        for path in reversed(expected):
            path.write_text("{}")
        (journal / "unrelated.json").write_text("{}")
        (journal / "attempt-0003.txt").write_text("{}")
        nested = journal / "nested"
        nested.mkdir()
        (nested / "attempt-9999.json").write_text("{}")
        self.assertEqual(evidence.attempt_paths(journal), expected)

    def test_attempt_paths_accept_128_and_reject_129(self):
        journal = self.root / "bounded-attempts"
        journal.mkdir()
        expected = []
        for number in range(1, 129):
            path = journal / f"attempt-{number:04d}.json"
            path.write_text("{}")
            expected.append(path)
        self.assertEqual(evidence.attempt_paths(journal), expected)
        (journal / "attempt-0129.json").write_text("{}")
        with self.assertRaises(ValueError):
            evidence.attempt_paths(journal)


class RegularLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='evidence-lock-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / 'delivery.lock'

    def test_creates_regular_lock_and_preserves_existing_bytes(self):
        with evidence.open_regular_lock(self.path) as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.path.write_bytes(b'preserve lock metadata')
        with evidence.open_regular_lock(self.path) as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.path.read_bytes(), b'preserve lock metadata')

    def test_relative_path_is_rejected_before_open(self):
        with patch.object(evidence.os, 'open', wraps=os.open) as opened:
            with self.assertRaises(ValueError):
                evidence.open_regular_lock('delivery.lock')
        opened.assert_not_called()

    def test_symlink_and_hardlink_rejected_without_changing_target(self):
        target = self.root / 'target'
        target.write_bytes(b'preserved')
        self.path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            evidence.open_regular_lock(self.path)
        self.path.unlink()
        os.link(target, self.path)
        with self.assertRaises(ValueError):
            evidence.open_regular_lock(self.path)
        self.assertEqual(target.read_bytes(), b'preserved')

    def test_fifo_directory_and_socket_refuse_promptly(self):
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        endpoint = self.root / 'socket'
        code = ('import sys\nimport cmux_evidence_io as e\n'
                'try:\n    e.open_regular_lock(sys.argv[1])\n'
                'except (OSError, ValueError):\n    raise SystemExit(0)\n'
                'raise SystemExit("special lock was accepted")\n')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(endpoint))
            for path in (fifo, self.root, endpoint):
                with self.subTest(path=path):
                    result = subprocess.run([sys.executable, '-B', '-c', code, str(path)],
                        cwd=str(Path(__file__).resolve().parent), capture_output=True,
                        text=True, timeout=3)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_replaced_lock_is_rejected_and_descriptor_closed(self):
        self.path.write_bytes(b'original')
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'replacement')
        real_fstat = os.fstat
        descriptors = []
        def swap(fd):
            descriptors.append(fd)
            value = real_fstat(fd)
            os.replace(replacement, self.path)
            return value
        with patch.object(evidence.os, 'fstat', side_effect=swap):
            with self.assertRaises(ValueError):
                evidence.open_regular_lock(self.path)
        with self.assertRaises(OSError):
            real_fstat(descriptors[0])
        self.assertEqual(self.path.read_bytes(), b'replacement')

    def test_competing_lock_remains_nonblocking(self):
        with evidence.open_regular_lock(self.path) as first:
            fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with evidence.open_regular_lock(self.path) as second:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main()

"""Exercise snapshot data integrity and durability failures through real files."""
import errno
import json
import os
from pathlib import Path
import random
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import ccc_state_snapshot as snapshot


def copied_clone(source, directory, name):
    """Portable clone stand-in; native COW is tested separately on macOS."""
    target = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    try:
        offset = 0
        while data := os.pread(source, 65536, offset):
            snapshot._write_at(target, data, offset)
            offset += len(data)
    finally:
        os.close(target)
    return True


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.json"
        for patcher in (mock.patch.object(snapshot, "_clone_function", return_value=object()),
                        mock.patch.object(snapshot, "_clone", side_effect=copied_clone)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.writer = snapshot.AtomicJSONSnapshot(self.path)
        self.original = {"archived": {"delivery_status": "unknown", "attempt": "original"},
                         "live": {"at": 1, "text": "中文\n🧭"}}

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_history_unicode_append_shrink_grow_remove_and_restart(self):
        value = json.loads(json.dumps(self.original))
        self.assertEqual(self.writer.write(value)["mode"], "full")
        value["live"]["text"] = "短"
        self.assertEqual(self.writer.write(value)["mode"], "clone")
        value["a new \"key\"\n"] = {"false": False, "zero": 0, "list": [None, {}]}
        self.assertEqual(self.writer.write(value)["mode"], "clone")
        self.assertEqual(self.read(), value)
        value["live"]["text"] = "大" * 5000
        self.assertEqual(self.writer.write(value)["mode"], "full")
        del value["a new \"key\"\n"]
        self.assertEqual(self.writer.write(value)["mode"], "full")
        self.assertEqual(self.read(), value)
        self.assertEqual(snapshot.AtomicJSONSnapshot(self.path).write(value)["mode"], "full")
        self.assertEqual(self.read(), value)
        self.assertEqual(self.read()["archived"], self.original["archived"])

    def test_empty_transitions_and_exact_unchanged_result(self):
        for value in ({}, self.original, {}, {}):
            self.writer.write(value)
            self.assertEqual(self.read(), value)
        with mock.patch.object(snapshot.os, "open", side_effect=AssertionError("unexpected write")):
            self.assertEqual(self.writer.write({})["written_bytes"], 0)

    def test_published_inode_and_open_reader_are_never_edited(self):
        self.writer.write(self.original)
        with self.path.open("rb") as reader:
            before = reader.read()
            old_inode = os.fstat(reader.fileno()).st_ino
            self.writer.write({**self.original, "live": {"at": 2}})
            reader.seek(0)
            self.assertEqual(reader.read(), before)
            self.assertNotEqual(self.path.stat().st_ino, old_inode)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_small_change_has_bounded_logical_writes(self):
        value = {str(i): {"text": "x" * 1500} for i in range(100)}
        full = self.writer.write(value)
        value["45"] = {"text": "changed"}
        changed = self.writer.write(value)
        self.assertEqual(changed["changed_records"], 1)
        self.assertLess(changed["written_bytes"], full["written_bytes"] / 20)
        self.assertEqual(self.read(), value)

    def test_foreign_replacement_and_deletion_invalidate_cached_snapshot(self):
        self.writer.write(self.original)
        foreign = self.path.with_suffix(".foreign")
        foreign.write_text('{"foreign":true}')
        os.replace(foreign, self.path)
        self.assertEqual(self.writer.write(self.original)["mode"], "full")
        self.assertEqual(self.read(), self.original)
        self.path.unlink()
        self.writer.write(self.original)
        self.assertEqual(self.read(), self.original)

    def test_in_place_external_edit_is_not_accepted_as_unchanged(self):
        self.writer.write(self.original)
        before = self.path.stat()
        with self.path.open("r+b") as stream:
            stream.write(b"[")
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.writer.write(self.original)["mode"], "full")
        self.assertEqual(self.read(), self.original)

    def test_unsupported_clone_falls_back_to_complete_compact_snapshot(self):
        self.writer.write(self.original)
        changed = {**self.original, "live": {"at": 2}}
        with mock.patch.object(snapshot, "_clone", return_value=False):
            self.assertEqual(self.writer.write(changed)["mode"], "compact")
        self.assertLess(self.path.stat().st_size, snapshot.BLOCK_SIZE)
        self.assertEqual(self.read(), changed)
        self.assertEqual(self.writer.write(self.original)["mode"], "compact")

    def test_no_clone_platform_uses_compact_from_first_write(self):
        with mock.patch.object(snapshot, "_clone_function", return_value=None):
            writer = snapshot.AtomicJSONSnapshot(self.path)
        self.assertEqual(writer.write(self.original)["mode"], "compact")
        self.assertEqual(self.read(), self.original)

    def test_short_writes_finish_without_truncating_data(self):
        actual = os.pwrite
        with mock.patch.object(snapshot.os, "pwrite", side_effect=lambda fd, data, offset: actual(fd, data[:37], offset)):
            self.writer.write(self.original)
        self.assertEqual(self.read(), self.original)

    def test_no_progress_write_preserves_old_snapshot_and_can_retry(self):
        self.writer.write(self.original)
        changed = {**self.original, "live": {"at": 2}}
        with mock.patch.object(snapshot.os, "pwrite", return_value=0):
            with self.assertRaises(OSError):
                self.writer.write(changed)
        self.assertEqual(self.read(), self.original)
        self.assertEqual(list(self.path.parent.glob(".*.tmp")), [])
        self.assertNotEqual(self.writer.write(changed)["mode"], "unchanged")
        self.assertEqual(self.read(), changed)

    def test_clone_error_is_not_hidden_or_published(self):
        self.writer.write(self.original)
        with mock.patch.object(snapshot, "_clone", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                self.writer.write({**self.original, "new": {}})
        self.assertEqual(self.read(), self.original)
        self.assertIsNone(self.writer._identity)

    def test_source_changed_during_clone_is_not_published(self):
        self.writer.write(self.original)
        def changed_source(*args):
            result = copied_clone(*args)
            self.path.write_text('{"foreign":true}')
            return result
        with mock.patch.object(snapshot, "_clone", side_effect=changed_source):
            with self.assertRaisesRegex(RuntimeError, "source changed"):
                self.writer.write({**self.original, "new": {}})
        self.assertEqual(self.read(), {"foreign": True})

    def test_file_and_directory_fsync_failures_require_fresh_durable_retry(self):
        for failure_call in (1, 2):
            with self.subTest(failure_call=failure_call):
                self.writer.write(self.original)
                changed = {**self.original, "live": {"at": 3}}
                actual, calls = os.fsync, []
                def fail_once(fd):
                    calls.append(fd)
                    if len(calls) == failure_call:
                        raise OSError(errno.EIO, "injected sync failure")
                    return actual(fd)
                with mock.patch.object(snapshot.os, "fsync", side_effect=fail_once):
                    with self.assertRaises(OSError):
                        self.writer.write(changed)
                self.assertEqual(self.read(), self.original if failure_call == 1 else changed)
                with mock.patch.object(snapshot.os, "fsync", wraps=actual) as retried:
                    self.assertNotEqual(self.writer.write(changed)["mode"], "unchanged")
                self.assertEqual(retried.call_count, 2)
                self.assertEqual(self.read(), changed)

    def test_replace_that_succeeds_then_raises_is_never_acknowledged(self):
        self.writer.write(self.original)
        changed = {**self.original, "live": {"at": 4}}
        actual = os.replace
        def replace_then_raise(*args, **kwargs):
            actual(*args, **kwargs)
            raise OSError(errno.EIO, "injected return failure")
        with mock.patch.object(snapshot.os, "replace", side_effect=replace_then_raise):
            with self.assertRaises(OSError):
                self.writer.write(changed)
        self.assertEqual(self.read(), changed)
        self.assertIsNone(self.writer._identity)
        with mock.patch.object(snapshot.os, "fsync", wraps=os.fsync) as synced:
            self.writer.write(changed)
        self.assertEqual(synced.call_count, 2)

    def test_random_record_transitions_remain_ordinary_complete_json(self):
        randomizer, value = random.Random(47278213), {}
        for i in range(100):
            key = randomizer.choice(["a", "b", "c", "d", "原记录"])
            if randomizer.randrange(5) == 0:
                value.pop(key, None)
            else:
                value[key] = {"value": "x" * randomizer.choice([0, 10, 6000]), "at": i}
            self.writer.write(value)
            self.assertEqual(self.read(), value)


class ProcessAndNativeSnapshotTests(unittest.TestCase):
    def test_abrupt_exit_at_publish_boundaries_leaves_whole_old_or_new_json(self):
        program = '''
import os, signal, sys
from pathlib import Path
import ccc_state_snapshot as s
p, phase = Path(sys.argv[1]), sys.argv[2]
w = s.AtomicJSONSnapshot(p)
w.write({"old": {"unknown": "keep"}})
original = os.replace
def stop_at_replace(*args, **kwargs):
    if phase == "after":
        original(*args, **kwargs)
    os.kill(os.getpid(), signal.SIGKILL)
s.os.replace = stop_at_replace
w.write({"new": {"attempt": "retained"}})
'''
        with tempfile.TemporaryDirectory() as directory:
            for phase in ("before", "after"):
                path = Path(directory) / (phase + ".json")
                result = subprocess.run([sys.executable, "-B", "-c", program, str(path), phase],
                                        cwd=Path(snapshot.__file__).parent, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, -9, result.stderr.decode())
                expected = {"old": {"unknown": "keep"}} if phase == "before" else {"new": {"attempt": "retained"}}
                self.assertEqual(json.loads(path.read_text()), expected)
                snapshot.AtomicJSONSnapshot(path).write(expected)
                self.assertEqual(json.loads(path.read_text()), expected)

    @unittest.skipUnless(sys.platform == "darwin", "native macOS clone integration")
    def test_native_clone_and_durable_directory_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            writer = snapshot.AtomicJSONSnapshot(path)
            original = {"old": {"unknown": "retain"}, "live": {"at": 1}}
            writer.write(original)
            with path.open("rb") as reader:
                before = reader.read()
                changed = {**original, "live": {"at": 2}}
                result = writer.write(changed)
                self.assertIn(result["mode"], {"clone", "compact"})
                reader.seek(0)
                self.assertEqual(reader.read(), before)
            self.assertEqual(json.loads(path.read_text()), changed)


if __name__ == "__main__":
    unittest.main()

"""Real private evidence, event-gated I/O, no native processes or input."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import ccc_standby_acceptance as acceptance
from tests import test_standby_acceptance as fixtures


class TrackingLock:
    """Record whether an actual contender has to wait, without elapsed assertions."""
    def __init__(self):
        self.lock = threading.RLock()
        self.attempted = threading.Event()
        self.contender_blocked = None

    def __enter__(self):
        acquired = self.lock.acquire(blocking=False)
        if threading.current_thread().name == 'contender' and self.contender_blocked is None:
            self.contender_blocked = not acquired
            self.attempted.set()
        if not acquired:
            self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


class EvidenceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ccc-evidence662-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.slow, self.fast = [self.directory / name for name in ('slow.json', 'fast.json')]
        for path in (self.slow, self.fast):
            path.write_text(json.dumps({'name': path.name}))
            path.chmod(0o600)
        self.observer = acceptance.FirstTaskObserver.__new__(acceptance.FirstTaskObserver)
        self.observer._files = {}
        self.observer._validated_files = set()
        self.observer._evidence_lock = TrackingLock()
        self.observer.selected = {'boot_id': acceptance.boot_id()}
        self.observer.directory = self.directory
        self.observer._directory = self.observer._dir_identity(self.directory)
        self.observer._read(self.slow)
        self.observer._read(self.fast)

    def overlap(self, operation):
        entered, release = threading.Event(), threading.Event()
        original = Path.read_bytes
        errors = []
        def blocked_read(path):
            if path == self.slow and threading.current_thread().name == 'slow':
                entered.set()
                if not release.wait(5):
                    raise AssertionError('test driver did not release gated read')
            return original(path)
        def invoke(callback):
            try:
                callback()
            except BaseException as exc:
                errors.append(exc)
        slow = threading.Thread(name='slow', target=invoke,
            args=(lambda: self.observer._read(self.slow),))
        other = threading.Thread(name='contender', target=invoke, args=(operation,))
        with patch.object(Path, 'read_bytes', blocked_read):
            slow.start()
            try:
                self.assertTrue(entered.wait(5), 'slow read never reached injection')
                other.start()
                self.assertTrue(self.observer._evidence_lock.attempted.wait(5),
                    'contender never reached registry lock')
                blocked = self.observer._evidence_lock.contender_blocked
            finally:
                release.set()
                slow.join(5)
                if other.ident is not None:
                    other.join(5)
        self.assertFalse(slow.is_alive() or other.is_alive(), 'fixture thread leaked')
        self.assertEqual(errors, [])
        return blocked

    def test_unrelated_evidence_read_does_not_wait_on_slow_reader(self):
        self.assertFalse(self.overlap(lambda: self.observer._read(self.fast)))

    def test_current_scan_does_not_wait_on_another_readers_io(self):
        self.assertFalse(self.overlap(self.observer._current))

    def test_same_generation_still_compares_bytes(self):
        generation = acceptance.batch._file_generation(self.fast)
        self.fast.write_text('{"name": "forged!!"}')
        with patch.object(acceptance.batch, '_file_generation', return_value=generation):
            with self.assertRaisesRegex(ValueError, 'original evidence changed'):
                self.observer._read(self.fast)

    def test_global_scan_rejects_another_slots_changed_evidence(self):
        self.slow.write_text('{"foreign": true}')
        with self.assertRaises(ValueError):
            self.observer._current()

    def test_global_scan_includes_files_registered_during_io(self):
        late = self.directory / 'late.json'
        late.write_text('{}')
        late.chmod(0o600)
        original = Path.read_bytes
        injected = []
        def read(path):
            raw = original(path)
            if path == self.fast and not injected:
                injected.append(True)
                self.observer._read(late)
                late.write_text('{"changed": true}')
            return raw
        with patch.object(Path, 'read_bytes', read):
            with self.assertRaisesRegex(ValueError, 'original evidence changed'):
                self.observer._current()
        self.assertEqual(len(injected), 1)

    def test_invalidation_during_scan_rejected(self):
        original = Path.read_bytes
        def read(path):
            raw = original(path)
            if path == self.fast:
                (self.directory / 'invalidated.json').write_text('{}')
            return raw
        with patch.object(Path, 'read_bytes', read):
            with self.assertRaisesRegex(ValueError, 'activation invalidated'):
                self.observer._current()

    def test_same_bytes_replacement_and_permission_change_rejected(self):
        for mode in ('inode', 'symlink', 'chmod'):
            with self.subTest(mode=mode):
                path = self.directory / (mode + '.json')
                path.write_text('{}')
                path.chmod(0o600)
                self.observer._read(path)
                if mode == 'chmod':
                    path.chmod(0o644)
                else:
                    saved = path.with_suffix('.original')
                    path.rename(saved)
                    if mode == 'symlink':
                        path.symlink_to(saved)
                    else:
                        path.write_text('{}')
                        path.chmod(0o600)
                with self.assertRaises(ValueError):
                    self.observer._read(path)


class ReleaseConcurrencyTests(unittest.TestCase):
    setUp = fixtures.FirstTaskTests.setUp
    start_native = fixtures.FirstTaskTests.start_native
    hook_fixture = fixtures.FirstTaskTests.hook_fixture
    bind = fixtures.FirstTaskTests.bind
    task = fixtures.FirstTaskTests.task
    hold = fixtures.FirstTaskTests.hold

    def test_release_does_not_reintroduce_global_io_lock(self):
        self.task()
        self.observer._evidence_lock = TrackingLock()
        original = self.observer.store.mutate
        threads, errors = [], []
        def read():
            try:
                self.observer._read(self.observer.path)
            except BaseException as exc:
                errors.append(exc)
        def mutate(callback):
            other = threading.Thread(name='contender', target=read)
            threads.append(other)
            other.start()
            self.assertTrue(self.observer._evidence_lock.attempted.wait(5))
            return original(callback)
        with patch.object(self.observer.store, 'mutate', side_effect=mutate):
            result = self.observer.poll(0)
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertFalse(self.observer._evidence_lock.contender_blocked)
        self.assertTrue(result['confirmation']['confirmed'])
        self.assertFalse(self.hold())
        self.assertFalse(self.client.sent)

    def test_late_foreign_evidence_change_prevents_hold_release(self):
        self.task()
        other = self.observer.directory / 'other-slot.json'
        other.write_text('{}')
        other.chmod(0o600)
        self.observer._read(other)
        original = self.observer.store.mutate
        def mutate(callback):
            other.write_text('{"changed":true}')
            return original(callback)
        with patch.object(self.observer.store, 'mutate', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'original evidence changed'):
                self.observer.poll(0)
        self.assertTrue(self.hold())
        self.assertFalse(self.client.sent)


if __name__ == '__main__':
    unittest.main()

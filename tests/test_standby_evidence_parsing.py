"""Full evidence reads remain mandatory; repeated JSON decoding does not."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import ccc_standby_acceptance as acceptance


class EvidenceParsingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ccc-evidence671-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.observer = acceptance.FirstTaskObserver.__new__(acceptance.FirstTaskObserver)
        self.observer._files = {}
        self.observer._validated_files = set()
        self.observer._evidence_lock = threading.RLock()
        self.observer.directory = self.directory
        self.observer._directory = self.observer._dir_identity(self.directory)
        self.observer.selected = {'boot_id': acceptance.boot_id()}
        self.paths = [self.write(name, '{"items": [1, 2]}') for name in ('a.json', 'b.json')]
        for path in self.paths:
            self.observer._read(path)

    def write(self, name, text):
        path = self.directory / name
        path.write_text(text)
        path.chmod(0o600)
        return path

    def test_repeated_scans_read_every_file_without_reparsing(self):
        original, reads = Path.read_bytes, []
        def read(path):
            raw = original(path)
            reads.append((path, raw))
            return raw
        with patch.object(Path, 'read_bytes', read), patch.object(
                acceptance.json, 'loads', wraps=json.loads) as loads:
            for _ in range(3):
                self.observer._current()
        self.assertEqual(reads, [(p, b'{"items": [1, 2]}') for _ in range(3) for p in self.paths])
        self.assertEqual(loads.call_count, 0)

    def test_failed_initial_json_read_is_never_treated_as_valid(self):
        bad = self.write('bad.json', '{')
        with self.assertRaises(json.JSONDecodeError):
            self.observer._read(bad)
        self.assertIn(bad, self.observer._files)
        with self.assertRaises(json.JSONDecodeError):
            self.observer._current()

    def test_current_rejects_changed_bytes_even_with_unchanged_generation(self):
        path = self.paths[0]
        generation = acceptance.batch._file_generation
        before = generation(path)
        path.write_text('{"items": [3, 4]}')
        with patch.object(acceptance.batch, '_file_generation',
                side_effect=lambda p: before if p == path else generation(p)):
            with self.assertRaisesRegex(ValueError, 'original evidence changed'):
                self.observer._current()

    def test_data_reads_return_independent_values(self):
        first = self.observer._read(self.paths[0])
        first['items'].append('local mutation')
        self.observer._current()
        self.assertEqual(self.observer._read(self.paths[0]), {'items': [1, 2]})

    def test_pending_unsuccessful_parse_cannot_certify_concurrent_scan(self):
        bad = self.write('pending.json', '{')
        entered, release, errors = threading.Event(), threading.Event(), []
        original = json.loads
        def loads(raw, *args, **kwargs):
            if raw == b'{' and threading.current_thread().name == 'decoder':
                entered.set()
                if not release.wait(5):
                    raise AssertionError('decoder not released by test driver')
            return original(raw, *args, **kwargs)
        def decode():
            try:
                self.observer._read(bad)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(name='decoder', target=decode)
        with patch.object(acceptance.json, 'loads', loads):
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(json.JSONDecodeError):
                    self.observer._current()
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], json.JSONDecodeError)

    def test_validated_file_still_requires_successful_new_read(self):
        path = self.paths[0]
        original = Path.read_bytes
        hits = []
        def read(p):
            if p == path:
                hits.append(p)
                raise PermissionError('read denied after initial parse')
            return original(p)
        with patch.object(Path, 'read_bytes', read):
            with self.assertRaisesRegex(PermissionError, 'read denied'):
                self.observer._current()
        self.assertEqual(hits, [path])


if __name__ == '__main__':
    unittest.main()

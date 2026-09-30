"""Historical B jobs must not multiply native-index parsing or retain stale grants."""
import concurrent.futures
import gc
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import weakref

import ccc_codex_queue as native


class NativeBindingCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'bindings.json'
        self.record = {'surfaceId': 'surface', 'workspaceId': 'workspace',
                       'pid': 123, 'pidStartSeconds': 100,
                       'transcriptPath': str(self.root / 'original.jsonl')}
        self.write({'original': self.record})

    def write(self, records):
        self.path.write_text(json.dumps({'sessions': records}))

    def queue(self, index=0):
        return native.QueueRecovery(self.root / f'ledger-{index}', self.path, self.root, 'continue')

    def test_many_historical_recoverers_parse_one_unchanged_generation(self):
        queues = [self.queue(i) for i in range(40)]
        with patch.object(native.json, 'loads', wraps=json.loads) as decode:
            for _ in range(3):
                for queue in queues:
                    self.assertEqual(queue.records()['original'], self.record)
            self.assertEqual(decode.call_count, 1)
        self.assertIs(queues[0].records(), queues[-1].records())

    def test_concurrent_readers_coalesce_without_an_extra_file_parse(self):
        queues = [self.queue(i) for i in range(12)]
        with patch.object(native.json, 'loads', wraps=json.loads) as decode, \
                concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda queue: queue.records(), queues))
            self.assertEqual(decode.call_count, 1)
        self.assertTrue(all(result is results[0] for result in results))

    def test_same_size_same_mtime_replacement_invalidates_every_reader(self):
        queues = [self.queue(i) for i in range(2)]
        for queue in queues:
            queue.records()
        before = self.path.stat()
        replacement = self.root / 'replacement'
        replacement.write_text(self.path.read_text().replace('"pid": 123', '"pid": 456'))
        self.assertEqual(before.st_size, replacement.stat().st_size)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(self.path)
        with patch.object(native.json, 'loads', wraps=json.loads) as decode:
            self.assertEqual([queue.records()['original']['pid'] for queue in queues], [456, 456])
            self.assertEqual(decode.call_count, 1)

    def test_in_place_change_with_restored_mtime_is_not_cached(self):
        queue = self.queue()
        queue.records()
        before = self.path.stat()
        self.write({'original': {**self.record, 'pid': 456}})
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(queue.records()['original']['pid'], 456)

    def test_deleted_or_corrupt_bindings_never_fall_back_to_cached_records(self):
        queue = self.queue()
        queue.records()
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            queue.records()
        for value in ('{broken', '[]', '{"sessions": []}', '{"sessions": {"bad": []}}'):
            with self.subTest(value=value):
                self.path.write_text(value)
                with self.assertRaises(ValueError):
                    queue.records()
        self.write({})
        self.assertEqual(queue.records(), {})

    def test_replacement_during_decode_cannot_publish_old_generation(self):
        queue = self.queue()
        decode = json.loads
        def replace(data):
            value = decode(data)
            replacement = self.root / 'replacement'
            replacement.write_text('{"sessions": {}}')
            replacement.replace(self.path)
            return value
        with patch.object(native.json, 'loads', side_effect=replace):
            with self.assertRaises(OSError):
                queue.records()
        self.assertEqual(queue.records(), {})

    def test_shared_file_is_released_after_its_last_queue(self):
        queue = self.queue()
        queue.records()
        reference = weakref.ref(queue._binding_file)
        del queue
        gc.collect()
        self.assertIsNone(reference())

    def test_index_keeps_workspace_separation_and_duplicate_session_veto(self):
        self.write({'original': self.record,
                    'another': {**self.record, 'pid': 456},
                    'foreign': {**self.record, 'workspaceId': 'other'}})
        queue = self.queue()
        target = {'surface_id': 'surface', 'workspace_id': 'workspace'}
        with patch.object(native, 'process_matches', return_value=True) as matches, \
                patch.object(native, 'task_snapshot', return_value={'kind': 'task_complete'}):
            self.assertEqual(queue.current_turn(target), {'kind': 'unknown'})
        self.assertEqual(matches.call_count, 2)
        self.assertTrue(all(call.args[0]['workspaceId'] == 'workspace' for call in matches.call_args_list))

    def test_cached_binding_never_caches_live_process_authorization(self):
        queue = self.queue()
        target = {'surface_id': 'surface', 'workspace_id': 'workspace'}
        queue.process_lookup = lambda _: {'agent_kind': 'shell'}
        with patch.object(native, 'process_matches', side_effect=[True, False]) as matches, \
                patch.object(native, 'task_snapshot', return_value={'kind': 'task_complete'}):
            self.assertEqual(queue.current_turn(target)['session_id'], 'original')
            self.assertEqual(queue.current_turn(target), {'kind': 'unknown'})
        self.assertEqual(matches.call_count, 2)


if __name__ == '__main__':
    unittest.main()

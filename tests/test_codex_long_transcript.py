"""Long active output must not revive an older terminal failure."""
import json
from pathlib import Path
import tempfile
import unittest

from ccc_codex_queue import task_snapshot, _task_snapshots


class LongTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'original.jsonl'
        self.path.write_text(json.dumps({'type': 'session_meta',
                                        'payload': {'id': 'original'}}) + '\n')
        self.addCleanup(_task_snapshots.clear)

    def event(self, kind, turn, error=None):
        with self.path.open('a') as stream:
            stream.write(json.dumps({'type': 'event_msg',
                                    'timestamp': '2026-10-01T13:12:51Z',
                                    'payload': {'type': kind, 'turn_id': turn,
                                                'error': error}}) + '\n')

    def long_running_output(self):
        self.event('task_complete', 'old', {'message': 'rate limit exceeded'})
        self.event('task_started', 'active')
        # Same scale as incident2915: lifecycle is over 17MiB behind the tail.
        with self.path.open('a') as stream:
            line = json.dumps({'type': 'response_item', 'payload': {'text': 'x' * 65536}}) + '\n'
            for _ in range(272):
                stream.write(line)

    def test_long_active_output_is_unknown_not_old_failure(self):
        self.long_running_output()
        self.assertIsNone(task_snapshot(self.path, 'original'))

    def test_new_final_failure_is_visible_after_long_output(self):
        self.long_running_output()
        self.assertIsNone(task_snapshot(self.path, 'original'))
        self.event('task_complete', 'active', {'message': 'rate limit exceeded'})
        actual = task_snapshot(self.path, 'original')
        self.assertEqual((actual['kind'], actual['turn_id']), ('task_complete', 'active'))
        self.assertEqual(actual['error']['message'], 'rate limit exceeded')

    def test_new_activity_invalidates_cached_failure(self):
        self.long_running_output()
        for kind in ('task_started', 'user_message', 'turn_aborted'):
            with self.subTest(kind=kind):
                self.event('task_complete', 'failed', {'message': 'rate limit exceeded'})
                self.assertEqual(task_snapshot(self.path, 'original')['kind'], 'task_complete')
                self.event(kind, 'new')
                actual = task_snapshot(self.path, 'original')
                self.assertEqual((actual['kind'], actual['turn_id']), (kind, 'new'))

    def test_replaced_transcript_cannot_reuse_failure_cache(self):
        self.event('task_complete', 'failed', {'message': 'rate limit exceeded'})
        self.assertEqual(task_snapshot(self.path, 'original')['kind'], 'task_complete')
        replacement = self.path.with_suffix('.replacement')
        replacement.write_text(json.dumps({'type': 'session_meta',
                                            'payload': {'id': 'foreign'}}) + '\n')
        replacement.replace(self.path)
        self.assertIsNone(task_snapshot(self.path, 'original'))

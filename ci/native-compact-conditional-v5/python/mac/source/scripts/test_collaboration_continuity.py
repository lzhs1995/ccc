import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import collaboration_continuity as c


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ids = [f'{i:08d}-1111-4111-8111-111111111111' for i in (1, 2, 3)]
        self.value = dict(schema=c.SCHEMA, task_id='original',
            workspace_uuid='99999999-1111-4111-8111-111111111111',
            automatic_fallback_authorized=True, authorization=self.pin('authorization'),
            max_subagents=2, observations=[], participants=[])
        for i, role in enumerate(('supervisor', 'executor', 'executor')):
            self.value['participants'].append(dict(role=role, surface_uuid=self.ids[i],
                session_id=self.ids[i], task_id='original', workspace_uuid=self.value['workspace_uuid']))

    def pin(self, text):
        raw = text.encode()
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / (digest + '.txt')
        path.write_bytes(raw)
        return dict(path=str(path), sha256=digest)

    def event(self, peer, kind='API_SUCCESS', at=1, status=None):
        obs = dict(task_id='original', surface_uuid=self.ids[peer], session_id=self.ids[peer],
            attempt_id=f'{peer}-{at}', kind=kind, terminal=True, at_epoch=at)
        if status is not None:
            obs['http_status'] = status
        obs['evidence'] = self.pin(json.dumps(obs, sort_keys=True))
        self.value['observations'].append(obs)
        return obs

    def outage(self, peer):
        for at in (10, 70, 130):
            self.event(peer, 'API_ERROR', at, 502)

    def test_all_three_available_then_each_failure_topology(self):
        for peer in range(3):
            self.event(peer)
        self.assertEqual(c.plan(self.value, now=150)['mode'], 'TRIO')
        self.outage(1)
        self.assertEqual(c.plan(self.value, now=150)['mode'], 'CODEX_CLAUDE')
        self.outage(2)
        result = c.plan(self.value, now=150)
        self.assertEqual(result['mode'], 'CODEX_SOLO')
        self.assertEqual(result['resource_releases'], 0)
        self.assertFalse(result['replacement_session_allowed'])
        for peer in (1, 2):
            self.event(peer, at=200)
        self.outage(0)
        self.assertEqual(c.plan(self.value, now=210)['mode'], 'CLAUDE_PEER')

    def test_queue_silence_and_unknown_are_not_api_failures(self):
        for peer in range(3):
            self.event(peer)
        for at in (10, 70, 130):
            self.event(0, 'NO_REPLY', at)
        result = c.plan(self.value, now=150)
        self.assertEqual(result['mode'], 'TRIO')
        self.assertEqual(result['health'][self.ids[0]]['consecutive_failures'], 0)

    def test_queue_does_not_erase_terminal_failures_or_invent_health(self):
        self.event(0, 'QUEUED', 1)
        self.assertEqual(c.plan(self.value, now=2)['health'][self.ids[0]]['state'], 'UNKNOWN')
        for at in (10, 70, 130):
            self.event(0, 'API_ERROR', at, 502)
            self.event(0, 'QUEUED', at+1)
        result = c.plan(self.value, now=150)
        self.assertEqual(result['health'][self.ids[0]]['state'], 'UNAVAILABLE')
        with self.assertRaises(ValueError):
            c.plan(self.value, now=float('nan'))

    def test_success_resets_failure_episode_and_retries_are_bounded(self):
        self.event(0)
        self.event(1, 'API_ERROR', 10, 502)
        self.event(1, at=20)
        self.event(1, 'API_ERROR', 30, 502)
        result = c.plan(self.value, now=40)
        self.assertEqual(result['health'][self.ids[1]]['consecutive_failures'], 1)
        self.assertEqual(result['health'][self.ids[1]]['retry_due_epoch'], 90)
        self.assertEqual(result['mode'], 'CODEX_CONTINUE_INDEPENDENT')

    def test_original_attempt_cannot_be_counted_twice_or_before_retry_after(self):
        obs = self.event(0, 'API_ERROR', 10, 502)
        self.value['observations'].append(copy.deepcopy(obs))
        with self.assertRaisesRegex(ValueError, 'multiple attempts'):
            c.plan(self.value, now=100)
        self.value['observations'].pop()
        self.event(0, 'API_ERROR', 20, 502)
        with self.assertRaisesRegex(ValueError, 'retry-after'):
            c.plan(self.value, now=100)

    def test_foreign_session_changed_evidence_and_future_event_refused(self):
        obs = self.event(0)
        obs['session_id'] = self.ids[1]
        with self.assertRaises(ValueError):
            c.plan(self.value, now=100)
        obs['session_id'] = self.ids[0]
        with self.assertRaises(ValueError):
            c.plan(self.value, now=0)
        Path(obs['evidence']['path']).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            c.plan(self.value, now=100)


if __name__ == '__main__':
    unittest.main()

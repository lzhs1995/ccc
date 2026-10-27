"""JEV must reach the real daemon retry path, with current native evidence."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import ccc_codex_goal as goal
from ccc_codex_queue import task_snapshot
from ccc_provider_retry import ProviderRetryStore
import ccc_jev_unknown as jev
import cmux_codex_watch as core
import tests.test_codex_goal_resume as resume_fixture
from tests.test_codex_status_chrome import status_payload, visible_text
from tests.test_watch import span


UNKNOWN = 'The service reports a request pace beyond its permitted window.'


class CodexJevRecoveryTests(unittest.TestCase):
    def setUp(self):
        resume_fixture.GoalResumeDeliveryTests.setUp(self)
        self.payload = status_payload(UNKNOWN)
        grid = self.payload['render_grid']
        row, width = grid['cursor']['row'], grid['columns']
        grid.update(full=True, anchor='screen', rows=row + 3)
        grid['styles'] += [dict(id=20, foreground='#FFFFFF', background='#393939', faint=False),
                           dict(id=21, foreground='#FFFFFF', background='#393939', faint=True),
                           dict(id=22, foreground='#989898', background='#1E1E1E', faint=True)]
        grid['row_spans'] = [s for s in grid['row_spans'] if s['row'] < row - 1] + [
            span(row - 1, 0, ' ' * width, 20), span(row, 0, '› ', 20),
            span(row, 2, 'Ask Codex to do anything'.ljust(width - 2), 21),
            span(row + 1, 0, ' ' * width, 20),
            span(row + 2, 0, 'gpt-6-astra max · Goal stalled (/goal resume)', 22)]
        self.client.payload, self.client.text = self.payload, visible_text(self.payload)
        self.path = Path(self.temp.name) / 'current-failure.jsonl'
        self.path.write_text('\n'.join(json.dumps(row) for row in [
            {'type': 'session_meta', 'payload': {'id': self.sid, 'model_provider': 'synthetic-provider'}},
            {'type': 'event_msg', 'timestamp': '1970-01-01T00:03:20Z',
             'payload': {'type': 'task_complete', 'turn_id': 'failed', 'error': {'message': UNKNOWN}}},
        ]) + '\n')
        self.turn = lambda: {'session_id': self.sid, 'pid': 12345, 'process_start': 1,
                            **(task_snapshot(self.path, self.sid) or {'kind': 'unknown'})}
        self.daemon.codex_queue_recovery.current_turn = lambda _: self.turn()
        self.proof.update(error={'message': UNKNOWN}, proof_source='native_failed_eof_v1',
                          native_evidence={'snapshot': task_snapshot(self.path, self.sid)})
        self.daemon._provider_retry = ProviderRetryStore(Path(self.temp.name) / 'jev-retry.sqlite',
            clock=lambda: self.retry_now, jitter=lambda: 0)
        self.requests = []
        self.answer = 'RATE_LIMIT'
        def transport(request, timeout):
            self.requests.append(request)
            return {'model': jev.MODEL, 'answers': {'diagnosis': {
                'type': 'choice', 'choice': self.answer, 'confidence': .95}}}
        self.daemon.jev_classifier = jev.JevUnknownClassifier(transport, start=False, is_enabled=lambda: True)
        self.addCleanup(self.daemon.jev_classifier.stop)

    def pass_once(self):
        with patch.object(goal, 'blocked_goal', return_value=self.proof):
            self.daemon.process_once(self.client)

    def classify(self):
        self.pass_once()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.daemon.jev_classifier.queue.qsize(), 1)
        self.daemon.jev_classifier.run_once()

    def allow_provider_delay(self, code='rate_limit'):
        evidence = self.daemon._provider_retry.observe(self.sid, 'synthetic-provider',
            'failed', code, UNKNOWN, 200)
        decision = self.daemon._provider_retry.evaluate(evidence)
        self.retry_now = max(self.retry_now, decision.retry_at + .001)

    def test_unknown_goal_reaches_one_real_continuation_after_async_answer(self):
        self.classify()
        self.allow_provider_delay()
        self.pass_once()
        self.assertEqual([row[-1] for row in self.client.sent], ['任务请继续'])
        for _ in range(2):
            self.pass_once()
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(len(self.requests), 1)

    def test_unknown_upstream_keeps_existing_goal_resume_route(self):
        self.answer = 'UPSTREAM'
        self.classify()
        self.allow_provider_delay('http_503')
        self.pass_once()
        self.assertEqual([row[-1] for row in self.client.sent], ['/goal resume'])

    def test_abstention_never_becomes_retry_permission(self):
        self.answer = 'AUTHENTICATION'
        self.classify()
        self.pass_once()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(len(self.requests), 1)

    def test_real_user_append_invalidates_cached_failure(self):
        self.classify()
        self.allow_provider_delay()
        with self.path.open('a') as handle:
            handle.write(json.dumps({'type': 'response_item', 'payload': {
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'my next task'}]}}) + '\n')
        self.pass_once()
        self.assertEqual(self.client.sent, [])

    def test_cached_answer_lost_during_reservation_cancels_input(self):
        self.classify()
        self.allow_provider_delay()
        save = self.daemon._save_delivery
        def evict(*args, **kwargs):
            result = save(*args, **kwargs)
            self.daemon.jev_classifier.cache.clear()
            return result
        with patch.object(self.daemon, '_save_delivery', side_effect=evict):
            self.pass_once()
        self.assertEqual(self.client.sent, [])

    def test_draft_and_original_unknown_delivery_do_not_enqueue(self):
        runtime = self.daemon.runtime.setdefault('surface-uuid', core.TargetRuntime())
        runtime.delivery_status = 'unknown'
        self.pass_once()
        self.assertEqual(self.daemon.jev_classifier.queue.qsize(), 0)
        runtime.delivery_status = ''
        prompt = next(s for s in self.payload['render_grid']['row_spans'] if s['row'] == 54 and s['column'] == 2)
        prompt.update(text='keep my real draft', style_id=0, cell_width=18)
        self.payload['render_grid']['cursor']['column'] = 20
        self.client.text = visible_text(self.payload)
        self.pass_once()
        self.assertEqual(self.daemon.jev_classifier.queue.qsize(), 0)
        self.assertEqual(self.client.sent, [])


if __name__ == '__main__':
    unittest.main()

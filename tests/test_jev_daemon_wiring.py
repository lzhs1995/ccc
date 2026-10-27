import math, types, unittest
from unittest import mock
import ccc_jev_unknown as jev
import cmux_codex_watch as w
import ccc_compact_guard as compact
from pathlib import Path

OK = jev.Gates(True, True, True)
def resp(choice, conf=0.95, model=jev.MODEL, typ='choice'):
    return {'model': model, 'answers': {'diagnosis': {'type': typ, 'choice': choice, 'confidence': conf}}}

class Fake:
    def __init__(self, reply): self.reply, self.requests = reply, []
    def __call__(self, request, timeout):
        self.requests.append(request); return self.reply

class ProtocolTests(unittest.TestCase):
    def test_official_request_shape(self):
        r = jev.build_request('codex', 'x')
        q = r['questions']['diagnosis']
        self.assertEqual((r['model'], q['type']), ('jev-1.14.0', 'choice'))
        self.assertNotIn('choices', q)
        self.assertTrue({'RATE_LIMIT', 'AUTHENTICATION', 'QUOTA', 'UPSTREAM', 'UNKNOWN'} <= set(q['criteria']))
    def test_response_validation(self):
        self.assertEqual(jev.parse_answer(resp('RATE_LIMIT', 1.0)), 'rate_limit')
        self.assertEqual(jev.parse_answer(resp('UPSTREAM')), 'http_503')
        for bad in (resp('AUTHENTICATION'), resp('QUOTA'), resp('UNKNOWN'), resp('RATE_LIMIT', math.nan),
                    resp('RATE_LIMIT', math.inf), resp('RATE_LIMIT', 1.5), resp('RATE_LIMIT', 0.5),
                    resp(['RATE_LIMIT']), resp('RATE_LIMIT', model='other'), resp('RATE_LIMIT', typ='text'),
                    {'answers': 'x'}, None, 'str'):
            self.assertIsNone(jev.parse_answer(bad), bad)
    def test_full_text_fingerprint(self):
        a = 'x' * 700 + 'A'; b = 'x' * 700 + 'B'
        self.assertNotEqual(jev.fingerprint('codex', '', a), jev.fingerprint('codex', '', b))
    def test_disabled_zero_calls_and_stop(self):
        c = jev.JevUnknownClassifier(Fake(resp('RATE_LIMIT')), start=False, is_enabled=lambda: False)
        self.assertIsNone(c.resolve(None, OK, 'codex', '', 'weird')); self.assertTrue(c.queue.empty())
        c = jev.JevUnknownClassifier(Fake(resp('RATE_LIMIT')), start=False, is_enabled=lambda: True)
        c.stop(); self.assertIsNone(c.resolve(None, OK, 'codex', '', 'weird')); self.assertFalse(c.run_once())
    def test_cache_limit(self):
        c = jev.JevUnknownClassifier(Fake(resp('RATE_LIMIT')), start=False, is_enabled=lambda: True)
        for i in range(jev.CACHE_LIMIT + 5): c._store(str(i), None)
        self.assertEqual(len(c.cache), jev.CACHE_LIMIT)

class DaemonWiringTests(unittest.TestCase):
    def daemon(self, reply, message, turn='t1'):
        d = w.WatchDaemon.__new__(w.WatchDaemon)
        d.jev_classifier = jev.JevUnknownClassifier(Fake(reply), start=False, is_enabled=lambda: True)
        err = {'message': message}
        snapshot = {'kind': 'task_complete', 'session_id': 's', 'turn_id': turn, 'error': err,
                    'at': 100, 'signature': [1, 2, 3],
                    'terminal_tail': {'offset': 0, 'end': 2, 'sha256': '0' * 64}}
        d.goal = {'proof_source': 'native_failed_eof_v1', 'session_id': 's', 'turn_id': turn, 'error': err,
                  'kind': 'goal_blocked', 'model_provider': 'custom', 'native_evidence': {'snapshot': snapshot}}
        d.config = {'mode': 'armed'}
        d.config_path = Path('unused-test-config')
        d.stop_requested = False
        d.runtime = {'surface-uuid': w.TargetRuntime()}
        d._current_blocked_goal = lambda target: d.goal
        d.codex_queue_recovery = types.SimpleNamespace(
            current_turn=lambda target: dict(d.goal['native_evidence']['snapshot']),
            has_pending_draft=lambda sid: False, blocks_turn=lambda target, turn: False)
        d._codex_terminal_tail_proven = lambda snap: True
        return d
    def call(self, d, draft_empty=True):
        state = w.ScreenState('idle', screen_signature='sig')
        grid = types.SimpleNamespace(lines=['', 'Goal stalled (/goal resume)', '', ''], cursor=types.SimpleNamespace(row=0))
        with mock.patch.object(w, '_goal_boundary_signature', return_value=('ok',) if draft_empty else None), \
                mock.patch.object(compact, 'inspect', return_value={}), \
                mock.patch.object(compact, 'status', return_value=None):
            return d._native_goal_error_state({'surface_id': 'surface-uuid'}, state, grid), state
    def test_unknown_native_failure_async_then_cached_retry_reason(self):
        d = self.daemon(resp('RATE_LIMIT'), 'the model service said: please slow your request pace')
        first, state = self.call(d)
        self.assertIs(first, state); self.assertEqual((d.jev_classifier.queue.qsize(), d.jev_classifier.calls), (1, 0))
        d.jev_classifier.run_once()
        second, _ = self.call(d)
        self.assertEqual((second.kind, second.error_type, second.requires_goal_proof), ('recoverable_error', 'rate_limit', True))
        self.assertEqual(d.jev_classifier.calls, 1)
    def test_known_error_zero_api(self):
        d = self.daemon(resp('UPSTREAM'), 'rate limit exceeded: Your requests to gpt-6.1-sol for gpt-6.1-sol in eastus2 have exceeded token rate limit.')
        out, _ = self.call(d)
        self.assertEqual((out.error_type, d.jev_classifier.calls, d.jev_classifier.queue.qsize()), ('rate_limit', 0, 0))
    def test_draft_blocks_before_api(self):
        d = self.daemon(resp('RATE_LIMIT'), 'weird new failure text')
        out, state = self.call(d, draft_empty=False)
        self.assertIs(out, state); self.assertEqual(d.jev_classifier.queue.qsize(), 0)
    def test_late_cache_for_changed_turn_not_used(self):
        d = self.daemon(resp('RATE_LIMIT'), 'weird new failure text', turn='t1')
        self.call(d); d.jev_classifier.run_once()
        d.goal['turn_id'] = 't2'; d.goal['native_evidence']['snapshot']['turn_id'] = 't2'
        out, state = self.call(d)
        self.assertIs(out, state)
    def test_unproven_tail_no_api(self):
        d = self.daemon(resp('RATE_LIMIT'), 'weird new failure text'); d._codex_terminal_tail_proven = lambda s: False
        out, state = self.call(d)
        self.assertIs(out, state); self.assertEqual(d.jev_classifier.queue.qsize(), 0)

if __name__ == '__main__':
    unittest.main()

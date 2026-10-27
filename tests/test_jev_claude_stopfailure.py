# Claude StopFailure → JEV：生产端有界原文 + 守护端缓存细化（不发真实请求）
import sys, types, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import claude_ccc_protocol as proto
import ccc_jev_unknown as jev
import cmux_codex_watch as watch


def stop_failure(error, details):
    return proto.build_event({'hook_event_name': 'StopFailure', 'session_id': 'S1', 'error': error,
                              'error_details': details}, {'CMUX_SURFACE_ID': 'X'})


class FakeClassifier:
    def __init__(self, answer=None):
        self.answer, self.calls = answer, []
    def resolve(self, local, gates, client, model, text, provider=''):
        self.calls.append((gates, client, text, provider))
        return self.answer if gates.allow() else None


def daemon(classifier, **rt):
    d = watch.WatchDaemon.__new__(watch.WatchDaemon)
    d.jev_classifier = classifier
    d.config = {'mode': 'armed'}
    d.stop_requested = False
    runtime = types.SimpleNamespace(claude_session_id='S1', claude_context_composer_kind='empty',
                                    claude_handshake_wait=False, paused_reason=None,
                                    claude_compaction_started_at=0.0, delivery_status='')
    runtime.__dict__.update(rt)
    return d, runtime


class ProducerTests(unittest.TestCase):
    def test_unknown_keeps_hash_bound_excerpt(self):
        e = stop_failure('weird_thing', 'server said: please slow down')
        self.assertEqual((e['error_kind'], e['stop_failure_error']), ('claude_api', 'unknown'))
        self.assertEqual(proto._digest(e['error_excerpt']), e['error_evidence_hash'])

    def test_known_has_no_excerpt(self):
        for err, det in (('rate_limit', ''), ('x', 'HTTP 503 overloaded'), ('authentication_failed', '')):
            self.assertNotIn('error_excerpt', stop_failure(err, det))

    def test_long_text_not_truncated_into_excerpt(self):
        self.assertNotIn('error_excerpt', stop_failure('odd', 'z' * 700))


class DaemonRefineTests(unittest.TestCase):
    def test_pending_returns_event_unchanged_then_cached_relabels(self):
        e = stop_failure('odd', 'server said: please slow down')
        pending = FakeClassifier(None)
        d, rt = daemon(pending)
        self.assertIs(d._jev_refine_claude_failure({}, rt, e, composer_kind='empty'), e)
        self.assertEqual(pending.calls[0][3], 'S1|' + e['event_id'])
        d.jev_classifier = FakeClassifier('rate_limit')
        out = d._jev_refine_claude_failure({}, rt, e, composer_kind='empty')
        self.assertEqual((out['error_kind'], out['error_reason']), ('claude_429', 'rate_limited'))
        self.assertIn(out['error_kind'], watch.CLAUDE_PROVIDER_RETRY_KINDS)
        d.jev_classifier = FakeClassifier('http_503')
        self.assertEqual(d._jev_refine_claude_failure({}, rt, e, composer_kind='empty')['error_kind'], 'claude_503')

    def test_known_failure_zero_classifier_calls(self):
        c = FakeClassifier('rate_limit'); d, rt = daemon(c)
        e = stop_failure('overloaded', '')
        self.assertIs(d._jev_refine_claude_failure({}, rt, e), e); self.assertEqual(c.calls, [])

    def test_tampered_excerpt_ignored(self):
        c = FakeClassifier('rate_limit'); d, rt = daemon(c)
        e = {**stop_failure('odd', 'slow down'), 'error_excerpt': 'forged'}
        self.assertIs(d._jev_refine_claude_failure({}, rt, e), e); self.assertEqual(c.calls, [])

    def test_gates_block_draft_queue_pause_compaction_unknown_delivery_identity(self):
        e = stop_failure('odd', 'slow down')
        for rt in ({'claude_context_composer_kind': 'draft'}, {'claude_handshake_wait': True},
                   {'paused_reason': 'workspace interrupt pause'}, {'claude_compaction_started_at': 5.0},
                   {'delivery_status': 'unknown'}, {'claude_session_id': 'OTHER'}):
            c = FakeClassifier('rate_limit'); d, r = daemon(c, **rt)
            self.assertIs(d._jev_refine_claude_failure({}, r, e,
                composer_kind=r.claude_context_composer_kind), e, rt)

    def test_real_classifier_gates_fail_no_enqueue(self):
        calls = []
        c = jev.JevUnknownClassifier(lambda req, t: calls.append(req), start=False, is_enabled=lambda: True)
        d, rt = daemon(c, claude_context_composer_kind='draft')
        d._jev_refine_claude_failure({}, rt, stop_failure('odd', 'slow down'), composer_kind='draft')
        self.assertEqual((c.calls, c.queue.qsize()), (0, 0))

    def test_old_empty_telemetry_without_current_composer_never_enqueues(self):
        c = jev.JevUnknownClassifier(lambda req, t: self.fail('unexpected API'),
            start=False, is_enabled=lambda: True)
        self.addCleanup(c.stop)
        d, rt = daemon(c, claude_context_composer_kind='empty')
        d._jev_refine_claude_failure({}, rt, stop_failure('odd', 'slow down'))
        self.assertEqual(c.queue.qsize(), 0)


if __name__ == '__main__':
    unittest.main()

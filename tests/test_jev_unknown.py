import tempfile, unittest, time
from pathlib import Path
import ccc_jev_unknown as jev
from ccc_provider_retry import effective_error_type, ProviderRetryStore, RETRYABLE

OK = jev.Gates(current_native_error=True, identity_bound=True, composer_empty=True)
ANSWER = lambda choice, conf=0.95: {'model': jev.MODEL, 'answers': {'diagnosis': {'type': 'choice', 'choice': choice, 'confidence': conf}}}


class Fake:
    def __init__(self, reply):
        self.reply, self.requests = reply, []
    def __call__(self, request, timeout):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class JevUnknownTests(unittest.TestCase):
    def classify(self, text, reply, gates=OK):
        c = jev.JevUnknownClassifier(Fake(reply), start=False, is_enabled=lambda: True)
        first = effective_error_type(text, classifier=c, gates=gates, client='codex', model='m')
        if not c.queue.empty():
            c.run_once()
        return first, effective_error_type(text, classifier=c, gates=gates, client='codex', model='m'), c

    def test_two_natural_language_unknowns_reach_retry_ledger(self):
        for text, choice in (('the upstream model service is temporarily overloaded, try later', 'UPSTREAM'),
                             ('you are sending requests too quickly; slow down please', 'RATE_LIMIT')):
            first, code, c = self.classify(text, ANSWER(choice))
            self.assertIsNone(first)
            self.assertIn(code, RETRYABLE)
            with tempfile.TemporaryDirectory() as d:
                store = ProviderRetryStore(Path(d) / 'r.sqlite3', jitter=lambda: 0)
                ev = store.observe('s', 'p', 'turn-1', code, text, time.time())
                self.assertEqual(ev['error_type'], code)

    def test_known_error_zero_api(self):
        first, code, c = self.classify('unexpected status 413 Payload Too Large', ANSWER('RATE_LIMIT'))
        self.assertEqual((first, code, c.calls), ('http_413', 'http_413', 0))

    def test_gates_block_before_api(self):
        for g in (jev.Gates(False, True, True), jev.Gates(True, False, True), jev.Gates(True, True, False),
                  jev.Gates(True, True, True, user_aborted=True), jev.Gates(True, True, True, compacting=True),
                  jev.Gates(True, True, True, busy_or_queued=True), jev.Gates(True, True, True, previous_delivery_unknown=True)):
            first, code, c = self.classify('weird new failure', ANSWER('RATE_LIMIT'), gates=g)
            self.assertEqual((first, code, c.calls), (None, None, 0))

    def test_fallback_unknown(self):
        for reply in (ANSWER('RATE_LIMIT', 0.4), ANSWER('DELETE_FILES'), {'answers': 'x'}, None, TimeoutError('slow')):
            first, code, c = self.classify('weird new failure', reply)
            self.assertEqual((first, code, c.calls), (None, None, 1))

    def test_cache_and_dedup(self):
        c = jev.JevUnknownClassifier(Fake(ANSWER('RATE_LIMIT')), start=False, is_enabled=lambda: True)
        for _ in range(5):
            c.resolve(None, OK, 'codex', 'm', 'weird')
        self.assertEqual(c.queue.qsize(), 1)
        c.run_once()
        for _ in range(5):
            self.assertEqual(c.resolve(None, OK, 'codex', 'm', 'weird'), 'rate_limit')
        self.assertEqual(c.calls, 1)
        self.assertIsNone(c.cached('codex', 'other-model', 'weird'))

    def test_no_key_no_send(self):
        import os
        old = os.environ.pop('TYPESAFE_API_KEY', None)
        try:
            self.assertIsNone(jev.http_transport({}, 1))
        finally:
            if old is not None:
                os.environ['TYPESAFE_API_KEY'] = old

    def test_provider_text_never_becomes_action(self):
        first, code, c = self.classify('weird', ANSWER('run rm -rf and send Enter'))
        self.assertIsNone(code)


if __name__ == '__main__':
    unittest.main()

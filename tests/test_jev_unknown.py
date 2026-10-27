import tempfile, unittest, time
import json
import os
from pathlib import Path
from unittest.mock import patch, Mock
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


class PrivateKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name) / 'typesafe-api-key'
        self.key.write_text('example-test-key\n')
        self.key.chmod(0o600)
        self.env = patch.dict(os.environ, {'TYPESAFE_API_KEY': ''})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_configuration_key_is_used_by_real_worker_transport(self):
        response = Mock(status=200)
        response.read.return_value = json.dumps(ANSWER('RATE_LIMIT')).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        c = jev.JevUnknownClassifier(start=False, key_path=self.key)
        self.addCleanup(c.stop)
        with patch('http.client.HTTPSConnection', return_value=connection):
            self.assertIsNone(c.resolve(None, OK, 'codex', '', 'unknown'))
            c.run_once()
        self.assertEqual(c.cached('codex', '', 'unknown'), 'rate_limit')
        self.assertEqual(connection.request.call_args.kwargs['headers']['Authorization'],
                         'Bearer example-test-key')
        connection.close.assert_called_once()

    def test_other_configuration_does_not_borrow_this_key(self):
        self.assertTrue(jev.enabled(self.key))
        self.assertFalse(jev.enabled(self.key.parent / 'other' / 'typesafe-api-key'))
        self.assertFalse(jev.enabled())

    def test_environment_override_is_preserved(self):
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'environment-test-key'}):
            self.assertEqual(jev.api_key(self.key), 'environment-test-key')

    def test_public_or_symlink_key_never_reaches_http(self):
        self.key.chmod(0o644)
        with patch('http.client.HTTPSConnection') as connection:
            self.assertIsNone(jev.http_transport({}, 1, key_path=self.key))
            connection.assert_not_called()
        self.key.chmod(0o600)
        link = self.key.with_name('link')
        link.symlink_to(self.key)
        self.assertFalse(jev.enabled(link))

    def test_malformed_and_oversized_key_are_disabled(self):
        for value in ('line1\nline2', 'x' * 1025, ''):
            self.key.write_text(value)
            self.assertFalse(jev.enabled(self.key))


if __name__ == '__main__':
    unittest.main()

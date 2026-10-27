import tempfile, unittest, time
import json
import os
import threading
import subprocess
import sys
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    def test_non_quiescent_worker_never_reports_closed(self):
        c = jev.JevUnknownClassifier(start=False)
        c.thread = Mock(daemon=False)
        c.thread.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'has not quiesced'):
            c.stop()
        self.assertTrue(c.stopped)
        c.thread.join.assert_called_once_with(jev.TIMEOUT_SECONDS + 1)

    def test_non_daemon_worker_classifies_and_quiesces(self):
        c = jev.JevUnknownClassifier(Fake(ANSWER('RATE_LIMIT')),
            is_enabled=lambda: True, daemon_threads=False)
        try:
            self.assertIsNone(c.resolve(None, OK, 'codex', 'm', 'new terminal failure'))
            deadline = time.monotonic() + 2
            while c.cached('codex', 'm', 'new terminal failure') is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(c.cached('codex', 'm', 'new terminal failure'), 'rate_limit')
            self.assertEqual(c.calls, 1)
        finally:
            c.stop()
        self.assertFalse(c.thread.is_alive())

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
        c = jev.JevUnknownClassifier(start=False, key_path=self.key)
        self.addCleanup(c.stop)
        with patch.object(jev, '_curl_post_json', return_value=ANSWER('RATE_LIMIT')) as request:
            self.assertIsNone(c.resolve(None, OK, 'codex', '', 'unknown'))
            c.run_once()
        self.assertEqual(c.cached('codex', '', 'unknown'), 'rate_limit')
        args = request.call_args.args
        self.assertEqual(args[0], 'https://' + jev.ENDPOINT_HOST + jev.ENDPOINT_PATH)
        self.assertEqual(args[2:], ('example-test-key', jev.TIMEOUT_SECONDS))

    def test_other_configuration_does_not_borrow_this_key(self):
        self.assertTrue(jev.enabled(self.key))
        self.assertFalse(jev.enabled(self.key.parent / 'other' / 'typesafe-api-key'))
        self.assertFalse(jev.enabled())

    def test_environment_override_is_preserved(self):
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'environment-test-key'}):
            self.assertEqual(jev.api_key(self.key), 'environment-test-key')

    def test_public_or_symlink_key_never_reaches_http(self):
        self.key.chmod(0o644)
        with patch.object(jev, '_curl_post_json') as connection:
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


class WholeRequestDeadlineTests(unittest.TestCase):
    def setUp(self):
        try:
            jev._curl_api()
        except OSError as exc:
            self.skipTest(str(exc))

    @contextmanager
    def upstream(self, mode):
        entered = threading.Event()
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                received.append((self.path, self.headers.get('Authorization'),
                    json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                entered.set()
                payload = json.dumps(ANSWER('RATE_LIMIT')).encode()
                headers = (b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                           + ('Content-Length: %d\r\n\r\n' % len(payload)).encode())
                try:
                    if mode == 'silent':
                        time.sleep(.4)
                    elif mode == 'headers':
                        for byte in headers:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(.02)
                    else:
                        self.wfile.write(headers)
                        for byte in payload:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            if mode == 'body':
                                time.sleep(.02)
                except OSError:
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
        thread.start()
        try:
            yield 'http://127.0.0.1:%d/current' % server.server_port, entered, received
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)

    def test_success_sends_the_original_request_and_key(self):
        with self.upstream('normal') as (url, _, received):
            request = jev.build_request('claude', 'current failure')
            self.assertEqual(jev._curl_post_json(url, request, 'example-test-key', 1),
                             ANSWER('RATE_LIMIT'))
            self.assertEqual(received, [('/current', 'Bearer example-test-key', request)])

    def test_slow_headers_body_and_silent_upstream_have_one_total_deadline(self):
        for mode in ('headers', 'body', 'silent'):
            with self.subTest(mode=mode), self.upstream(mode) as (url, _, __):
                started = time.monotonic()
                self.assertIsNone(jev._curl_post_json(url, {}, 'example-test-key', .15))
                self.assertLess(time.monotonic() - started, .85)

    def test_slow_response_worker_quiesces_and_keeps_unknown(self):
        with self.upstream('body') as (url, entered, _), patch.object(jev, 'TIMEOUT_SECONDS', .15):
            classifier = jev.JevUnknownClassifier(
                lambda request, timeout: jev._curl_post_json(url, request, 'example-test-key', timeout),
                is_enabled=lambda: True, daemon_threads=False)
            self.addCleanup(classifier.stop)
            self.assertIsNone(classifier.resolve(None, OK, 'claude', '', 'current failure'))
            self.assertTrue(entered.wait(1))
            classifier.stop()
            self.assertFalse(classifier.thread.is_alive())
            self.assertEqual(classifier.calls, 1)
            self.assertEqual(list(classifier.cache.values()), [None])

    def test_missing_bounded_transport_never_falls_back_to_unbounded_http(self):
        with patch.object(jev, '_curl_api', side_effect=OSError('unavailable')):
            classifier = jev.JevUnknownClassifier(
                lambda request, timeout: jev._curl_post_json('http://127.0.0.1/', request, '', timeout),
                start=False, is_enabled=lambda: True)
            self.assertIsNone(classifier.resolve(None, OK, 'claude', '', 'current failure'))
            classifier.run_once()
            self.assertIsNone(classifier.cached('claude', '', 'current failure'))

    @unittest.skipUnless(sys.version_info >= (3, 14), 'native lanes use Python 3.14 interpreters')
    def test_slow_response_isolated_worker_and_interpreter_quiesce(self):
        with self.upstream('body') as (url, _, __):
            child = f'''
import ccc_jev_unknown as jev
jev.TIMEOUT_SECONDS = .15
classifier = jev.JevUnknownClassifier(
    lambda request, timeout: jev._curl_post_json({url!r}, request, 'example-test-key', timeout),
    is_enabled=lambda: True, daemon_threads=False)
assert classifier.resolve(None, jev.Gates(True, True, True), 'claude', '', 'failure') is None
import time
time.sleep(.04)
classifier.stop()
assert not classifier.thread.is_alive()
assert classifier.calls == 1
assert list(classifier.cache.values()) == [None]
'''
            script = ('from concurrent import interpreters\n'
                      'lane=interpreters.create()\ntry:\n'
                      f'    lane.exec({child!r})\nfinally:\n    lane.close()\n')
            result = subprocess.run([sys.executable, '-B', '-c', script],
                cwd=Path(jev.__file__).parent, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()

import http.client
import json
from pathlib import Path
import ssl
import tempfile
import unittest
import uuid

from tools.standby_test_provider import LocalProvider, recovered_turn


class ProviderTests(unittest.TestCase):
    def test_unmatched_or_interrupted_lifecycle_cannot_certify_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'transcript.jsonl'
            sid = str(uuid.uuid4())
            start = dict(type='task_started', turn_id='first')
            done = dict(type='task_complete', turn_id='first',
                        error=dict(codex_error_info='rate_limit_exceeded'))
            next_turn = dict(type='task_started', turn_id='second')
            for events in ([done, next_turn], [start, next_turn],
                           [start, done, start],
                           [start, dict(type='turn_aborted'), done, next_turn]):
                with self.subTest(events=events):
                    rows = [dict(type='session_meta', payload=dict(id=sid))]
                    rows += [dict(type='event_msg', payload=event) for event in events]
                    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
                    with self.assertRaises(ValueError):
                        recovered_turn(path, sid)

    def test_delayed_original_transcript_never_authorizes_early_success(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            sid = str(uuid.uuid4())
            path = root/'late.jsonl'
            selected = [None]
            provider = LocalProvider(root/'provider', 'Reply OK')
            self.addCleanup(provider.close)
            provider.bind(sid, lambda: selected[0])
            context = ssl.create_default_context(cafile=str(provider.cert))

            def request():
                connection = http.client.HTTPSConnection(
                    '127.0.0.1', provider.server.server_port, context=context, timeout=5)
                try:
                    connection.request('POST', '/v1/responses', json.dumps(dict(input=[
                        dict(role='user', content=[dict(type='input_text', text='Reply OK')])])),
                        {'thread-id': sid, 'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()

            for raw in (None, '', '{"type":"session_meta"'):
                if raw is not None:
                    path.write_text(raw)
                    selected[0] = path
                status, body = request()
                self.assertEqual(status, 200)
                self.assertIn(b'response.failed', body)
                self.assertNotIn(b'response.completed', body)
            path.write_text(json.dumps(dict(type='session_meta', payload=dict(id=sid)))+'\n')
            self.assertIn(b'response.failed', request()[1])
            path.write_text(json.dumps(dict(type='session_meta', payload=dict(id=str(uuid.uuid4()))))+'\n')
            self.assertEqual(request()[0], 400)
            selected[0] = root/'missing.jsonl'
            self.assertEqual(request()[0], 400)
            rows = [json.loads(line) for line in (provider.directory/'requests.jsonl').read_text().splitlines()]
            self.assertFalse(any(row.get('recovered_turn') for row in rows))

    def test_real_https_preserves_reconnect_until_terminal_and_new_turn(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            sid = str(uuid.uuid4())
            transcript = root/'transcript.jsonl'
            transcript.write_text(json.dumps(dict(type='session_meta', payload=dict(id=sid)))+'\n')
            provider = LocalProvider(root/'provider', 'Reply OK')
            self.addCleanup(provider.close)
            provider.bind(sid, transcript)
            context = ssl.create_default_context(cafile=str(provider.cert))

            def event(kind, **payload):
                with transcript.open('a') as stream:
                    stream.write(json.dumps(dict(type='event_msg', payload=dict(type=kind, **payload)))+'\n')

            def request(identity=sid, prompt='Reply OK'):
                connection = http.client.HTTPSConnection('127.0.0.1', provider.server.server_port,
                                                         context=context, timeout=5)
                try:
                    body = dict(input=[dict(role='user', content=[dict(type='input_text', text=prompt)])])
                    connection.request('POST', '/v1/responses', json.dumps(body),
                                       {'thread-id': identity, 'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()

            event('task_started', turn_id='first')
            for _ in range(3):
                status, data = request()
                self.assertEqual(status, 200)
                self.assertIn(b'response.failed', data)
                self.assertNotIn(b'response.completed', data)
            event('error', message='rate limit exceeded', will_retry=True)
            self.assertIn(b'response.failed', request()[1])
            event('task_complete', turn_id='first', error=dict(codex_error_info='rate_limit_exceeded'))
            self.assertIn(b'response.failed', request()[1])
            event('task_started', turn_id='second')
            self.assertIn(b'response.completed', request()[1])
            self.assertEqual(request(str(uuid.uuid4()))[0], 400)
            self.assertEqual(request(prompt='other')[0], 400)
            with self.assertRaises(ValueError):
                provider.bind(sid, transcript)
            rows = [json.loads(line) for line in (provider.directory/'requests.jsonl').read_text().splitlines()]
            self.assertEqual(len([row for row in rows if row.get('recovered_turn') == 'second']), 1)


if __name__ == '__main__':
    unittest.main()

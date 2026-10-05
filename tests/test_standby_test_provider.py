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
        import cmux_codex_watch as core
        from ccc_provider_retry import ProviderRetryStore
        from tests.test_codex_status_chrome import status_payload, visible_text
        from tests.test_watch import FakeClient, armed_daemon, span, visible_lines

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
                # Parse the actual HTTPS bytes, then apply Codex's native
                # error prefix and feed the real prefilter + structural pass.
                # Checking just the SSE code missed the native154 join defect.
                events = [json.loads(line[6:]) for line in data.decode().splitlines()
                          if line.startswith('data: ')]
                error = next(row['response']['error'] for row in events
                             if row['type'] == 'response.failed')
                self.assertEqual(error['code'], 'rate_limit_exceeded')
                banner = 'rate limit exceeded: ' + error['message']
                for columns in (80, 126):
                    payload = status_payload(banner, columns=columns)
                    self.assertEqual(core.classify_text_prefilter(visible_text(payload)).kind,
                                     'candidate')
                    state = core.classify_grid(core.Grid.from_rpc(payload, 'surface-uuid'))
                    self.assertEqual((state.kind, state.error_type),
                                     ('recoverable_error', 'rate_limit'))
            # A reconnect card may be an error candidate. Only native terminal
            # evidence can authorize delivery; an old error under a live turn
            # must never trigger either text or Enter.
            payload = status_payload(banner, columns=126)
            row = min(s['row'] for s in payload['render_grid']['row_spans'])
            payload['render_grid']['row_spans'].append(
                span(row - 1, 0, '• Reconnecting... 1/10 (1s • esc to interrupt)'))
            client = FakeClient(payload, '\n'.join(visible_lines(payload)))
            (root/'daemon').mkdir()
            daemon = armed_daemon(root/'daemon', client)
            self.addCleanup(daemon._process_snapshots.close)
            now = [1000.0]
            turn = dict(kind='task_started', session_id=sid, turn_id='first', at=200.0,
                        model_provider='synthetic-provider')
            daemon.codex_queue_recovery.current_turn = lambda _: dict(turn)
            daemon._provider_retry = ProviderRetryStore(
                daemon._provider_retry.path, clock=lambda: now[0], jitter=lambda: 0)
            for offset in (0.0, 1.0, 60.0):
                now[0] = 1000.0 + offset
                daemon.process_once(client)
                self.assertEqual(client.sent, [])
            event('error', message='rate limit exceeded', will_retry=True)
            self.assertIn(b'response.failed', request()[1])
            event('task_complete', turn_id='first', error=dict(codex_error_info='rate_limit_exceeded'))
            self.assertIn(b'response.failed', request()[1])
            turn.update(kind='task_complete', error=dict(message=banner))
            daemon.process_once(client)
            now[0] += 0.25
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            daemon.process_once(client)
            self.assertEqual(len(client.sent), 1)
            event('task_started', turn_id='second')
            self.assertIn(b'response.completed', request()[1])
            foreign_sid = str(uuid.uuid4())
            self.assertEqual(request(foreign_sid)[0], 400)
            self.assertEqual(request(prompt='other')[0], 400)
            with self.assertRaises(ValueError):
                provider.bind(sid, transcript)
            rows = [json.loads(line) for line in (provider.directory/'requests.jsonl').read_text().splitlines()]
            self.assertEqual(len([row for row in rows if row.get('recovered_turn') == 'second']), 1)
            refused = [row for row in rows if row.get('error')]
            self.assertEqual(len(refused), 2)
            self.assertEqual(
                [(r['session_id'], r['refusal_reason'], r['session_bound'],
                  r['prompt_matches']) for r in refused],
                [(foreign_sid, 'unbound_session', False, True),
                 (sid, 'unexpected_prompt', True, False)])
            import hashlib
            for row, prompt in zip(refused, ('Reply OK', 'other')):
                raw = json.dumps(dict(input=[dict(role='user', content=[
                    dict(type='input_text', text=prompt)])])).encode()
                self.assertEqual(row['body_sha256'], hashlib.sha256(raw).hexdigest())
                self.assertNotIn('authorization', row)
                self.assertNotIn('body', row)


if __name__ == '__main__':
    unittest.main()

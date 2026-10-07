import http.client
import json
from pathlib import Path
import ssl
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from tools.standby_test_provider import (LocalProvider, recovered_turn,
                                        resolve_request_transcript)


class ProviderTests(unittest.TestCase):
    def test_resolution_failures_keep_attempts_elapsed_and_cause(self):
        for late_path in (False, True):
            with self.subTest(late_path=late_path):
                now = [0.0]
                diagnostic = {}
                def observe():
                    now[0] += .1
                    return Path('/original') if late_path else None
                with self.assertRaises(TimeoutError):
                    resolve_request_transcript(observe, recovery_requested=True,
                        deadline=.1, clock=lambda: now[0], diagnostic=diagnostic)
                self.assertEqual(diagnostic['transcript_resolution_polls'], 1)
                self.assertEqual(diagnostic['transcript_observed'], late_path)
                self.assertAlmostEqual(diagnostic['transcript_resolution_elapsed_seconds'], .1)
                self.assertEqual(diagnostic['refusal_reason'],
                    'transcript_observation_late' if late_path else 'transcript_observation_timeout')

        now = [0.0]
        diagnostic = {}
        def changed():
            now[0] += .02
            raise ValueError('identity changed')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            resolve_request_transcript(changed, recovery_requested=True, deadline=1,
                clock=lambda: now[0], diagnostic=diagnostic)
        self.assertEqual(diagnostic['transcript_resolution_polls'], 1)
        self.assertEqual(diagnostic['refusal_reason'], 'transcript_observer_error')
        self.assertAlmostEqual(diagnostic['transcript_resolution_elapsed_seconds'], .02)

    def test_https_refusal_diagnostics_preserve_prompt_and_lifecycle_evidence(self):
        def short_resolution(transcript, **kwargs):
            kwargs['deadline'] = min(kwargs['deadline'], time.monotonic() + .02)
            return resolve_request_transcript(transcript, **kwargs)
        self.enterContext(patch('tools.standby_test_provider.resolve_request_transcript',
                                side_effect=short_resolution))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sid = str(uuid.uuid4())
            transcript = root/'original.jsonl'
            meta = dict(type='session_meta', payload=dict(id=sid))
            transcript.write_text(json.dumps(meta)+'\n')
            provider = LocalProvider(root/'provider', 'Activate', recovery_prompt='Continue')
            try:
                provider.bind(sid, transcript)
                context = ssl.create_default_context(cafile=str(provider.cert))
                def request(prompt):
                    connection = http.client.HTTPSConnection('127.0.0.1',
                        provider.server.server_port, context=context, timeout=5)
                    try:
                        body = json.dumps(dict(input=[dict(role='user', content=[
                            dict(type='input_text', text=prompt)])]))
                        connection.request('POST', '/v1/responses', body,
                            {'thread-id': sid, 'Authorization': 'Bearer fake-do-not-log'})
                        response = connection.getresponse()
                        status, data = response.status, response.read()
                    finally:
                        connection.close()
                    rows = [json.loads(line) for line in
                        (provider.directory/'requests.jsonl').read_text().splitlines()]
                    self.assertEqual(len(rows), request.count + 1)
                    request.count += 1
                    self.assertNotIn('fake-do-not-log', json.dumps(rows))
                    for row in rows:
                        self.assertFalse({'body', 'text', 'authorization'} & row.keys())
                    return status, data, rows[-1]
                request.count = 0
                start = dict(type='task_started', turn_id='first')
                limit = dict(type='task_complete', turn_id='first',
                             error=dict(codex_error_info='rate_limit_exceeded'))
                other = dict(type='task_complete', turn_id='first')
                second = dict(type='task_started', turn_id='second')
                cases = [([], 'no_task', None),
                         ([start], 'initial_task_active', 'first'),
                         ([start, other], 'terminal_not_rate_limit', None),
                         ([start, limit], 'rate_limit_terminal', None),
                         ([start, limit, second], 'recovered_task_active', 'second')]
                for events, stage, active in cases:
                    with self.subTest(stage=stage):
                        transcript.write_text(json.dumps(meta)+'\n'+''.join(
                            json.dumps(dict(type='event_msg', payload=e))+'\n' for e in events))
                        prompt = 'Activate' if active == 'second' else 'Continue'
                        status, _, row = request(prompt)
                        self.assertEqual(status, 400)
                        self.assertEqual(row['lifecycle_stage'], stage)
                        self.assertEqual(row['active_turn'], active)
                        self.assertEqual(row['prompt_class'],
                            'initial' if active == 'second' else 'recovery')
                        self.assertEqual(row['prompt_matches'], prompt == provider.prompt)
                        self.assertFalse(row['prompt_lifecycle_matches'])
                        self.assertEqual(row['refusal_reason'], 'prompt_lifecycle_mismatch'
                            if active == 'second' else 'lifecycle_observation_timeout')
                        self.assertGreaterEqual(row['request_elapsed_seconds'], 0)
                status, data, row = request('Continue')
                self.assertEqual(status, 200)
                self.assertIn(b'response.completed', data)
                self.assertTrue(row['prompt_lifecycle_matches'])
                self.assertEqual(row['recovered_turn'], 'second')

                # The real helper and HTTPS handler must carry failure details
                # into the single request row. Only this test's deadline is short.
                def short_resolution(transcript, **kwargs):
                    kwargs['deadline'] = time.monotonic() + .01
                    return resolve_request_transcript(transcript, **kwargs)
                def changed():
                    raise ValueError('identity changed')
                for observer, reason in ((lambda: None, 'transcript_observation_timeout'),
                                         (changed, 'transcript_observer_error')):
                    with provider.lock:
                        provider.sessions[sid] = observer
                    with patch('tools.standby_test_provider.resolve_request_transcript',
                               side_effect=short_resolution):
                        status, _, row = request('Continue')
                    self.assertEqual(status, 400)
                    self.assertEqual(row['refusal_reason'], reason)
                    self.assertGreater(row['transcript_resolution_polls'], 0)
                    self.assertGreaterEqual(row['transcript_resolution_elapsed_seconds'], 0)
                    self.assertGreaterEqual(row['request_elapsed_seconds'], 0)
            finally:
                provider.close()

    def test_pending_recovery_inventory_is_reobserved_without_cached_path(self):
        now = [0.0]
        observations = iter([None, None, Path('/original')])
        path, polls = resolve_request_transcript(
            lambda: next(observations), recovery_requested=True, deadline=1,
            clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0]+n))
        self.assertEqual((path, polls), (Path('/original'), 3))
        self.assertAlmostEqual(now[0], .1)

    def test_pending_observation_never_extends_request_deadline(self):
        now = [0.0]
        with self.assertRaises(TimeoutError):
            resolve_request_transcript(lambda: None, recovery_requested=True, deadline=.1,
                clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0]+n))
        self.assertAlmostEqual(now[0], .1)
        def late():
            now[0] = 2
            return Path('/original')
        with self.assertRaises(TimeoutError):
            resolve_request_transcript(late, recovery_requested=True, deadline=1,
                                       clock=lambda: now[0])

    def test_identity_failure_and_initial_absence_are_not_polled(self):
        def changed():
            raise ValueError('identity changed')
        with self.assertRaisesRegex(ValueError, 'identity changed'):
            resolve_request_transcript(changed, recovery_requested=True, deadline=1,
                                       clock=lambda: 0)
        self.assertEqual(resolve_request_transcript(
            lambda: None, recovery_requested=False, deadline=1), (None, 1))

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


    def test_distinct_recovery_prompt_requires_terminal_and_new_native_turn(self):
        def short_resolution(transcript, **kwargs):
            kwargs['deadline'] = min(kwargs['deadline'], time.monotonic() + .3)
            return resolve_request_transcript(transcript, **kwargs)
        self.enterContext(patch('tools.standby_test_provider.resolve_request_transcript',
                                side_effect=short_resolution))
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            sid = str(uuid.uuid4())
            transcript = root/'original.jsonl'
            transcript.write_text(json.dumps(dict(type='session_meta', payload=dict(id=sid)))+'\n')
            provider = LocalProvider(root/'provider', 'Activate', recovery_prompt='任务请继续')
            self.addCleanup(provider.close)
            provider.bind(sid, transcript)
            context = ssl.create_default_context(cafile=str(provider.cert))
            def event(kind, **data):
                with transcript.open('a') as stream:
                    stream.write(json.dumps(dict(type='event_msg', payload=dict(type=kind, **data)))+'\n')
            def request(prompt):
                connection = http.client.HTTPSConnection('127.0.0.1', provider.server.server_port,
                                                        context=context, timeout=5)
                try:
                    body = dict(input=[dict(role='user', content=[dict(type='input_text', text=prompt)])])
                    connection.request('POST', '/v1/responses', json.dumps(body),
                                       {'thread-id': sid, 'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()
            self.assertEqual(request('任务请继续')[0], 400)
            event('task_started', turn_id='first')
            for _ in range(2):
                status, body = request('Activate')
                self.assertEqual(status, 200)
                self.assertIn(b'response.failed', body)
            self.assertEqual(request('任务请继续')[0], 400)
            event('task_complete', turn_id='first', error=dict(codex_error_info='rate_limit_exceeded'))
            self.assertEqual(request('任务请继续')[0], 400)
            event('task_started', turn_id='second')
            self.assertEqual(request('Activate')[0], 400)
            # One HTTPS request must survive a temporarily incomplete live
            # inventory. The original lifecycle is still checked afterward.
            observations = iter([None, None, transcript])
            with provider.lock:
                provider.sessions[sid] = lambda: next(observations)
            status, body = request('任务请继续')
            self.assertEqual(status, 200)
            self.assertIn(b'response.completed', body)
            rows = [json.loads(line) for line in
                    (provider.directory/'requests.jsonl').read_text().splitlines()]
            success = [r for r in rows if r.get('recovered_turn') == 'second']
            self.assertEqual(len(success), 1)
            self.assertEqual(success[0]['transcript_resolution_polls'], 3)
            self.assertTrue(success[0]['transcript_observed'])
            self.assertEqual(request('arbitrary')[0], 400)

    def test_visible_transcript_waits_for_late_native_turn_in_one_https_request(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sid = str(uuid.uuid4())
            path = root/'original.jsonl'
            path.write_text(json.dumps(dict(type='session_meta', payload=dict(id=sid)))+'\n')
            observed_twice = threading.Event()
            calls = []
            def observe():
                calls.append(time.monotonic())
                if len(calls) >= 3:  # Exclude bind; at least two request observations.
                    observed_twice.set()
                return path
            provider = LocalProvider(root/'provider', 'Activate', 'Continue')
            try:
                provider.bind(sid, observe)
                def event(kind, **fields):
                    with path.open('a') as stream:
                        stream.write(json.dumps(dict(type='event_msg',
                            payload=dict(type=kind, **fields)))+'\n')
                event('task_started', turn_id='first')
                event('task_complete', turn_id='first',
                      error=dict(codex_error_info='rate_limit_exceeded'))
                def request():
                    connection = http.client.HTTPSConnection('127.0.0.1',
                        provider.server.server_port,
                        context=ssl.create_default_context(cafile=str(provider.cert)), timeout=3)
                    try:
                        connection.request('POST', '/v1/responses', json.dumps(dict(input=[
                            dict(role='user', content=[dict(type='input_text', text='Continue')])])),
                            {'thread-id': sid})
                        response = connection.getresponse()
                        return response.status, response.read()
                    finally:
                        connection.close()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    pending = executor.submit(request)
                    try:
                        self.assertTrue(observed_twice.wait(2))
                        self.assertFalse(pending.done())
                        self.assertFalse((provider.directory/'requests.jsonl').exists())
                    finally:
                        event('task_started', turn_id='second')
                    status, body = pending.result(timeout=3)
                self.assertEqual(status, 200)
                self.assertIn(b'response.completed', body)
                rows = [json.loads(line) for line in
                        (provider.directory/'requests.jsonl').read_text().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(provider.counts[sid], 1)
                self.assertEqual(rows[0]['recovered_turn'], 'second')
                self.assertGreaterEqual(rows[0]['lifecycle_pending_polls'], 1)
                self.assertEqual(rows[0]['transcript_resolution_polls'], len(calls)-1)
            finally:
                provider.close()

    def test_lifecycle_wait_does_not_cache_path_or_extend_deadline(self):
        now = [0.0]
        observations = iter([Path('/first'), None, Path('/second')])
        seen = []
        def lifecycle(path):
            seen.append(path)
            return 'recovered' if path == Path('/second') else None
        diagnostic = {}
        result = resolve_request_transcript(lambda: next(observations),
            recovery_requested=True, deadline=.2, clock=lambda: now[0],
            sleep=lambda n: now.__setitem__(0, now[0]+n),
            lifecycle=lifecycle, diagnostic=diagnostic)
        self.assertEqual(result, (Path('/second'), 3))
        self.assertEqual(seen, [Path('/first'), None, Path('/second')])
        self.assertAlmostEqual(now[0], .1)
        self.assertEqual(diagnostic['lifecycle_pending_polls'], 1)

    def test_lifecycle_timeout_does_not_observe_after_deadline(self):
        now = [0.0]
        calls = []
        diagnostic = {}
        def observe():
            calls.append(now[0])
            return Path('/original')
        with self.assertRaises(TimeoutError):
            resolve_request_transcript(observe, recovery_requested=True, deadline=.1,
                clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0]+n),
                lifecycle=lambda path: None, diagnostic=diagnostic)
        self.assertEqual(calls, [0, .05])
        self.assertAlmostEqual(now[0], .1)
        self.assertEqual(diagnostic['refusal_reason'], 'lifecycle_observation_timeout')
        with self.assertRaises(TimeoutError):
            resolve_request_transcript(observe, recovery_requested=True, deadline=.1,
                clock=lambda: now[0], lifecycle=lambda path: 'second')
        self.assertEqual(calls, [0, .05])

    def test_lifecycle_parse_finishing_late_cannot_authorize_success(self):
        now = [0.0]
        diagnostic = {}
        def lifecycle(path):
            now[0] = .1
            return 'second'
        with self.assertRaises(TimeoutError):
            resolve_request_transcript(Path('/original'), recovery_requested=True,
                deadline=.1, clock=lambda: now[0], lifecycle=lifecycle, diagnostic=diagnostic)
        self.assertEqual(diagnostic['refusal_reason'], 'lifecycle_observation_late')

    def test_lifecycle_wait_rejects_identity_drift_and_invalid_history_immediately(self):
        for bad in ('identity', 'lifecycle'):
            with self.subTest(bad=bad):
                now, calls = [0.0], []
                def observe():
                    calls.append(now[0])
                    if bad == 'identity' and len(calls) == 2:
                        raise ValueError('identity changed')
                    return Path('/original')
                def lifecycle(path):
                    if bad == 'lifecycle' and len(calls) == 2:
                        raise ValueError('original turn aborted')
                diagnostic = {}
                with self.assertRaisesRegex(ValueError, 'identity changed|original turn aborted'):
                    resolve_request_transcript(observe, recovery_requested=True, deadline=1,
                        clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0]+n),
                        lifecycle=lifecycle, diagnostic=diagnostic)
                self.assertEqual(calls, [0, .05])
                self.assertEqual(diagnostic['refusal_reason'],
                    'transcript_observer_error' if bad == 'identity' else 'lifecycle_validation_failed')


if __name__ == '__main__':
    unittest.main()

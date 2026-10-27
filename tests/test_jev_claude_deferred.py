"""JEV uses the real Claude handler and its existing guarded retry route."""
import tempfile
import unittest
from unittest import mock

import ccc_jev_unknown as jev
import claude_ccc_protocol as protocol
import cmux_codex_watch as core
import tests.test_watch as tw
from tests.test_watch import (FakeClient, claude_grid_payload, claude_idle_screen,
                              process_fixture, claude_hook_event, CLAUDE_MESSAGE)


class FakeClassifier:
    def __init__(self, answer=None):
        self.answer, self.calls = answer, []

    def resolve(self, local, gates, client, model, text, provider=''):
        self.calls.append((gates, provider))
        return self.answer if gates.allow() else None


def unknown_failure(event_id):
    event = claude_hook_event(event_id, 'StopFailure', error_kind='claude_api')
    excerpt = 'odd_thing\nserver said: please slow down'
    event.update(agent_pid=1234, process_generation='fixture-birth-1234', stop_failure_error='unknown',
                 error_reason='provider_error', error_excerpt=excerpt,
                 error_evidence_hash=protocol._digest(excerpt))
    return event


class DeferredJevTests(unittest.TestCase):
    armed = tw.WatchTests._claude_armed_daemon

    def _setup(self, directory, classifier, *, composer='empty'):
        client = FakeClient(claude_grid_payload(lines=['unfinished'], completed=True, composer=composer),
                            'unfinished\n' + claude_idle_screen(),
                            top=process_fixture(('surface-uuid', 'claude')))
        daemon = self.armed(directory, client)
        self.addCleanup(daemon._process_snapshots.close)
        daemon.jev_classifier = classifier
        daemon.runtime.setdefault('surface-uuid', core.TargetRuntime(
            claude_process_pid=1234, claude_process_generation='fixture-birth-1234',
            claude_session_id='session-uuid', claude_context_composer_kind='empty'))
        return client, daemon

    def _poll(self, daemon, client, at):
        runtime = daemon.runtime['surface-uuid']
        grid = core.Grid.from_rpc(client.replay('workspace-uuid', 'surface-uuid'), 'surface-uuid')
        state = core.classify_claude_grid(grid)
        with mock.patch.object(core.time, 'time', return_value=at):
            return daemon._maybe_send_deferred_claude_stop(daemon.config['targets'][0], runtime, state, client)

    def test_pending_uses_existing_wait_then_cached_answer_retries_once(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0):
            pending = FakeClassifier(None)
            client, daemon = self._setup(d, pending)
            daemon._handle_claude_event(unknown_failure('unknown-1'), client)
            runtime = daemon.runtime['surface-uuid']
            self.assertEqual(client.sent, [])
            self.assertEqual(len(pending.calls), 1)
            self.assertTrue(pending.calls[0][0].allow())
            self.assertEqual(pending.calls[0][1], 'session-uuid|unknown-1')
            due = runtime.claude_provider_guard['retry_after']
            self.assertGreaterEqual(due - runtime.claude_deferred_since, 60)
            self._poll(daemon, client, due - .001)
            self.assertEqual(client.sent, [])
            daemon.jev_classifier = FakeClassifier('rate_limit')
            self._poll(daemon, client, due + 1)
            self._poll(daemon, client, due + 1)
            self.assertEqual([item[2] for item in client.sent], [CLAUDE_MESSAGE])
            self.assertEqual(runtime.claude_last_resume_event_id, 'unknown-1')
            self.assertEqual(daemon.jev_classifier.calls[0][1], 'session-uuid|unknown-1')

    def test_known_kind_never_calls_classifier(self):
        with tempfile.TemporaryDirectory() as d:
            c = FakeClassifier('rate_limit')
            client, daemon = self._setup(d, c)
            event = claude_hook_event('known-1', 'StopFailure', error_kind='claude_503')
            event.update(agent_pid=1234, process_generation='fixture-birth-1234')
            daemon._handle_claude_event(event, client)
            self.assertEqual(c.calls, [])

    def test_host_draft_blocks_send_and_same_event_recovers_after_clearing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0):
            client, daemon = self._setup(d, FakeClassifier(None))
            daemon._handle_claude_event(unknown_failure('draft-1'), client)
            runtime = daemon.runtime['surface-uuid']
            daemon.jev_classifier = FakeClassifier('rate_limit')
            client.payload = claude_grid_payload(lines=['unfinished'], completed=True, composer='busy')
            due = runtime.claude_provider_guard['retry_after'] + 1
            self._poll(daemon, client, due)
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.claude_deferred_event['event_id'], 'draft-1')
            client.payload = claude_grid_payload(lines=['unfinished'], completed=True, composer='empty')
            self._poll(daemon, client, due + 1)
            self._poll(daemon, client, due + 1)
            self.assertEqual([item[2] for item in client.sent], [CLAUDE_MESSAGE])

    def test_first_event_reads_host_draft_instead_of_old_empty_telemetry(self):
        with tempfile.TemporaryDirectory() as d:
            c = jev.JevUnknownClassifier(lambda req, timeout: self.fail('unexpected API'),
                start=False, is_enabled=lambda: True)
            self.addCleanup(c.stop)
            client, daemon = self._setup(d, c, composer='busy')
            daemon._handle_claude_event(unknown_failure('first-draft'), client)
            self.assertEqual(c.queue.qsize(), 0)
            self.assertEqual(client.sent, [])
            runtime = daemon.runtime['surface-uuid']
            self._poll(daemon, client, runtime.claude_provider_guard['retry_after'] + 1)
            self.assertEqual(c.queue.qsize(), 0)
            self.assertEqual(client.sent, [])
            self.assertEqual(runtime.claude_deferred_event['event_id'], 'first-draft')

    def test_missing_answer_adds_no_wait_after_provider_budget_is_due(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(core, 'CLAUDE_EVENT_PREFLIGHT_SETTLE_SEC', 0):
            client, daemon = self._setup(d, FakeClassifier(None))
            daemon._handle_claude_event(unknown_failure('timeout-1'), client)
            runtime = daemon.runtime['surface-uuid']
            due = runtime.claude_provider_guard['retry_after'] + 1
            runtime.claude_deferred_since = due - .001
            self._poll(daemon, client, due)
            self._poll(daemon, client, due)
            self.assertEqual([item[2] for item in client.sent], [CLAUDE_MESSAGE])


if __name__ == '__main__':
    unittest.main()

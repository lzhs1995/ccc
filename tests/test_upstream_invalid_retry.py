"""Measured masked upstream errors retry without borrowing input authority."""
import json
import time
import unittest
from unittest.mock import patch

import ccc_codex_queue as queue
import ccc_provider_retry as provider
import cmux_codex_watch as core
from tests.test_codex_resource_mismatch import screen
from tests import test_private_check as private_cases
from tests.test_watch import grid_payload


ERROR = {'error': {'message': 'Upstream rejected the request as invalid '
    '[trace_id=a89742bf0a6c1c7f222c3f974612f0e5]',
    'type': 'invalid_request_error', 'param': '', 'code': None}}
MESSAGE = json.dumps(ERROR)


class UpstreamInvalidClassificationTests(unittest.TestCase):
    def test_exact_envelope_agrees_across_native_and_viewport(self):
        for prefix in ('', 'stream disconnected before completion: ',
                       'unexpected status 400 Bad Request: '):
            message = prefix + MESSAGE
            with self.subTest(prefix=prefix):
                self.assertEqual(provider.structured_error_type(message), 'upstream_invalid')
                self.assertTrue(queue._retryable_completed_message(message))
                self.assertEqual(core._match_error_block('■ ' + message), 'upstream_invalid')
                for wrapped in (False, True):
                    state = screen(message, wrapped=wrapped)
                    self.assertEqual((state.kind,state.error_type),
                                     ('recoverable_error','upstream_invalid'))

    def test_concrete_error_fields_and_other_messages_stay_blocked(self):
        changes = ({'param':'model'}, {'code':'invalid_encrypted_content'},
            {'code':'insufficient_quota'}, {'code':'DeploymentNotFound'},
            {'code':'content_policy_violation'}, {'param':None}, {'status':403},
            {'message':'Invalid model parameter'},
            {'message':'resource has been temporarily blocked because it violates our content policy'},
            {'message':ERROR['error']['message'].replace('a89742','z89742')})
        for change in changes:
            message = json.dumps({'error':dict(ERROR['error'],**change)})
            with self.subTest(change=change):
                self.assertIn(provider.structured_error_type(message), provider.BLOCKED)
                self.assertFalse(queue._retryable_completed_message(message))
        for prefix in ('unexpected status 401 Unauthorized: ', 'HTTP 403: '):
            with self.subTest(prefix=prefix):
                self.assertFalse(queue._retryable_completed_message(prefix + MESSAGE))
        for extra in ({'status':403},{'example':True}):
            with self.subTest(extra=extra):
                self.assertIn(provider.structured_error_type(json.dumps(dict(ERROR,**extra))),provider.BLOCKED)

    def test_quoted_history_and_user_draft_do_not_authorize_retry(self):
        for prefix in ('Documentation: ', '■ Example: ', 'Quoted: '):
            with self.subTest(prefix=prefix):
                self.assertFalse(queue._retryable_completed_message(prefix+MESSAGE))
        self.assertEqual(screen(MESSAGE,composer='busy').kind,'composer_busy')


class UpstreamInvalidDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.case = private_cases.PrivateCheckDeliveryTests('test_first_failure_sends_short_check_with_prior_durable_record')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        case = self.case
        case.append_turn('ordinary',time.time()-5,message='Fix the current task')
        records = [json.loads(line) for line in case.path.read_text().splitlines()]
        records[-1]['payload']['error'] = {'message':MESSAGE}
        case.path.write_text('\n'.join(json.dumps(r) for r in records)+'\n')
        case.client.payload = grid_payload([],error=MESSAGE,columns=180)
        case.client.payload['render_grid']['surface_id'] = case.sid
        case.state = core.classify_grid(core.Grid.from_rpc(case.client.payload,case.sid))
        self.now = [time.time()]
        case.daemon._provider_retry = provider.ProviderRetryStore(
            case.daemon._provider_retry.path,clock=lambda:self.now[0],jitter=lambda:0)
        turn = case.read_turn()
        case.daemon._provider_retry.observe(case.session,'test-provider',turn['turn_id'],
            'upstream_invalid',MESSAGE,turn['at'])
        self.now[0] += 16

    def test_original_backoff_is_preserved(self):
        self.now[0] -= 16
        self.case.send()
        self.assertEqual(self.case.client.sent,[])

    def test_ordinary_failed_turn_sends_the_exact_prompt_once(self):
        case = self.case
        case.send()
        self.assertEqual(case.client.sent,[(case.wid,case.sid,'任务请继续')])
        case.runtime.awaiting = False
        case.runtime.last_send_at = 0
        case.send()
        self.assertEqual(len(case.client.sent),1)

    def test_unknown_original_input_is_not_resent(self):
        case = self.case
        with patch.object(case.client,'send',side_effect=core.UncertainDeliveryError('lost reply')) as send:
            case.send()
            case.runtime.awaiting = False
            case.runtime.last_send_at = 0
            case.send()
        self.assertEqual(send.call_count,1)

    def test_new_running_turn_supersedes_visible_error(self):
        case = self.case
        case.append([{'type':'event_msg','timestamp':case.iso(time.time()),
                      'payload':{'type':'task_started','turn_id':'new-work'}}])
        case.send()
        self.assertEqual(case.client.sent,[])

    def test_operator_draft_is_preserved(self):
        case = self.case
        frame = grid_payload([],error=MESSAGE,composer='busy',columns=180)
        frame['render_grid']['surface_id'] = case.sid
        case.client.payload = frame
        case.state = core.classify_grid(core.Grid.from_rpc(frame,case.sid))
        case.send()
        self.assertEqual(case.client.sent,[])
        self.assertEqual(case.client.payload,frame)

    def test_native_transport_rechecks_draft_after_stale_classification(self):
        from tests import test_input_not_sent as input_cases
        case = input_cases.InputNotSentTests()
        case.setUp()
        try:
            fixture = case.f
            records = [json.loads(line) for line in fixture.path.read_text().splitlines()]
            records[-1]['payload']['error'] = {'message':MESSAGE}
            fixture.path.write_text('\n'.join(json.dumps(r) for r in records)+'\n')
            fixture.client.payload = grid_payload([],error=MESSAGE,columns=180)
            fixture.client.payload['render_grid']['surface_id'] = fixture.sid
            fixture.state = core.classify_grid(core.Grid.from_rpc(fixture.client.payload,fixture.sid))
            turn = fixture.daemon.codex_queue_recovery.current_turn(fixture.target)
            fixture.daemon._provider_retry.observe(turn['session_id'],'test-provider',
                turn['turn_id'],'upstream_invalid',MESSAGE,turn['at'])
            case.now[0] += 16
            # The real socket reader reports a new operator draft after the
            # initial failure classification, before any terminal mutation.
            self.assertTrue(case.deny)
            case.send()
            self.assertIn('terminal.replay',case.rows)
            self.assertEqual(case.pending,[])
        finally:
            case.doCleanups()


if __name__ == '__main__':
    unittest.main()

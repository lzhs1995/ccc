"""Explicit task Stop enforcement over real registration and reply files."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import cmux_workflow_advisory as wrapper
import cmux_native_delivery as native
import executor_proactive_hook as hook
import executor_ready as ready
import executor_reask as reask
import executor_reask_lifecycle as lifecycle
import executor_reask_scheduler as scheduler
import test_executor_reask_reservation_inquiries as fixtures
from test_executor_reask_identity import CALLER, SUP, SESSION


class ProactiveHookTests(unittest.TestCase):
    setUp = fixtures.ReservationInquiryIntegrationTests.setUp
    select_provider = fixtures.ReservationInquiryIntegrationTests.select_provider
    write_registration = fixtures.ReservationInquiryIntegrationTests.write_registration
    process = fixtures.ReservationInquiryIntegrationTests.process
    ps = fixtures.ReservationInquiryIntegrationTests.ps
    tree = fixtures.ReservationInquiryIntegrationTests.tree
    live = fixtures.ReservationInquiryIntegrationTests.live
    snapshot = fixtures.ReservationInquiryIntegrationTests.snapshot
    files = fixtures.ReservationInquiryIntegrationTests.files
    reserve = fixtures.ReservationInquiryIntegrationTests.reserve

    def begin(self, proactive=True):
        auth = self.home / 'authorization.txt'
        auth.write_text('Continue this task; ask every60seconds until actual reply, '
                        'including reservations. Enforce using a task-scoped hook.')
        continuation = dict(enabled=True, owner=SUP, overall_task='original task',
                            authorization_file=str(auth), reservation_inquiries=True)
        if proactive:
            continuation['proactive_hook'] = True
        return scheduler.start(self.live(), task_id='proactive', now=1000.,
                               authenticate_executor=True, continuing=True,
                               continuation=continuation)

    def payload(self):
        return dict(hook_event_name='Stop', session_id=SESSION,
                    transcript_path=str(self.log), stop_hook_active=True)

    def call(self, payload=None):
        with patch.dict(os.environ, CMUX_SURFACE_ID=CALLER):
            return hook.decide(self.payload() if payload is None else payload, now=1001.)

    def episode_files(self):
        return {k: v for k, v in self.files().items() if not k.startswith('executor-proactive-v1/')}

    def test_current_authorized_original_hook_only_writes_its_cadence_no_input(self):
        self.begin()
        before = self.files()
        result = self.call()
        self.assertEqual(result['decision'], 'block')
        self.assertIn('run --caller-uuid', result['reason'])
        self.assertIn('--max-seconds 60', result['reason'])
        self.assertEqual(self.episode_files(), before)
        self.assertNotIn('rtk proxy', result['reason'])
        self.assertFalse((ready.state_root() / 'message-dispatch-v1').exists())

    def test_legacy_registration_never_acquires_hook_authority(self):
        state = self.begin(False)
        self.assertNotIn('proactive_hook', lifecycle.binding(state['continuation']))
        self.assertIsNone(self.call())

    def test_user_stop_and_changed_authorization_end_enforcement(self):
        state = self.begin()
        Path(state['continuation']['authorization']['path']).write_text('revoked')
        self.assertIsNone(self.call())
        ready.write_json(reask.stop_file(CALLER), dict(reason='USER_STOP'))
        self.assertIsNone(self.call())

    def test_wrong_session_child_or_other_event_gets_no_enforcement(self):
        self.begin()
        for update in ({'agent_id': 'child'}, {'hook_event_name': 'PostToolUse'}):
            self.assertIsNone(self.call(dict(self.payload(), **update)))
        with self.assertRaises(ValueError):
            self.call(dict(self.payload(), session_id='another-session'))

    def test_real_mailbox_reply_requires_original_full_read_consume(self):
        self.begin()
        scheduler.sweep(now=1001.)
        request = ready.read_json(reask.inbox_path(reask.load(CALLER)))
        body = dict(request['reply_template'], trigger='Read and continue',
                    next_due_epoch=1062., deadline_epoch=1122.)
        ready.write_json(Path(request['reply_mailbox']), body)
        scheduler.sweep(now=1002.)
        state = reask.load(CALLER)
        before = self.files()
        result = self.call()
        self.assertIn(state['reply']['body_sha256'], result['reason'])
        self.assertIn('consume', result['reason'])
        self.assertEqual(self.episode_files(), before)

    def test_repeated_empty_stop_is_throttled_without_ending_task(self):
        self.begin()
        clock = [1001.]
        waits = []
        def sleep(seconds):
            # Waiting never owns the episode lock.
            with reask.episode_lock(CALLER):
                pass
            clock[0] += seconds
            waits.append(seconds)
        with patch.dict(os.environ, CMUX_SURFACE_ID=CALLER):
            for expected in (1001., 1056., 1111.):
                result = hook.decide(self.payload(), clock=lambda: clock[0],
                                     monotonic=lambda: clock[0], sleep=sleep)
                self.assertEqual(result['decision'], 'block')
                self.assertEqual(clock[0], expected)
        self.assertEqual(sum(waits), 110.)
        self.assertEqual(reask.load(CALLER)['state'], reask.WAITING)
        self.assertFalse((ready.state_root() / 'message-dispatch-v1').exists())

    def test_stop_during_cadence_wait_returns_promptly_without_releasing_task(self):
        _, reservation_path = self.reserve()
        reserved_bytes = reservation_path.read_bytes()
        self.begin()
        clock = [1001.]
        def sleep(seconds):
            clock[0] += seconds
            ready.write_json(reask.stop_file(CALLER), dict(reason='USER_STOP'))
        with patch.dict(os.environ, CMUX_SURFACE_ID=CALLER):
            hook.decide(self.payload(), clock=lambda: clock[0])
            result = hook.decide(self.payload(), clock=lambda: clock[0],
                                 monotonic=lambda: clock[0], sleep=sleep)
        self.assertIsNone(result)
        self.assertEqual(clock[0], 1002.)
        self.assertEqual(reservation_path.read_bytes(), reserved_bytes)

    def test_episode_busy_is_distinct_from_binding_failure_in_wrapper_audit(self):
        import cmux_hook_runtime_audit as audit
        self.begin()
        with patch.object(hook, 'decide', side_effect=reask.EpisodeBusy()), \
                patch.object(audit, 'record') as record, \
                patch.object(sys, 'argv', ['hook', '--hook', 'cmux_executor_idle_guard.py']), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(self.payload()))):
            wrapper.main()
        self.assertEqual(record.call_args.args[1], 'TASK_CONTINUATION_EPISODE_BUSY')

    def test_new_mailbox_reply_interrupts_wait_before_scheduler_promotes_answered(self):
        self.begin()
        scheduler.sweep(now=1001.)
        request = ready.read_json(reask.inbox_path(reask.load(CALLER)))
        clock = [1002.]
        def sleep(seconds):
            clock[0] += seconds
            ready.write_json(Path(request['reply_mailbox']), dict(request['reply_template'],
                trigger='Continue the task', next_due_epoch=1063., deadline_epoch=1123.))
        with patch.dict(os.environ, CMUX_SURFACE_ID=CALLER):
            hook.decide(self.payload(), clock=lambda: clock[0])
            result = hook.decide(self.payload(), clock=lambda: clock[0],
                                 monotonic=lambda: clock[0], sleep=sleep)
        self.assertIn('mailbox reply appeared', result['reason'])
        self.assertEqual(clock[0], 1003.)
        self.assertEqual(reask.load(CALLER)['state'], reask.WAITING)

    def test_new_pending_handshake_interrupts_wait_without_ack_or_replay(self):
        marker, path = self.reserve()
        self.begin()
        clock = [1001.]
        def sleep(seconds):
            clock[0] += seconds
            artifact = self.home / 'formal-artifacts'
            artifact.mkdir(exist_ok=True)
            marker['artifact_root'] = str(artifact)
            marker['participants'][1].update(native_session_id=SESSION)
            ready.write_json(path, marker)
            ready.write_json(artifact / 'handshake-receipt.json', dict(executors=[dict(
                task_id=marker['task_id'], executor=CALLER, executor_ack=False, ack_nonce='1234abcd')]))
        with patch.dict(os.environ, CMUX_SURFACE_ID=CALLER):
            hook.decide(self.payload(), clock=lambda: clock[0])
            result = hook.decide(self.payload(), clock=lambda: clock[0],
                                 monotonic=lambda: clock[0], sleep=sleep)
        self.assertIn('1234abcd', result['reason'])
        self.assertEqual(clock[0], 1002.)
        self.assertFalse((ready.state_root() / 'message-dispatch-v1').exists())

    def test_revocation_during_wait_returns_promptly(self):
        state = self.begin()
        clock = [1001.]
        def sleep(seconds):
            clock[0] += seconds
            Path(state['continuation']['authorization']['path']).write_text('revoked')
        with patch.dict(os.environ, CMUX_SURFACE_ID=CALLER):
            hook.decide(self.payload(), clock=lambda: clock[0])
            self.assertIsNone(hook.decide(self.payload(), clock=lambda: clock[0],
                                         monotonic=lambda: clock[0], sleep=sleep))
        self.assertEqual(clock[0], 1002.)

    def test_reservation_allows_authorized_foreground_status_only(self):
        self.reserve()
        self.begin()
        sender = Mock(return_value=dict(outcome='TEST_ONLY', terminal=False))
        state = reask.tick(CALLER, 1001., send=sender)
        sender.assert_called_once()
        self.assertEqual(state['asks'][-1]['inquiry_kind'], 'RESERVATION_STATUS')
        self.assertEqual(len(state['paused_tasks']), 1)
        # The launchd scheduler never becomes a sender, even for new authority.
        with patch.object(reask, 'default_send', side_effect=AssertionError('scheduler sent')):
            result = scheduler.sweep(now=1061.)
        self.assertEqual(result['input_operations'], 0)
        self.assertEqual(result['file_posts'], 1)

    def test_pending_formal_inputs_do_not_block_reverse_status_or_change_originals(self):
        _, reservation = self.reserve()
        self.begin()
        # Real files model supervisor -> executor pending work. The reverse
        # inquiry's sender, binding and file channels run; only native endpoint
        # inventory and actual transport are external I/O substitutes.
        handshake = self.home / 'handshake-receipt.json'
        pack_attempt = self.home / 'task-attempt.json'
        ready.write_json(handshake, dict(status='PENDING', nonce='original-handshake'))
        ready.write_json(pack_attempt, dict(phase='NATIVE_INPUT_INTENT', nonce='original-pack'))
        frozen = {p: p.read_bytes() for p in (reservation, handshake, pack_attempt)}
        def advance(bridge, supervisor, caller, ask, *, allow_send=False):
            self.assertEqual((supervisor, caller, allow_send), (SUP, CALLER, True))
            request = ready._original_request(caller, supervisor, ask['marker'])
            import cmux_prompt_reference as reference
            body = reference.read_body(reference.wire_reference(request['text']))[0]
            self.assertIn('RESERVATION_STATUS', body)
            self.assertIn('neither dispatches work nor releases a reservation', body)
            ask.update(outcome='CONFIRMED', receipt=dict(native_received_at_epoch=1001.))
        with patch.object(native, 'bind_target', return_value=dict(provider='codex')) as bind, \
                patch.object(ready, 'advance_ask', side_effect=advance) as transport:
            state = reask.tick(CALLER, 1001.)
        self.assertEqual(state['asks'][-1]['outcome'], 'CONFIRMED')
        self.assertEqual(bind.call_args.args[1], SUP)
        transport.assert_called_once()
        self.assertEqual(len(state['paused_tasks']), 1)
        self.assertEqual(frozen, {p: p.read_bytes() for p in frozen})

    def test_reservation_keeps_unconfirmed_reverse_input_original_only(self):
        _, reservation = self.reserve()
        self.begin()
        state = reask.load(CALLER)
        pending = dict(marker='ORIGINAL_PENDING', origin=reask.origin(state),
                       outcome='NATIVE_PENDING', terminal=True)
        ready.write_record(CALLER, pending['marker'], 'STATUS: ORIGINAL_PENDING original inquiry',
                           SUP, state['task_id'], state['episode_id'],
                           controller=state['controller'], workspace=state['workspace_uuid'])
        state['terminal_pending'] = copy.deepcopy(pending)
        reask.save(state)
        frozen = reservation.read_bytes()
        with patch.object(reask, '_advance_original', return_value=False) as reconcile, \
                patch.object(ready, 'advance_ask', side_effect=AssertionError('new input')):
            state = reask.tick(CALLER, 1001.)
        self.assertEqual(state['asks'][-1]['outcome'], 'SKIPPED_UNVERIFIED')
        self.assertEqual(state['terminal_pending'], pending)
        self.assertEqual(state['file_posts'], 1)
        self.assertEqual(reservation.read_bytes(), frozen)
        reconcile.assert_called_once()

    def test_installed_wrapper_enforces_only_the_single_scoped_stop_entry(self):
        self.begin()
        with patch.dict(os.environ, CMUX_SURFACE_ID=CALLER), \
                patch.object(sys, 'argv', ['hook', '--hook', 'cmux_executor_idle_guard.py']), \
                patch.object(sys, 'stdin', io.StringIO(json.dumps(self.payload()))), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wrapper.main(), 0)
        self.assertEqual(json.loads(output.getvalue())['decision'], 'block')


if __name__ == '__main__':
    unittest.main()

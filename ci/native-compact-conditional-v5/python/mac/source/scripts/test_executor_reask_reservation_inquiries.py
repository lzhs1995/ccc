"""Temporary registration/consumption integration; no live input or jobs."""
import copy
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import hashlib
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import executor_ready as ready
import executor_reask as reask
import executor_reask_lifecycle as lifecycle
import executor_reask_scheduler as scheduler
import test_executor_reask_identity as fixtures

CALLER, SUP, WORKSPACE = fixtures.CALLER, fixtures.SUP, fixtures.WORKSPACE
SUCCESSOR = '99999999-9999-4999-8999-999999999999'


class ReservationInquiryIntegrationTests(unittest.TestCase):
    # Share fixture helpers without inheriting/re-running the identity suite.
    setUp = fixtures.ExecutorIdentityIntegrationTests.setUp
    select_provider = fixtures.ExecutorIdentityIntegrationTests.select_provider
    write_registration = fixtures.ExecutorIdentityIntegrationTests.write_registration
    process = fixtures.ExecutorIdentityIntegrationTests.process
    ps = fixtures.ExecutorIdentityIntegrationTests.ps
    tree = fixtures.ExecutorIdentityIntegrationTests.tree
    live = fixtures.ExecutorIdentityIntegrationTests.live
    snapshot = fixtures.ExecutorIdentityIntegrationTests.snapshot
    files = fixtures.ExecutorIdentityIntegrationTests.files

    def begin(self, *, enabled=True, until=None):
        self.auth = self.home / 'authorization.txt'
        self.auth.write_text(
            'Continue the original task. While formally reserved, ask the same '
            'supervisor by file every 60 seconds. Preserve terminal attempts '
            'and task reservations until their exact release.\n')
        self.continuation = dict(enabled=True, owner=SUP, overall_task='original overall task',
                                 authorization_file=str(self.auth), until_epoch=until)
        if enabled:
            self.continuation['reservation_inquiries'] = True
        return scheduler.start(self.live(), task_id='reservation-integration', now=1000.,
                               authenticate_executor=True, continuing=True,
                               continuation=self.continuation)

    def reserve(self, *, owner=SUP, task='formal-task', collaboration='formal-collaboration',
                at=999., notify=False):
        marker = dict(workspace_uuid=WORKSPACE, collaboration_id=collaboration, task_id=task,
                      armed_at=datetime.fromtimestamp(at, timezone.utc).isoformat(),
                      participants=[dict(role='supervisor', surface_uuid=owner),
                                    dict(role='executor', surface_uuid=CALLER)])
        path = self.home / 'markers' / WORKSPACE / (collaboration + '.json')
        ready.write_json(path, marker)
        if notify:
            lifecycle.task_boundary(marker, 'RESERVED', now=max(1000., at))
        return marker, path

    @contextmanager
    def files_only(self):
        with ExitStack() as stack:
            mocks = [stack.enter_context(patch.object(module, name, side_effect=AssertionError(
                'reserved file inquiry invoked ' + name))) for module, name in (
                    (ready, '_bridge'), (ready, 'Tail'), (reask, 'idle_binding'),
                    (reask, '_advance_original'))]
            sender = Mock(side_effect=AssertionError('reserved inquiry sent terminal input'))
            yield sender
            for operation in [sender, *mocks]:
                operation.assert_not_called()

    def preserve_pending(self):
        journal = ready.state_root() / 'original-pending-attempt.json'
        journal.write_bytes(b'{"phase":"NATIVE_INPUT_INTENT","outcome":"UNKNOWN"}\n')
        state = reask.load(CALLER)
        pending = dict(marker='ORIGINAL_UNKNOWN', terminal=True, outcome='UNKNOWN',
                       journal=str(journal), controller=copy.deepcopy(state['controller']),
                       nonce='original-nonce')
        state['terminal_pending'] = pending
        state['transcript'] = str(self.log)
        state['tail_position'] = dict(offset=0)
        reask.save(state)
        return pending, journal, journal.read_bytes(), self.log.read_bytes()

    def assert_preserved(self, frozen):
        pending, journal, journal_raw, transcript_raw = frozen
        state = reask.load(CALLER)
        self.assertEqual(state['terminal_pending'], pending)
        self.assertEqual(journal.read_bytes(), journal_raw)
        self.assertEqual(self.log.read_bytes(), transcript_raw)
        self.assertEqual(len(state['paused_tasks']), 1)
        for key in ('terminal_sends', 'terminal_attempts', 'native_receipts'):
            self.assertEqual(state[key], 0)

    def answer(self, *, now, status='WAITING_DEPENDENCY'):
        request = ready.read_json(reask.inbox_path(reask.load(CALLER)))
        body = dict(request['reply_template'], status=status, trigger='等待主管核收原任务完整报告。',
                    next_due_epoch=now + 60., deadline_epoch=now + 120.)
        ready.write_json(Path(request['reply_mailbox']), body)
        with self.files_only():
            scheduler.sweep(now=now)
        state = reask.load(CALLER)
        self.assertEqual(state['state'], reask.ANSWERED)
        self.assertEqual(state['reply']['body'], body)
        return state

    def test_enrollment_during_reservation_posts_every_sixty_seconds(self):
        self.reserve()
        state = self.begin()
        self.assertTrue(scheduler.registered(state))
        original = copy.deepcopy(state['controller'])
        with self.files_only():
            for now, posts in ((1001., 1), (1060., 0), (1061., 1), (1062., 0)):
                with self.subTest(now=now):
                    result = scheduler.sweep(now=now)
                    self.assertEqual(result['status'], 'SWEPT')
                    self.assertEqual(result['file_posts'], posts)
                    self.assertEqual(result['input_operations'], 0)
        state = reask.load(CALLER)
        request = ready.read_json(reask.inbox_path(state))
        self.assertEqual(state['ask_count'], 2)
        self.assertEqual([a['at_epoch'] for a in state['asks']], [1001., 1061.])
        self.assertEqual(request['inquiry_kind'], 'RESERVATION_STATUS')
        self.assertIn('requests RESERVATION_STATUS', request['request'])
        self.assertIn('neither dispatches work nor releases a reservation', request['request'])
        self.assertEqual(request['reservations'], state['paused_tasks'])
        self.assertEqual(state['controller'], original)
        self.assertTrue(lifecycle.binding(state['continuation'])['reservation_inquiries'])

    def test_foreground_tick_and_run_preserve_unknown_without_input_or_wait(self):
        self.reserve()
        self.begin()
        frozen = self.preserve_pending()
        with self.files_only() as sender:
            first = reask.tick(CALLER, 1001., send=sender)
            self.assertEqual(first['file_posts'], 1)
            sleep = Mock(side_effect=AssertionError('reserved runner kept executor busy'))
            result = reask.run(CALLER, clock=lambda: 1061., sleep=sleep, send=sender)
            sleep.assert_not_called()
        self.assertEqual(result['file_posts'], 2)
        self.assertEqual(result['run_return_reason'], 'FORMAL_RESERVATION_WAIT')
        self.assertFalse(result['wait_boundary']['idle_observed'])
        self.assert_preserved(frozen)

    def test_scheduler_preserves_original_pending_and_journal(self):
        self.reserve()
        self.begin()
        frozen = self.preserve_pending()
        with self.files_only():
            scheduler.sweep(now=1001.)
            scheduler.sweep(now=1061.)
        self.assert_preserved(frozen)

    def test_two_reply_read_consume_rearm_cycles_keep_reservation(self):
        self.reserve()
        self.begin()
        frozen = self.preserve_pending()
        receipts = []
        with self.files_only():
            scheduler.sweep(now=1001.)
        for index, detected in enumerate((1002., 1103.), 1):
            state = self.answer(now=detected)
            count = state['ask_count']
            with self.files_only() as sender:
                scheduler.sweep(now=detected + 80.)
                reask.tick(CALLER, detected + 90., send=sender)
            self.assertEqual(reask.load(CALLER)['ask_count'], count)
            before = self.files()
            with self.assertRaisesRegex(ValueError, 'exact --reply-sha256'):
                lifecycle.consume(CALLER, '0' * 64, now=detected + 100.)
            self.assertEqual(self.files(), before)
            consumed = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=detected + 100.)
            self.assertEqual(consumed['consume_result']['status'], 'REPLY_CONSUMED')
            self.assertEqual(consumed['rearm_count'], index)
            receipt = ready.read_json(Path(consumed['consume_result']['receipt']))
            self.assertEqual(receipt['body'], state['reply']['body'])
            self.assertEqual(receipt['executor_authentication']['session_id'], fixtures.SESSION)
            receipts.append(consumed['consume_result']['receipt'])
            self.assert_preserved(frozen)
            with self.files_only():
                scheduler.sweep(now=detected + 100.)
                scheduler.sweep(now=detected + 101.)
            self.assertEqual(reask.load(CALLER)['ask_count'], count + 1)
        self.assertEqual(len(set(receipts)), 2)
        self.assertEqual(reask.load(CALLER)['rearm_count'], 2)

    def test_reply_cannot_release_and_only_exact_release_clears_reservation(self):
        marker, path = self.reserve()
        self.begin()
        with self.files_only():
            scheduler.sweep(now=1001.)
        state = self.answer(now=1002., status='DISPATCHED')
        lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        self.assertEqual(len(reask.load(CALLER)['paused_tasks']), 1)
        path.unlink()  # Missing/expired marker is not a RELEASE event.
        with self.files_only():
            scheduler.sweep(now=5000.)
        reserved = copy.deepcopy(reask.load(CALLER)['paused_tasks'])
        for delta in (dict(task_id='other-task'), dict(collaboration_id='other-collaboration'),
                      dict(workspace_uuid=SUCCESSOR), dict(participants=[
                          dict(role='supervisor', surface_uuid=SUCCESSOR),
                          dict(role='executor', surface_uuid=CALLER)])):
            with self.subTest(delta=delta):
                lifecycle.task_boundary(dict(marker, **delta), 'RELEASED', now=5001.)
                self.assertEqual(reask.load(CALLER)['paused_tasks'], reserved)
        lifecycle.task_boundary(marker, 'RELEASED', now=5002.)
        self.assertEqual(reask.load(CALLER)['paused_tasks'], [])
        self.assertEqual(reask.load(CALLER)['last_task_boundary']['event'], 'RELEASED')

    def test_operator_stop_ends_inquiries_without_releasing_task(self):
        self.reserve()
        self.begin()
        with self.files_only():
            scheduler.sweep(now=1001.)
        reask.stop(CALLER)
        with self.files_only():
            result = scheduler.sweep(now=1061.)
        state = reask.load(CALLER)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(state['state'], reask.STOPPED)
        self.assertEqual(state['exit_reason'], 'OPERATOR_STOP')
        self.assertEqual(len(state['paused_tasks']), 1)

    def test_stop_resume_reauthenticates_and_preserves_reservation_and_pending(self):
        self.begin(enabled=False)
        self.reserve(at=1001., notify=True)
        frozen = self.preserve_pending()
        with self.files_only():
            reask.stop(CALLER)
        previous = reask.state_path(CALLER).read_bytes()
        registration = scheduler.enrollment_path(CALLER).read_bytes()
        stop = reask.stop_file(CALLER).read_bytes()
        changed = dict(self.continuation, reservation_inquiries=True)
        state = scheduler.start(self.live(), task_id='reservation-integration', now=1003.,
                                authenticate_executor=True, continuing=True, continuation=changed,
                                resume_stopped=True)
        self.assertTrue(scheduler.registered(state))
        self.assertEqual(state['executor_identity']['session_id'], fixtures.SESSION)
        self.assertNotEqual(state['episode_id'], ready.read_json(
            next(p for p in scheduler.root().glob('transactions/*/*/previous-state.json')
                 if p.read_bytes() == previous))['episode_id'])
        preserved = next(p.parent for p in scheduler.root().glob(
            'transactions/*/*/previous-state.json') if p.read_bytes() == previous)
        self.assertEqual((preserved / 'previous-enrollment.json').read_bytes(), registration)
        self.assertEqual((preserved / 'previous-stop.json').read_bytes(), stop)
        self.assertFalse(reask.stop_file(CALLER).exists())
        self.assert_preserved(frozen)
        with self.files_only():
            for now, posts in ((1004., 1), (1063., 0), (1064., 1)):
                result = scheduler.sweep(now=now)
                self.assertEqual(result['file_posts'], posts)
                self.assertEqual(result['input_operations'], 0)
        self.assertEqual(reask.load(CALLER)['ask_count'], 2)
        self.assert_preserved(frozen)

    def test_stop_resume_keeps_recorded_reservation_when_marker_is_missing(self):
        self.begin(enabled=False)
        marker, path = self.reserve(at=1001., notify=True)
        frozen = self.preserve_pending()
        with self.files_only():
            reask.stop(CALLER)
        path.unlink()  # Disappearance is not an exact RELEASE.
        changed = dict(self.continuation, reservation_inquiries=True)
        state = scheduler.start(self.live(), task_id='reservation-integration', now=1003.,
                                authenticate_executor=True, continuing=True, continuation=changed,
                                resume_stopped=True)
        self.assert_preserved(frozen)
        with self.files_only():
            result = scheduler.sweep(now=1004.)
        request = ready.read_json(reask.inbox_path(reask.load(CALLER)))
        self.assertEqual(result['file_posts'], 1)
        self.assertEqual(request['inquiry_kind'], 'RESERVATION_STATUS')
        self.assertEqual(request['reservations'], state['paused_tasks'])
        self.assert_preserved(frozen)

    def test_stop_resume_requires_explicit_flag_and_original_executor(self):
        self.begin(enabled=False)
        self.reserve(at=1001., notify=True)
        frozen = self.preserve_pending()
        with self.files_only():
            reask.stop(CALLER)
        changed = dict(self.continuation, reservation_inquiries=True)
        before = self.files()
        result = scheduler.start(self.live(), task_id='reservation-integration', now=1003.,
                                 authenticate_executor=True, continuing=True, continuation=changed)
        self.assertEqual(result['state'], reask.STOPPED)
        self.assertEqual(self.files(), before)
        self.processes[fixtures.SHELL_PID]['executable'] = '/test/foreign-python'
        with self.assertRaises(fixtures.REFUSED):
            scheduler.start(self.live(), task_id='reservation-integration', now=1003.,
                            authenticate_executor=True, continuing=True, continuation=changed,
                            resume_stopped=True)
        self.assertEqual(self.files(), before)
        self.assert_preserved(frozen)

    def test_resume_preview_cannot_publish_past_operator_stop(self):
        self.begin()
        marker, _ = self.reserve(at=1001., notify=True)
        with self.files_only():
            reask.stop(CALLER)
        before = self.files()
        state = reask.load(CALLER)
        for call in (
                lambda: lifecycle.apply_boundary(state, marker, 'RESERVED', 1003.,
                                                 preview_resume=True),
                lambda: lifecycle.observe_formal_tasks(state, 1003., preview_resume=True)):
            with self.assertRaisesRegex(ValueError, 'unpublished registration preview'):
                call()
        self.assertEqual(self.files(), before)


    def test_authorization_change_stops_reserved_inquiries(self):
        self.reserve()
        self.begin()
        with self.files_only():
            scheduler.sweep(now=1001.)
        self.auth.write_text('Authorization revoked.')
        with self.files_only():
            result = scheduler.sweep(now=1061.)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(reask.load(CALLER)['exit_reason'], 'AUTHORIZATION_CHANGED')
        self.assertEqual(len(reask.load(CALLER)['paused_tasks']), 1)

    def test_expiry_stops_reserved_inquiries(self):
        self.reserve()
        self.begin(until=1061.)
        with self.files_only():
            scheduler.sweep(now=1001.)
            result = scheduler.sweep(now=1061.)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(reask.load(CALLER)['exit_reason'], 'AUTHORIZATION_EXPIRED')
        self.assertEqual(len(reask.load(CALLER)['paused_tasks']), 1)

    def test_new_supervisor_reservation_ends_original_inquiries(self):
        self.reserve()
        self.begin()
        with self.files_only():
            scheduler.sweep(now=1001.)
        self.reserve(owner=SUCCESSOR, collaboration='new-owner', task='new-task', at=1002.)
        with self.files_only():
            result = scheduler.sweep(now=1061.)
        state = reask.load(CALLER)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(state['exit_reason'], 'FORMAL_OWNER_CHANGED')
        self.assertEqual(state['supervisor'], SUP)
        self.assertEqual(state['formal_successor']['supervisor'], SUCCESSOR)

    def test_default_authorization_still_pauses_existing_episode(self):
        self.begin(enabled=False)
        self.reserve(at=1001., notify=True)
        with self.files_only() as sender:
            state = reask.tick(CALLER, 1002., send=sender)
            result = scheduler.sweep(now=1062.)
        self.assertEqual(state['ask_count'], 0)
        self.assertEqual(result['file_posts'], 0)
        self.assertNotIn('reservation_inquiries', state['continuation'])
        self.assertFalse(lifecycle.reservation_inquiries_allowed(state))

    def test_reservation_without_capability_refuses_new_enrollment(self):
        self.reserve()
        with self.assertRaisesRegex(ValueError, 'formal task owns'):
            self.begin(enabled=False)
        self.assertIsNone(reask.load(CALLER))
        self.assertFalse(scheduler.enrollment_path(CALLER).exists())

    def test_existing_episode_cannot_gain_capability_through_start(self):
        self.begin(enabled=False)
        before = self.files()
        changed = dict(self.continuation, reservation_inquiries=True)
        with self.assertRaisesRegex(ValueError, 'before changing reservation'):
            scheduler.start(self.live(), task_id='reservation-integration', now=1001.,
                            authenticate_executor=True, continuing=True, continuation=changed)
        self.assertEqual(self.files(), before)

    def test_capability_cannot_be_tampered_into_registered_binding(self):
        self.begin(enabled=False)
        state = reask.load(CALLER)
        state['continuation']['reservation_inquiries'] = True
        reask.save(state)
        self.reserve(at=1001.)
        self.assertFalse(scheduler.registered(state))
        with self.files_only():
            result = scheduler.sweep(now=1061.)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(result['episodes'][0]['status'], 'BINDING_OR_CONTROLLER_MISMATCH')

    def test_existing_enrollment_cannot_reauthorize_an_edited_capability(self):
        original = self.begin(enabled=False)
        path = scheduler.enrollment_path(CALLER)
        raw = path.read_bytes()
        self.reserve(at=1001.)
        for missing in ('none', 'token', 'enrollment', 'both', 'stale-binding'):
            for route in ('enroll-existing', 'start', 'compatibility-enroll'):
                with self.subTest(missing=missing, route=route):
                    state = copy.deepcopy(original)
                    state['continuation']['reservation_inquiries'] = True
                    if missing in ('token', 'both'):
                        state.pop('registration')
                    reask.save(state)
                    path.write_bytes(raw)
                    if missing == 'stale-binding':
                        record = ready.read_json(path)
                        record['binding']['episode_id'] = 'stale-enrolled-episode'
                        ready.write_json(path, record)
                    if missing in ('enrollment', 'both'):
                        path.unlink()
                    before = self.files()
                    with self.assertRaises(ValueError):
                        if route == 'enroll-existing':
                            digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else 'ABSENT'
                            scheduler.enroll_existing(self.live(), state['episode_id'], digest, now=1002.)
                        elif route == 'start':
                            scheduler.start(self.live(), task_id='reservation-integration', now=1002.,
                                            authenticate_executor=True, continuing=True,
                                            continuation=dict(self.continuation, reservation_inquiries=True))
                        else:
                            scheduler.enroll(state, self.live(), now=1002.)
                    self.assertEqual(self.files(), before)

    def test_removed_registration_cannot_authorize_reserved_foreground_inquiries(self):
        self.begin(enabled=False)
        self.reserve(at=1001., notify=True)
        frozen = self.preserve_pending()
        state = reask.load(CALLER)
        state['continuation']['reservation_inquiries'] = True
        state.pop('registration')
        reask.save(state)
        enrollment = scheduler.enrollment_path(CALLER).read_bytes()
        with self.files_only() as sender:
            result = reask.tick(CALLER, 1002., send=sender)
        self.assertEqual(result['registration_observation'], 'REGISTRATION_UNCOMMITTED')
        self.assertEqual(result['input_operations'], 0)
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(result['asks'], [])
        self.assertEqual(scheduler.enrollment_path(CALLER).read_bytes(), enrollment)
        self.assertFalse(reask.inbox_path(result).exists())
        self.assert_preserved(frozen)

    def test_original_authorized_enrollment_remains_reusable(self):
        original = self.begin()
        self.reserve(at=1001., notify=True)
        before = self.files()
        digest = hashlib.sha256(scheduler.enrollment_path(CALLER).read_bytes()).hexdigest()
        state = scheduler.enroll_existing(self.live(), original['episode_id'], digest, now=1002.)
        self.assertEqual(self.files(), before)
        with self.files_only():
            result = scheduler.sweep(now=1003.)
        self.assertEqual(result['file_posts'], 1)
        self.assertEqual(state['episode_id'], original['episode_id'])

    def test_reservation_inquiries_require_the_original_current_controller(self):
        state = self.begin()
        self.assertTrue(lifecycle.reservation_inquiries_allowed(state))
        state['continuation']['controller']['sha256'] = 'f' * 64
        self.assertFalse(lifecycle.reservation_inquiries_allowed(state))

    def test_cli_requires_continuing_and_original_authorization_file(self):
        base = ['start', '--caller-uuid', CALLER, '--supervisor', SUP, '--reservation-inquiries']
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            reask.main(base)
        self.assertEqual(raised.exception.code, 2)
        with self.assertRaisesRegex(ValueError, 'authorization must be'):
            reask.main(base + ['--continuing', '--overall-task', 'original task'])
        self.assertIsNone(reask.load(CALLER))
        self.assertFalse(scheduler.enrollment_path(CALLER).exists())

    def test_cli_authorized_reservation_registers_and_issues_file_only(self):
        self.reserve()
        auth = self.home / 'cli-authorization.txt'
        auth.write_text('Ask the original supervisor through files every 60 seconds while reserved.')
        with redirect_stdout(io.StringIO()):
            result = reask.main(['start', '--caller-uuid', CALLER, '--supervisor', SUP,
                                 '--continuing', '--reservation-inquiries', '--file-only',
                                 '--overall-task', 'original task', '--authorization-file', str(auth)])
        self.assertEqual(result, 0)
        state = reask.load(CALLER)
        self.assertEqual(state['ask_count'], 1)
        self.assertEqual(state['asks'][0]['inquiry_kind'], 'RESERVATION_STATUS')
        self.assertFalse(state['asks'][0]['terminal'])
        self.assertIsNone(state['terminal_pending'])
        self.assertTrue(scheduler.registered(state))

    def test_non_boolean_capability_is_rejected_without_publication(self):
        auth = self.home / 'authorization.txt'
        auth.write_text('Explicit ongoing task authorization.')
        for value in ('true', 1, None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'reservation inquiries'):
                scheduler.start(self.live(), now=1000., continuing=True, continuation=dict(
                    enabled=True, owner=SUP, overall_task='original task', authorization_file=str(auth),
                    reservation_inquiries=value))
            self.assertIsNone(reask.load(CALLER))


if __name__ == '__main__':
    unittest.main()

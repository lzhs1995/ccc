"""Continuing file requests preserve authority, consumption and task boundaries."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import executor_ready as ready
import executor_reask as reask
import executor_reask_lifecycle as lifecycle
import executor_reask_scheduler as scheduler
import test_executor_reask_scheduler as fixtures
from test_executor_reask_scheduler import CALLER, SUP, WORKSPACE


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        fixtures.SchedulerTests.setUp(self)
        self.consumer_auth = self.stack.enter_context(patch(
            'executor_reask_identity.authenticate', side_effect=fixtures.consumer_fixture))

    live = fixtures.SchedulerTests.live
    enroll = fixtures.SchedulerTests.enroll
    def begin(self, caller=CALLER, **kwargs):
        auth = self.home / 'explicit-user-authorization.txt'
        if not auth.exists():
            auth.write_text('Continue requests every 60 seconds until the overall task finishes.')
        value = dict(enabled=True, owner=SUP, overall_task='overall-work',
                     authorization_file=str(auth))
        value.update(kwargs.pop('authorization', {}))
        return reask.start(caller, SUP, 'original-task', workspace=WORKSPACE, now=1000.,
                           continuing=True, continuation=value, **kwargs)

    def answered(self, *, status='WAITING_DEPENDENCY', due=1061.):
        scheduler.sweep(now=1001.)
        state = reask.load(CALLER)
        current = ready.read_json(reask.inbox_path(state))
        body = dict(current['reply_template'], status=status, trigger='Await the next scoped result.',
                    next_due_epoch=due, deadline_epoch=due + 60)
        ready.write_json(Path(current['reply_mailbox']), body)
        scheduler.sweep(now=1002.)
        state = reask.load(CALLER)
        self.assertEqual(state['state'], reask.ANSWERED)
        return state

    def test_two_real_state_rounds_require_explicit_consumption_and_new_markers(self):
        original = self.enroll()
        for index in range(2):
            due = 1061. + index * 60
            scheduler.sweep(now=1001. + index * 60)
            current = ready.read_json(reask.inbox_path(reask.load(CALLER)))
            marker = current['marker']
            body = dict(current['reply_template'], trigger='Next result expected.',
                        next_due_epoch=due, deadline_epoch=due + 60)
            ready.write_json(Path(current['reply_mailbox']), body)
            scheduler.sweep(now=1002. + index * 60)
            state = reask.load(CALLER)
            self.assertEqual(state['state'], reask.ANSWERED)
            self.assertEqual(ready.read_json(scheduler.enrollment_path(CALLER))['status'], 'ENROLLED')
            self.assertEqual(scheduler.sweep(now=due + 2)['file_posts'], 0)
            state = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003. + index * 60)
            self.assertEqual(state['reply_consumed_epoch'], 1003. + index * 60)
            self.assertEqual(state['reply_detected_epoch'], 1002. + index * 60)
            self.assertEqual(state['episode_id'], original['episode_id'])
            self.assertEqual(scheduler.sweep(now=due - 1)['file_posts'], 0)
            self.assertEqual(scheduler.sweep(now=due)['file_posts'], 1)
            self.assertNotEqual(reask.load(CALLER)['asks'][-1]['marker'], marker)
        self.assertEqual(reask.load(CALLER)['rearm_count'], 2)
        self.transport.assert_not_called()

    def test_no_consumption_on_hash_mismatch_or_timer(self):
        self.enroll()
        state = self.answered()
        for digest in ('', '0' * 64):
            with self.assertRaises(ValueError):
                lifecycle.consume(CALLER, digest, now=1003.)
        self.assertIsNone(reask.load(CALLER)['reply_consumed_epoch'])
        self.assertEqual(scheduler.sweep(now=5000.)['file_posts'], 0)
        self.assertEqual(reask.load(CALLER)['state'], reask.ANSWERED)

    def consumption_files(self):
        return {str(path): path.read_bytes() for path in ready.state_root().rglob('*')
                if path.is_file() and path.suffix != '.lock'}

    def invalid_epochs(self):
        return (None, True, False, '1061', [], {}, -1, float('nan'),
                float('inf'), float('-inf'), 10 ** 400, lifecycle.MAX_EPOCH + 1)

    def test_invalid_task_reply_times_do_not_leave_receipt_or_partial_state(self):
        self.enroll()
        original = self.answered(status='TASK')
        for field in ('next_due_epoch', 'deadline_epoch'):
            for value in self.invalid_epochs():
                with self.subTest(field=field, value=value):
                    state = copy.deepcopy(original)
                    body = dict(state['reply']['body'], **{field: value})
                    state['reply'].update(reask.reply_record(body, state['reply']['source']))
                    reask.save(state)
                    before = self.consumption_files()
                    with self.assertRaises(ValueError):
                        lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
                    self.assertEqual(self.consumption_files(), before)
                    self.assertEqual(reask.load(CALLER)['state'], reask.ANSWERED)
        self.transport.assert_not_called()

    def test_task_reply_may_omit_optional_schedule_times(self):
        self.enroll()
        state = self.answered(status='TASK')
        body = dict(state['reply']['body'])
        for field in ('next_due_epoch', 'deadline_epoch'):
            body.pop(field)
        state['reply'].update(reask.reply_record(body, state['reply']['source']))
        reask.save(state)
        consumed = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        self.assertTrue(consumed['consume_result']['consumed'])
        self.assertEqual(consumed['next_due_epoch'], state['next_due_epoch'])
        self.assertEqual(consumed['rearm_count'], 1)

    def test_invalid_now_or_state_due_cannot_partially_consume(self):
        self.enroll()
        original = self.answered()
        for value in self.invalid_epochs():
            with self.subTest(field='state.next_due_epoch', value=value):
                state = dict(original, next_due_epoch=value)
                reask.save(state)
                before = self.consumption_files()
                with self.assertRaises(ValueError):
                    lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
                self.assertEqual(self.consumption_files(), before)
            if value is not None:  # None selects the actual clock, not a supplied epoch.
                with self.subTest(field='now', value=value):
                    reask.save(original)
                    before = self.consumption_files()
                    with self.assertRaises(ValueError):
                        lifecycle.consume(CALLER, original['reply']['body_sha256'], now=value)
                    self.assertEqual(self.consumption_files(), before)

    def test_invalid_prior_receipt_time_preserves_original_evidence(self):
        self.enroll()
        original = self.answered()
        result = lifecycle.consume(CALLER, original['reply']['body_sha256'], now=1003.)
        path = Path(result['consume_result']['receipt'])
        receipt = ready.read_json(path)
        # Simulate a receipt committed before the episode update, then corrupted.
        reask.save(original)
        for value in self.invalid_epochs():
            with self.subTest(value=value):
                ready.write_json(path, dict(receipt, consumed_epoch=value))
                before = self.consumption_files()
                with self.assertRaises(ValueError):
                    lifecycle.consume(CALLER, original['reply']['body_sha256'], now=1004.)
                self.assertEqual(self.consumption_files(), before)
        ready.write_json(path, receipt)
        recovered = lifecycle.consume(CALLER, original['reply']['body_sha256'], now=1004.)
        self.assertEqual(recovered['reply_consumed_epoch'], 1003.)
        self.assertEqual(recovered['rearm_count'], 1)
        self.assertEqual(ready.read_json(path), receipt)

    def test_consumed_body_stays_complete_and_late_old_reply_cannot_close_new_round(self):
        self.enroll()
        state = self.answered()
        body = state['reply']['body']
        body['detail'] = '正文' * 2000
        ready.write_json(Path(state['reply']['path']), body)
        # An observed reply is pinned. A changed disk file is not silently read
        # as the body that the foreground executor is acknowledging.
        state = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        receipt = ready.read_json(Path(state['reply_history'][-1]['receipt']))
        self.assertNotIn('detail', receipt['body'])
        scheduler.sweep(now=1061.)
        self.assertEqual(scheduler.sweep(now=1062.)['episodes'][0]['status'], reask.WAITING)

    def test_wait_reply_requires_owner_due_and_deadline(self):
        self.enroll()
        scheduler.sweep(now=1001.)
        state = reask.load(CALLER)
        current = ready.read_json(reask.inbox_path(state))
        valid = dict(current['reply_template'], trigger='Await result.', next_due_epoch=1061., deadline_epoch=1121.)
        for delta in (dict(owner=CALLER), dict(next_due_epoch=None), dict(deadline_epoch=0),
                      dict(deadline_epoch=float('inf')), dict(next_due_epoch=float('nan'))):
            ready.write_json(Path(current['reply_mailbox']), dict(valid, **delta))
            scheduler.sweep(now=1002.)
            self.assertEqual(reask.load(CALLER)['state'], reask.WAITING)

    def test_stop_while_answered_prevents_consume_and_rearm(self):
        self.enroll()
        state = self.answered()
        ready.write_json(reask.stop_file(CALLER), {'reason': 'OPERATOR'})
        stopped = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        self.assertEqual(stopped['state'], reask.STOPPED)
        self.assertIsNone(stopped['reply_consumed_epoch'])
        self.assertEqual(scheduler.sweep(now=1061.)['file_posts'], 0)

    def test_authorization_revocation_and_expiry_stop_without_sending(self):
        self.enroll()
        (self.home / 'explicit-user-authorization.txt').write_text('Authorization revoked.')
        scheduler.sweep(now=1001.)
        self.assertEqual(reask.load(CALLER)['exit_reason'], 'AUTHORIZATION_CHANGED')
        self.transport.assert_not_called()

    def test_authorization_expiry_is_checked_even_with_unconsumed_reply(self):
        state = self.begin(authorization={'until_epoch': 1100.})
        scheduler.enroll(state, self.live(), now=1000.)
        self.answered()
        scheduler.sweep(now=1100.)
        self.assertEqual(reask.load(CALLER)['exit_reason'], 'AUTHORIZATION_EXPIRED')

    def test_changed_authorization_binding_cannot_gain_scheduler_authority(self):
        self.enroll()
        original = reask.load(CALLER)
        for change in (None, dict(original['continuation'], overall_task='other')):
            changed = copy.deepcopy(original)
            changed['continuation'] = change
            reask.save(changed)
            self.assertEqual(scheduler.sweep(now=1001.)['file_posts'], 0)
        reask.save(original)

    def task_marker(self, task='child-task', supervisor=SUP):
        return dict(collaboration_id=task, task_id=task, workspace_uuid=WORKSPACE,
                    participants=[dict(role='supervisor', surface_uuid=supervisor),
                                  dict(role='executor', surface_uuid=CALLER)])

    def test_formal_reservation_pauses_and_child_release_resumes_same_authorization(self):
        self.enroll()
        state = self.answered(status='TASK')
        lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        # A mailbox TASK alone did not pause or prove a dispatch.
        self.assertEqual(scheduler.sweep(now=1061.)['file_posts'], 1)
        marker = self.task_marker()
        lifecycle.task_boundary(marker, 'RESERVED', now=1062.)
        self.assertEqual(scheduler.sweep(now=1121.)['file_posts'], 0)
        lifecycle.task_boundary(marker, 'RELEASED', now=1122.)
        self.assertEqual(scheduler.sweep(now=1122.)['file_posts'], 1)
        self.assertEqual(reask.load(CALLER)['continuation']['status'], 'ACTIVE')

    def test_foreign_release_and_one_of_two_children_cannot_unpause(self):
        self.enroll()
        for task in ('a', 'b'):
            lifecycle.task_boundary(self.task_marker(task), 'RESERVED', now=1001.)
        lifecycle.task_boundary(self.task_marker('b', CALLER), 'RELEASED', now=1002.)
        lifecycle.task_boundary(self.task_marker('a'), 'RELEASED', now=1002.)
        self.assertEqual(scheduler.sweep(now=1003.)['file_posts'], 0)
        self.assertEqual(len(reask.load(CALLER)['paused_tasks']), 1)

    def test_unknown_original_survives_reply_consumption_and_reservation(self):
        state = self.enroll()
        pending = dict(marker='original', outcome='UNKNOWN', origin={'controller': 'old'})
        state['terminal_pending'] = copy.deepcopy(pending)
        reask.save(state)
        state = self.answered()
        lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        lifecycle.task_boundary(self.task_marker(), 'RESERVED', now=1004.)
        lifecycle.task_boundary(self.task_marker(), 'RELEASED', now=1005.)
        scheduler.sweep(now=1061.)
        self.assertEqual(reask.load(CALLER)['terminal_pending'], pending)
        self.transport.assert_not_called()

    def test_harness_arm_and_disarm_preserve_overall_authorization(self):
        import mac_harness as harness
        self.enroll()
        with patch.object(harness, '_workspace_key', return_value=WORKSPACE), \
                patch.object(harness, '_ACTIVE_DIR', self.home / 'active'), \
                patch.object(lifecycle, 'ACTIVE_DIR', self.home / 'active'):
            marker = harness.arm_task('child', self.home / 'report', supervisor_surface_uuid=SUP,
                                      executor_surface_uuid=CALLER, workspace_uuid=WORKSPACE)
            self.assertEqual(scheduler.sweep(now=1001.)['file_posts'], 0)
            self.assertEqual(harness.disarm_task('child'), 1)
        self.assertEqual(reask.load(CALLER)['paused_tasks'], [])
        self.assertEqual(reask.load(CALLER)['continuation']['status'], 'ACTIVE')

    def test_complete_reply_above_display_limit_is_preserved_in_consumption(self):
        self.enroll()
        scheduler.sweep(now=1001.)
        current = ready.read_json(reask.inbox_path(reask.load(CALLER)))
        body = dict(current['reply_template'], trigger='Wait for complete review.',
                    next_due_epoch=1061., deadline_epoch=1121., detail='审阅正文' * 1000)
        ready.write_json(Path(current['reply_mailbox']), body)
        scheduler.sweep(now=1002.)
        state = reask.load(CALLER)
        self.assertEqual(state['reply']['body'], body)
        state = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        self.assertEqual(ready.read_json(Path(state['reply_history'][-1]['receipt']))['body'], body)
class PausedReplyTests(unittest.TestCase):
    live = fixtures.SchedulerTests.live
    begin = LifecycleTests.begin
    task_marker = LifecycleTests.task_marker

    def setUp(self):
        LifecycleTests.setUp(self)
        import mac_harness as harness
        self.stack.enter_context(patch.object(harness, '_ACTIVE_DIR', self.home / 'active'))
        self.stack.enter_context(patch.object(lifecycle, 'ACTIVE_DIR', self.home / 'active'))
        self.stack.enter_context(patch.object(ready, 'Tail', side_effect=AssertionError('no transcript')))
        self.stack.enter_context(patch.object(reask, 'idle_binding', side_effect=AssertionError('no idle binding')))

    def paused_wait(self, *, continuing=True):
        state = (self.begin() if continuing else reask.start(
            CALLER, SUP, 'original-task', workspace=WORKSPACE, now=1000.))
        scheduler.enroll(state, self.live(), now=1000.)
        self.assertEqual(scheduler.sweep(now=1001.)['file_posts'], 1)
        state = reask.load(CALLER)
        current = ready.read_json(reask.inbox_path(state))
        state.update(terminal_pending=dict(marker='original-unknown', outcome='UNKNOWN',
                                          nonce='original-nonce', attempt='original-attempt'),
                     transcript='/must-not-read', tail_position={'offset': 42})
        reask.save(state)
        lifecycle.task_boundary(self.task_marker(), 'RESERVED', now=1001.5)
        body = dict(current['reply_template'], trigger='Read the frozen review at the safe boundary.',
                    next_due_epoch=1061., deadline_epoch=1121.)
        ready.write_json(Path(current['reply_mailbox']), body)
        return reask.load(CALLER), body, Path(current['reply_mailbox'])

    def assert_preserved_inquiry(self, before, after):
        for key in ('controller', 'episode_id', 'task_id', 'supervisor', 'workspace_uuid',
                    'asks', 'ask_count', 'attempt_count', 'terminal_pending', 'paused_tasks',
                    'transcript', 'tail_position'):
            self.assertEqual(after[key], before[key], key)
        self.transport.assert_not_called()

    def consume_cli(self, digest=''):
        output = io.StringIO()
        bridge = SimpleNamespace(pin_workspace=lambda *_args, **_kwargs: self.live())
        with patch.object(ready, '_bridge', return_value=bridge), \
                patch.object(lifecycle.time, 'time', return_value=2001.), \
                redirect_stdout(output):
            rc = reask.main(['consume', '--caller-uuid', CALLER, '--reply-sha256', digest])
        return rc, json.loads(output.getvalue())

    def test_consume_cli_distinguishes_no_ack_from_consumption_while_reserved(self):
        original, body, _path = self.paused_wait()
        # A mailbox exists, but it has not yet been authenticated as ANSWERED.
        digest = reask.reply_record(body, 'file')['body_sha256']
        rc, pending = self.consume_cli(digest)
        self.assertEqual(rc, 2)
        self.assertEqual(pending['outcome'], reask.WAITING)
        self.assertEqual(pending['consume_result']['status'], 'NOT_ANSWERED')
        self.assertFalse(pending['consume_result']['consumed'])
        self.assertTrue(pending['consume_result']['reservation_paused'])
        self.assertIsNone(pending['reply_consumed_epoch'])
        self.assert_preserved_inquiry(original, reask.load(CALLER))
        self.assertNotIn('consume_result', reask.load(CALLER))
        answered = reask.tick(CALLER, now=2000.)
        rc, consumed = self.consume_cli(answered['reply']['body_sha256'])
        self.assertEqual(rc, 0)
        self.assertEqual(consumed['outcome'], reask.WAITING)
        observation = consumed['consume_result']
        self.assertEqual(observation['status'], 'REPLY_CONSUMED')
        self.assertTrue(observation['consumed'])
        self.assertTrue(observation['reservation_paused'])
        receipt = ready.read_json(Path(observation['receipt']))
        self.assertEqual(receipt['body'], body)
        for key in ('marker', 'body_sha256', 'consumed_epoch'):
            self.assertEqual(observation[key], receipt[key])
        self.assertEqual(consumed['rearm_count'], 1)
        self.assertNotIn('consume_result', reask.load(CALLER))
        # Repeating the same invocation cannot claim another consumption.
        before_repeat = reask.state_path(CALLER).read_bytes()
        rc, repeated = self.consume_cli(digest)
        self.assertEqual(rc, 2)
        self.assertFalse(repeated['consume_result']['consumed'])
        self.assertEqual(repeated['consume_result']['status'], 'NOT_ANSWERED')
        self.assertEqual(repeated['reply_consumed_epoch'], consumed['reply_consumed_epoch'])
        self.assertEqual(reask.state_path(CALLER).read_bytes(), before_repeat)
        self.assertEqual(scheduler.sweep(now=3000.)['file_posts'], 0)
        self.assert_preserved_inquiry(original, reask.load(CALLER))

    def test_consume_cli_no_episode_and_operator_stop_are_not_success(self):
        rc, missing = self.consume_cli()
        self.assertEqual(rc, 2)
        self.assertEqual(missing['consume_result']['status'], 'NO_EPISODE')
        self.assertFalse(missing['consume_result']['consumed'])
        self.paused_wait()
        state = reask.tick(CALLER, now=2000.)
        ready.write_json(reask.stop_file(CALLER), dict(reason='OPERATOR'))
        rc, stopped = self.consume_cli(state['reply']['body_sha256'])
        self.assertEqual(rc, 2)
        self.assertEqual(stopped['consume_result']['status'], 'STOPPED')
        self.assertFalse(stopped['consume_result']['consumed'])
        self.assertIsNone(stopped['reply_consumed_epoch'])
        self.assertEqual(stopped['rearm_count'], 0)

    def test_consume_cli_foreign_controller_refusal_preserves_original_bytes(self):
        original, _body, _path = self.paused_wait()
        original['controller']['source'] = str(self.home / 'original-foreign-controller')
        original['continuation']['controller'] = copy.deepcopy(original['controller'])
        reask.save(original)
        before = reask.state_path(CALLER).read_bytes()
        rc, refused = self.consume_cli()
        self.assertEqual(rc, 2)
        self.assertEqual(refused['consume_result']['status'], 'ORIGINAL_CONTROLLER_REQUIRED')
        self.assertFalse(refused['consume_result']['consumed'])
        diagnostic = refused['controller_diagnostic']
        self.assertEqual(diagnostic['original_controller'], original['controller'])
        self.assertEqual(diagnostic['invoked_controller'], reask.controller())
        self.assertEqual(diagnostic['original_identity'], 'RECORDED')
        self.assertEqual(diagnostic['next_action'], 'USE_ORIGINAL_CONTROLLER')
        self.assertEqual(reask.state_path(CALLER).read_bytes(), before)

    def test_consume_cli_revoked_authorization_does_not_acknowledge_reply(self):
        self.paused_wait()
        state = reask.tick(CALLER, now=2000.)
        (self.home / 'explicit-user-authorization.txt').write_text('Authorization revoked.')
        rc, revoked = self.consume_cli(state['reply']['body_sha256'])
        self.assertEqual(rc, 2)
        self.assertFalse(revoked['consume_result']['consumed'])
        self.assertEqual(revoked['consume_result']['reason'], 'AUTHORIZATION_CHANGED')
        self.assertEqual(revoked['rearm_count'], 0)
        self.assertIsNone(revoked['reply_consumed_epoch'])
        self.assertTrue(revoked['paused_tasks'])

    def test_consume_cli_foreign_executor_cannot_acknowledge_reply(self):
        self.paused_wait()
        state = reask.tick(CALLER, now=2000.)
        before = reask.state_path(CALLER).read_bytes()
        with patch('executor_reask_identity.authenticate',
                   side_effect=ValueError('original executor identity changed')) as auth, \
                self.assertRaises(ValueError):
            reask.main(['consume', '--caller-uuid', CALLER,
                        '--reply-sha256', state['reply']['body_sha256']])
        auth.assert_called_once()
        self.assertEqual(auth.call_args.args[0], state)
        self.assertEqual(reask.state_path(CALLER).read_bytes(), before)

    def test_paused_reply_is_detected_consumed_and_stays_paused_until_exact_release(self):
        original, body, _path = self.paused_wait()
        # The original foreground tick also observes only files while reserved.
        answered = reask.tick(CALLER, now=1002.)
        self.assertEqual(answered['state'], reask.ANSWERED)
        self.assertEqual(answered['reply']['body'], body)
        self.assertEqual(answered['reply_detected_epoch'], 1002.)
        self.assert_preserved_inquiry(original, answered)
        self.assertIsNone(answered['reply_consumed_epoch'])
        self.assertEqual(scheduler.sweep(now=2000.)['file_posts'], 0)
        consumed = lifecycle.consume(CALLER, answered['reply']['body_sha256'], now=2001.)
        self.assertEqual(consumed['state'], reask.WAITING)
        self.assertEqual(consumed['reply_consumed_epoch'], 2001.)
        self.assert_preserved_inquiry(original, consumed)
        receipt = ready.read_json(Path(consumed['reply_history'][-1]['receipt']))
        self.assertEqual(receipt['body'], body)
        for now in (2061., 4000.):
            self.assertEqual(scheduler.sweep(now=now)['file_posts'], 0)
            self.assert_preserved_inquiry(original, reask.load(CALLER))
        lifecycle.task_boundary(self.task_marker(), 'RELEASED', now=4001.)
        self.assertEqual(scheduler.sweep(now=4001.)['file_posts'], 1)
        resumed = reask.load(CALLER)
        self.assertEqual(resumed['episode_id'], original['episode_id'])
        self.assertEqual(resumed['terminal_pending'], original['terminal_pending'])
        self.assertEqual(resumed['paused_tasks'], [])
        self.assertNotEqual(resumed['asks'][-1]['marker'], original['asks'][-1]['marker'])
        self.assertEqual(scheduler.sweep(now=4002.)['file_posts'], 0)
        self.transport.assert_not_called()

    def test_paused_ordinary_inquiry_detects_reply_without_legacy_dispatch_or_rearm(self):
        original, body, _path = self.paused_wait(continuing=False)
        answered = reask.tick(CALLER, now=1002.)
        self.assertEqual(answered['state'], reask.ANSWERED)
        self.assertEqual(answered['reply']['body'], body)
        self.assert_preserved_inquiry(original, answered)
        consumed = lifecycle.consume(CALLER, answered['reply']['body_sha256'], now=1003.)
        self.assertEqual(consumed['state'], reask.CONSUMED)
        self.assert_preserved_inquiry(original, consumed)
        self.assertEqual(scheduler.sweep(now=2000.)['file_posts'], 0)

    def test_pause_does_not_weaken_reply_binding_or_body_hash(self):
        original, valid, path = self.paused_wait()
        changes = [{key: 'unrelated'} for key in ('marker', 'caller_surface_uuid',
                   'supervisor_uuid', 'task_id', 'episode_id', 'owner')]
        changes += [dict(queued=True), dict(received=False), dict(next_due_epoch=None)]
        for change in changes:
            with self.subTest(change=change):
                ready.write_json(path, dict(valid, **change))
                self.assertEqual(scheduler.sweep(now=2000.)['file_posts'], 0)
                state = reask.load(CALLER)
                self.assertEqual(state['state'], reask.WAITING)
                self.assertIsNone(state['reply'])
                self.assert_preserved_inquiry(original, state)
        ready.write_json(path, valid)
        scheduler.sweep(now=2001.)
        state = reask.load(CALLER)
        self.assertEqual(state['state'], reask.ANSWERED)
        with self.assertRaises(ValueError):
            lifecycle.consume(CALLER, '0' * 64, now=2002.)
        self.assertIsNone(reask.load(CALLER)['reply_consumed_epoch'])

    def assert_stop_wins(self, reason):
        with patch.object(reask, 'find_reply', side_effect=AssertionError('reply after authority ended')):
            self.assertEqual(scheduler.sweep(now=1002.)['file_posts'], 0)
        state = reask.load(CALLER)
        self.assertEqual((state['state'], state['exit_reason']), (reask.STOPPED, reason))
        self.assertIsNone(state['reply'])
        self.assertIsNone(state['reply_consumed_epoch'])
        self.transport.assert_not_called()

    def test_operator_stop_precedes_bound_reply_while_paused(self):
        self.paused_wait()
        ready.write_json(reask.stop_file(CALLER), {'reason': 'OPERATOR'})
        self.assert_stop_wins('OPERATOR_STOP')

    def test_revocation_precedes_bound_reply_while_paused(self):
        self.paused_wait()
        (self.home / 'explicit-user-authorization.txt').write_text('Authorization revoked.')
        self.assert_stop_wins('AUTHORIZATION_CHANGED')

    def test_expiry_precedes_bound_reply_while_paused(self):
        self.begin(authorization={'until_epoch': 1002.})
        self.paused_wait()
        self.assert_stop_wins('AUTHORIZATION_EXPIRED')

    def test_foreign_formal_owner_precedes_bound_reply_while_paused(self):
        self.paused_wait()
        marker = self.task_marker('successor-task', '55555555-5555-4555-8555-555555555555')
        marker['armed_at'] = '1970-01-01T00:16:41.750000+00:00'
        ready.write_json(self.home / 'active' / WORKSPACE / 'successor-task.json', marker)
        self.assert_stop_wins('FORMAL_OWNER_CHANGED')

    def test_paused_foreign_controller_cannot_poll_or_consume_through_current(self):
        original, _body, _path = self.paused_wait()
        changed = copy.deepcopy(original)
        changed['controller']['source'] = str(self.home / 'old-controller')
        changed['continuation']['controller'] = copy.deepcopy(changed['controller'])
        reask.save(changed)
        before = reask.state_path(CALLER).read_bytes()
        with patch.object(reask, 'find_reply', side_effect=AssertionError('foreign reply observation')):
            # Registration drift is rejected before interpreting any replies.
            result = reask.tick(CALLER, now=1002.)
            self.assertEqual(reask.summary(result)['outcome'], 'REGISTRATION_UNCOMMITTED')
            self.assertEqual(lifecycle.consume(CALLER, now=1003.)['outcome'], 'ORIGINAL_CONTROLLER_REQUIRED')
        self.assertEqual(reask.state_path(CALLER).read_bytes(), before)
        # An unenrolled foreign episode still has no foreground or file grant.
        changed.pop('registration')
        reask.save(changed)
        before = reask.state_path(CALLER).read_bytes()
        refused = reask.tick(CALLER, now=1004.)
        self.assertEqual(refused['outcome'], 'ORIGINAL_CONTROLLER_REQUIRED')
        self.assertEqual(refused['controller_diagnostic']['original_controller'], changed['controller'])
        self.assertEqual(refused['controller_diagnostic']['invoked_controller'], reask.controller())
        self.assertEqual(reask.tick(CALLER, now=1004., file_only=True)['outcome'], 'UNSUPPORTED_FILE_PROTOCOL')
        self.assertEqual(reask.state_path(CALLER).read_bytes(), before)
        self.transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()

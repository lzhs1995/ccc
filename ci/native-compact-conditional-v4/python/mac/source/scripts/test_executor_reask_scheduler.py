"""Durable inquiries continue without a foreground turn or any transport input."""
from contextlib import ExitStack, contextmanager
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import call, patch
import uuid

import executor_ready as ready
import executor_reask as reask
import executor_reask_lifecycle as lifecycle
import executor_reask_scheduler as scheduler
import executor_reply_advisory as reply_advisory

CALLER = '11111111-1111-4111-8111-111111111111'
SUP = '22222222-2222-4222-8222-222222222222'
WORKSPACE = '33333333-3333-4333-8333-333333333333'
PANE = '44444444-4444-4444-8444-444444444444'


def consumer_fixture(state):
    """Dependency stub for lifecycle tests; real authentication has its own suite."""
    return dict(schema='executor-reply-consumer-proof-v1', provider='claude',
                session_id='55555555-5555-4555-8555-555555555555', process={},
                identity_sha256='a' * 64, episode_id=state['episode_id'])


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(Path, 'home', return_value=self.home))
        self.stack.enter_context(patch.object(lifecycle, 'ACTIVE_DIR', self.home / 'active'))
        self.current = dict(source=str(Path(reask.__file__).resolve().parents[1]),
                            python=str(Path(scheduler.sys.executable).resolve()))
        self.approved = self.stack.enter_context(patch.object(
            scheduler, 'approved_release', return_value=self.current))
        self.transport = self.stack.enter_context(patch.object(
            ready, '_bridge', side_effect=AssertionError('background transport forbidden')))
        self.capture = self.stack.enter_context(patch.object(
            reply_advisory, 'capture_executor',
            return_value=dict(schema='executor-reask-consumer-identity-v1',
                              fixture='scheduler dependency only')))

    def begin(self, caller=CALLER, **kwargs):
        return reask.start(caller, SUP, 'original-task', workspace=WORKSPACE, now=1000., **kwargs)

    def live(self, caller=CALLER):
        return dict(caller_surface_uuid=caller, target_surface_uuid=SUP,
                    workspace_uuid=WORKSPACE, target_pane_uuid=PANE)

    def enroll(self, caller=CALLER):
        state = self.begin(caller)
        scheduler.enroll(state, self.live(caller), now=1000.)
        return state

    def test_schedule_continues_after_finite_foreground_run_and_respects_due_time(self):
        self.enroll()
        clock = [1001.]
        def sleep(seconds):
            clock[0] += seconds
        send = lambda *_: dict(outcome='SKIPPED_UNVERIFIED', terminal=False)
        state = reask.run(CALLER, 5, send=send, clock=lambda: clock[0], sleep=sleep)
        self.assertEqual((state['state'], state['ask_count']), (reask.WAITING, 1))
        self.assertIsNone(state['continuation'])
        self.assertEqual(state['wait_boundary']['next_action'], 'END_WAITING_TURN_NORMALLY')
        self.assertEqual(scheduler.sweep(now=1060.)['file_posts'], 0)
        self.assertEqual(scheduler.sweep(now=1061.)['file_posts'], 1)
        self.assertEqual(scheduler.sweep(now=2000.)['file_posts'], 1)
        state = reask.load(CALLER)
        self.assertEqual((state['ask_count'], state['next_due_epoch']), (3, 2060.))
        self.assertEqual(state['asks'][-1]['outcome'], 'FILE_ONLY_SCHEDULED')
        self.transport.assert_not_called()

    def test_legacy_null_continuation_returns_during_formal_reservation(self):
        state = dict(state=reask.WAITING, continuation=None, paused_tasks=['original-task'])
        with patch.object(reask, 'tick', return_value=state), patch('time.sleep') as sleep:
            result = reask.run(CALLER, 5, sleep=sleep)
        self.assertEqual(result['run_return_reason'], 'FORMAL_RESERVATION_WAIT')
        self.assertEqual(result['wait_boundary']['next_action'], 'END_WAITING_TURN_NORMALLY')
        self.assertIsNone(result['continuation'])
        sleep.assert_not_called()

    def test_eight_enrollments_share_verification_only_within_each_sweep(self):
        for index in range(8):
            self.enroll(str(uuid.UUID(int=index + 1)))
        for now in (1001., 1061.):
            result = scheduler.sweep(now=now)
            self.assertEqual(result['status'], 'SWEPT', result)
            self.assertEqual(result['file_posts'], 8)
            self.assertEqual(len(result['episodes']), 8)
            counts = result['release_verification']
            self.assertEqual(counts['full_verifications'], 1)
            self.assertGreaterEqual(counts['reuses'], 15)
        self.transport.assert_not_called()

    def test_cached_release_does_not_hide_a_stop_between_enrollments(self):
        callers = [str(uuid.UUID(int=index + 1)) for index in range(2)]
        for caller in callers:
            self.enroll(caller)
        sweep_one = scheduler._sweep_one
        def stop_other(path, *args, **kwargs):
            result = sweep_one(path, *args, **kwargs)
            for caller in callers:
                if caller != result.get('caller_surface_uuid'):
                    ready.write_json(reask.stop_file(caller), dict(reason='USER_STOP'))
            return result
        with patch.object(scheduler, '_sweep_one', side_effect=stop_other):
            result = scheduler.sweep(now=1001.)
        self.assertEqual(result['file_posts'], 1)
        self.assertEqual(result['episodes'][1]['status'], reask.STOPPED)
        self.assertEqual(result['release_verification']['full_verifications'], 1)
        self.transport.assert_not_called()

    def test_exact_reply_finishes_registration_wrong_task_does_not(self):
        self.enroll()
        scheduler.sweep(now=1001.)
        state = reask.load(CALLER)
        current = ready.read_json(reask.inbox_path(state))
        reply = dict(current['reply_template'], trigger='Existing review finished.')
        path = Path(current['reply_mailbox'])
        ready.write_json(path, dict(reply, task_id='foreign'))
        self.assertEqual(scheduler.sweep(now=1002.)['episodes'][0]['status'], reask.WAITING)
        ready.write_json(path, reply)
        result = scheduler.sweep(now=1003.)
        self.assertEqual((result['file_posts'], result['episodes'][0]['status']), (0, reask.ANSWERED))
        self.assertEqual(ready.read_json(scheduler.enrollment_path(CALLER))['status'], 'FINISHED')
        self.assertEqual(scheduler.sweep(now=2000.)['file_posts'], 0)

    def test_operator_stop_survives_schedule_and_cannot_reopen(self):
        self.enroll()
        ready.write_json(reask.stop_file(CALLER), dict(reason='USER_STOP'))
        self.assertEqual(scheduler.sweep(now=1001.)['episodes'][0]['status'], reask.STOPPED)
        self.assertEqual(self.begin()['state'], reask.STOPPED)
        self.assertEqual(scheduler.sweep(now=2000.)['file_posts'], 0)

    def test_unenrolled_busy_or_mismatched_episode_never_posts(self):
        self.begin()
        self.assertEqual(scheduler.sweep(now=1001.)['episodes'], [])
        self.enroll()
        with reask.episode_lock(CALLER):
            result = scheduler.sweep(now=1001.)
        self.assertEqual(result['episodes'][0]['status'], 'EPISODE_BUSY')
        path = scheduler.enrollment_path(CALLER)
        record = ready.read_json(path)
        for key in ('binding', 'controller', 'scheduler', 'live_binding'):
            changed = copy.deepcopy(record)
            changed[key] = {}
            ready.write_json(path, changed)
            result = scheduler.sweep(now=1001.)
            self.assertEqual(result['file_posts'], 0)
            self.assertEqual(reask.load(CALLER)['ask_count'], 0)
        self.transport.assert_not_called()

    def test_file_only_schedule_preserves_uncertain_original_and_skips_transcript_and_binding(self):
        state = self.enroll()
        pending = dict(marker='UNKNOWN_original', outcome='INTENT_UNRESOLVED',
                       origin=dict(controller='old-controller'), terminal=True)
        state['terminal_pending'] = copy.deepcopy(pending)
        state['transcript'] = '/must-not-read'
        state['tail_position'] = dict(offset=0)
        reask.save(state)
        with patch.object(ready, 'Tail', side_effect=AssertionError('no transcript')), \
                patch.object(reask, 'idle_binding', side_effect=AssertionError('no idle binding')):
            result = scheduler.sweep(now=1001.)
        self.assertEqual(result['file_posts'], 1)
        self.assertEqual(reask.load(CALLER)['terminal_pending'], pending)
        self.transport.assert_not_called()

    def test_enrollment_requires_matching_authenticated_pair_and_canonical_channels(self):
        state = self.begin()
        for changed in (dict(target_surface_uuid=CALLER), dict(caller_surface_uuid=SUP),
                        dict(workspace_uuid=SUP)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                scheduler.enroll(state, dict(self.live(), **changed))
        for field, value in [('channels', ['/tmp/../tmp']), ('channels', ['relative']),
                             ('channels', ['/tmp'] * (scheduler.MAX_CHANNELS + 1)),
                             ('supervisor', 'surface:28'), ('mailbox', '/unrelated')]:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                scheduler.binding(dict(state, **{field: value}))
        self.assertFalse(scheduler.enrollment_path(CALLER).exists())

    def test_existing_controller_registration_cannot_be_replaced_for_same_episode(self):
        state = self.enroll()
        path = scheduler.enrollment_path(CALLER)
        old = ready.read_json(path)
        old['controller']['source'] = '/old-release'
        ready.write_json(path, old)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            scheduler.enroll(state, self.live())
        self.assertEqual(path.read_bytes(), before)

    def test_cli_persists_authenticated_uuid_instead_of_surface_alias(self):
        self.transport.side_effect = None
        self.transport.return_value.pin_workspace.return_value = self.live()
        with patch.object(reask, 'tick', side_effect=lambda caller, **kwargs: reask.load(caller)) as tick, \
                patch.object(reask.sys, 'stdout', io.StringIO()):
            self.assertEqual(reask.main(['start', '--supervisor', 'surface:28',
                                         '--caller-uuid', CALLER, '--task', 'next']), 0)
        state = reask.load(CALLER)
        tick.assert_called_once_with(CALLER, file_only=False)
        self.assertEqual((state['supervisor'], state['workspace_uuid']), (SUP, WORKSPACE))
        self.assertEqual(ready.read_json(scheduler.enrollment_path(CALLER))['status'], 'ENROLLED')

    def test_cli_file_only_posts_and_schedules_without_transcript_or_transport(self):
        self.transport.side_effect = None
        self.transport.return_value.pin_workspace.return_value = self.live()
        with patch.object(reask, 'idle_binding', side_effect=AssertionError('no idle binding')), \
                patch.object(ready, 'Tail', side_effect=AssertionError('no transcript')), \
                patch.object(reask.sys, 'stdout', io.StringIO()):
            self.assertEqual(reask.main(['start', '--supervisor', SUP, '--caller-uuid', CALLER,
                                         '--task', 'next', '--file-only']), 0)
            state = reask.load(CALLER)
            self.assertEqual(state['asks'][-1]['outcome'], 'FILE_ONLY_SCHEDULED')
            self.assertEqual((state['ask_count'], state['file_posts']), (1, 1))
            self.assertIsNone(state['continuation'])
            self.assertEqual((state['terminal_attempts'], state['native_receipts']), (0, 0))
            self.assertIsNone(state['terminal_pending'])
            self.assertEqual(scheduler.sweep(now=state['next_due_epoch'])['file_posts'], 1)
        # Resolve initially and recheck under the registration lock; no input.
        self.assertEqual(self.transport.call_args_list, [call(), call()])
        self.assertEqual(self.transport.return_value.mock_calls,
                         [call.pin_workspace(SUP, caller_uuid=CALLER)] * 2)
        self.assertEqual(reask.load(CALLER)['episode_id'], state['episode_id'])

    def test_cli_file_only_continuation_still_requires_explicit_authorization(self):
        self.transport.side_effect = None
        self.transport.return_value.pin_workspace.return_value = self.live()
        args = ['start', '--supervisor', SUP, '--caller-uuid', CALLER, '--task', 'next',
                '--file-only', '--continuing', '--overall-task', 'overall']
        with patch.object(reask.sys, 'stdout', io.StringIO()), self.assertRaises(ValueError):
            reask.main(args)
        self.assertIsNone(reask.load(CALLER))
        auth = self.home / 'explicit-authorization.txt'
        auth.write_text('Continue file inquiries for the overall task.')
        with patch.object(reask.sys, 'stdout', io.StringIO()):
            self.assertEqual(reask.main(args + ['--authorization-file', str(auth)]), 0)
        state = reask.load(CALLER)
        self.assertEqual(state['continuation']['overall_task'], 'overall')
        self.assertEqual(state['asks'][-1]['outcome'], 'FILE_ONLY_SCHEDULED')
        self.assertEqual(self.transport.return_value.mock_calls,
                         [call.pin_workspace(SUP, caller_uuid=CALLER)] * 4)

    def test_cli_file_only_rejects_transcript_before_registration(self):
        with patch.object(reask.sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as exc:
            reask.main(['start', '--supervisor', SUP, '--caller-uuid', CALLER,
                        '--file-only', '--transcript', '/must-not-read'])
        self.assertEqual(exc.exception.code, 2)
        self.assertIsNone(reask.load(CALLER))
        self.transport.assert_not_called()

    def test_cli_start_rejects_same_owner_reservation_created_before_episode(self):
        import mac_harness as harness
        active = self.home / 'formal-task-markers'
        self.stack.enter_context(patch.object(harness, '_ACTIVE_DIR', active))
        self.stack.enter_context(patch.object(lifecycle, 'ACTIVE_DIR', active))
        auth = self.home / 'continuing-authorization.txt'
        auth.write_text('Continue inquiries for the overall task after formal release.')
        self.transport.side_effect = None
        for continuing in (False, True):
            for file_only in (False, True):
                with self.subTest(continuing=continuing, file_only=file_only):
                    caller = str(uuid.uuid4()).upper()
                    marker = dict(collaboration_id='prior-reservation', task_id='business-task',
                                  workspace_uuid=WORKSPACE, armed_at='1970-01-01T00:00:01+00:00',
                                  participants=[dict(role='supervisor', surface_uuid=SUP),
                                                dict(role='executor', surface_uuid=caller)])
                    marker_path = active / WORKSPACE / (caller + '.json')
                    ready.write_json(marker_path, marker)
                    original_marker = marker_path.read_bytes()
                    self.assertIsNone(reask.load(caller))
                    self.transport.reset_mock()
                    self.transport.return_value.pin_workspace.return_value = self.live(caller)
                    args = ['start', '--supervisor', SUP, '--caller-uuid', caller, '--task', 'next']
                    if file_only:
                        args.append('--file-only')
                    if continuing:
                        args += ['--continuing', '--overall-task', 'overall',
                                 '--authorization-file', str(auth)]
                    with patch.object(reask.sys, 'stdout', io.StringIO()), \
                            patch.object(reask, 'idle_binding', side_effect=AssertionError('reserved')), \
                            patch.object(ready, 'Tail', side_effect=AssertionError('reserved')), \
                            patch.object(reask, 'tick', side_effect=AssertionError('reserved')) as tick:
                        with self.assertRaisesRegex(ValueError, 'formal task owns the executor'):
                            reask.main(args)
                        tick.assert_not_called()
                        self.assertIsNone(reask.load(caller))
                        self.assertFalse(reask.state_path(caller).exists())
                        self.assertFalse(scheduler.enrollment_path(caller).exists())
                        self.assertEqual(scheduler.sweep()['file_posts'], 0)
                        self.assertEqual(marker_path.read_bytes(), original_marker)
                    self.assertEqual(self.transport.return_value.mock_calls,
                                     [call.pin_workspace(SUP, caller_uuid=caller)] * 2)

    def test_capacity_enumeration_is_bounded(self):
        visited = []
        @contextmanager
        def huge(_directory):
            def entries():
                for index in range(100000):
                    visited.append(index)
                    yield object()
            yield entries()
        with patch.object(scheduler.os, 'scandir', side_effect=huge), self.assertRaises(ValueError):
            scheduler.check_capacity(self.home)
        self.assertEqual(len(visited), scheduler.MAX_ENROLLMENTS)

    def test_deadline_rotation_makes_progress_past_slow_first_caller(self):
        for _ in range(3):
            self.enroll(str(uuid.uuid4()).upper())
        visited = []
        def deadline(caller, *_args, **_kwargs):
            visited.append(caller)
            raise ready.WaitBudgetExpired()
        with patch.object(reask, 'tick_locked', side_effect=deadline):
            for _ in range(3):
                self.assertEqual(scheduler.sweep(now=1001.)['status'], 'SWEEP_BUDGET_EXHAUSTED')
        self.assertEqual(len(set(visited)), 3)
        self.assertTrue(ready.read_json(scheduler.root() / 'STATUS.json')['last_enrollment'])

    def test_release_deadline_preserves_prior_rotation_and_next_caller_progresses(self):
        for _ in range(3):
            self.enroll(str(uuid.uuid4()).upper())
        visited = []
        def deadline(caller, *_args, **_kwargs):
            visited.append(caller)
            raise ready.WaitBudgetExpired()
        with patch.object(reask, 'tick_locked', side_effect=deadline):
            first = scheduler.sweep(now=1001.)
            self.approved.side_effect = ready.WaitBudgetExpired()
            failed = scheduler.sweep(now=1001.)
            self.assertEqual(failed['phase'], 'VERIFY_RELEASE')
            self.assertEqual(failed['last_enrollment'], first['last_enrollment'])
            self.assertEqual(failed['episodes'], [])
            self.approved.side_effect = None
            scheduler.sweep(now=1001.)
        self.assertEqual(len(set(visited)), 2)
        self.transport.assert_not_called()

    def test_cursor_read_deadline_preserves_exact_status_and_records_error_separately(self):
        self.enroll()
        scheduler.sweep(now=1001.)
        path = scheduler.root() / 'STATUS.json'
        before = path.read_bytes()
        read = ready.read_json
        def slow_read(candidate):
            if candidate == path:
                raise ready.WaitBudgetExpired()
            return read(candidate)
        self.approved.reset_mock()
        with patch.object(ready, 'read_json', side_effect=slow_read):
            failed = scheduler.sweep(now=1062.)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(failed['status_record_unchanged'])
        self.assertEqual(failed['phase'], 'LOAD_CURSOR')
        self.assertEqual(ready.read_json(scheduler.root() / 'LAST-ERROR.json'), failed)
        self.approved.assert_not_called()

    def test_real_release_io_deadline_identifies_phase_without_episode_work(self):
        self.enroll()
        def slow_release(*_args):
            time.sleep(0.2)
            return self.current
        self.approved.side_effect = slow_release
        with patch.object(scheduler, 'MAX_SWEEP_SECONDS', 0.02):
            failed = scheduler.sweep(now=1001.)
        self.assertEqual(failed['status'], 'SWEEP_BUDGET_EXHAUSTED')
        self.assertEqual(failed['phase'], 'VERIFY_RELEASE')
        self.assertIn('VERIFY_RELEASE', failed['phase_seconds'])
        self.assertEqual(reask.load(CALLER)['ask_count'], 0)
        self.assertFalse(failed['file_posts_complete'])
        self.transport.assert_not_called()

    def test_interrupted_committed_tick_reports_partial_counters(self):
        self.enroll()
        tick = reask.tick_locked
        def interrupted(*args, **kwargs):
            tick(*args, **kwargs)
            raise ready.WaitBudgetExpired()
        with patch.object(reask, 'tick_locked', side_effect=interrupted):
            failed = scheduler.sweep(now=1001.)
        self.assertEqual(failed['status'], 'SWEEP_BUDGET_EXHAUSTED')
        self.assertEqual(failed['phase'], 'EPISODE')
        self.assertFalse(failed['file_posts_complete'])
        self.assertTrue(failed['pending_enrollment'])
        self.assertEqual(failed['file_posts'], 0)
        self.assertEqual(reask.load(CALLER)['file_posts'], 1)
        self.transport.assert_not_called()

    def test_release_failure_is_observed_without_any_episode_work(self):
        self.enroll()
        self.approved.side_effect = ValueError('manifest changed')
        result = scheduler.sweep(now=1001.)
        self.assertEqual(result['status'], 'SCHEDULER_FAILED')
        self.assertEqual(reask.load(CALLER)['ask_count'], 0)
        self.transport.assert_not_called()

    def test_separate_launchd_job_has_no_transport_or_guardian_invocation(self):
        spec = scheduler.launchd_spec(self.home, self.current['source'], '/python', '/CURRENT.json')
        self.assertEqual(spec['Label'], scheduler.LABEL)
        self.assertEqual(spec['StartInterval'], 60)
        self.assertEqual(spec['ProgramArguments'][-3:], ['sweep', '--manifest', '/CURRENT.json'])
        self.assertNotIn('configuration_guardian.py', str(spec))
        self.assertNotIn('run', spec['ProgramArguments'])
        self.assertEqual(spec['ProcessType'], 'Standard')

    def test_cadence_is_separate_from_sweep_outcome_and_handles_clock_regression(self):
        first = scheduler.sweep(now=1000.)
        self.assertEqual(first['cadence']['status'], 'FIRST_OBSERVATION')
        second = scheduler.sweep(now=1120.)
        self.assertEqual(second['cadence']['status'], 'ON_TIME')
        delayed = scheduler.sweep(now=1301.)
        self.assertEqual((delayed['status'], delayed['cadence']['status']), ('SWEPT', 'SWEEP_LATE'))
        self.assertEqual(delayed['cadence']['interval_observed_seconds'], 181.)
        self.assertEqual(scheduler.sweep(now=1200.)['cadence']['status'], 'CLOCK_REGRESSED')
        self.assertNotIn('startup_observation', delayed)  # library imports are not process launches

    def test_exact_release_watermark_and_legacy_first_visible_are_distinct(self):
        from executor_reask_lifecycle import apply_boundary
        state = self.enroll()
        scheduler.sweep(now=1001.)
        state = reask.load(CALLER)
        marker = dict(collaboration_id='release-test', task_id='business', workspace_uuid=WORKSPACE,
                      participants=[dict(role='supervisor', surface_uuid=SUP),
                                    dict(role='executor', surface_uuid=CALLER)])
        self.assertTrue(apply_boundary(state, marker, 'RESERVED', 1002.))
        self.assertTrue(apply_boundary(state, marker, 'RELEASED', 1003.))
        self.assertEqual(state['last_task_boundary']['file_posts_at_release'], 1)
        reask.save(state)
        result = scheduler.sweep(now=1300.)
        observation = result['episodes'][0]['release_resume_observation']
        self.assertTrue(observation['first_is_actual_first'])
        self.assertEqual(observation['release_to_first_file_post_seconds'], 297.)
        self.assertEqual(observation['delay_status'], 'RESUME_LATE')
        saved = ready.read_json(scheduler.enrollment_path(CALLER))['release_resume_observation']
        self.assertEqual(saved, observation)
        # Later posts cannot replace the recorded first successful observation.
        self.assertEqual(scheduler.sweep(now=1360.)['episodes'][0]['release_resume_observation'], saved)
        state = reask.load(CALLER)
        boundary = state['last_task_boundary']
        boundary.pop('file_posts_at_release')
        boundary.pop('attempt_count_at_release')
        state['asks'] = state['asks'][-1:]  # bounded legacy history really lost earlier posts
        visible = scheduler._release_observation(state, None, 1400.)
        self.assertFalse(visible['first_is_actual_first'])
        self.assertNotIn('release_to_first_file_post_seconds', visible)
        self.assertEqual(visible['status'], 'FIRST_VISIBLE_POST_OBSERVED')
        self.transport.assert_not_called()
        self.capture.assert_not_called()

    def test_release_pending_never_infers_eligibility_or_release(self):
        state = dict(state=reask.WAITING, asks=[], last_task_boundary=dict(
            event='RELEASED', at_epoch=1000., task_id='task', collaboration_id='collab'))
        waiting = scheduler._release_observation(state, None, 1121.)
        self.assertEqual((waiting['status'], waiting['delay_status']),
                         ('AWAITING_FILE_POST', 'RESUME_LATE'))
        for extra in (dict(state=reask.STOPPED), dict(state=reask.ANSWERED),
                      dict(paused_tasks=[dict(task_id='other', collaboration_id='new')])):
            held = scheduler._release_observation(dict(state, **extra), None, 1121.)
            self.assertEqual(held['status'], 'RESUME_NOT_CURRENTLY_ELIGIBLE')
            self.assertEqual(held['delay_status'], 'PENDING')
        self.assertIsNone(scheduler._release_observation(dict(state, last_task_boundary={}), None, 1121.))

    def test_subprocess_startup_observation_flush_state_and_lock_release(self):
        # Execute in an isolated actual process, introducing import latency
        # before heavy modules. Do not sample or touch the running launchd job.
        code = r'''
import atexit, builtins, fcntl, io, json, os, sys, time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
home = Path(sys.argv[2])
real_import = builtins.__import__
def delayed(name, *args, **kwargs):
    if name == 'configuration_guardian':
        time.sleep(.06)
    return real_import(name, *args, **kwargs)
with patch.object(builtins, '__import__', side_effect=delayed):
    import executor_reask_scheduler as scheduler
import executor_ready as ready
atexit.register(lambda: (home / 'normal-finalization').write_text('ok'))
class Output(io.StringIO):
    flushed = False
    def flush(self):
        self.flushed = True
out = Output()
with patch.object(Path, 'home', return_value=home), patch.object(scheduler.sys, 'stdout', out):
    def approved(*args):
        if sys.argv[3] == 'failure':
            raise ValueError('simulated invalid manifest')
        return dict(source=sys.argv[1], python=sys.executable)
    with patch.object(scheduler, 'approved_release', side_effect=approved):
        rc = scheduler.main(['sweep'])
    with open(scheduler.root() / 'sweep.lock', 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    result = json.loads(out.getvalue())
    assert result == ready.read_json(scheduler.root() / 'STATUS.json')
    assert out.flushed
print(json.dumps(dict(rc=rc, result=result, flushed=out.flushed)))
'''
        for mode in ('success', 'failure'):
            home = self.home / mode
            home.mkdir()
            run = subprocess.run([sys.executable, '-B', '-c', code,
                                  str(Path(scheduler.__file__).parent), str(home), mode],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(run.stdout)
            self.assertEqual(report['rc'], 0 if mode == 'success' else 2)
            startup = report['result']['startup_observation']
            self.assertGreaterEqual(startup['module_import_seconds'], .06)
            self.assertGreaterEqual(startup['module_to_sweep_seconds'], startup['module_import_seconds'])
            self.assertFalse(startup['interpreter_start_observed'])
            self.assertTrue((home / 'normal-finalization').is_file())

    def test_atomic_state_failure_leaves_no_temporary_files(self):
        path = self.home / 'state.json'
        ready.write_json(path, {'original': True})
        with patch.object(ready.os, 'fsync', side_effect=ready.WaitBudgetExpired), \
                self.assertRaises(ready.WaitBudgetExpired):
            ready.write_json(path, {'replacement': True})
        self.assertEqual(ready.read_json(path), {'original': True})
        self.assertEqual(list(self.home.glob('.*.tmp')), [])


if __name__ == '__main__':
    unittest.main()

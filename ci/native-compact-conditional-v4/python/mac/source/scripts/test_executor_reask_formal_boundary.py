"""Formal ownership barriers, isolated file previews and real caller locks."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

import executor_ready as ready
import executor_reask as reask
import executor_reask_lifecycle as lifecycle
import executor_reask_protocol as protocol
import executor_reask_scheduler as scheduler
import mac_harness as harness


CALLER = '11111111-1111-4111-8111-111111111111'
SUPERVISOR = '22222222-2222-4222-8222-222222222222'
SUCCESSOR = '33333333-3333-4333-8333-333333333333'
WORKSPACE = '44444444-4444-4444-8444-444444444444'


def stamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


class FormalBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix='reask-formal-boundary-'))).resolve()
        self.active = self.home / 'active'
        self.consume = lifecycle.consume
        self.stack.enter_context(patch.object(ready, 'state_root', return_value=self.home / 'state'))
        self.stack.enter_context(patch.object(harness, '_ACTIVE_DIR', self.active))
        self.stack.enter_context(patch.object(lifecycle, 'ACTIVE_DIR', self.active))
        for module, name in ((protocol, 'supports_state'), (protocol, 'supports_record'),
                             (reask, 'default_send'), (reask, 'idle_binding'),
                             (ready, 'Tail'), (lifecycle, 'consume')):
            self.stack.enter_context(patch.object(
                module, name, side_effect=AssertionError(f'unexpected {name}')))
        self.stack.enter_context(patch('subprocess.run', side_effect=AssertionError('no transport')))
        self.channels = self.stack.enter_context(patch.object(
            reask, 'write_channels', wraps=reask.write_channels))
        self.journal = self.home / 'original-journal.jsonl'
        self.journal.write_bytes(b'{"outcome":"UNKNOWN","intent":"original only"}\n')
        self.journal_bytes = self.journal.read_bytes()
        self.serial = 0

    def fixture(self, *, status=reask.WAITING, continuing=False, current=False,
                controller_present=True):
        self.serial += 1
        # Deliberately unsupported/nonexistent pins: a barrier may restrict this
        # owner without importing its controller or granting file-tick authority.
        controller = (reask.controller() if current else
                      dict(source=str(self.home / 'old-0.4.27'), python='/original/python',
                           files={'VERSION': 'old-pin', 'scripts/executor_reask.py': 'old-code'}))
        state = dict(version=3 if current else 2, caller_surface_uuid=CALLER,
                     supervisor=SUPERVISOR, workspace_uuid=WORKSPACE,
                     episode_id=f'original-episode-{self.serial}', task_id='original-task',
                     purpose='REQUEST_NEXT_TASK', state=status, started_epoch=1000.,
                     next_due_epoch=1000., mailbox=str(self.home / 'mailbox'),
                     channels=[str(self.home / 'channel')], ask_count=1, attempt_count=1,
                     asks=[dict(marker='original-ask', outcome='UNKNOWN', terminal=True,
                                journal=str(self.journal), origin={'controller': controller})],
                     terminal_pending=dict(marker='original-ask', outcome='UNKNOWN',
                                           journal=str(self.journal), controller=controller),
                     reply={'status': 'TASK', 'body': {'detail': '完整原始答复'}},
                     reply_history=[{'marker': 'earlier', 'body_sha256': 'original'}])
        if controller_present:
            state['controller'] = copy.deepcopy(controller)
        if current:
            state['file_protocol'] = protocol.descriptor()
        if continuing:
            authorization = self.home / 'authorization.txt'
            authorization.write_text('Explicit original continuing authorization.\n')
            state['continuation'] = dict(enabled=True, id='original-authorization', status='ACTIVE',
                                         overall_task='original overall task', owner=SUPERVISOR,
                                         authorized_epoch=900., until_epoch=None,
                                         authorization=lifecycle.authorization_pin(authorization),
                                         controller=copy.deepcopy(controller))
        self.write_original(state)
        return state

    def write_original(self, state):
        path = reask.state_path(CALLER)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.raw_state = (json.dumps(state, ensure_ascii=False, indent=2) + '\n').encode()
        path.write_bytes(self.raw_state)
        enrollment = scheduler.enrollment_path(CALLER)
        scheduler.root().mkdir(parents=True, exist_ok=True, mode=0o700)
        enrollment.parent.mkdir(parents=True, exist_ok=True)
        self.raw_enrollment = (json.dumps(dict(status='ENROLLED', binding={
            key: state.get(key) for key in scheduler.BINDING_KEYS},
            controller=state.get('controller'), original_journal=str(self.journal)),
            indent=3) + '\n').encode()
        enrollment.write_bytes(self.raw_enrollment)
        for path in (reask.inbox_path(state), Path(state['channels'][0]) / reask.current_name(CALLER)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'{"original_channel":"must survive preview"}\n')

    def marker(self, *, supervisor=SUPERVISOR, collaboration='collaboration-a',
               task='child-task', armed=1001.):
        return dict(marker_version=2, collaboration_id=collaboration, task_id=task,
                    workspace_uuid=WORKSPACE, artifact_root=str(self.home / 'task-evidence'),
                    participants=[dict(role='supervisor', surface_uuid=supervisor),
                                  dict(role='executor', surface_uuid=CALLER)],
                    armed_at=stamp(armed), last_activity_at=stamp(armed), ttl_seconds=1)

    def put_marker(self, marker, *, legacy=False):
        path = (self.active / (WORKSPACE + '.json') if legacy else
                self.active / WORKSPACE / (marker['collaboration_id'] + '.json'))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(marker))
        return path

    def boundary(self, marker, event='RESERVED', *, now=1002.):
        lifecycle.task_boundary(marker, event, now=now)
        return reask.load(CALLER)

    def observe(self, *, now=1002., publish=True):
        with reask.episode_lock(CALLER):
            state = reask.load(CALLER)
            lifecycle.observe_formal_tasks(state, now, publish=publish)
            if publish:
                reask.save(state)
            return state

    def snapshot(self, state):
        return (reask.root() / 'legacy-snapshots' / ready.digest(CALLER)
                / ready.digest(state['episode_id']))

    def files(self):
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in self.home.rglob('*') if p.is_file()}

    def assert_preserved(self, before, after):
        for key in ('version', 'caller_surface_uuid', 'supervisor', 'workspace_uuid',
                    'episode_id', 'task_id', 'controller', 'asks', 'terminal_pending',
                    'reply', 'reply_history', 'ask_count', 'attempt_count'):
            self.assertEqual((key in after, after.get(key)), (key in before, before.get(key)), key)
        if before.get('continuation'):
            for key, value in before['continuation'].items():
                if key != 'status':
                    self.assertEqual(after['continuation'][key], value, key)
        self.assertEqual(self.journal.read_bytes(), self.journal_bytes)
        self.assertEqual(scheduler.enrollment_path(CALLER).read_bytes(), self.raw_enrollment)

    def test_unsupported_027_ordinary_waiting_stops_for_real_new_owner(self):
        original = self.fixture()
        marker = self.marker(supervisor=SUCCESSOR)
        self.put_marker(marker)
        stopped = self.observe()
        self.assertEqual(stopped['state'], reask.STOPPED)
        self.assertEqual(stopped['exit_reason'], 'FORMAL_OWNER_CHANGED')
        self.assertEqual(stopped['formal_successor']['reserved_epoch'], 1001.)
        self.assertEqual(stopped['formal_successor']['supervisor'], SUCCESSOR)
        self.assertNotIn('continuation', stopped)
        self.assertEqual(ready.read_json(reask.inbox_path(stopped))['status'], reask.STOPPED)
        self.assert_preserved(original, stopped)

    def test_missing_top_controller_and_legacy_marker_location_are_supported(self):
        original = self.fixture(controller_present=False)
        self.put_marker(self.marker(supervisor=SUCCESSOR), legacy=True)
        stopped = self.observe()
        self.assertEqual(stopped['exit_reason'], 'FORMAL_OWNER_CHANGED')
        self.assertNotIn('controller', stopped)
        self.assert_preserved(original, stopped)

    def test_original_controller_conflict_does_not_disable_the_barrier(self):
        original = self.fixture(continuing=True)
        original['continuation']['controller']['source'] = '/another/original-release'
        self.write_original(original)
        stopped = self.boundary(self.marker(supervisor=SUCCESSOR))
        self.assertEqual(stopped['exit_reason'], 'FORMAL_OWNER_CHANGED')
        self.assertEqual(stopped['continuation']['status'], 'FORMAL_OWNER_CHANGED')
        self.assert_preserved(original, stopped)

    def test_original_armed_time_not_callback_time_or_heartbeat_controls_handover(self):
        original = self.fixture()
        for armed in (900., 1000., 1003.):
            for observed in (False, True):
                with self.subTest(armed=armed, observed=observed):
                    marker = self.marker(supervisor=SUCCESSOR, armed=armed)
                    marker['last_activity_at'] = stamp(999999.)
                    state = copy.deepcopy(original)
                    self.assertFalse(lifecycle.apply_boundary(
                        state, marker, 'RESERVED', 1002., observed=observed))
                    self.assertEqual(state, original)
        self.assertFalse(self.snapshot(original).exists())
        self.channels.assert_not_called()

    def test_observed_missing_or_invalid_armed_time_cannot_transfer_ownership(self):
        original = self.fixture()
        for armed in (None, '', 'bad', '1970-01-01T00:16:41', 1001., {}, []):
            with self.subTest(armed=armed):
                marker = self.marker(supervisor=SUCCESSOR)
                marker['armed_at'] = armed
                state = copy.deepcopy(original)
                self.assertFalse(lifecycle.apply_boundary(
                    state, marker, 'RESERVED', 1002., observed=True))
                self.assertEqual(state, original)
        marker.pop('armed_at')
        self.assertFalse(lifecycle.apply_boundary(
            state, marker, 'RESERVED', 1002., observed=True))
        self.assertFalse(self.snapshot(original).exists())

    def test_trusted_direct_reservation_keeps_timestamp_free_public_api(self):
        original = self.fixture()
        marker = self.marker(supervisor=SUCCESSOR)
        marker.pop('armed_at')
        stopped = self.boundary(marker)
        self.assertEqual(stopped['formal_successor']['reserved_epoch'], 1002.)
        self.assert_preserved(original, stopped)

    def test_wrong_binding_and_malformed_markers_never_mutate(self):
        original = self.fixture()
        valid = self.marker(supervisor=SUCCESSOR)
        changes = [dict(workspace_uuid=SUCCESSOR), dict(workspace_uuid=''),
                   dict(collaboration_id=''), dict(task_id=''), dict(task_id=[]),
                   dict(participants=None), dict(participants='bad'), dict(participants=[None]),
                   dict(participants=valid['participants'][:1]),
                   dict(participants=valid['participants'] + [valid['participants'][0]]),
                   dict(participants=[valid['participants'][0],
                                      dict(role='executor', surface_uuid=SUPERVISOR)])]
        for change in changes:
            with self.subTest(change=change):
                state = copy.deepcopy(original)
                self.assertFalse(lifecycle.apply_boundary(
                    state, dict(valid, **change), 'RESERVED', 1002.))
                self.assertEqual(state, original)
        self.assertFalse(self.snapshot(original).exists())
        self.channels.assert_not_called()

    def test_same_owner_pauses_ordinary_waiting_and_only_exact_release_unpauses(self):
        original = self.fixture()
        a = self.marker()
        b = self.marker(collaboration='collaboration-b')  # same task, different reservation
        state = self.boundary(a)
        state = self.boundary(b)
        self.assertEqual(len(state['paused_tasks']), 2)
        for change in (dict(collaboration_id='unrelated'), dict(task_id='another-task'),
                       dict(workspace_uuid=SUCCESSOR),
                       dict(participants=self.marker(supervisor=SUCCESSOR)['participants'])):
            self.assertEqual(self.boundary(dict(a, **change), 'RELEASED'), state)
        self.assertEqual(self.boundary(a), state)  # repeated observation is idempotent
        state = self.boundary(a, 'RELEASED', now=1010.)
        self.assertEqual(state['paused_tasks'], [dict(collaboration_id=b['collaboration_id'],
                                                     task_id=b['task_id'])])
        self.assertEqual(self.boundary(a, 'RELEASED', now=1020.), state)
        state = self.boundary(b, 'RELEASED', now=1030.)
        self.assertEqual(state['state'], reask.WAITING)
        self.assertEqual(state['paused_tasks'], [])
        self.assertEqual(state['next_due_epoch'], 1030.)
        self.assertNotIn('continuation', state)
        self.assert_preserved(original, state)

    def test_missing_marker_heartbeat_and_ttl_never_release_a_reservation(self):
        original = self.fixture()
        marker = self.marker()
        path = self.put_marker(marker)
        paused = self.observe()
        marker.update(last_activity_at=stamp(8000.), ttl_seconds=0)
        path.write_text(json.dumps(marker))
        self.assertEqual(self.observe(now=9000.), paused)
        path.unlink()  # Simulated legacy disappearance, without a formal release.
        self.assertEqual(self.observe(now=10000.), paused)
        self.assert_preserved(original, reask.load(CALLER))

    def test_formal_stop_cannot_be_reversed_by_release_or_marker_disappearance(self):
        original = self.fixture()
        marker = self.marker(supervisor=SUCCESSOR)
        path = self.put_marker(marker)
        stopped = self.observe()
        self.assertEqual(self.boundary(marker, 'RELEASED'), stopped)
        self.assertEqual(self.boundary(self.marker(), 'RELEASED'), stopped)
        path.unlink()
        self.assertEqual(self.observe(now=10000.), stopped)
        self.assert_preserved(original, stopped)

    def test_noncontinuing_terminal_states_and_unknown_states_are_untouched(self):
        for status in (reask.ANSWERED, reask.CONSUMED, reask.STOPPED, 'UNKNOWN'):
            with self.subTest(status=status):
                original = self.fixture(status=status)
                for event in ('RESERVED', 'RELEASED'):
                    self.assertEqual(self.boundary(self.marker(), event), original)
                    self.assertEqual(self.boundary(self.marker(supervisor=SUCCESSOR), event), original)
                self.put_marker(self.marker(supervisor=SUCCESSOR))
                self.assertEqual(self.observe(publish=False), original)
                self.assertFalse(self.snapshot(original).exists())
        self.channels.assert_not_called()

    def test_continuing_answered_can_pause_but_release_never_rearms_it(self):
        original = self.fixture(status=reask.ANSWERED, continuing=True)
        marker = self.marker()
        self.assertEqual(self.boundary(marker)['state'], reask.ANSWERED)
        released = self.boundary(marker, 'RELEASED')
        self.assertEqual(released['state'], reask.ANSWERED)
        self.assertEqual(released['paused_tasks'], [])
        self.assert_preserved(original, released)
        stopped = self.boundary(self.marker(supervisor=SUCCESSOR))
        self.assertEqual(stopped['state'], reask.STOPPED)
        self.assert_preserved(original, stopped)

    def test_terminal_exact_release_clears_only_matching_pause_without_revival(self):
        marker = self.marker()
        reservation = dict(collaboration_id=marker['collaboration_id'], task_id=marker['task_id'])
        other = dict(collaboration_id='keep-other', task_id=marker['task_id'])
        for status in (reask.ANSWERED, reask.CONSUMED, reask.STOPPED):
            with self.subTest(status=status):
                state = self.fixture(status=status)
                state.update(paused_tasks=[reservation, other], stopped_epoch=1001.,
                             exit_reason='ORIGINAL_TERMINAL_REASON')
                self.write_original(state)
                released = self.boundary(marker, 'RELEASED', now=1010.)
                self.assertEqual(released['paused_tasks'], [other])
                self.assertEqual(released['state'], status)
                for key in ('next_due_epoch', 'stopped_epoch', 'exit_reason'):
                    self.assertEqual(released[key], state[key])
                self.assertEqual(released['last_task_boundary']['event'], 'RELEASED')
                self.assertEqual(self.boundary(marker, 'RELEASED', now=1020.), released)
                self.assert_preserved(state, released)
        self.channels.assert_not_called()

    def test_terminal_wrong_release_and_unknown_state_keep_all_evidence(self):
        marker = self.marker()
        reservation = dict(collaboration_id=marker['collaboration_id'], task_id=marker['task_id'])
        for status in (reask.ANSWERED, reask.CONSUMED, reask.STOPPED, 'UNKNOWN'):
            state = self.fixture(status=status)
            state['paused_tasks'] = [reservation]
            self.write_original(state)
            variants = [dict(task_id='wrong'), dict(collaboration_id='wrong'),
                        dict(workspace_uuid=SUCCESSOR),
                        dict(participants=self.marker(supervisor=SUCCESSOR)['participants'])]
            if status == 'UNKNOWN':
                variants.append({})
            for change in variants:
                with self.subTest(status=status, change=change):
                    self.assertEqual(self.boundary(dict(marker, **change), 'RELEASED'), state)
            self.assertEqual(self.boundary(marker, 'RESERVED'), state)
        self.channels.assert_not_called()

    def test_expiry_and_operator_stop_at_release_clear_pause_but_keep_stop(self):
        marker = self.marker()
        reservation = dict(collaboration_id=marker['collaboration_id'], task_id=marker['task_id'])
        for cause in ('AUTHORIZATION_EXPIRED', 'OPERATOR_STOP'):
            state = self.fixture(continuing=True)
            state['paused_tasks'] = [reservation]
            if cause == 'AUTHORIZATION_EXPIRED':
                state['continuation']['until_epoch'] = 1001.
            else:
                ready.write_json(reask.stop_file(CALLER), {'reason': 'explicit stop'})
            self.write_original(state)
            stopped = self.boundary(marker, 'RELEASED', now=1010.)
            self.assertEqual(stopped['state'], reask.STOPPED)
            self.assertEqual(stopped['exit_reason'], cause)
            self.assertEqual(stopped['paused_tasks'], [])
            self.assertEqual(stopped['next_due_epoch'], state['next_due_epoch'])
            self.assert_preserved(state, stopped)

    def test_terminal_release_preview_does_not_publish_or_poll_authorization(self):
        state = self.fixture(status=reask.STOPPED, continuing=True)
        marker = self.marker()
        state['paused_tasks'] = [dict(collaboration_id=marker['collaboration_id'], task_id=marker['task_id'])]
        self.write_original(state)
        before = self.files()
        with patch.object(lifecycle, 'poll_authorization', side_effect=AssertionError('terminal poll')):
            preview = copy.deepcopy(state)
            self.assertTrue(lifecycle.apply_boundary(preview, marker, 'RELEASED', 1010., publish=False))
        self.assertEqual(preview['paused_tasks'], [])
        self.assertEqual(preview['state'], reask.STOPPED)
        self.assertEqual(self.files(), before)

    def test_operator_stop_without_continuation_is_preserved_before_publication(self):
        original = self.fixture()
        ready.write_json(reask.stop_file(CALLER), {'reason': 'operator stop'})
        stopped = self.boundary(self.marker())
        self.assertEqual(stopped['exit_reason'], 'OPERATOR_STOP')
        self.assertNotIn('continuation', stopped)
        self.assertEqual((self.snapshot(original) / 'state.json').read_bytes(), self.raw_state)
        self.assert_preserved(original, stopped)

    def test_expired_authority_is_snapshotted_before_channel_write(self):
        original = self.fixture(continuing=True)
        original['continuation']['until_epoch'] = 1001.
        self.write_original(original)
        write_channels = self.channels._mock_wraps

        def checked_write(state, *args, **kwargs):
            self.assertEqual((self.snapshot(original) / 'state.json').read_bytes(), self.raw_state)
            self.assertEqual((self.snapshot(original) / 'enrollment.json').read_bytes(), self.raw_enrollment)
            return write_channels(state, *args, **kwargs)

        self.channels.side_effect = checked_write
        stopped = self.boundary(self.marker())
        self.assertEqual(stopped['exit_reason'], 'AUTHORIZATION_EXPIRED')
        self.assert_preserved(original, stopped)

    def test_one_complete_original_byte_snapshot_survives_pause_release_and_stop(self):
        original = self.fixture()
        a, b = self.marker(), self.marker(collaboration='collaboration-b')
        self.boundary(a)
        directory = self.snapshot(original)
        initial = {p.name: (p.read_bytes(), p.stat().st_ino) for p in directory.iterdir()}
        self.boundary(b)
        self.boundary(a, 'RELEASED')
        self.boundary(self.marker(supervisor=SUCCESSOR))
        self.assertEqual(initial, {p.name: (p.read_bytes(), p.stat().st_ino)
                                  for p in directory.iterdir()})
        self.assertEqual(initial['state.json'][0], self.raw_state)
        self.assertEqual(initial['enrollment.json'][0], self.raw_enrollment)
        self.assertEqual(list(directory.parent.iterdir()), [directory])
        self.assertEqual(self.journal.read_bytes(), self.journal_bytes)

    def test_current_state_does_not_get_a_legacy_snapshot(self):
        original = self.fixture(current=True)
        self.boundary(self.marker())
        self.boundary(self.marker(), 'RELEASED')
        self.assertFalse(self.snapshot(original).exists())

    def test_snapshot_failure_prevents_state_mutation_and_channel_publication(self):
        original = self.fixture()
        for marker in (self.marker(), self.marker(supervisor=SUCCESSOR)):
            state = copy.deepcopy(original)
            with reask.episode_lock(CALLER):
                before = self.files()
                with patch.object(reask, 'preserve_legacy_snapshot', side_effect=OSError('disk full')):
                    with self.assertRaisesRegex(OSError, 'disk full'):
                        lifecycle.apply_boundary(state, marker, 'RESERVED', 1002.)
                self.assertEqual(state, original)
                self.assertEqual(self.files(), before)
        self.channels.assert_not_called()

    def test_apply_boundary_previews_pause_release_stop_and_operator_stop_without_writes(self):
        original = self.fixture()
        marker = self.marker()
        with reask.episode_lock(CALLER):
            before = self.files()
            with patch.object(reask, 'preserve_legacy_snapshot', side_effect=AssertionError('preview snapshot')), \
                    patch.object(ready, 'write_json', side_effect=AssertionError('preview write')):
                preview = copy.deepcopy(original)
                self.assertTrue(lifecycle.apply_boundary(preview, marker, 'RESERVED', 1002., publish=False))
                self.assertTrue(lifecycle.apply_boundary(preview, marker, 'RELEASED', 1003., publish=False))
                self.assertEqual(preview['paused_tasks'], [])
                self.assertTrue(lifecycle.apply_boundary(
                    preview, self.marker(supervisor=SUCCESSOR), 'RESERVED', 1004., publish=False))
                self.assertEqual(preview['exit_reason'], 'FORMAL_OWNER_CHANGED')
            self.assertEqual(self.files(), before)
        ready.write_json(reask.stop_file(CALLER), {'reason': 'stop'})
        with reask.episode_lock(CALLER):
            before = self.files()
            preview = copy.deepcopy(original)
            lifecycle.apply_boundary(preview, marker, 'RESERVED', 1002., publish=False)
            self.assertEqual(preview['exit_reason'], 'OPERATOR_STOP')
            self.assertEqual(self.files(), before)
        self.channels.assert_not_called()
        self.assertEqual(reask.load(CALLER), original)

    def test_authorization_preview_expiry_revocation_and_invalidity_write_nothing(self):
        original = self.fixture(continuing=True)
        changes = ({'until_epoch': 1001.}, {'status': 'REVOKED'},
                   {'until_epoch': float('inf')}, {'authorization': {'path': '/nonexistent/auth'}})
        with reask.episode_lock(CALLER):
            before = self.files()
            with patch.object(reask, 'preserve_legacy_snapshot', side_effect=AssertionError('preview snapshot')), \
                    patch.object(ready, 'write_json', side_effect=AssertionError('preview write')):
                for change in changes:
                    preview = copy.deepcopy(original)
                    preview['continuation'].update(change)
                    lifecycle.poll_authorization(preview, 1002., publish=False)
                    self.assertEqual(preview['state'], reask.STOPPED)
            self.assertEqual(self.files(), before)
        self.channels.assert_not_called()

    def test_observe_preview_is_equivalent_to_publication_without_mutating_live_state(self):
        original = self.fixture()
        self.put_marker(self.marker())
        self.put_marker(self.marker(supervisor=SUCCESSOR, collaboration='collaboration-b'))
        with reask.episode_lock(CALLER):
            before = self.files()
            preview = copy.deepcopy(original)
            with patch.object(reask, 'preserve_legacy_snapshot', side_effect=AssertionError('preview snapshot')), \
                    patch.object(ready, 'write_json', side_effect=AssertionError('preview write')):
                lifecycle.observe_formal_tasks(preview, 1002., publish=False)
            self.assertEqual(self.files(), before)
            self.assertEqual(reask.load(CALLER), original)
        self.channels.assert_not_called()
        self.assertEqual(self.observe(), preview)

    def test_marker_writer_never_acquires_registry_lock(self):
        original = self.fixture()
        with scheduler.registry_lock():
            with patch.object(scheduler, 'registry_lock', side_effect=AssertionError('lock inversion')):
                paused = self.boundary(self.marker())
        self.assertEqual(len(paused['paused_tasks']), 1)
        self.assert_preserved(original, paused)

    def test_busy_caller_rejects_release_until_observation_is_saved(self):
        self.fixture()
        marker = self.marker()
        self.put_marker(marker)
        observed, release = Event(), Event()

        def observer():
            with reask.episode_lock(CALLER):
                state = reask.load(CALLER)
                lifecycle.observe_formal_tasks(state, 1002.)
                observed.set()
                if not release.wait(3):
                    raise AssertionError('test did not release observer')
                reask.save(state)

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(observer)
            try:
                self.assertTrue(observed.wait(3))
                with self.assertRaisesRegex(RuntimeError, 'busy'):
                    self.boundary(marker, 'RELEASED', now=1003.)
            finally:
                release.set()
            future.result(timeout=3)
        self.assertEqual(len(reask.load(CALLER)['paused_tasks']), 1)
        self.assertEqual(self.boundary(marker, 'RELEASED', now=1004.)['paused_tasks'], [])


class ConsumeControllerTests(unittest.TestCase):
    def setUp(self):
        FormalBoundaryTests.setUp(self)
        from test_executor_reask_scheduler import consumer_fixture
        self.stack.enter_context(patch('executor_reask_identity.authenticate',
                                       side_effect=consumer_fixture))
    fixture = FormalBoundaryTests.fixture
    write_original = FormalBoundaryTests.write_original
    files = FormalBoundaryTests.files
    snapshot = FormalBoundaryTests.snapshot

    def answered(self, *, continuing=False):
        state = self.fixture(status=reask.ANSWERED, continuing=continuing, current=True)
        state['mailbox'] = str(ready.mailbox_dir(CALLER))
        state['reply'] = reask.reply_record(
            dict(marker='original-ask', status='TASK', detail='完整原答复\n第二行'),
            'original-mailbox', at_epoch=1001.)
        self.write_original(state)
        return state

    def enroll_v2(self, state, *, token=None, status='ENROLLED'):
        identity = state.pop('controller')
        state['version'] = 2
        path = scheduler.enrollment_path(CALLER)
        if token:
            state['registration'] = dict(token=token, enrollment=str(path))
        else:
            state['file_schedule'] = dict(enrollment=str(path), registered_epoch=1000.)
        self.write_original(state)
        record = dict(schema=scheduler.SCHEMA if token else scheduler.LEGACY_SCHEMA,
                      authorization='EXPLICIT_REASK_ENROLL_EXISTING' if token else 'EXPLICIT_REASK_START',
                      status=status, uid=os.getuid(), controller=identity,
                      binding=scheduler.binding(state), enrolled_epoch=1000.)
        if token:
            record['registration'] = copy.deepcopy(state['registration'])
        ready.write_json(path, record)
        return record

    def assert_rejected_without_writes(self, state):
        # Provision only the caller lock before comparing every evidence byte.
        with reask.episode_lock(CALLER):
            pass
        before = self.files()
        channel_calls = self.channels.call_count
        with ExitStack() as stack:
            for module, name in ((lifecycle, 'poll_authorization'), (reask, 'save'),
                                 (reask, 'preserve_legacy_snapshot'), (reask, 'tick_locked'),
                                 (ready, 'write_json'), (scheduler, 'registry_lock'),
                                 (scheduler, 'approved_release')):
                stack.enter_context(patch.object(
                    module, name, side_effect=AssertionError(f'rejected consume called {name}')))
            result = self.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        diagnostic = result.pop('controller_diagnostic')
        self.assertEqual(diagnostic['invoked_controller'], reask.controller())
        self.assertIn(diagnostic['original_identity'], ('RECORDED', 'UNKNOWN'))
        self.assertEqual(result, dict(
            outcome='ORIGINAL_CONTROLLER_REQUIRED', input_operations=0,
            consume_result=dict(status='ORIGINAL_CONTROLLER_REQUIRED', consumed=False,
                                episode_state=None, reservation_paused=False,
                                input_operations=0)))
        self.assertEqual(self.files(), before)
        self.assertEqual(self.channels.call_count, channel_calls)
        return diagnostic

    def assert_consumed(self, state, *, continuing=False):
        result = self.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        self.assertEqual(result['state'], reask.WAITING if continuing else reask.CONSUMED)
        for key in ('version', 'controller', 'episode_id', 'asks', 'terminal_pending'):
            self.assertEqual((key in result, result.get(key)), (key in state, state.get(key)), key)
        self.assertEqual(result['reply_history'][:-1], state['reply_history'])
        receipt = ready.read_json(Path(result['reply_history'][-1]['receipt']))
        self.assertEqual(receipt['body'], state['reply']['body'])
        self.assertEqual(receipt['body_sha256'], state['reply']['body_sha256'])
        self.assertEqual(receipt['consumed_epoch'], 1003.)
        self.assertEqual(result.get('rearm_count', 0), int(continuing))
        self.assertEqual(self.journal.read_bytes(), self.journal_bytes)
        return result

    def test_every_original_controller_component_is_required_before_any_write(self):
        for part in ('source', 'python', *protocol.FILES):
            with self.subTest(part=part):
                state = self.answered()
                if part in ('source', 'python'):
                    state['controller'][part] = str(self.home / ('old-' + part))
                else:
                    state['controller']['files'][part] = '0' * 64
                self.write_original(state)
                diagnostic = self.assert_rejected_without_writes(state)
                self.assertEqual(diagnostic['original_controller'], state['controller'])
                self.assertEqual(diagnostic['original_identity'], 'RECORDED')

    def test_cross_controller_precedes_authorization_expiry_and_revocation(self):
        for change in ({'until_epoch': 1002.}, {'status': 'REVOKED'}, {}):
            with self.subTest(change=change):
                state = self.answered(continuing=True)
                state['controller']['source'] = str(self.home / 'old-release')
                state['continuation']['controller'] = copy.deepcopy(state['controller'])
                state['continuation'].update(change)
                self.write_original(state)
                if not change:
                    Path(state['continuation']['authorization']['path']).write_text('revoked')
                self.assert_rejected_without_writes(state)

    def test_missing_or_conflicting_state_controller_cannot_be_repaired(self):
        for case in ('v3-missing', 'v2-missing', 'malformed', 'continuation-missing',
                     'continuation-conflict', 'v2-continuation-conflict'):
            with self.subTest(case=case):
                state = self.answered(continuing='continuation' in case)
                if case in ('v3-missing', 'v2-missing'):
                    state.pop('controller')
                    if case == 'v2-missing':
                        state['version'] = 2
                elif case == 'malformed':
                    state['controller'] = {}
                elif case == 'continuation-missing':
                    state['continuation'].pop('controller')
                else:
                    state['continuation']['controller']['source'] = str(self.home / 'other-release')
                    if case.startswith('v2-'):
                        state['version'] = 2
                self.write_original(state)
                diagnostic = self.assert_rejected_without_writes(state)
                self.assertIsNone(diagnostic['original_controller'])
                self.assertEqual(diagnostic['original_identity'], 'UNKNOWN')
                self.assertEqual(diagnostic['next_action'], 'INSPECT_ORIGINAL_EVIDENCE')

    def test_current_original_controller_can_consume_and_rearm(self):
        for continuing in (False, True):
            with self.subTest(continuing=continuing):
                self.assert_consumed(self.answered(continuing=continuing), continuing=continuing)

    def test_v2_continuation_supplies_original_identity_without_migration(self):
        state = self.answered(continuing=True)
        state['version'] = 2
        state.pop('controller')
        self.write_original(state)
        self.assert_consumed(state, continuing=True)

    def test_v2_bound_enrollment_supplies_identity_without_adding_controller(self):
        for status, token in (('ENROLLED', None), ('FINISHED', None), ('ENROLLED', 'a' * 32)):
            with self.subTest(status=status, token=token):
                state = self.answered()
                self.enroll_v2(state, status=status, token=token)
                before = scheduler.enrollment_path(CALLER).read_bytes()
                self.assert_consumed(state)
                self.assertEqual(scheduler.enrollment_path(CALLER).read_bytes(), before)

    def test_v2_cannot_borrow_another_binding_or_uncommitted_registration(self):
        changes = [('binding', key) for key in scheduler.BINDING_KEYS]
        changes += [('record', key) for key in
                    ('schema', 'uid', 'status', 'authorization', 'enrolled_epoch', 'registration', 'controller')]
        for section, key in changes:
            with self.subTest(section=section, key=key):
                state = self.answered()
                record = self.enroll_v2(state)
                if section == 'binding':
                    record['binding'][key] = 'different-original-binding'
                elif key == 'controller':
                    record['controller']['source'] = str(self.home / 'old-release')
                else:
                    record[key] = 'not-the-original-enrollment'
                ready.write_json(scheduler.enrollment_path(CALLER), record)
                self.assert_rejected_without_writes(state)
        for token in ('b' * 32, None, 'invalid'):
            with self.subTest(token=token):
                state = self.answered()
                record = self.enroll_v2(state, token='a' * 32)
                record['registration']['token'] = token
                ready.write_json(scheduler.enrollment_path(CALLER), record)
                self.assert_rejected_without_writes(state)

    def envelope(self, state, *, committed=False, corrupt=False):
        active = self.enroll_v2(state)
        token = f'{self.serial:032x}'
        path = scheduler.enrollment_path(CALLER)
        prepared = copy.deepcopy(active)
        prepared.update(schema=scheduler.SCHEMA, authorization='EXPLICIT_REASK_ENROLL_EXISTING',
                        registration=dict(token=token, enrollment=str(path)))
        prepared['controller']['source'] = str(self.home / 'different-controller')
        tx = dict(schema=scheduler.TRANSACTION_SCHEMA, token=token, caller_surface_uuid=CALLER,
                  active=active, prepared=prepared, status='PREPARING')
        previous_state = reask.state_path(CALLER).read_bytes()
        previous_enrollment = path.read_bytes()
        tx.update(previous_state_sha256=ready.digest(previous_state.decode()),
                  previous_enrollment_sha256=ready.digest(previous_enrollment.decode()))
        intent = scheduler.root() / 'transactions' / ready.digest(CALLER) / token / 'intent.json'
        raw = json.dumps(tx, sort_keys=True).encode()
        prepared_state = copy.deepcopy(state)
        prepared_state['registration'] = copy.deepcopy(prepared['registration'])
        reask.snapshot_files(intent.parent, {'intent.json': raw, 'previous-state.json': previous_state,
                                            'previous-enrollment.json': previous_enrollment,
                                            'previous-stop.json': None,
                                            'prepared-state.json': json.dumps(prepared_state).encode()},
                             dict(schema=scheduler.TRANSACTION_SCHEMA, caller=CALLER, token=token))
        envelope = dict(tx, intent=dict(path=str(intent), sha256=ready.digest(raw.decode())),
                        snapshot_sha256=scheduler._sha((intent.parent / 'INDEX.json').read_bytes()))
        if corrupt:
            envelope['intent']['sha256'] = '0' * 64
        ready.write_json(path, envelope)
        if committed:
            state['registration'] = copy.deepcopy(prepared['registration'])
            reask.save(state)

    def test_v2_envelope_uses_visible_state_token_and_never_recovers_intent(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                state = self.answered()
                self.envelope(state, committed=committed)
                before = scheduler.enrollment_path(CALLER).read_bytes()
                if committed:
                    self.assert_rejected_without_writes(state)
                else:
                    with patch.object(scheduler, 'registry_lock', side_effect=AssertionError('lock inversion')):
                        self.assert_consumed(state)
                self.assertEqual(scheduler.enrollment_path(CALLER).read_bytes(), before)

    def test_v2_corrupt_envelope_is_zero_write_failure(self):
        state = self.answered()
        self.envelope(state, corrupt=True)
        self.assert_rejected_without_writes(state)

    def test_original_expiry_still_stops_without_consumption(self):
        state = self.answered(continuing=True)
        state['continuation']['until_epoch'] = 1002.
        self.write_original(state)
        result = self.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        self.assertEqual(result['exit_reason'], 'AUTHORIZATION_EXPIRED')
        self.assertEqual(result['state'], reask.STOPPED)
        self.assertEqual(result['reply_history'], state['reply_history'])
        self.assertFalse((reask.root() / 'consumed').exists())

    def test_operator_stop_remains_effective_for_foreign_controller(self):
        state = self.answered(continuing=True)
        state['controller']['source'] = str(self.home / 'old-release')
        state['continuation']['controller'] = copy.deepcopy(state['controller'])
        self.write_original(state)
        ready.write_json(reask.stop_file(CALLER), {'reason': 'OPERATOR'})
        # A foreign consumer cannot mutate even a stopped original episode.
        self.assert_rejected_without_writes(state)
        # The existing file stop path remains effective without granting consume.
        with patch.object(lifecycle, 'poll_authorization', side_effect=AssertionError('poll after stop')):
            result = reask.tick(CALLER, now=1003., file_only=True)
        self.assertEqual(result['state'], reask.STOPPED)
        self.assertEqual(result['exit_reason'], 'OPERATOR_STOP')
        for key in ('controller', 'asks', 'terminal_pending', 'reply', 'reply_history'):
            self.assertEqual(result[key], state[key])
        self.assertNotIn('rearm_count', result)
        self.assertFalse((reask.root() / 'consumed').exists())
        self.assertEqual((self.snapshot(state) / 'state.json').read_bytes(), self.raw_state)
        self.assertEqual((self.snapshot(state) / 'enrollment.json').read_bytes(), self.raw_enrollment)


if __name__ == '__main__':
    unittest.main()

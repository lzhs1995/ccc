"""Registration crash recovery with small isolated files and no real inputs."""
import copy
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import executor_ready as ready
import executor_reask as reask
import executor_reask_lifecycle as lifecycle
import executor_reask_protocol as protocol
import executor_reask_scheduler as scheduler
import mac_harness as harness


CALLER = '11111111-1111-4111-8111-111111111111'
SUPERVISOR = '22222222-2222-4222-8222-222222222222'
WORKSPACE = '33333333-3333-4333-8333-333333333333'
PANE = '44444444-4444-4444-8444-444444444444'


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix='reask-registration-'))).resolve()
        self.active = self.home / 'active'
        self.channel = self.home / 'channel'
        self.stack.enter_context(patch.dict(os.environ, {
            'HOME': str(self.home), 'MAC_ACTIVE_MARKERS_DIR': str(self.active)}))
        self.stack.enter_context(patch.object(Path, 'home', return_value=self.home))
        # The harness also has an import-time absolute root independent of HOME.
        self.stack.enter_context(patch.object(harness, '_ACTIVE_DIR', self.active))
        self.stack.enter_context(patch.object(lifecycle, 'ACTIVE_DIR', self.active))
        current = dict(source=str(Path(reask.__file__).resolve().parents[1]),
                       python=str(Path(scheduler.sys.executable).resolve()))
        self.stack.enter_context(patch.object(scheduler, 'approved_release', return_value=current))
        self.bridge = Mock(spec_set=['pin_workspace'])
        self.bridge.pin_workspace.return_value = self.live()
        self.stack.enter_context(patch.object(ready, '_bridge', return_value=self.bridge))
        self.forbidden = []
        for module, name in ((subprocess, 'run'), (subprocess, 'Popen'),
                             (reask, 'default_send'), (reask, 'idle_binding'),
                             (ready, 'Tail'), (lifecycle, 'consume')):
            self.forbidden.append(self.stack.enter_context(patch.object(
                module, name, side_effect=AssertionError(f'forbidden operation: {name}'))))
        self.addCleanup(self.assert_no_external_operations)

    def assert_no_external_operations(self):
        for operation in self.forbidden:
            operation.assert_not_called()

    def live(self):
        return dict(caller_surface_uuid=CALLER, target_surface_uuid=SUPERVISOR,
                    workspace_uuid=WORKSPACE, target_pane_uuid=PANE)

    def start(self, task='original-task', now=1000.):
        return scheduler.start(self.live(), task_id=task, channels=[str(self.channel)], now=now)

    def local_episode(self, task='original-task', now=1000.):
        return reask.start(CALLER, SUPERVISOR, task, channels=[str(self.channel)],
                           workspace=WORKSPACE, now=now)

    def sweep(self, now=1001.):
        with patch.object(ready, '_bridge', side_effect=AssertionError('sweep cannot authenticate')) as bridge:
            result = scheduler.sweep(now=now)
        bridge.assert_not_called()
        self.assertEqual(result['status'], 'SWEPT', result)
        self.assertEqual(result['input_operations'], 0)
        return result

    def channel_bytes(self, state):
        paths = [reask.inbox_path(state), self.channel / reask.current_name(CALLER)]
        return {str(path): path.read_bytes() if path.exists() else None for path in paths}

    def publish(self, state, text='original file request', now=1000.):
        result = reask.write_channels(state, 'fixture-marker', text, now)
        self.assertFalse(result['errors'])
        self.assertEqual(len(result['written']), 2)

    def evidence(self, directory):
        return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}

    def envelope(self):
        value = ready.read_json(scheduler.enrollment_path(CALLER))
        self.assertEqual(value['schema'], scheduler.TRANSACTION_SCHEMA)
        self.assertEqual(value['status'], 'PREPARING')
        return value, Path(value['intent']['path']).parent

    def test_registry_contention_preserves_episode_registration_and_channels(self):
        state = self.start()
        self.publish(state)
        state_before = reask.state_path(CALLER).read_bytes()
        enrollment_before = scheduler.enrollment_path(CALLER).read_bytes()
        channels_before = self.channel_bytes(state)
        memory_before = copy.deepcopy(state)
        calls = {
            'start': lambda: self.start(),
            'enroll': lambda: scheduler.enroll(state, self.live(), now=1001.),
            'enroll-existing': lambda: scheduler.enroll_existing(
                self.live(), state['episode_id'], digest(enrollment_before), now=1001.),
        }
        for name, call in calls.items():
            with self.subTest(entrypoint=name):
                with scheduler.registry_lock(), patch.object(
                        reask, 'episode_lock', side_effect=AssertionError('registry not acquired')):
                    result = call()
                self.assertEqual(result, dict(outcome='REGISTRY_BUSY', input_operations=0,
                                              retry_same_arguments=True))
                self.assertEqual(reask.state_path(CALLER).read_bytes(), state_before)
                self.assertEqual(scheduler.enrollment_path(CALLER).read_bytes(), enrollment_before)
                self.assertEqual(self.channel_bytes(state), channels_before)
                self.assertEqual(state, memory_before)
                # The same request succeeds after the original lock is released.
                self.assertNotEqual(call().get('outcome'), 'REGISTRY_BUSY')
                self.assertTrue(scheduler.registered(reask.load(CALLER)))
                self.assertEqual(reask.state_path(CALLER).read_bytes(), state_before)
                self.assertEqual(scheduler.enrollment_path(CALLER).read_bytes(), enrollment_before)
                self.assertEqual(self.channel_bytes(state), channels_before)

    def test_first_registration_contention_creates_no_episode_or_enrollment(self):
        with scheduler.registry_lock():
            self.assertEqual(self.start()['outcome'], 'REGISTRY_BUSY')
        self.assertFalse(reask.state_path(CALLER).exists())
        self.assertFalse(scheduler.enrollment_path(CALLER).exists())
        self.assertFalse(self.channel.exists())
        self.assertFalse((ready.state_root() / 'supervisor-inbox-v1').exists())
        self.assertTrue(scheduler.registered(self.start()))

    def test_registration_cli_reports_registry_busy_with_nonzero_exit_and_no_tick(self):
        state = self.start()
        self.publish(state)
        state_before = reask.state_path(CALLER).read_bytes()
        enrollment_before = scheduler.enrollment_path(CALLER).read_bytes()
        channels_before = self.channel_bytes(state)
        commands = [
            ['start', '--supervisor', SUPERVISOR, '--task', 'original-task',
             '--channel', str(self.channel), '--file-only'],
            ['enroll-existing', '--episode', state['episode_id'],
             '--enrollment-sha256', digest(enrollment_before)],
        ]
        for command in commands:
            with self.subTest(command=command[0]):
                output = io.StringIO()
                with scheduler.registry_lock(), redirect_stdout(output), patch.object(
                        reask, 'tick', side_effect=AssertionError('busy registration cannot tick')):
                    code = reask.main(command + ['--caller-uuid', CALLER])
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output.getvalue()), dict(
                    outcome='REGISTRY_BUSY', input_operations=0, retry_same_arguments=True))
                self.assertEqual(reask.state_path(CALLER).read_bytes(), state_before)
                self.assertEqual(scheduler.enrollment_path(CALLER).read_bytes(), enrollment_before)
                self.assertEqual(self.channel_bytes(state), channels_before)

    def test_body_blocking_io_error_is_not_reclassified_as_registry_contention(self):
        state = self.start()
        enrollment_sha = digest(scheduler.enrollment_path(CALLER).read_bytes())
        calls = [lambda: self.start(),
                 lambda: scheduler.enroll(state, self.live()),
                 lambda: scheduler.enroll_existing(self.live(), state['episode_id'], enrollment_sha)]
        for call in calls:
            failure = BlockingIOError('downstream operation, not registry acquisition')
            with patch.object(scheduler, 'approved_release', side_effect=failure):
                with self.assertRaises(BlockingIOError) as raised:
                    call()
            self.assertIs(raised.exception, failure)

    def fail_before_state_save(self, task='replacement-task', now=1002.):
        with patch.object(reask, 'save', side_effect=OSError('injected before state save')) as save:
            result = self.start(task, now)
        save.assert_called_once()
        self.assertEqual(result['outcome'], 'REGISTRATION_UNCOMMITTED', result)
        self.assertEqual(result['input_operations'], 0)
        return self.envelope()

    def fail_after_state_commit(self, task='replacement-task', now=1002.):
        with patch.object(scheduler, '_post_commit', side_effect=OSError('injected postcommit')) as post:
            result = self.start(task, now)
        post.assert_called_once()
        self.assertEqual(result['registration_observation'], 'COMMITTED_RECOVERY_REQUIRED', result)
        envelope, directory = self.envelope()
        state = reask.load(CALLER)
        self.assertEqual(state['registration'], envelope['prepared']['registration'])
        self.assertEqual(state['episode_id'], result['episode_id'])
        return state, directory

    def assert_recovery_refused(self):
        path = scheduler.enrollment_path(CALLER)
        enrollment_before = path.read_bytes()
        state = reask.load(CALLER)
        state_before = reask.state_path(CALLER).read_bytes() if state else None
        channels_before = self.channel_bytes(state) if state else None
        with patch.object(reask, 'tick_locked', side_effect=AssertionError('invalid intent cannot tick')) as tick:
            result = self.sweep()
        tick.assert_not_called()
        self.assertEqual(result['file_posts'], 0)
        self.assertEqual(path.read_bytes(), enrollment_before)
        self.assertEqual(reask.state_path(CALLER).read_bytes() if state else None, state_before)
        if state:
            self.assertEqual(self.channel_bytes(state), channels_before)
        else:
            self.assertFalse(reask.state_path(CALLER).exists())

    def test_first_start_save_failure_recovers_absent_enrollment_without_state_or_tick(self):
        envelope, directory = self.fail_before_state_save(task='first-task', now=1000.)
        self.assertIsNone(reask.load(CALLER))
        intent = json.loads((directory / 'intent.json').read_bytes())
        self.assertEqual(intent['previous_state_sha256'], 'ABSENT')
        self.assertEqual(intent['previous_enrollment_sha256'], 'ABSENT')
        self.assertEqual(digest((directory / 'intent.json').read_bytes()), envelope['intent']['sha256'])
        original_evidence = self.evidence(directory)
        with patch.object(reask, 'tick_locked', side_effect=AssertionError('no episode may tick')) as tick:
            self.assertEqual(self.sweep()['file_posts'], 0)
            self.assertFalse(scheduler.enrollment_path(CALLER).exists())
            self.assertEqual(self.sweep(now=1002.)['episodes'], [])
        tick.assert_not_called()
        self.assertFalse(reask.state_path(CALLER).exists())
        self.assertFalse(self.channel.exists())
        self.assertFalse((ready.state_root() / 'supervisor-inbox-v1').exists())
        self.assertEqual(self.evidence(directory), original_evidence)

    def test_old_unenrolled_state_and_channels_survive_failed_replacement_and_recovery(self):
        old = self.local_episode()
        self.publish(old)
        state_before = reask.state_path(CALLER).read_bytes()
        channels_before = self.channel_bytes(old)
        _envelope, directory = self.fail_before_state_save()
        self.assertEqual(reask.state_path(CALLER).read_bytes(), state_before)
        self.assertEqual(self.channel_bytes(old), channels_before)
        self.assertEqual((directory / 'previous-state.json').read_bytes(), state_before)
        self.assertEqual(self.sweep()['file_posts'], 0)
        self.assertFalse(scheduler.enrollment_path(CALLER).exists())
        self.assertEqual(reask.state_path(CALLER).read_bytes(), state_before)
        self.assertEqual(self.channel_bytes(old), channels_before)

    def test_failed_replacement_restores_original_active_enrollment(self):
        self.start()
        self.sweep()
        old = reask.load(CALLER)
        state_before = reask.state_path(CALLER).read_bytes()
        enrollment_before = scheduler.enrollment_path(CALLER).read_bytes()
        channels_before = self.channel_bytes(old)
        envelope, directory = self.fail_before_state_save()
        self.assertNotEqual(envelope['prepared']['binding']['episode_id'], old['episode_id'])
        self.assertEqual(reask.state_path(CALLER).read_bytes(), state_before)
        self.assertEqual(self.channel_bytes(old), channels_before)
        self.assertEqual((directory / 'previous-enrollment.json').read_bytes(), enrollment_before)
        self.assertEqual(self.sweep(now=1003.)['file_posts'], 0)
        recovered = reask.load(CALLER)
        record = ready.read_json(scheduler.enrollment_path(CALLER))
        self.assertTrue(scheduler.registered(recovered))
        for key in ('episode_id', 'registration', 'controller', 'asks', 'terminal_pending'):
            self.assertEqual(recovered[key], old[key], key)
        self.assertEqual(record['registration'], old['registration'])
        self.assertEqual(self.channel_bytes(old), channels_before)

    def test_state_replace_then_directory_fsync_failure_recovers_same_commit_once(self):
        fsync = os.fsync
        failures = []

        def fail_live_directory(fd):
            state_path = reask.state_path(CALLER)
            info = os.fstat(fd)
            if not failures and state_path.exists() and stat.S_ISDIR(info.st_mode):
                parent = state_path.parent.stat()
                if (info.st_dev, info.st_ino) == (parent.st_dev, parent.st_ino):
                    failures.append(reask.load(CALLER))
                    raise OSError('injected directory fsync after visible state replace')
            return fsync(fd)

        with patch.object(os, 'fsync', side_effect=fail_live_directory):
            result = self.start()
        self.assertEqual(len(failures), 1)
        self.assertEqual(result['registration_observation'], 'COMMITTED_RECOVERY_REQUIRED', result)
        state = reask.load(CALLER)
        self.assertEqual(state, failures[0])
        self.assertEqual(state['ask_count'], 0)
        envelope, directory = self.envelope()
        self.assertEqual(state['registration'], envelope['prepared']['registration'])
        original_evidence = self.evidence(directory)
        self.assertEqual(self.sweep()['file_posts'], 1)
        self.assertEqual(self.sweep(now=1002.)['file_posts'], 0)
        recovered = reask.load(CALLER)
        self.assertEqual(recovered['episode_id'], state['episode_id'])
        self.assertEqual(recovered['registration'], state['registration'])
        self.assertEqual(recovered['ask_count'], 1)
        self.assertTrue(scheduler.registered(recovered))
        self.assertEqual(self.evidence(directory), original_evidence)
        self.assertEqual(list(directory.parent.iterdir()), [directory])

    def test_postcommit_retry_does_not_retire_a_channel_already_owned_by_new_episode(self):
        old = self.local_episode()
        self.publish(old)
        old_state = reask.state_path(CALLER).read_bytes()
        state, directory = self.fail_after_state_commit()
        self.assertNotEqual(state['episode_id'], old['episode_id'])
        self.assertEqual((directory / 'previous-state.json').read_bytes(), old_state)
        # Another file publisher may already have replaced the shared channel.
        self.publish(state, 'new episode owns this channel', now=1003.)
        channel_before = self.channel_bytes(state)
        post_commit = scheduler._post_commit
        observations = []

        def observe_cleanup(*args):
            post_commit(*args)
            observations.append(self.channel_bytes(state))

        with patch.object(scheduler, '_post_commit', side_effect=observe_cleanup):
            self.assertEqual(self.sweep(now=1004.)['file_posts'], 1)
        self.assertEqual(observations, [channel_before])
        self.assertEqual(self.sweep(now=1005.)['file_posts'], 0)
        recovered = reask.load(CALLER)
        self.assertEqual(recovered['episode_id'], state['episode_id'])
        self.assertEqual(recovered['registration'], state['registration'])
        self.assertTrue(scheduler.registered(recovered))
        for raw in self.channel_bytes(recovered).values():
            self.assertEqual(json.loads(raw)['episode_id'], state['episode_id'])
            self.assertEqual(json.loads(raw)['status'], reask.WAITING)

    def test_old_postcommit_intent_cannot_touch_a_later_episode_or_its_channels(self):
        old = self.local_episode()
        self.publish(old)
        self.fail_after_state_commit()
        later = self.local_episode(task='later-independent-task', now=1003.)
        self.publish(later, 'later episode evidence', now=1003.)
        self.assert_recovery_refused()
        self.assertEqual(reask.load(CALLER)['episode_id'], later['episode_id'])

    def test_changed_intent_cannot_recover_even_first_absent_state(self):
        _envelope, directory = self.fail_before_state_save()
        path = directory / 'intent.json'
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b'\n')
        path.chmod(0o400)
        self.assert_recovery_refused()

    def test_changed_index_identity_cannot_complete_visible_commit(self):
        self.fail_after_state_commit()
        _envelope, directory = self.envelope()
        path = directory / 'INDEX.json'
        value = json.loads(path.read_bytes())
        value['identity']['token'] = 'f' * 32
        ready.write_json(path, value)
        path.chmod(0o400)
        self.assert_recovery_refused()

    def test_index_missing_required_member_cannot_authorize_absent_recovery(self):
        _envelope, directory = self.fail_before_state_save()
        path = directory / 'INDEX.json'
        value = json.loads(path.read_bytes())
        del value['files']['prepared-state.json']
        ready.write_json(path, value)
        path.chmod(0o400)
        self.assert_recovery_refused()

    def test_changed_prepared_state_cannot_be_self_certified_by_index_bytes_and_sha(self):
        _state, directory = self.fail_after_state_commit()
        path = directory / 'prepared-state.json'
        changed = path.read_bytes() + b'\n'
        path.chmod(0o600)
        path.write_bytes(changed)
        path.chmod(0o400)
        index_path = directory / 'INDEX.json'
        index = json.loads(index_path.read_bytes())
        index['files'][path.name] = dict(bytes=len(changed), sha256=digest(changed))
        ready.write_json(index_path, index)
        index_path.chmod(0o400)
        self.assert_recovery_refused()

    def test_changed_preimage_is_rejected_even_with_consistent_index_pin(self):
        self.start()
        self.sweep()
        _envelope, directory = self.fail_before_state_save()
        originals = self.evidence(directory)
        for name in ('previous-state.json', 'previous-enrollment.json'):
            with self.subTest(member=name):
                path = directory / name
                changed = originals[name] + b'\n'
                path.chmod(0o600)
                path.write_bytes(changed)
                path.chmod(0o400)
                index = json.loads(originals['INDEX.json'])
                index['files'][name] = dict(bytes=len(changed), sha256=digest(changed))
                ready.write_json(directory / 'INDEX.json', index)
                (directory / 'INDEX.json').chmod(0o400)
                self.assert_recovery_refused()
                path.chmod(0o600)
                path.write_bytes(originals[name])
                path.chmod(0o400)
                ready.write_json(directory / 'INDEX.json', json.loads(originals['INDEX.json']))
                (directory / 'INDEX.json').chmod(0o400)

    def legacy_episode(self):
        # Independent tiny trust roots; validation still reads every real member.
        # Deliberately non-executable historical code must never be imported.
        # This fixture models the six-member, pre-file-protocol controller.
        # Adding a newer trusted release must not silently change that model.
        name = '0.4.30-ff75100795a9'
        source = self.home / '.local/share/multi-agent-collaboration/releases' / name / 'source'
        pins = {}
        for member in (*protocol.LEGACY_FILES, protocol.SCHEDULER,
                       'scripts/configuration_guardian.py', 'scripts/manage_install.py'):
            raw = ("raise AssertionError('old release code executed')\n# " + member).encode()
            path = source / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            path.chmod(0o444)
            pins[member] = dict(bytes=len(raw), sha256=digest(raw), executable=False)
        manifest = json.dumps(dict(files=pins), sort_keys=True).encode()
        (source.parent / 'MANIFEST.json').write_bytes(manifest)
        (source.parent / 'MANIFEST.json').chmod(0o444)
        self.stack.enter_context(patch.dict(protocol.LEGACY, {
            name: {key: pins[key]['sha256'] for key in (*protocol.LEGACY_FILES, protocol.SCHEDULER)}}))
        self.stack.enter_context(patch.dict(protocol.LEGACY_MANIFESTS, {name: digest(manifest)}))
        original = dict(source=str(source), python=str(self.home / 'original-python'),
                        files={key: pins[key]['sha256'] for key in protocol.LEGACY_FILES})
        state = self.local_episode()
        state.update(version=2, controller=original)
        state.pop('file_protocol')
        authorization = self.home / 'authorization.txt'
        authorization.write_bytes(b'Explicit continuing authorization for the original task.\n')
        state['continuation'] = dict(enabled=True, id='original-authorization', status='ACTIVE',
                                     overall_task='original overall task', owner=SUPERVISOR,
                                     authorized_epoch=900., until_epoch=None,
                                     authorization=lifecycle.authorization_pin(authorization),
                                     controller=copy.deepcopy(original))
        self.journal = self.home / 'original-journal.jsonl'
        self.journal.write_bytes(b'{"outcome":"UNKNOWN","intent":"preserve original"}\n')
        pending = dict(marker='UNKNOWN_original', outcome='UNKNOWN', terminal=True,
                       journal=str(self.journal), origin={'controller': copy.deepcopy(original)})
        state.update(terminal_pending=copy.deepcopy(pending), asks=[copy.deepcopy(pending)],
                     ask_count=1, attempt_count=1, reply_history=[{'marker': 'older-evidence'}])
        reask.state_path(CALLER).write_bytes((json.dumps(state, indent=2) + '\n').encode())
        self.publish(state)
        scheduler.secure_directory(scheduler.root())
        path = scheduler.enrollment_path(CALLER)
        scheduler.secure_directory(path.parent)
        # Real upgrade shape: a new original episode coexists with stale enrollment.
        old = dict(schema=scheduler.LEGACY_SCHEMA, status='ENROLLED', uid=os.getuid(),
                   authorization='EXPLICIT_REASK_START', enrolled_epoch=900.,
                   binding=dict(scheduler.binding(state), episode_id='stale-enrolled-episode',
                                supervisor=PANE), controller=copy.deepcopy(original),
                   live_binding=dict(self.live(), target_surface_uuid=PANE),
                   scheduler=dict(path=str(source / protocol.SCHEDULER),
                                  sha256=pins[protocol.SCHEDULER]['sha256'],
                                  python=original['python'], label=scheduler.LABEL))
        path.write_bytes((json.dumps(old, indent=3) + '\n').encode())
        self.assertTrue(protocol.supports_state(state))
        return state

    def test_enroll_existing_keeps_original_controller_unknown_and_complete_evidence(self):
        state = self.legacy_episode()
        state_before = reask.state_path(CALLER).read_bytes()
        enrollment_before = scheduler.enrollment_path(CALLER).read_bytes()
        channels_before = self.channel_bytes(state)
        journal_before = self.journal.read_bytes()
        with patch.object(reask, 'tick_locked', side_effect=AssertionError('enroll must not tick')) as tick:
            result = scheduler.enroll_existing(self.live(), state['episode_id'],
                                                digest(enrollment_before), now=1002.)
        tick.assert_not_called()
        expected = dict(state, registration=result['registration'], file_schedule=result['file_schedule'])
        self.assertEqual(result, expected)
        self.assertEqual(reask.load(CALLER), expected)
        self.assertEqual(self.channel_bytes(state), channels_before)
        self.assertEqual(self.journal.read_bytes(), journal_before)
        record = ready.read_json(scheduler.enrollment_path(CALLER))
        self.assertEqual(record['authorization'], 'EXPLICIT_REASK_ENROLL_EXISTING')
        self.assertEqual(record['controller'], state['controller'])
        self.assertTrue(scheduler.registered(result))
        directory = scheduler.root() / 'transactions' / ready.digest(CALLER) / result['registration']['token']
        self.assertEqual((directory / 'previous-state.json').read_bytes(), state_before)
        self.assertEqual((directory / 'previous-enrollment.json').read_bytes(), enrollment_before)
        snapshot = reask.root() / 'legacy-snapshots' / ready.digest(CALLER) / ready.digest(state['episode_id'])
        self.assertEqual((snapshot / 'state.json').read_bytes(), state_before)
        self.assertEqual((snapshot / 'enrollment.json').read_bytes(), enrollment_before)

    def test_enroll_existing_wrong_original_sha_refuses_without_state_channel_or_snapshot_writes(self):
        state = self.legacy_episode()
        state_before = reask.state_path(CALLER).read_bytes()
        path = scheduler.enrollment_path(CALLER)
        enrollment_before = path.read_bytes()
        channels_before = self.channel_bytes(state)
        with self.assertRaises(ValueError):
            scheduler.enroll_existing(self.live(), state['episode_id'], '0' * 64, now=1002.)
        self.assertEqual(path.read_bytes(), enrollment_before)
        self.assertEqual(reask.state_path(CALLER).read_bytes(), state_before)
        self.assertEqual(self.channel_bytes(state), channels_before)
        self.assertFalse((scheduler.root() / 'transactions').exists())
        self.assertFalse((reask.root() / 'legacy-snapshots').exists())


if __name__ == '__main__':
    unittest.main()

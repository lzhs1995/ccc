"""Consumer integration: real registrations, journals, locks, replies and receipts.

Only the OS process inventory, cmux inventory and approved test release are
substituted. Native session/file readers and consumer authentication run intact.
These isolated checks are not evidence of active-client installation or input.
"""
import copy
from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cmux_foreground_thread as foreground
import cmux_native_delivery as native
import cmux_workspace_guard as workspace
import executor_ready as ready
import executor_reask as reask
import executor_reask_identity as identity
import executor_reask_lifecycle as lifecycle
import executor_reask_scheduler as scheduler
import mac_harness

CALLER = '11111111-1111-4111-8111-111111111111'
SUP = '22222222-2222-4222-8222-222222222222'
WORKSPACE = '33333333-3333-4333-8333-333333333333'
PANE = '44444444-4444-4444-8444-444444444444'
SESSION = '55555555-5555-4555-8555-555555555555'
OTHER_SESSION = '66666666-6666-4666-8666-666666666666'
CLIENT_EPOCH = '77777777-7777-4777-8777-777777777777'
PID, SHELL_PID, DAEMON_PID = 501, 502, 700
REFUSED = (ValueError, native.NativeDeliveryError, OSError)


class ExecutorIdentityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(Path, 'home', return_value=self.home))
        self.stack.enter_context(patch.object(mac_harness, '_ACTIVE_DIR', self.home / 'markers'))
        self.stack.enter_context(patch.object(lifecycle, 'ACTIVE_DIR', self.home / 'markers'))
        self.stack.enter_context(patch.object(foreground, '_seen', set()))
        self.stack.enter_context(patch.object(scheduler, 'approved_release', return_value=dict(
            source=str(Path(reask.__file__).resolve().parents[1]),
            python=str(Path(sys.executable).resolve()))))
        self.pane, self.tree_tty, self.process_tty = PANE, 'ttys001', 'ttys001'
        self.parent = SHELL_PID
        self.stack.enter_context(patch.object(identity.os, 'getppid', side_effect=lambda: self.parent))
        self.stack.enter_context(patch.object(native.identity, 'process', side_effect=self.process))
        self.stack.enter_context(patch.object(native.subprocess, 'run', side_effect=self.ps))
        self.bridge = SimpleNamespace(_run=self.tree, pin_workspace=lambda *a, **kw: self.live())
        self.stack.enter_context(patch.object(ready, '_bridge', return_value=self.bridge))
        self.stack.enter_context(patch.object(workspace, 'caller_snapshot', side_effect=self.snapshot))
        self.select_provider('claude')

    def select_provider(self, provider):
        self.provider = provider
        exe = '/test/' + provider
        self.client = dict(pid=PID, ppid=490, birth=[int(time.time()) - 60, 12345],
                           executable=exe, file_identity=[1, 2, 3, 4],
                           argv=[exe, '--resume', SESSION],
                           env=dict(HOME=str(self.home), CMUX_SURFACE_ID=CALLER,
                                    CMUX_WORKSPACE_ID=WORKSPACE))
        self.processes = {
            PID: self.client,
            SHELL_PID: dict(pid=SHELL_PID, ppid=PID, birth=[1, 2],
                            executable='/bin/zsh', file_identity=[5, 6, 7, 8], argv=['zsh'], env={}),
        }
        self.proof = dict(session=SESSION, pid=PID, birth=self.client['birth'][:],
                          tty=self.process_tty, executable=exe, surface_uuid=CALLER,
                          daemon_pid=DAEMON_PID, argv_sha256='a' * 64,
                          foreground=dict(thread_id=SESSION))
        self.resolved = dict(caller=dict(surface_uuid=CALLER, workspace_uuid=WORKSPACE,
                                         pane_uuid=PANE, surface_type='terminal'))
        self.registration = (self.home / '.claude/sessions' / f'{PID}.json' if provider == 'claude'
                             else self.home / '.codex/credential-observations' / f'client-{PID}-thread.json')
        self.write_registration()
        root = native._native_root(provider)
        name = SESSION + '.jsonl' if provider == 'claude' else 'rollout-test-' + SESSION + '.jsonl'
        self.log = root / 'test' / name
        self.log.parent.mkdir(parents=True, exist_ok=True)
        meta = (dict(type='user', sessionId=SESSION) if provider == 'claude'
                else dict(type='session_meta', payload=dict(id=SESSION)))
        self.log.write_text(json.dumps(meta) + '\n' + json.dumps(dict(type='progress', text='x' * 9000)) + '\n')

    def write_registration(self, session=SESSION, **changes):
        self.registration.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.provider == 'claude':
            value = dict(pid=PID, kind='interactive', sessionId=session,
                         procStart=time.strftime('%a %b %e %H:%M:%S %Y', time.gmtime(self.client['birth'][0])))
        else:
            self.registration.parent.chmod(0o700)
            marker = self.registration.parent / 'enabled-v1'
            marker.write_bytes(b'ccc-request-credentials-v1\n')
            marker.chmod(0o600)
            value = dict(schema=1, pid=PID, purpose='client_foreground_thread',
                         client_epoch=CLIENT_EPOCH, published_at_ms=int(time.time() * 1000),
                         thread_id=session)
        value.update(changes)
        self.registration.write_text(json.dumps(value))
        self.registration.chmod(0o600)

    def process(self, pid, **_):
        if pid not in self.processes:
            raise native.NativeDeliveryError('test client no longer exists')
        return copy.deepcopy(self.processes[pid])

    def ps(self, args, **_):
        self.assertEqual(args[0], '/bin/ps')
        if args[-1] == 'pid=,comm=':
            out = '\n'.join(f"{p['pid']} {p['executable']}" for p in self.processes.values())
        elif args[-1] == 'tty=':
            out = self.process_tty + '\n'
        else:
            self.fail('unexpected process operation: ' + repr(args))
        return subprocess.CompletedProcess(args, 0, out)

    def tree(self, *args):
        self.assertEqual(args, ('tree', '--all', '--json', '--id-format', 'both'))
        return json.dumps(dict(windows=[dict(workspaces=[dict(id=WORKSPACE, panes=[dict(
            id=self.pane, surfaces=[dict(id=CALLER, type='terminal', tty=self.tree_tty)])])])]))

    def live(self):
        return dict(caller_surface_uuid=CALLER, target_surface_uuid=SUP,
                    workspace_uuid=WORKSPACE, target_pane_uuid=PANE, caller_pane_uuid=PANE)

    def snapshot(self):
        return copy.deepcopy(self.resolved), {}, {}, copy.deepcopy(self.proof)

    def begin(self, continuing=False):
        kwargs = {}
        if continuing:
            auth = self.home / 'user-authorization.txt'
            auth.write_text('Continue the original task and ask every 60 seconds until completion.')
            kwargs = dict(continuing=True, continuation=dict(enabled=True, owner=SUP,
                          overall_task='original overall task', authorization_file=str(auth)))
        result = scheduler.start(self.live(), task_id='identity-integration', now=1000.,
                                 authenticate_executor=True, **kwargs)
        self.assertEqual(result['state'], reask.WAITING)
        self.assertEqual(result['executor_identity']['session_id'], SESSION)
        self.assertEqual(result['executor_identity']['episode_binding']['episode_id'], result['episode_id'])
        return result

    def answered(self, now=1001.):
        scheduler.sweep(now=now)
        request = ready.read_json(reask.inbox_path(reask.load(CALLER)))
        body = dict(request['reply_template'], trigger='完整正文：继续原任务。',
                    next_due_epoch=now + 60, deadline_epoch=now + 120)
        ready.write_json(Path(request['reply_mailbox']), body)
        scheduler.sweep(now=now + 1)
        state = reask.load(CALLER)
        self.assertEqual(state['state'], reask.ANSWERED)
        return state

    def files(self):
        return {str(p.relative_to(ready.state_root())): p.read_bytes()
                for p in ready.state_root().rglob('*') if p.is_file() and p.suffix != '.lock'}

    def refused(self, digest, *, cli=False):
        before = self.files()
        with self.assertRaises(REFUSED):
            if cli:
                with redirect_stdout(io.StringIO()):
                    reask.main(['consume', '--caller-uuid', CALLER, '--reply-sha256', digest])
            else:
                lifecycle.consume(CALLER, digest, now=1003.)
        self.assertEqual(self.files(), before, 'rejected consumption must write no state or receipt')

    def test_claude_native_registration_and_real_consumption_receipt(self):
        original = self.begin()
        state = self.answered()
        result = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        receipt = ready.read_json(Path(result['consume_result']['receipt']))
        self.assertEqual(receipt['body'], state['reply']['body'])
        self.assertEqual(receipt['executor_authentication']['session_id'], SESSION)
        self.assertEqual(receipt['episode_id'], original['episode_id'])
        self.assertEqual(result['state'], reask.CONSUMED)
        self.assertEqual(result['consume_result']['input_operations'], 0)

    def test_registration_is_revalidated_not_inferred_from_resume_argv(self):
        self.registration.unlink()
        with self.assertRaisesRegex(native.NativeDeliveryError, 'NATIVE_CLIENT_SESSION_REQUIRED'):
            self.begin()
        self.assertIsNone(reask.load(CALLER))
        self.assertFalse(scheduler.enrollment_path(CALLER).exists())

    def test_malformed_or_foreign_claude_registration_cannot_enroll(self):
        for delta in (dict(pid=PID + 1), dict(kind='background'), dict(procStart='another birth'),
                      dict(sessionId='invalid')):
            with self.subTest(delta=delta):
                self.write_registration(**delta)
                with self.assertRaises(REFUSED):
                    self.begin()
                self.assertIsNone(reask.load(CALLER))
                self.assertFalse(scheduler.enrollment_path(CALLER).exists())

    def test_environment_alone_cannot_impersonate_original_claude(self):
        self.parent = DAEMON_PID
        self.processes[DAEMON_PID] = dict(pid=DAEMON_PID, ppid=PID, birth=[1, 2],
                                        executable='/test/codex', argv=['codex'],
                                        file_identity=[8, 8, 8, 8], env={})
        with patch.dict(os.environ, CMUX_SURFACE_ID=CALLER, CLAUDE_SESSION_ID=SESSION):
            with self.assertRaisesRegex(ValueError, 'not a tool of the original Claude'):
                self.begin()
        self.assertIsNone(reask.load(CALLER))

    def test_multiple_clients_claiming_the_surface_cannot_enroll(self):
        self.processes[DAEMON_PID] = dict(pid=DAEMON_PID, ppid=PID, birth=[1, 2],
                                        executable='/test/codex', argv=['codex'],
                                        file_identity=[8, 8, 8, 8], env=self.client['env'])
        with self.assertRaisesRegex(native.NativeDeliveryError, 'NATIVE_TARGET_PROCESS_NOT_UNIQUE'):
            self.begin()
        self.assertIsNone(reask.load(CALLER))
        self.assertFalse(scheduler.enrollment_path(CALLER).exists())

    def test_foreign_ancestor_is_refused_even_with_correct_environment(self):
        self.begin()
        state = self.answered()
        self.processes[SHELL_PID]['executable'] = '/test/python'
        self.refused(state['reply']['body_sha256'], cli=True)

    def test_changed_session_cannot_consume_ordinary_reply(self):
        self.changed_session(continuing=False)

    def test_changed_session_cannot_consume_continuing_reply(self):
        self.changed_session(continuing=True)

    def changed_session(self, continuing):
        self.begin(continuing)
        state = self.answered()
        self.write_registration(OTHER_SESSION)
        self.refused(state['reply']['body_sha256'], cli=True)

    def test_same_surface_replaced_process_birth_tty_or_pane_is_refused(self):
        self.begin()
        state = self.answered()
        digest = state['reply']['body_sha256']
        original = copy.deepcopy(self.client)
        for delta in (dict(pid=PID + 1), dict(birth=[1, 2]), dict(file_identity=[9, 9, 9, 9]),
                      dict(executable='/other/claude'),
                      dict(env=dict(CMUX_SURFACE_ID=SUP, CMUX_WORKSPACE_ID=WORKSPACE)),
                      dict(env=dict(CMUX_SURFACE_ID=CALLER, CMUX_WORKSPACE_ID=SUP))):
            with self.subTest(delta=delta):
                self.processes[PID] = dict(original, **delta)
                self.refused(digest)
        self.processes[PID] = original
        for attr, value in (('process_tty', 'ttys999'), ('tree_tty', 'ttys999'), ('pane', SUP)):
            with self.subTest(attr=attr):
                previous = getattr(self, attr)
                setattr(self, attr, value)
                self.refused(digest)
                setattr(self, attr, previous)

    def test_missing_registration_is_not_repaired_when_consuming(self):
        self.begin()
        state = self.answered()
        self.registration.unlink()
        self.refused(state['reply']['body_sha256'])
        self.assertFalse(self.registration.exists())

    def test_claude_ancestor_changes_during_authentication(self):
        self.begin()
        state = self.answered()
        calls = []
        def changed(pid, **kw):
            result = self.process(pid, **kw)
            if pid == SHELL_PID:
                calls.append(pid)
                if len(calls) > 1:
                    result['birth'] = [9, 9]
            return result
        with patch.object(native.identity, 'process', side_effect=changed):
            self.refused(state['reply']['body_sha256'])
        self.assertGreaterEqual(len(calls), 2)

    def test_native_log_append_allowed(self):
        self.begin()
        state = self.answered()
        with self.log.open('ab') as stream:
            stream.write(b'{"type":"progress","text":"appended"}\n')
        result = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        self.assertTrue(result['consume_result']['consumed'])

    def test_native_log_change_during_caller_resolution_is_refused(self):
        self.select_provider('codex')
        self.begin()
        state = self.answered()

        def changed_snapshot():
            result = self.snapshot()
            self.log.write_bytes(self.log.read_bytes()[:-1])
            return result

        with patch.object(workspace, 'caller_snapshot', side_effect=changed_snapshot) as observed:
            self.refused(state['reply']['body_sha256'])
        observed.assert_called_once()

    def test_native_session_change_during_caller_resolution_is_refused(self):
        self.select_provider('codex')
        self.begin()
        state = self.answered()

        def changed_snapshot():
            result = self.snapshot()
            self.write_registration(OTHER_SESSION)
            return result

        with patch.object(workspace, 'caller_snapshot', side_effect=changed_snapshot) as observed:
            self.refused(state['reply']['body_sha256'])
        observed.assert_called_once()

    def test_native_log_truncation_prefix_and_tail_changes_are_refused(self):
        self.begin()
        state = self.answered()
        raw = self.log.read_bytes()
        for changed in (raw[:-1], raw.replace(b'"type"', b'"tyPe"', 1), raw[:-5] + b'y' + raw[-4:]):
            with self.subTest(size=len(changed)):
                self.log.write_bytes(changed)
                self.refused(state['reply']['body_sha256'])
                self.log.write_bytes(raw)

    def test_identical_bytes_in_replacement_log_are_refused(self):
        self.begin()
        state = self.answered()
        replacement = self.log.with_suffix('.replacement')
        replacement.write_bytes(self.log.read_bytes())
        os.replace(replacement, self.log)
        self.refused(state['reply']['body_sha256'])

    def test_locked_episode_replacement_cannot_use_unlocked_identity(self):
        self.begin()
        state = self.answered()
        original_lock = reask.episode_lock
        locked_bytes = []
        @contextmanager
        def replaced(caller):
            with original_lock(caller):
                replacement = copy.deepcopy(state)
                replacement['episode_id'] = 'replacement-after-cli-entry'
                reask.save(replacement)
                locked_bytes.append(self.files())
                yield
        with patch.object(reask, 'episode_lock', replaced):
            with self.assertRaisesRegex(ValueError, 'no matching foreground consumer identity'):
                reask.main(['consume', '--caller-uuid', CALLER,
                            '--reply-sha256', state['reply']['body_sha256']])
        self.assertEqual(self.files(), locked_bytes[0])

    def test_real_episode_lock_contention_writes_nothing(self):
        self.begin(continuing=True)
        state = self.answered()
        before = self.files()
        with reask.episode_lock(CALLER):
            output = io.StringIO()
            with redirect_stdout(output), patch.object(reask, 'load') as load, \
                    patch.object(identity, 'authenticate') as authenticate:
                code = reask.main(['consume', '--caller-uuid', CALLER,
                                   '--reply-sha256', state['reply']['body_sha256']])
            self.assertEqual(code, 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result['outcome'], 'EPISODE_BUSY')
            self.assertEqual(result['input_operations'], 0)
            self.assertEqual(result['consume_result']['status'], 'EPISODE_BUSY')
            self.assertFalse(result['consume_result']['consumed'])
            self.assertIsNone(result['consume_result']['episode_state'])
            self.assertIsNone(result['consume_result']['reservation_paused'])
            self.assertTrue(result['consume_result']['retry_same_arguments'])
            self.assertEqual(result['consume_result']['retry_after_seconds'], 1)
            load.assert_not_called()
            authenticate.assert_not_called()
        self.assertEqual(self.files(), before)
        consumed = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        self.assertTrue(consumed['consume_result']['consumed'])
        self.assertEqual(consumed['consume_result']['body_sha256'], state['reply']['body_sha256'])
        self.assertEqual(consumed['rearm_count'], 1)

    def test_consume_body_io_failure_is_not_lock_contention(self):
        self.begin()
        state = self.answered()
        before = self.files()
        failure = BlockingIOError('downstream I/O, not lock acquisition')
        with patch.object(identity, 'authenticate', side_effect=failure):
            with self.assertRaises(BlockingIOError) as raised:
                lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.files(), before)

    def test_ordinary_consumption_requires_exact_body_sha(self):
        self.changed_body(continuing=False)

    def test_continuing_consumption_requires_exact_body_sha(self):
        self.changed_body(continuing=True)

    def changed_body(self, continuing):
        self.begin(continuing)
        state = self.answered()
        for value in ('', '0' * 64):
            with self.subTest(supplied_sha=value):
                self.refused(value, cli=True)
        changed = copy.deepcopy(state)
        changed['reply']['body']['trigger'] += 'tampered'
        reask.save(changed)
        self.refused(state['reply']['body_sha256'])

    def test_two_rounds_rearm_only_after_authenticated_full_body_consumption(self):
        original = self.begin(continuing=True)
        markers = []
        for index in range(2):
            now = 1001. + 60 * index
            state = self.answered(now)
            markers.append(state['reply']['marker'])
            self.assertEqual(scheduler.sweep(now=now + 60)['file_posts'], 0)
            consumed = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=now + 2)
            self.assertEqual(consumed['episode_id'], original['episode_id'])
            self.assertEqual(consumed['rearm_count'], index + 1)
            self.assertEqual(consumed['state'], reask.WAITING)
            receipt = ready.read_json(Path(consumed['consume_result']['receipt']))
            self.assertEqual(receipt['body'], state['reply']['body'])
            self.assertEqual(receipt['executor_authentication']['process']['pid'], PID)
            self.assertEqual(scheduler.sweep(now=now + 59)['file_posts'], 0)
        self.assertEqual(len(set(markers)), 2)

    def test_receipt_commit_then_state_failure_recovers_with_original_identity_only(self):
        self.begin(continuing=True)
        state = self.answered()
        digest = state['reply']['body_sha256']
        with patch.object(reask, 'save', side_effect=OSError('injected state write failure')):
            with self.assertRaisesRegex(OSError, 'state write failure'):
                lifecycle.consume(CALLER, digest, now=1003.)
        self.assertEqual(reask.load(CALLER), state)
        receipts = list((reask.root() / 'consumed').rglob('*.json'))
        self.assertEqual(len(receipts), 1)
        receipt_before = receipts[0].read_bytes()
        self.write_registration(OTHER_SESSION)
        self.refused(digest)
        self.write_registration()
        recovered = lifecycle.consume(CALLER, digest, now=1010.)
        self.assertEqual(recovered['reply_consumed_epoch'], 1003.)
        self.assertEqual(recovered['rearm_count'], 1)
        self.assertEqual(receipts[0].read_bytes(), receipt_before)
        self.assertEqual(len(recovered['reply_history']), 1)

    def test_prior_receipt_identity_cannot_be_rebound(self):
        self.begin(continuing=True)
        state = self.answered()
        result = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        receipt_path = Path(result['consume_result']['receipt'])
        receipt = ready.read_json(receipt_path)
        reask.save(state)
        receipt['executor_authentication']['session_id'] = OTHER_SESSION
        ready.write_json(receipt_path, receipt)
        self.refused(state['reply']['body_sha256'])

    def test_codex_root_uses_native_foreground_file_and_distinct_daemon(self):
        self.select_provider('codex')
        self.begin()
        state = self.answered()
        result = lifecycle.consume(CALLER, state['reply']['body_sha256'], now=1003.)
        receipt = ready.read_json(Path(result['consume_result']['receipt']))
        proof = receipt['executor_authentication']['caller_proof']
        self.assertEqual((proof['pid'], proof['daemon_pid']), (PID, DAEMON_PID))
        self.assertEqual(proof['foreground']['thread_id'], SESSION)

    def test_codex_missing_foreground_registration_cannot_use_resume_argv(self):
        self.select_provider('codex')
        self.registration.unlink()
        with self.assertRaisesRegex(native.NativeDeliveryError, 'NATIVE_CLIENT_SESSION_REQUIRED'):
            self.begin()
        self.assertIsNone(reask.load(CALLER))

    def test_codex_child_cannot_consume_roots_reply_via_inherited_selector(self):
        self.select_provider('codex')
        self.begin(continuing=True)
        state = self.answered()
        self.proof['session'] = OTHER_SESSION
        # The client/foreground file/environment still point at root.
        self.refused(state['reply']['body_sha256'], cli=True)

    def test_codex_shared_daemon_or_incomplete_proof_is_not_client_authority(self):
        self.select_provider('codex')
        self.begin()
        state = self.answered()
        original = copy.deepcopy(self.proof)
        for proof in (None, {}, dict(original, pid=DAEMON_PID), dict(original, daemon_pid=None),
                      dict(original, argv_sha256='bad'), dict(original, birth=[0, 0]),
                      dict(original, foreground=dict(thread_id=OTHER_SESSION))):
            with self.subTest(proof=proof):
                self.proof = proof
                self.refused(state['reply']['body_sha256'])
        self.proof = original

    def test_codex_changed_well_formed_caller_proof_cannot_consume(self):
        self.select_provider('codex')
        self.begin()
        state = self.answered()
        original = copy.deepcopy(self.proof)
        for delta in (dict(daemon_pid=DAEMON_PID + 1), dict(argv_sha256='b' * 64),
                      dict(foreground=dict(thread_id=SESSION, extra_pin='changed'))):
            with self.subTest(delta=delta):
                self.proof = dict(original, **delta)
                self.refused(state['reply']['body_sha256'])
        self.proof = original

    def test_codex_switched_native_foreground_is_refused(self):
        self.select_provider('codex')
        self.begin(continuing=True)
        state = self.answered()
        self.write_registration(OTHER_SESSION)
        self.refused(state['reply']['body_sha256'])

    def test_codex_registration_mode_and_process_epoch_remain_checked(self):
        self.select_provider('codex')
        self.begin()
        state = self.answered()
        self.registration.chmod(0o644)
        self.refused(state['reply']['body_sha256'])
        self.write_registration(published_at_ms=1)
        self.refused(state['reply']['body_sha256'])


if __name__ == '__main__':
    unittest.main()

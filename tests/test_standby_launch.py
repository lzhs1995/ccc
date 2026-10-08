"""No native execution: exact argv, permanent claim and activation/Hook joins."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from tests.context_fixture import enter_context
from unittest.mock import patch
import uuid

import cmux_codex_watch as core
import ccc_workspace_batch as batch
import ccc_standby_launch as launch
import ccc_standby_environment as native_environment
import ccc_native_standby as standby
from tests import test_batch_argv_initial as fixtures


class StandbyLaunchTests(unittest.TestCase):
    def setUp(self):
        fixtures.ArgvInitialTests.setUp(self)
        fixtures.ArgvInitialTests.prepare(self)
        # Synthetic job only. Production code never converts an existing job.
        self.worker.job.pop('initial_prompt_policy')
        self.boot = str(uuid.uuid4())
        self.gen = 'a' * 64
        self.target_environment = {'HOME': str(self.root.resolve()),
            'CODEX_HOME': str((self.root / 'standby-home').resolve()),
            'PATH': '/selected/bin', 'API_KEY': 'test-selected-credential'}
        self.worker.job.update(standby_policy=standby.POLICY, standby_mode='b',
            standby_environment_sha256=native_environment.signature(self.target_environment),
            standby_cohort_id=str(uuid.uuid4()), standby_generation=self.gen,
            standby_boot_id=self.boot, native_runtime_policy=batch.NATIVE_RUNTIME_POLICY)
        self.worker.save()
        self.addCleanup(self.worker.close)
        self.exec_mock = enter_context(self, patch.object(os, 'execve'))
        enter_context(self, patch('ccc_batch_guard.native_binary', return_value=sys.executable))
        self.birth_mock = enter_context(self, patch('ccc_guard_scope.birth', return_value=[1234, 5678]))
        enter_context(self, patch('ccc_batch_timing.boot_id', return_value=self.boot))
        enter_context(self, patch.object(batch, 'register'))
        enter_context(self, patch.object(batch, '_bootstrap_client', return_value=self.client))
        enter_context(self, patch.dict(os.environ, {'CMUX_SURFACE_ID': self.slot['surface_id'],
            'CMUX_WORKSPACE_ID': self.wid}))

    def start_native(self, **changes):
        return launch.launch_registered(self.config, self.worker.job['id'], 0, self.slot['launch_id'],
            generation_current=changes.get('generation_current', lambda: self.gen),
            environment_current=changes.get('environment_current', lambda: dict(self.target_environment)))

    def test_exact_no_prompt_argv_is_consumed_before_guarded_exec(self):
        argv = launch.launch_argv(self.config, self.worker.job, 0)
        self.start_native()
        self.exec_mock.assert_called_once()
        self.assertEqual(self.exec_mock.call_args.args[:2], (argv[0], argv))
        claim = json.loads(launch.claim_path(self.config, self.worker.job['id'], 0).read_bytes())
        self.assertEqual(claim['argv'], argv)
        self.assertEqual(claim['policy'], standby.POLICY)
        self.assertEqual(claim['requested_environment']['CODEX_CLIENT_THREAD_OBSERVER'], '1')
        self.assertEqual(self.exec_mock.call_args.args[2]['CODEX_CLIENT_THREAD_OBSERVER'], '1')
        self.assertNotIn(batch.PROMPT, argv)
        self.assertNotIn(batch.LEGACY_PROMPT, argv)
        self.assertFalse((self.worker.path.parent / 'initial-argv-0.json').exists())
        # Every token after argv[0] belongs to an explicit option/value pair.
        self.assertEqual(len(argv[1:]) % 2, 0)
        self.assertTrue(all(k in ('--cd', '-c', '--disable', '--enable') for k in argv[1::2]))
        self.assertIn('ccc_standby_launch.py', ' '.join(argv))
        self.assertNotIn('bind-initial', ' '.join(argv))
        hold = core.batch_start_hold(self.store.load()['workspace_rules'][0], self.slot['surface_id'])
        self.assertEqual(hold['job_id'], self.worker.job['id'])
        with self.assertRaises(FileExistsError):
            self.start_native()
        self.assertEqual(self.exec_mock.call_count, 1)

    def test_standby_preserves_original_skill_feature_selection(self):
        for options in ([], ['--enable', 'skill_search'],
                        ['--disable', 'skill_search'],
                        ['-c', 'features.skill_search=true'],
                        ['-c', 'tui.show_tooltips=true', '--enable', 'skill_search']):
            with self.subTest(options=options):
                job = copy.deepcopy(self.worker.job)
                job['standby_target'] = {
                    'argv': [sys.executable, *options], 'provider': 'custom',
                    'upstream_url': 'https://example.invalid/v1',
                    'route_urls': [f'http://127.0.0.1:43210/{"a" * 48}/{i}'
                                   for i in range(50)]}
                argv = launch.launch_argv(self.config, job, 0)
                self.assertEqual(argv[1:1 + len(options)], options)
                skill_options = [v for v in argv if 'skill_search' in v]
                self.assertEqual(skill_options, [v for v in options if 'skill_search' in v])
                tooltip_options = [v for v in argv if v.startswith('tui.show_tooltips=')]
                self.assertEqual(tooltip_options[-1], 'tui.show_tooltips=false')
        self.exec_mock.assert_not_called()

    def membership_sequence(self, values):
        original = self.client.workspace_tree(self.wid)
        absent = copy.deepcopy(original)
        for window in absent['windows']:
            for workspace in window['workspaces']:
                for pane in workspace['panes']:
                    pane['surfaces'] = []
        return patch.object(self.client, 'workspace_tree',
                            side_effect=[original if v else absent for v in values])

    def test_created_surface_waits_for_initial_tree_visibility_without_recreation(self):
        with self.membership_sequence([False, False, True, True]), \
                patch.object(launch.time, 'sleep') as wait:
            self.start_native()
        self.assertEqual(wait.call_count, 2)
        self.exec_mock.assert_called_once()
        batch.register.assert_called_once()
        self.assertFalse(self.client.sent)

    def test_initial_tree_wait_has_monotonic_deadline_and_keeps_claim(self):
        clock = [10.0]
        def advance(_):
            clock[0] = 16.0
        with self.membership_sequence([False, False]), \
                patch.object(launch.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(launch.time, 'sleep', side_effect=advance):
            with self.assertRaisesRegex(ValueError, 'visibility deadline'):
                self.start_native()
        self.exec_mock.assert_not_called()
        self.assertTrue(launch.claim_path(self.config, self.worker.job['id'], 0).exists())

    def test_initial_tree_wait_rechecks_birth_before_another_read(self):
        def revoke(_):
            self.birth_mock.return_value = None
        with self.membership_sequence([False]) as tree, \
                patch.object(launch.time, 'sleep', side_effect=revoke):
            with self.assertRaises(ValueError):
                self.start_native()
        self.assertEqual(tree.call_count, 1)
        self.exec_mock.assert_not_called()

    def test_visible_surface_disappearance_at_exec_is_not_retried(self):
        with self.membership_sequence([True, False]), patch.object(launch.time, 'sleep') as wait:
            with self.assertRaises(core.CmuxError):
                self.start_native()
        wait.assert_not_called()
        self.exec_mock.assert_not_called()

    def test_initial_tree_rpc_failure_is_not_retried(self):
        with patch.object(self.client, 'workspace_tree', side_effect=core.CmuxError('RPC failed')), \
                patch.object(launch.time, 'sleep') as wait:
            with self.assertRaisesRegex(core.CmuxError, 'RPC failed'):
                self.start_native()
        wait.assert_not_called()
        self.exec_mock.assert_not_called()

    def test_tree_visibility_returning_after_deadline_does_not_exec(self):
        clock = [10.0]
        original = self.client.workspace_tree(self.wid)
        def delayed_tree(_):
            clock[0] = 16.0
            return original
        with patch.object(self.client, 'workspace_tree', side_effect=delayed_tree), \
                patch.object(launch.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(ValueError, 'visibility deadline'):
                self.start_native()
        self.exec_mock.assert_not_called()

    def test_initial_tree_duplicate_identity_does_not_exec(self):
        tree = copy.deepcopy(self.client.workspace_tree(self.wid))
        tree['windows'].extend(copy.deepcopy(tree['windows']))
        with patch.object(self.client, 'workspace_tree', return_value=tree), \
                patch.object(launch.time, 'sleep') as wait:
            with self.assertRaises(ValueError):
                self.start_native()
        wait.assert_not_called()
        self.exec_mock.assert_not_called()

    def test_wrong_mode_mixed_policy_and_legacy_are_rejected(self):
        for key, value in (('standby_mode', 'B'), ('standby_policy', 'old'),
                           ('initial_prompt_policy', batch.ARGV_INITIAL_POLICY),
                           ('initial_prompt', batch.LEGACY_PROMPT), ('name_policy', 'before-first-turn-v1'),
                           ('native_runtime_policy', None), ('standby_generation', ''),
                           ('native_trace_policy', batch.NATIVE_TRACE_POLICY)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                launch.launch_argv(self.config, {**self.worker.job, key:value}, 0)
        self.exec_mock.assert_not_called()

    def test_exec_uses_target_credentials_not_new_shell_and_claim_has_only_hashes(self):
        with patch.dict(os.environ, {'PATH': '/wrong/bin', 'API_KEY': 'wrong-key',
                'NEW_CREDENTIAL': 'unselected', 'CODEX_THREAD_ID': 'parent-thread'}):
            self.start_native()
        environment = self.exec_mock.call_args.args[2]
        for key, value in self.target_environment.items():
            self.assertEqual(environment[key], value)
        self.assertNotIn('NEW_CREDENTIAL', environment)
        self.assertNotIn('CODEX_THREAD_ID', environment)
        raw = launch.claim_path(self.config, self.worker.job['id'], 0).read_bytes()
        self.assertNotIn(b'test-selected-credential', raw)
        self.assertNotIn(b'wrong-key', raw)
        claim = json.loads(raw)
        self.assertEqual(claim['environment_sha256'], native_environment.signature(environment))
        self.assertEqual(environment['CMUX_SURFACE_ID'], self.slot['surface_id'])

    def test_environment_revoked_after_entering_cwd_prevents_exec(self):
        previous = Path.cwd()
        def current():
            value = dict(self.target_environment)
            if Path.cwd() != previous:
                value['API_KEY'] = 'changed-at-final-guard'
            return value
        with self.assertRaises(ValueError):
            self.start_native(environment_current=current)
        self.exec_mock.assert_not_called()
        self.assertEqual(Path.cwd(), previous)
        self.assertTrue(launch.claim_path(self.config, self.worker.job['id'], 0).exists())

    def test_wrong_admitted_environment_refuses_before_registration(self):
        self.worker.job['standby_environment_sha256'] = 'b' * 64
        self.worker.save()
        with self.assertRaises(ValueError):
            self.start_native()
        batch.register.assert_not_called()
        self.exec_mock.assert_not_called()

    def test_surface_identity_change_in_environment_callback_refuses_exec(self):
        previous = Path.cwd()
        def current():
            if Path.cwd() != previous:
                os.environ['CMUX_SURFACE_ID'] = str(uuid.uuid4())
            return dict(self.target_environment)
        with self.assertRaises(ValueError):
            self.start_native(environment_current=current)
        self.exec_mock.assert_not_called()

    def test_actual_exec_enters_declared_cwd_and_restores_mock_caller(self):
        previous = Path.cwd()
        observed = []
        self.exec_mock.side_effect = lambda *_: observed.append(Path.cwd())
        self.start_native()
        claim = json.loads(launch.claim_path(self.config, self.worker.job['id'], 0).read_bytes())
        self.assertEqual(observed, [Path(claim['cwd'])])
        self.assertEqual(Path.cwd(), previous)

    def test_cwd_change_during_last_authorization_refuses_exec(self):
        previous = Path.cwd()
        def generation():
            if Path.cwd() != previous:
                os.chdir(previous)
            return self.gen
        with self.assertRaisesRegex(ValueError, 'OS working directory changed'):
            self.start_native(generation_current=generation)
        self.exec_mock.assert_not_called()
        self.assertEqual(Path.cwd(), previous)
        self.assertTrue(launch.claim_path(self.config, self.worker.job['id'], 0).exists())

    def test_legacy_entrypoints_cannot_submit_or_resume_standby(self):
        before = self.worker.path.read_bytes()
        with patch.object(batch.subprocess, 'Popen') as spawn:
            for invoke in (
                    lambda: batch._launch(self.config, self.worker.job),
                    lambda: batch.BatchWorker(self.config, self.worker.job['id'], client=self.client),
                    lambda: batch.launch_registered(self.config, self.worker.job['id'], 0, self.slot['launch_id']),
                    lambda: batch.start(self.config, self.wid, client=self.client, launch=False, private_check=True)):
                with self.assertRaises(RuntimeError):
                    invoke()
            reconciler = batch.BatchReconciler(self.config, self.client)
            with patch.object(reconciler, '_worker') as worker:
                reconciler.cycle()
                worker.assert_not_called()
            spawn.assert_not_called()
        self.assertEqual(self.worker.path.read_bytes(), before)
        self.assertFalse(self.client.sent)
        self.exec_mock.assert_not_called()

    def test_generation_change_after_claim_prevents_exec_and_keeps_claim(self):
        calls = []
        def generation():
            calls.append(1)
            return self.gen if len(calls) == 1 else 'b' * 64
        with self.assertRaises(ValueError):
            self.start_native(generation_current=generation)
        self.exec_mock.assert_not_called()
        self.assertTrue(launch.claim_path(self.config, self.worker.job['id'], 0).exists())

    def test_exec_final_guards_recheck_permission_birth_hold_and_policy(self):
        checks = []
        def inspect(config, job_id, index, record, argv, first, final, *, environment):
            for check in (first, final):
                self.assertTrue(check())
                self.birth_mock.return_value = [1234, 5679]
                self.assertFalse(check())
                self.birth_mock.return_value = [1234, 5678]
                with patch.object(batch, 'allowed', return_value=False):
                    self.assertFalse(check())
                with patch.object(core, 'batch_start_hold', return_value={}):
                    self.assertFalse(check())
                original = self.worker.path.read_bytes()
                changed = json.loads(original)
                changed['standby_generation'] = 'b'*64
                core.atomic_write_json(self.worker.path, changed)
                self.assertFalse(check())
                self.worker.path.write_bytes(original)
                checks.append(True)
        with patch.object(launch, 'exec_claimed', side_effect=inspect):
            self.start_native()
        self.assertEqual(checks, [True, True])
        self.exec_mock.assert_not_called()

    def hook_fixture(self):
        self.start_native()
        self.claim_path = launch.claim_path(self.config, self.worker.job['id'], 0)
        self.claim = json.loads(self.claim_path.read_bytes())
        self.native_home = (self.root / 'standby-home').resolve()
        (self.native_home / 'sessions').mkdir(parents=True)
        locks = self.native_home / 'thread-writer-locks'
        locks.mkdir()
        self.session = '01a0e8aa-25f3-79d1-ab3f-bbe626e84426'
        self.lock = locks / (self.session + '.lock')
        self.lock.write_bytes(b'')
        info = self.lock.stat()
        tui = Path(self.claim['tui_log'])
        header = dict(dir='meta', kind='session_start', cwd=self.claim['cwd'],
            ts=datetime.fromtimestamp(self.claim['at'], timezone.utc).isoformat())
        tui.write_text(''.join(json.dumps(e)+'\n' for e in (header,
            dict(dir='to_tui', kind='app_event', variant='StartupThreadStarted'),
            dict(dir='from_tui', kind='op', payload={'UserTurn': {
                'items': [{'type':'text','text':batch.PROMPT}]}}))))
        ledger = standby.StandbyLedger.create(self.worker.path.parent / 'standby',
            cohort_id=self.worker.job['standby_cohort_id'], workspace_id=self.wid,
            boot_id=self.boot, mode='b', prompt=batch.PROMPT, config_generation=self.gen,
            clock=lambda:10.0)
        self.action = str(uuid.uuid4())
        rows = [dict(index=i, launch_id=str(uuid.uuid4()), surface_id=str(uuid.uuid4()),
            session_id=str(uuid.uuid4()), workspace_id=self.wid, pid=1000+i, birth=[1234,i],
            claim_sha256='b'*64, argv_sha256='c'*64, initialized=True, idle=True,
            composer_empty=True, pending_approval=False, task_count=0, user_input_count=0,
            model_request_count=0, observed_monotonic=10.0, boot_id=self.boot, generation=self.gen)
            for i in range(50)]
        rows[0].update(launch_id=self.slot['launch_id'], surface_id=self.slot['surface_id'],
            session_id=self.session, pid=self.claim['bootstrap_pid'], birth=self.claim['bootstrap_birth'],
            claim_sha256=hashlib.sha256(self.claim_path.read_bytes()).hexdigest(),
            argv_sha256=hashlib.sha256(json.dumps(self.claim['argv'], separators=(',',':')).encode()).hexdigest(),
            writer_lock=str(self.lock), writer_identity=[info.st_dev,info.st_ino])
        ledger.observe_ready(rows, config_generation=self.gen, boot_id=self.boot, authorized=True)
        ledger.consume_activation(action_id=self.action, mode='b', prompt=batch.PROMPT,
            config_generation=self.gen, boot_id=self.boot, authorized=True)
        def send(row, prompt, input_id, *, write_guard):
            with write_guard():
                pass  # No real input; native evidence is synthetic in this test.
        ledger.deliver(0, action_id=self.action, observe=lambda i:copy.deepcopy(rows[i]),
            authorized=lambda i:True, send=send)
        self.native = dict(pid=rows[0]['pid'], birth=rows[0]['birth'], argv=self.claim['argv'],
            surface_id=self.slot['surface_id'], environment_workspace_id=self.wid,
            environment=copy.deepcopy(self.exec_mock.call_args.args[2]))
        enter_context(self, patch('ccc_guard_scope.process', return_value=self.native))
        self.files = {p:dict(device=p.stat().st_dev,inode=p.stat().st_ino) for p in (self.lock,tui)}
        enter_context(self, patch('ccc_codex_queue.process_writable_files', side_effect=lambda *a, **k:copy.deepcopy(self.files)))
        return dict(hook_event_name='SessionStart', source='startup', session_id=self.session,
                    cwd=self.claim['cwd'], transcript_path=None)

    def bind(self, payload):
        launch.bind_initial(self.config, self.worker.job['id'], 0, self.slot['launch_id'], payload)

    def test_postactivation_hook_matches_original_session_and_consumes_callback(self):
        self.bind(self.hook_fixture())
        path = self.worker.path.parent / 'standby-session-0.json'
        row = json.loads(path.read_bytes())
        self.assertEqual((row['session_id'], row['action_id']), (self.session,self.action))
        original = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.bind(dict(hook_event_name='SessionStart', source='startup'))
        self.assertEqual(path.read_bytes(), original)

    def test_wrong_session_hook_cannot_be_repaired_by_later_startup(self):
        payload = self.hook_fixture()
        with self.assertRaises(ValueError):
            self.bind({**payload,'session_id':str(uuid.uuid4())})
        with self.assertRaises(FileExistsError):
            self.bind(payload)
        self.assertFalse((self.worker.path.parent / 'standby-session-0.json').exists())

    def inventory_clock(self):
        clock = [10.0]
        enter_context(self, patch.object(launch.time, 'monotonic', side_effect=lambda: clock[0]))
        enter_context(self, patch.object(launch.time, 'sleep',
                                        side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)))
        return clock

    def assert_inventory_failure_consumed(self, payload):
        self.assertFalse((self.worker.path.parent / 'standby-session-0.json').exists())
        path = self.worker.path.parent / 'standby-hook-attempt-0.json'
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.bind(payload)
        self.assertEqual(path.read_bytes(), before)

    def test_inventory_initial_transient_recovers_without_replaying_hook(self):
        from ccc_codex_queue import IncompleteVnodeRead, VnodeInventoryChanged
        payload = self.hook_fixture()
        self.inventory_clock()
        with patch('ccc_codex_queue.process_writable_files', side_effect=[
                IncompleteVnodeRead('partial'), VnodeInventoryChanged('changed'),
                self.files, self.files]) as reader:
            self.bind(payload)
            self.assertEqual(reader.call_count, 4)
            path = self.worker.path.parent / 'standby-session-0.json'
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                self.bind(payload)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(reader.call_count, 4)

    def test_inventory_final_transient_recovers(self):
        from ccc_codex_queue import VnodeInventoryChanged
        payload = self.hook_fixture()
        self.inventory_clock()
        with patch('ccc_codex_queue.process_writable_files', side_effect=[
                self.files, VnodeInventoryChanged('changed'), self.files]) as reader:
            self.bind(payload)
        self.assertEqual(reader.call_count, 3)
        self.assertTrue((self.worker.path.parent / 'standby-session-0.json').exists())

    def test_inventory_continuous_changes_exhaust_budget_without_binding(self):
        from ccc_codex_queue import VnodeInventoryChanged
        payload = self.hook_fixture()
        clock = self.inventory_clock()
        with patch('ccc_codex_queue.process_writable_files',
                   side_effect=VnodeInventoryChanged('changed')) as reader:
            with self.assertRaises(TimeoutError):
                self.bind(payload)
        self.assertGreater(reader.call_count, 1)
        self.assertLessEqual(clock[0], 12.000001)
        self.assert_inventory_failure_consumed(payload)

    def test_inventory_two_snapshots_share_one_budget(self):
        from ccc_codex_queue import IncompleteVnodeRead
        payload = self.hook_fixture()
        clock = self.inventory_clock()
        calls = []
        def read(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                clock[0] += 1.5
                return self.files
            raise IncompleteVnodeRead('partial')
        with patch('ccc_codex_queue.process_writable_files', side_effect=read):
            with self.assertRaises(TimeoutError):
                self.bind(payload)
        self.assertGreater(len(calls), 2)
        self.assertLessEqual(clock[0], 12.000001)
        self.assert_inventory_failure_consumed(payload)

    def test_inventory_late_success_cannot_bind(self):
        payload = self.hook_fixture()
        clock = self.inventory_clock()
        def read(*args, **kwargs):
            clock[0] += 2.01
            return self.files
        with patch('ccc_codex_queue.process_writable_files', side_effect=read) as reader:
            with self.assertRaises(TimeoutError):
                self.bind(payload)
        self.assertEqual(reader.call_count, 1)
        self.assert_inventory_failure_consumed(payload)

    def test_inventory_late_birth_cannot_authorize_another_read(self):
        payload = self.hook_fixture()
        clock = self.inventory_clock()
        def birth(*args, **kwargs):
            clock[0] += 2.01
            return self.native['birth']
        self.birth_mock.side_effect = birth
        with patch('ccc_codex_queue.process_writable_files') as reader:
            with self.assertRaises(TimeoutError):
                self.bind(payload)
        reader.assert_not_called()
        self.assert_inventory_failure_consumed(payload)

    def test_inventory_birth_changes_during_retry_refuses(self):
        from ccc_codex_queue import VnodeInventoryChanged
        payload = self.hook_fixture()
        self.inventory_clock()
        def read(*args, **kwargs):
            self.birth_mock.return_value = [1234, 9999]
            raise VnodeInventoryChanged('changed')
        with patch('ccc_codex_queue.process_writable_files', side_effect=read) as reader:
            with self.assertRaisesRegex(ValueError, 'process changed'):
                self.bind(payload)
        self.assertEqual(reader.call_count, 1)
        self.assert_inventory_failure_consumed(payload)

    def test_inventory_ordinary_oserror_is_not_retried(self):
        payload = self.hook_fixture()
        self.inventory_clock()
        original = OSError('permission denied')
        with patch('ccc_codex_queue.process_writable_files', side_effect=original) as reader:
            with self.assertRaises(OSError) as caught:
                self.bind(payload)
        self.assertIs(caught.exception, original)
        self.assertEqual(reader.call_count, 1)
        self.assert_inventory_failure_consumed(payload)

    def inventory_final_change(self, mutate):
        from ccc_codex_queue import VnodeInventoryChanged
        payload = self.hook_fixture()
        enter_context(self, patch('ccc_guard_scope.process',
                                 side_effect=lambda *a, **k:copy.deepcopy(self.native)))
        self.inventory_clock()
        calls = []
        def read(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                mutate()
                raise VnodeInventoryChanged('changed')
            return copy.deepcopy(self.files)
        with patch('ccc_codex_queue.process_writable_files', side_effect=read):
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.bind(payload)
        self.assertEqual(len(calls), 3)
        self.assert_inventory_failure_consumed(payload)

    def test_inventory_retry_preserves_writer_inode_guard(self):
        def mutate():
            self.lock.rename(self.lock.with_suffix('.retained'))
            self.lock.write_bytes(b'')
            self.files[self.lock] = dict(device=self.lock.stat().st_dev,
                                        inode=self.lock.stat().st_ino)
        self.inventory_final_change(mutate)

    def test_inventory_retry_preserves_activation_guard(self):
        def mutate():
            path = self.worker.path.parent / 'standby' / 'activation.json'
            path.write_bytes(path.read_bytes() + b' ')
        self.inventory_final_change(mutate)

    def test_inventory_retry_preserves_directory_identity_guard(self):
        def mutate():
            path = self.worker.path.parent / 'standby'
            path.rename(path.with_name('retained-standby'))
            path.mkdir(mode=0o700)
        self.inventory_final_change(mutate)

    def test_inventory_retry_preserves_full_process_guard(self):
        def mutate():
            self.native['argv'] = ['different-client']
        self.inventory_final_change(mutate)

    def diagnostic_bind(self, read_payload):
        return launch.bind_with_diagnostics(self.config, self.worker.job['id'], 0,
                                            self.slot['launch_id'], read_payload)

    def diagnostic_rows(self):
        paths = list(self.worker.path.parent.glob('standby-hook-diagnostic-0-*.jsonl'))
        return [json.loads(line) for path in paths for line in path.read_text().splitlines()]

    def test_diagnostic_success_preserves_original_binding(self):
        payload = self.hook_fixture()
        self.diagnostic_bind(lambda: payload)
        self.assertEqual([row['state'] for row in self.diagnostic_rows()], ['started', 'bound'])
        self.assertTrue((self.worker.path.parent / 'standby-session-0.json').exists())
        for path in self.worker.path.parent.glob('standby-hook-diagnostic-*.jsonl'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_diagnostic_failure_preserves_consumption_and_original_stack(self):
        payload = self.hook_fixture()
        payload['session_id'] = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, 'differs from original activation'):
            self.diagnostic_bind(lambda: payload)
        rows = self.diagnostic_rows()
        self.assertEqual([r['state'] for r in rows], ['started', 'failed'])
        self.assertEqual(rows[-1]['error_type'], 'ValueError')
        self.assertEqual(rows[-1]['traceback'][-1]['function'], 'bind_initial')
        attempt = self.worker.path.parent / 'standby-hook-attempt-0.json'
        original = attempt.read_bytes()
        with self.assertRaises(FileExistsError):
            self.diagnostic_bind(lambda: payload)
        self.assertEqual(attempt.read_bytes(), original)
        self.assertEqual(len(self.diagnostic_rows()), 4)
        self.assertFalse((self.worker.path.parent / 'standby-session-0.json').exists())

    def test_diagnostic_payload_error_records_type_without_secret_or_retry(self):
        error = ValueError('fake-secret-prompt-and-key')
        def payload():
            raise error
        with patch.object(launch, 'bind_initial') as bind:
            with self.assertRaises(ValueError) as caught:
                self.diagnostic_bind(payload)
        self.assertIs(caught.exception, error)
        bind.assert_not_called()
        rows = self.diagnostic_rows()
        self.assertEqual(rows[-1]['state'], 'failed')
        self.assertNotIn(str(error), json.dumps(rows))
        self.assertNotIn('test-selected-credential', json.dumps(rows))

    def test_diagnostic_fsync_failure_preserves_original_exception(self):
        error = RuntimeError('original failure')
        with patch.object(launch.os, 'fsync', side_effect=OSError('log full')):
            with patch.object(launch, 'bind_initial', side_effect=error) as bind:
                with self.assertRaises(RuntimeError) as caught:
                    self.diagnostic_bind(lambda: {})
        self.assertIs(caught.exception, error)
        self.assertEqual(bind.call_count, 1)

    def test_diagnostic_fsync_failure_does_not_turn_success_into_failure(self):
        with patch.object(launch.os, 'fsync', side_effect=OSError('log full')):
            with patch.object(launch, 'bind_initial', return_value='bound') as bind:
                self.assertEqual(self.diagnostic_bind(lambda: {}), 'bound')
        self.assertEqual(bind.call_count, 1)

    def test_diagnostic_rejects_symlink_and_shared_directory(self):
        directory = self.worker.path.parent
        for kind in ('shared', 'symlink'):
            with self.subTest(kind=kind):
                original_mode = directory.stat().st_mode & 0o777
                retained = directory.with_name(directory.name + '.retained')
                if kind == 'shared':
                    directory.chmod(0o755)
                else:
                    directory.rename(retained)
                    directory.symlink_to(retained, target_is_directory=True)
                try:
                    with patch.object(launch, 'bind_initial', return_value='bound'):
                        self.assertEqual(self.diagnostic_bind(lambda: {}), 'bound')
                    self.assertEqual(self.diagnostic_rows(), [])
                finally:
                    if kind == 'shared':
                        directory.chmod(original_mode)
                    else:
                        directory.unlink()
                        retained.rename(directory)

    def test_diagnostic_keeps_original_open_file_after_directory_replacement(self):
        directory = self.worker.path.parent
        retained = directory.with_name(directory.name + '.retained')
        def bind(*args):
            directory.rename(retained)
            directory.mkdir(mode=0o700)
        try:
            with patch.object(launch, 'bind_initial', side_effect=bind):
                self.diagnostic_bind(lambda: {})
            self.assertEqual(list(directory.iterdir()), [])
            paths = list(retained.glob('standby-hook-diagnostic-*.jsonl'))
            self.assertEqual(len(paths), 1)
            self.assertEqual([json.loads(s)['state'] for s in paths[0].read_text().splitlines()],
                             ['started', 'bound'])
        finally:
            if retained.exists():
                directory.rmdir()
                retained.rename(directory)

    def test_writer_inode_changed_after_activation_rejects_hook(self):
        payload = self.hook_fixture()
        self.lock.rename(self.lock.with_suffix('.old'))
        self.lock.write_bytes(b'')
        self.files[self.lock] = dict(device=self.lock.stat().st_dev,inode=self.lock.stat().st_ino)
        with self.assertRaises(ValueError):
            self.bind(payload)
        self.assertFalse((self.worker.path.parent / 'standby-session-0.json').exists())

    def test_hook_before_durable_activation_is_rejected(self):
        payload = self.hook_fixture()
        (self.worker.path.parent / 'standby' / 'activation.json').rename(self.worker.path.parent / 'retained-activation.json')
        with self.assertRaises(FileNotFoundError):
            self.bind(payload)
        self.assertFalse((self.worker.path.parent / 'standby-session-0.json').exists())

    def test_writer_paths_changed_during_final_process_read_reject_hook(self):
        payload = self.hook_fixture()
        for path in (self.lock, Path(self.claim['tui_log'])):
            with self.subTest(path=path.name):
                calls = []
                retained = path.with_suffix('.retained')
                def process(*args, **kwargs):
                    if calls:
                        path.rename(retained)
                        path.write_bytes(retained.read_bytes())
                    calls.append(1)
                    return copy.deepcopy(self.native)
                with patch('ccc_guard_scope.process', side_effect=process):
                    with self.assertRaisesRegex(ValueError, 'writer path changed'):
                        self.bind(payload)
                self.assertFalse((self.worker.path.parent / 'standby-session-0.json').exists())
                # Independent synthetic case; restore its original fixture.
                path.unlink()
                retained.rename(path)
                (self.worker.path.parent / 'standby-hook-attempt-0.json').unlink()


if __name__ == '__main__':
    unittest.main()

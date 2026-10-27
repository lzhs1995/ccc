"""主管报告发现的真实证据、认证身份、有限扫描和 hook 出口回归测试。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
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

import cmux_daemon_identity as daemon
import cmux_evidence_io as evidence_io
import cmux_supervisor_report_guard as guard
import cmux_workspace_guard as workspace_guard
import executor_closeout
import offline_test_hook
from offline_hook_thread_fixture import native_processes, native_thread
import test_cmux_daemon_identity as identity_fixtures


SCRIPT = Path(__file__).with_name('cmux_supervisor_report_guard.py')


class SupervisorReportGuardTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix='supervisor-report-'))).resolve()
        self.active = self.root / 'active'
        self.active.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.home / '.local/state/multi-agent-collaboration/supervisor-report-discovery-v1'
        self.stack.enter_context(patch.object(guard.stop_guard, 'ACTIVE_DIR', self.active))
        self.stack.enter_context(patch.object(Path, 'home', return_value=self.home))
        # 复用进程/树夹具；collect_hook、caller_snapshot、resolve 全部保留真实实现。
        self.identity_fixture = identity_fixtures.DaemonTests()
        self.identity_fixture.setUp()
        self.workspace = identity_fixtures.W
        self.supervisor = identity_fixtures.C
        self.executor = identity_fixtures.T
        self.payload, _ = native_thread(self.home, actual=identity_fixtures.S)
        self.payload.update(tool_name='Bash', tool_input={'command': 'touch MUST_NOT_RUN'})
        self.sequence = 0

    @contextmanager
    def authenticated(self, caller=None):
        with native_processes(self.identity_fixture, caller):
            yield

    @contextmanager
    def stationary_clock(self):
        # 只固定守卫的外部时钟；身份解析仍使用独立的真实 deadline。
        clock = SimpleNamespace(now=0.0)
        with patch.object(guard, 'time', SimpleNamespace(
                monotonic=lambda: clock.now, time=time.time)):
            yield clock

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def sha(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def add_report(self, *, workspace=None, supervisor=None, executor=None,
                   task_id=None, marker_name=None, legacy=False):
        self.sequence += 1
        workspace = workspace or self.workspace
        supervisor = supervisor or self.supervisor
        executor = executor or self.executor
        task_id = task_id or 'task-' + str(self.sequence)
        root = self.root / 'tasks' / ('report-' + str(self.sequence))
        root.mkdir(parents=True)
        marker_path = (self.active / (workspace + '.json') if legacy else
                       self.active / workspace / (marker_name or f'm-{self.sequence:04d}.json'))
        report = root / 'executor-report.md'
        report.write_text('已冻结的有界结果；回调仍待主管核收。\n', encoding='utf-8')
        pack_path, receipt = root / 'task-pack.json', root / 'receipt.json'
        marker = dict(task_id=task_id, workspace_uuid=workspace, artifact_root=str(root),
                      participants=[
                          dict(role='supervisor', surface_ref='surface:1', surface_uuid=supervisor,
                               native_session_id=identity_fixtures.S),
                          dict(role='executor', surface_uuid=executor)])
        pack = dict(draft=False, task_id=task_id, executor_uuid=executor,
                    completion_nonce='nonce-' + str(self.sequence),
                    completion_callback='DONE|' + task_id + '|nonce-' + str(self.sequence),
                    callback_target='surface:1', report=str(report), completion_receipt=str(receipt))
        journal = root / 'receipt-attempts'
        journal.mkdir()
        lock = journal / 'delivery.lock'
        lock.touch()
        attempt_path = journal / 'attempt-0001.json'
        attempt = dict(phase='POST_ENTER_OBSERVATION', started_at_epoch=1.0,
                       ended_at_epoch=3.0, events=[
                           dict(phase='PASTE_INTENT', at_epoch=1.5),
                           dict(phase='POST_ENTER_OBSERVATION', at_epoch=2.0)],
                       error='native delivery remains unconfirmed', delivery_state='QUEUED')
        item = SimpleNamespace(root=root, workspace=workspace, supervisor=supervisor,
                               executor=executor, marker=marker, marker_path=marker_path,
                               report=report, pack=pack, pack_path=pack_path, receipt=receipt,
                               journal=journal, lock=lock, attempt=attempt, attempt_path=attempt_path)
        self.write(marker_path, marker)
        self.freeze(item)
        return item

    def freeze(self, item):
        self.write(item.pack_path, item.pack)
        item.attempt['binding'] = dict(
            **{k: item.pack[k] for k in ('task_id', 'completion_nonce', 'completion_callback',
                                       'callback_target', 'report')},
            task_pack_sha256=self.sha(item.pack_path), report_sha256=self.sha(item.report),
            report_bytes=item.report.stat().st_size,
            identity=dict(workspace_uuid=item.workspace, caller_surface_uuid=item.executor,
                          target_surface_uuid=item.supervisor, target_pane_uuid=identity_fixtures.P))
        self.write(item.attempt_path, item.attempt)
        self.assertIsNotNone(executor_closeout.terminal_report(
            item.marker, item.workspace, item.executor), '夹具必须通过真实 terminal_report')

    def discover(self, payload=None, state_root=None, caller=None):
        with self.authenticated(caller):
            return guard.discover(self.payload if payload is None else payload,
                                  self.state if state_root is None else state_root)

    def invoke_main(self, payload=None, raw=None, caller=None):
        raw = json.dumps(self.payload if payload is None else payload) if raw is None else raw
        stdout, stderr = io.StringIO(), io.StringIO()
        with self.authenticated(caller), patch.object(sys, 'stdin', io.StringIO(raw)), \
                patch.object(sys, 'stdout', stdout), patch.object(sys, 'stderr', stderr):
            code = guard.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def hook(self, *, payload=None, raw=None, home=None):
        env = dict(os.environ, HOME=str(home or self.home), CMUX_WORKSPACE_ID=self.workspace,
                   CMUX_SURFACE_ID=self.supervisor)
        if home is not None and payload is None and raw is None:
            payload, _ = native_thread(home, actual=identity_fixtures.S)
        raw = json.dumps(self.payload if payload is None else payload) if raw is None else raw
        return subprocess.run(offline_test_hook.command(SCRIPT, self.active, native=True), input=raw,
                              text=True, capture_output=True, env=env, cwd=self.root, timeout=5)

    def notice_paths(self, state=None):
        return sorted(p for p in (state or self.state).glob('*.json')
                      if not p.name.startswith('cursor-'))

    def cursor_path(self):
        key = hashlib.sha256(json.dumps([self.workspace, self.supervisor]).encode()).hexdigest()
        return self.state / ('cursor-' + key + '.json')

    def lock_path(self):
        return self.cursor_path().with_name(self.cursor_path().name.replace('cursor-', 'sweep-')
                                           .replace('.json', '.lock'))

    def source_snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                for directory in (self.active, self.root / 'tasks')
                for p in directory.rglob('*') if p.is_file()}

    def directory_path(self, value, candidates):
        # scandir 可以接收绑定 fd；用真实 inode 映射夹具目录，避免重走路径。
        if not isinstance(value, int):
            return Path(value)
        observed = os.fstat(value)
        for candidate in candidates:
            expected = candidate.stat()
            if (observed.st_dev, observed.st_ino) == (expected.st_dev, expected.st_ino):
                return candidate
        self.fail('扫描了未预期的目录 fd: ' + str(value))

    def assert_discovery_only(self, notice):
        self.assertEqual(notice['state'], 'REPORT_DISCOVERED')
        self.assertEqual(notice['input_operations'], 0)
        for key in ('delivery_confirmed', 'accepted', 'disarmed'):
            self.assertIs(notice[key], False)

    def test_managed_native_workspace_and_supervisor_override_inherited_and_payload_identity(self):
        item = self.add_report()
        payload = dict(self.payload, workspace_id=identity_fixtures.X,
                       surface_id=identity_fixtures.D, surface_uuid=identity_fixtures.D)
        with self.authenticated():
            before = dict(os.environ)
            self.assertEqual(guard.hook_identity.resolve(payload), (self.workspace, self.supervisor))
            notices = guard.discover(payload, self.state)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual([n['report'] for n in notices], [str(item.report)])
        self.assertEqual(notices[0]['workspace_uuid'], self.workspace)
        self.assertEqual(notices[0]['supervisor_uuid'], self.supervisor)

    def test_executor_cannot_discover_supervisor_report(self):
        self.add_report()
        self.assertEqual(self.discover(caller=self.executor), [])
        self.assertEqual(self.notice_paths(), [])
        self.assertEqual(len(self.discover()), 1)

    def test_uppercase_and_lowercase_uuid_workspaces_are_valid(self):
        for workspace in ('ABCDEFAB-1234-4678-8ABC-0123456789AB',
                          'abcdefac-1234-4678-8abc-0123456789ab'):
            with self.subTest(workspace=workspace):
                self.workspace = workspace
                self.identity_fixture.client_env['CMUX_WORKSPACE_ID'] = workspace
                self.identity_fixture.tree['windows'][0]['workspaces'][0]['id'] = workspace
                item = self.add_report()
                self.assertEqual([n['report'] for n in self.discover()], [str(item.report)])

    def test_frozen_report_discovery_does_not_deliver_accept_disarm_or_modify_sources(self):
        item = self.add_report()
        before = self.source_snapshot()
        notices = self.discover()
        self.assertEqual(len(notices), 1)
        self.assert_discovery_only(notices[0])
        stored = json.loads(Path(notices[0]['discovery_record']).read_text())
        self.assert_discovery_only(stored)
        self.assertEqual(stored['report_sha256'], self.sha(item.report))
        self.assertEqual(stored['attempt_sha256'], self.sha(item.attempt_path))
        self.assertFalse(item.receipt.exists())
        self.assertTrue(item.marker_path.exists())
        self.assertEqual(before, self.source_snapshot())

    def test_no_input_terminal_attempt_is_discoverable_without_delivery(self):
        item = self.add_report()
        item.attempt.update(phase='NO_INPUT', events=[])
        self.freeze(item)
        notices = self.discover()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['attempt_phase'], 'NO_INPUT')
        self.assert_discovery_only(notices[0])
        self.assertFalse(item.receipt.exists())

    def test_real_journal_and_content_pins_are_required(self):
        item = self.add_report()
        original_attempt = item.attempt_path.read_bytes()
        original_report = item.report.read_bytes()
        original_pack = item.pack_path.read_bytes()
        for case in ('nonterminal', 'missing_end', 'changed_report', 'changed_pack', 'missing_attempt'):
            with self.subTest(case=case):
                item.attempt_path.write_bytes(original_attempt)
                item.report.write_bytes(original_report)
                item.pack_path.write_bytes(original_pack)
                if case == 'nonterminal':
                    self.write(item.attempt_path, dict(item.attempt, phase='PREPARED'))
                elif case == 'missing_end':
                    self.write(item.attempt_path, dict(item.attempt, ended_at_epoch=None))
                elif case == 'changed_report':
                    item.report.write_text('changed after the original call')
                elif case == 'changed_pack':
                    self.write(item.pack_path, dict(item.pack, scope='changed'))
                else:
                    item.attempt_path.unlink()
                self.assertEqual(self.discover(), [])
                self.assertEqual(self.notice_paths(), [])
        item.attempt_path.write_bytes(original_attempt)
        item.pack_path.write_bytes(original_pack)
        item.report.write_bytes(original_report)
        self.assertEqual(len(self.discover()), 1)

    def test_inflight_original_delivery_lock_is_not_a_frozen_report(self):
        item = self.add_report()
        with item.lock.open('rb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.discover(), [])
        self.assertEqual(len(self.discover()), 1)

    def test_v1_and_v2_aliases_share_one_durable_notice(self):
        item = self.add_report()
        self.write(self.active / (self.workspace + '.json'), item.marker)
        notices = self.discover()
        self.assertEqual(len(notices), 1)
        path = Path(notices[0]['discovery_record'])
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(self.discover(), [])
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(self.notice_paths(), [path])

    def test_distinct_task_reports_do_not_share_dedup_slots(self):
        first, second = self.add_report(task_id='same-task'), self.add_report(task_id='same-task')
        notices = self.discover()
        self.assertEqual({n['artifact_root'] for n in notices}, {str(first.root), str(second.root)})
        self.assertEqual(len(self.notice_paths()), 2)

    def test_new_bound_report_pack_or_nonce_can_create_a_new_discovery(self):
        item = self.add_report()
        previous = self.discover()[0]
        for change in ('report', 'pack', 'nonce'):
            with self.subTest(change=change):
                if change == 'report':
                    item.report.write_text('新版冻结报告\n', encoding='utf-8')
                elif change == 'pack':
                    item.pack['scope'] = 'new bounded scope'
                else:
                    item.pack.update(completion_nonce='nonce-v2',
                                     completion_callback='DONE|' + item.pack['task_id'] + '|nonce-v2')
                self.freeze(item)
                notices = self.discover()
                self.assertEqual(len(notices), 1)
                self.assertNotEqual(notices[0]['discovery_record'], previous['discovery_record'])
                self.assertEqual(self.discover(), [])
                previous = notices[0]
        self.assertEqual(len(self.notice_paths()), 4)

    def test_new_attempt_for_same_frozen_report_does_not_repeat_notice(self):
        item = self.add_report()
        self.assertEqual(len(self.discover()), 1)
        newer = copy.deepcopy(item.attempt)
        newer['ended_at_epoch'] = 4.0
        self.write(item.journal / 'attempt-0002.json', newer)
        self.assertIsNotNone(executor_closeout.terminal_report(item.marker, item.workspace, item.executor))
        self.assertEqual(self.discover(), [])
        self.assertEqual(len(self.notice_paths()), 1)

    def test_hook_protocol_and_dedup_survive_fresh_processes(self):
        item = self.add_report()
        before = self.source_snapshot()
        first = self.hook()
        self.assertEqual((first.returncode, first.stderr), (0, ''))
        self.assertEqual(len(first.stdout.splitlines()), 1)
        output = json.loads(first.stdout)
        self.assertEqual(set(output), {'hookSpecificOutput'})
        specific = output['hookSpecificOutput']
        self.assertEqual(set(specific), {'hookEventName', 'additionalContext'})
        self.assertEqual(specific['hookEventName'], 'PostToolUse')
        context = specific['additionalContext']
        self.assertTrue(context.startswith('REPORT_DISCOVERED:'))
        self.assertIn('not native delivery, acceptance or disarm', context)
        self.assertIn('TASK_ID=' + item.pack['task_id'] + ' REPORT=' + str(item.report)
                      + ' ATTEMPT=' + str(item.attempt_path), context)
        record = self.notice_paths()[0]
        record_before = (record.read_bytes(), record.stat().st_mtime_ns)
        second = self.hook()
        self.assertEqual((second.returncode, second.stdout, second.stderr), (0, '', ''))
        self.assertEqual((record.read_bytes(), record.stat().st_mtime_ns), record_before)
        self.assertEqual(before, self.source_snapshot())
        self.assertFalse((self.root / 'MUST_NOT_RUN').exists())
        self.assertFalse(item.receipt.exists())

    def test_non_posttool_events_and_missing_active_directory_do_no_identity_work(self):
        for event in ('PreToolUse', 'Stop', 'SubagentStop', 'SessionStart', None):
            with self.subTest(event=event), patch.object(
                    guard.hook_identity, 'resolve', side_effect=AssertionError('不应解析身份')):
                self.assertEqual(guard.discover(dict(self.payload, hook_event_name=event), self.state), [])
        self.active.rmdir()
        with patch.object(guard.hook_identity, 'resolve', side_effect=AssertionError('不应解析身份')):
            self.assertEqual(guard.discover(self.payload, self.state), [])
        self.assertFalse(self.state.exists())

    def test_malformed_stdin_and_nonobject_payloads_fail_open(self):
        self.add_report()
        for raw in ('{', '', 'null', '[]', '1', '"PostToolUse"'):
            with self.subTest(raw=raw):
                result = self.hook(raw=raw)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertEqual(self.notice_paths(), [])

    def test_all_declared_identity_error_classes_fail_open(self):
        self.add_report()
        failures = [workspace_guard.WorkspaceScopeError('scope drift'),
                    daemon.IdentityError('native identity changed'), OSError('process read'),
                    ValueError('bad identity'), subprocess.TimeoutExpired('identity', 0.01),
                    subprocess.CalledProcessError(1, 'identity')]
        for failure in failures:
            with self.subTest(error=type(failure).__name__), patch.object(
                    daemon, 'collect_hook', side_effect=failure):
                self.assertEqual(self.invoke_main(), (0, '', ''))
        self.assertFalse(self.state.exists())

    def test_missing_managed_session_does_not_fall_back_to_payload_supervisor(self):
        self.add_report()
        payload = dict(self.payload, workspace_id=self.workspace, surface_id=self.supervisor)
        payload.pop('session_id')
        self.assertEqual(self.invoke_main(payload), (0, '', ''))
        self.assertFalse(self.state.exists())

    def test_missing_caller_or_default_workspace_does_not_read_markers(self):
        self.add_report()
        for identity in ({}, {'workspace_id': self.workspace},
                         {'workspace_id': 'default', 'surface_id': self.supervisor}):
            with self.subTest(identity=identity), offline_test_hook.hook_environment(self.active), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(guard, '_read_marker', wraps=guard._read_marker) as read:
                self.assertEqual(guard.discover(dict(self.payload, **identity), self.state), [])
                read.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_foreign_workspaces_are_neither_enumerated_nor_read(self):
        local = self.add_report()
        foreign = self.add_report(workspace=identity_fixtures.X, supervisor=identity_fixtures.D)
        self.write(self.active / (identity_fixtures.X + '.json'), foreign.marker)
        before = self.source_snapshot()
        real_read, real_scandir = evidence_io.snapshot, os.scandir
        real_marker = guard._read_marker
        directories = (self.active, local.marker_path.parent, foreign.marker_path.parent,
                       local.journal, foreign.journal)
        reads, scans = [], []

        def read(path, *args, **kwargs):
            reads.append(Path(path))
            return real_read(path, *args, **kwargs)

        def scan(path):
            scans.append(self.directory_path(path, directories))
            return real_scandir(path)

        def read_marker(path, directory_fd):
            reads.append(self.directory_path(directory_fd, directories) / path.name)
            return real_marker(path, directory_fd)

        with patch.object(evidence_io, 'snapshot', side_effect=read), \
                patch.object(guard, '_read_marker', side_effect=read_marker), \
                patch.object(guard.os, 'scandir', side_effect=scan):
            notices = self.discover()
        self.assertEqual([n['report'] for n in notices], [str(local.report)])
        # terminal_report 还会有界枚举本任务 journal；这里只限定 marker 的目录辖区。
        marker_scans = [p for p in scans if p == self.active or self.active in p.parents]
        self.assertEqual(marker_scans, [self.active / self.workspace])
        self.assertFalse(any(foreign.root == p or foreign.root in p.parents for p in scans))
        self.assertTrue(reads)
        self.assertFalse(any(p == self.active / (identity_fixtures.X + '.json')
                             or (self.active / identity_fixtures.X) in p.parents
                             or foreign.root in p.parents for p in reads))
        self.assertEqual(before, self.source_snapshot())

    def test_workspace_directory_symlink_does_not_read_foreign_markers(self):
        foreign = self.add_report(workspace=identity_fixtures.X, supervisor=identity_fixtures.D)
        (self.active / self.workspace).symlink_to(foreign.marker_path.parent, target_is_directory=True)
        with patch.object(guard, '_read_marker', wraps=guard._read_marker) as read:
            self.assertEqual(self.invoke_main(), (0, '', ''))
        read.assert_not_called()
        self.assertEqual(self.notice_paths(), [])

    def test_active_root_symlink_does_not_read_markers(self):
        self.add_report()
        moved = self.root / 'moved-active'
        self.active.rename(moved)
        self.active.symlink_to(moved, target_is_directory=True)
        with patch.object(guard, '_read_marker', wraps=guard._read_marker) as read:
            self.assertEqual(self.invoke_main(), (0, '', ''))
        read.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_workspace_replacement_after_enumeration_never_reads_foreign_marker(self):
        item = self.add_report()
        foreign = self.add_report(marker_name='shadow-source.json')
        foreign_directory = self.active / identity_fixtures.X
        foreign_directory.mkdir()
        foreign_marker = foreign_directory / item.marker_path.name
        foreign.marker_path.rename(foreign_marker)
        directory = self.active / self.workspace
        original_save, original_read = guard._save_cursor, evidence_io.snapshot
        original_fdopen = os.fdopen
        foreign_stat = foreign_marker.stat()
        swapped, reads = [], []

        def swap_after_cursor(*args):
            original_save(*args)
            if not swapped:
                directory.rename(self.root / 'retired-workspace')
                directory.symlink_to(foreign_directory, target_is_directory=True)
                swapped.append(True)

        def read(path, *args, **kwargs):
            reads.append(Path(path).resolve())
            return original_read(path, *args, **kwargs)

        def open_stream(fd, *args, **kwargs):
            # 按真实打开的 inode 观察 marker；路径已变为 symlink，不能 resolve 名义路径。
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) == (foreign_stat.st_dev, foreign_stat.st_ino):
                reads.append(foreign_marker.resolve())
            return original_fdopen(fd, *args, **kwargs)

        # 注入真实 rename/symlink；不改报告、terminal_report 或身份裁决。
        # 实现也可以继续读已绑定的原目录 fd，只要不跟随替换后的外部目录。
        with patch.object(guard, '_save_cursor', side_effect=swap_after_cursor), \
                patch.object(guard.os, 'fdopen', side_effect=open_stream), \
                patch.object(evidence_io, 'snapshot', side_effect=read):
            code, stdout, stderr = self.invoke_main()
        self.assertEqual((code, stderr), (0, ''))
        self.assertTrue(swapped)
        self.assertNotIn(foreign_marker.resolve(), reads)
        self.assertNotIn(str(foreign.report), stdout)
        for path in self.notice_paths():
            self.assertNotEqual(json.loads(path.read_text())['report'], str(foreign.report))

    def test_payload_workspace_path_cannot_escape_the_active_root(self):
        foreign = self.add_report(workspace=identity_fixtures.X, supervisor=identity_fixtures.D)
        for selector in (str(foreign.marker_path.parent), '../active/' + identity_fixtures.X):
            payload = dict(self.payload, workspace_id=selector, surface_id=self.supervisor)
            with self.subTest(selector=selector), offline_test_hook.hook_environment(self.active), \
                    patch.dict(os.environ, {}, clear=True), \
                    patch.object(guard, '_read_marker', wraps=guard._read_marker) as read, \
                    patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), \
                    patch.object(sys, 'stdout', io.StringIO()), patch.object(sys, 'stderr', io.StringIO()):
                self.assertEqual(guard.main(), 0)
                read.assert_not_called()

    def test_fresh_workspace_and_unique_supervisor_are_required(self):
        item = self.add_report()
        original = copy.deepcopy(item.marker)
        expired = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        cases = [
            dict(workspace_uuid=identity_fixtures.X),
            dict(armed_at=expired, ttl_seconds=1),
            dict(participants=[original['participants'][1]]),
            dict(participants=original['participants'] + [original['participants'][0]]),
            dict(participants=[dict(original['participants'][0], surface_uuid=identity_fixtures.D),
                               original['participants'][1]])]
        for changes in cases:
            with self.subTest(changes=changes):
                self.write(item.marker_path, dict(original, **changes))
                self.assertEqual(self.discover(), [])
        item.marker_path.unlink()
        self.assertEqual(self.discover(), [])
        self.write(item.marker_path, original)
        self.assertEqual(len(self.discover()), 1)

    def test_malformed_participant_types_never_crash_or_claim_discovery(self):
        item = self.add_report()
        cases = [None, True, 7, 'executor', {}, [],
                 [None, True, 7, 'executor', [], {}],
                 [item.marker['participants'][0], None, item.marker['participants'][1]],
                 [item.marker['participants'][0], dict(role=None, surface_uuid=self.executor)],
                 [item.marker['participants'][0], dict(role='executor', surface_uuid=['wrong'])]]
        for participants in cases:
            with self.subTest(participants=participants):
                self.write(item.marker_path, dict(item.marker, participants=participants))
                self.assertEqual(self.invoke_main(), (0, '', ''))
        self.assertEqual(self.notice_paths(), [])

    def test_bad_marker_files_do_not_block_later_valid_report(self):
        item = self.add_report(marker_name='z-valid.json')
        directory = item.marker_path.parent
        (directory / 'a-invalid.json').write_text('{')
        (directory / 'b-list.json').write_text('[]')
        (directory / 'c-directory.json').mkdir()
        os.mkfifo(directory / 'd-fifo.json')
        (directory / 'e-symlink.json').symlink_to(item.marker_path)
        self.write(directory / '.hidden.json', item.marker)
        self.write(directory / 'not-json.txt', item.marker)
        result = self.hook()
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertIn(str(item.report), result.stdout)
        self.assertEqual(len(self.notice_paths()), 1)
        self.assertTrue((directory / 'd-fifo.json').exists())

    def test_workspace_file_or_fifo_fails_open_without_blocking(self):
        self.add_report(legacy=True)
        directory = self.active / self.workspace
        for kind in ('file', 'fifo'):
            with self.subTest(kind=kind):
                if kind == 'fifo':
                    os.mkfifo(directory)
                else:
                    directory.write_text('not a directory')
                try:
                    result = self.hook()
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
                finally:
                    directory.unlink()

    def test_discovery_root_file_fifo_and_symlink_fail_open(self):
        self.add_report()
        for kind in ('file', 'fifo', 'symlink'):
            with self.subTest(kind=kind):
                home = self.root / ('bad-home-' + kind)
                state = home / '.local/state/multi-agent-collaboration/supervisor-report-discovery-v1'
                state.parent.mkdir(parents=True)
                if kind == 'file':
                    state.write_text('unrelated content')
                elif kind == 'fifo':
                    os.mkfifo(state)
                else:
                    target = self.root / 'foreign-discovery'
                    target.mkdir()
                    (target / 'keep.txt').write_text('untouched')
                    state.symlink_to(target, target_is_directory=True)
                result = self.hook(home=home)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
                if kind == 'symlink':
                    self.assertEqual([p.name for p in target.iterdir()], ['keep.txt'])
                    self.assertEqual((target / 'keep.txt').read_text(), 'untouched')

    def test_sweep_lock_special_files_fail_open(self):
        self.add_report()
        self.state.mkdir(parents=True)
        path = self.lock_path()
        for kind in ('directory', 'fifo', 'symlink'):
            with self.subTest(kind=kind):
                if kind == 'directory':
                    path.mkdir()
                elif kind == 'fifo':
                    os.mkfifo(path)
                else:
                    target = self.root / 'foreign-lock'
                    target.write_text('untouched')
                    path.symlink_to(target)
                try:
                    result = self.hook()
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
                    self.assertEqual(self.notice_paths(), [])
                    if kind == 'symlink':
                        self.assertEqual(target.read_text(), 'untouched')
                finally:
                    path.rmdir() if kind == 'directory' else path.unlink()

    def test_lock_contention_is_nonblocking_and_does_not_advance_cursor(self):
        self.add_report()
        self.state.mkdir(parents=True)
        with self.lock_path().open('wb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.hook()
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
            self.assertFalse(self.cursor_path().exists())
            self.assertEqual(self.notice_paths(), [])
        self.assertEqual(len(self.discover()), 1)

    def test_concurrent_processes_publish_only_one_durable_notice(self):
        self.add_report()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.hook(), range(4)))
        for result in results:
            self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertEqual(sum(bool(result.stdout) for result in results), 1)
        self.assertEqual(len(self.notice_paths()), 1)
        self.assertEqual(list(self.state.glob('.notice-*')), [])
        self.assertEqual(list(self.state.glob('.cursor-*')), [])

    def test_storage_failure_is_fail_open_and_cleans_temporary_files(self):
        self.add_report()
        for operation in ('replace', 'link'):
            with self.subTest(operation=operation), patch.object(
                    guard.os, operation, side_effect=PermissionError('test storage failure')):
                self.assertEqual(self.invoke_main(), (0, '', ''))
                self.assertEqual(self.notice_paths(), [])
                self.assertEqual(list(self.state.glob('.notice-*')), [])
                self.assertEqual(list(self.state.glob('.cursor-*')), [])
        self.assertEqual(len(self.discover()), 1)

    def test_at_most_eight_notices_per_sweep_with_fair_followup(self):
        reports = [self.add_report() for _ in range(17)]
        with self.stationary_clock():
            sweeps = [self.discover() for _ in range(4)]
        self.assertEqual([len(items) for items in sweeps], [8, 8, 1, 0])
        self.assertEqual({notice['report'] for items in sweeps for notice in items},
                         {str(item.report) for item in reports})
        self.assertEqual(len(self.notice_paths()), 17)

    def test_scan_reads_at_most_32_markers_and_rotates_past_unrelated_and_malformed_rows(self):
        item = self.add_report(marker_name='m-0064.json')
        for number in range(64):
            path = item.marker_path.with_name(f'm-{number:04d}.json')
            if number % 2:
                path.write_text('{')
            else:
                self.write(path, dict(item.marker, participants=[
                    dict(role='supervisor', surface_uuid=identity_fixtures.D)]))
        visited = []
        with self.stationary_clock():
            for expected in (0, 0, 1):
                with patch.object(guard, '_read_marker', wraps=guard._read_marker) as read:
                    self.assertEqual(len(self.discover()), expected)
                    paths = [Path(call.args[0]) for call in read.call_args_list]
                    self.assertLessEqual(len(paths), 32)
                    visited.extend(paths)
        self.assertEqual(len(set(visited)), 65)
        self.assertEqual(len(self.notice_paths()), 1)

    def test_deduplicated_markers_also_advance_the_cursor(self):
        item = self.add_report(marker_name='m-0000.json')
        self.assertEqual(len(self.discover()), 1)
        for number in range(1, 65):
            self.write(item.marker_path.with_name(f'm-{number:04d}.json'), item.marker)
        late = self.add_report(marker_name='z-late.json')
        with self.stationary_clock():
            self.assertEqual(self.discover(), [])
            self.assertEqual(self.discover(), [])
            notices = self.discover()
        self.assertEqual([n['report'] for n in notices], [str(late.report)])

    def test_corrupt_oversized_and_fifo_cursors_reset_without_losing_report(self):
        item = self.add_report()
        for number, raw in enumerate(('{', '[]', '{"last": 7}', 'x' * 4097, None)):
            state = self.root / ('cursor-case-' + str(number))
            state.mkdir()
            cursor = state / self.cursor_path().name
            if raw is None:
                os.mkfifo(cursor)
            else:
                cursor.write_text(raw)
            with self.subTest(raw=repr(raw)[:40]):
                notices = self.discover(state_root=state)
                self.assertEqual([n['report'] for n in notices], [str(item.report)])
                self.assertEqual(json.loads(cursor.read_text())['last'], str(item.marker_path))

    def test_directory_entry_budget_stops_before_any_marker_content_read(self):
        self.add_report()
        seen = []

        @contextmanager
        def crowded_directory(path):
            directory = self.directory_path(path, (self.active / self.workspace,))
            self.assertEqual(directory, self.active / self.workspace)
            def entries():
                for number in range(5000):
                    seen.append(number)
                    name = f'm-{number:04d}.json'
                    yield SimpleNamespace(name=name, path=str(directory / name))
            yield entries()

        with self.stationary_clock(), patch.object(guard.os, 'scandir', side_effect=crowded_directory), \
                patch.object(guard, '_read_marker', wraps=guard._read_marker) as read:
            self.assertEqual(self.invoke_main(), (0, '', ''))
        self.assertLessEqual(len(seen), 4097)
        self.assertGreater(len(seen), 0)
        read.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_directory_enumeration_honors_the_075_second_budget(self):
        item = self.add_report()
        seen = []
        with self.stationary_clock() as clock:
            @contextmanager
            def slow_directory(path):
                self.assertEqual(self.directory_path(path, (item.marker_path.parent,)),
                                 item.marker_path.parent)
                def entries():
                    for _ in range(100):
                        seen.append(True)
                        clock.now = 0.76
                        yield SimpleNamespace(name=item.marker_path.name, path=str(item.marker_path))
                yield entries()
            with patch.object(guard.os, 'scandir', side_effect=slow_directory), \
                    patch.object(guard, '_read_marker', wraps=guard._read_marker) as read:
                self.assertEqual(self.invoke_main(), (0, '', ''))
        self.assertEqual(len(seen), 1)
        read.assert_not_called()

    def test_evidence_scan_stops_at_deadline_and_resumes_after_cursor(self):
        first, second = self.add_report(), self.add_report()
        real_terminal = guard.terminal_report
        with self.stationary_clock() as clock:
            def slow_terminal(*args):
                evidence = real_terminal(*args)
                clock.now = 0.76
                return evidence
            with patch.object(guard, 'terminal_report', side_effect=slow_terminal) as terminal:
                notices = self.discover()
                self.assertEqual([n['report'] for n in notices], [str(first.report)])
                self.assertEqual(terminal.call_count, 1)
            clock.now = 0.0
            notices = self.discover()
            self.assertEqual([n['report'] for n in notices], [str(second.report)])


if __name__ == '__main__':
    unittest.main()

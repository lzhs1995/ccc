"""File inquiries remain discoverable without arming a research task."""
import contextlib
import copy
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

import executor_ready as ready
import executor_reask as reask
import executor_reply as replies
import supervisor_inbox as inbox
import cmux_supervisor_report_guard as guard
from offline_hook_thread_fixture import native_thread

CALLER = 'FD51FB34-5C79-4013-BD6B-9E10CFC743CD'
SUP = '41F454EB-0DDD-42BD-A529-BC79589F6DD5'
WORKSPACE = '6FB20312-9E66-43DB-AF09-92C3B18E1296'


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(mock.patch.object(Path, 'home', return_value=self.home))
        self.payload, self.proof = native_thread(self.home)
        self.emissions = []
        self.discovery_trace = []

    def discover(self, *, now=100.0, emit=None):
        # Keep native paging evidence in assertion failures: directory offsets
        # and EOF behavior differ between the real CI filesystems.
        frontier = self.cursor() / 'frontier.json'
        trace = dict(now=now, before=ready.read_json(frontier), pages=[])
        native = inbox.directory.read_page

        def observe(fd, offset):
            names, following = native(fd, offset)
            trace['pages'].append(dict(offset=offset, following=following,
                                       count=len(names), names=names[:4]))
            return names, following

        try:
            with mock.patch.object(inbox.directory, 'read_page', side_effect=observe):
                return inbox.discover_bound(
                    WORKSPACE, SUP, thread_id=self.payload['session_id'],
                    emit=self.emissions.append if emit is None else emit, now=now)
        finally:
            trace['after'] = ready.read_json(frontier)
            self.discovery_trace = (self.discovery_trace + [trace])[-4:]

    def request(self, caller=CALLER, marker='EXECUTOR_READY_one'):
        state = reask.start(caller, SUP, 'completed-task', now=1.0, workspace=WORKSPACE)
        result = reask.write_channels(state, marker, 'request next fixed pack', 2.0)
        self.assertEqual(result['errors'], [])
        return state, reask.inbox_path(state)

    def test_discovery_exact_snapshot_no_delivery_deduplicates_and_renotifies_new_round(self):
        state, path = self.request()
        raw = path.read_bytes()
        notices = self.discover()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['request_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertFalse(notices[0]['native_delivery_confirmed'])
        self.assertEqual(notices[0]['input_operations'], 0)
        self.assertEqual(self.discover(), [])
        reask.write_channels(state, 'EXECUTOR_READY_two', 'request', 62.0)
        self.assertEqual(len(self.discover()), 1, self.discovery_trace)
        self.assertEqual(len(list(path.parent.glob('*.json'))), 1)

    def test_notice_snapshot_survives_rotation_before_supervisor_first_read(self):
        state, current = self.request()
        original = current.read_bytes()
        notice = self.discover()[0]
        reask.write_channels(state, 'EXECUTOR_READY_two', 'new inquiry', 62.0)
        snapshot = Path(notice['request'])
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                         notice['request_sha256'])
        self.assertFalse(snapshot.stat().st_mode & 0o222)
        self.assertEqual(notice['source_request'], str(current))
        body = json.loads(snapshot.read_bytes())
        self.assertEqual(body['marker'], notice['marker'])
        self.assertEqual(body['reply_mailbox'], notice['reply_mailbox'])

    def test_same_snapshot_is_reused_without_changing_its_identity(self):
        _state, _current = self.request()
        first = self.discover()[0]
        snapshot = Path(first['request'])
        before = snapshot.stat()
        second = self.discover(now=161.)[0]
        after = Path(second['request']).stat()
        self.assertEqual(first['request'], second['request'])
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))
        self.assertFalse(after.st_mode & 0o222)

    def test_conflicting_snapshot_is_not_overwritten_or_offered(self):
        _state, _current = self.request()
        first = self.discover()[0]
        snapshot = Path(first['request'])
        snapshot.chmod(0o600)
        snapshot.write_bytes(b'changed by another writer')
        other = str(uuid.uuid4())
        self.request(other)
        notices = self.discover(now=161.)
        self.assertEqual([n['caller_surface_uuid'] for n in notices], [other])
        self.assertEqual(snapshot.read_bytes(), b'changed by another writer')

    def test_snapshot_directory_symlink_cannot_redirect_discovered_body(self):
        _state, _current = self.request()
        external = self.home / 'other-owner'
        external.mkdir()
        snapshots = ready.state_root() / 'supervisor-request-snapshots-v1'
        snapshots.symlink_to(external, target_is_directory=True)
        self.assertEqual(self.discover(), [])
        self.assertEqual(list(external.iterdir()), [])

    def test_failed_publish_never_leaves_a_partial_final_snapshot(self):
        _state, current = self.request()
        with mock.patch.object(inbox.os, 'link', side_effect=OSError('publish interrupted')):
            self.assertEqual(self.discover(), [])
        root = ready.state_root() / 'supervisor-request-snapshots-v1'
        self.assertEqual([p.name for p in root.iterdir()], ['.publication.lock'])
        frontier = ready.read_json(self.cursor() / 'frontier.json')
        self.assertEqual(frontier['snapshot_failures'], {'count': 1, 'last_kind': 'OSError'})
        notice = self.discover(now=161.)[0]
        self.assertEqual(Path(notice['request']).read_bytes(), current.read_bytes())

    def test_snapshot_is_complete_and_read_only_before_final_name_is_published(self):
        _state, current = self.request()
        original = current.read_bytes()
        link = inbox.os.link
        def inspect(source, destination, **kwargs):
            fd = os.open(source, os.O_RDONLY, dir_fd=kwargs['src_dir_fd'])
            with os.fdopen(fd, 'rb') as stream:
                self.assertEqual(stream.read(), original)
                self.assertFalse(os.fstat(stream.fileno()).st_mode & 0o222)
            return link(source, destination, **kwargs)
        with mock.patch.object(inbox.os, 'link', side_effect=inspect):
            self.assertEqual(len(self.discover()), 1)

    def test_postlink_process_death_recovers_exact_staging_without_replacing_final(self):
        _state, current = self.request()
        raw = current.read_bytes()
        first = inbox.freeze_request(raw)
        final = Path(first['path'])
        inode = final.stat().st_ino
        staging = final.parent / ('.staging-' + first['sha256'])
        os.link(final, staging)
        self.assertEqual(final.stat().st_nlink, 2)
        notice = self.discover()[0]
        self.assertEqual(notice['request'], str(final))
        self.assertEqual(final.stat().st_ino, inode)
        self.assertEqual(final.stat().st_nlink, 1)
        self.assertFalse(staging.exists())
        self.assertEqual(final.read_bytes(), raw)

    def test_prelink_partial_staging_is_recovered_without_touching_other_names(self):
        _state, current = self.request()
        root = ready.state_root() / 'supervisor-request-snapshots-v1'
        root.mkdir(mode=0o700)
        digest = hashlib.sha256(current.read_bytes()).hexdigest()
        (root / ('.staging-' + digest)).write_bytes(b'partial')
        unrelated = root / '.staging-unrelated'
        unrelated.write_bytes(b'preserve')
        notice = self.discover()[0]
        self.assertEqual(Path(notice['request']).read_bytes(), current.read_bytes())
        self.assertEqual(unrelated.read_bytes(), b'preserve')

    def test_busy_snapshot_publication_lock_never_waits_or_removes_staging(self):
        _state, current = self.request()
        root = ready.state_root() / 'supervisor-request-snapshots-v1'
        root.mkdir(mode=0o700)
        temporary = root / ('.staging-' + hashlib.sha256(current.read_bytes()).hexdigest())
        temporary.write_bytes(b'in progress')
        with (root / '.publication.lock').open('w') as lock:
            os.chmod(lock.name, 0o600)
            inbox.fcntl.flock(lock, inbox.fcntl.LOCK_EX | inbox.fcntl.LOCK_NB)
            self.assertEqual(self.discover(), [])
            self.assertEqual(temporary.read_bytes(), b'in progress')
        self.assertEqual(len(self.discover(now=161.)), 1)

    def test_wrong_workspace_supervisor_paths_templates_and_inactive_requests_refused(self):
        _state, path = self.request()
        body = ready.read_json(path)
        changes = [dict(workspace_uuid=str(uuid.uuid4())), dict(supervisor=CALLER),
                   dict(caller_surface_uuid=str(uuid.uuid4())), dict(status='ANSWERED'),
                   dict(reply_mailbox='/tmp/unrelated.json'), dict(reply_template={}),
                   dict(marker='different')]
        for change in changes:
            with self.subTest(change=change):
                ready.write_json(path, dict(body, **change))
                self.assertEqual(self.discover(), [])
        ready.write_json(path, body)
        self.assertEqual(len(self.discover()), 1, self.discovery_trace)

    def test_frozen_full_request_can_reply_after_current_path_and_history_rotate(self):
        import mac_harness as harness
        with mock.patch.object(harness, '_ACTIVE_DIR', self.home / 'formal-task-markers'), \
                mock.patch.object(ready, '_bridge', side_effect=AssertionError('no transport')):
            reask.start(CALLER, SUP, 'slow-review', now=1., workspace=WORKSPACE)
            state = reask.tick(CALLER, 2., file_only=True)
            path = reask.inbox_path(state)
            notice = self.discover(now=3.)[0]
            raw, body = inbox.request_bytes(path)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), notice['request_sha256'])
            frozen = self.home / 'request-read-once.json'
            with frozen.open('xb') as handle:
                handle.write(raw)
            for index in range(1, reask.HISTORY + 5):
                state = reask.tick(CALLER, 2. + index * reask.INTERVAL, file_only=True)
            self.assertNotIn(body['marker'], [ask['marker'] for ask in state['asks']])
            self.assertNotEqual(hashlib.sha256(path.read_bytes()).hexdigest(), notice['request_sha256'])
            self.assertEqual(frozen.read_bytes(), raw)
            reply = dict(json.loads(frozen.read_bytes())['reply_template'],
                         trigger='The original complete request has been reviewed.')
            ready.write_json(Path(body['reply_mailbox']), reply)
            state = reask.tick(CALLER, state['next_due_epoch'], file_only=True)
            self.assertEqual(state['state'], reask.ANSWERED)
            self.assertEqual(state['reply']['marker'], body['marker'])
            self.assertEqual(state['reply']['body'], reply)
            self.assertEqual(state['terminal_attempts'], 0)

    def test_complete_reply_skips_notice_placeholder_is_not_a_reply(self):
        state, path = self.request()
        body = ready.read_json(path)
        reply_path = Path(body['reply_mailbox'])
        ready.write_json(reply_path, body['reply_template'])
        self.assertEqual(len(self.discover()), 1)
        reask.write_channels(state, 'EXECUTOR_READY_two', 'request', 62.0)
        body = ready.read_json(path)
        reply = dict(body['reply_template'], trigger='original chapter review completes')
        ready.write_json(Path(body['reply_mailbox']), reply)
        self.assertEqual(self.discover(), [])

    def test_symlink_and_oversized_request_are_not_read(self):
        _state, path = self.request()
        target = path.with_suffix('.source')
        path.rename(target)
        path.symlink_to(target)
        self.assertEqual(self.discover(), [])
        self.assertEqual(inbox.request_bytes(path), (None, None))
        path.unlink()
        path.write_bytes(b' ' * (ready.MAX_JSON_BYTES + 1))
        self.assertEqual(self.discover(), [])

    def test_more_than_notice_budget_progresses_next_hook(self):
        for _ in range(inbox.MAX_NOTICES + 1):
            self.request(str(uuid.uuid4()))
        self.assertEqual(len(self.discover()), inbox.MAX_NOTICES)
        self.assertEqual(len(self.discover()), 1)

    def test_sparse_tool_events_and_changing_markers_do_not_starve_later_callers(self):
        states = {}
        for _ in range(inbox.MAX_NOTICES * 3):
            caller = str(uuid.uuid4()).upper()
            states[caller], _path = self.request(caller)
        first = self.discover(now=100.0)
        # Repeatedly asking callers do not overtake those never shown yet.
        for item in first:
            reask.write_channels(states[item['caller_surface_uuid']],
                                 'EXECUTOR_READY_new', 'still waiting', 159.0)
        second = self.discover(now=160.0)
        third = self.discover(now=220.0)
        shown = [item['caller_surface_uuid'] for batch in (first, second, third)
                 for item in batch]
        self.assertEqual(len(shown), len(states))
        self.assertEqual(set(shown), set(states))
        self.assertEqual(len(set(shown)), len(shown))

    def test_emit_failure_and_read_only_observation_do_not_consume_notice(self):
        self.request()
        with self.assertRaises(BrokenPipeError):
            self.discover(emit=mock.Mock(side_effect=BrokenPipeError('client closed')))
        read_only = inbox.discover_bound(WORKSPACE, SUP,
                                        thread_id=self.payload['session_id'], now=100.0)
        self.assertEqual(len(read_only), 1)
        self.assertEqual(len(self.discover()), 1)
        self.assertEqual(self.discover(), [])

    def test_unanswered_request_is_reminded_after_sixty_seconds(self):
        self.request()
        self.assertEqual(len(self.discover(now=100.0)), 1)
        self.assertEqual(self.discover(now=159.999), [])
        self.assertEqual(len(self.discover(now=160.0)), 1, self.discovery_trace)
        self.assertEqual(self.discover(now=160.001), [])

    def test_eof_restart_uses_an_independent_directory_stream(self):
        _state, path = self.request()
        previous = inbox._frontier(path.parent, {})
        previous['pending'] = []
        self.assertGreater(previous['offset'], 0)
        native = inbox.directory.read_page
        first_fd = None
        pages = []

        def sticky_eof(fd, offset):
            nonlocal first_fd
            if first_fd is None:
                first_fd = fd
                os.lseek(fd, offset, os.SEEK_SET)
                result = [], offset
            else:
                # Model ext4's per-open-file EOF hash cache. Rewinding the
                # original stream (including a dup) does not clear that cache.
                os.lseek(fd, 0, os.SEEK_SET)
                if os.lseek(first_fd, 0, os.SEEK_CUR) == 0:
                    result = [], previous['offset']
                else:
                    result = native(fd, offset)
            pages.append(result)
            return result

        with mock.patch.object(inbox.directory, 'read_page', side_effect=sticky_eof):
            current = inbox._frontier(path.parent, previous)
        self.assertEqual(current['pending'], [path.name])
        self.assertNotEqual(current['round'], previous['round'])
        self.assertEqual(current['directory'], previous['directory'])
        self.assertEqual(len(pages), 2)
        self.assertEqual(sum(bool(names) for names, _offset in pages), 1)

    def test_eof_restart_stays_with_open_inode_when_directory_path_is_replaced(self):
        _state, path = self.request()
        root = path.parent
        previous = inbox._frontier(root, {})
        previous['pending'] = []
        archived = root.with_name(root.name + '-previous')
        replacement_name = '0' * 64 + '.json'
        native = inbox.directory.read_page
        reads = 0

        def replace_at_eof(fd, offset):
            nonlocal reads
            reads += 1
            if reads == 1:
                root.rename(archived)
                root.mkdir()
                (root / replacement_name).touch()
                return [], offset
            return native(fd, offset)

        with mock.patch.object(inbox.directory, 'read_page', side_effect=replace_at_eof):
            current = inbox._frontier(root, previous)
        self.assertEqual(current['directory'], previous['directory'])
        self.assertEqual(current['pending'], [path.name])
        self.assertEqual(reads, 2)
        # The next event sees the replacement inode and discards the old page.
        replaced = inbox._frontier(root, current)
        self.assertNotEqual(replaced['directory'], current['directory'])
        self.assertEqual(replaced['pending'], [replacement_name])

    def test_eof_restart_rejects_a_different_inode_and_closes_both_streams(self):
        _state, path = self.request()
        previous = inbox._frontier(path.parent, {})
        previous['pending'] = []
        original = copy.deepcopy(previous)
        other = self.home / 'different-inbox'
        other.mkdir()
        native_open = os.open
        opened = []

        def replace_reopened_stream(name, flags, *args, **kwargs):
            if name == '.' and 'dir_fd' in kwargs:
                fd = native_open(other, flags)
            else:
                fd = native_open(name, flags, *args, **kwargs)
            opened.append(fd)
            return fd

        with mock.patch.object(inbox.os, 'open', side_effect=replace_reopened_stream), \
                mock.patch.object(inbox.directory, 'read_page', return_value=([], previous['offset'])) as read:
            with self.assertRaises(OSError) as raised:
                inbox._frontier(path.parent, previous)
        self.assertEqual(raised.exception.errno, errno.ESTALE)
        self.assertEqual(read.call_count, 1)  # The foreign stream was never read.
        self.assertEqual(previous, original)
        self.assertEqual(len(opened), 2)
        for fd in opened:
            with self.assertRaises(OSError) as closed:
                os.fstat(fd)
            self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_discovery_cursors_are_isolated_by_actual_thread(self):
        self.request()
        self.assertEqual(len(self.discover()), 1)
        other = inbox.discover_bound(WORKSPACE, SUP, thread_id=str(uuid.uuid4()),
                                     emit=self.emissions.append, now=100.0)
        self.assertEqual(len(other), 1)
        self.assertEqual(self.discover(), [])

    def test_automatic_entry_contract_without_active_task_marker(self):
        self.request()
        output = io.StringIO()
        payload = self.payload
        with mock.patch.object(inbox.identity, 'evaluation', return_value=contextlib.nullcontext()) as evaluation, \
                mock.patch.object(inbox.identity, 'identity', return_value=(WORKSPACE, SUP)), \
                mock.patch.object(inbox.identity, 'native_proof', return_value=self.proof), \
                mock.patch.object(guard.stop_guard, 'ACTIVE_DIR', self.home / 'absent'), \
                mock.patch.object(guard.sys, 'stdin', io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(output):
            self.assertEqual(guard.main(), 0)
        evaluation.assert_called_once_with(payload)
        result = json.loads(output.getvalue())['hookSpecificOutput']
        self.assertEqual(result['hookEventName'], 'PostToolUse')
        self.assertIn('REQUEST_DISCOVERED', result['additionalContext'])
        self.assertIn('REPLY=', result['additionalContext'])
        self.assertEqual(inbox.discover(dict(hook_event_name='Stop')), [])

    def cursor(self, thread=None):
        return (ready.state_root() / 'supervisor-inbox-discovery-v2'
                / ready.digest(json.dumps([WORKSPACE, SUP, thread or self.payload['session_id']])))

    def refuse_whole_inbox_scan(self, root):
        real_scandir = os.scandir

        def scan(path='.'):
            if not isinstance(path, int) and Path(os.fsdecode(path)).resolve() == root.resolve():
                raise AssertionError('no full inbox scan')
            # Bound reply-mailbox lookup is a separate, legitimate operation.
            return real_scandir(path)

        return mock.patch.object(os, 'scandir', side_effect=scan)

    def test_large_real_inbox_drains_despite_marker_churn_and_sparse_events(self):
        states = {}
        for _ in range(inbox.MAX_ENTRIES + 65):
            caller = str(uuid.uuid4()).upper()
            states[caller], _path = self.request(caller)
        seen = set()
        first = None
        calls = []
        native = inbox.directory.read_page

        def observe(fd, offset):
            result = native(fd, offset)
            calls.append(len(result[0]))
            return result

        with mock.patch.object(inbox.directory, 'read_page', side_effect=observe), \
                self.refuse_whole_inbox_scan(_path.parent):
            for index in range(len(states)):
                calls.clear()
                batch = self.discover(now=100.0 + index * 60)
                self.assertLessEqual(len(batch), inbox.MAX_NOTICES)
                self.assertLessEqual(sum(calls), inbox.MAX_ENTRIES)
                self.assertLessEqual(len(calls), 2)
                if first is None and batch:
                    first = batch[0]['caller_surface_uuid']
                shown = {notice['caller_surface_uuid'] for notice in batch}
                # Before the first walk completes, even old requests eligible
                # for a reminder cannot overtake undiscovered later callers.
                self.assertFalse(shown & seen)
                seen.update(shown)
                if seen == set(states):
                    break
                if first:
                    reask.write_channels(states[first], f'EXECUTOR_READY_churn_{index}',
                                         'still waiting', 101.0 + index * 60)
            self.assertEqual(seen, set(states))

    def test_junk_bad_files_and_symlinks_do_not_stop_later_valid_requests(self):
        _state, valid = self.request()
        wanted = json.loads(valid.read_bytes())
        for index in range(inbox.MAX_ENTRIES * 3):
            name = f'{index:064x}.json'
            p = valid.parent / name
            if index % 4 == 0:
                p.symlink_to(valid)
            elif index % 4 == 1:
                p.mkdir()
            elif index % 4 == 2:
                p.write_text('{broken')
            else:
                p.write_text(json.dumps(dict(wanted, supervisor=CALLER)))
        for index in range(inbox.MAX_ENTRIES + 1):
            (valid.parent / f'junk-{index}').touch()
        batches = []
        with self.refuse_whole_inbox_scan(valid.parent):
            for _ in range(100):
                batch = self.discover()
                batches.extend(batch)
                if batch:
                    break
        self.assertEqual([notice['caller_surface_uuid'] for notice in batches], [CALLER])

    def test_emit_failure_leaves_frontier_and_reminder_files_unchanged(self):
        for _ in range(20):
            self.request(str(uuid.uuid4()))
        first = self.discover()
        cursor = self.cursor()
        before = {p.name: p.read_bytes() for p in cursor.iterdir() if p.suffix == '.json'}
        observation = inbox.discover_bound(WORKSPACE, SUP,
            thread_id=self.payload['session_id'], now=160.0)
        self.assertEqual(len(observation), inbox.MAX_NOTICES)
        with self.assertRaises(BrokenPipeError):
            self.discover(now=160.0, emit=mock.Mock(side_effect=BrokenPipeError('flush failed')))
        self.assertEqual(before, {p.name: p.read_bytes() for p in cursor.iterdir()
                                  if p.suffix == '.json'})
        second = self.discover(now=160.0)
        self.assertEqual(second, observation)
        self.assertFalse({x['caller_surface_uuid'] for x in first}
                         & {x['caller_surface_uuid'] for x in second})

    def test_empty_pages_commit_only_after_successful_flush_without_empty_notice(self):
        _state, path = self.request()
        path.unlink()
        (path.parent / 'junk').touch()
        cursor = self.cursor()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.discover(emit=inbox.emit_context), [])
        self.assertEqual(output.getvalue(), '')
        frontier = cursor / 'frontier.json'
        before = frontier.read_bytes()
        with mock.patch.object(inbox.sys, 'stdout') as stream:
            stream.flush.side_effect = BrokenPipeError('no client')
            with self.assertRaises(BrokenPipeError):
                self.discover(emit=inbox.emit_context)
        self.assertEqual(frontier.read_bytes(), before)

    def test_replaced_directory_invalidates_saved_backlog_and_symlink_is_refused(self):
        for _ in range(15):
            self.request(str(uuid.uuid4()))
        first = self.discover()
        root = Path(first[0]['source_request']).parent
        archived = root.with_name(root.name + '-previous')
        root.rename(archived)
        root.mkdir()
        caller = str(uuid.uuid4())
        self.request(caller)
        self.assertEqual([x['caller_surface_uuid'] for x in self.discover()], [caller])
        for p in root.iterdir():
            p.unlink()
        root.rmdir()
        root.symlink_to(archived, target_is_directory=True)
        self.assertEqual(self.discover(), [])

    def test_large_inbox_frontiers_are_independent_between_actual_threads(self):
        for _ in range(20):
            self.request(str(uuid.uuid4()))
        first = self.discover()
        other = str(uuid.uuid4())
        another = inbox.discover_bound(WORKSPACE, SUP, thread_id=other,
                                       emit=self.emissions.append, now=100.0)
        self.assertEqual([x['request'] for x in first], [x['request'] for x in another])
        second = self.discover()
        another_second = inbox.discover_bound(WORKSPACE, SUP, thread_id=other,
                                              emit=self.emissions.append, now=100.0)
        self.assertEqual([x['request'] for x in second], [x['request'] for x in another_second])


if __name__ == '__main__':
    unittest.main()

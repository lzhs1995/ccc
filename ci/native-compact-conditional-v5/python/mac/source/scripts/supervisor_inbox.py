"""Bounded file-request discovery by the existing authenticated supervisor hook.

No terminal input. A discovery record is not native receipt or task acceptance.
Current requests and cursors remain bounded; emitted request snapshots retain
the exact bytes until their original reply can be read and consumed.
"""
import fcntl
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import time
import uuid

import bounded_directory as directory
import cmux_hook_identity as identity
import cmux_hook_thread as thread_scope
import cmux_prompt_reference as references
import executor_ready as ready
import executor_reask as reask
import executor_reply as replies

MAX_ENTRIES = 256
MAX_NOTICES = 8
REMINDER_SECONDS = 60.0


def _request_name(name):
    return (isinstance(name, str) and len(name) == 69 and name.endswith('.json')
            and all(char in '0123456789abcdef' for char in name[:-5]))


def _frontier(inbox, previous):
    """At most one nonempty native page; retain its unprocessed request names."""
    fd = os.open(inbox, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        binding = [info.st_dev, info.st_ino]
        state = previous if isinstance(previous, dict) else {}
        offset = state.get('offset')
        names = state.get('pending')
        if (state.get('schema') != 'supervisor-inbox-frontier-v1'
                or state.get('directory') != binding
                or type(offset) is not int or not 0 <= offset < 2**63
                or not _uuid(state.get('round'))
                or not isinstance(names, list) or len(names) > MAX_ENTRIES
                or any(not _request_name(name) for name in names)):
            state = dict(schema='supervisor-inbox-frontier-v1', directory=binding,
                         offset=0, pending=[], round=str(uuid.uuid4()))
        else:
            state = dict(state, pending=list(names))
        if state['pending']:
            return state
        try:
            names, following = directory.read_page(fd, state['offset'])
            if not names and state['offset']:
                # An EOF read consumes no entries. Restart here so small inboxes
                # need no extra empty hook between reminders or new markers.
                state['round'] = str(uuid.uuid4())
                # ext4 can retain its EOF hash after lseek(0) on this stream.
                # Reopen relative to the held fd for a fresh stream of the same
                # inode, even if the inbox path was replaced. dup shares state.
                rewind_fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY
                                    | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    rewind_info = os.fstat(rewind_fd)
                    if [rewind_info.st_dev, rewind_info.st_ino] != binding:
                        raise OSError(errno.ESTALE, 'reopened inbox directory identity changed')
                    names, following = directory.read_page(rewind_fd, 0)
                finally:
                    os.close(rewind_fd)
        except OSError as exc:
            if exc.errno != errno.EINVAL or not state['offset']:
                raise
            # A removed directory entry can invalidate a filesystem offset.
            # Commit the rewind only after emit/flush, then try next event.
            state.update(offset=0, pending=[], round=str(uuid.uuid4()))
            return state
        state['offset'] = following
        state['pending'] = list(dict.fromkeys(name for name in names if _request_name(name)))
        return state
    finally:
        os.close(fd)


def request_bytes(path):
    """Hash and parse the same bounded regular-file snapshot, never a second read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > ready.MAX_JSON_BYTES:
                return None, None
            raw = handle.read(ready.MAX_JSON_BYTES + 1)
            after = os.fstat(handle.fileno())
            if (len(raw) > ready.MAX_JSON_BYTES or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                return None, None
        body = json.loads(raw)
        return (raw, body) if isinstance(body, dict) else (None, None)
    except (OSError, ValueError):
        return None, None


def freeze_request(raw):
    """Publish exact bytes; recover an interrupted own staging name in O(1)."""
    root = ready.state_root() / 'supervisor-request-snapshots-v1'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(raw).hexdigest()
    final = digest + '.txt'
    temporary = '.staging-' + digest
    reference = dict(format=references.SINGLE_LINE_FORMAT,
                     path=str(root / final), sha256=digest, bytes=len(raw))
    text = raw.decode('utf-8')
    folder = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    try:
        info = os.fstat(folder)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('UNSAFE_REQUEST_SNAPSHOT_DIRECTORY')
        lock = os.open('.publication.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                       0o600, dir_fd=folder)
        lock_info = os.fstat(lock)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o077):
            raise ValueError('UNSAFE_REQUEST_SNAPSHOT_LOCK')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # One short publication lock lets a later hook clean this exact staging
        # name after process death. No directory scan or age-based deletion.
        try:
            stale = os.stat(temporary, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            stale = None
        if stale is not None:
            if (not stat.S_ISREG(stale.st_mode) or stale.st_uid != os.getuid()
                    or stale.st_nlink not in (1, 2)):
                raise ValueError('UNSAFE_REQUEST_STAGING')
            if stale.st_nlink == 2:
                final_info = os.stat(final, dir_fd=folder, follow_symlinks=False)
                if (stale.st_dev, stale.st_ino) != (final_info.st_dev, final_info.st_ino):
                    raise ValueError('UNSAFE_REQUEST_STAGING_LINK')
            os.unlink(temporary, dir_fd=folder)
        try:
            os.stat(final, dir_fd=folder, follow_symlinks=False)
        except FileNotFoundError:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=folder)
            try:
                with os.fdopen(fd, 'wb') as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fchmod(handle.fileno(), 0o400)
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, final, src_dir_fd=folder, dst_dir_fd=folder,
                            follow_symlinks=False)
                except FileExistsError:
                    pass  # Never replace a conflicting final name.
            finally:
                os.unlink(temporary, dir_fd=folder)
            os.fsync(folder)
        current = root.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('REQUEST_SNAPSHOT_DIRECTORY_CHANGED')
        references.verify(reference, text)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(folder)
    return reference


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError):
        return False


def discover(payload, *, emit=None):
    if not isinstance(payload, dict) or payload.get('hook_event_name') != 'PostToolUse':
        return []
    facts = thread_scope.describe(payload)
    if facts['thread_scope'] != 'ROOT_METADATA':
        return []
    with identity.evaluation(payload):
        workspace, supervisor = identity.identity(payload)
        if (not _uuid(workspace) or not _uuid(supervisor)
                or not thread_scope.authenticated_root(facts, identity.native_proof())):
            return []
        return discover_bound(workspace, supervisor, thread_id=facts['actual_thread_id'], emit=emit)


def answered(body, reply_path, scan=None):
    binding = dict(caller=body['caller_surface_uuid'], supervisor=body['supervisor'],
                   task_id=body.get('task_id', ''), episode_id=body['episode_id'])
    if replies.valid(ready.read_json(reply_path), [body['marker']], **binding):
        return True
    state = reask.load(body['caller_surface_uuid']) or {}
    keys = ('caller_surface_uuid', 'supervisor', 'workspace_uuid', 'task_id', 'episode_id')
    if any(state.get(key) != body.get(key) for key in keys):
        return False
    if scan is not None:
        state['reply_scan_ordinal'] = scan.get('reply_scan_ordinal', 1)
    direct = reask.mailbox_reply(state)
    if scan is not None:
        scan['reply_scan_ordinal'] = state.get('reply_scan_ordinal', 1)
    if direct:
        return True
    # Authenticate older issued markers even after the 20-entry history rolls over.
    for path in reask._recent_files(ready.mailbox_dir(binding['caller']),
                                    float(state.get('started_epoch', 0))):
        reply = ready.read_json(path)
        if (reply and reask.issued_marker(state, reply.get('marker'))
                and replies.valid(reply, [reply['marker']], **binding)):
            return True
    return False


def discover_bound(workspace, supervisor, *, thread_id=None, emit=None, now=None):
    """Freeze exact requests before exposure; commit cursors only after flush."""
    if not _uuid(workspace) or not _uuid(supervisor) or not _uuid(thread_id):
        return []
    now = time.time() if now is None else now
    inbox = ready.state_root() / 'supervisor-inbox-v1' / ready.digest(supervisor)
    if not inbox.is_dir() or inbox.is_symlink():
        return []
    cursor = (ready.state_root() / 'supervisor-inbox-discovery-v2'
              / ready.digest(json.dumps([workspace, supervisor, thread_id])))
    cursor.mkdir(parents=True, exist_ok=True, mode=0o700)
    if cursor.is_symlink():
        return []
    fd = os.open(cursor / 'sweep.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return []
        frontier_path = cursor / 'frontier.json'
        frontier = _frontier(inbox, ready.read_json(frontier_path))
        paths = [inbox / name for name in frontier['pending']]
        # Use the last successful emission, even when a new marker replaced the
        # request. Path order starves later callers when tool events are >=60s
        # apart; a busy caller issuing new markers must not jump the queue.
        pending = []
        for path in paths:
            seen = ready.read_json(cursor / path.name) or {}
            try:
                last_emitted = float(seen.get('discovered_epoch', 0))
            except (ValueError, TypeError, OverflowError):
                last_emitted = 0.0
            if not math.isfinite(last_emitted) or last_emitted > now:
                last_emitted = 0.0
            pending.append((last_emitted, str(path), path, seen))
        notices = []
        snapshot_failures = dict(count=0)
        ordered = sorted(pending, key=lambda item: item[:2])
        frontier['pending'] = []
        for index, (last_emitted, _name, path, seen) in enumerate(ordered):
            if seen.get('discovery_round') == frontier['round']:
                continue  # replacement/churn must not jump ahead in this walk
            raw, body = request_bytes(path)
            if (not body or body.get('supervisor') != supervisor
                    or body.get('workspace_uuid') != workspace
                    or not _uuid(body.get('caller_surface_uuid'))
                    or path.name != ready.digest(body['caller_surface_uuid']) + '.json'
                    or body.get('status') != 'WAITING_REPLY'
                    or body.get('schema') not in ('executor-reask-current-v2', 'coordination-request-v1')):
                continue
            marker = body.get('marker')
            if not isinstance(marker, str) or not marker or len(marker) > 160:
                continue
            template = body.get('reply_template')
            if not replies.valid(template, [marker], caller=body['caller_surface_uuid'],
                                 supervisor=supervisor, task_id=body.get('task_id', ''),
                                 episode_id=body.get('episode_id'), template_only=True):
                continue
            # Only the canonical mailbox is offered as a write target.
            reply_path = ready.mailbox_dir(body['caller_surface_uuid']) / (marker + '.json')
            if body.get('reply_mailbox') != str(reply_path):
                continue
            scan = dict(reply_scan_ordinal=seen.get('reply_scan_ordinal', 1))
            if answered(body, reply_path, scan):
                continue
            token = ready.digest(json.dumps([marker, body['episode_id'], body.get('task_id', '')]))
            if (seen.get('token') == token
                    and now - last_emitted < REMINDER_SECONDS):
                continue
            try:
                frozen = freeze_request(raw)
            except (OSError, ValueError) as exc:
                # Do not publish a moving path or overwrite a conflicting
                # snapshot. Other valid callers can still be discovered.
                snapshot_failures.update(count=snapshot_failures['count'] + 1,
                                         last_kind=type(exc).__name__)
                continue
            notice = dict(state='REQUEST_DISCOVERED', request=frozen['path'],
                          source_request=str(path),
                          request_sha256=frozen['sha256'], marker=marker,
                          caller_surface_uuid=body['caller_surface_uuid'], supervisor_uuid=supervisor,
                          workspace_uuid=workspace, task_id=body.get('task_id', ''),
                          episode_id=body['episode_id'], reply_mailbox=str(reply_path),
                          reply_template=template, discovered_epoch=now, actual_thread_id=thread_id,
                          discovery_round=frontier['round'],
                          token=token, input_operations=0, native_delivery_confirmed=False)
            notice.update(scan)
            notices.append(notice)
            if len(notices) >= MAX_NOTICES:
                frontier['pending'] = [item[2].name for item in ordered[index + 1:]]
                break
        if emit is not None:
            emit(notices)  # exceptions leave all cursors unchanged; no lost notice
            frontier['snapshot_failures'] = snapshot_failures
            for notice in notices:
                ready.write_json(cursor / Path(notice['source_request']).name, notice)
            # Empty/invalid pages also advance, but observation never consumes.
            ready.write_json(frontier_path, frontier)
        return notices


def emit_context(notices):
    if not notices:
        sys.stdout.flush()
        return
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PostToolUse', 'additionalContext': context(notices),
    }}, ensure_ascii=False), flush=True)


def context(notices):
    lines = ['REQUEST_DISCOVERED: read the exact file request and reply to its bound mailbox. '
             'File discovery is not native transport, a new task pack or business acceptance.']
    for notice in notices:
        lines.append('REQUEST=' + notice['request'] + ' SHA256=' + notice['request_sha256']
                     + ' REPLY=' + notice['reply_mailbox'] + ' TEMPLATE='
                     + json.dumps(notice['reply_template'], ensure_ascii=False))
    return '\n'.join(lines)

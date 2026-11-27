"""Bounded, read-only check of the original Codex IDs' native writer locks.

This never creates/removes lock files or starts an agent. An idle result is an
observation in one Codex home; the official client acquires its own writer lock
again when the user actually resumes. No input or recovery authority is issued.
"""
from contextlib import ExitStack
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

MAX_HEADER = 256 * 1024
LABELS = {
    'IN_USE': '原会话仍被占用，请勿重复恢复',
    'IDLE_AT_CHECK': '本次检查未见占用；仅供手动恢复前参考',
    'UNKNOWN': '无法确认，保留原会话并检查原因',
}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def identity(info):
    return info.st_dev, info.st_ino


def regular(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_nlink == 1 and not info.st_mode & 0o022)


def inspect_writer(target, *, codex_home=None):
    """No side effects beyond short, nonblocking shared advisory locks."""
    result = {
        'session_id': target.get('sid'), 'state': 'UNKNOWN',
        'reason': 'not_checked', 'file_writes': 0, 'agent_launches': 0,
        'recovery_authorized': False,
        'scope': 'exact original history and native locks in one Codex home',
    }
    try:
        require(target.get('agent') == 'codex', 'not_a_codex_target')
        sid = target['sid']
        require(isinstance(sid, str) and str(uuid.UUID(sid)) == sid,
                'original_sid_not_canonical')
        home = Path(codex_home if codex_home is not None else
                    os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        require(home.is_absolute() and home.resolve(strict=True) == home,
                'codex_home_not_canonical')
        history = Path(target['history'])
        require(history.is_absolute() and history.is_relative_to(home / 'sessions')
                and history.resolve(strict=True) == history,
                'original_history_outside_current_home')
        directory = home / 'thread-writer-locks'
        require(directory.resolve(strict=True) == directory,
                'writer_directory_not_canonical')
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        with ExitStack() as cleanup:
            dfd = os.open(directory, flags | os.O_DIRECTORY)
            cleanup.callback(os.close, dfd)
            dinfo = os.fstat(dfd)
            require(stat.S_ISDIR(dinfo.st_mode) and dinfo.st_uid == os.getuid()
                    and not dinfo.st_mode & 0o022,
                    'writer_directory_owner_or_mode')

            def directory_unchanged():
                require(directory.resolve(strict=True) == directory
                        and identity(directory.lstat()) == identity(dinfo),
                        'writer_directory_replaced')

            def open_existing(name):
                fd = os.open(name, flags, dir_fd=dfd)
                cleanup.callback(os.close, fd)
                before = os.fstat(fd)
                require(regular(before), 'native_lock_not_regular_owned_single_link')
                try:
                    current = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except FileNotFoundError as error:
                    raise ValueError('native_lock_disappeared_after_open') from error
                require(identity(current) == identity(before) and regular(current),
                        'native_lock_replaced')
                return fd, before

            def nonblocking_shared(fd):
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                        return False
                    raise
                cleanup.callback(fcntl.flock, fd, fcntl.LOCK_UN)
                return True

            # The native implementation takes this lock for creation/removal.
            # A busy coordinator is UNKNOWN; do not wait or infer writer absence.
            coord, cinfo = open_existing('.coordination.lock')
            require(nonblocking_shared(coord), 'native_coordination_busy')
            directory_unchanged()

            hfd = os.open(history, flags)
            cleanup.callback(os.close, hfd)
            hinfo = os.fstat(hfd)
            # Historical backups can legitimately retain a hard link to this
            # same inode. This is only a bounded read; never unlink or rewrite
            # those records. Native lock files still require a single link.
            require(stat.S_ISREG(hinfo.st_mode) and hinfo.st_uid == os.getuid()
                    and not hinfo.st_mode & 0o022,
                    'original_history_not_regular_owned')
            raw = os.pread(hfd, MAX_HEADER, 0)
            require(b'\n' in raw, 'original_history_header_incomplete')
            header = json.loads(raw.split(b'\n', 1)[0])
            require(header.get('type') == 'session_meta'
                    and header.get('payload', {}).get('id') == sid,
                    'original_history_header_sid_mismatch')

            lock_name = sid + '.lock'
            try:
                writer, winfo = open_existing(lock_name)
            except FileNotFoundError:
                state, reason = 'IDLE_AT_CHECK', 'no_thread_lock_under_coordination'
                writer = None
            else:
                if nonblocking_shared(writer):
                    state, reason = 'IDLE_AT_CHECK', 'thread_lock_not_held_at_check'
                else:
                    state, reason = 'IN_USE', 'native_thread_writer_lock_held'

            # All final checks use the same opened namespace and original file.
            directory_unchanged()
            require(identity(os.stat('.coordination.lock', dir_fd=dfd,
                                     follow_symlinks=False)) == identity(cinfo),
                    'native_coordination_replaced')
            require(history.resolve(strict=True) == history
                    and identity(history.lstat()) == identity(hinfo)
                    and os.fstat(hfd).st_size >= hinfo.st_size,
                    'original_history_replaced_or_truncated')
            if writer is not None:
                require(identity(os.stat(lock_name, dir_fd=dfd, follow_symlinks=False))
                        == identity(winfo), 'native_writer_lock_replaced')
            else:
                try:
                    os.stat(lock_name, dir_fd=dfd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError('native_writer_lock_appeared_during_check')
            result.update(state=state, reason=reason, codex_home=str(home))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result.update(state='UNKNOWN', reason=str(error))
    return result


def print_checks(targets):
    """A user check, not a preflight authorization or a machine-wide scan."""
    print('原 Codex 会话占用检查（只读，不启动任何会话）：')
    for name, target in targets.items():
        if target.get('agent') != 'codex':
            continue
        report = inspect_writer(target)
        print(f"{name}: {report['session_id']}\n"
              f"  {LABELS[report['state']]} [{report['state']}: {report['reason']}]")
    print('检查结束后状态仍可能变化；真正恢复由官方客户端再次取得原 ID 的写锁。')

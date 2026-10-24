"""Publish complete observation backups without replacing an existing name.

Control intents retain their original create-before-mutation semantics. Only
raw ledger backups use a staged, fsynced file and an atomic exclusive rename.
A crash leaves either a complete single-link backup or a separate temp file.
"""
import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys


def _rename_exclusive(directory_fd, source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'darwin':
        rename = libc.renameatx_np
        flag = 0x00000004  # RENAME_EXCL, sys/stdio.h.
    elif sys.platform.startswith('linux'):
        rename = getattr(libc, 'renameat2', None)
        if rename is None:
            raise NotImplementedError('atomic exclusive rename is unavailable')
        flag = 1  # RENAME_NOREPLACE.
    else:
        raise NotImplementedError('atomic exclusive backup publication is unsupported')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                       ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    result = rename(directory_fd, os.fsencode(source), directory_fd,
                    os.fsencode(destination), flag)
    if result:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, os.strerror(error), destination)
        raise OSError(error, os.strerror(error), destination)


def _discard_unpublished(directory, temporary, held_fd, original):
    """Discard only this call's held inode containing its own backup prefix."""
    directory.verify()
    held = os.fstat(held_fd)
    current = directory._file_identity(temporary, held)
    if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.geteuid()
            or held.st_nlink != 1 or current.st_size > len(original)):
        raise ValueError('unpublished backup identity changed; retained')
    data = os.pread(held_fd, current.st_size, 0)
    if not original.startswith(data) or len(data) != current.st_size:
        raise ValueError('unpublished backup bytes changed; retained')
    directory._file_identity(temporary, held)
    os.unlink(temporary, dir_fd=directory.fds[-1])
    os.fsync(directory.fds[-1])


def publish_backup(directory, name, raw):
    """Keep an existing digest-name backup intact; never expose partial bytes."""
    name = directory._name(name)
    if not isinstance(raw, bytes):
        raise TypeError('backup data must be bytes')
    directory.verify()
    if directory._file_identity(name) is not None:
        raise FileExistsError(name)
    temporary = '.backup-' + hashlib.sha256(raw).hexdigest() + '-' + secrets.token_hex(12) + '.tmp'
    held_fd = None
    published = False
    try:
        # Keep the inode open even when a partial write raises. A random-name
        # collision or foreign replacement is never owned by this call.
        held_fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=directory.fds[-1])
        prepared = os.fstat(held_fd)
        with os.fdopen(held_fd, 'wb', closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory._file_identity(temporary, prepared)
        os.fsync(directory.fds[-1])
        if directory.read_bytes(temporary, limit=len(raw)) != raw:
            raise ValueError('prepared backup changed')
        directory.verify()
        _rename_exclusive(directory.fds[-1], temporary, name)
        published = True
        os.fsync(directory.fds[-1])
        directory._file_identity(name, prepared)
        directory.verify()
    except BaseException as original_error:
        if held_fd is not None and not published:
            try:
                _discard_unpublished(directory, temporary, held_fd, raw)
            except Exception as cleanup_error:
                if hasattr(original_error, 'add_note'):
                    original_error.add_note('Unpublished backup retained: ' + str(cleanup_error)[:300])
        raise
    finally:
        if held_fd is not None:
            os.close(held_fd)
    return hashlib.sha256(raw).hexdigest()

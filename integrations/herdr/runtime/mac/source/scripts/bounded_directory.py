"""One bounded kernel directory page, without libc DIR stream read-ahead.

The fd offset belongs to the directory device/inode, not a telldir cookie or a
Python iterator. Callers retain unread names and only commit the next offset
after their output succeeds. An atomic request replacement must not reset the
walk. Directory replacement does reset it. Real directory tests exercise both
Darwin's getdirentries64 wrapper and Linux's getdents64 in their native CI jobs.
"""
import ctypes
import errno
import os
import struct
import sys

PAGE_BYTES = 4096
# Even the smallest native record occupies at least 24 bytes on both systems.
MAX_PAGE_ENTRIES = PAGE_BYTES // 24


def _names(raw, darwin):
    names = []
    at = 0
    while at < len(raw):
        if len(raw) - at < 24:
            raise OSError(errno.EIO, 'short directory record')
        length = struct.unpack_from('=H', raw, at + 16)[0]
        if length < 24 or at + length > len(raw):
            raise OSError(errno.EIO, 'invalid directory record length')
        start = at + (21 if darwin else 19)
        if darwin:
            size = struct.unpack_from('=H', raw, at + 18)[0]
            end = start + size
            if end >= at + length or raw[end] != 0:
                raise OSError(errno.EIO, 'invalid directory name length')
        else:
            end = raw.find(b'\0', start, at + length)
            if end < 0:
                raise OSError(errno.EIO, 'unterminated directory name')
        name = raw[start:end]
        if not name or b'/' in name or b'\0' in name:
            raise OSError(errno.EIO, 'invalid directory name')
        names.append(os.fsdecode(name))
        at += length
    if len(names) > MAX_PAGE_ENTRIES:
        raise OSError(errno.EIO, 'directory page exceeds entry budget')
    return names


def read_page(fd, offset):
    """Return (names, next_offset); an empty page means EOF.

    Uses a fresh/reopened fd and a bounded native buffer. No stat per entry,
    sorting of the whole directory, unbounded scandir or background process.
    Unsupported platforms fail closed instead of silently doing an unbounded
    scan. The inbox observer handles an invalidated saved offset by restarting
    on a later event; it does not bind it to a different directory inode.
    """
    os.lseek(fd, offset, os.SEEK_SET)
    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(PAGE_BYTES)
    if sys.platform == 'darwin':
        # This is the 64-bit wrapper also used by Darwin's libc readdir. The
        # deprecated getdirentries symbol has a different, 32-bit inode ABI.
        fn = getattr(libc, '__getdirentries64', None)
        if fn is None:
            raise OSError(errno.ENOSYS, '64-bit directory paging unavailable')
        fn.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t,
                       ctypes.POINTER(ctypes.c_int64)]
        fn.restype = ctypes.c_ssize_t
        base = ctypes.c_int64()
        size = fn(fd, buffer, PAGE_BYTES, ctypes.byref(base))
    elif sys.platform.startswith('linux'):
        fn = getattr(libc, 'getdents64', None)
        if fn is None:
            raise OSError(errno.ENOSYS, '64-bit directory paging unavailable')
        fn.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        fn.restype = ctypes.c_ssize_t
        size = fn(fd, buffer, PAGE_BYTES)
    else:
        raise OSError(errno.ENOSYS, 'directory paging platform unsupported')
    if size < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if size > PAGE_BYTES:
        raise OSError(errno.EIO, 'oversized directory page')
    following = os.lseek(fd, 0, os.SEEK_CUR)
    if size and following == offset:
        raise OSError(errno.EIO, 'directory page made no progress')
    return _names(buffer.raw[:size], sys.platform == 'darwin'), following

"""Bounded, fresh Darwin vnode FD inventory through the public libproc ABI.

Layouts match sys/proc_info.h in the macOS SDK. No FD results are cached, and
an incomplete or changing inventory is an authentication failure, not a reason
to fall back to a weaker selection. The caller still checks process birth,
ancestry, unique writable rollout, native metadata and the file before/after.
"""
import ctypes as C
import os
import sys

import cmux_identity_budget as budget

MAX_FDS = 8192
MAX_PATH = 1024
PROC_PIDLISTFDS = 1
PROC_PIDFDVNODEPATHINFO = 2
PROX_FDTYPE_VNODE = 1
FREAD, FWRITE = 1, 2


class FDInfo(C.Structure):
    _fields_ = [('fd', C.c_int32), ('fdtype', C.c_uint32)]


class FileInfo(C.Structure):
    _fields_ = [('flags', C.c_uint32), ('status', C.c_uint32),
                ('offset', C.c_int64), ('type', C.c_int32), ('guard', C.c_uint32)]


class VStat(C.Structure):
    _fields_ = [('dev', C.c_uint32), ('mode', C.c_uint16), ('nlink', C.c_uint16),
                ('ino', C.c_uint64), ('uid', C.c_uint32), ('gid', C.c_uint32),
                ('atime', C.c_int64), ('atimensec', C.c_int64),
                ('mtime', C.c_int64), ('mtimensec', C.c_int64),
                ('ctime', C.c_int64), ('ctimensec', C.c_int64),
                ('birthtime', C.c_int64), ('birthtimensec', C.c_int64),
                ('size', C.c_int64), ('blocks', C.c_int64),
                ('blksize', C.c_int32), ('flags', C.c_uint32),
                ('gen', C.c_uint32), ('rdev', C.c_uint32), ('spare', C.c_int64 * 2)]


class VNodeInfo(C.Structure):
    _fields_ = [('stat', VStat), ('type', C.c_int32), ('pad', C.c_int32),
                ('fsid', C.c_int32 * 2)]


class VNodePath(C.Structure):
    _fields_ = [('info', VNodeInfo), ('path', C.c_char * MAX_PATH)]


class VNodeFD(C.Structure):
    _fields_ = [('file', FileInfo), ('vnode', VNodePath)]


def supported():
    return sys.platform == 'darwin'


def _need(ok, reason):
    if not ok:
        raise ValueError('native FD inventory: ' + reason)


def _library():
    _need(supported(), 'Darwin required')
    _need(tuple(C.sizeof(t) for t in (FDInfo, FileInfo, VStat, VNodeInfo, VNodePath, VNodeFD))
          == (8, 24, 136, 152, 1176, 1200)
          and VNodeFD.vnode.offset == 24 and VNodePath.path.offset == 152,
          'unsupported libproc ABI')
    library = C.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    library.proc_pidinfo.argtypes = [C.c_int, C.c_int, C.c_uint64, C.c_void_p, C.c_int]
    library.proc_pidinfo.restype = C.c_int
    library.proc_pidfdinfo.argtypes = [C.c_int, C.c_int, C.c_int, C.c_void_p, C.c_int]
    library.proc_pidfdinfo.restype = C.c_int
    return library


def _list(library, pid):
    # A spare slot distinguishes the supported maximum from truncation. Never
    # accept a full buffer or rely on an earlier count while FDs may be added.
    entries = (FDInfo * (MAX_FDS + 1))()
    budget.check()
    count = library.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, entries, C.sizeof(entries))
    budget.check()
    _need(0 < count < C.sizeof(entries) and count % C.sizeof(FDInfo) == 0,
          'failed, truncated or malformed descriptor list')
    result = [(e.fd, e.fdtype) for e in entries[:count // C.sizeof(FDInfo)]]
    _need(all(fd >= 0 for fd, _ in result) and len({fd for fd, _ in result}) == len(result),
          'duplicate or invalid descriptor')
    return sorted(result)


def frames(pid):
    _need(type(pid) is int and 1 < pid <= 2147483647, 'valid pid required')
    library = _library()
    before = _list(library, pid)
    result = []
    for fd, kind in before:
        if kind != PROX_FDTYPE_VNODE:
            continue
        info = VNodeFD()
        budget.check()
        count = library.proc_pidfdinfo(pid, fd, PROC_PIDFDVNODEPATHINFO,
                                       C.byref(info), C.sizeof(info))
        budget.check()
        # Even an unrelated vanished vnode could have been a second writer.
        _need(count == C.sizeof(info), 'vnode unavailable or changed during read')
        if not info.file.flags & FWRITE:
            continue
        raw = C.string_at(C.addressof(info) + VNodeFD.vnode.offset + VNodePath.path.offset, MAX_PATH)
        end = raw.find(b'\0')
        _need(end > 0, 'empty or unterminated writable vnode path')
        result.append({'f': str(fd), 'a': 'u' if info.file.flags & FREAD else 'w',
                       'i': str(info.vnode.info.stat.ino), 'n': os.fsdecode(raw[:end])})
    _need(_list(library, pid) == before, 'descriptor list changed during read')
    budget.check()
    return result

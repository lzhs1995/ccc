"""Fence pre-lease entries during a retained-controller guard handoff.

Old entries must publish a new immutable claim before serving any child.
Temporarily removing directory write permission closes that publication path
for same-UID, non-root entries; already-published live/unknown claims refuse
the handoff. This changes no claim, input, process or provider record.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import NamedTuple


class MissingHostProcess(NamedTuple):
    """Explicit ESRCH for the queried PID, never an unavailable birth lookup."""
    pid: int


def native_birth_observer(scope):
    """Use the pinned native structure, preserving errno for this local read.

    The shared scope.birth contract intentionally returns None on any failure.
    Do not alter it or infer absence from it. This maintenance-only observer
    returns an explicit missing-PID result only for a fresh proc_pidinfo ESRCH.
    No signals, input, enumeration or shared-reader modifications are involved.
    """
    try:
        pidinfo = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True).proc_pidinfo
        pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                           ctypes.c_void_p, ctypes.c_int]
        pidinfo.restype = ctypes.c_int
    except (OSError, AttributeError):
        return lambda pid: None

    def lookup(pid):
        if type(pid) is not int or not 1 < pid < 2**31:
            return None
        try:
            info = scope.native._BsdInfo()
            size = ctypes.sizeof(info)
            ctypes.set_errno(0)
            result = pidinfo(pid, 3, 0, ctypes.byref(info), size)
            error = ctypes.get_errno()
            if result == 0 and error == errno.ESRCH:
                return MissingHostProcess(pid)
            if (result != size or info.pid != pid or info.status == 5
                    or info.start_sec <= 0 or not 0 <= info.start_usec < 1000000):
                return None
            return [info.start_sec, info.start_usec]
        except (OSError, AttributeError, TypeError, ValueError):
            return None
    return lookup


class FrozenHostClaims:
    NAME = 'original-child-hosts'

    def __init__(self, state, state_identity):
        self.state = Path(state)
        self.expected_state = list(state_identity)
        self.parent = None
        self.fd = None
        self.identity = None
        self.original_mode = None
        self.frozen = False

    @staticmethod
    def need(condition, message):
        if not condition: raise ValueError(message)

    def check(self):
        parent = os.fstat(self.parent)
        parent_path = self.state.lstat()
        self.need(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid()
                  and not parent.st_mode & 0o022
                  and [parent.st_dev,parent.st_ino] == self.expected_state
                  and (parent_path.st_dev,parent_path.st_ino) == (parent.st_dev,parent.st_ino),
                  'claim_freeze_state_changed')
        current = os.fstat(self.fd)
        path = os.stat(self.NAME,dir_fd=self.parent,follow_symlinks=False)
        self.need(stat.S_ISDIR(current.st_mode) and current.st_uid == os.getuid()
                  and [current.st_dev,current.st_ino] == self.identity
                  and (path.st_dev,path.st_ino) == (current.st_dev,current.st_ino)
                  and stat.S_IMODE(current.st_mode) == (0o500 if self.frozen else 0o700),
                  'claim_freeze_registry_changed')

    def __enter__(self):
        # Root bypasses DAC permissions; it cannot use this publication fence.
        self.need(os.geteuid() == os.getuid() and os.getuid() != 0, 'claim_freeze_requires_unprivileged_owner')
        self.need(self.state.is_absolute(), 'claim_freeze_absolute_state_required')
        try:
            self.parent = os.open(self.state,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            parent = os.fstat(self.parent)
            self.need([parent.st_dev,parent.st_ino] == self.expected_state
                      and parent.st_uid == os.getuid() and not parent.st_mode & 0o022,
                      'claim_freeze_state_changed')
            try: os.mkdir(self.NAME,0o700,dir_fd=self.parent)
            except FileExistsError: pass
            self.fd = os.open(self.NAME,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self.parent)
            current = os.fstat(self.fd)
            self.identity = [current.st_dev,current.st_ino]
            self.original_mode = stat.S_IMODE(current.st_mode)
            self.check()
            os.fchmod(self.fd,0o500)
            self.frozen = True
            os.fsync(self.fd)
            self.check()
            return self
        except BaseException:
            self.close()
            raise

    def assert_no_live_hosts(self, birth_lookup):
        self.check()
        names = sorted(os.listdir(self.fd))
        self.need(len(names) <= 128, 'claim_freeze_registry_over_capacity')
        for name in names:
            self.need(len(name) == 69 and name.endswith('.json')
                      and all(c in '0123456789abcdef' for c in name[:-5]),
                      'claim_freeze_unknown_or_unfinished_claim')
            fd = os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self.fd)
            try:
                before = os.fstat(fd)
                self.need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                          and before.st_nlink == 1 and not before.st_mode & 0o077
                          and 0 < before.st_size <= 131072, 'claim_freeze_unsafe_claim')
                raw = b''
                while len(raw) <= 131072:
                    part = os.read(fd,min(4096,131073-len(raw)))
                    if not part: break
                    raw += part
                after = os.fstat(fd)
                at_path = os.stat(name,dir_fd=self.fd,follow_symlinks=False)
                key = lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid,s.st_nlink)
                self.need(key(before) == key(after) == key(at_path) and len(raw) == before.st_size,
                          'claim_freeze_claim_changed')
            finally: os.close(fd)
            claim = json.loads(raw)
            self.need(claim.get('schema') == 'ccc-original-natural-host-claim-v1'
                      and claim.get('original_state_identity') == self.expected_state,
                      'claim_freeze_claim_schema_unknown')
            host = claim['host_identity']
            canonical = json.dumps(host,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
            self.need(hashlib.sha256(canonical).hexdigest() + '.json' == name,
                      'claim_freeze_claim_identity_changed')
            pid,birth = host['host_pid'],host['host_birth']
            self.need(type(pid) is int and 1 < pid < 2**31 and isinstance(birth,list) and len(birth) == 2
                      and all(type(n) is int for n in birth) and birth[0] > 0
                      and 0 <= birth[1] < 1000000, 'claim_freeze_birth_unknown')
            actual = birth_lookup(pid)
            if isinstance(actual, MissingHostProcess):
                self.need(type(actual.pid) is int and actual.pid == pid,
                          'claim_freeze_native_lookup_unknown')
            else:
                self.need(isinstance(actual,list) and len(actual) == 2
                          and all(type(n) is int for n in actual) and actual[0] > 0
                          and 0 <= actual[1] < 1000000, 'claim_freeze_native_lookup_unknown')
                self.need(actual != birth, 'claim_freeze_live_original_host')
            self.check()
        self.need(sorted(os.listdir(self.fd)) == names, 'claim_freeze_registry_changed_during_scan')
        self.check()
        return True

    def close(self):
        try:
            if self.frozen:
                # If another actor changes the path/mode, preserve that conflict
                # rather than chmod a replacement or claim successful recovery.
                self.check()
                os.fchmod(self.fd,self.original_mode)
                self.frozen = False
                os.fsync(self.fd)
        finally:
            if self.fd is not None: os.close(self.fd); self.fd = None
            if self.parent is not None: os.close(self.parent); self.parent = None

    def __exit__(self,*error): self.close()


def run_original_handoff(descriptor, *, native_birth, original_check, original_apply):
    """Wrap the retained apply operation; do not replace its own validations."""
    from ccc_host_lifetime_gate import HostLifetimeGate
    if not all(callable(f) for f in (native_birth,original_check,original_apply)):
        raise ValueError('original_handoff_callbacks_required')
    with HostLifetimeGate(descriptor,exclusive=True) as lease:
        with FrozenHostClaims(descriptor['state'],descriptor['state_identity']) as claims:
            original_check()
            claims.assert_no_live_hosts(native_birth)
            lease.check();claims.check()
            result=original_apply()
            lease.check();claims.check()
            return result

"""Keep the original guard while one of its terminal hosts remains alive.

The installer establishes and pins the file before it is used. Hosts take a
shared kernel flock before publishing or launching; the retained controller
takes an exclusive flock across its complete handoff. This never transfers
host ownership, edits a ledger, waits indefinitely, or restarts a client.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat


class HostLifetimeGate:
    def __init__(self, descriptor, *, exclusive=False):
        self.spec = descriptor
        self.exclusive = exclusive
        self.parent_fd = None
        self.fd = None
        self.pid = os.getpid()

    @staticmethod
    def need(condition, message):
        if not condition:
            raise ValueError(message)

    def check(self):
        self.need(self.pid == os.getpid(), 'lifetime_gate_used_by_other_process')
        self.need(self.fd is not None and self.parent_fd is not None, 'lifetime_gate_not_acquired')
        parent = os.fstat(self.parent_fd)
        parent_path = os.stat(self.spec['state'], follow_symlinks=False)
        self.need(stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid()
                  and not parent.st_mode & 0o022
                  and [parent.st_dev, parent.st_ino] == self.spec['state_identity']
                  and (parent_path.st_dev, parent_path.st_ino) == (parent.st_dev, parent.st_ino),
                  'lifetime_gate_state_replaced')
        info = os.fstat(self.fd)
        at_path = os.stat(self.spec['filename'], dir_fd=self.parent_fd, follow_symlinks=False)
        self.need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                  and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                  and [info.st_dev, info.st_ino] == self.spec['lock_identity']
                  and (at_path.st_dev, at_path.st_ino) == (info.st_dev, info.st_ino),
                  'lifetime_gate_lock_replaced_or_unsafe')

    def acquire(self):
        self.need(self.fd is None and self.parent_fd is None, 'lifetime_gate_already_acquired')
        self.need(self.spec.get('schema') == 'ccc-original-host-lifetime-lock-v1', 'lifetime_gate_schema_invalid')
        self.need(Path(self.spec['state']).is_absolute()
                  and self.spec['filename'] == 'original-host-lifetime.lock', 'lifetime_gate_path_invalid')
        try:
            self.parent_fd = os.open(self.spec['state'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.fd = os.open(self.spec['filename'], os.O_RDWR | os.O_NOFOLLOW,
                              dir_fd=self.parent_fd)
            os.set_inheritable(self.fd, False)
            self.check()
            fcntl.flock(self.fd, (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            self.check()
            # fork inherits a reference to the same flock. Close, never unlock,
            # in the business child so it cannot outlive the owning host lease.
            os.register_at_fork(after_in_child=self.close)
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        # No LOCK_UN: on a forked descriptor that would unlock the parent's lease.
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.parent_fd is not None:
            os.close(self.parent_fd)
            self.parent_fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *error):
        self.close()


@contextmanager
def original_guard_handoff(descriptor, *, original_check, legacy_host_check):
    """Call only around the original controller, never a replacement installer.

    legacy_host_check must fail for live pre-gate hosts and UNKNOWN claims.
    First adoption still requires fencing old entrypoints; this lease alone
    cannot block an older entry which never acquired it.
    """
    if not callable(original_check) or not callable(legacy_host_check):
        raise ValueError('original_handoff_checks_required')
    with HostLifetimeGate(descriptor, exclusive=True) as lease:
        original_check()
        if legacy_host_check() is not True:
            raise ValueError('pre_gate_host_absence_not_proven')
        lease.check()
        yield lease.check
        lease.check()

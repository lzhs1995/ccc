"""The existing deliveries-v1 pane/target mutex, also used by probe writers."""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import os
from pathlib import Path
import stat

from cmux_evidence_io import open_regular_lock


class InputMutexError(RuntimeError):
    pass


def lock_paths(identity):
    values = [identity.get(k) for k in
              ('workspace_uuid', 'target_pane_uuid', 'target_surface_uuid')]
    if not all(isinstance(v, str) and v for v in values):
        raise InputMutexError('INPUT_MUTEX_IDENTITY_REQUIRED')
    workspace, pane, surface = values
    root = Path.home() / '.local/state/multi-agent-collaboration/deliveries-v1'
    digest = lambda s: hashlib.sha256(s.encode()).hexdigest()
    return (root / ('pane-' + digest(workspace + ':' + pane) + '.lock'),
            root / ('target-' + digest(surface) + '.lock'))


@contextmanager
def hold(identity):
    """No wait, no input, no daemon changes. Keep the original lock inodes."""
    pins = []
    with ExitStack() as stack:
        for path in lock_paths(identity):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            handle = stack.enter_context(open_regular_lock(path))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InputMutexError('INPUT_CHANNEL_BUSY: zero input; retry at idle boundary') from exc
            st = os.fstat(handle.fileno())
            pins.append((path, st.st_dev, st.st_ino))

        def check():
            for path, device, inode in pins:
                st = path.lstat()
                if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (device, inode):
                    raise InputMutexError('INPUT_MUTEX_REPLACED: preserve draft; no further keys')

        check()
        yield check


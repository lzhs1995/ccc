"""Durable intent files, events and independent compact deadlines. No live I/O at import."""

import hashlib
from contextlib import contextmanager
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path


@contextmanager
def owned_file(path):
    """Open an owned regular file without following any ancestor symlink.

    Keep directory descriptors through the read and verify their path entries
    again afterwards. A same-named replacement is not the original evidence.
    Callers resolve the platform /tmp alias at their explicit trust boundary.
    """
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('evidence path must be absolute without parent traversal')
    if sys.platform == 'darwin' and path.parts[1:2] in [('tmp',), ('var',), ('etc',)]:
        # Only Apple's root-owned platform aliases, never a caller's symlink.
        path = Path('/private') / path.relative_to('/')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fds = [os.open(path.anchor, flags | os.O_DIRECTORY)]
    links = []
    try:
        for part in path.parts[1:-1]:
            parent = fds[-1]
            fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            fds.append(fd)
            st = os.fstat(fd)
            links.append((parent, part, st.st_dev, st.st_ino))
        fd = os.open(path.name, flags, dir_fd=fds[-1])
        with os.fdopen(fd, 'rb') as stream:
            st = os.fstat(stream.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                    or st.st_mode & 0o022 or st.st_nlink != 1):
                raise ValueError('untrusted evidence file')
            yield stream, st
            after = os.stat(path.name, dir_fd=fds[-1], follow_symlinks=False)
            if (after.st_dev, after.st_ino) != (st.st_dev, st.st_ino):
                raise ValueError('evidence file replaced during read')
            for parent, part, dev, ino in links:
                current = os.stat(part, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (dev, ino):
                    raise ValueError('evidence ancestor replaced during read')
    finally:
        for fd in reversed(fds):
            os.close(fd)


def sync_directory(path):
    """Make creation/rename durable before a control operation is permitted."""
    fd = os.open(Path(path), os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class OwnedDirectory:
    """Retain the journal directory and verify every ancestor before writes."""

    def __init__(self, path):
        self.path = Path(path)
        self.fds = []
        self.links = []
        if (not self.path.is_absolute() or '..' in self.path.parts
                or self.path.resolve(strict=True) != self.path):
            raise ValueError('journal directory must be an absolute canonical path')
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            self.fds.append(os.open('/', flags))
            self._trusted(os.fstat(self.fds[0]))
            for name in self.path.parts[1:]:
                parent = self.fds[-1]
                fd = os.open(name, flags, dir_fd=parent)
                self.fds.append(fd)
                info = os.fstat(fd)
                self._trusted(info)
                self.links.append((parent, name, info.st_dev, info.st_ino))
            if os.fstat(self.fds[-1]).st_uid != os.geteuid():
                raise ValueError('journal directory must belong to the current user')
            self.verify()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _trusted(info):
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or info.st_mode & 0o022):
            raise ValueError('untrusted journal ancestor')

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or name in ('', '.', '..') or '/' in name or '\x00' in name:
            raise ValueError('invalid journal basename')
        return name

    def verify(self):
        if not self.fds:
            raise ValueError('journal directory is closed')
        for parent, name, device, inode in self.links:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            self._trusted(info)
            if (info.st_dev, info.st_ino) != (device, inode):
                raise ValueError('journal ancestor replaced')

    def write_once(self, name, value):
        name = self._name(name)
        raw = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        self.verify()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fds[-1])
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(self.fds[-1])
        # A failed recheck consumes the original filename; never unlink or refund it.
        self.verify()
        return hashlib.sha256(raw).hexdigest()

    def atomic_json(self, name, value):
        import secrets
        name = self._name(name)
        temporary = '.' + name + '-' + secrets.token_hex(12) + '.tmp'
        self.write_once(temporary, value)
        self.verify()
        os.rename(temporary, name, src_dir_fd=self.fds[-1], dst_dir_fd=self.fds[-1])
        os.fsync(self.fds[-1])
        self.verify()

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def write_once(path, value):
    path = Path(path)
    with OwnedDirectory(path.parent) as directory:
        return directory.write_once(path.name, value)

class Journal:
    def __init__(self, path, *, directory=None):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.directory = directory

    def record(self, event, **fields):
        value = dict(event=event, epoch=time.time(), **fields)
        raw = json.dumps(value, ensure_ascii=False) + '\n'
        with self.lock:
            owned = self.directory or OwnedDirectory(self.path.parent)
            try:
                owned.verify()
                fd = os.open(owned._name(self.path.name),
                             os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                             0o600, dir_fd=owned.fds[-1])
                with os.fdopen(fd, 'w') as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                            or info.st_mode & 0o022 or info.st_nlink != 1):
                        raise ValueError('untrusted event journal')
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                owned.verify()
            finally:
                if self.directory is None:
                    owned.close()
        print(raw, end='', flush=True)
        return value

class Deadline:
    """Separate monotonic timer; slow warning I/O cannot delay deadline entry.

    This is a recovery *entry* deadline, not a claim about upstream completion.
    callbacks execute in separate threads. They must journal before any control.
    """
    def __init__(self, callback, *, warn=180.0, limit=300.0):
        if not 0 < warn < limit:
            raise ValueError('require 0 < warning < deadline')
        self.warn, self.limit = warn, limit
        self.callback = callback
        self.started = time.monotonic()
        self.stop = threading.Event()
        self.expired = threading.Event()
        self.deadline_done = threading.Event()
        self.events = []
        self.callbacks = []
        self.callback_errors = []
        self.worker = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.worker.start()
        return self

    def _run(self):
        for delay, name in ((self.warn, 'WARNING_180S'), (self.limit, 'DEADLINE_300S')):
            if self.stop.wait(max(0, self.started + delay - time.monotonic())):
                return
            elapsed = time.monotonic() - self.started
            if name == 'DEADLINE_300S':
                self.expired.set()
            self.events.append((name, elapsed))
            # The control callback must survive the main observer reaching its
            # deadline. Warning delivery may be abandoned; a mutation may not.
            worker = threading.Thread(target=self._callback, args=(name, elapsed),
                                      name=name, daemon=name != 'DEADLINE_300S')
            self.callbacks.append(worker)
            worker.start()

    def _callback(self, name, elapsed):
        try:
            self.callback(name, elapsed)
        except BaseException as exc:
            self.callback_errors.append((name, type(exc).__name__, str(exc)))
        finally:
            if name == 'DEADLINE_300S':
                self.deadline_done.set()

    def close(self, control_timeout=20):
        self.stop.set()
        self.worker.join(timeout=1)
        if self.expired.is_set():
            # Return a truthful status if bounded control is still in flight;
            # its non-daemon worker keeps the process alive until it finishes.
            return self.deadline_done.wait(control_timeout)
        return True

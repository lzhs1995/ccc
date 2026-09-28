"""Pin a declared effective launch inventory for a single standby generation.

The launch adapter supplies the complete dependency graph. This module checks
that graph, not Codex configuration precedence; it cannot certify omitted
skills/config sources or native readiness. No file contents or env values are
written into receipts. A detected change permanently invalidates this object.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import stat
import threading


SCOPES = {'runtime', 'native_binary', 'codex_config', 'profile', 'skills', 'rules'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


def _stamp(info):
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns]


def _ancestor_stamp(info):
    # Unrelated children of /Users, HOME or CODEX_HOME are not configuration.
    # Preserve path/type/owner/mode, without treating a sibling creation as a
    # source change. Declared dependency directories still track all children.
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid]


class StandbyGeneration:
    """A callback suitable for launch_registered(generation_current=pin.current).

    roots are absolute paths in all required named scopes. Missing optional
    files are tracked too, so creating them invalidates the prepared cohort.
    effective() includes resolved profile/settings, relevant environment and
    the adapter's config-layer/source inventory. It must exclude volatile job
    progress, which is checked separately by the live authorization guard.
    """
    def __init__(self, roots, effective, *, max_entries=50000, max_read_bytes=512 * 1024**2,
                 use_events=False, max_watch_files=4096):
        if set(roots) != SCOPES or any(not paths for paths in roots.values()):
            raise ValueError('all effective launch dependency scopes are required')
        self.roots = {key: tuple(sorted(str(Path(p)) for p in paths)) for key, paths in roots.items()}
        if any(not Path(p).is_absolute() or '..' in Path(p).parts
               for paths in self.roots.values() for p in paths):
            raise ValueError('absolute dependency paths required')
        if not callable(effective) or max_entries < 1 or max_read_bytes < 1:
            raise ValueError('invalid generation reader or bounds')
        self.effective = effective
        self.max_entries, self.max_read_bytes = max_entries, max_read_bytes
        self._cache = {}
        self._lock = threading.RLock()
        self._invalid = False
        self._watch = None
        with self._lock:
            first = self._snapshot()
            if self._snapshot() != first:
                self._invalid = True
                raise ValueError('launch dependencies changed during generation capture')
            self.value = first
            self._effective_sha = _digest(self.effective())
            self._roots_sha = _digest(self.roots)
            if use_events:
                try:
                    self._watch = _VnodeWatch(self._inventory_paths, max_watch_files)
                    # Register first, rescan every dependency, then drain.
                    # Changes in the registration window cannot disappear.
                    if self._snapshot() != self.value:
                        raise ValueError('dependencies changed while arming watches')
                    self._watch.check()
                except Exception:
                    self.close()
                    raise

    def _snapshot(self):
        effective = self.effective()
        if not isinstance(effective, dict) or not effective:
            raise ValueError('effective settings are unavailable')
        effective_sha = _digest(effective)
        rows, anchors, active, seen = {}, {}, set(), set()
        read_bytes = 0

        def visit(path):
            nonlocal read_bytes
            key = str(path)
            if key in active:
                raise ValueError('cyclic launch dependency')
            if key in seen:
                return
            seen.add(key)
            if len(seen) > self.max_entries:
                raise ValueError('launch dependency inventory too large')
            # Track every ancestor symlink as well as its resolved target.
            # A root beneath a replaced symlink is not the original graph.
            for parent in reversed(path.parents):
                if str(parent) not in anchors:
                    try:
                        anchors[str(parent)] = _ancestor_stamp(parent.lstat())
                    except FileNotFoundError:
                        anchors[str(parent)] = ['missing']
                if parent.is_symlink():
                    visit(parent)
            try:
                before = path.lstat()
            except FileNotFoundError:
                rows[key] = ['missing']
                return
            identity = _stamp(before)
            active.add(key)
            try:
                if stat.S_ISLNK(before.st_mode):
                    target = os.readlink(path)
                    resolved = path.resolve(strict=True)
                    rows[key] = ['link', identity, target, str(resolved)]
                    visit(resolved)
                elif stat.S_ISDIR(before.st_mode):
                    children = sorted(path.iterdir())
                    rows[key] = ['directory', identity, [p.name for p in children]]
                    for child in children:
                        visit(child)
                elif stat.S_ISREG(before.st_mode):
                    cached = self._cache.get(key)
                    if cached is not None and cached[0] == identity:
                        content_sha = cached[1]
                    else:
                        read_bytes += before.st_size
                        if read_bytes > self.max_read_bytes:
                            raise ValueError('launch dependency read budget exceeded')
                        with path.open('rb') as stream:
                            if _stamp(os.fstat(stream.fileno())) != identity:
                                raise ValueError('dependency replaced before reading')
                            content = hashlib.sha256()
                            consumed = 0
                            while chunk := stream.read(1024 * 1024):
                                consumed += len(chunk)
                                if consumed > before.st_size:
                                    raise ValueError('dependency grew beyond captured size')
                                content.update(chunk)
                            if _stamp(os.fstat(stream.fileno())) != identity:
                                raise ValueError('dependency changed while reading')
                        content_sha = content.hexdigest()
                        self._cache[key] = (identity, content_sha)
                    rows[key] = ['file', identity, content_sha]
                else:
                    raise ValueError('unsupported launch dependency file type')
                if _stamp(path.lstat()) != identity:
                    raise ValueError('dependency changed during scan')
            finally:
                active.remove(key)

        for paths in self.roots.values():
            for path in paths:
                visit(Path(path))
        if _digest(self.effective()) != effective_sha:
            raise ValueError('effective settings changed during inventory')
        for name, identity in anchors.items():
            if identity == ['missing']:
                if os.path.lexists(name):
                    raise ValueError('launch dependency ancestor appeared')
            elif _ancestor_stamp(Path(name).lstat()) != identity:
                raise ValueError('launch dependency ancestor changed')
        # The callback above can wait or cause a source change. File checks
        # must follow that callback, including each already-read dependency.
        for name, row in rows.items():
            path = Path(name)
            if row[0] == 'missing':
                if os.path.lexists(path):
                    raise ValueError('optional dependency appeared')
            elif _stamp(path.lstat()) != row[1]:
                raise ValueError('dependency changed before snapshot completed')
        self._inventory_paths = {name: 'identity' for name in anchors}
        for name, row in rows.items():
            if row[0] != 'missing':
                self._inventory_paths[name] = 'content'
            else:
                # A missing source has no vnode. Any child change on its
                # nearest existing ancestor conservatively invalidates it.
                parent = Path(name).parent
                while not os.path.lexists(parent):
                    parent = parent.parent
                self._inventory_paths[str(parent)] = 'content'
        return _digest({'roots': self.roots, 'effective': effective_sha, 'entries': rows,
                        'ancestors': anchors})

    def current(self):
        with self._lock:
            if self._invalid:
                raise ValueError('standby configuration generation permanently invalidated')
            try:
                if self._watch is None:
                    if self._snapshot() != self.value:
                        raise ValueError('standby effective configuration changed')
                else:
                    self._watch.check()
                    if (_digest(self.effective()) != self._effective_sha
                            or _digest(self.roots) != self._roots_sha):
                        raise ValueError('standby effective settings changed')
                    self._watch.check()  # include changes inside callbacks
            except Exception:
                self._invalid = True
                raise
            return self.value

    def close(self):
        with self._lock:
            self._invalid = True
            if self._watch is not None:
                self._watch.close()
            self._cache.clear()


class _VnodeWatch:
    """Darwin event latch covering leaves, symlinks and every ancestor.

    Any event or query failure invalidates permanently. We never clear an
    event and accept a subsequent empty queue, or reuse the old vnode after
    replacement. Registration/descriptor limits fail closed, with no fallback.
    """
    def __init__(self, paths, limit):
        if not hasattr(select, 'kqueue') or not hasattr(os, 'O_SYMLINK'):
            raise ValueError('native dependency events unavailable')
        existing = [(Path(path), kind) for path, kind in paths.items() if os.path.lexists(path)]
        if type(limit) is not int or not 1 <= limit <= 8192 or len(existing) > limit:
            raise ValueError('dependency watch descriptor limit exceeded')
        self._fds = []
        self._queue = select.kqueue()
        self._invalid = False
        self._pid = os.getpid()
        try:
            identity_notes = (select.KQ_NOTE_DELETE |
                     select.KQ_NOTE_ATTRIB | select.KQ_NOTE_LINK | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE)
            for path, kind in existing:
                notes = identity_notes
                if kind == 'content':
                    notes |= select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                fd = os.open(path, os.O_EVTONLY | os.O_SYMLINK | os.O_CLOEXEC)
                self._fds.append(fd)
                event = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR, fflags=notes)
                self._queue.control([event], 0, 0)
        except Exception:
            self.close()
            raise

    def check(self):
        if self._invalid or self._pid != os.getpid():
            raise ValueError('dependency event watcher invalidated or inherited')
        try:
            if self._queue.control([], 1, 0):
                raise ValueError('native dependency event observed')
        except Exception:
            self._invalid = True
            raise

    def close(self):
        self._invalid = True
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        self._queue.close()

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
import resource
import select
import stat
import threading


SCOPES = {'runtime', 'native_binary', 'codex_config', 'profile', 'skills', 'rules'}
# The production source census includes the native binary, complete host skills
# and plugin caches. Keep the small standalone defaults; opt production into
# these explicit ceilings without changing any user's declared source graph.
PRODUCTION_BOUNDS = {'use_events': True, 'max_entries': 50000,
                     'max_read_bytes': 1536 * 1024**2, 'max_watch_files': 40000}


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
    if stat.S_ISLNK(info.st_mode):
        return _stamp(info)
    return [info.st_dev, info.st_ino, info.st_mode, info.st_uid]


def _access_stable_stamp(info):
    # Darwin exec/read can emit NOTE_ATTRIB for access time alone. Keep all
    # mutation timestamps and permission/ownership/link metadata in this
    # exception's comparison; a chmod/content round trip changes ctime.
    return [*_stamp(info), info.st_gid, info.st_nlink, info.st_flags]


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
        self._configure(roots, effective, max_entries, max_read_bytes)
        self._lock = threading.RLock()
        self._invalid = False
        self._watch = None
        with self._lock:
            first = self._snapshot()
            if self._snapshot() != first:
                self._invalid = True
                raise ValueError('launch dependencies changed during generation capture')
            self.value = first
            # The fast path must compare against the effective settings that
            # produced value, never an independent callback between snapshots.
            self._effective_sha = self._snapshot_effective_sha
            self._roots_sha = _digest(self.roots)
            if use_events:
                try:
                    self._watch = _VnodeWatch(self._inventory_paths, max_watch_files,
                                              self._missing_children)
                    # Register first, rescan every dependency, then drain.
                    # Changes in the registration window cannot disappear.
                    if self._snapshot() != self.value:
                        raise ValueError('dependencies changed while arming watches')
                    self._watch.check()
                except BaseException:
                    self.close()
                    raise

    def _configure(self, roots, effective, max_entries, max_read_bytes):
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
        self.read_bytes_total = 0

    @classmethod
    def measure(cls, roots, *, max_entries=50000):
        """Metadata-only capacity report using the identical dependency walk.

        This does not create a generation, arm events, read contents or certify
        readiness. No exclusions or source filtering are applied.
        """
        probe = cls.__new__(cls)
        probe._configure(roots, lambda: {'capacity_only': True}, max_entries, 1)
        probe._snapshot(read_contents=False)
        return probe.capacity

    def _snapshot(self, *, read_contents=True):
        effective = self.effective()
        if not isinstance(effective, dict) or not effective:
            raise ValueError('effective settings are unavailable')
        effective_sha = _digest(effective)
        rows, anchors, active, seen = {}, {}, set(), set()
        read_bytes = 0
        file_identities = {}
        root_usage = {}
        current_root = None
        resolving = set()

        def budget_error(message, path, **details):
            error = ValueError(message + ': ' + str(path))
            error.dependency_budget = {'path': str(path), 'root': current_root,
                'entries': len(seen), 'ancestors': len(anchors),
                'read_bytes': read_bytes, 'max_read_bytes': self.max_read_bytes,
                'max_entries': self.max_entries, **details}
            return error

        def anchor_path(path):
            # Ancestor links contribute path identities, not all content below
            # their targets. /tmp -> /private/tmp must not inventory every
            # unrelated temporary directory or special file on the machine.
            # Resolve one component/hop at a time: Path.resolve() alone loses
            # intermediate links in A -> B -> C, leaving B unwatched.
            current = Path(path.anchor)
            for part in ('.', *path.parts[1:]):
                if part == '..':
                    current = current.parent
                elif part != '.':
                    current = current / part
                key = str(current)
                try:
                    info = current.lstat()
                except FileNotFoundError:
                    identity = ['missing']
                    if key in anchors and anchors[key] != identity:
                        raise ValueError('launch ancestor changed while resolving')
                    anchors[key] = identity
                    continue
                identity = _ancestor_stamp(info)
                if key in anchors and anchors[key] != identity:
                    raise ValueError('launch ancestor changed while resolving')
                anchors[key] = identity
                if len(anchors) + len(seen) > self.max_entries:
                    raise budget_error('launch dependency inventory too large', current)
                if stat.S_ISLNK(info.st_mode):
                    if key in resolving or len(resolving) >= 40:
                        raise ValueError('cyclic launch dependency link')
                    resolving.add(key)
                    try:
                        target = Path(os.readlink(current))
                        target = target if target.is_absolute() else current.parent / target
                        resolved = anchor_path(target)
                        if not os.path.lexists(resolved):
                            raise ValueError('unresolved launch dependency link')
                        if _ancestor_stamp(current.lstat()) != identity:
                            raise ValueError('launch link changed while resolving')
                        current = resolved
                    finally:
                        resolving.remove(key)
            return current

        def visit(path):
            nonlocal read_bytes
            key = str(path)
            if key in active:
                raise ValueError('cyclic launch dependency')
            if key in seen:
                return
            seen.add(key)
            if len(seen) > self.max_entries:
                raise budget_error('launch dependency inventory too large', path)
            # Track every ancestor symlink as well as its resolved target.
            # A root beneath a replaced symlink is not the original graph.
            anchor_path(path.parent)
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
                    resolved = anchor_path(path)
                    rows[key] = ['link', identity, target, str(resolved)]
                    visit(resolved)
                elif stat.S_ISDIR(before.st_mode):
                    children = sorted(path.iterdir())
                    rows[key] = ['directory', identity, [p.name for p in children]]
                    for child in children:
                        visit(child)
                elif stat.S_ISREG(before.st_mode):
                    # Share content only for the same inode AND full stamp.
                    # Every path, ancestor and intermediate symlink stays in
                    # the inventory and receives the usual final identity check.
                    inode = (before.st_dev, before.st_ino)
                    if inode not in file_identities:
                        file_identities[inode] = before.st_size
                        root_usage[current_root] = root_usage.get(current_root, 0) + before.st_size
                    cached = self._cache.get(inode)
                    if not read_contents:
                        content_sha = None
                    elif cached is not None and cached[0] == identity:
                        content_sha = cached[1]
                    else:
                        read_bytes += before.st_size
                        if read_bytes > self.max_read_bytes:
                            raise budget_error('launch dependency read budget exceeded', path,
                                               file_bytes=before.st_size)
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
                        self._cache[inode] = (identity, content_sha)
                    rows[key] = ['file', identity, content_sha]
                else:
                    raise ValueError('unsupported launch dependency file type')
                if _stamp(path.lstat()) != identity:
                    raise ValueError('dependency changed during scan')
            finally:
                active.remove(key)

        for scope, paths in sorted(self.roots.items()):
            for path in paths:
                current_root = path
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
        self._missing_children = {}
        for name, row in rows.items():
            if row[0] != 'missing':
                self._inventory_paths[name] = 'content'
            else:
                # A missing source has no vnode. Watch its nearest existing
                # parent, then check the specific missing child on a directory
                # event. Job/log siblings are not configuration dependencies.
                parent = Path(name).parent
                while not os.path.lexists(parent):
                    parent = parent.parent
                child = parent / Path(name).relative_to(parent).parts[0]
                self._missing_children.setdefault(str(parent), set()).add(str(child))
        for parent in self._missing_children:
            if self._inventory_paths.get(parent) != 'content':
                self._inventory_paths[parent] = 'missing'
        existing = [Path(p).lstat() for p in self._inventory_paths if os.path.lexists(p)]
        self.read_bytes_total += read_bytes
        self.capacity = {'entries': len(rows), 'ancestors': len(anchors),
            'file_inodes': len(file_identities), 'unique_file_bytes': sum(file_identities.values()),
            'root_unique_bytes': root_usage, 'read_bytes_total': self.read_bytes_total,
            'watch_paths': len(existing),
            'watch_inodes': len({(s.st_dev, s.st_ino) for s in existing}),
            'rlimit_nofile': list(resource.getrlimit(resource.RLIMIT_NOFILE))}
        self._snapshot_effective_sha = effective_sha
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

    Dependency content/identity changes and query failures latch permanently.
    Directory notifications carry no child name: optional missing paths are
    checked on those events. This proves current absence, not that a missing
    file could never have appeared and vanished between checks. Existing
    dependency vnodes still detect content round trips. No replaced vnode is
    reused. Registration/descriptor limits fail closed, with no fallback.
    """
    def __init__(self, paths, limit, missing_children=None):
        if not hasattr(select, 'kqueue') or not hasattr(os, 'O_SYMLINK'):
            raise ValueError('native dependency events unavailable')
        if type(limit) is not int or not 1 <= limit <= 50000:
            raise ValueError('dependency watch descriptor limit exceeded')
        # One vnode descriptor can cover multiple paths to that same inode.
        # Keep every alias and its strongest requested event mask: resolving
        # paths here would silently lose intermediate symlink identities.
        groups = {}
        for name, kind in paths.items():
            path = Path(name)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if kind not in ('content', 'identity', 'missing'):
                raise ValueError('unknown dependency watch kind')
            groups.setdefault((info.st_dev, info.st_ino), []).append(
                (path, kind, _ancestor_stamp(info), stat.S_ISDIR(info.st_mode)))
        soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        # Leave space for controller/routes/worker IO. Existing unrelated FDs
        # can still exhaust the process/kernel budget; open failure below
        # closes everything already acquired, without changing OS limits.
        if (len(groups) > limit or (soft_limit != resource.RLIM_INFINITY
                                  and len(groups) + 1024 > soft_limit)):
            error = ValueError('dependency watch descriptor limit exceeded')
            error.dependency_budget = {'watch_paths': sum(map(len, groups.values())),
                'watch_inodes': len(groups), 'max_watch_files': limit,
                'rlimit_nofile': soft_limit, 'reserved_descriptors': 1024}
            raise error
        self._fds = []
        self._entries = {}
        self._access_stamps = {}
        self._missing = missing_children or {}
        self._queue = select.kqueue()
        self._invalid = False
        self.failure_diagnostic = None
        self._pid = os.getpid()
        try:
            identity_notes = (select.KQ_NOTE_DELETE |
                     select.KQ_NOTE_ATTRIB | select.KQ_NOTE_LINK | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE)
            for aliases in groups.values():
                notes = identity_notes
                if any(kind in ('content', 'missing') for _, kind, _, _ in aliases):
                    notes |= select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                path = aliases[0][0]
                fd = os.open(path, os.O_EVTONLY | os.O_SYMLINK | os.O_CLOEXEC)
                self._fds.append(fd)
                info = os.fstat(fd)
                identity = _ancestor_stamp(info)
                for alias, _, expected, _ in aliases:
                    if identity != expected or _ancestor_stamp(alias.lstat()) != expected:
                        raise ValueError('dependency changed during watch registration')
                self._entries[fd] = aliases
                if stat.S_ISREG(info.st_mode):
                    self._access_stamps[fd] = (_access_stable_stamp(info), info.st_atime_ns)
                event = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR, fflags=notes)
                self._queue.control([event], 0, 0)
        except BaseException:
            self.close()
            raise

    def check(self):
        if self._invalid or self._pid != os.getpid():
            raise ValueError('dependency event watcher invalidated or inherited')
        try:
            # EV_CLEAR coalesces each registered vnode into one pending event.
            # Read enough entries for the whole registered set in one syscall.
            # Requiring an empty queue after validation lets unrelated sibling
            # writes starve a valid generation indefinitely. Events arriving
            # after this snapshot remain latched for the next boundary check.
            events = self._queue.control([], max(1, len(self._fds)), 0)
            for event in events:
                entry = self._entries.get(event.ident)
                if (entry is None or event.filter != select.KQ_FILTER_VNODE
                        or event.flags & (select.KQ_EV_ERROR | select.KQ_EV_EOF)):
                    raise ValueError('native dependency event source failed')
                child_notes = select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_LINK
                fd_info = os.fstat(event.ident)
                fd_identity = _ancestor_stamp(fd_info)
                access = self._access_stamps.get(event.ident)
                if (event.fflags == select.KQ_NOTE_ATTRIB and access is not None
                        and _access_stable_stamp(fd_info) == access[0]):
                    # Every original alias must still identify this exact
                    # unchanged regular file. No WRITE/EXTEND or directory
                    # event is forgiven, even if bytes were restored.
                    # A notification need not expose a new atime: delayed or
                    # repeated ATTRIB can describe the already observed stamp.
                    # Require unchanged mutation timestamps/permissions on all
                    # aliases, rather than requiring access time to advance.
                    infos = [path.lstat() for path, _, _, _ in entry]
                    if all(_access_stable_stamp(info) == access[0]
                           and info.st_atime_ns == fd_info.st_atime_ns for info in infos):
                        self._access_stamps[event.ident] = (access[0], fd_info.st_atime_ns)
                        continue
                for path, kind, identity, directory in entry:
                    if (kind == 'content' or not directory or not event.fflags
                            or event.fflags & ~child_notes
                            or _ancestor_stamp(path.lstat()) != identity
                            or fd_identity != identity):
                        error = ValueError('native dependency event observed')
                        error.dependency_event = {'path': str(path), 'kind': kind,
                            'fflags': event.fflags, 'flags': event.flags,
                            'expected_identity': identity, 'fd_identity': fd_identity,
                            'access_baseline': access,
                            'current_access_stamp': _access_stable_stamp(fd_info),
                            'current_atime_ns': fd_info.st_atime_ns}
                        raise error
                    for child in self._missing.get(str(path), ()):
                        if os.path.lexists(child):
                            raise ValueError('optional dependency path appeared')
        except Exception as exc:
            self._invalid = True
            # Other workers can observe invalidation before the original
            # exception reaches the service. Preserve that first cause even
            # after close, without retaining exception tracebacks or frames.
            self.failure_diagnostic = {'error_type': type(exc).__name__,
                'message': str(exc), 'dependency_event': getattr(exc, 'dependency_event', None)}
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

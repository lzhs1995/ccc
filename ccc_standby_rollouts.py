"""Shared, live rollout-name inventory for untouched native sessions.

Preparation inventories sessions and archived_sessions, including compressed
rollouts. The click path queries kqueue and scans only directories whose names
changed. It never opens historical transcripts or infers a path from UUID/time.
This is current filesystem absence, not a certificate of zero model requests.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import select
import stat
import threading
import uuid


_SESSION = re.compile(r'(?<![0-9a-f])([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
                      r'[0-9a-f]{4}-[0-9a-f]{12})\.jsonl(?:\.zst)?$', re.I)


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid


class RolloutInventory:
    """One in-memory monitor shared by all slots using a native home.

    Canonical roots must come from the actual native environment. All their
    ancestors are pinned. No persistent index, restart reuse or event-error
    fallback is allowed. A previously observed session stays used after delete.
    Directory replacement invalidates this monitor; ordinary unrelated session
    creation updates the index without invalidating the untouched originals.
    """
    def __init__(self, sessions_root, *, max_directories=2048,
                 max_entries=200000, max_update_entries=4096, max_rounds=3):
        if not hasattr(select, 'kqueue') or not hasattr(os, 'O_EVTONLY'):
            raise ValueError('live rollout directory events unavailable')
        root = Path(sessions_root)
        if (not root.is_absolute() or root.name != 'sessions'
                or root.resolve(strict=True) != root):
            raise ValueError('canonical native sessions directory required')
        if any(type(v) is not int or v < 1 for v in
               (max_directories, max_entries, max_update_entries, max_rounds)):
            raise ValueError('positive rollout inventory bounds required')
        self.sessions_root = root
        self.roots = (root, root.parent / 'archived_sessions')
        self.max_directories, self.max_entries = max_directories, max_entries
        self.max_update_entries, self.max_rounds = max_update_entries, max_rounds
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._invalid = False
        self._closed = False
        self._queue = select.kqueue()
        self._fds, self._paths, self._identities = {}, {}, {}
        self._content = set()
        self._sessions = set()
        self._entry_count = 0
        try:
            # Watch the home before checking for a missing archive directory.
            # Ancestor identity events catch rename/symlink replacement even
            # when the original leaf inode remains open through another path.
            for path in reversed((root.parent, *root.parent.parents)):
                self._arm(path, content=path == root.parent)
            for path in self.roots:
                if os.path.lexists(path):
                    self._scan_tree(path, self.max_entries)
            # Watches precede each readdir. Drain all registration-window
            # events before publishing any negative observation.
            self._refresh()
        except BaseException:
            self.close()
            raise

    def _arm(self, path, *, content):
        if path in self._fds:
            return
        if len(self._fds) >= self.max_directories:
            raise ValueError('rollout directory watch bound exceeded')
        before = path.lstat()
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError('rollout ancestry must remain a real directory')
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if _identity(os.fstat(fd)) != _identity(before):
                raise ValueError('rollout directory changed while opening')
            notes = (select.KQ_NOTE_DELETE | select.KQ_NOTE_ATTRIB |
                     select.KQ_NOTE_LINK | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE)
            if content:
                notes |= select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
            event = select.kevent(fd, filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                fflags=notes)
            self._queue.control([event], 0, 0)
            if _identity(path.lstat()) != _identity(before):
                raise ValueError('rollout directory changed while arming')
        except BaseException:
            os.close(fd)
            raise
        self._fds[path], self._paths[fd] = fd, path
        self._identities[path] = _identity(before)
        if content:
            self._content.add(path)

    def _scan_tree(self, root, budget):
        pending = [root]
        scanned = 0
        while pending:
            path = pending.pop()
            self._arm(path, content=True)
            if _identity(path.lstat()) != self._identities[path]:
                raise ValueError('rollout directory replaced')
            # A new open description avoids reusing scandir's directory
            # offset, while dir_fd keeps this read on the admitted inode.
            reader = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                             dir_fd=self._fds[path])
            try:
                with os.scandir(reader) as entries:
                    for entry in entries:
                        scanned += 1
                        self._entry_count += 1
                        if scanned > budget:
                            raise ValueError('rollout inventory update bound exceeded')
                        child = path / entry.name
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            raise ValueError('linked rollout content is unsupported')
                        if stat.S_ISDIR(info.st_mode):
                            if child not in self._fds:
                                pending.append(child)
                        elif match := _SESSION.search(entry.name):
                            # Names alone block negative usage evidence.
                            self._sessions.add(str(uuid.UUID(match.group(1))))
                            if len(self._sessions) + len(self._fds) > self.max_entries:
                                raise ValueError('rollout index entry bound exceeded')
            finally:
                os.close(reader)
            if _identity(path.lstat()) != self._identities[path]:
                raise ValueError('rollout directory changed during inventory')
        return scanned

    def _check_anchors(self):
        # Ancestors and roots are a small fixed set. Changed descendant
        # directory identities are checked only when kqueue reports them.
        for path in (self.sessions_root.parent, *self.sessions_root.parent.parents,
                     *self.roots):
            expected = self._identities.get(path)
            if expected is None:
                if os.path.lexists(path):
                    raise ValueError('rollout root appeared without inventory')
            elif _identity(path.lstat()) != expected:
                raise ValueError('rollout root or ancestor changed')

    def _refresh(self):
        remaining = self.max_update_entries
        for _ in range(self.max_rounds):
            events = self._queue.control([], self.max_directories, 0)
            if not events:
                self._check_anchors()
                # Include filesystem activity during final path checks.
                events = self._queue.control([], self.max_directories, 0)
                if not events:
                    return
            for event in events:
                path = self._paths.get(event.ident)
                if (path is None or event.flags & (select.KQ_EV_ERROR | select.KQ_EV_EOF)
                        or event.filter != select.KQ_FILTER_VNODE):
                    raise ValueError('rollout event source failed')
                if event.fflags & ~(select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_LINK):
                    error = ValueError('rollout directory identity event')
                    error.rollout_event = {'path': str(path), 'fflags': event.fflags,
                        'flags': event.flags, 'filter': event.filter}
                    raise error
                if _identity(path.lstat()) != self._identities[path]:
                    raise ValueError('rollout directory replaced before event read')
                if path not in self._content:
                    # A sibling directory changes the ancestor's link count
                    # (Darwin may co-report WRITE). It changes neither the
                    # pinned path identity nor the declared rollout content.
                    # ATTRIB/RENAME/DELETE still invalidate, including A-B-A.
                    continue
                if path == self.sessions_root.parent:
                    # Native home also holds logs/locks/config. Only the two
                    # declared rollout roots contribute content dependencies.
                    for root in self.roots:
                        if root not in self._fds and os.path.lexists(root):
                            remaining -= self._scan_tree(root, remaining)
                else:
                    remaining -= self._scan_tree(path, remaining)
        raise ValueError('rollout inventory did not settle within update bound')

    def absent(self, sessions_root, session_id):
        with self._lock:
            if self._invalid or self._closed or self._pid != os.getpid():
                raise ValueError('rollout inventory invalidated, closed or inherited')
            try:
                if Path(sessions_root) != self.sessions_root:
                    raise ValueError('rollout inventory belongs to another native home')
                session_id = str(uuid.UUID(session_id))
                self._refresh()
                return session_id not in self._sessions
            except BaseException:
                self._invalid = True
                raise

    def close(self):
        with self._lock:
            self._invalid = True
            if self._closed:
                return
            self._closed = True
            try:
                self._queue.close()
            finally:
                for fd in self._paths:
                    os.close(fd)
                self._paths.clear()
                self._fds.clear()

"""Atomic, ordinary JSON snapshots with bounded writes on clone-capable macOS.

Historical surface records stay in the same JSON object. Whitespace reserves
block-aligned slots so an APFS clone needs writes only for changed records.
The published inode is never edited. Unsupported filesystems use a complete
compact atomic write. Neither path acknowledges persistence before both the
file and its containing directory have been fsynced.
"""
from __future__ import annotations

import contextlib
import copy
import ctypes
import errno
from functools import lru_cache
import json
import os
from pathlib import Path
import stat
import sys
import threading
import uuid


BLOCK_SIZE = 4096
WRITE_BATCH_BYTES = 1024 * 1024
_UNSET = object()


class RuntimeRecords(dict):
    """Keep every fetched runtime eligible for later background publication.

    A worker may hold a record or mutate a nested value after a save, so these
    candidates are never cleared. Untouched startup history remains on disk.
    Bulk mutation through ``items``/``values`` must explicitly call touch_all.
    Removing a key requires one full publication to remove it from the file.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._candidate_lock = threading.Lock()
        self._candidates = set()
        self._removals = 0

    def touch(self, key):
        with self._candidate_lock:
            self._candidates.add(key)

    def touch_all(self):
        with self._candidate_lock:
            self._candidates.update(tuple(super().keys()))

    def publication(self):
        with self._candidate_lock:
            return self._removals, tuple(self._candidates)

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.touch(key)
        return value

    def get(self, key, default=None):
        value = super().get(key, _UNSET)
        if value is _UNSET:
            return default
        self.touch(key)
        return value

    def setdefault(self, key, default=None):
        value = super().setdefault(key, default)
        self.touch(key)
        return value

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.touch(key)

    def update(self, *args, **kwargs):
        values = dict(*args, **kwargs)
        super().update(values)
        with self._candidate_lock:
            self._candidates.update(values)

    def __ior__(self, values):
        self.update(values)
        return self

    def __delitem__(self, key):
        super().__delitem__(key)
        with self._candidate_lock:
            self._removals += 1

    def pop(self, key, *default):
        existed = key in self
        value = super().pop(key, *default)
        if existed:
            with self._candidate_lock:
                self._removals += 1
        return value

    def popitem(self):
        value = super().popitem()
        with self._candidate_lock:
            self._removals += 1
        return value

    def clear(self):
        existed = bool(self)
        super().clear()
        if existed:
            with self._candidate_lock:
                self._removals += 1


@lru_cache(maxsize=1)
def _clone_function():
    if sys.platform != "darwin":
        return None
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    function = getattr(library, "fclonefileat", None)
    if function is not None:
        function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        function.restype = ctypes.c_int
    return function


def _clone(source_fd: int, directory_fd: int, name: str) -> bool:
    function = _clone_function()
    if function is None:
        return False
    if function(source_fd, directory_fd, os.fsencode(name), 0) == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS, errno.EINVAL}:
        return False
    raise OSError(error, os.strerror(error), name)


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _write_at(fd: int, data: bytes, offset: int) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.pwrite(fd, remaining, offset)
        if written <= 0:
            raise OSError(errno.EIO, "snapshot write made no progress")
        remaining = remaining[written:]
        offset += written


def _write_ranges(fd: int, ranges) -> int:
    """Batch adjacent slots without writing across unchanged clone regions."""
    pending, start, written_bytes = bytearray(), 0, 0
    for offset, data in ranges:
        if pending and (offset != start + len(pending)
                        or len(pending) + len(data) > WRITE_BATCH_BYTES):
            _write_at(fd, pending, start)
            pending = bytearray()
        if len(data) > WRITE_BATCH_BYTES:
            _write_at(fd, data, offset)
        else:
            if not pending:
                start = offset
            pending.extend(data)
        written_bytes += len(data)
    if pending:
        _write_at(fd, pending, start)
    return written_bytes


def _same_record(left, right):
    # Python considers True == 1 and 1 == 1.0; their JSON encodings differ.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return all(isinstance(key, str) for key in left) and left.keys() == right.keys() and all(
            _same_record(value, right[key]) for key, value in left.items())
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_same_record(a, b) for a, b in zip(left, right))
    if isinstance(left, float):
        return left.hex() == right.hex()
    return left == right


class AtomicJSONSnapshot:
    """Single-writer snapshot cache; the caller serializes calls to ``write``.

    The slot index is only an optimization in memory, never recovery evidence.
    Restart, deletion, replacement, growth beyond a slot or removal of a record
    reconstructs a whole snapshot. Failed writes invalidate the cache, including
    failures after rename, so an unchanged retry must cross the durable barrier.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._entries: dict[str, bytes] = {}
        # Only selected publication candidates are retained as decoded copies;
        # storing a second decoded copy of all startup history is unnecessary.
        self._selected_records: dict[str, object] = {}
        self._slots: dict[str, tuple[int, int]] = {}
        self._identity = None
        self._end = BLOCK_SIZE
        self._clone_enabled = _clone_function() is not None

    def _current_identity(self):
        try:
            value = self.path.lstat()
        except FileNotFoundError:
            return None
        return _identity(value) if stat.S_ISREG(value.st_mode) else None

    def _update_identity(self):
        try:
            value = self.path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(value.st_mode):
            raise RuntimeError("snapshot update source is not a regular file")
        return _identity(value)

    @staticmethod
    def _encode(value):
        result = {}
        for key, record in value.items():
            if not isinstance(key, str):
                raise TypeError("snapshot keys must be surface strings")
            text = json.dumps(key, ensure_ascii=False) + ":" + json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            result[key] = text.encode("utf-8")
        return result

    @staticmethod
    def _layout(entries):
        slots, offset = {}, BLOCK_SIZE
        for key in sorted(entries):
            # At least 1 KiB of growth room, rounded to filesystem blocks.
            needed = len(entries[key]) + 1 + BLOCK_SIZE // 4
            capacity = ((needed + BLOCK_SIZE - 1) // BLOCK_SIZE) * BLOCK_SIZE
            slots[key] = (offset, capacity)
            offset += capacity
        return slots, offset

    def update(self, value) -> dict:
        """Durably replace selected rows without re-encoding archived history.

        The same complete JSON file remains the recovery source. A cold or
        invalidated cache reads the original file before merging; malformed,
        changing or non-regular sources grant no persistence acknowledgement.
        Callers must serialize writes. Source checks detect external changes;
        checking identity and replacing the file are not one atomic operation.
        """
        value = copy.deepcopy(value)
        current = self._update_identity()
        if self._identity is not None and current == self._identity:
            entries = dict(self._entries)
            changed = {key: record for key, record in value.items()
                       if key not in self._selected_records
                       or not _same_record(record, self._selected_records[key])}
        else:
            self._selected_records.clear()
            try:
                source = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                if self.path.is_symlink():
                    raise RuntimeError("snapshot update source is not a regular file")
                if self._entries:
                    raise RuntimeError("snapshot source disappeared before update")
                previous = {}
                current = None
            else:
                try:
                    before = os.fstat(source)
                    if not stat.S_ISREG(before.st_mode):
                        raise RuntimeError("snapshot update source is not a regular file")
                    with os.fdopen(os.dup(source), "rb") as reader:
                        previous = json.load(reader)
                    current = _identity(before)
                    if (_identity(os.fstat(source)) != current
                            or self._current_identity() != current):
                        raise RuntimeError("snapshot source changed during update read")
                finally:
                    os.close(source)
            if not isinstance(previous, dict):
                raise ValueError("snapshot update source is not a JSON object")
            entries = self._encode(previous)
            changed = value
        entries.update(self._encode(changed))
        result = self._write_entries(entries, expected=current)
        self._selected_records.update(changed)
        return result

    def write(self, value) -> dict:
        self._selected_records.clear()
        return self._write_entries(self._encode(value))

    def _write_entries(self, entries, *, expected=_UNSET) -> dict:
        current = self._current_identity() if expected is _UNSET else self._update_identity()
        if expected is not _UNSET and current != expected:
            raise RuntimeError("snapshot source changed before update")
        reusable = self._identity is not None and current == self._identity
        if reusable and entries == self._entries:
            return {"mode": "unchanged", "written_bytes": 0,
                    "snapshot_bytes": current[2], "records": len(entries), "changed_records": 0}

        changed = {key for key in entries if entries[key] != self._entries.get(key)}
        incremental = (reusable and self._clone_enabled and bool(self._slots)
                       and self._entries.keys() <= entries.keys()
                       and all(len(entries[key]) + 1 <= capacity
                               for key, (_, capacity) in self._slots.items()))
        slots, end = (dict(self._slots), self._end) if incremental else self._layout(entries)
        if incremental:
            additions, extra = self._layout({key: entries[key] for key in entries.keys() - slots.keys()})
            for key, (offset, capacity) in additions.items():
                slots[key] = (end + offset - BLOCK_SIZE, capacity)
            end += extra - BLOCK_SIZE

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        name = f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        destination = source = None
        published = False
        written_bytes = 0
        mode = "full"
        try:
            if incremental:
                source = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                if _identity(os.fstat(source)) != self._identity:
                    raise RuntimeError("snapshot source changed before clone")
                cloned = _clone(source, directory, name)
                if cloned:
                    destination = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
                    if (_identity(os.fstat(source)) != self._identity
                            or self._current_identity() != self._identity):
                        raise RuntimeError("snapshot source changed during clone")
                    if os.fstat(destination).st_ino == os.fstat(source).st_ino:
                        raise RuntimeError("snapshot clone reused the published inode")
                    mode = "clone"
                else:
                    # A failed clone is never treated as a partially usable file.
                    self._clone_enabled = False
                    incremental = False
            if destination is None:
                destination = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                      0o600, dir_fd=directory)

            if self._clone_enabled:
                if not incremental:
                    slots, end = self._layout(entries)
                    changed = set(entries)
                def ranges():
                    if not incremental:
                        yield 0, b"{" + b" " * (BLOCK_SIZE - 1)
                    for index, (key, (offset, capacity)) in enumerate(slots.items()):
                        if key in changed:
                            data = (b"\n" if index == 0 else b",") + entries[key]
                            yield offset, data + b" " * (capacity - len(data))
                    if not incremental or end != self._end:
                        yield end, b"\n}\n"
                written_bytes = _write_ranges(destination, ranges())
                size = end + 3
            else:
                data = b"{" + b",".join(entries[key] for key in sorted(entries)) + b"}\n"
                _write_at(destination, data, 0)
                written_bytes = size = len(data)
                slots, end = {}, BLOCK_SIZE
                mode = "compact"
            os.ftruncate(destination, size)
            os.fchmod(destination, 0o600)
            os.fsync(destination)
            if expected is not _UNSET and self._update_identity() != expected:
                raise RuntimeError("snapshot source changed before update publication")
            os.replace(name, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
            published = True
            os.fsync(directory)
            committed = _identity(os.fstat(destination))
            if self._current_identity() != committed:
                raise RuntimeError("snapshot publication replaced by another writer")
            self._entries, self._slots, self._end = entries, slots, end
            self._identity = committed
            return {"mode": mode, "written_bytes": written_bytes, "snapshot_bytes": size,
                    "records": len(entries), "changed_records": len(changed)}
        except BaseException:
            self._identity = None
            raise
        finally:
            if source is not None:
                os.close(source)
            if destination is not None:
                os.close(destination)
            if not published:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=directory)
            os.close(directory)

"""Bounded, read-only resource samples tied to original process generations.

RSS sums include shared pages. Disk allocation includes APFS shared extents;
neither is a unique physical-cost or peak measurement.
"""
import ctypes
import math
import os
from pathlib import Path
import stat
import subprocess
import time


def descriptors(pid):
    import ccc_codex_queue as native
    if native._proc_pidinfo is None:
        raise OSError('native descriptor inventory unavailable')
    size = ctypes.sizeof(native._FdInfo)
    needed = native._proc_pidinfo(pid, 1, 0, None, 0)
    if needed <= 0 or needed % size or needed // size > 65536:
        raise OSError('invalid descriptor inventory size')
    rows = (native._FdInfo * (needed // size + 64))()
    returned = native._proc_pidinfo(pid, 1, 0, rows, ctypes.sizeof(rows))
    if returned <= 0 or returned >= ctypes.sizeof(rows) or returned % size:
        raise OSError('incomplete descriptor inventory')
    fds = [row.fd for row in rows[:returned // size]]
    if len(set(fds)) != len(fds) or any(fd < 0 for fd in fds):
        raise OSError('invalid descriptor identities')
    return len(fds)


def allocated_tree(root, check):
    """Count only this exact private root; refuse links and moving entries."""
    root = Path(root)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError('canonical private root required')
    first = root.stat()
    if not stat.S_ISDIR(first.st_mode):
        raise ValueError('private directory required')
    seen, files, visited = set(), 0, 0
    logical = allocated = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    def identity(info):
        return info.st_dev, info.st_ino
    def walk(fd, depth):
        nonlocal files, logical, allocated, visited
        check()
        if depth > 128:
            raise ValueError('private resource tree exceeds depth bound')
        with os.scandir(fd) as entries:
            for entry in entries:
                check()
                visited += 1
                if visited > 200000:
                    raise ValueError('private resource tree exceeds observation bound')
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise ValueError('link in private resource tree')
                if info.st_dev != first.st_dev:
                    raise ValueError('private tree crossed filesystem')
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(entry.name, flags, dir_fd=fd)
                    try:
                        if identity(os.fstat(child)) != identity(info):
                            raise ValueError('private directory changed before read')
                        walk(child, depth + 1)
                        if identity(os.stat(entry.name, dir_fd=fd, follow_symlinks=False)) != identity(info):
                            raise ValueError('private directory changed during read')
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode):
                    key = identity(info)
                    if key not in seen:
                        seen.add(key)
                        files += 1
                        logical += info.st_size
                        allocated += info.st_blocks * 512
    fd = os.open(root, flags)
    try:
        if identity(os.fstat(fd)) != identity(first):
            raise ValueError('private root changed before read')
        walk(fd, 0)
        disk = os.fstatvfs(fd)
    finally:
        os.close(fd)
    final = root.lstat()
    if (first.st_dev, first.st_ino) != (final.st_dev, final.st_ino):
        raise ValueError('private root changed during sample')
    check()
    return dict(root=str(root), root_identity=[first.st_dev, first.st_ino],
                regular_files=files, logical_bytes=logical, allocated_metadata_bytes=allocated,
                filesystem_free_bytes=disk.f_bavail * disk.f_frsize,
                allocation_atomic=False, unique_physical_bytes_proven=False)


def capture(originals, root, *, expected_count=500, seconds=30, auxiliaries=()):
    from ccc_guard_scope import birth
    if (type(seconds) not in (int, float) or not math.isfinite(seconds)
            or not 0 < seconds <= 120 or type(expected_count) is not int
            or not 1 <= expected_count <= 1000 or len(originals) != expected_count
            or len(auxiliaries) > 1000):
        raise ValueError('exact original process set and bounded duration required')
    started = time.monotonic()
    deadline = started + seconds
    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError('resource observation deadline expired')
    pins = {}
    roles = {}
    for row in [*originals, *auxiliaries]:
        pid, generation = row['pid'], row['birth']
        if (type(pid) is not int or pid <= 1 or pid in pins
                or not isinstance(generation, list) or len(generation) != 2
                or any(type(x) is not int for x in generation)
                or generation[0] <= 0 or not 0 <= generation[1] < 1000000):
            raise ValueError('unique original PID and birth required')
        check()
        if birth(pid) != generation:
            raise ValueError('original process generation changed before resource read')
        pins[pid] = list(generation)
        roles[pid] = 'native' if len(pins) <= expected_count else 'auxiliary'
    budget = min(10, deadline-time.monotonic())
    if budget <= 0:
        raise TimeoutError('resource observation deadline expired')
    result = subprocess.run(['/bin/ps', '-o', 'pid=,rss=', '-p', ','.join(map(str, pins))],
                            capture_output=True, text=True, timeout=budget)
    check()
    if result.returncode != 0 or result.stderr:
        raise OSError('resource process query failed')
    memory = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise ValueError('invalid RSS row')
        pid, kib = map(int, fields)
        if pid not in pins or pid in memory or kib < 0:
            raise ValueError('unexpected RSS process or value')
        memory[pid] = kib * 1024
    if set(memory) != set(pins):
        raise ValueError('incomplete RSS process set')
    rows = []
    for pid, generation in pins.items():
        check()
        count = descriptors(pid)
        if birth(pid) != generation:
            raise ValueError('original process changed during descriptor read')
        rows.append(dict(pid=pid, birth=generation, role=roles[pid],
                         rss_bytes=memory[pid], fd_count=count))
    disk = allocated_tree(root, check)
    for pid, generation in pins.items():
        check()
        if birth(pid) != generation:
            raise ValueError('original process changed before sample completion')
    check()
    return dict(started_monotonic=started, finished_monotonic=time.monotonic(),
                processes=rows, process_count=len(rows),
                native_process_count=expected_count, auxiliary_process_count=len(auxiliaries),
                rss_sum_bytes=sum(r['rss_bytes'] for r in rows),
                fd_sum=sum(r['fd_count'] for r in rows), disk=disk,
                peak_usage_proven=False, full_500_acceptance=False,
                limits=['Sequential observations, not an atomic fleet snapshot.',
                        'RSS sums include shared memory; disk blocks include cloned extents.',
                        'Only explicitly bound processes; transient children and shared cmux costs excluded.'])

"""Read a native TUI's foreground selection; never supply terminal identity."""
import json
import os
from pathlib import Path
import stat
import time
import uuid

import ccc_foreground_lease as lease

_seen = set()
_leased_generations = {}


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path, limit):
    initial = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_mode & 0o077 or before.st_size > limit):
            raise ValueError('untrusted foreground evidence')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    signature = _signature(before)
    if (len(raw) > limit or signature != _signature(initial)
            or signature != _signature(after) or signature != _signature(path.lstat())):
        raise ValueError('foreground evidence changed')
    return raw, signature


def read(client):
    """None means legacy absent; an observed null thread means cleared selection.

    A required/missing, malformed or changing record is an error, never an argv
    fallback. A verified optional record predating this process belongs to an
    earlier PID incarnation, unless this incarnation already had a selection.
    The caller authenticates process/ancestry and repeats this read.
    """
    argv, env = client['argv'], client['env']
    if len(argv) > 1 and argv[1] == 'app-server':
        return None
    required = env.get('CODEX_CLIENT_THREAD_OBSERVER')
    if required not in (None, '1'):
        raise ValueError('invalid foreground observer mode')
    home = env.get('CODEX_HOME')
    if home is None:
        base = env.get('HOME')
        if not base:
            if required:
                raise ValueError('foreground observer home missing')
            return None
        home = str(Path(base) / '.codex')
    home = Path(home)
    if not home.is_absolute():
        raise ValueError('foreground observer home is not absolute')
    directory = home / 'credential-observations'
    path = directory / f"client-{client['pid']}-thread.json"
    key = (client['pid'], tuple(client['birth']))
    try:
        path.lstat()
    except FileNotFoundError:
        if required or key in _seen:
            raise ValueError('required foreground selection missing')
        return None
    hs, ds = home.lstat(), directory.lstat()
    if (not stat.S_ISDIR(hs.st_mode) or not stat.S_ISDIR(ds.st_mode)
            or hs.st_uid != os.getuid() or ds.st_uid != os.getuid()
            or hs.st_mode & 0o022 or ds.st_mode & 0o077
            or path.resolve(strict=True) != path):
        raise ValueError('untrusted foreground directory')
    marker = directory / 'enabled-v1'
    marker_raw, marker_signature = _read(marker, 27)
    if marker_raw != b'ccc-request-credentials-v1\n':
        raise ValueError('foreground observation is not enabled')
    raw, signature = _read(path, 4096)
    data = json.loads(raw, object_pairs_hook=lease.unique_object)
    if (not isinstance(data, dict) or type(data.get('schema')) is not int
            or data['schema'] not in (1, 2) or type(data.get('pid')) is not int
            or data['pid'] != client['pid'] or data.get('purpose') != 'client_foreground_thread'):
        raise ValueError('foreground selection schema or PID mismatch')
    if data['schema'] == 1 and key in _leased_generations:
        raise ValueError('foreground selection schema regressed')
    epoch = data.get('client_epoch')
    if not isinstance(epoch, str) or str(uuid.UUID(epoch)) != epoch:
        raise ValueError('invalid foreground client epoch')
    stamp = data.get('published_at_ms')
    birth_ms = client['birth'][0] * 1000 + client['birth'][1] / 1000
    if type(stamp) is not int or stamp > time.time() * 1000 + 1000:
        raise ValueError('foreground selection predates process or is in the future')
    thread = data['thread_id']
    if thread is not None and (not isinstance(thread, str) or str(uuid.UUID(thread)) != thread):
        raise ValueError('invalid foreground thread')
    candidate = evidence = None
    if data['schema'] == 2:
        if stamp < birth_ms:
            raise ValueError('native leased selection predates this process')
        candidate, evidence = lease.validate(data, key, epoch, stamp, thread, _signature(ds), directory,
            cache=_leased_generations, wall_ms=time.time() * 1000,
            local_mono=time.monotonic(), wire_ns=time.clock_gettime_ns(time.CLOCK_MONOTONIC))
    if (_signature(hs)[:4] != _signature(home.lstat())[:4]
            or _signature(ds)[:4] != _signature(directory.lstat())[:4]
            or _read(marker, 27) != (marker_raw, marker_signature)):
        raise ValueError('foreground source changed')
    final_raw, final_signature = _read(path, 4096)
    if data['schema'] == 1:
        if (final_raw, final_signature) != (raw, signature):
            raise ValueError('foreground source changed')
    else:
        final = json.loads(final_raw, object_pairs_hook=lease.unique_object)
        # The first record has already bound PID/schema/epoch/SID. A later
        # heartbeat may replace its inode, but cannot change those semantics.
        if (not isinstance(final, dict) or type(final.get('schema')) is not int
                or final['schema'] != 2 or type(final.get('pid')) is not int
                or final['pid'] != client['pid'] or final.get('purpose') != 'client_foreground_thread'
                or final.get('client_epoch') != epoch or final.get('thread_id') != thread
                or type(final.get('published_at_ms')) is not int
                or final['published_at_ms'] < stamp
                or type(final.get('monotonic_published_ns')) is not int
                or final['monotonic_published_ns'] < data['monotonic_published_ns']):
            raise ValueError('native leased selection changed during read')
        candidate, final_evidence = lease.validate(final, key, epoch, final['published_at_ms'], thread,
            _signature(ds), directory, cache=_leased_generations, wall_ms=time.time() * 1000,
            local_mono=time.monotonic(), wire_ns=time.clock_gettime_ns(time.CLOCK_MONOTONIC))
        if (final_evidence != evidence
                or _signature(hs)[:4] != _signature(home.lstat())[:4]
                or _signature(ds)[:4] != _signature(directory.lstat())[:4]
                or _read(marker, 27) != (marker_raw, marker_signature)
                or time.monotonic() >= candidate['deadline']
                or time.clock_gettime_ns(time.CLOCK_MONOTONIC) >= final['monotonic_expires_ns']):
            raise ValueError('native leased source changed or expired at final check')
    if stamp < birth_ms:
        if required or key in _seen:
            raise ValueError('foreground selection predates process or is in the future')
        # The filename contains only the PID. A new legacy client can reuse it
        # without publishing an observer record. Discard only this fully checked
        # old selection, never its errors, and do not mark the new client seen.
        # Exact resume argv and all kernel/TTY/UUID checks remain the caller's job.
        return None
    if len(_seen) >= 4096 and key not in _seen:
        raise ValueError('foreground identity capacity exceeded')
    _seen.add(key)
    if candidate is not None:
        _leased_generations[key] = candidate
        # Schema2 proof is committed semantics, not heartbeat-varying mtime,
        # inode or publication time. Caller process/ancestry checks stay intact.
        return {'schema': 2, 'thread_id': thread, 'client_epoch': epoch,
                'connection_epoch': data['connection_epoch'],
                'selection_generation': data['selection_generation'],
                'path': str(path), 'signature': evidence, 'marker_signature': marker_signature}
    return {'thread_id': thread, 'client_epoch': epoch, 'published_at_ms': stamp,
            'path': str(path), 'signature': signature, 'marker_signature': marker_signature}

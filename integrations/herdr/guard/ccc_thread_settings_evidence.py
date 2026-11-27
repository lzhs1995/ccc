"""Read persisted settings of one proven original thread; never launch or send.

config/read is configuration provenance, not an in-memory thread snapshot.
This module only reports the last complete thread-owned native snapshot at a
fixed, rechecked EOF. Caller must separately reprove the live selected thread,
effective provider endpoint/credentials, client, and original recovery policy.
"""
from __future__ import annotations

from ccc_public_protection import PUBLIC_PROTECTED_PID, PUBLIC_PROTECTED_SURFACE
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

LIMIT = 16 * 1024 * 1024
HEADER_LIMIT = 256 * 1024
FLAGS = ('isMeta', 'isSynthetic', 'synthetic', 'isSidechain', 'isCompactSummary')
REQUIRED = {'model', 'model_provider_id', 'approval_policy', 'approvals_reviewer',
            'permission_profile', 'cwd', 'collaboration_mode', 'disabled_plugin_ids'}
OPTIONAL = {'service_tier', 'active_permission_profile', 'runtime_workspace_roots',
            'reasoning_effort', 'reasoning_summary', 'personality',
            'requested_approvals_reviewer'}


def need(value, reason):
    if not value:
        raise ValueError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        need(key not in value, 'duplicate_json_key')
        value[key] = item
    return value


def decode(raw):
    return json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=unique_pairs,
                      parse_constant=lambda _: need(False, 'nonfinite_json'))


def canonical_sid(value):
    need(isinstance(value, str) and str(uuid.UUID(value)) == value, 'invalid_thread_id')
    return value


def timestamp(value):
    need(isinstance(value, str), 'settings_timestamp_missing')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    need(parsed.utcoffset() is not None, 'settings_timestamp_without_timezone')
    return parsed.timestamp()


def settings_snapshot(value):
    need(isinstance(value, dict) and REQUIRED <= value.keys()
         and value.keys() <= REQUIRED | OPTIONAL, 'unsupported_thread_settings_schema')
    need(all(isinstance(value[k], str) and value[k] for k in
             ('model', 'model_provider_id', 'cwd', 'approvals_reviewer')),
         'invalid_thread_settings_identity')
    need(Path(value['cwd']).is_absolute(), 'settings_cwd_not_absolute')
    need(isinstance(value['permission_profile'], dict)
         and isinstance(value['approval_policy'], (str, dict))
         and isinstance(value['disabled_plugin_ids'], list)
         and all(isinstance(x, str) for x in value['disabled_plugin_ids']),
         'invalid_thread_settings_policy')
    for key in ('service_tier', 'reasoning_effort', 'reasoning_summary', 'personality',
                'requested_approvals_reviewer'):
        need(value.get(key) is None or isinstance(value[key], str), 'invalid_' + key)
    roots = value.get('runtime_workspace_roots')
    need(roots is None or isinstance(roots, list) and all(
        isinstance(x, str) and Path(x).is_absolute() for x in roots), 'invalid_workspace_roots')
    mode = value['collaboration_mode']
    need(isinstance(mode, dict) and isinstance(mode.get('settings'), dict),
         'invalid_collaboration_mode')
    need(mode['settings'].get('model') == value['model']
         and mode['settings'].get('reasoning_effort') == value.get('reasoning_effort'),
         'snapshot_model_or_effort_conflict')
    return value


def scan_span(raw, *, start, session_id):
    """Top-level events only. Never search nested replacement histories or text."""
    canonical_sid(session_id)
    need(raw.endswith(b'\n'), 'incomplete_eof_record')
    offset = start
    if start:
        cut = raw.find(b'\n')
        need(cut >= 0, 'no_complete_record_in_window')
        offset += cut + 1
        raw = raw[cut + 1:]
    selected = None
    reason = 'no_owned_snapshot_in_bounded_window'
    count = 0
    foreign = 0
    for line in raw.splitlines(keepends=True):
        position, offset = offset, offset + len(line)
        event = decode(line)
        need(isinstance(event, dict), 'invalid_top_level_record')
        payload = event.get('payload')
        if not isinstance(payload, dict):
            continue
        if event.get('type') == 'event_msg' and payload.get('type') == 'thread_settings_applied':
            owner = payload.get('thread_id')
            # A missing owner is not evidence for this SID, including legacy copies.
            if owner is None:
                selected, reason = None, 'unowned_settings_snapshot'
                continue
            canonical_sid(owner)
            if owner != session_id:
                foreign += 1
                continue
            need(not any(event.get(k) or payload.get(k) for k in FLAGS),
                 'synthetic_owned_settings_snapshot')
            need(set(payload) == {'type', 'thread_id', 'thread_settings'},
                 'unsupported_settings_event')
            settings = settings_snapshot(payload.get('thread_settings'))
            timestamp(event.get('timestamp'))
            selected = dict(offset=position, bytes=len(line), sha256=digest(line),
                            timestamp=event['timestamp'], settings=settings)
            reason, count = 'persisted_snapshot_at_observed_eof', count + 1
        elif event.get('type') == 'turn_context' and selected is not None:
            # A later turn may have request-specific settings. Do not substitute
            # its model while retaining the earlier snapshot's provider/policy.
            old = selected['settings']
            if (payload.get('model') != old['model']
                    or payload.get('effort') != old.get('reasoning_effort')):
                selected, reason = None, 'later_turn_settings_conflict'
    return dict(snapshot=selected, reason=reason, owned_snapshots=count,
                foreign_snapshots=foreign, scanned_start=start, scanned_end=offset)


def stamp(s):
    return (s.st_dev, s.st_ino, s.st_uid, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def read_settings(document, *, limit=LIMIT):
    """Bounded no-follow read; old saved contexts provide locators, not authority."""
    result = dict(schema='ccc-original-thread-settings-evidence-v1',
                  status='UNKNOWN', reason='unverified', input_operations=0,
                  execution_authorized=False, current_thread_authority=False,
                  provider_endpoint_verified=False, live_runtime_settings_verified=False)
    try:
        need(document.get('schema') == 'ccc-original-exit-context-v1', 'wrong_context_schema')
        session_id = canonical_sid(document['session_id'])
        result['session_id'] = session_id
        need(document['binding'].get('cmux_surface_id', '').upper()
             != PUBLIC_PROTECTED_SURFACE
             and document['binding'].get('pid') != PUBLIC_PROTECTED_PID, 'protected_business_surface')
        need(type(limit) is int and 0 < limit <= LIMIT, 'invalid_read_limit')
        evidence = document['history']
        path = Path(evidence['path'])
        need(path.is_absolute() and path.resolve(strict=True) == path, 'history_path_not_canonical')
        expected = (evidence['device'], evidence['inode'])
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(fd)
            need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                 and not before.st_mode & 0o022 and (before.st_dev, before.st_ino) == expected,
                 'history_identity_or_permissions_changed')
            header = os.pread(fd, min(before.st_size, HEADER_LIMIT), 0).split(b'\n', 1)[0] + b'\n'
            need(len(header) == evidence['header_bytes'] and digest(header) == evidence['header_sha256'],
                 'original_header_changed')
            event = decode(header)
            need(event.get('type') == 'session_meta' and event.get('payload', {}).get('id') == session_id,
                 'header_thread_mismatch')
            start = max(0, before.st_size - limit)
            raw = os.pread(fd, before.st_size - start, start)
            need(len(raw) == before.st_size - start, 'short_history_read')
            outcome = scan_span(raw, start=start, session_id=session_id)
            need(os.pread(fd, len(raw), start) == raw and os.pread(fd, len(header), 0) == header,
                 'history_bytes_changed')
            need(stamp(os.fstat(fd)) == stamp(before) == stamp(path.lstat())
                 and path.resolve(strict=True) == path, 'history_changed_during_read')
        finally:
            os.close(fd)
        result.update(outcome)
        result['history'] = dict(path=str(path), device=before.st_dev, inode=before.st_ino,
                                observed_size=before.st_size, span_start=start,
                                span_bytes=len(raw), span_sha256=digest(raw),
                                header_sha256=digest(header))
        if outcome['snapshot'] is not None:
            result['status'] = 'PERSISTED_THREAD_SETTINGS_OBSERVED'
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        result.update(reason=str(error), error_type=type(error).__name__)
        return result

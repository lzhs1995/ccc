"""Bounded native thread metadata, separate from an inherited hook session_id.

No inventory, terminal input or task gate. Missing proof means no supervisor
discovery; ordinary tools and turn-end remain available.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

MAX_META_BYTES = 128 * 1024


def uuid_value(value):
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        return None


def describe(payload):
    inherited = uuid_value(payload.get('session_id') or payload.get('sessionId'))
    result = dict(inherited_session_id=inherited, actual_thread_id=None,
                  parent_thread_id=None, thread_scope='UNVERIFIED')
    name = payload.get('transcript_path') or payload.get('transcriptPath')
    if not isinstance(name, str) or not name:
        return result
    # An unverified child must not overwrite its parent's adoption receipt.
    result['unverified_key'] = hashlib.sha256(name.encode()).hexdigest()[:16]
    try:
        path = Path(name)
        if not path.is_absolute() or path.resolve() != path:
            return result
        bases = (Path.home() / '.codex/sessions', Path.home() / '.codex/archived_sessions')
        if not any(path.is_relative_to(base) for base in bases):
            return result
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
                return result
            raw = handle.readline(MAX_META_BYTES + 1)
            after = os.fstat(handle.fileno())
        if (len(raw) > MAX_META_BYTES or not raw.endswith(b'\n')
                or before.st_ino != after.st_ino or after.st_size < before.st_size):
            return result
        entry = json.loads(raw)
        meta = entry.get('payload') if entry.get('type') == 'session_meta' else None
        if not isinstance(meta, dict):
            return result
        actual = uuid_value(meta.get('id'))
        if not actual or not path.name.endswith('-' + actual + '.jsonl'):
            return result
        source = meta.get('source')
        subagent = source.get('subagent') if isinstance(source, dict) else None
        spawn = subagent.get('thread_spawn', {}) if isinstance(subagent, dict) else {}
        parent = uuid_value(meta.get('parent_thread_id') or spawn.get('parent_thread_id'))
        child = bool(subagent or parent or meta.get('thread_source') == 'subagent'
                     or actual != inherited)
        for key in ('subagent_context', 'subagent'):
            hint = payload.get(key)
            if isinstance(hint, dict) and hint:
                child = True
        scope = 'CHILD_METADATA' if child else 'ROOT_METADATA'
        if not child and (not isinstance(source, str) or not source
                          or meta.get('thread_source') not in (None, 'user', 'root')):
            scope = 'UNVERIFIED'
        result.update(actual_thread_id=actual, parent_thread_id=parent,
                      thread_scope=scope, transcript_path=str(path),
                      transcript_device=before.st_dev, transcript_inode=before.st_ino,
                      metadata_sha256=hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return result


def authenticated_root(facts, proof):
    """Metadata alone cannot authenticate the current foreground supervisor."""
    if facts.get('thread_scope') != 'ROOT_METADATA' or not isinstance(proof, dict):
        return False
    actual = facts.get('actual_thread_id')
    foreground = proof.get('foreground')
    return bool(actual and uuid_value(proof.get('session')) == actual
                and isinstance(foreground, dict)
                and uuid_value(foreground.get('thread_id')) == actual)

"""One durable compact, followed by one continuation in the original session.

Only native rollout records establish compaction. An uncertain input retains
its intent across restarts; neither a screen nor a timeout permits a resend.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from ccc_provider_retry import structured_error_type

ACTIVE = frozenset({'compact_intent', 'compacting', 'compact_unknown',
                    'compact_complete', 'continue_intent', 'continue_unknown'})
COMPACT_PHASES = ACTIVE - {'continue_intent', 'continue_unknown'}
IDENTITY_FIELDS = ('surface_id', 'workspace_id', 'session_id', 'pid', 'process_start')
READ_LIMIT = 8 * 1024 * 1024


def _native_goal_context(payload):
    """Only native Goal metadata paired with its exact envelope is advisory."""
    metadata = payload.get('internal_chat_message_metadata_passthrough') or {}
    content = payload.get('content')
    if (not isinstance(metadata, dict)
            or not isinstance(content, list) or len(content) != 1
            or not isinstance(content[0], dict) or content[0].get('type') != 'input_text'):
        return False
    kinds = metadata.get('content_item_kinds')
    if kinds == ['user.goal']:
        source = 'user_goal'
    elif kinds == ['goal.internal_context']:
        source = 'goal'
    else:
        return False
    text = content[0].get('text')
    return (isinstance(text, str)
            and text.startswith('<codex_internal_context source="' + source + '">\n')
            and text.endswith('\n</codex_internal_context>'))

def same_session(record, identity):
    return bool(identity and all(record['identity'].get(k) == identity.get(k)
                                 for k in IDENTITY_FIELDS)
                and ('birth' not in record['identity']
                     or record['identity']['birth'] == identity.get('birth')))


class EncryptedRecoveryStore:
    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS repairs '
                       '(key TEXT PRIMARY KEY, surface TEXT NOT NULL, record TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, surface_id):
        if not self.path.exists():
            return None
        with self.transaction() as db:
            row = db.execute('SELECT record FROM repairs WHERE surface=? '
                             'ORDER BY rowid DESC LIMIT 1', (surface_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def begin(self, identity, source, path):
        error = source.get('error')
        if (source.get('kind') != 'task_complete' or not isinstance(error, dict)
                or structured_error_type(error.get('message')) != 'invalid_encrypted_content'
                or not identity or any(not identity.get(k) for k in IDENTITY_FIELDS)
                or identity.get('turn_id') != source.get('turn_id')
                or identity.get('at') != source.get('at')):
            raise ValueError('original terminal encrypted-content failure required')
        path = Path(path).resolve(strict=True)
        with path.open('rb') as stream:
            info = os.fstat(stream.fileno())
            header = json.loads(stream.readline())
            if (header.get('type') != 'session_meta'
                    or header.get('payload', {}).get('id') != identity['session_id']):
                raise ValueError('original rollout session mismatch')
            fence = info.st_size
            stream.seek(max(0, fence - 4096))
            boundary = stream.read()
            signature = source.get('signature', [])
            if (not boundary.endswith(b'\n') or len(signature) < 2
                    or signature[0] != info.st_ino or signature[1] != fence):
                raise ValueError('original native EOF changed')
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        value = dict(key=key, identity=dict(identity), source_turn=source['turn_id'],
                     path=str(path), file_id=[info.st_dev, info.st_ino],
                     fence=fence, boundary_start=max(0, fence - 4096),
                     boundary_sha256=hashlib.sha256(boundary).hexdigest(), cursor=fence,
                     phase='compact_intent', compact_attempt=uuid.uuid4().hex,
                     compacted_offset=None, compact_turn=None, compact_started_offset=None,
                     compact_completed_offset=None, pending_tool_calls=[], created_at=self.clock(),
                     input_operations=1, replay_authorized=False)
        with self.transaction() as db:
            existing = db.execute('SELECT record FROM repairs WHERE key=?', (key,)).fetchone()
            if existing:
                return json.loads(existing[0]), False
            active = db.execute('SELECT record FROM repairs WHERE surface=? '
                                'ORDER BY rowid DESC LIMIT 1', (identity['surface_id'],)).fetchone()
            if active and json.loads(active[0]).get('phase') in ACTIVE:
                raise ValueError('original encrypted recovery remains pending')
            previous = db.execute('SELECT record FROM repairs WHERE surface=? '
                                  'ORDER BY rowid DESC', (identity['surface_id'],)).fetchall()
            for row in previous:
                prior = json.loads(row[0])
                if (prior['identity'].get('session_id') == identity['session_id']
                        and prior.get('compact_turn') == source['turn_id']):
                    # The fresh admission must enforce this too: the watcher
                    # can observe a different turn before taking its locks.
                    raise ValueError('repair own compact turn cannot start another recovery')
            attempts = 0
            for row in previous:
                prior = json.loads(row[0])
                if prior['identity'].get('session_id') != identity['session_id'] or prior.get('tool_progress'):
                    break
                attempts += 1
            if attempts >= 2:
                raise ValueError('two compact repairs without native tool progress; diagnosis required')
            db.execute('INSERT INTO repairs VALUES (?,?,?)',
                       (key, identity['surface_id'], json.dumps(value)))
        return value, True

    def update(self, original, **changes):
        value = dict(copy.deepcopy(original), **changes, updated_at=self.clock())
        with self.transaction() as db:
            row = db.execute('SELECT record FROM repairs WHERE key=?', (original['key'],)).fetchone()
            if not row or json.loads(row[0]) != original:
                raise ValueError('original recovery changed during admission')
            db.execute('UPDATE repairs SET record=? WHERE key=?',
                       (json.dumps(value), original['key']))
        return value

    def observe(self, original, turn):
        """Advance a bounded append cursor; user input/abort supersedes recovery."""
        if not same_session(original, turn):
            return original, 'identity_changed'
        value = copy.deepcopy(original)
        path = Path(value['path'])
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if [before.st_dev, before.st_ino] != value['file_id'] or before.st_size < value['cursor']:
                return original, 'rollout_changed'
            stream.seek(value['boundary_start'])
            if hashlib.sha256(stream.read(value['fence'] - value['boundary_start'])).hexdigest() != value['boundary_sha256']:
                return original, 'original_boundary_changed'
            stream.seek(value['cursor'])
            consumed = 0
            while consumed < READ_LIMIT:
                start = stream.tell()
                remaining = READ_LIMIT - consumed
                raw = stream.readline(remaining + 1)
                if not raw or not raw.endswith(b'\n') or len(raw) > remaining:
                    break
                consumed += len(raw)
                event = json.loads(raw)
                payload = event.get('payload', {})
                if not isinstance(payload, dict):
                    raise ValueError('invalid native rollout record')
                outer, kind = event.get('type'), payload.get('type')
                if outer == 'event_msg' and kind == 'turn_aborted':
                    value.update(phase='cancelled', reason='native user interruption preserved')
                elif outer == 'event_msg' and kind == 'user_message':
                    text = str(payload.get('message') or '').strip()
                    if value.get('continue_message') and text == value['continue_message']:
                        # event_msg is advisory; wait for the original user
                        # response_item before recording a native receipt.
                        pass
                    elif text != '/compact':
                        value.update(phase='superseded', reason='later original-session user input')
                elif outer == 'response_item' and kind == 'message' and payload.get('role') == 'user':
                    if _native_goal_context(payload):
                        value['cursor'] = stream.tell()
                        continue
                    content = payload.get('content', [])
                    texts = [item.get('text', '') for item in content if isinstance(item, dict)]
                    text = '\n'.join(texts).strip()
                    if value.get('continue_message') and text == value['continue_message']:
                        value.update(phase='done', reason='original native continuation received')
                    elif text != '/compact':
                        value.update(phase='superseded', reason='later original-session user input')
                elif (outer == 'event_msg' and kind == 'task_started'
                      and value['phase'] in COMPACT_PHASES):
                    turn_id = payload.get('turn_id')
                    if not isinstance(turn_id, str) or not turn_id:
                        raise ValueError('native compact start lacks a turn identity')
                    if value['compact_turn'] is None:
                        value.update(compact_turn=turn_id, compact_started_offset=start)
                    elif turn_id != value['compact_turn']:
                        value.update(phase='superseded', reason='original session started another turn')
                elif (outer == 'compacted' and value['phase'] in COMPACT_PHASES
                      and value.get('compact_started_offset') is not None
                      and start > value['compact_started_offset']):
                    value.update(compacted_offset=start,
                                 compacted_bytes=len(raw),
                                 compacted_sha256=hashlib.sha256(raw).hexdigest())
                elif (outer == 'event_msg' and kind == 'task_complete'
                      and value['phase'] in COMPACT_PHASES
                      and payload.get('turn_id') == value['compact_turn']):
                    if payload.get('error'):
                        value.update(phase='failed', reason='native compaction ended with an error')
                    elif value.get('compacted_offset') is not None and start > value['compacted_offset']:
                        value['compact_completed_offset'] = start
                elif outer == 'response_item' and kind in {'function_call', 'custom_tool_call'}:
                    # Compaction can re-emit a retained call without executing
                    # it. A call alone is not evidence of native tool progress.
                    call = payload.get('call_id')
                    if isinstance(call, str) and call not in value.get('pending_tool_calls', []):
                        value.setdefault('pending_tool_calls', []).append(call)
                elif (outer == 'event_msg' and kind in {'exec_command_begin', 'mcp_tool_call_begin'}
                      or outer == 'response_item' and kind in {'function_call_output', 'custom_tool_call_output'}
                      and payload.get('call_id') in value.get('pending_tool_calls', [])):
                    value.update(phase='superseded', tool_progress=True,
                                 reason='original session already made tool progress')
                value['cursor'] = stream.tell()
            caught_up = value['cursor'] == before.st_size == os.fstat(stream.fileno()).st_size
        ready = (value['phase'] in COMPACT_PHASES
                 and value.get('compact_completed_offset') is not None and caught_up
                 and turn.get('kind') == 'task_complete' and not turn.get('error')
                 and turn.get('turn_id') == value.get('compact_turn'))
        if ready:
            value.update(phase='compact_complete', completion_turn=dict(turn))
        if value != original:
            value = self.update(original, **{k: v for k, v in value.items() if original.get(k) != v})
        return value, 'ready' if ready else value['phase']

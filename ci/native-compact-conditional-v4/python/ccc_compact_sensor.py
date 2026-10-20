"""Read-only compaction evidence for the four explicitly designated threads.

No terminal input, process management, configuration writes, or model requests.
The native preparation span and post-sampling decision precede HTTP requests.
"""
from __future__ import annotations

import datetime as dt
from contextlib import closing
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from ccc_codex_goal import _debug_fields
from ccc_compact_trace import native_trace, trace_id

LOG_DB = '/Users/lzhs/.codex/logs_2.sqlite'
TRUSTED_SPANS = {
    'codex_mcp::connection_manager::tool_catalog',
    'codex_http_client::client', 'feedback_tags',
    'codex_core::session::turn', 'codex_core::compact',
    'codex_core::client', 'codex_api::endpoint::responses',
}


def epoch(value):
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def classify_start(rows, turn, completed_at=0):
    """Return a stable decision/preparation boundary, never an HTTP restart.

    rows are trusted indexed log rows with native thread and turn identities.
    The caller excludes completed compactions and provides the *current* turn.
    Pre-turn time includes preparation from native turn start; ordinary long
    turns, standalone reasoning, and HTTP errors do not establish compaction.
    """
    if not turn or turn.get('status') != 'inProgress':
        return None
    choices = []
    for row in rows:
        body = row['body']
        at = row['at']
        if at <= completed_at or turn['id'] not in body:
            continue
        source = None
        cause = {}
        started = at
        if (row['target'] == 'codex_core::session::turn'
                and 'post sampling token usage' in body
                and re.search(r'(?<![\w_])token_limit_reached=true\b', body)
                and re.search(r'(?<![\w_])needs_follow_up=true\b', body)):
            source = 'post_sampling_decision'
        elif row['target'] in {'codex_core::session::handlers', 'codex_core::codex'}:
            # A quoted operation or another submission's id is not a manual
            # compact. Parse the outer native envelope, including its id.
            marker = re.match(r'^session_loop\{thread_id=([^{}\s]+)\}: Submission sub=', body)
            try:
                if not marker or row.get('thread_id', marker[1]) != marker[1]:
                    continue
                value = _debug_fields(body[marker.end():], 'Submission')
                if json.loads(value['id']) == turn['id'] and value['op'] == 'Compact':
                    source = 'manual_submission'
                    parent = native_trace(value.get('trace'))
                    cause = {'submission_id': turn['id'], 'thread_id': marker[1],
                             'traceparent': parent, 'trace_id': trace_id(parent)}
            except (IndexError, KeyError, TypeError, ValueError):
                pass
        elif (completed_at < float(turn['startedAt'])
              and row['target'] in TRUSTED_SPANS and 'run_pre_sampling_compact:' in body
              and (':step_context.capture' in body or ':run_auto_compact{' in body)):
            source = 'pre_sampling_preparation'
            started = max(float(turn['startedAt']), completed_at)
        if source:
            choices.append({'key': f"{turn['id']}:{source}:{started:.6f}",
                            'turn_id': turn['id'], 'started_at': started,
                            'source': source, 'log_id': row['id'], 'observed_log_at': at,
                            **cause})
    if not choices:
        return None
    # Preserve all causes. A manual submission in this turn prevents a later
    # natural-only handoff, even if natural preparation was logged first.
    manual = [item for item in choices if item['source'] == 'manual_submission']
    chosen = dict(min(manual or choices, key=lambda item: item['started_at']))
    chosen['candidates'] = sorted(choices, key=lambda item: (item['started_at'], item['log_id']))
    chosen['manual_present'] = bool(manual)
    return chosen


class RolloutFacts:
    """Incremental non-content facts. Initial scan skips reasoning payloads."""
    def __init__(self, path):
        self.path = Path(path)
        stat = self.path.stat()
        self.file_id = (stat.st_dev, stat.st_ino)
        self.offset = 0
        self.compacted_at = 0.0
        self.compacted_offset = -1
        self.replacement_items = 0
        self.user_offset = -1
        self.user_at = 0.0
        self.tool_result_at = 0.0
        self.tool_result_offset = -1
        self.native_task_at = 0.0
        self.last_turn_id = None
        # This is a journal observation, never an authenticated RPC active turn.
        self.observed_turn = None
        self.lifecycle_error = None
        self.lifecycle_at = 0.0
        self.session_id = None
        self.session_created_at = None
        self.compaction_occurrence = None
        self.seen_compaction_occurrences = set()
        self.turn_context = None
        # Keep every complete native receipt, including repeated equal inputs.
        # A newer user message must never hide an older undelivered callback.
        self.native_inputs = {}
        self.receipt_count = 0
        self.receipt_overflow = False

    def refresh(self, max_bytes=16 * 1024 * 1024):
        stat = self.path.stat()
        if (stat.st_dev, stat.st_ino) != self.file_id or stat.st_size < self.offset:
            raise RuntimeError('original rollout replaced or truncated; observe only')
        stop = self.offset + max_bytes
        with self.path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != self.file_id:
                raise RuntimeError('original rollout changed during open')
            stream.seek(self.offset)
            while stream.tell() < min(stop, stat.st_size):
                start = stream.tell()
                raw = stream.readline(16 * 1024 * 1024 + 1)
                if not raw.endswith(b'\n'):
                    if len(raw) > 16 * 1024 * 1024:
                        raise RuntimeError('oversized rollout record; observe only')
                    break
                end = stream.tell()
                head = raw[:512]
                if not any(token in head for token in (
                    b'"compacted"', b'"function_call_output"', b'"custom_tool_call_output"',
                    b'"message"', b'"user_message"', b'"task_started"', b'"task_complete"',
                    b'"turn_aborted"', b'"session_meta"', b'"item_started"',
                    b'"item_completed"', b'"turn_context"')):
                    self.offset = end
                    continue
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict) or not isinstance(value.get('payload'), dict):
                        raise ValueError('native record and payload must be objects')
                    self.observe_record(value, raw, start, end)
                except (ValueError, TypeError, KeyError, AttributeError, RuntimeError, OverflowError) as exc:
                    # A malformed end record must not be skipped on the next
                    # poll, leaving an old start looking unsettled forever.
                    self.offset = start
                    self.observed_turn = None
                    self.lifecycle_error = 'malformed_native_record'
                    raise RuntimeError('malformed native rollout record') from exc
                # Commit only after all semantics are checked. Otherwise a
                # bad session_meta or compact record could be skipped next poll.
                self.offset = end
        return self.snapshot()

    def observe_record(self, value, raw, start, end):
        outer = value.get('type')
        payload = value['payload']
        typ = payload.get('type')
        if outer == 'session_meta':
            sid = payload.get('id')
            created = epoch(payload.get('timestamp') or value['timestamp'])
            if not isinstance(sid, str) or not sid or not math.isfinite(created) or created <= 0:
                raise ValueError('invalid rollout session metadata')
            if self.session_id is not None and (sid != self.session_id or created != self.session_created_at):
                raise RuntimeError('rollout session metadata changed')
            self.session_id, self.session_created_at = sid, created
        elif outer == 'compacted':
            self.compacted_at = epoch(value['timestamp'])
            self.compacted_offset = start
            self.replacement_items = len(payload.get('replacement_history') or [])
            self.compaction_occurrence = None
        elif outer == 'response_item' and typ == 'message' and payload.get('role') == 'user':
            self.user_offset = start
            self.user_at = epoch(value['timestamp'])
            content = payload.get('content')
            if (isinstance(content, list) and len(content) == 1
                    and isinstance(content[0], dict)
                    and set(content[0]) == {'type', 'text'}
                    and content[0]['type'] == 'input_text'
                    and isinstance(content[0]['text'], str)):
                data = content[0]['text'].encode('utf-8')
                identity = (hashlib.sha256(data).hexdigest(), len(data))
                if self.receipt_count >= 32768:
                    # Do not silently evict a receipt that a durable
                    # unresolved input may still need after a restart.
                    self.receipt_overflow = True
                else:
                    self.native_inputs.setdefault(identity, []).append({
                        'offset': start, 'end_offset': end,
                        'record_sha256': hashlib.sha256(raw).hexdigest(),
                        'payload_sha256': identity[0], 'payload_bytes': identity[1],
                        'at': self.user_at, 'timestamp': value['timestamp'],
                        'metadata': payload.get('internal_chat_message_metadata_passthrough'),
                    })
                    self.receipt_count += 1
        elif outer == 'event_msg' and typ == 'user_message':
            # Activity can veto control, but is not a receipt. In
            # particular, never add event_msg or queues to native_inputs.
            self.user_offset = start
            self.user_at = epoch(value['timestamp'])
        elif outer == 'response_item' and typ in {'function_call_output', 'custom_tool_call_output'}:
            self.tool_result_at = epoch(value['timestamp'])
            self.tool_result_offset = start
        elif outer == 'turn_context':
            if (not isinstance(payload.get('turn_id'), str) or not payload['turn_id']
                    or not isinstance(payload.get('model'), str) or not payload['model']
                    or 'effort' not in payload
                    or not (payload['effort'] is None or isinstance(payload['effort'], str)
                            and bool(payload['effort']))):
                raise ValueError('unbound native turn context')
            self.turn_context = dict(turn_id=payload['turn_id'], model=payload['model'],
                                     effort=payload.get('effort'), offset=start)
        elif outer == 'event_msg' and typ in {'item_started', 'item_completed'}:
            item = payload.get('item')
            if isinstance(item, dict) and item.get('type') == 'ContextCompaction':
                self.observe_compaction_item(typ, payload, value.get('timestamp'), start, raw)
        elif outer == 'event_msg' and typ in {'task_started', 'task_complete', 'turn_aborted'}:
            self.observe_lifecycle(typ, payload.get('turn_id'), value.get('timestamp'), start)

    def observe_compaction_item(self, typ, payload, timestamp, offset, raw):
        item = payload['item']
        occurrence = item.get('id')
        turn = self.observed_turn or {}
        if (not isinstance(occurrence, str) or str(uuid.UUID(occurrence)) != occurrence
                or payload.get('thread_id') != self.session_id
                or payload.get('turn_id') != turn.get('id')
                or turn.get('status') != 'started_without_end_record'):
            raise ValueError('native compaction item identity changed')
        if typ == 'item_completed':
            if self.compaction_occurrence and self.compaction_occurrence['id'] == occurrence:
                self.compaction_occurrence = None
            return
        millis = payload.get('started_at_ms')
        at = epoch(timestamp)
        if (type(millis) is not int or millis <= 0 or not math.isfinite(at)
                or millis / 1000 < turn['started_at'] - .001
                or millis / 1000 > at + 1):
            raise ValueError('native compaction start timestamp invalid')
        if self.compaction_occurrence and self.compaction_occurrence['id'] == occurrence:
            if self.compaction_occurrence['started_at_ms'] != millis:
                raise ValueError('same native occurrence changed its deadline')
            return
        if occurrence in self.seen_compaction_occurrences:
            raise ValueError('completed or replaced native occurrence cannot be rearmed')
        if len(self.seen_compaction_occurrences) >= 1024:
            raise ValueError('native compaction occurrence history exceeds its bound')
        self.seen_compaction_occurrences.add(occurrence)
        self.compaction_occurrence = dict(id=occurrence, turn_id=payload['turn_id'],
            thread_id=payload['thread_id'], started_at_ms=millis, started_at=millis / 1000,
            offset=offset, record_sha256=hashlib.sha256(raw).hexdigest())

    def observe_lifecycle(self, typ, turn_id, timestamp, offset):
        previous, self.observed_turn = self.observed_turn, None
        self.compaction_occurrence = None
        try:
            at = epoch(timestamp)
        except (AttributeError, TypeError, ValueError, OverflowError):
            self.lifecycle_error = 'invalid_lifecycle_timestamp'
            return
        if not math.isfinite(at) or at < self.lifecycle_at:
            self.lifecycle_error = 'out_of_order_lifecycle'
            return
        self.lifecycle_at = at
        if not isinstance(turn_id, str) or not turn_id:
            self.lifecycle_error = 'unbound_lifecycle'
            return
        if typ == 'task_started':
            if turn_id == self.last_turn_id:
                self.lifecycle_error = 'duplicate_turn_start'
                return
            self.last_turn_id, self.native_task_at = turn_id, at
            self.seen_compaction_occurrences.clear()
            self.observed_turn = dict(id=turn_id, started_at=at, start_offset=offset,
                                      status='started_without_end_record')
        elif previous and turn_id == previous['id']:
            self.observed_turn = dict(previous, status=typ, ended_at=at, end_offset=offset)
        else:
            self.lifecycle_error = 'unmatched_terminal_event'
            return
        self.lifecycle_error = None

    def snapshot(self):
        stat = self.path.stat()
        if (stat.st_dev, stat.st_ino) != self.file_id or stat.st_size < self.offset:
            raise RuntimeError('original rollout changed')
        return {'path': str(self.path), 'file_id': list(self.file_id), 'offset': self.offset,
                'caught_up': self.offset == stat.st_size,
                'receipt_overflow': self.receipt_overflow,
                'observed_turn': dict(self.observed_turn) if self.observed_turn else None,
                'lifecycle_error': self.lifecycle_error,
                'session_id': self.session_id, 'session_created_at': self.session_created_at,
                'compaction_occurrence': dict(self.compaction_occurrence) if self.compaction_occurrence else None,
                'turn_context': dict(self.turn_context) if self.turn_context else None,
                'compacted_at': self.compacted_at, 'compacted_offset': self.compacted_offset,
                'replacement_items': self.replacement_items,
                'user_offset': self.user_offset, 'user_at': self.user_at,
                'tool_result_at': self.tool_result_at, 'tool_result_offset': self.tool_result_offset}


def unsettled_compaction(sid, facts, db_path=LOG_DB):
    """Evidence of an unclosed compact, without claiming an active native turn.

    The rollout must be caught up and show a start with no matching terminal
    event. A missing end record can also mean a stalled writer; this result must
    never authorize an interrupt, a compact request, or an input receipt.
    """
    observed = facts.get('observed_turn') or {}
    if (not facts.get('caught_up') or facts.get('session_id') != sid
            or facts.get('lifecycle_error')
            or observed.get('status') != 'started_without_end_record'
            or not isinstance(observed.get('id'), str) or not observed['id']
            or type(observed.get('started_at')) not in (float, int)
            or not math.isfinite(observed['started_at'])
            or observed['started_at'] < (facts.get('session_created_at') or 0)):
        return None
    # Reuse the log classifier's shape locally. This synthetic shape is not
    # returned as RPC metadata and is never passed to the control state machine.
    evidence_turn = {'id': observed['id'], 'startedAt': observed['started_at'],
                     'status': 'inProgress'}
    found = compaction_start(sid, evidence_turn, facts['compacted_at'], db_path)
    return dict(found, active_turn_verified=False) if found else None


def compaction_start(sid, turn, completed_at, db_path=LOG_DB):
    if not turn or turn.get('status') != 'inProgress':
        return None
    # Every query uses the thread/time index; none scans the shared whole log.
    natural_boundary = max(float(turn['startedAt']) - .001, completed_at)
    # Compact submission may precede queued turn creation by minutes. Use the
    # last summary boundary and exact submission id, never a guessed time skew.
    manual_marker = f'Submission sub=Submission {{ id: "{turn["id"]}", op: Compact,'
    turn_pattern = '%' + turn['id'].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
    with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=.15)) as db:
        deadline = time.monotonic() + .8
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        rows = []
        for boundary, clause, extra in (
            (natural_boundary, "target='codex_core::session::turn' AND feedback_log_body LIKE '%post sampling token usage%' AND feedback_log_body LIKE '% token_limit_reached=true%' AND feedback_log_body LIKE '% needs_follow_up=true%'", ()),
            (completed_at, "target IN ('codex_core::session::handlers','codex_core::codex') AND instr(feedback_log_body, ?) > 0", (manual_marker,)),
            (natural_boundary, "target IN ('codex_mcp::connection_manager::tool_catalog','codex_http_client::client','feedback_tags','codex_core::session::turn','codex_core::compact','codex_core::client','codex_api::endpoint::responses') AND feedback_log_body LIKE '%run_pre_sampling_compact:%'", ()),
        ):
            seconds, nanos = divmod(round(boundary * 1_000_000_000), 1_000_000_000)
            found = db.execute(
                'SELECT id,ts,ts_nanos,target,feedback_log_body FROM logs INDEXED BY idx_logs_thread_id_ts '
                'WHERE thread_id=? AND ts>=? AND (ts>? OR ts_nanos>?) '
                "AND feedback_log_body LIKE ? ESCAPE '\\' AND (" + clause + ') '
                'ORDER BY ts,ts_nanos LIMIT 8',
                (sid, seconds, seconds, nanos, turn_pattern, *extra)).fetchall()
            rows.extend({'id': x[0], 'at': x[1] + x[2] / 1e9, 'target': x[3], 'body': x[4],
                         'thread_id': sid} for x in found)
    return classify_start(rows, turn, completed_at)


def latest_input(sid, since, db_path=LOG_DB):
    """Submission includes inputs still waiting before their rollout receipt.

    Return identifiers only; never print or persist submitted user text.
    The bounded indexed query deliberately matches the actual operation, not
    the ubiquitous turn_input span on unrelated HTTP/reasoning messages.
    """
    with closing(sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=.15)) as db:
        deadline = time.monotonic() + .8
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
        row = db.execute(
            'SELECT id,ts,ts_nanos,substr(feedback_log_body,1,400) '
            'FROM logs INDEXED BY idx_logs_thread_id_ts WHERE thread_id=? AND ts>=? '
            "AND target IN ('codex_core::session::handlers','codex_core::codex') "
            "AND feedback_log_body LIKE '%Submission {%op: TurnInput {%' "
            'ORDER BY ts DESC,ts_nanos DESC LIMIT 1', (sid, int(since))).fetchone()
    if row is None:
        return None
    match = re.search(r'Submission \{ id: "([^"]+)"', row[3])
    if not match:
        raise RuntimeError('unrecognized native input submission; observe only')
    return {'log_id': row[0], 'at': row[1] + row[2] / 1e9, 'submission_id': match[1]}


def may_interrupt(episode, snapshot):
    """Final operation boundary; uncertainty and operator activity always win."""
    from ccc_compact_inputs import clear_guard
    if (not snapshot.get('binding_ok') or snapshot.get('paused')
            or not clear_guard(snapshot)
            or snapshot['input_guard']['token'] != episode.get('input_token')):
        return False
    turn = snapshot.get('turn') or {}
    facts = snapshot['facts']
    compact = snapshot.get('compact')
    return bool(
        turn.get('id') == episode['turn_id'] and turn.get('status') == 'inProgress'
        and facts['caught_up'] and facts['path'] == episode['path']
        and facts['file_id'] == episode['file_id']
        and facts['compacted_at'] < episode['started_at']
        and facts['user_offset'] == episode['user_offset']
        and compact and compact['turn_id'] == episode['turn_id']
        and compact['key'] == episode['compact_key']
        and episode.get('compaction_occurrence') is not None
        and compact.get('compaction_occurrence') == episode['compaction_occurrence']
        and snapshot.get('client_capability') == 'thread-compact-interrupt-v4')


def native_compaction_start(sid, turn, facts):
    occurrence = facts.get('compaction_occurrence') or {}
    if (not facts.get('caught_up') or facts.get('lifecycle_error')
            or facts.get('session_id') != sid or occurrence.get('thread_id') != sid
            or not turn or turn.get('status') != 'inProgress'
            or occurrence.get('turn_id') != turn.get('id')
            or not isinstance(occurrence.get('id'), str)):
        return None
    return dict(key=f"{sid}:{turn['id']}:{occurrence['id']}",
                turn_id=turn['id'], compaction_occurrence=occurrence['id'],
                started_at=occurrence['started_at'], source='native_compaction_item',
                start_offset=occurrence['offset'], record_sha256=occurrence['record_sha256'],
                manual_present=False, candidates=[])

"""Bind the existing native Claude journal to individual Herdr foreground agents.

This reuses CCC's completion, provider error and native transcript readers.
No lifecycle is manufactured and no legacy event, receipt or budget is changed.
Herdr labels and startup argv are display hints, never current session authority.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid
from datetime import datetime
from functools import lru_cache

EVENTS = frozenset({'SessionStart', 'UserPromptSubmit', 'Stop', 'StopFailure'})
MAX_JOURNAL = 64 * 1024 * 1024
MAX_APPEND = 8 * 1024 * 1024
MAX_LINE = 64000


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


@lru_cache(maxsize=4096)
def _uuid_matches(value, uppercase):
    canonical_id = str(uuid.UUID(value))
    return value == (canonical_id.upper() if uppercase else canonical_id)


def _event_uuid(value, *, uppercase=False):
    return isinstance(value, str) and len(value) == 36 and _uuid_matches(value, uppercase)


def valid_event(event, now):
    at = event.get('created_at') if isinstance(event, dict) else None
    try:
        return (event.get('version') == 1 and event.get('event_name') in EVENTS
                and type(event.get('agent_pid')) is int and event['agent_pid'] > 0
                and type(at) in (int, float) and math.isfinite(at) and 0 < at <= now + 1
                and isinstance(event.get('event_id'), str) and 0 < len(event['event_id']) <= 160
                and _event_uuid(event['session_id'])
                and _event_uuid(event['surface_id'], uppercase=True)
                and _event_uuid(event['workspace_id'], uppercase=True)
                and not any(event.get(k) for k in ('synthetic', 'synthetic_fallback',
                    'provider_migration_source_event_id', 'report_migration_source_event_id',
                    'dispatch_migration_source_event_id')))
    except (ValueError, KeyError, TypeError, AttributeError):
        return False


class NativeJournal:
    """Bounded initial read, append-only updates, and per-PID lifecycle isolation."""

    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.identity, self.offset = None, 0
        self.last_change, self.boundary = None, b''
        self.entries, self.error = {}, 'native_journal_not_read'

    def refresh(self):
        try:
            first = self.path.lstat()
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                info = os.fstat(fd)
                identity = (info.st_dev, info.st_ino)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or info.st_mode & 0o022
                        or (first.st_dev, first.st_ino) != identity):
                    raise ValueError('Untrusted native journal')
                reset = identity != self.identity or info.st_size < self.offset
                change = (info.st_mtime_ns, info.st_ctime_ns)
                if not reset and info.st_size == self.offset:
                    reset = change != self.last_change
                if not reset and self.boundary:
                    stream.seek(self.offset - len(self.boundary))
                    reset = stream.read(len(self.boundary)) != self.boundary
                if reset:
                    start, entries = max(0, info.st_size - MAX_JOURNAL), {}
                else:
                    start, entries = self.offset, dict(self.entries)
                    if info.st_size - start > MAX_APPEND:
                        # Recover a bounded actual tail. Missing SessionStart
                        # remains UNKNOWN; never synthesize a lifecycle.
                        start, entries = max(0, info.st_size - MAX_JOURNAL), {}
                stream.seek(start)
                raw = stream.read(info.st_size - start)
                if start:
                    stream.seek(start - 1)
                    if stream.read(1) != b'\n':
                        boundary = raw.find(b'\n')
                        if boundary < 0:
                            raise ValueError('Native journal tail has no complete record')
                        start += boundary + 1
                        raw = raw[boundary + 1:]
                if raw and not raw.endswith(b'\n'):
                    raise ValueError('Native journal has a partial final record')
                stream.seek(max(0, info.st_size - 256))
                boundary = stream.read(256)
                after = os.fstat(fd)
                current = self.path.lstat()
                if ((after.st_dev, after.st_ino) != identity
                        or (current.st_dev, current.st_ino) != identity
                        or after.st_size != info.st_size or current.st_size != info.st_size
                        or (after.st_mtime_ns, after.st_ctime_ns) != change
                        or (current.st_mtime_ns, current.st_ctime_ns) != change):
                    raise ValueError('Native journal changed during read')
            now, offset = self.clock(), start
            for raw_line in raw.splitlines(keepends=True):
                line_offset = offset
                offset += len(raw_line)
                if len(raw_line) > MAX_LINE:
                    continue
                try:
                    event = json.loads(raw_line)
                except (ValueError, UnicodeError):
                    continue
                if not valid_event(event, now):
                    continue
                key = (event['surface_id'], event['workspace_id'], event['agent_pid'])
                row = dict(entries.get(key, {}))
                previous = row.get('latest', {}).get('event', {})
                high_water = max(row.get('chronology_high_water', 0), previous.get('created_at', 0))
                if event['created_at'] < high_water:
                    row['chronology_unknown'] = True
                row['chronology_high_water'] = max(high_water, event['created_at'])
                proof = {'event': event, 'offset': line_offset, 'bytes': len(raw_line),
                         'sha256': digest(raw_line), 'journal_path': str(self.path),
                         'journal_device': identity[0], 'journal_inode': identity[1]}
                row['latest'] = proof
                if event['event_name'] == 'SessionStart':
                    # A later real lifecycle supersedes uncertainty from an
                    # older one. Do not forgive an out-of-order start against
                    # the newest timestamp seen, even if the prior row is old.
                    if event['created_at'] >= high_water:
                        row.pop('chronology_unknown', None)
                    row['start'] = proof
                    row['prompt'] = None
                    row['prompt_history'] = []
                elif event['event_name'] == 'UserPromptSubmit':
                    row['prompt_history'] = [*row.get('prompt_history', []), proof][-16:]
                    row['prompt'] = proof
                entries[key] = row
                if len(entries) > 4096:
                    oldest = min(entries, key=lambda k: entries[k]['latest']['event']['created_at'])
                    del entries[oldest]
            self.identity, self.offset, self.entries, self.error = identity, offset, entries, None
            self.last_change, self.boundary = change, boundary
            return True
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            self.error = str(exc)[:160]
            return False

    def lookup(self, endpoint, process):
        if self.error:
            return None, self.error
        key = (endpoint['cmux_surface_id'], endpoint['cmux_workspace_id'], process['pid'])
        row = self.entries.get(key, {})
        start = row.get('start', {}).get('event', {})
        latest = row.get('latest', {}).get('event', {})
        born = process['birth'][0] + process['birth'][1] / 1e6
        if (row.get('chronology_unknown') or not start or not latest
                or not born <= start['created_at'] <= latest['created_at'] <= self.clock() + 1
                or start['session_id'] != latest['session_id']):
            return None, 'native_start_or_current_session_unverified'
        return row, None


def queued_prompt_boundary(records, index, prompt, session):
    """Bind a delayed human receipt to its original Hook and native queue.

    The queue supplies timing provenance only. A unique, exact mainline user
    message supplies receipt; neither enqueue nor dequeue is an input receipt.
    """
    import claude_dispatch_recovery as dispatch
    import claude_provider_native_evidence as provider
    receipt, _, _ = records[index]
    if (prompt.get('prompt_kind') != 'human' or receipt.get('promptSource') != 'queued'
            or receipt.get('origin') != {'kind': 'human'}
            or receipt.get('turnOrigin') != 'human'
            or receipt.get('isMeta') or receipt.get('isSynthetic')
            or receipt.get('isCompactSummary')):
        return None
    try:
        if str(uuid.UUID(receipt['uuid'])) != receipt['uuid']:
            return None
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    received = provider._epoch(receipt.get('timestamp'))
    if not prompt['created_at'] <= received <= time.time() + 1:
        return None
    text = dispatch._text(receipt)
    enqueue_keys = {'type', 'operation', 'timestamp', 'sessionId', 'content'}
    dequeue_keys = {'type', 'operation', 'timestamp', 'sessionId'}
    enqueues = []
    for position, (item, offset, sha) in enumerate(records[:index]):
        if (item.get('type') == 'queue-operation' and item.get('operation') == 'enqueue'
                and item.get('sessionId') == session and item.get('content') == text
                and abs(provider._epoch(item.get('timestamp')) - prompt['created_at']) <= 2):
            if set(item) != enqueue_keys:
                return None
            enqueues.append((position, item, offset, sha))
    if len(enqueues) != 1:
        return None
    position, enqueue, enqueue_offset, enqueue_sha = enqueues[0]
    queued_at = provider._epoch(enqueue['timestamp'])
    dequeues = []
    for item, offset, sha in records[position + 1:index]:
        if item.get('type') != 'queue-operation':
            continue
        if item.get('sessionId') != session:
            return None
        if item.get('operation') == 'dequeue':
            if (set(item) != dequeue_keys
                    or not queued_at <= provider._epoch(item['timestamp']) <= received
                    or abs(provider._epoch(item['timestamp']) - received) > 2):
                return None
            dequeues.append({'offset': offset, 'sha256': sha})
        elif item.get('operation') != 'enqueue' or not dequeues:
            # An interposed enqueue before the selected dequeue or an unknown
            # operation makes the native queue mapping ambiguous.
            return None
    if len(dequeues) != 1:
        return None
    return {'queue_bound': True, 'native_received_at': received,
            'enqueue': {'offset': enqueue_offset, 'sha256': enqueue_sha},
            'dequeue': dequeues[0]}


def current_turn_records(records, prompt, session):
    """A real Hook-bound prompt supersedes queues from earlier native turns.

    Native scheduled prompts may be isMeta; they provide a turn boundary only.
    They never count as receipt of a CCC prompt.
    """
    import claude_dispatch_recovery as dispatch
    import claude_provider_native_evidence as provider
    if not prompt or not prompt.get('message_hash'):
        return records, None
    matches = []
    for index, (item, offset, sha) in enumerate(records):
        message = item.get('message')
        if (item.get('type') != 'user' or item.get('isSidechain') is not False
                or item.get('sessionId') != session or item.get('isSynthetic')
                or item.get('isCompactSummary') or not isinstance(message,dict)
                or message.get('role') != 'user'):
            continue
        text = dispatch._text(item)
        if text is None or dispatch.digest(text) != prompt['message_hash']:
            continue
        direct = abs(provider._epoch(item.get('timestamp')) - prompt['created_at']) <= 2
        queued = None if direct else queued_prompt_boundary(records, index, prompt, session)
        if direct or queued is not None:
            proof = {'native_uuid':item.get('uuid'),'offset':offset,
                     'sha256':sha,'isMeta':item.get('isMeta') is True}
            if queued is not None:
                proof.update(queued)
            matches.append((index, proof))
    if len(matches) != 1:
        raise ValueError('Original prompt is not uniquely bound to this native turn')
    index, proof = matches[0]
    return records[index:], proof


DISPLAY_METADATA = frozenset({'last-prompt','ai-title','mode','permission-mode','atis-latch','pr-link'})


def background_notification_boundary(records, row):
    """Bind one absorbed system task event, preserving its preceding real input.

    This never turns an enqueue, attachment or background task into a user
    receipt. The prior original Hook must independently match a native user.
    """
    import claude_dispatch_recovery as dispatch
    import claude_provider_native_evidence as provider
    latest = row.get('prompt') or {}
    history = row.get('prompt_history', [])
    event = latest.get('event', {})
    session = event.get('session_id')
    matches = [(i, item) for i, (item, _, _) in enumerate(records)
               if item.get('type') == 'attachment'
               and isinstance(item.get('attachment'), dict)
               and isinstance(item['attachment'].get('prompt'), str)
               and dispatch.digest(item['attachment']['prompt']) == event.get('message_hash')]
    if not matches:
        return None
    if len(matches) != 1 or len(history) < 2 or history[-1] != latest:
        raise ValueError('Background task lacks one original previous prompt')
    index, item = matches[0]
    attachment = item['attachment']
    expected_keys = {'type', 'prompt', 'source_uuid', 'delivery_id', 'commandMode',
                     'origin', 'runId', 'timestamp'}
    if (set(attachment) != expected_keys or attachment['type'] != 'queued_command'
            or attachment['commandMode'] != 'task-notification'
            or attachment['origin'] != {'kind':'task-notification','producer':'session-task'}
            or item.get('renderedRole') != 'system' or item.get('isSidechain') is not False
            or item.get('sessionId') != session or item.get('session_id',session) != session
            or item.get('isSynthetic') or item.get('isCompactSummary')
            or not isinstance(attachment['runId'],str) or not attachment['runId']
            or item.get('timestamp') != attachment['timestamp'] or index == 0):
        raise ValueError('Background task native origin or schema changed')
    try:
        for key in ('source_uuid','delivery_id'):
            if str(uuid.UUID(attachment[key])) != attachment[key]:
                raise ValueError('Background task has invalid identity')
        if str(uuid.UUID(item['uuid'])) != item['uuid']:
            raise ValueError('Background attachment has invalid identity')
    except (KeyError,TypeError,AttributeError) as exc:
        raise ValueError('Background task identity unavailable') from exc
    remove = records[index-1][0]
    remove_keys = {'type','operation','timestamp','sessionId','content','reason','commandUuid','deliveryId'}
    if (set(remove) != remove_keys or remove['type'] != 'queue-operation'
            or remove['operation'] != 'remove' or remove['reason'] != 'absorbed_mid_turn'
            or remove['sessionId'] != session or remove['content'] != attachment['prompt']
            or remove['commandUuid'] != attachment['source_uuid']
            or remove['deliveryId'] != attachment['delivery_id']
            or abs(provider._epoch(remove['timestamp']) - event['created_at']) > 2):
        raise ValueError('Background task removal is not bound to the original Hook')
    enqueue_keys = {'type','operation','timestamp','sessionId','content'}
    enqueues = [(i,r) for i,(r,_,_) in enumerate(records[:index-1])
                if r.get('type')=='queue-operation' and r.get('operation')=='enqueue'
                and r.get('sessionId')==session and r.get('content')==attachment['prompt']]
    if len(enqueues) != 1 or set(enqueues[0][1]) != enqueue_keys:
        raise ValueError('Background task enqueue is not uniquely bound')
    enqueue_index, enqueue = enqueues[0]
    parent = next((r for r,_,_ in reversed(records[:index-1]) if r.get('uuid')), None)
    if (parent is None or item.get('parentUuid') != parent.get('uuid')
            or parent.get('sessionId') != session or parent.get('isSidechain') is not False
            or any(r.get('type')=='queue-operation' for r,_,_ in records[enqueue_index+1:index-1])):
        raise ValueError('Background task parent or queue sequence changed')
    at = provider._epoch(item['timestamp'])
    if (abs(provider._epoch(enqueue['timestamp'])-at)>2
            or not 0<at<=event['created_at']<=provider._epoch(remove['timestamp'])+2
            or provider._epoch(remove['timestamp'])>time.time()+1):
        raise ValueError('Background task chronology changed')
    previous = history[-2]
    original = previous.get('event',{})
    if (original.get('event_name')!='UserPromptSubmit' or original.get('session_id')!=session
            or not row['start']['event']['created_at']<=original['created_at']<=at
            or any(original.get(k)!=event.get(k) for k in ('agent_pid','surface_id','workspace_id','transcript_id'))):
        raise ValueError('Background task previous input belongs to another lifecycle')
    selected, proof = current_turn_records(records, original, session)
    if proof is None or proof.get('isMeta') or records[enqueue_index][1] <= proof['offset']:
        raise ValueError('Background task does not follow a genuine original prompt')
    # A new real input or any unrecognized queue event remains authoritative.
    # Only this exact system notification's three records leave the queue check.
    for record, offset, _ in selected[1:]:
        if record.get('type')=='user' and record.get('isSidechain') is False:
            text=dispatch._text(record)
            if text is not None:
                raise ValueError('A later native input supersedes the background task boundary')
    excluded={records[i][1] for i in (enqueue_index,index-1,index)}
    queue_view=[r for r in selected if r[1] not in excluded]
    note={'schema':'herdr-native-system-task-boundary-v1','user_receipt':False,
          'notification_hook':latest,
          'native_records':[{'offset':records[i][1],'sha256':records[i][2]} for i in (enqueue_index,index-1,index)],
          'native_attachment_uuid':item['uuid'],'source_uuid':attachment['source_uuid'],
          'delivery_id':attachment['delivery_id']}
    return selected, proof, previous, queue_view, note


def _human_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _human_mainline(item, session):
    return (item.get('isSidechain') is False and item.get('sessionId') == session
            and item.get('session_id', session) == session
            and not any(item.get(k) for k in ('isMeta', 'isSynthetic', 'isCompactSummary'))
            and _human_uuid(item.get('uuid')))


def _human_tool_results(item):
    message = item.get('message', {})
    content = message.get('content') if isinstance(message, dict) else None
    if (item.get('type') != 'user' or not isinstance(message, dict) or message.get('role') != 'user'
            or not isinstance(content, list) or not content
            or any(not isinstance(b, dict) or b.get('type') != 'tool_result'
                   or not isinstance(b.get('tool_use_id'), str) or not b['tool_use_id']
                   or set(b) not in ({'type', 'tool_use_id', 'content', 'is_error'},
                                     {'type', 'tool_use_id', 'content'})
                   or not isinstance(b['content'], str)
                   or ('is_error' in b and type(b['is_error']) is not bool)
                   for b in content)):
        return None
    ids = [b['tool_use_id'] for b in content]
    return set(ids) if len(set(ids)) == len(ids) else None


def absorbed_human_prompt_boundary(records, row, process):
    """Recognize one current human input absorbed during its original tool turn.

    This is an input boundary, never a CCC receipt. Keep the latest human Hook
    and every interleaved record; only this proven queue leaves the generic
    unresolved-queue check. The task-notification contract stays separate.
    """
    import claude_dispatch_recovery as dispatch
    import claude_provider_native_evidence as provider
    latest = row.get('prompt') or {}
    prompt = latest.get('event', {})
    matches = [(i, r) for i, (r, _, _) in enumerate(records)
               if r.get('type') == 'attachment' and isinstance(r.get('attachment'), dict)
               and isinstance(r['attachment'].get('prompt'), str)
               and dispatch.digest(r['attachment']['prompt']) == prompt.get('message_hash')]
    if not any('humanTurn' in r['attachment'] or r['attachment'].get('commandMode') == 'prompt'
               or r['attachment'].get('origin') == {'kind': 'human'} for _, r in matches):
        return None
    if len(matches) != 1:
        raise ValueError('Absorbed human prompt is not unique')
    index, item = matches[0]
    attachment = item['attachment']
    start, stop = row.get('start', {}).get('event', {}), row['latest']['event']
    history = row.get('prompt_history', [])
    session, now = prompt.get('session_id'), time.time()
    born = process['birth'][0] + process['birth'][1] / 1e6
    identity = ('agent_pid', 'surface_id', 'workspace_id', 'session_id', 'transcript_id')
    if (not history or history[-1] != latest or prompt.get('prompt_kind') != 'human'
            or prompt.get('event_name') != 'UserPromptSubmit'
            or start.get('event_name') != 'SessionStart'
            or stop.get('event_name') not in {'Stop', 'StopFailure'}
            or not all(valid_event(e, now) for e in (start, prompt, stop))
            or prompt.get('agent_pid') != process['pid']
            or not isinstance(prompt.get('transcript_id'), str) or not prompt['transcript_id']
            or any(e.get(k) != prompt.get(k) for e in (start, stop) for k in identity)
            or not born <= start['created_at'] <= prompt['created_at'] <= stop['created_at']):
        raise ValueError('Absorbed human prompt is outside the current original lifecycle')
    keys = {'type', 'prompt', 'source_uuid', 'delivery_id', 'commandMode',
            'origin', 'timestamp', 'humanTurn'}
    if (set(attachment) != keys or attachment['type'] != 'queued_command'
            or attachment['commandMode'] != 'prompt' or attachment['origin'] != {'kind': 'human'}
            or attachment['humanTurn'] is not True or item.get('renderedRole') != 'system'
            or not _human_mainline(item, session) or item.get('version') != '2.1.303'
            or item.get('timestamp') != attachment['timestamp']
            or not all(_human_uuid(attachment[k]) for k in ('source_uuid', 'delivery_id'))):
        raise ValueError('Absorbed human prompt origin or schema changed')
    id_matches = [r for r, _, _ in records if isinstance(r.get('attachment'), dict)
                  and (r['attachment'].get('source_uuid') == attachment['source_uuid']
                       or r['attachment'].get('delivery_id') == attachment['delivery_id'])]
    if len(id_matches) != 1 or len({attachment['source_uuid'], attachment['delivery_id'], item['uuid']}) != 3:
        raise ValueError('Absorbed human delivery identity is ambiguous')
    text = attachment['prompt']
    enqueues = [(i, r) for i, (r, _, _) in enumerate(records)
                if r.get('type') == 'queue-operation' and r.get('operation') == 'enqueue'
                and r.get('content') == text]
    removes = [(i, r) for i, (r, _, _) in enumerate(records)
               if r.get('type') == 'queue-operation' and r.get('operation') == 'remove'
               and (r.get('content') == text or r.get('commandUuid') == attachment['source_uuid']
                    or r.get('deliveryId') == attachment['delivery_id'])]
    if len(enqueues) != 1 or len(removes) != 1:
        raise ValueError('Absorbed human queue is not unique')
    enqueue_index, enqueue = enqueues[0]
    remove_index, remove = removes[0]
    if (set(enqueue) != {'type', 'operation', 'timestamp', 'sessionId', 'content'}
            or set(remove) != {'type', 'operation', 'timestamp', 'sessionId', 'content',
                               'reason', 'commandUuid', 'deliveryId'}
            or enqueue['sessionId'] != session or remove['sessionId'] != session
            or remove['content'] != text or remove['reason'] != 'absorbed_mid_turn'
            or remove['commandUuid'] != attachment['source_uuid']
            or remove['deliveryId'] != attachment['delivery_id']
            or not enqueue_index < remove_index < index):
        raise ValueError('Absorbed human queue identity or order changed')
    arrival, queued, absorbed = (provider._epoch(r['timestamp']) for r in (item, enqueue, remove))
    if (not start['created_at'] <= arrival <= prompt['created_at'] <= queued <= absorbed <= now + 1
            or prompt['created_at'] - arrival > 2 or queued - prompt['created_at'] > 2):
        raise ValueError('Absorbed human arrival or absorption time changed')
    # The system attachment follows a real tool result, not a second user text.
    parent = records[index - 1][0]
    tool_ids = _human_tool_results(parent)
    if (not _human_mainline(parent, session) or tool_ids is None
            or item.get('parentUuid') != parent['uuid']
            or parent.get('parentUuid') != parent.get('sourceToolAssistantUUID')
            or not _human_uuid(parent.get('sourceToolAssistantUUID'))):
        raise ValueError('Absorbed human attachment lacks its original tool-result parent')
    ancestors = [(i, r) for i, (r, _, _) in enumerate(records[:index - 1])
                 if r.get('uuid') == parent['sourceToolAssistantUUID']]
    if len(ancestors) != 1:
        raise ValueError('Absorbed human assistant parent evidence is unavailable')
    ancestor_index, ancestor = ancestors[0]
    message = ancestor.get('message', {})
    content = message.get('content') if isinstance(message, dict) else None
    uses = [b for b in content if isinstance(b, dict) and b.get('type') == 'tool_use'] if isinstance(content, list) else []
    if (ancestor.get('type') != 'assistant' or not _human_mainline(ancestor, session)
            or not isinstance(message, dict) or message.get('role') != 'assistant' or not uses
            or any(not isinstance(b.get('id'), str) or not b['id'] for b in uses)
            or len({b['id'] for b in uses}) != len(uses) or {b['id'] for b in uses} != tool_ids):
        raise ValueError('Absorbed human tool result belongs to another assistant')
    names = {b['id']: b.get('name') for b in uses}
    if any('is_error' not in b and names.get(b['tool_use_id']) != 'Read'
           for b in parent['message']['content']):
        raise ValueError('Absorbed human optional tool-result field lacks its Read source')
    if any(sum(r.get('uuid') == identity for r, _, _ in records) != 1
           for identity in (item['uuid'], parent['uuid'], ancestor['uuid'])):
        raise ValueError('Absorbed human parent identity is ambiguous')
    parent_at, ancestor_at = provider._epoch(parent['timestamp']), provider._epoch(ancestor['timestamp'])
    if not start['created_at'] <= ancestor_at <= parent_at <= absorbed or absorbed - parent_at > 2:
        raise ValueError('Absorbed human tool-result time changed')
    previous = ancestor['uuid']
    hook_keys = {'type', 'hookName', 'toolUseID', 'hookEvent', 'content', 'stdout',
                 'stderr', 'exitCode', 'command', 'durationMs'}
    tool_names = {b['id']: b.get('name') for b in uses}
    for position in range(ancestor_index + 1, index - 1):
        interleaved = records[position][0]
        if position in (enqueue_index, remove_index):
            continue
        hook = interleaved.get('attachment', {})
        if (interleaved.get('type') != 'attachment' or not _human_mainline(interleaved, session)
                or interleaved.get('parentUuid') != previous or set(hook) != hook_keys
                or hook.get('type') != 'hook_success' or hook.get('hookEvent') != 'PreToolUse'
                or type(hook.get('exitCode')) is not int or hook['exitCode'] != 0
                or hook.get('toolUseID') not in tool_ids
                or hook.get('hookName') != 'PreToolUse:' + str(tool_names.get(hook.get('toolUseID')))
                or any(not isinstance(hook.get(k), str) for k in ('content', 'stdout', 'stderr', 'command'))
                or type(hook.get('durationMs')) is not int or hook['durationMs'] < 0
                or not ancestor_at <= provider._epoch(interleaved.get('timestamp')) <= parent_at):
            raise ValueError('Absorbed human interleaved tool chain changed')
        previous = interleaved['uuid']
    selected = records[min(enqueue_index, ancestor_index):]
    seen = set()
    for record, _, _ in selected:
        if record.get('type') in {'assistant', 'user', 'attachment'} and not _human_mainline(record, session):
            raise ValueError('Absorbed human mainline identity is unavailable')
        if record.get('uuid'):
            if record['uuid'] in seen or not _human_mainline(record, session):
                raise ValueError('Absorbed human mainline identity is ambiguous')
            seen.add(record['uuid'])
        if record.get('type') == 'user' and _human_tool_results(record) is None:
            raise ValueError('A later native input supersedes the absorbed human boundary')
    excluded = {records[i][1] for i in (enqueue_index, remove_index, index)}
    queue_view = [r for r in selected if r[1] not in excluded]
    proof = {'schema': 'herdr-native-absorbed-human-boundary-r17', 'user_receipt': False,
             'human_queue_bound': True, 'isMeta': False, 'native_uuid': item['uuid'],
             'offset': records[index][1], 'sha256': records[index][2],
             # Removal is a conservative terminal barrier after the separately
             # matched emission, not an exact persistence/model-receipt time.
             'original_prompt': latest, 'arrival_at': arrival, 'queue_removed_at': absorbed,
             'source_uuid': attachment['source_uuid'], 'delivery_id': attachment['delivery_id'],
             'native_records': [{'offset': records[i][1], 'sha256': records[i][2]}
                                for i in (enqueue_index, remove_index, index)],
             'tool_chain': [{'offset': records[i][1], 'sha256': records[i][2]}
                            for i in range(ancestor_index, index)]}
    return selected, proof, queue_view


def _human_request_telemetry(item, session):
    """Validate the observed native log schema, never request delivery/receipt."""
    kind = item.get('type')
    if kind not in {'api-request', 'api-request-blob'}:
        return False
    hex64 = lambda value: isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None
    if item.get('sessionId') != session:
        raise ValueError('Human-boundary request telemetry changed session')
    if kind == 'api-request-blob':
        if (set(item) != {'type', 'sessionId', 'hash', 'message'}
                or not isinstance(item['message'], dict)
                or set(item['message']) != {'role', 'content'}
                or item['message']['role'] not in {'user', 'assistant', 'system'}
                or not isinstance(item['message']['content'], (str, list))
                or not hex64(item['hash']) or digest(canonical(item['message'])) != item['hash']):
            raise ValueError('Human-boundary request blob shape or digest changed')
    else:
        keys = {'type', 'sessionId', 'id', 'timestamp', 'version', 'querySource', 'shapeHash',
                'params', 'base', 'keep', 'tail', 'cacheControl', 'requestDigest'}
        params = item.get('params')
        if (set(item) != keys or not _human_uuid(item['id']) or not _human_uuid(item['base'])
                or item['version'] != '2.1.303' or item['querySource'] != 'repl_main_thread'
                or not hex64(item['shapeHash']) or not hex64(item['requestDigest'])
                or type(item['keep']) is not int or item['keep'] < 0
                or not isinstance(item['tail'], list) or not all(hex64(h) for h in item['tail'])
                or not isinstance(params, dict) or set(params) != {
                    'model', 'betas', 'max_tokens', 'thinking', 'context_management', 'output_config'}
                or not isinstance(params['model'], str) or not params['model']
                or not isinstance(params['betas'], list) or not all(isinstance(b, str) for b in params['betas'])
                or type(params['max_tokens']) is not int or params['max_tokens'] <= 0
                or not all(isinstance(params[k], dict) for k in ('thinking', 'context_management', 'output_config'))
                or not isinstance(item['cacheControl'], list)):
            raise ValueError('Human-boundary request telemetry schema changed')
        for entry in item['cacheControl']:
            if (not isinstance(entry, dict) or set(entry) != {'path', 'value'}
                    or not isinstance(entry['path'], list) or not entry['path']
                    or not all(isinstance(k, str) or type(k) is int and k >= 0 for k in entry['path'])
                    or entry['value'] != {'type': 'ephemeral'}):
                raise ValueError('Human-boundary request cache metadata changed')
    return True


def _human_known_context(item, prompt):
    """Only the reviewed text/template variants; context is retained, not input."""
    value = item.get('attachment', {})
    if not isinstance(value, dict):
        raise ValueError('Human-boundary context attachment is not an object')
    kind = value.get('type')
    text = None
    if kind == 'hook_additional_context':
        content = value.get('content')
        if (set(value) != {'type', 'content', 'hookName', 'toolUseID', 'hookEvent'}
                or value['hookName'] != 'UserPromptSubmit' or value['hookEvent'] != 'UserPromptSubmit'
                or not isinstance(value['toolUseID'], str) or not value['toolUseID'].startswith('hook-')
                or not _human_uuid(value['toolUseID'][5:])
                or not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], str)
                or digest(content[0].encode()) != 'c5067e48bbddb1b7267b31597f036ed3a3b4c9a82b4b7586ec0b6f7f2aafe407'):
            raise ValueError('Human prompt additional context is outside the reviewed original text')
        text = 'UserPromptSubmit hook additional context: ' + content[0]
    elif kind in {'total_tokens_reminder', 'silent_turn_reminder'}:
        text = value.get('text')
        if set(value) != {'type', 'text'} or not isinstance(text, str):
            raise ValueError('Human-boundary reminder shape changed')
        if kind == 'total_tokens_reminder':
            if re.fullmatch(r'<total_tokens>[0-9]{1,18} tokens left</total_tokens>', text) is None:
                raise ValueError('Human-boundary token reminder changed template')
        elif digest(text.encode()) != '67664dc70416f261e866ca3f0ebd43aa946b129bb82aacf407684e0f26f06980':
            raise ValueError('Human-boundary silent reminder changed original text')
    elif kind == 'environment':
        snapshot, changes = value.get('snapshot'), value.get('changes')
        keys = {'workingDirectory', 'isWorktree', 'isGitRepo', 'additionalWorkingDirectories',
                'platform', 'shell', 'osVersion'}
        if (set(value) != {'type', 'snapshot', 'changes'} or not isinstance(snapshot, dict)
                or set(snapshot) != keys or not isinstance(snapshot['workingDirectory'], str)
                or not Path(snapshot['workingDirectory']).is_absolute()
                or snapshot['workingDirectory'] != item.get('cwd')
                or type(snapshot['isWorktree']) is not bool or snapshot['isGitRepo'] is not True
                or snapshot['additionalWorkingDirectories'] != [] or snapshot['platform'] != 'darwin'
                or snapshot['shell'] != 'zsh' or not isinstance(snapshot['osVersion'], str)
                or re.fullmatch(r'Darwin [0-9]+\.[0-9]+\.[0-9]+', snapshot['osVersion']) is None
                or not isinstance(changes, list) or len(changes) not in (1, 2)
                or not isinstance(changes[0], dict) or set(changes[0]) != {'field', 'from'}
                or changes[0]['field'] != 'workingDirectory' or not isinstance(changes[0]['from'], str)
                or not Path(changes[0]['from']).is_absolute()
                or (len(changes) == 2 and (changes[1] != {'field': 'isWorktree'} or not snapshot['isWorktree']))):
            raise ValueError('Human-boundary environment change is outside the reviewed schema')
        prefix = ('<system-reminder>\n# Environment update\n - Primary working directory: '
                  + snapshot['workingDirectory'] + ' (was ' + changes[0]['from'] + ')\n')
        rendered = item.get('rendered')
        if (not isinstance(rendered, list) or len(rendered) != 1 or not isinstance(rendered[0], dict)
                or set(rendered[0]) != {'content'} or not isinstance(rendered[0]['content'], str)
                or not rendered[0]['content'].startswith(prefix)):
            raise ValueError('Human-boundary environment rendering changed')
        rest = rendered[0]['content'][len(prefix):]
        if ((len(changes) == 1 and rest != '</system-reminder>')
                or (len(changes) == 2 and digest(rest.encode()) !=
                    '81042bdc41af76e16e1ee3957c87b3150d44921688b9c316987d5e29ed36a8cf')
                or item.get('renderedRole') != 'system'):
            raise ValueError('Human-boundary environment instructions changed')
        return True
    else:
        return False
    if (item.get('renderedRole') != 'system'
            or item.get('rendered') != [{'content': '<system-reminder>\n' + text + '\n</system-reminder>'}]):
        raise ValueError('Human-boundary context rendering changed')
    return True


def _human_response_group(item):
    """Recognize only the observed native per-response block envelope."""
    body = item.get('message')
    if not isinstance(body, dict):
        return None, None
    index, request = item.get('apiBlockIndex'), item.get('requestRef')
    identifier, model = body.get('id'), body.get('model')
    if (type(index) is not int or index < 0 or not _human_uuid(request)
            or not isinstance(identifier, str) or not identifier
            or not isinstance(model, str) or not model
            or item.get('requestedModel') != model):
        return None, None
    return (request, identifier, model), index


def absorbed_human_terminal(records, proof, message, offset, event, trailing):
    """Classify every post-input record, including records beside the parent path.

    Only mainline assistant/tool-result exchanges precede the terminal. Its
    entire remaining suffix must be the original terminal_record-validated
    Stop bookkeeping/display records. Unknown input or a branch stays held.
    """
    import claude_provider_native_evidence as provider
    absorbed = proof['queue_removed_at']
    if (offset <= proof['offset'] or event['created_at'] < absorbed
            or provider._epoch(message.get('timestamp')) < absorbed):
        raise ValueError('Native terminal predates absorbed human input')
    after_terminal = [r for r in records if r[1] > offset]
    if after_terminal != trailing:
        raise ValueError('Absorbed human terminal has unclassified trailing records')
    suffix = [r for r in records if proof['offset'] < r[1] <= offset]
    previous_uuid, previous, last_at = proof['native_uuid'], None, proof['arrival_at']
    prompt = proof['original_prompt']['event']
    tool_sources = {}
    response_group, response_index, response_blocks = None, None, 0
    classified, telemetry = [], []
    blob_hashes, referenced_hashes = set(), set()
    index = 0
    while index < len(suffix):
        item, item_offset, item_sha = suffix[index]
        index += 1
        if _human_request_telemetry(item, event['session_id']):
            if item['type'] == 'api-request':
                at = provider._epoch(item['timestamp'])
                if not prompt['created_at'] <= at <= event['created_at'] + 1:
                    raise ValueError('Human-boundary request telemetry time changed')
                referenced_hashes.update(item['tail'])
            else:
                blob_hashes.add(item['hash'])
            telemetry.append({'offset': item_offset, 'sha256': item_sha, 'type': item['type']})
            continue
        if item.get('type') in DISPLAY_METADATA:
            group = [suffix[index - 1]]
            while index < len(suffix) and suffix[index][0].get('type') in DISPLAY_METADATA:
                group.append(suffix[index]); index += 1
            display_metadata(group, event['session_id'], previous_uuid)
            classified.extend({'offset': o, 'sha256': s, 'type': 'display_metadata'} for _, o, s in group)
            continue
        stamp = provider._epoch(item.get('timestamp'))
        # Hooks created by an earlier block may be drained after a later block.
        # Their creation clock is not the writer's append-order parent clock.
        deferred_hook = False
        value = item.get('attachment')
        if (item.get('type') == 'attachment' and isinstance(value, dict)
                and value.get('type') == 'hook_success'
                and response_group is not None and response_blocks > 1):
            sources = [s for s in tool_sources.values()
                       if any(b.get('type') == 'tool_use' and b.get('id') == value.get('toolUseID')
                              for b in s['message']['content'])]
            if len(sources) == 1:
                source = sources[0]
                deferred_hook = (absorbed <= provider._epoch(source.get('timestamp')) <= stamp
                                 and _human_response_group(source)[0] == response_group)
        if (not _human_mainline(item, event['session_id'])
                or stamp > event['created_at'] + 1
                or (stamp < last_at and not deferred_hook)):
            raise ValueError('Absorbed human suffix has an unknown branch or chronology')
        body = item.get('message')
        if item.get('type') == 'assistant':
            if item.get('parentUuid') != previous_uuid or stamp < absorbed:
                raise ValueError('Absorbed human assistant has an unknown branch or predates removal')
            content = body.get('content') if isinstance(body, dict) else None
            if not isinstance(body, dict) or body.get('role') != 'assistant':
                raise ValueError('Absorbed human suffix assistant schema changed')
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        raise ValueError('Absorbed human suffix has unknown assistant content')
                    kind = block.get('type')
                    valid = ((kind == 'text' and isinstance(block.get('text'), str))
                             or (kind == 'thinking' and isinstance(block.get('thinking'), str))
                             or (kind == 'redacted_thinking' and isinstance(block.get('data'), str))
                             or (kind == 'tool_use' and isinstance(block.get('id'), str) and block['id']
                                 and isinstance(block.get('name'), str) and block['name']
                                 and isinstance(block.get('input'), dict)))
                    if not valid:
                        raise ValueError('Absorbed human suffix has unknown assistant content')
            elif not isinstance(content, str):
                raise ValueError('Absorbed human suffix assistant content unavailable')
            group, block_index = _human_response_group(item)
            continuing = (group is not None and group == response_group
                          and previous is not None and previous.get('type') == 'assistant'
                          and block_index == response_index + 1)
            if continuing:
                response_blocks += 1
            else:
                if tool_sources:
                    raise ValueError('Human response changed before its original tools completed')
                response_blocks = 1
            response_group, response_index = group, block_index
            uses = [b for b in content if b.get('type') == 'tool_use'] if isinstance(content, list) else []
            ids = [b['id'] for b in uses]
            existing_ids = {b['id'] for s in tool_sources.values()
                            for b in s['message']['content'] if b.get('type') == 'tool_use'}
            if len(ids) != len(set(ids)) or existing_ids.intersection(ids):
                raise ValueError('Human response has duplicate original tool IDs')
            if uses:
                tool_sources[item['uuid']] = item
        elif item.get('type') == 'user':
            ids = _human_tool_results(item)
            tool_source = tool_sources.get(item.get('sourceToolAssistantUUID'))
            before = tool_source.get('message', {}) if tool_source is not None else {}
            blocks = before.get('content') if isinstance(before, dict) else None
            uses = [b for b in blocks if isinstance(b, dict) and b.get('type') == 'tool_use'] if isinstance(blocks, list) else []
            if (ids is None or tool_source is None or stamp < absorbed
                    or item.get('sourceToolAssistantUUID') != tool_source['uuid']
                    or item.get('parentUuid') != tool_source['uuid']
                    or not uses or len({b['id'] for b in uses}) != len(uses)
                    or {b['id'] for b in uses} != ids):
                raise ValueError('Absorbed human suffix tool result has no exact original source')
            names = {b['id']: b.get('name') for b in uses}
            if any('is_error' not in b and names.get(b['tool_use_id']) != 'Read'
                   for b in item['message']['content']):
                raise ValueError('Human-boundary optional tool-result field lacks its Read source')
            del tool_sources[tool_source['uuid']]
        elif item.get('type') == 'attachment':
            if item.get('parentUuid') != previous_uuid:
                raise ValueError('Human-boundary attachment has an unknown branch')
            value = item.get('attachment', {})
            if not isinstance(value, dict):
                raise ValueError('Human-boundary attachment is not an object')
            if value.get('type') == 'hook_success':
                sources = [s for s in tool_sources.values()
                           if any(b.get('type') == 'tool_use' and b.get('id') == value.get('toolUseID')
                                  for b in s['message']['content'])]
                tool_source = sources[0] if len(sources) == 1 else None
                blocks = tool_source.get('message', {}).get('content') if tool_source is not None else None
                uses = [b for b in blocks if isinstance(b, dict) and b.get('type') == 'tool_use'] if isinstance(blocks, list) else []
                names = {b['id']: b.get('name') for b in uses}
                keys = {'type', 'hookName', 'toolUseID', 'hookEvent', 'content', 'stdout',
                        'stderr', 'exitCode', 'command', 'durationMs'}
                if (tool_source is None or set(value) != keys or value['hookEvent'] != 'PreToolUse'
                        or value['toolUseID'] not in names
                        or value['hookName'] != 'PreToolUse:' + str(names[value['toolUseID']])
                        or type(value['exitCode']) is not int or value['exitCode'] != 0
                        or value['content'] != '' or value['stderr'] != ''
                        or not isinstance(value['stdout'], str) or not isinstance(value['command'], str)
                        or type(value['durationMs']) is not int or value['durationMs'] < 0):
                    raise ValueError('Human-boundary PreToolUse hook source or schema changed')
            elif _human_known_context(item, prompt):
                if tool_sources:
                    raise ValueError('Human context cannot discard uncompleted original tools')
                if value['type'] == 'hook_additional_context':
                    if previous_uuid != proof['native_uuid'] or abs(stamp - prompt['created_at']) > 2:
                        raise ValueError('Human prompt context is not bound to the original arrival')
                tool_sources.clear()
                response_group, response_index, response_blocks = None, None, 0
            else:
                raise ValueError('Absorbed human suffix contains an unclassified attachment')
        else:
            raise ValueError('Absorbed human suffix contains an unclassified record')
        classified.append({'offset': item_offset, 'sha256': item_sha, 'type': item['type']})
        previous_uuid, previous, last_at = item['uuid'], item, max(last_at, stamp)
    if previous_uuid != message.get('uuid'):
        raise ValueError('Native terminal has no complete absorbed human suffix')
    if not blob_hashes <= referenced_hashes:
        raise ValueError('Human-boundary request blob has no original telemetry reference')
    proof['validated_suffix_records'] = classified
    proof['request_telemetry'] = {'records': telemetry, 'input_authority': False,
                                  'complete_request_verified': False, 'model_receipt': False}


def display_metadata(records, session, leaf):
    """Only the observed display schema, anchored to the same completed leaf."""
    shapes = {'last-prompt':{'type','leafUuid','sessionId'},
              'ai-title':{'type','aiTitle','sessionId'},
              'mode':{'type','mode','sessionId'},
              'permission-mode':{'type','permissionMode','sessionId'},
              'atis-latch':{'type','atis','sessionId'},
              'pr-link':{'type','sessionId','prNumber','prUrl','prRepository','timestamp'}}
    anchors = 0
    for item, _, _ in records:
        kind = item.get('type')
        last_prompt_text = kind == 'last-prompt' and set(item) == shapes[kind] | {'lastPrompt'}
        if (kind not in shapes or (set(item) != shapes[kind] and not last_prompt_text)
                or item.get('sessionId') != session):
            raise ValueError('Trailing display metadata schema or session changed')
        if last_prompt_text and (not isinstance(item['lastPrompt'], str) or len(item['lastPrompt']) > 65536):
            raise ValueError('Trailing last-prompt display text changed shape')
        if kind == 'last-prompt':
            anchors += 1
            try:
                valid = str(uuid.UUID(item['leafUuid'])) == item['leafUuid'] == leaf
            except (ValueError,TypeError,AttributeError):
                valid = False
            if not valid:
                raise ValueError('Trailing display metadata leaf changed')
        elif kind == 'ai-title':
            title = item['aiTitle']
            if not isinstance(title,str) or len(title) > 512 or any(ord(c) < 32 for c in title):
                raise ValueError('Trailing display title changed shape')
        elif kind == 'mode' and item['mode'] != 'normal':
            raise ValueError('Trailing display mode is outside the observed normal mode')
        elif kind == 'permission-mode' and item['permissionMode'] not in {
                'default','acceptEdits','bypassPermissions','dontAsk','auto'}:
            raise ValueError('Trailing permission mode requires separate admission')
        elif kind == 'atis-latch' and item['atis'] != '':
            raise ValueError('Trailing display latch is not empty')
        elif kind == 'pr-link':
            number, repo, stamp = item['prNumber'], item['prRepository'], item['timestamp']
            valid = (type(number) is int and 0 < number <= 2147483647
                and isinstance(repo,str)
                and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}',repo) is not None
                and all(part not in {'.','..'} for part in repo.split('/'))
                and item['prUrl'] == f'https://github.com/{repo}/pull/{number}'
                and isinstance(stamp,str)
                and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z',stamp,re.ASCII) is not None)
            try:
                epoch = datetime.fromisoformat(stamp[:-1] + '+00:00').timestamp() if valid else 0
                valid = valid and math.isfinite(epoch) and 0 < epoch <= time.time() + 1
            except (ValueError,TypeError,OverflowError):
                valid = False
            if not valid:
                raise ValueError('Trailing PR display metadata changed shape')
            # This display timestamp grants no authority. The native assistant,
            # original Hook, process time and unique leaf checks remain below.
    if anchors != 1:
        raise ValueError('Trailing display metadata lacks one original leaf anchor')


def terminal_record(records, event):
    """Recognize completed Stop bookkeeping and a strictly bound display suffix."""
    import claude_provider_native_evidence as provider
    index = len(records) - 1
    metadata = []
    while index >= 0 and records[index][0].get('type') in DISPLAY_METADATA:
        if len(metadata) >= 16:
            raise ValueError('Trailing display metadata exceeds bound')
        metadata.insert(0,records[index])
        index -= 1
    trailing = []
    away = None
    if index >= 0 and records[index][0].get('type') == 'system' and records[index][0].get('subtype') == 'away_summary':
        away = records[index][0]
        shape = {'parentUuid', 'isSidechain', 'type', 'subtype', 'content', 'timestamp',
                 'uuid', 'isMeta', 'userType', 'entrypoint', 'cwd', 'sessionId', 'version', 'gitBranch'}
        if (event['event_name'] != 'Stop' or set(away) not in (shape, shape | {'session_id'})
                or away.get('session_id', event['session_id']) != event['session_id']
                or away.get('isMeta') is not False or not isinstance(away.get('content'), str)
                or not 0 < len(away['content']) <= 32768):
            raise ValueError('Trailing away summary changed native display schema')
        try:
            valid_uuid = str(uuid.UUID(away['uuid'])) == away['uuid']
        except (ValueError, TypeError, AttributeError):
            valid_uuid = False
        if not valid_uuid:
            raise ValueError('Trailing away summary has no native identity')
        # This recap is presentation only. It never supplies reply text, a
        # completion decision, or a prompt receipt. The original assistant and
        # Stop Hook remain the authority after the full parent chain is checked.
        trailing.append(records[index])
        index -= 1
    duration = None
    if index >= 0 and records[index][0].get('type') == 'system' and records[index][0].get('subtype') == 'turn_duration':
        duration = records[index][0]
        trailing.insert(0, records[index])
        index -= 1
    summary = None
    if index >= 0 and records[index][0].get('type') == 'system' and records[index][0].get('subtype') == 'stop_hook_summary':
        summary = records[index][0]
        if (event['event_name'] != 'Stop' or summary.get('preventedContinuation') is not False
                or summary.get('hookErrors') != [] or summary.get('stopReason') not in (None,'')
                or summary.get('hookAdditionalContext',[]) != []):
            raise ValueError('Stop Hook summary requires input or further work')
        trailing.insert(0, records[index])
        index -= 1
        while index >= 0 and records[index][0].get('type') == 'attachment':
            item = records[index][0]
            attachment = item.get('attachment',{})
            if (attachment.get('type') != 'hook_success' or attachment.get('hookEvent') != 'Stop'
                    or attachment.get('exitCode') != 0
                    or attachment.get('toolUseID') != summary.get('toolUseID') or len(trailing) >= 34):
                raise ValueError('Trailing attachment is not completed original Stop bookkeeping')
            trailing.insert(0, records[index])
            index -= 1
    if index < 0:
        raise ValueError('Native terminal message unavailable')
    message, offset, sha = records[index]
    if away is not None and (summary is None or duration is None
            or any(away.get(k) != message.get(k) for k in ('version', 'cwd', 'userType', 'entrypoint'))):
        raise ValueError('Trailing away summary is not attached to completed original Stop bookkeeping')
    previous, stamp = message.get('uuid'), provider._epoch(message.get('timestamp'))
    for item, _, _ in trailing:
        later = provider._epoch(item.get('timestamp'))
        if (item.get('parentUuid') != previous or item.get('sessionId') != event['session_id']
                or item.get('isSidechain') is not False or not item.get('uuid')
                or item.get('isSynthetic') or item.get('isCompactSummary')
                or not stamp <= later <= time.time() + 1):
            raise ValueError('Trailing bookkeeping is not bound to this native terminal message')
        previous, stamp = item['uuid'], later
    if metadata:
        display_metadata(metadata,event['session_id'],previous)
    return message, offset, sha, trailing + metadata


_NATIVE_OBSERVATION_CACHE = {}


def native_observation(row, process, *, allow_cache=False):
    """Validate the actual native transcript behind one original Hook event."""
    import copy
    import ccc_claude_closeout as closeout
    import claude_ccc_protocol as protocol
    import claude_dispatch_recovery as dispatch
    import claude_provider_native_evidence as provider

    event = row['latest']['event']
    result = {'native_reader_available': True, 'native_source': 'original_claude_hook',
              'native_session_id': event['session_id'], 'native_event_id': event['event_id'],
              'native_event_name': event['event_name'], 'native_at': event['created_at'],
              'native_completion_reported': bool(event.get('completed')),
              'native_turn_kind': 'native_activity', 'continuation_eligible': False}
    prompt = (row.get('prompt') or {}).get('event', {})
    protocol_hold = any(prompt.get(k) or event.get(k) for k in (
        'collaboration_protocol', 'handshake_challenge', 'handshake_ack',
        'handshake_provider_mismatch', 'task_dispatch', 'report_ready_task_id'))
    result['native_collaboration_guard'] = protocol_hold
    if event['event_name'] not in {'Stop', 'StopFailure'}:
        result['recovery_reason'] = 'native_turn_open' if event['event_name'] == 'UserPromptSubmit' else 'session_started'
        return result, None
    path = dispatch.native_path(event)
    cache_key = None
    if allow_cache:
        st = path.lstat()
        if stat.S_ISREG(st.st_mode) and path.resolve(strict=True) == path:
            cache_key = (
                str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
                row['latest'].get('sha256'), (row.get('prompt') or {}).get('sha256'),
                process['pid'], tuple(process['birth']), bool(event.get('stop_hook_active')),
            )
            hit = _NATIVE_OBSERVATION_CACHE.get(cache_key)
            if hit is not None:
                return copy.deepcopy(hit[0]), copy.deepcopy(hit[1])
    raw, boundary = closeout.native_tail(path)
    records = provider._records(raw, boundary)
    original_prompt = row.get('prompt')
    try:
        records, prompt_proof = current_turn_records(records, prompt, event['session_id'])
        queue_view, background_proof = records, None
    except ValueError as error:
        if str(error) != 'Original prompt is not uniquely bound to this native turn':
            raise
        human = absorbed_human_prompt_boundary(records, row, process)
        if human is not None:
            records, prompt_proof, queue_view = human
            background_proof = None
        else:
            background = background_notification_boundary(records, row)
            if background is None:
                raise
            records, prompt_proof, original_prompt, queue_view, background_proof = background
            prompt = original_prompt['event']
            protocol_hold = protocol_hold or any(prompt.get(k) for k in (
                'collaboration_protocol', 'handshake_challenge', 'handshake_ack',
                'handshake_provider_mismatch', 'task_dispatch', 'report_ready_task_id'))
            result['native_collaboration_guard'] = protocol_hold
    provider._unresolved_queue(queue_view, event['session_id'])
    if not records:
        raise ValueError('Native transcript has no terminal record')
    if (prompt_proof and prompt_proof.get('queue_bound') is True
            and event['created_at'] < prompt_proof['native_received_at'] - 1):
        # The queued prompt was consumed after the previous turn's Stop. That
        # older terminal event cannot close or authorize input to the new turn.
        born = process['birth'][0] + process['birth'][1] / 1e6
        received = prompt_proof['native_received_at']
        if not born <= prompt['created_at'] <= received <= time.time() + 1:
            raise ValueError('Queued native input is outside this process lifecycle')
        result.update(native_at=received, native_completion_reported=False,
                      native_turn_kind='native_activity', continuation_eligible=False,
                      recovery_reason='native_queued_prompt_consumed_after_previous_stop')
        proof = {'schema': 'herdr-original-queued-current-v27', 'event': row['latest'],
                 'native_boundary': boundary, 'original_prompt': row.get('prompt'),
                 'native_prompt_boundary': prompt_proof}
        result['native_proof_sha256'] = digest(canonical(proof))
        if cache_key is not None:
            if len(_NATIVE_OBSERVATION_CACHE) >= 128 and cache_key not in _NATIVE_OBSERVATION_CACHE:
                _NATIVE_OBSERVATION_CACHE.pop(next(iter(_NATIVE_OBSERVATION_CACHE)))
            _NATIVE_OBSERVATION_CACHE[cache_key] = (copy.deepcopy(result), copy.deepcopy(proof))
        return result, proof
    message, offset, record_sha, trailing = terminal_record(records, event)
    if prompt_proof and prompt_proof.get('human_queue_bound') is True:
        absorbed_human_terminal(records, prompt_proof, message, offset, event, trailing)
    if (message.get('type') != 'assistant' or message.get('isSidechain') is not False
            or message.get('sessionId') != event['session_id']
            or not isinstance(message.get('message'), dict)
            or message['message'].get('role') != 'assistant'
            or not isinstance(message.get('uuid'), str) or not message['uuid']):
        raise ValueError('Native terminal record is not a mainline assistant message')
    stamp = provider._epoch(message.get('timestamp'))
    born = process['birth'][0] + process['birth'][1] / 1e6
    if not born <= stamp <= event['created_at'] + 1:
        raise ValueError('Native terminal message is outside this process lifecycle')
    if prompt and stamp < prompt['created_at'] - 1:
        raise ValueError('Native terminal message predates current input')
    text = dispatch._text(message)
    if text is None:
        raise ValueError('Native terminal message has no actual reply text')
    failed = event['event_name'] == 'StopFailure'
    if failed:
        if (message.get('isApiErrorMessage') is not True
                or event.get('stop_failure_error') not in protocol.STOP_FAILURE_ENUMS
                or message.get('error') not in (None, event['stop_failure_error'])
                or protocol.stop_failure_source_hash_binding(event, text) is None):
            raise ValueError('Native API error and original Hook source disagree')
        failure = protocol.classify_stop_failure(event['stop_failure_error'], text)
        result.update(native_error_type=failure['error_kind'],
                      native_error_reason=failure['error_reason'],
                      native_error_message=text[:600], native_turn_kind='task_complete')
    else:
        if message.get('isApiErrorMessage') or dispatch.digest(text) != event.get('message_hash'):
            raise ValueError('Native reply does not match the original Stop Hook')
        failure = None
        result.update(native_error_type=None, native_error_reason=None,
                      native_turn_kind='task_complete')
    completed = protocol.completion_reported(text)
    if bool(event.get('completed')) != completed:
        raise ValueError('Original completion result and actual native reply disagree')
    result['native_completion_reported'] = completed
    result['continuation_eligible'] = not completed and not protocol_hold and not event.get('stop_hook_active')
    result['recovery_reason'] = ('completed' if completed else 'collaboration_protocol_held'
        if protocol_hold else 'stop_hook_active' if event.get('stop_hook_active') else 'incomplete_native_turn')
    proof = {'schema': 'herdr-original-native-terminal-v174', 'event': row['latest'],
             'native_boundary': boundary, 'native_message_uuid': message['uuid'],
             'native_record_offset': offset, 'native_record_sha256': record_sha,
             'native_text_sha256': digest(text.encode()), 'failure': failure,
             'original_prompt':original_prompt if prompt_proof else None,
             'native_prompt_boundary':prompt_proof,
             'trailing_records':[{'offset':o,'sha256':s} for _,o,s in trailing]}
    if background_proof is not None:
        proof['background_notification'] = background_proof
    result['native_proof_sha256'] = digest(canonical(proof))
    if cache_key is not None:
        if len(_NATIVE_OBSERVATION_CACHE) >= 128 and cache_key not in _NATIVE_OBSERVATION_CACHE:
            _NATIVE_OBSERVATION_CACHE.pop(next(iter(_NATIVE_OBSERVATION_CACHE)))
        _NATIVE_OBSERVATION_CACHE[cache_key] = (copy.deepcopy(result), copy.deepcopy(proof))
    return result, proof


def composer_state(text, kind):
    """Conservative plain viewport guard; it never supplies a native lifecycle."""
    lines = text.splitlines()
    if kind != 'claude':
        return 'unknown'
    prompt_rows = [i for i, line in enumerate(lines) if line.lstrip().startswith('❯')]
    if not prompt_rows:
        return 'unknown'
    i = prompt_rows[-1]
    divider = lambda s: bool(s.strip()) and bool(re.fullmatch(r'[─━\- ]+(?:ultracode[─━\- ]*)?', s.strip(), re.I))
    if i == 0 or not divider(lines[i - 1]):
        return 'unknown'
    if lines[i].strip() != '❯':
        return 'draft'
    end = next((j for j in range(i + 1, len(lines)) if divider(lines[j])), None)
    if end is None:
        return 'unknown'
    if any(line.strip() for line in lines[i + 1:end]):
        return 'draft'
    return 'empty'


def screen_guard(text):
    """Current bottom-of-reply guards; historical banners do not authorize input."""
    import re
    lines = text.splitlines()
    prompts = [i for i, s in enumerate(lines) if s.lstrip().startswith('❯')]
    footer = '\n'.join(lines[max(0, prompts[-1] - 16):]) if prompts else '\n'.join(lines[-25:])
    if re.search(r'Interrupted\s*·|What should Claude do instead', footer):
        return 'user_interrupted'
    if re.search(r'Esc to cancel|Do you trust|Permission required|Enter to confirm|Select.{0,25}model', footer, re.I):
        return 'dialog'
    if re.search(r'(?:running (?:Pre|Post)ToolUse hooks|esc to interrupt|Reconnecting\s+\d+/\d+)', footer, re.I):
        return 'client_working_or_reconnecting'
    return None


def admission(observation, text, *, globally_paused=False, enabled=True, pending=False):
    if globally_paused or not enabled:
        return False, 'paused_or_disabled'
    if pending:
        return False, 'original_input_pending'
    if not observation.get('continuation_eligible'):
        return False, observation.get('recovery_reason', 'native_terminal_unverified')
    if composer_state(text, 'claude') != 'empty':
        return False, 'composer_not_verified_empty'
    reason = screen_guard(text)
    return (False, reason) if reason else (True, 'native_incomplete_turn')

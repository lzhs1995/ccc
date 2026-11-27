"""Bounded read-only evidence after an existing CCC native receipt.

This never sends, settles an attempt, changes a budget, or claims business success.
The caller supplies a frozen original attempt and current process/selection proof.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import copy
from datetime import datetime

MAX_SPAN = 2 * 1024 * 1024


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def selection_verified(current):
    coverage = current.get('control_coverage')
    return (current.get('official_selection_verified') is True
            or isinstance(coverage, dict) and coverage.get('current_selection_verified') is True)


def unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate JSON key')
        out[key] = value
    return out


def decode(raw):
    result = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError('not a JSON object')
    return result


# Claude has no Codex session_meta or selection token. Its original guard receipt
# is bound to the native boundary, endpoint, process birth and native session.
CLAUDE_BINDING = ('pid', 'birth', 'session', 'pane_id', 'terminal_id', 'server',
                  'cmux_surface_id', 'cmux_workspace_id')
CLAUDE_METADATA = {'api-request', 'api-request-blob', 'last-prompt', 'ai-title',
                   'mode', 'permission-mode', 'atis-latch', 'pr-link',
                   'file-history-snapshot'}
CLAUDE_ATTACHMENTS = {'total_tokens_reminder', 'hook_additional_context',
                      'hook_success', 'hook_non_blocking_error'}
CLAUDE_STATE_LIMIT = 1024
CLAUDE_CALL_LIMIT = 128


def native_time(value):
    if not isinstance(value, str):
        raise ValueError('missing native timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('native timestamp has no timezone')
    return parsed.timestamp()


def claude_binding(attempt, current, process_birth):
    if (attempt.get('schema') != 'herdr-original-input-intent-v174'
            or attempt.get('phase') != 'native_received'):
        raise ValueError('no original Claude receipt')
    binding = attempt['binding']
    for name in CLAUDE_BINDING:
        if binding.get(name) is None or binding[name] != current.get(name):
            raise ValueError('current Claude binding changed: ' + name)
    if (current.get('agent') != 'claude' or current.get('process_bound') is not True
            or current.get('native_reader_available') is not True
            or not isinstance(attempt.get('session_id'), str) or not attempt['session_id']
            or current.get('native_session_id') != attempt['session_id']
            or current.get('key') != attempt.get('key')
            or attempt['key'] != binding['socket'] + '#' + binding['pane_id']
            or process_birth(binding['pid']) != binding['birth']):
        raise ValueError('current Claude process or native session unverified')


def claude_identity(attempt, current):
    return sha(json.dumps([attempt.get('intent_id'), attempt.get('source_event_id'),
                          attempt.get('binding'), attempt.get('session_id'),
                          attempt.get('receipt'), attempt.get('message_sha256'),
                          attempt.get('native_proof', {}).get('native_boundary'),
                          {k: current.get(k) for k in CLAUDE_BINDING}],
                         sort_keys=True, separators=(',', ':')).encode())


def claude_user_text(event):
    message = event.get('message')
    if (event.get('type') != 'user' or event.get('isSidechain') is not False
            or any(event.get(k) for k in ('isMeta', 'isSynthetic', 'isCompactSummary'))
            or not isinstance(message, dict) or message.get('role') != 'user'):
        raise ValueError('not an original Claude user record')
    content = message.get('content')
    if isinstance(content, str):
        return content
    if (isinstance(content, list) and content and all(isinstance(b, dict)
            and b.get('type') == 'text' and isinstance(b.get('text'), str) for b in content)):
        return ''.join(b['text'] for b in content)
    raise ValueError('Claude user contains nontext input')


def claude_state(receipt):
    return {'next_offset': receipt['offset'] + receipt['bytes'],
            'nodes': [receipt['native_uuid']], 'calls': {}, 'outputs': [],
            'tool_pairs': [], 'errors': [], 'replies': [], 'assistant_count': 0,
            'tool_calls_count': 0, 'tool_error_count': 0, 'tool_outcomes_unknown': 0,
            'tool_content_missing_count': 0, 'last_activity': None,
            'next_user': None, 'terminal': None, 'records_scanned': 0}


def claude_tool_content(block):
    """Validate supported content shapes, without interpreting tool success.

    The native consumer makes content optional. Its absence proves only that
    a result record appeared. String/text/image/PDF shapes are supported by
    the pinned producer; other future block types remain unknown.
    """
    if 'content' not in block:
        return False
    content = block['content']
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        raise ValueError('unsupported Claude tool result content shape')
    for part in content:
        if not isinstance(part, dict):
            raise ValueError('invalid Claude tool result content block')
        kind = part.get('type')
        if kind == 'text':
            if not isinstance(part.get('text'), str):
                raise ValueError('invalid Claude tool result text')
        elif kind in ('image', 'document'):
            source = part.get('source')
            if (not isinstance(source, dict) or source.get('type') != 'base64'
                    or not isinstance(source.get('data'), str) or not source['data']
                    or not isinstance(source.get('media_type'), str)
                    or kind == 'image' and not source['media_type'].startswith('image/')
                    or kind == 'document' and source['media_type'] != 'application/pdf'):
                raise ValueError('unsupported Claude tool result media shape')
        else:
            raise ValueError('unsupported Claude tool result block type')
    return True


def summarize_claude(raw, start, state, session_id):
    """Accumulate only original top-level records rooted at the exact receipt.

    Parent links may branch (e.g. concurrent tools and PreToolUse hooks). A tool
    result still needs its own call ID and exact assistant parent/source UUID. API request
    copies and hook/display text are never expanded into native activity.
    """
    state = copy.deepcopy(state)
    offset, partial = start, False
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b'\n'):
            partial = True
            break
        event = decode(line)
        kind = event.get('type')
        evidence = {'offset': offset, 'bytes': len(line), 'sha256': sha(line),
                    'at': event.get('timestamp'), 'uuid': event.get('uuid')}
        offset += len(line)
        if ('sessionId' in event and event['sessionId'] != session_id
                or 'session_id' in event and event['session_id'] != session_id):
            raise ValueError('Claude record session changed')
        if kind in CLAUDE_METADATA:
            if kind != 'file-history-snapshot' and event.get('sessionId') != session_id:
                raise ValueError('Claude telemetry session missing')
            # Even a replay containing tool_result/user blocks has no authority.
        elif kind == 'queue-operation':
            if event.get('sessionId') != session_id or event.get('operation') not in ('enqueue', 'remove'):
                raise ValueError('unsupported Claude queue operation')
            state['next_user'] = dict(evidence, kind='queued_human_boundary')
            state['next_offset'] = offset
            break
        elif kind in ('user', 'assistant', 'attachment', 'system'):
            if event.get('sessionId') != session_id:
                raise ValueError('Claude native session missing')
            if event.get('isSidechain') is True:
                state['next_offset'] = offset
                continue
            uid, parent = event.get('uuid'), event.get('parentUuid')
            if (event.get('isSidechain') is not False or not isinstance(uid, str) or not uid
                    or uid in state['nodes'] or parent not in state['nodes']):
                raise ValueError('Claude native lineage unknown or duplicate')
            if len(state['nodes']) >= CLAUDE_STATE_LIMIT:
                raise ValueError('Claude lineage state bound exceeded')
            state['nodes'].append(uid)
            native_time(event.get('timestamp'))
            if kind == 'user':
                message = event.get('message')
                if not isinstance(message, dict) or message.get('role') != 'user':
                    raise ValueError('invalid Claude native user')
                content = message.get('content')
                if (isinstance(content, list) and content and all(isinstance(b, dict)
                        and b.get('type') == 'tool_result' for b in content)):
                    if any(event.get(k) for k in ('isMeta', 'isSynthetic', 'isCompactSummary')):
                        raise ValueError('synthetic Claude tool result')
                    for block in content:
                        call_id = block.get('tool_use_id')
                        call = state['calls'].get(call_id) if isinstance(call_id, str) else None
                        if (not call or call_id in state['outputs']
                                or event.get('sourceToolAssistantUUID') != call['uuid']
                                or parent != call['uuid']
                                or 'is_error' in block and type(block['is_error']) is not bool):
                            raise ValueError('Claude tool result pairing unknown')
                        content_present = claude_tool_content(block)
                        # Optional is_error is not evidence of false/success,
                        # regardless of the tool name (including Read/Bash).
                        is_error = block.get('is_error')
                        state['outputs'].append(call_id)
                        state['tool_pairs'].append({'call_id': call_id, 'call': call,
                                                    'result': evidence, 'is_error': is_error,
                                                    'error_status_known': is_error is not None,
                                                    'content_present': content_present})
                        state['tool_error_count'] += is_error is True
                        state['tool_outcomes_unknown'] += is_error is None
                        state['tool_content_missing_count'] += not content_present
                    state['last_activity'] = evidence
                else:
                    # A new native input ends attribution to this attempt. Its
                    # contents never become evidence of work by the old turn.
                    claude_user_text(event)
                    state['next_user'] = dict(evidence, kind='native_user')
            elif kind == 'assistant':
                message = event.get('message')
                if (not isinstance(message, dict) or message.get('role') != 'assistant'
                        or not isinstance(message.get('content'), list)):
                    raise ValueError('invalid Claude assistant shape')
                if event.get('isApiErrorMessage') is True:
                    if not isinstance(event.get('error'), str) or not event['error']:
                        raise ValueError('Claude API failure kind unknown')
                    state['errors'].append(dict(evidence, error=event['error'],
                                                status=event.get('apiErrorStatus')))
                elif (message.get('model') == '<synthetic>' or any(event.get(k) for k in
                        ('isSynthetic', 'isMeta', 'isCompactSummary'))):
                    raise ValueError('unsupported synthetic Claude assistant')
                else:
                    substantive, reply = False, False
                    for block in message['content']:
                        if not isinstance(block, dict):
                            raise ValueError('invalid Claude assistant block')
                        block_type = block.get('type')
                        if block_type in ('text', 'thinking'):
                            value = block.get('text' if block_type == 'text' else 'thinking')
                            if not isinstance(value, str):
                                raise ValueError('invalid Claude assistant text')
                            substantive |= bool(value.strip())
                            reply |= block_type == 'text' and bool(value.strip())
                        elif block_type == 'tool_use':
                            call_id = block.get('id')
                            if (not isinstance(call_id, str) or not call_id or call_id in state['calls']
                                    or not isinstance(block.get('name'), str) or not block['name']
                                    or not isinstance(block.get('input'), dict)
                                    or len(state['calls']) >= CLAUDE_CALL_LIMIT):
                                raise ValueError('Claude tool call ID invalid or over bound')
                            state['calls'][call_id] = dict(evidence, name=block['name'])
                            state['tool_calls_count'] += 1
                            substantive = True
                        else:
                            raise ValueError('unsupported Claude assistant block')
                    if substantive:
                        state['assistant_count'] += 1
                        state['last_activity'] = evidence
                    if reply:
                        state['replies'].append(evidence)
            elif kind == 'attachment':
                attachment = event.get('attachment')
                if not isinstance(attachment, dict):
                    raise ValueError('invalid Claude attachment')
                if attachment.get('type') == 'queued_command':
                    state['next_user'] = dict(evidence, kind='queued_human_boundary')
                elif attachment.get('type') not in CLAUDE_ATTACHMENTS:
                    raise ValueError('unsupported Claude attachment')
            elif kind == 'system':
                if event.get('subtype') != 'turn_duration':
                    raise ValueError('unsupported Claude system record')
                state['terminal'] = dict(evidence, kind='turn_duration')
        else:
            raise ValueError('unsupported Claude native record type')
        state['records_scanned'] += 1
        state['next_offset'] = offset
        if state['next_user']:
            break
    return state, partial


def claude_phase(state):
    if state['next_user']:
        return 'superseded_after_failure' if state['errors'] else 'superseded'
    if state['errors']:
        return 'error_after_tool_activity' if state['tool_pairs'] else 'failed_again'
    if state['tool_pairs']:
        return 'tool_result_observed'
    if state['replies']:
        return 'reply_observed_no_tools'
    if state['assistant_count']:
        return 'assistant_activity_observed'
    return 'no_assistant_yet'


def inspect_claude(attempt, current, process_birth, *, max_span=MAX_SPAN, cursor=None):
    """Read-only bounded suffix; cursor is private, in-memory monitor state.

    Every scan rechecks exact receipt, birth, SID, inode and the cursor anchor.
    Older scanned spans remain historical evidence; they are not reread or
    claimed immutable. Unknown/partial/limited observations never claim success.
    """
    result = {'schema': 'ccc-original-claude-progress-r17', 'provider': 'claude',
              'input_operations': 0, 'budget_changes': 0, 'business_success_claimed': False,
              'native_receipt_verified': False}
    try:
        if type(max_span) is not int or not 0 < max_span <= MAX_SPAN:
            raise ValueError('invalid Claude scan budget')
        claude_binding(attempt, current, process_birth)
        receipt, boundary = attempt['receipt'], attempt['native_proof']['native_boundary']
        path = Path(receipt['transcript_path'])
        if (receipt.get('schema') != 'herdr-native-user-receipt-v174'
                or not path.is_absolute() or str(path) != boundary['path'] or path.resolve() != path
                or type(receipt['offset']) is not int or receipt['offset'] < boundary['stat'][2]
                or type(receipt['bytes']) is not int or not 0 < receipt['bytes'] <= 64 * 1024
                or receipt['device_inode'] != boundary['stat'][:2]):
            raise ValueError('invalid original Claude receipt boundary')
        identity = claude_identity(attempt, current)
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or [st.st_dev, st.st_ino] != receipt['device_inode']:
                raise ValueError('Claude native inode changed')
            stream.seek(receipt['offset'])
            line = stream.read(receipt['bytes'])
            event = decode(line)
            if (not line.endswith(b'\n') or sha(line) != receipt['sha256']
                    or event.get('sessionId') != attempt['session_id']
                    or event.get('uuid') != receipt['native_uuid'] or not receipt['native_uuid']
                    or event.get('timestamp') != receipt['native_timestamp']
                    or claude_user_text(event) != attempt['message']
                    or sha(attempt['message'].encode()) != attempt['message_sha256']):
                raise ValueError('original Claude receipt changed')
            receipt_at = native_time(receipt['native_timestamp'])
            result.update(receipt_bytes_matched_at_scan_start=True, receipt=copy.deepcopy(receipt), receipt_at=receipt_at)
            state = claude_state(receipt)
            original_start = state['next_offset']
            if cursor is not None:
                if (cursor.get('identity') != identity or cursor['state']['next_offset'] < original_start
                        or st.st_size < cursor['observed_file_size']):
                    raise ValueError('Claude cursor identity changed or transcript truncated')
                anchor = cursor['anchor']
                if (type(anchor['bytes']) is not int or not 0 <= anchor['bytes'] <= 128
                        or anchor['offset'] + anchor['bytes'] != cursor['state']['next_offset']):
                    raise ValueError('invalid Claude cursor anchor')
                stream.seek(anchor['offset'])
                if sha(stream.read(anchor['bytes'])) != anchor['sha256']:
                    raise ValueError('Claude cursor anchor changed')
                state = copy.deepcopy(cursor['state'])
            start = state['next_offset']
            if st.st_size < start:
                raise ValueError('Claude native transcript truncated')
            stream.seek(start)
            raw = b'' if state['next_user'] else stream.read(min(max_span, st.st_size - start))
            if not state['next_user'] and len(raw) != min(max_span, st.st_size - start):
                raise ValueError('Claude native transcript shortened during scan')
            state, partial = summarize_claude(raw, start, state, attempt['session_id'])
            stream.seek(receipt['offset'])
            if stream.read(receipt['bytes']) != line:
                raise ValueError('Claude receipt changed during scan')
            stream.seek(start)
            if stream.read(len(raw)) != raw or os.fstat(stream.fileno()).st_size < st.st_size:
                raise ValueError('Claude native span changed during scan')
            anchor_start = max(original_start, state['next_offset'] - 128)
            stream.seek(anchor_start)
            anchor_raw = stream.read(state['next_offset'] - anchor_start)
            now_stat = os.stat(path, follow_symlinks=False)
            if ([now_stat.st_dev, now_stat.st_ino] != receipt['device_inode']
                    or now_stat.st_size < st.st_size):
                raise ValueError('Claude path changed during scan')
            claude_binding(attempt, current, process_birth)
            limited = start + len(raw) < st.st_size and not state['next_user']
            phase = ('scan_limit_unknown' if limited else 'partial_record_unknown' if partial
                     else claude_phase(state))
            next_cursor = {'identity': identity, 'state': state, 'observed_file_size': st.st_size,
                           'anchor': {'offset': anchor_start, 'bytes': len(anchor_raw), 'sha256': sha(anchor_raw)}}
            result.update(phase=phase, native_receipt_verified=True, cursor=next_cursor,
                          partial_record=partial, scan_limited=bool(limited),
                          scanned_end=state['next_offset'], observed_file_size=st.st_size,
                          fixed_span={'path': str(path), 'device': st.st_dev, 'inode': st.st_ino,
                                      'start': start, 'end': start + len(raw), 'sha256': sha(raw)},
                          tool_pairs=copy.deepcopy(state['tool_pairs']), errors=copy.deepcopy(state['errors']),
                          replies=copy.deepcopy(state['replies']), next_user=state['next_user'],
                          terminal=state['terminal'], substantive_assistant_count=state['assistant_count'],
                          tool_calls_observed=state['tool_calls_count'], tool_errors_observed=state['tool_error_count'],
                          tool_outcomes_unknown=state['tool_outcomes_unknown'],
                          tool_content_missing_observed=state['tool_content_missing_count'],
                          last_activity=state['last_activity'], historical_spans_not_revalidated=True)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, OverflowError) as error:
        result.update(phase='evidence_unknown', reason=str(error)[:180])
    return result


def user_text(event):
    p = event.get('payload', {})
    if not isinstance(p, dict):
        raise ValueError('invalid receipt payload')
    if event.get('type') != 'response_item' or p.get('type') != 'message' or p.get('role') != 'user':
        return None
    content = p.get('content')
    if not isinstance(content, list) or any(not isinstance(x, dict) or x.get('type') != 'input_text'
                                          or not isinstance(x.get('text'), str) for x in content):
        raise ValueError('receipt contains nontext input')
    return ''.join(x['text'] for x in content)


def summarize(raw, start):
    """Only top-level native records before the next user input count."""
    calls, outputs, pairs, errors, replies = {}, set(), [], [], []
    seen_end = start
    boundary = None
    aborted = None
    incomplete = False
    for line in raw.splitlines(keepends=True):
        offset = seen_end
        if not line.endswith(b'\n'):
            incomplete = True
            break
        event = decode(line)
        p = event.get('payload', {})
        if not isinstance(p, dict):
            raise ValueError('invalid native payload')
        seen_end += len(line)
        if ((event.get('type') == 'response_item' and p.get('type') == 'message' and p.get('role') == 'user')
                or (event.get('type') == 'event_msg' and p.get('type') == 'user_message')):
            boundary = {'offset': offset, 'at': event.get('timestamp'), 'sha256': sha(line)}
            break
        if event.get('type') == 'response_item':
            kind, call_id = p.get('type'), p.get('call_id')
            if kind in ('function_call', 'custom_tool_call'):
                if not isinstance(call_id, str) or not call_id or call_id in calls or call_id in outputs:
                    raise ValueError('ambiguous tool call id')
                calls[call_id] = (kind, offset, sha(line))
            elif kind in ('function_call_output', 'custom_tool_call_output'):
                if not isinstance(call_id, str) or not call_id or call_id in outputs:
                    raise ValueError('ambiguous tool output id')
                outputs.add(call_id)
                call = calls.get(call_id)
                if call and kind == call[0] + '_output':
                    pairs.append({'call_id': call_id, 'call_offset': call[1], 'call_sha256': call[2],
                                  'output_offset': offset, 'output_sha256': sha(line)})
            elif kind == 'message' and p.get('role') == 'assistant':
                text = ''.join(x.get('text', '') for x in p.get('content', [])
                               if isinstance(x, dict) and x.get('type') == 'output_text')
                if text.strip():
                    replies.append({'offset': offset, 'sha256': sha(line)})
        elif event.get('type') == 'event_msg' and p.get('type') in ('task_complete', 'turn_aborted', 'error'):
            error = p.get('error')
            if error:
                message = error.get('message', '') if isinstance(error, dict) else str(error)
                if not isinstance(message, str):
                    raise ValueError('invalid native error message')
                errors.append({'offset': offset, 'at': event.get('timestamp'), 'sha256': sha(line),
                               'message_sha256': sha(message.encode()),
                               'opaque_writer_refused': 'Opaque continuation writer failed' in message})
            if p.get('type') == 'turn_aborted':
                aborted = {'offset': offset, 'at': event.get('timestamp'), 'sha256': sha(line),
                           'reason': p.get('reason')}
                break
    if incomplete:
        phase = 'partial_record_unknown'
    elif boundary:
        phase = 'superseded_after_failure' if errors else 'superseded'
    elif aborted:
        phase = 'turn_aborted'
    elif errors:
        phase = 'error_after_tool_activity' if pairs else 'failed_again'
    elif pairs:
        phase = 'tool_result_observed'
    elif replies:
        phase = 'reply_observed_no_tools'
    else:
        phase = 'no_tool_result_yet'
    return {'phase': phase, 'tool_pairs': pairs, 'errors': errors, 'replies': replies,
            'next_user': boundary, 'aborted': aborted, 'scanned_end': seen_end, 'partial_record': incomplete,
            'business_success_claimed': False}


def inspect(attempt, current, process_birth, *, max_span=MAX_SPAN):
    result = {'schema': 'ccc-original-recovery-progress-r1', 'input_operations': 0,
              'budget_changes': 0, 'business_success_claimed': False}
    try:
        if not 0 < max_span <= MAX_SPAN:
            raise ValueError('invalid scan budget')
        if attempt.get('phase') != 'native_received' or attempt.get('route') != 'continue':
            raise ValueError('no original continue receipt')
        binding, receipt, source = attempt['binding'], attempt['receipt'], attempt['source']['turn']
        for key in ('pid', 'birth', 'session', 'pane_id', 'terminal_id', 'cmux_surface_id', 'cmux_workspace_id', 'server'):
            if binding.get(key) != current.get(key):
                raise ValueError('current binding changed: ' + key)
        if (not current.get('native_reader_available') or not selection_verified(current)
                or current.get('native_session_id') != source['session_id']
                or process_birth(binding['pid']) != binding['birth']):
            raise ValueError('current process or selection unverified')
        fence = attempt['fence']
        path = Path(receipt['path'])
        if (str(path) != source['transcript_path'] or str(path) != fence['path']
                or path.resolve() != path):
            raise ValueError('receipt path mismatch')
        if (not isinstance(receipt['offset'], int) or receipt['offset'] < fence['size']
                or not 0 < receipt['bytes'] <= 1024 * 1024
                or not 0 < fence['header_bytes'] <= 1024 * 1024):
            raise ValueError('invalid receipt bounds')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
            st = os.fstat(stream.fileno())
            identity = [st.st_dev, st.st_ino]
            if (not stat.S_ISREG(st.st_mode) or identity != [receipt['device'], receipt['inode']]
                    or identity != [fence['device'], fence['inode']]):
                raise ValueError('receipt inode changed')
            header = stream.read(fence['header_bytes'])
            if sha(header) != fence['header_sha256']:
                raise ValueError('original header changed')
            meta = decode(header)
            if (meta.get('type') != 'session_meta' or not isinstance(meta.get('payload'), dict)
                    or meta['payload'].get('id') != source['session_id']):
                raise ValueError('native session mismatch')
            stream.seek(receipt['offset'])
            line = stream.read(receipt['bytes'])
            if not line.endswith(b'\n') or sha(line) != receipt['sha256']:
                raise ValueError('native receipt changed')
            if (user_text(decode(line)) != attempt['message']
                    or sha(attempt['message'].encode()) != attempt['message_sha256']):
                raise ValueError('native receipt text mismatch')
            start = receipt['offset'] + receipt['bytes']
            if st.st_size < start:
                raise ValueError('native transcript truncated')
            raw = stream.read(min(max_span, st.st_size - start))
            if len(raw) != min(max_span, st.st_size - start):
                raise ValueError('native transcript shortened during read')
            observed = summarize(raw, start)
            stream.seek(receipt['offset'])
            if stream.read(receipt['bytes']) != line:
                raise ValueError('receipt changed during scan')
            stream.seek(start)
            if stream.read(len(raw)) != raw or os.fstat(stream.fileno()).st_size < st.st_size:
                raise ValueError('native span changed during scan')
            stream.seek(0)
            if stream.read(fence['header_bytes']) != header:
                raise ValueError('header changed during scan')
            now = os.stat(path, follow_symlinks=False)
            if [now.st_dev, now.st_ino] != identity or process_birth(binding['pid']) != binding['birth']:
                raise ValueError('process or native path changed during scan')
            limited = start + len(raw) < st.st_size and observed['next_user'] is None and observed['aborted'] is None
            if limited:
                observed['phase'] = 'scan_limit_unknown'
            result.update(observed, native_receipt_verified=True, receipt=receipt,
                          fixed_span={'path': str(path), 'device': st.st_dev, 'inode': st.st_ino,
                                      'start': start, 'end': start + len(raw), 'sha256': sha(raw)},
                          scan_limited=limited, observed_file_size=st.st_size)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
        result.update(phase='evidence_unknown', native_receipt_verified=False, reason=str(error)[:180])
    return result

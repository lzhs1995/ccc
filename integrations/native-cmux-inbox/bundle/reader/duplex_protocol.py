"""Pure checks for the isolated two-root notice/body fixture; no host calls."""
import base64
import hashlib
import json
from pathlib import Path
import uuid

MESSAGE_KEYS = {'id','thread_id','sender_name','sender_surface_id','sender_workspace_id',
                'recipient_surface_id','recipient_workspace_id','body','created_at','in_reply_to',
                'state','delivered_at','delivered_via','read_at','failure_reason'}


def need(value, reason):
    if not value:
        raise ValueError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    need(isinstance(value, str), 'UUID string required')
    return str(uuid.UUID(value))


def endpoints(pair):
    need(pair.get('schema') == 'private-native-duplex-pair-v1'
         and set(pair.get('actors', {})) == {'A', 'B'}, 'two original roots required')
    actors = pair['actors']
    for label in ('A', 'B'):
        row = actors[label]
        need(row['root'] == str(Path(pair['root']) / ('x' if label == 'A' else 'y')),
             'fixed private actor directory required')
        b = row['binding']
        need(b['root_metadata']['thread_scope'] == 'ROOT_METADATA'
             and b['root_metadata']['parent_thread_id'] is None
             and canonical(b['actor']['session_id']) == b['root_metadata']['actual_thread_id']
             and b['client'] == b['bootstrap']['client'], 'child/unbound actor rejected')
        c = row['client']
        need(c['pid'] == b['client']['pid'] and c['birth'] == b['client']['birth']
             and canonical(c['surface']) == b['actor']['surface_uuid']
             and canonical(c['workspace']) == b['actor']['workspace_uuid']
             and isinstance(c['tty'], str) and c['tty'], 'original native endpoint required')
    for key in ('session_id', 'surface_uuid', 'workspace_uuid'):
        need(actors['A']['binding']['actor'][key] != actors['B']['binding']['actor'][key],
             'two distinct actual root endpoints required')
    need(actors['A']['client']['pid'] != actors['B']['client']['pid']
         and actors['A']['client']['tty'] != actors['B']['client']['tty'],
         'one TUI/TTY cannot stand in for two actors')
    return actors


def reference(pair, sender, raw, in_reply_to=None, parent_body_sha256=None):
    endpoints(pair)
    recipient = 'B' if sender == 'A' else 'A'
    need(sender in ('A', 'B') and 4096 < len(raw) <= 16384, 'full referenced body bound')
    need((sender == 'A' and in_reply_to is None and parent_body_sha256 is None)
         or (sender == 'B' and canonical(in_reply_to) == in_reply_to
             and isinstance(parent_body_sha256, str) and len(parent_body_sha256) == 64),
         'return direction must bind original message and body')
    value = dict(schema='CMUX_BODY_REFERENCE_V1', nonce=pair['nonce'], direction=sender+recipient,
                 marker='CMUX_DUPLEX_'+sender+recipient+'_'+pair['nonce'],
                 path=str(Path(pair['root'])/'bodies'/(sender+recipient+'.txt')),
                 bytes=len(raw), sha256=sha(raw), in_reply_to=in_reply_to,
                 parent_body_sha256=parent_body_sha256)
    text = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    need(len(text.encode()) <= 700 and not any(c in text for c in '\r\n\t'), 'bounded single-line notice')
    return value, text


def message(row, pair, sender, ref, text):
    actors = endpoints(pair)
    recipient = 'B' if sender == 'A' else 'A'
    need(isinstance(row, dict) and MESSAGE_KEYS <= set(row)
         and canonical(row.get('id')) == row['id'], 'complete actual message payload required')
    for role, label in (('sender', sender), ('recipient', recipient)):
        actor = actors[label]['binding']['actor']
        for name in ('surface', 'workspace'):
            need(canonical(row.get(role+'_'+name+'_id')) == actor[name+'_uuid'], 'message endpoint differs')
    need(row.get('body') == text and row.get('state') == 'queued'
         and row.get('thread_id') == pair['nonce']
         and row.get('in_reply_to') == ref['in_reply_to'], 'original queued notice/thread/reply differs')
    need(all(row[k] is None for k in ('delivered_at','delivered_via','read_at','failure_reason')),
         'original queued state was mutated')
    need(json.loads(text) == ref, 'notice bytes and reference differ')
    return row


def body_output(raw):
    # The actual ToolCtx output contains every byte, not just a digest assertion.
    return 'CMUX_FULL_BODY_V1:'+base64.b64encode(raw).decode('ascii')


def full_body(proof, output):
    raw = base64.b64decode(proof['full_body_base64'], validate=True)
    need(proof.get('body_read_complete') is True
         and len(raw) == proof['body_bytes'] == proof['reference']['bytes']
         and sha(raw) == proof['body_sha256'] == proof['reference']['sha256']
         and body_output(raw) in output, 'complete original body bytes absent or changed')
    return raw


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


def subsequent_context(body, context, issued, output):
    need(isinstance(context, str) and context.startswith('CMUX_INBOX_CONTEXT_V1:'), 'actual reader context required')
    need(not any(context in s for s in strings(issued)) and not any(context in s for s in strings(output)),
         'tool command/output cannot substitute for automatic context')
    # The provider stores the complete original request before this check.
    non_tools = [item for item in body.get('input', []) if isinstance(item, dict)
                 and item.get('type') not in ('function_call', 'function_call_output',
                                             'custom_tool_call', 'custom_tool_call_output')]
    need(any(context in s for s in strings(non_tools)), 'automatic context absent from subsequent native request')


def hook_scope(result, payload, command, sid, client, incoming=None):
    need(payload.get('hook_event_name') == 'PostToolUse' and payload.get('tool_name') == 'Bash'
         and payload.get('tool_input') == {'command': command}, 'wrong natural tool hook')
    origin = result['native_origin']
    reply = origin['reply']
    need(origin['client'] == {k: client[k] for k in ('pid', 'birth')}
         and reply['threadId'] == sid and reply['callId'] == payload['tool_use_id']
         and reply['origin']['kind'] == 'native-command-hook'
         and reply['origin']['event'] == 'PostToolUse'
         and reply['origin']['turnId'] == payload['turn_id'], 'native hook lineage differs')
    need(result.get('host_mocks') is False and result.get('manual_hook') is False
         and result.get('mailbox_mutations') == 0 and result.get('terminal_inputs') == 0,
         'hook is not read-only automatic evidence')
    if result['role'] == 'child':
        need(incoming is None and result['status'] == 'ACTUAL_CHILD_REFUSED_BEFORE_INBOX'
             and all(result[k] == [] for k in ('full_reader_calls', 'actual_parsed_host_queries',
                                               'emitted_contexts', 'notices')), 'child reached root inbox')
    else:
        need(len(result['actual_parsed_host_queries']) == 1, 'one actual inbox query required')
        if incoming is None:
            need(result['status'] == 'FULL_READER_CALLED_NO_NEW_CONTEXT'
                 and len(result['full_reader_calls']) == 1
                 and result['emitted_contexts'] == [] and result['notices'] == [], 'unexpected duplicate context')
        else:
            need(result['status'] == 'FULL_READER_CONTEXT_EMITTED'
                 and len(result['full_reader_calls']) == 2
                 and len(result['emitted_contexts']) == len(result['notices']) == 1, 'nonempty full reader path missing')
            notice = result['notices'][0]
            need(notice['id'] == incoming['id'] and notice.get('body') == incoming['body']
                 and notice['body_bytes'] == len(incoming['body'].encode())
                 and notice['body_sha256'] == sha(incoming['body'].encode()), 'notice differs from original queued message')
    return reply['callId']

"""Exact pure r4 inbox acceptance only; full cmux host binding is NOT present."""
import uuid
def need(condition, reason):
    if not condition:
        raise ValueError(reason)


def canonical(value):
    need(isinstance(value, str), 'UUID string required')
    result = str(uuid.UUID(value))
    need(result == value.lower(), 'canonical UUID required')
    return result


def validate_reply(reply, challenge, client, current, chain, epoch, payload, facts, handler_id):
    """Pure acceptance. No reply can supply foreground or sending authority."""
    need(isinstance(reply, dict) and set(reply) == {'schema','purpose','scope','source','challenge',
         'clientPid','clientBirth','clientEpoch','peerPid','peerBirth','spawnPid','spawnBirth',
         'threadId','callId','origin','foreground','sendAuthority','controllerAuthority','inboxAuthority'}
         and type(reply.get('schema')) is int and reply['schema'] == 1
         and reply.get('purpose') == 'native_caller_thread'
         and reply.get('scope') == 'caller-thread-readonly'
         and reply.get('source') == 'same-process-native-spawn', 'wrong caller evidence scope')
    need(reply.get('challenge') == challenge and reply.get('clientPid') == client['pid']
         and reply.get('clientBirth') == client['birth'] and reply.get('clientEpoch') == epoch
         and reply.get('peerPid') == current['pid'] and reply.get('peerBirth') == current['birth'],
         'native caller generation or nonce mismatch')
    need(reply.get('foreground') is None and all(reply.get(k) is False for k in
         ('sendAuthority', 'controllerAuthority', 'inboxAuthority')), 'caller evidence grants forbidden authority')
    origin = reply.get('origin')
    need(isinstance(origin, dict) and set(origin) == {'kind','event','turnId','toolUseId','handlerId'}
         and origin['kind'] == 'native-command-hook' and origin['event'] == 'PostToolUse'
         and origin['handlerId'] == handler_id, 'not the original automatic PostToolUse hook')
    need(all(isinstance(origin[k],str) and 0 < len(origin[k]) <= 256 for k in ('turnId','toolUseId'))
         and payload.get('hook_event_name') == origin['event']
         and payload.get('turn_id') == origin['turnId'] and bool(origin['turnId'])
         and payload.get('tool_use_id') == origin['toolUseId'] and bool(origin['toolUseId'])
         and reply.get('callId') == origin['toolUseId'], 'native hook trigger differs from stdin')
    thread = canonical(reply.get('threadId'))
    need(facts.get('thread_scope') == 'ROOT_METADATA' and not facts.get('parent_thread_id')
         and canonical(facts.get('actual_thread_id')) == thread
         and canonical(facts.get('inherited_session_id')) == thread
         and canonical(payload.get('session_id')) == thread, 'child or mismatched root metadata')
    matches = [p for p in [current] + chain if p.get('pid') == reply.get('spawnPid')
               and p.get('birth') == reply.get('spawnBirth')]
    need(len(matches) == 1 and matches[0]['ppid'] == client['pid'], 'native hook spawn is not this reader ancestor')
    return dict(thread_id=thread, origin=dict(origin), spawn_pid=reply['spawnPid'], spawn_birth=reply['spawnBirth'])



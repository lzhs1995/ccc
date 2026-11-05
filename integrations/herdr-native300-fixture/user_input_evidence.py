"""Validate one original fixture input across native event schema versions."""


def need(value, reason):
    if not value:
        raise ValueError('fixture did not remain the one original user task: ' + reason)


def one_text(content, kind, initial):
    return (isinstance(content, list) and len(content) == 1
            and isinstance(content[0], dict) and content[0].get('type') == kind
            and content[0].get('text') == initial)


def original_user_span(records, sid, turn, initial, start, end):
    legacy = [(r, s) for r, s in records if r.get('type') == 'event_msg'
              and r.get('payload', {}).get('type') == 'user_message']
    current = [(r, s) for r, s in records if r.get('type') == 'event_msg'
               and r.get('payload', {}).get('type') == 'item_completed'
               and r.get('payload', {}).get('item', {}).get('type') == 'UserMessage']
    need(len(legacy) <= 1 and len(current) <= 1 and (legacy or current),
         'missing or repeated native input event')
    for record, span in legacy:
        p = record['payload']
        need(p.get('message') == initial and p.get('turn_id', turn) == turn
             and start < span['offset'] < end, 'legacy input differs')
    if not current:
        return legacy[0][1]
    record, span = current[0]
    p = record['payload']
    need(p.get('thread_id') == sid and p.get('turn_id') == turn
         and one_text(p['item'].get('content'), 'text', initial)
         and start < span['offset'] < end, 'current input differs')
    # The current schema also retains the actual submitted input. Harness
    # context is separate and cannot count as a user submission.
    submitted = []
    harness_kinds = {'agents_md.instructions': 'agents_md',
                     'environments.environment_context': 'harness'}
    for r, s in records:
        q = r.get('payload', {})
        if r.get('type') != 'response_item' or q.get('role') != 'user':
            continue
        m = q.get('internal_chat_message_metadata_passthrough') or {}
        kinds = m.get('content_item_kinds')
        provenance = m.get('content_item_metadata')
        content = q.get('content')
        need(m.get('turn_id') == turn and isinstance(kinds, list) and kinds
             and isinstance(provenance, list) and isinstance(content, list)
             and len(kinds) == len(provenance) == len(content), 'unknown retained user provenance')
        if kinds == ['user.text']:
            need(provenance[0].get('provenance', {}).get('type') == 'user'
                 and one_text(content, 'input_text', initial)
                 and start < s['offset'] < span['offset'], 'retained submission differs')
            submitted.append(s)
        else:
            need(all(k in harness_kinds and meta.get('provenance', {}).get('type') == harness_kinds[k]
                     and item.get('type') == 'input_text' and isinstance(item.get('text'), str)
                     for k, meta, item in zip(kinds, provenance, content)), 'unexpected extra user content')
    need(len(submitted) == 1, 'missing or repeated retained submission')
    return span

"""Exact request identities for CCC compaction, independent of wall-clock skew.

The native compact response is empty. A matching trace in the original thread's
Compact Submission is the causal receipt; temporal proximity is not one.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets

from ccc_codex_goal import _debug_fields


def trace_id(parent):
    if not isinstance(parent, str):
        return None
    match = re.fullmatch(r'00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})', parent)
    if not match or int(match[1], 16) == 0 or int(match[2], 16) == 0:
        return None
    return match[1]


def native_trace(value):
    """Parse only the outer Submission trace, never quoted operation content."""
    try:
        if not isinstance(value, str) or not value.startswith('Some(') or not value.endswith(')'):
            return None
        fields = _debug_fields(value[5:-1], 'W3cTraceContext')
        if set(fields) != {'traceparent', 'tracestate'}:
            return None
        parent = fields['traceparent']
        if not parent.startswith('Some(') or not parent.endswith(')'):
            return None
        parent = json.loads(parent[5:-1])
        return parent if trace_id(parent) else None
    except (TypeError, ValueError):
        return None


def request_bytes(request, sid):
    """Validate the complete persisted envelope before sending or reconciling."""
    if (not isinstance(request, dict) or set(request) != {'id', 'method', 'params', 'trace'}
            or request.get('method') != 'thread/compact/start'
            or not isinstance(sid, str) or not sid
            or request.get('params') != {'threadId': sid}
            or not isinstance(request.get('trace'), dict)
            or set(request['trace']) != {'traceparent'}):
        raise ValueError('compact requires its original complete traced request')
    identity = trace_id(request['trace']['traceparent'])
    if not identity or request.get('id') != 'ccc-compact-' + identity:
        raise ValueError('invalid compact request trace identity')
    return json.dumps(request, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def new_request(sid):
    identity, span = secrets.token_hex(16), secrets.token_hex(8)
    value = {'id': 'ccc-compact-' + identity, 'method': 'thread/compact/start',
             'params': {'threadId': sid},
             'trace': {'traceparent': f'00-{identity}-{span}-01'}}
    request_bytes(value, sid)
    return value


def intent_trace(intent, sid):
    """Legacy intents lacking a causal identity remain observation-only."""
    try:
        request = intent['native_request']
        raw = request_bytes(request, sid)
        if hashlib.sha256(raw).hexdigest() != intent.get('native_request_sha256'):
            return None
        return trace_id(request['trace']['traceparent'])
    except (KeyError, TypeError, ValueError):
        return None

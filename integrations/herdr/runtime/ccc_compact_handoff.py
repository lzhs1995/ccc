"""Settle baseline, watchdog handoff records and a pure-read consumer.

Producer helpers return plain records; only the watchdog writes them, each
exactly once. admit() is the consumer: it opens files read-only, never builds
Worker/PendingInputs/IntentIndex, never refreshes or snapshots, and never
continues a turn. Its result is an admission verdict plus the handoff SHA and
identity; binding that SHA to one send attempt belongs to the caller.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import time

from ccc_compact_goals import verify_event

SETTLE_SCHEMA = 'ccc-compact-settle-v1'
HANDOFF_SCHEMA = 'ccc-compact-handoff-v1'
NATURAL_SOURCES = frozenset({'post_sampling_decision', 'pre_sampling_preparation'})
# Inferred from the app-server turn status vocabulary; not measured here.
FAILED_TURN = 'failed'
SETTLE_TTL = 1800
HANDOFF_TTL = 600
STATE_FRESH = 15
GOAL_CORE = ('threadId', 'objective', 'tokenBudget')
MAX_READ = 64 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def goal_core(goal):
    return {key: (goal or {}).get(key) for key in GOAL_CORE} if goal else None


def file_id(value):
    return list(value) if value is not None else None


def settle_rejects(episode, snap):
    """W1: only a first natural compaction of an armed episode may settle."""
    reasons = []
    if episode.get('compact_source') not in NATURAL_SOURCES:
        reasons.append('compaction source not natural')
    if episode.get('manual_present') is not False:
        reasons.append('manual compaction present or unknown')
    if episode.get('parent_key') or episode.get('attempt', 0) != 0 or episode.get('root') != episode.get('key'):
        reasons.append('watchdog recovery episode')
    if not snap.get('binding_ok'):
        reasons.append('binding not authenticated')
    if snap.get('paused'):
        reasons.append('paused or STOP')
    facts = snap.get('facts') or {}
    if not facts.get('caught_up') or facts.get('receipt_overflow'):
        reasons.append('rollout not caught up or receipt overflow')
    guard = snap.get('input_guard') or {}
    if not (guard.get('safe') is True and guard.get('caught_up') is True and guard.get('token')
            and not guard.get('pending') and not guard.get('unknown')):
        reasons.append('input ledger not clear')
    return reasons


def settle_record(episode, snap, target, config_sha256, now):
    """Persisted post-compaction baseline; later checks never use pre-compact offsets."""
    facts, guard = snap['facts'], snap['input_guard']
    return dict(schema=SETTLE_SCHEMA, key=episode['key'], turn_id=episode['turn_id'],
                session_id=target['session_id'], identity=target['identity'],
                surface_id=target.get('surface_id'), path=facts['path'],
                file_id=file_id(facts['file_id']), compacted_offset=facts['compacted_offset'],
                compacted_at=facts['compacted_at'], user_offset=facts['user_offset'],
                input_token=guard['token'], goal_core=goal_core(snap.get('goal_full')),
                goal_status=(snap.get('goal_full') or {}).get('status'),
                pre_compact_blocked_revision=episode.get('blocked_goal_revision'),
                compact_source=episode.get('compact_source'),
                model=(snap.get('thread') or {}).get('model'),
                effort=(snap.get('thread') or {}).get('reasoningEffort'),
                config_sha256=config_sha256, settled_at=now, expires_at=now + SETTLE_TTL)


def revoke_reasons(settle, view, deadline, now):
    """W3 against the settle baseline. view uses snapshot / state['native'] keys."""
    reasons = []
    facts, guard = view.get('facts') or {}, view.get('input_guard') or {}
    if now > deadline:
        reasons.append('expired')
    if not view.get('binding_ok'):
        reasons.append('binding not authenticated')
    if view.get('paused'):
        reasons.append('paused or STOP')
    if not facts.get('caught_up') or facts.get('receipt_overflow'):
        reasons.append('rollout not caught up or receipt overflow')
    if facts.get('path') != settle['path'] or file_id(facts.get('file_id')) != settle['file_id']:
        reasons.append('rollout path or file identity changed')
    if facts.get('compacted_offset') != settle['compacted_offset']:
        reasons.append('another compaction after settle')
    if facts.get('user_offset') != settle['user_offset']:
        reasons.append('new user input after settle')
    if not (guard.get('safe') is True and guard.get('caught_up') is True
            and not guard.get('pending') and not guard.get('unknown')):
        reasons.append('input ledger not clear')
    if guard.get('token') != settle['input_token']:
        reasons.append('input token changed')
    goal = view.get('goal_full')
    if goal_core(goal) != settle['goal_core'] or (goal or {}).get('status') not in {settle['goal_status'], 'blocked'}:
        reasons.append('goal changed')
    turn = view.get('turn')
    if turn and turn.get('id') != settle['turn_id']:
        reasons.append('different turn')
    elif turn and turn.get('status') not in {'inProgress', FAILED_TURN}:
        reasons.append('turn ended without failure')
    return reasons


def handoff_ready(settle, snap):
    """W2: same-turn failure with a fresh, verified blocked Goal event."""
    turn, event, goal = snap.get('turn') or {}, snap.get('blocked_goal_event'), snap.get('goal_full') or {}
    if turn.get('id') != settle['turn_id'] or turn.get('status') != FAILED_TURN:
        return False
    if (not event or event.get('turn_id') != settle['turn_id']
            or event.get('thread_id') != settle['session_id'] or event.get('goal') != goal
            or goal.get('status') != 'blocked'):
        return False
    revision = {k: goal.get(k) for k in ('createdAt', 'updatedAt')}
    return revision != settle['pre_compact_blocked_revision'] and goal_core(goal) == settle['goal_core']


def handoff_record(settle, settle_sha256, snap, now):
    event = snap['blocked_goal_event']
    return dict(schema=HANDOFF_SCHEMA, key=settle['key'], settle_sha256=settle_sha256,
                session_id=settle['session_id'], identity=settle['identity'],
                turn_id=settle['turn_id'], turn_status=snap['turn']['status'],
                blocked_goal_event=event, blocked_goal_event_sha256=digest(event),
                produced_at=now, expires_at=now + HANDOFF_TTL)


# ---- consumer: read-only from here on -------------------------------------

def read_bytes(path):
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_READ + 1)
    if len(raw) > MAX_READ:
        raise ValueError('evidence too large')
    return raw


def goal_source_rejects(source, settle):
    try:
        fd = os.open(source['path'], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            st = os.fstat(fd)
            raw = os.pread(fd, source['bytes'], source['offset'])
        finally:
            os.close(fd)
    except (OSError, KeyError, TypeError):
        return ['goal evidence unreadable']
    if not stat.S_ISREG(st.st_mode) or [st.st_dev, st.st_ino] != source.get('file_id'):
        return ['goal evidence replaced']
    if len(raw) != source['bytes'] or hashlib.sha256(raw).hexdigest() != source.get('sha256'):
        return ['goal evidence changed']
    record = json.loads(raw)
    params = (record.get('envelope') or {}).get('params') or {}
    if (record.get('kind') != 'GOAL_NOTIFICATION' or record.get('session_id') != settle['session_id']
            or record.get('cursor') != source.get('cursor')
            or params.get('threadId') != settle['session_id'] or params.get('turnId') != settle['turn_id']):
        return ['goal evidence does not match settle']
    return []


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def rollout_size(settle):
    """Size of the original rollout, read-only; None if it is not that regular file."""
    fd = os.open(settle['path'], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        st = os.fstat(fd)
    finally:
        os.close(fd)
    if not stat.S_ISREG(st.st_mode) or [st.st_dev, st.st_ino] != settle['file_id']:
        return None
    return st.st_size


def admit(handoff_path, *, state_path, config_path, session_id, identity, bound_handoffs=(),
          expected_sha256=None, now=None):
    """Pure-read admission. Never writes, refreshes, snapshots or continues.

    Positive predicate: every fact must be present and match; any malformed
    structure, JSON or timestamp is a refusal, never an exception.
    """
    result = dict(admitted=False, reasons=[], handoff_sha256=None, key=None,
                  session_id=session_id, turn_id=None)
    try:
        _admit(result, handoff_path, state_path, config_path, session_id, identity,
               bound_handoffs, expected_sha256, time.time() if now is None else now)
    except Exception as exc:
        result['reasons'].append('malformed evidence: %s' % type(exc).__name__)
    result['admitted'] = not result['reasons']
    return result


def _admit(result, handoff_path, state_path, config_path, session_id, identity,
           bound_handoffs, expected_sha256, now):
    reasons = result['reasons']
    if not finite(now):
        reasons.append('clock not finite')
        return
    try:
        raw = read_bytes(handoff_path)
        sha = result['handoff_sha256'] = hashlib.sha256(raw).hexdigest()
        handoff = json.loads(raw)
        key = handoff.get('key') if isinstance(handoff, dict) else None
        if not isinstance(key, str) or not key or os.sep in key or key.startswith('.'):
            reasons.append('malformed handoff')
            return
        settle_path = os.path.join(os.path.dirname(handoff_path), 'settle-%s.json' % key)
        settle_raw = read_bytes(settle_path)
        settle = json.loads(settle_raw)
        state_raw = read_bytes(state_path)
        state = json.loads(state_raw)
        config_sha = hashlib.sha256(read_bytes(config_path)).hexdigest()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reasons.append('unreadable evidence: %s' % type(exc).__name__)
        return
    result.update(key=key, turn_id=handoff.get('turn_id'))
    native = state.get('native') if isinstance(state, dict) else None
    facts = native.get('facts') if isinstance(native, dict) else None
    if not (isinstance(settle, dict) and isinstance(native, dict) and isinstance(facts, dict)):
        reasons.append('malformed watchdog state')
        return
    if expected_sha256 is not None and sha != expected_sha256:
        reasons.append('handoff changed since first admission')
    if handoff.get('schema') != HANDOFF_SCHEMA or settle.get('schema') != SETTLE_SCHEMA:
        reasons.append('schema')
    if hashlib.sha256(settle_raw).hexdigest() != handoff.get('settle_sha256'):
        reasons.append('settle changed')
    if os.path.exists(os.path.join(os.path.dirname(handoff_path), 'revoked-%s.json' % handoff.get('key'))):
        reasons.append('revoked')
    if (handoff.get('session_id') != session_id or settle.get('session_id') != session_id
            or handoff.get('identity') != identity or settle.get('identity') != identity
            or state.get('session_id') != session_id):
        reasons.append('identity or session drift')
    if handoff.get('turn_id') != settle.get('turn_id') or handoff.get('key') != settle.get('key'):
        reasons.append('handoff does not match settle')
    if sha in set(bound_handoffs):
        reasons.append('handoff already bound to a send attempt')
    record = state.get('handoff') if isinstance(state.get('handoff'), dict) else {}
    if record.get('phase') != 'handoff' or record.get('handoff_sha256') != sha:
        reasons.append('watchdog state does not carry this handoff')
    observed, produced, expires = state.get('observed_at'), handoff.get('produced_at'), handoff.get('expires_at')
    if not (finite(observed) and finite(produced) and finite(expires)):
        reasons.append('timestamps not finite numbers')
        return
    if observed > now or produced > now:
        reasons.append('timestamp in the future')
    # Same-tick production is not yet observed; one later watchdog tick is required.
    if observed <= produced or now - observed > STATE_FRESH:
        reasons.append('watchdog observation stale')
    if config_sha != settle.get('config_sha256'):
        reasons.append('config changed')
    if reasons:
        return
    reasons.extend(revoke_reasons(settle, native, expires, now))
    # Positive facts, not the producer's waiting-phase revocation predicate.
    turn, goal, event = native.get('turn'), native.get('goal_full'), handoff.get('blocked_goal_event')
    if not (isinstance(turn, dict) and turn.get('id') == settle['turn_id'] == handoff.get('turn_id')
            and turn.get('status') == FAILED_TURN == handoff.get('turn_status')):
        reasons.append('current turn is not the settled failed turn')
    if not (isinstance(goal, dict) and isinstance(event, dict) and goal.get('status') == 'blocked'
            and goal == event.get('goal') and goal_core(goal) == settle['goal_core']
            and {k: goal.get(k) for k in ('createdAt', 'updatedAt')}
            != settle['pre_compact_blocked_revision']):
        reasons.append('current Goal is not the blocked revision of this handoff')
    if reasons:
        return
    size = rollout_size(settle)
    if size is None or size <= settle['compacted_offset']:
        reasons.append('rollout replaced or truncated')
    elif facts.get('caught_up') is not True or facts.get('offset') != size:
        reasons.append('rollout changed after the last watchdog observation')
    if digest(event) != handoff.get('blocked_goal_event_sha256'):
        reasons.append('blocked event changed')
    else:
        reasons.extend(goal_source_rejects(event.get('source') or {}, settle))
    if not reasons:
        try:
            verify_event(event, session_id, settle['turn_id'], goal)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            reasons.append('Goal evidence not verified: %s' % type(exc).__name__)
    if rollout_size(settle) != size or read_bytes(state_path) != state_raw:
        reasons.append('evidence changed during admission')

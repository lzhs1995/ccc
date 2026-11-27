"""Choose a task's active collaborators from bounded, evidenced outcomes.

This is an execution plan, not a release of another participant's files,
callback, resource lease or original nonce. The same task and original panel
survive mode changes. Native receipt/queue uncertainty is never an API outage.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

RETRYABLE = {429, 500, 502, 503, 504, 524, 529}
PERMANENT = {401, 402, 403}
UNKNOWN = {'QUEUED', 'NATIVE_PENDING', 'COMPOSE_PENDING', 'IDENTITY_UNKNOWN',
           'TIMEOUT', 'NO_REPLY', 'TOOL_EXIT_ZERO'}
SCHEMA = 'collaboration-continuity-v1'


def evidence(pin):
    path = Path(pin['path'])
    if not path.is_absolute() or path.is_symlink():
        raise ValueError('canonical evidence file required')
    with path.open('rb') as f:
        raw = f.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != pin['sha256']:
        raise ValueError('bounded original outcome evidence changed')
    return raw


def validate(value):
    if value.get('schema') != SCHEMA or not value.get('task_id'):
        raise ValueError('same-task continuity plan required')
    uuid.UUID(value['workspace_uuid'])
    peers = value['participants']
    if (len(peers) != 3 or [p['role'] for p in peers] !=
            ['supervisor', 'executor', 'executor']):
        raise ValueError('one original supervisor and two original executors required')
    for p in peers:
        uuid.UUID(p['surface_uuid'])
        uuid.UUID(p['session_id'])
        if uuid.UUID(p['workspace_uuid']) != uuid.UUID(value['workspace_uuid']) or p['task_id'] != value['task_id']:
            raise ValueError('participant task/workspace mismatch')
    if (len({uuid.UUID(p['surface_uuid']) for p in peers}) != 3
            or len({uuid.UUID(p['session_id']) for p in peers}) != 3):
        raise ValueError('duplicate participant')
    if value.get('max_subagents') != 2:
        raise ValueError('shared task child-agent budget must be two')
    if value.get('automatic_fallback_authorized') is not True:
        raise ValueError('explicit task fallback authorization required')
    evidence(value['authorization'])
    if len(value.get('observations', [])) > 256:
        raise ValueError('retain a bounded outcome episode; archive older observations')
    known = {uuid.UUID(p['surface_uuid']): p for p in peers}
    seen = set()
    for obs in value.get('observations', []):
        member = known.get(uuid.UUID(obs['surface_uuid']))
        if (member is None or uuid.UUID(obs.get('session_id')) != uuid.UUID(member['session_id'])
                or obs.get('task_id') != value['task_id']):
            raise ValueError('outcome belongs to another original session/task')
        at = obs['at_epoch']
        if type(at) not in (int, float) or not math.isfinite(at) or at < 0:
            raise ValueError('outcome timestamp must be a finite epoch')
        if not isinstance(obs.get('attempt_id'), str) or not obs['attempt_id'].strip():
            raise ValueError('distinct original API attempt identity required')
        key = (uuid.UUID(obs['surface_uuid']), obs['attempt_id'])
        if key in seen:
            raise ValueError('one outcome cannot count as multiple attempts')
        seen.add(key)
        evidence(obs['evidence'])
    return value


def health(observations, now):
    state, consecutive, due, last = 'UNKNOWN', 0, None, None
    for obs in sorted(observations, key=lambda o: o['at_epoch']):
        if obs['at_epoch'] > now:
            raise ValueError('future outcome cannot establish availability')
        kind = obs.get('kind')
        if kind in UNKNOWN or obs.get('terminal') is not True:
            # A queue notification is not an API outcome and cannot erase a
            # genuine success or interrupt the count of terminal failures.
            continue
        elif kind == 'API_SUCCESS':
            state, consecutive, due = 'AVAILABLE', 0, None
        elif kind in ('PROCESS_EXITED', 'SESSION_LOST'):
            state, consecutive = 'UNAVAILABLE', 0
        elif kind == 'API_ERROR' and obs.get('http_status') in PERMANENT:
            state, consecutive = 'UNAVAILABLE', 0
        elif kind == 'API_ERROR' and obs.get('http_status') in RETRYABLE:
            # A caller must not count a too-early duplicate attempt as progress
            # toward takeover. Native internal retries are one observed attempt.
            if due is not None and obs['at_epoch'] < due:
                raise ValueError('retry preceded original retry-after boundary')
            delay = obs.get('retry_after_seconds', 60)
            if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0:
                raise ValueError('invalid retry-after')
            consecutive += 1
            due = obs['at_epoch'] + max(60, delay)
            state = 'UNAVAILABLE' if consecutive >= 3 else 'RETRYING'
        else:
            state, consecutive, due = 'UNKNOWN', 0, None
        last = obs
    return dict(state=state, consecutive_failures=consecutive,
                retry_due_epoch=due if state == 'RETRYING' else None,
                last_outcome_epoch=last['at_epoch'] if last else None,
                observed_age_seconds=now-last['at_epoch'] if last else None)


def plan(value, *, now=None):
    validate(value)
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError('observation time must be a finite epoch')
    peers = value['participants']
    statuses = {p['surface_uuid']: health([o for o in value.get('observations', [])
                  if uuid.UUID(o['surface_uuid']) == uuid.UUID(p['surface_uuid'])], now) for p in peers}
    root, *executors = peers
    root_state = statuses[root['surface_uuid']]['state']
    available = [p['surface_uuid'] for p in executors
                 if statuses[p['surface_uuid']]['state'] == 'AVAILABLE']
    if root_state == 'AVAILABLE':
        mode = ('TRIO' if len(available) == 2 else 'CODEX_CLAUDE' if available else
                'CODEX_SOLO' if all(statuses[p['surface_uuid']]['state'] == 'UNAVAILABLE'
                                    for p in executors) else 'CODEX_CONTINUE_INDEPENDENT')
        active = [root['surface_uuid'], *available]
    elif root_state == 'UNAVAILABLE':
        mode = ('CLAUDE_PEER' if len(available) == 2 else 'CLAUDE_SOLO' if available
                else 'NO_VERIFIED_AVAILABLE_PARTICIPANT')
        active = available
    else:
        mode = 'CONTINUE_INDEPENDENT_AND_VERIFY_SUPERVISOR'
        active = available
    return dict(schema=SCHEMA, task_id=value['task_id'], mode=mode, active=active,
        health=statuses, max_subagents=2, child_budget_scope='WHOLE_TASK',
        replacement_session_allowed=False, replay_allowed=False,
        shared_write_authority='PRESERVE_EXISTING_OWNER_OR_EXPLICIT_HANDOFF',
        next_action=('Original Claude peers agree CLAIMs and merge owner, continue '
                     'authorized task, verify child result and cross-review. Return '
                     'checkpoint and unresolved nonces when Codex recovers.'
                     if mode.startswith('CLAUDE_') else
                     'Continue authorized independent work immediately. Reuse the '
                     'original available participants. Apply existing single-writer '
                     'handoff before taking over another participant’s mutable files.'),
        final_acceptance='original supervisor or user; peer review is not supervisor acceptance',
        input_operations=0, resource_releases=0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    args = parser.parse_args(argv)
    with args.input.open('rb') as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('continuity input budget exceeded')
    value = json.loads(raw)
    print(json.dumps(plan(value), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

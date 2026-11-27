"""Explicit continuing authorization; file state only, never input or hook loops."""
import copy
from contextlib import contextmanager, ExitStack
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import time

import executor_ready as ready
from cmux_evidence_io import read_bytes
from cmux_coordination_paths import ACTIVE_DIR

WAIT_STATUSES = {'WAITING_DEPENDENCY', 'SOLO', 'BLOCKED'}
MAX_FORMAL_MARKERS = 256
MAX_EPOCH = 253402300799.0  # Last complete UTC second representable by datetime.


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def valid_epoch(value):
    # Check the range before math.isfinite: arbitrarily large JSON integers
    # can overflow when converted to float. Bool is not a timestamp.
    return type(value) in (int, float) and 0 <= value <= MAX_EPOCH


def require_epoch(value, name):
    if not valid_epoch(value):
        raise ValueError(name + ' must be a finite nonnegative UTC epoch within datetime range')
    return value


def authorization_pin(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError('authorization must be a canonical absolute regular file')
    raw = read_bytes(path, limit=65536)
    if not raw.strip():
        raise ValueError('empty continuation authorization')
    return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))


def validate_authorization(value, controller, now):
    if not isinstance(value, dict) or value.get('enabled') is not True:
        raise ValueError('explicit continuing authorization required')
    task = value.get('overall_task')
    if not isinstance(task, str) or not task.strip() or len(task) > 4096:
        raise ValueError('overall task is required for continuing inquiries')
    until = value.get('until_epoch')
    if until is not None and (not finite(until) or until <= now):
        raise ValueError('authorization expiry must be a finite future time')
    reservation_inquiries = value.get('reservation_inquiries', False)
    if type(reservation_inquiries) is not bool:
        raise ValueError('reservation inquiries require an explicit boolean authorization')
    proactive_hook = value.get('proactive_hook', False)
    if type(proactive_hook) is not bool:
        raise ValueError('proactive hook requires an explicit boolean authorization')
    if proactive_hook and not reservation_inquiries:
        raise ValueError('proactive hook requires reservation inquiries')
    pin = authorization_pin(value.get('authorization_file', ''))
    result = dict(enabled=True, id=secrets.token_hex(16), status='ACTIVE',
                  overall_task=task, owner=value['owner'], authorization=pin,
                  authorized_epoch=now, until_epoch=until, controller=controller)
    if reservation_inquiries:
        result['reservation_inquiries'] = True
    if proactive_hook:
        result['proactive_hook'] = True
    return result


def binding(value):
    """Immutable authorization identity participates in scheduler enrollment."""
    if not value:
        return None
    keys = ('enabled', 'id', 'overall_task', 'owner', 'authorization',
            'authorized_epoch', 'until_epoch', 'controller')
    # Absence is meaningful for historical authorization. Never add this
    # capability while comparing or enrolling an existing episode.
    if 'reservation_inquiries' in value:
        keys += ('reservation_inquiries',)
    if 'proactive_hook' in value:
        keys += ('proactive_hook',)
    return {key: copy.deepcopy(value[key]) for key in keys}


def reservation_inquiries_allowed(state, *, verification=None):
    """Verified explicit authority permits file status requests, never release.

    The caller must separately validate the authorization file and enrollment.
    Preserve reviewed historical capabilities under the original controller;
    compatibility cannot grant a capability absent from its enrollment.
    """
    from executor_reask_protocol import supports_state
    value = state.get('continuation')
    return (isinstance(value, dict) and value.get('enabled') is True
            and value.get('status') == 'ACTIVE'
            and value.get('reservation_inquiries') is True
            and supports_state(state, verification=verification))


def valid_wait_contract(body, state):
    if body['status'] not in WAIT_STATUSES:
        return True
    due, deadline = body.get('next_due_epoch'), body.get('deadline_epoch')
    return (body.get('owner') == state['supervisor']
            and valid_epoch(due) and valid_epoch(deadline) and 0 < due <= deadline)


def poll_authorization(state, now, *, publish=True):
    """Check authority; publish=False changes only the supplied preview state."""
    import executor_reask as reask
    value = state.get('continuation')
    if not value or state['state'] == reask.STOPPED:
        return
    reason = None
    if not isinstance(value, dict):
        reason = 'AUTHORIZATION_INVALID'
    elif value.get('status') != 'ACTIVE':
        reason = 'AUTHORIZATION_INACTIVE'
    elif value.get('until_epoch') is not None and not finite(value['until_epoch']):
        reason = 'AUTHORIZATION_INVALID'
    elif value.get('until_epoch') is not None and now >= value['until_epoch']:
        reason = 'AUTHORIZATION_EXPIRED'
    else:
        try:
            if authorization_pin(value['authorization']['path']) != value['authorization']:
                reason = 'AUTHORIZATION_CHANGED'
        except (OSError, ValueError, KeyError, TypeError):
            reason = 'AUTHORIZATION_UNAVAILABLE'
    if reason:
        if publish:
            reask.preserve_legacy_snapshot(state)
        if isinstance(value, dict):
            value['status'] = reason
        state.update(state=reask.STOPPED, stopped_epoch=now, exit_reason=reason)
        if publish:
            marker = state['asks'][-1]['marker'] if state.get('asks') else ''
            reask.write_channels(state, marker, reason, now, status=reask.STOPPED)


def _consume_controller(state):
    """Recover identity only; compatibility never grants consumption authority."""
    from executor_reask_protocol import state_controller
    original = state_controller(state)
    if (original is not None or state.get('version') != 2 or 'controller' in state
            or state.get('continuation') is not None):
        return original
    import executor_reask_scheduler as scheduler
    try:
        path = scheduler.enrollment_path(state['caller_surface_uuid'])
        # Caller lock is already held. Selection only reads the committed
        # state's matching intent; never recover a transaction or lock registry.
        record = scheduler._select(ready.read_json(path), state, path)
        if (not record or record.get('schema') not in (scheduler.SCHEMA, scheduler.LEGACY_SCHEMA)
                or record.get('status') not in ('ENROLLED', 'FINISHED')
                or record.get('uid') != os.getuid()
                or record.get('authorization') not in (
                    'EXPLICIT_REASK_START', 'EXPLICIT_REASK_ENROLL_EXISTING')
                or (record.get('schema') == scheduler.LEGACY_SCHEMA
                    and record.get('authorization') != 'EXPLICIT_REASK_START')
                or record.get('binding') != scheduler.binding(state)
                or record.get('registration') != state.get('registration')
                or not scheduler._registration_matches(record, state, path)):
            return None
        return state_controller(state, record)
    except (KeyError, ValueError, TypeError, AttributeError, OSError):
        return None


def _consume_result(state, status, *, receipt=None, receipt_path=None):
    """Report this invocation separately from the durable inquiry state."""
    result = dict(status=status, consumed=status == 'REPLY_CONSUMED',
                  episode_state=state.get('state'),
                  reservation_paused=bool(state.get('paused_tasks')),
                  input_operations=0)
    if receipt is not None:
        result.update(receipt=str(receipt_path), marker=receipt['marker'],
                      body_sha256=receipt['body_sha256'],
                      consumed_epoch=receipt['consumed_epoch'])
    if state.get('exit_reason'):
        result['reason'] = state['exit_reason']
    # Never persist this response: a later status/tick must not look like a
    # fresh acknowledgement of a previously consumed reply.
    return dict(state, consume_result=result)


def consume(caller, reply_sha256='', *, now=None):
    """The foreground executor acknowledges the complete pinned reply body.

    Authenticate the state reloaded under the episode lock, not an earlier CLI
    snapshot. A scheduler or advisory interpreter has no consumption authority.
    """
    import executor_reask as reask
    now = time.time() if now is None else now
    require_epoch(now, 'consumed_epoch')
    with ExitStack() as stack:
        try:
            stack.enter_context(reask.episode_lock(caller))
        except reask.EpisodeBusy:
            # No unlocked episode read, acknowledgement, or automatic retry.
            result = _consume_result(dict(outcome='EPISODE_BUSY', input_operations=0),
                                     'EPISODE_BUSY')
            result['consume_result'].update(reservation_paused=None,
                                            retry_same_arguments=True,
                                            retry_after_seconds=1)
            return result
        state = reask.load(caller) or {}
        if not state:
            return _consume_result(dict(outcome='NO_EPISODE'), 'NO_EPISODE')
        original, invoked = _consume_controller(state), reask.controller()
        if original != invoked:
            from executor_reask_protocol import controller_diagnostic
            return _consume_result(
                dict(outcome='ORIGINAL_CONTROLLER_REQUIRED', input_operations=0,
                     controller_diagnostic=controller_diagnostic(original, invoked)),
                'ORIGINAL_CONTROLLER_REQUIRED')
        from executor_reask_identity import authenticate
        consumer = authenticate(state)
        if reask.stop_file(caller).exists():
            stopped = reask.tick_locked(caller, now, file_only=True)
            return _consume_result(stopped, 'STOPPED')
        poll_authorization(state, now)
        if state.get('state') != reask.ANSWERED:
            reask.save(state)
            return _consume_result(state, 'STOPPED' if state.get('state') == reask.STOPPED
                                   else 'NOT_ANSWERED')
        reply = state['reply']
        continuing = bool(state.get('continuation'))
        if not reply_sha256 or reply_sha256 != reply.get('body_sha256'):
            raise ValueError('read the complete reply body and supply its exact --reply-sha256')
        body = reply.get('body', {})
        if reask.reply_record(body, reply['source'])['body_sha256'] != reply_sha256:
            raise ValueError('stored reply body changed')
        # Validate every prospective scheduling value before writing a durable
        # consumption receipt. Optional TASK fields do not bypass this check.
        if not isinstance(body, dict):
            raise ValueError('stored reply body must be an object')
        for name in ('next_due_epoch', 'deadline_epoch'):
            if name in body:
                require_epoch(body[name], 'reply.' + name)
        next_due = None
        if continuing:
            next_due = max(require_epoch(state.get('next_due_epoch', now), 'state.next_due_epoch'),
                           body.get('next_due_epoch', now))
        marker = reply.get('marker') or body.get('marker', '')
        receipt = dict(schema='executor-reply-consumed-v1', caller_surface_uuid=caller,
                       supervisor=state['supervisor'], task_id=state['task_id'],
                       episode_id=state['episode_id'], marker=marker,
                       detected_epoch=reply.get('at_epoch'), consumed_epoch=now,
                       body_sha256=reply.get('body_sha256'), body=body,
                       consumption='EXPLICIT_FOREGROUND_EXECUTOR_ACK',
                       executor_authentication=consumer)
        path = reask.root() / 'consumed' / ready.digest(caller) / (ready.digest(
            state['episode_id'] + ':' + marker) + '.json')
        prior = ready.read_json(path)
        if prior:
            if any(prior.get(k) != receipt[k] for k in ('episode_id', 'marker', 'body_sha256')):
                raise ValueError('original consumption receipt mismatch')
            prior_consumer = prior.get('executor_authentication', {})
            if any(prior_consumer.get(key) != consumer[key] for key in (
                    'schema', 'provider', 'session_id', 'process', 'identity_sha256', 'episode_id')):
                raise ValueError('original consumption identity proof mismatch')
            require_epoch(prior.get('consumed_epoch'), 'original consumed_epoch')
            receipt = prior
            now = prior['consumed_epoch']
        else:
            ready.write_json(path, receipt)
        state.update(state=reask.CONSUMED, reply_consumed_epoch=now)
        state['reply_history'] = (state.get('reply_history', []) + [dict(
            marker=marker, status=reply['status'], detected_epoch=reply.get('at_epoch'),
            consumed_epoch=now, body_sha256=reply.get('body_sha256'), receipt=str(path))])[-reask.HISTORY:]
        state['replies_closed_through'] = state.get('attempt_count', 0)
        if continuing:
            # TASK/DISPATCHED text is only a reply. The formal task reservation
            # below pauses inquiries; a mailbox assertion cannot dispatch work.
            state['state'] = reask.WAITING
            state['rearm_count'] = state.get('rearm_count', 0) + 1
            state['next_due_epoch'] = max(now, next_due)
            state['last_reply'] = reply
            state['reply'] = None
        reask.write_channels(state, marker, 'reply explicitly consumed', now, status=reask.CONSUMED)
        reask.save(state)
        return _consume_result(state, 'REPLY_CONSUMED', receipt=receipt, receipt_path=path)


def marker_callers(marker):
    participants = marker.get('participants') if isinstance(marker, dict) else None
    if not isinstance(participants, list):
        return []
    return sorted({p['surface_uuid'] for p in participants
                   if isinstance(p, dict) and p.get('role') == 'executor'
                   and isinstance(p.get('surface_uuid'), str) and p['surface_uuid'].strip()})


@contextmanager
def marker_locks(*markers):
    """All CURRENT formal writers share the scheduler's caller locks.

    Sort multi-executor locks globally. Never hold a caller lock while taking
    the enrollment registry lock (the registration order is registry -> caller).
    Older immutable harnesses cannot acquire this new protection retroactively.
    """
    import executor_reask as reask
    with ExitStack() as stack:
        for caller in sorted({c for marker in markers for c in marker_callers(marker)}):
            stack.enter_context(reask.episode_lock(caller))
        yield


def marker_epoch(marker):
    try:
        value = datetime.fromisoformat(marker['armed_at'].replace('Z', '+00:00'))
        return value.timestamp() if value.tzinfo is not None else None
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return None


def _boundary_eligible(state):
    """Stopping/pausing an owner needs no permission to run its file protocol."""
    import executor_reask as reask
    continuation = state.get('continuation')
    return (state.get('state') == reask.WAITING
            or isinstance(continuation, dict) and continuation.get('enabled') is True
            and state.get('state') in (reask.ANSWERED, reask.CONSUMED))


def _boundary_stop(state, reason, now, *, publish, **evidence):
    import executor_reask as reask
    if publish:
        reask.preserve_legacy_snapshot(state)
    state.update(state=reask.STOPPED, stopped_epoch=now, exit_reason=reason, **evidence)
    if isinstance(state.get('continuation'), dict):
        state['continuation']['status'] = 'STOPPED' if reason == 'OPERATOR_STOP' else reason
    if publish:
        last = state['asks'][-1]['marker'] if state.get('asks') else ''
        reask.write_channels(state, last, reason, now, status=reask.STOPPED)


def _release_reservation(state, reservation, now, *, publish):
    """Clear exact ownership bookkeeping without reviving a terminal inquiry."""
    paused = state.get('paused_tasks', [])
    if not isinstance(paused, list) or reservation not in paused:
        return False
    if publish:
        import executor_reask as reask
        reask.preserve_legacy_snapshot(state)
    paused.remove(reservation)
    if _boundary_eligible(state):
        due = state.get('next_due_epoch')
        state['next_due_epoch'] = max(now, due) if finite(due) else now
    state['last_task_boundary'] = dict(event='RELEASED', at_epoch=now, **reservation)
    # Capture the counter before any resumed request, under the episode lock.
    # Old boundaries without this evidence remain first-visible observations.
    counters = {key: state.get(key) for key in ('file_posts', 'attempt_count')}
    if all(type(value) is int and value >= 0 for value in counters.values()):
        state['last_task_boundary'].update(
            file_posts_at_release=counters['file_posts'],
            attempt_count_at_release=counters['attempt_count'])
    return True


def apply_boundary(state, marker, event, now, *, observed=False, publish=True,
                   preview_resume=False):
    """Formal reservation only; never grants a new owner or changes controller.

    publish=False performs the same checks on a caller-supplied deep copy,
    without publishing channels, snapshots or state. Direct RESERVED calls are
    fresh harness events; disk observations must carry their original armed_at.
    """
    import executor_reask as reask
    if preview_resume and publish:
        raise ValueError('operator resume is only allowed in an unpublished registration preview')
    eligible = _boundary_eligible(state)
    terminal_release = (event == 'RELEASED'
                        and state.get('state') in (reask.ANSWERED, reask.CONSUMED, reask.STOPPED))
    if (not (eligible or terminal_release) or not isinstance(marker, dict)
            or event not in ('RESERVED', 'RELEASED') or not finite(now)):
        return False
    participants = marker.get('participants')
    if not isinstance(participants, list) or not all(isinstance(p, dict) for p in participants):
        return False
    owners = [p.get('surface_uuid') for p in participants if p.get('role') == 'supervisor']
    uuids = [p.get('surface_uuid') for p in participants if p.get('surface_uuid')]
    if (len(owners) != 1 or not all(isinstance(u, str) and u.strip() for u in uuids)
            or not owners[0] or len(uuids) != len(set(uuids))
            or not state.get('supervisor') or not state.get('workspace_uuid')
            or state.get('caller_surface_uuid') not in marker_callers(marker)
            or state.get('workspace_uuid') != marker.get('workspace_uuid')
            or any(not isinstance(marker.get(key), str) or not marker[key].strip()
                   for key in ('collaboration_id', 'task_id'))):
        return False
    foreign = owners[0] != state['supervisor']
    reserved = marker_epoch(marker)
    if event == 'RESERVED':
        if reserved is None and not observed and 'armed_at' not in marker:
            reserved = now
        if not finite(reserved) or reserved > now:
            return False
        # A still-present older owner's marker is not a handover. Neither a
        # heartbeat nor observing the marker later can refresh this timestamp.
        if foreign and (not finite(state.get('started_epoch'))
                        or reserved <= state['started_epoch']):
            return False
    elif foreign or 'armed_at' in marker and reserved is None:
        return False
    before = copy.deepcopy(state)
    reservation = dict(collaboration_id=marker['collaboration_id'], task_id=marker['task_id'])
    if not eligible:
        # Exact RELEASE is bookkeeping, not continuing authorization. Do not
        # poll or rewrite an already-terminal episode's reason/timestamps.
        return _release_reservation(state, reservation, now, publish=publish)
    if reask.stop_file(state['caller_surface_uuid']).exists() and not preview_resume:
        _boundary_stop(state, 'OPERATOR_STOP', now, publish=publish)
        if event == 'RELEASED':
            _release_reservation(state, reservation, now, publish=publish)
        return state != before
    poll_authorization(state, now, publish=publish)
    if state['state'] == reask.STOPPED:
        if event == 'RELEASED':
            _release_reservation(state, reservation, now, publish=publish)
        return state != before
    if foreign:
        _boundary_stop(state, 'FORMAL_OWNER_CHANGED', now, publish=publish,
                       formal_successor=dict(supervisor=owners[0],
                                             reserved_epoch=reserved, **reservation))
        return state != before
    paused = state.get('paused_tasks', [])
    if event == 'RESERVED' and reservation not in paused:
        if publish:
            reask.preserve_legacy_snapshot(state)
        state.setdefault('paused_tasks', []).append(reservation)
    elif event == 'RELEASED' and reservation in paused:
        _release_reservation(state, reservation, now, publish=publish)
    else:
        return state != before
    if event == 'RESERVED':
        state['last_task_boundary'] = dict(event=event, at_epoch=now, **reservation)
    return state != before


def observe_formal_tasks(state, now, *, publish=True, preview_resume=False):
    """Bounded observation while the caller lock is held, including old harnesses.

    Absence or TTL expiry never releases a recorded reservation. Only a matching
    formal RELEASED event can do that. A legacy writer racing this read remains
    a disclosed limit; CURRENT writers use marker_locks.
    """
    if preview_resume and publish:
        raise ValueError('operator resume is only allowed in an unpublished registration preview')
    if not _boundary_eligible(state):
        return
    workspace = state.get('workspace_uuid')
    if not isinstance(workspace, str) or not workspace or Path(workspace).name != workspace:
        return
    directory = ACTIVE_DIR / workspace
    paths = []
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_FORMAL_MARKERS:
                    raise ValueError('formal marker observation capacity exceeded')
                if entry.is_file(follow_symlinks=False) and entry.name.endswith('.json'):
                    paths.append(Path(entry.path))
    except FileNotFoundError:
        pass
    legacy = ACTIVE_DIR / (workspace + '.json')
    if legacy.is_file():
        paths.append(legacy)
    for path in sorted(paths):
        marker = ready.read_json(path)
        if marker:
            apply_boundary(state, marker, 'RESERVED', now, observed=True, publish=publish,
                           preview_resume=preview_resume)


def task_boundary_locked(marker, event, *, now=None):
    """Used only when the caller holds every marker participant lock."""
    import executor_reask as reask
    now = time.time() if now is None else now
    results = []
    for caller in marker_callers(marker):
        state = reask.load(caller)
        if state and apply_boundary(state, marker, event, now):
            # Includes revocation/stop changes, even if no reservation survives.
            reask.save(state)
            results.append(dict(caller=caller, event=event,
                                state=state['state'], paused_tasks=len(state.get('paused_tasks', []))))
    return results


def task_boundary(marker, event, *, now=None):
    """Reserve/release tasks from the formal harness, not from mailbox prose.

    A reservation pauses inquiries before input; it is not dispatch evidence.
    This helper never enables continuation, changes controllers, or sends input.
    """
    try:
        with marker_locks(marker):
            return task_boundary_locked(marker, event, now=now)
    except BlockingIOError:
        raise RuntimeError('inquiry state busy; task boundary was not recorded')

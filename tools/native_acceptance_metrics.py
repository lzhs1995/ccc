"""Strict local native timing acceptance; this does not certify UI or upstream service."""
import math


def evaluate_native_completion(records, session_id, rounds):
    """Validate a frozen fixture transcript, not a live run or cleanup claim.

    The fixture deliberately fails exactly `rounds` tasks before one final OK.
    Every completion must close its own started turn; a later start invalidates
    a previously successful completion. Input files and process identity remain
    the caller's responsibility.
    """
    errors, completed, seen = [], [], set()
    active, pending_input = None, False
    if (not isinstance(records, list) or not records
            or type(rounds) is not int or rounds < 1
            or not isinstance(session_id, str) or not session_id):
        return {'passed': False, 'errors': ['invalid_completion_inputs']}
    metadata = records[0]
    payload = metadata.get('payload', {}) if isinstance(metadata, dict) else {}
    if (not isinstance(metadata, dict) or metadata.get('type') != 'session_meta'
            or not isinstance(payload, dict)
            or payload.get('session_id', payload.get('id')) != session_id):
        errors.append('original_session_mismatch')
    for index, record in enumerate(records):
        if not isinstance(record, dict) or not isinstance(record.get('payload'), dict):
            errors.append('malformed_record')
            continue
        if record.get('type') == 'session_meta' and index:
            errors.append('repeated_session_metadata')
        if record.get('type') != 'event_msg':
            continue
        event = record['payload']
        kind, turn = event.get('type'), event.get('turn_id')
        if kind == 'task_started':
            pending_input = False
            if active is not None or not isinstance(turn, str) or not turn or turn in seen:
                errors.append('overlapping_or_duplicate_start')
            if isinstance(turn, str):
                seen.add(turn)
            active = turn
        elif kind == 'task_complete':
            if active is None or turn != active:
                errors.append('unmatched_completion')
            completed.append(event)
            active = None
        elif kind == 'turn_aborted':
            errors.append('aborted_task')
        elif kind == 'user_message' and completed and active is None:
            # A submitted/echoed followup without its start is not final quiescence.
            # Earlier between-round inputs are allowed if a later task starts.
            pending_input = True
    if active is not None or pending_input:
        errors.append('unfinished_tail')
    if len(completed) != rounds + 1:
        errors.append('incorrect_completed_rounds')
    if any(not row.get('error') for row in completed[:-1]):
        errors.append('unexpected_early_success')
    last = completed[-1] if completed else {}
    if not last or last.get('error') or last.get('last_agent_message') != 'OK':
        errors.append('missing_final_success')
    return {'passed': not errors, 'errors': errors, 'session_id': session_id,
            'completed_turns': len(completed), 'final_turn_id': last.get('turn_id'),
            'scope': 'frozen fixture lifecycle only; not live identity, cleanup or run terminal'}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def evaluate_native_timings(started_at, first_tasks, continuations, count, rounds):
    """Require complete, unique identities and every end-to-end latency in budget.

    first_tasks: surface_id/session_id/task_at per original native.
    continuations: one row per original failed turn, as produced by the fixture.
    The caller binds these rows to original claims/transcripts. No averages or
    percentile may substitute for the maximum, and missing rows never pass.
    """
    expected = count * rounds
    start_valid = _finite(started_at)
    startup_rows = []
    for row in first_tasks:
        at = row.get('task_at')
        delay = at - started_at if start_valid and _finite(at) else None
        startup_rows.append({**row, 'seconds': delay})
    identities = [(r.get('surface_id'), r.get('session_id')) for r in startup_rows]
    startup_complete = (count > 0 and len(startup_rows) == count
                        and len({s for s, _ in identities}) == count
                        and len({s for _, s in identities}) == count
                        and all(all(x) for x in identities))
    startup_passed = (startup_complete and all(
        _finite(r['seconds']) and 0 <= r['seconds'] <= 1 for r in startup_rows))
    failures = []
    turns = set()
    next_turns = set()
    input_ids = set()
    per_session = {}
    known = set(identities)
    for row in continuations:
        identity = (row.get('surface_id'), row.get('session_id'))
        key = (*identity, row.get('failed_turn'))
        reasons = []
        if identity not in known or not all(key) or key in turns:
            reasons.append('unknown_or_duplicate_failed_turn')
        turns.add(key)
        per_session[identity] = per_session.get(identity, 0) + 1
        if row.get('input_count') != 1:
            reasons.append('not_exactly_one_input')
        if not row.get('next_turn') or row.get('next_turn') == row.get('failed_turn'):
            reasons.append('missing_new_native_turn')
        next_key = (*identity, row.get('next_turn'))
        if next_key in next_turns:
            reasons.append('reused_next_native_turn')
        next_turns.add(next_key)
        input_id = row.get('input_id')
        if input_id is not None:
            if input_id in input_ids:
                reasons.append('reused_input')
            input_ids.add(input_id)
        for metric in ('forward_ms', 'ack_ms', 'native_next_ms'):
            value = row.get(metric)
            if not _finite(value) or not 0 <= value < 1000:
                reasons.append(metric + '_outside_budget')
        if reasons:
            failures.append({'surface_id': identity[0], 'failed_turn': key[-1], 'reasons': reasons})
    continuation_complete = (rounds > 0 and startup_complete and len(continuations) == expected
                             and all(per_session.get(k) == rounds for k in known))
    continuation_passed = continuation_complete and not failures
    return {'scope': 'local_worker_launch_to_native_task; not UI button or external provider',
            'startup_expected': count, 'startup_observed': len(startup_rows),
            'startup_complete': startup_complete, 'startup_passed': startup_passed,
            'first_task_startup_seconds': max((r['seconds'] for r in startup_rows
                                              if _finite(r['seconds'])), default=None),
            'continuation_expected': expected, 'continuation_observed': len(continuations),
            'continuation_complete': continuation_complete,
            'continuation_passed': continuation_passed, 'failures': failures,
            'local_performance_passed': startup_passed and continuation_passed}

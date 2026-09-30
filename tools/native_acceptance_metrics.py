"""Strict local native timing acceptance; this does not certify UI or upstream service."""
import math


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

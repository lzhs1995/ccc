#!/usr/bin/env python3
"""PreToolUse: end post-delivery work on the currently armed executor task."""
import json
import cmux_hook_identity as hook_identity
import cmux_hook_scope as hook_scope
import sys
from cmux_consensus_stop_guard import (
    _active_markers, _scope_candidates, _workspace_key, _surface_key,
)
from executor_closeout import terminal_report, superseded, closeout_instructions
from cmux_callback_queue_resume import allowed as queue_resume_allowed
from cmux_callback_reconcile import allowed as reconcile_allowed
from cmux_callback_diagnose import allowed as diagnose_allowed
from cmux_callback_no_input_successor import allowed as no_input_successor_allowed


def _evaluate_resolved(payload):
    if payload.get('hook_event_name') != 'PreToolUse':
        return True, ''
    workspace, surface = _workspace_key(payload), _surface_key(payload)
    markers = _active_markers(payload)
    for marker in markers:
        evidence = terminal_report(marker, workspace, surface)
        # Only a fresh, later task armed by the same supervisor moves scope.
        if evidence and not superseded(evidence, markers, workspace, surface):
            if (queue_resume_allowed(payload, marker, evidence)
                    or reconcile_allowed(payload, marker, evidence)
                    or diagnose_allowed(payload, marker, evidence)
                    or no_input_successor_allowed(payload, marker, evidence)):
                continue
            return False, (
                'EXECUTOR_CLOSEOUT: report frozen; original callback attempt returned. '
                'Preserve the frozen report and original callback attempt. '
                'Only the exact task-bound cmux_callback_reconcile.py command '
                '(zero terminal input), cmux_callback_diagnose.py, '
                'the existing guarded queue-resume entrypoint, or the exact original callback '
                'CLI once after sole attempt-0001 NO_INPUT/events=[] is allowed. '
                'A queued/entered/unknown attempt permits no new paste, watcher, or retry loop; '
                'no alternate sender. '
                'Otherwise end this turn honestly. The supervisor owns '
                'acceptance/disarm before another task; typed text cannot lift this '
                'seal. This is not product acceptance. '
                'This task boundary is not evidence of an API failure. Do not ask the '
                'user to relay status to the already-bound supervisor. After verified '
                'disarm, authorized follow-up uses current receipts and disposition, '
                'not an old recap; this is not a permanent session stop. '
                + closeout_instructions(evidence))
    return True, ''


def evaluate(payload):
    if payload.get('hook_event_name') != 'PreToolUse':
        return True, ''
    try:
        with hook_scope.evaluation(payload, _scope_candidates()) as markers:
            if not markers:
                return True, ''
            return _evaluate_resolved(payload)
    except hook_identity.ERRORS as exc:
        return False, 'TASK_EVIDENCE_INVALID: ' + str(exc)


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
    except (ValueError, OSError):
        return 0
    ok, reason = evaluate(payload)
    if not ok:
        print(reason, file=sys.stderr)
    return 0 if ok else 2


if __name__ == '__main__':
    raise SystemExit(main())

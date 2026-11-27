#!/usr/bin/env python3
"""Observe workflow hook payloads without making ordinary turns fail.

The workspace and panel guards remain the transport boundary.  This entrypoint
never calls the historical workflow gates. At the supervisor PostToolUse entry,
it also exposes authenticated file requests as context, without sending input,
settling tasks, or preventing ordinary work and turn-end.
"""
import json
import sys

WORKFLOW_HOOKS = frozenset({
    'cmux_executor_closeout_guard.py',
    'cmux_handshake_receipt_guard.py',
    'cmux_consensus_round_guard.py',
    'cmux_lease_guard.py',
    'cmux_consensus_stop_guard.py',
    'cmux_executor_idle_guard.py',
    'cmux_executor_reask_stop_guard.py',
    'cmux_native_delivery_guard.py',
    'cmux_supervisor_report_guard.py',
    'executor_reply_advisory.py',
    'callback_settlement_advisory.py',
    'handshake_advisory.py',
})
MAX_INPUT_CHARS = 2 * 1024 * 1024


def hook_from_argv(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] != '--hook':
        return None
    value = argv[1]
    return value if value in WORKFLOW_HOOKS else None


def main() -> int:
    # A hook must never turn an unreadable or stale payload into a session
    # block.  Keep stdin bounded by the host's normal hook payload limits.
    try:
        raw = sys.stdin.read(MAX_INPUT_CHARS + 1)
        if len(raw) > MAX_INPUT_CHARS:
            return 0
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            return 0
        hook = hook_from_argv()
        if not hook:
            return 0
    except BaseException:
        return 0

    try:
        import cmux_hook_runtime_audit as runtime_audit
        runtime_audit.record(
            payload,
            'ADVISORY_OBSERVED',
            original_hook=hook,
            mode='advisory',
        )
    except BaseException:
        # Advisory telemetry is never allowed to recreate the outage it
        # diagnoses.  Native delivery and explicit harness paths retain their
        # own strict checks and receipts.
        pass

    # Explicitly enrolled task participants asked for active continuation. This
    # one existing Stop registration enforces it; other/global workflow hooks
    # stay advisory. Never revive a legacy episode or add another sender here.
    if (hook == 'cmux_executor_idle_guard.py'
            and payload.get('hook_event_name') == 'Stop'):
        try:
            import executor_proactive_hook
            result = executor_proactive_hook.decide(payload)
            if result:
                print(json.dumps(result, ensure_ascii=False), flush=True)
                try:
                    runtime_audit.record(payload, 'TASK_CONTINUATION_REQUIRED',
                                         original_hook=hook, mode='advisory')
                except Exception:
                    pass
        except Exception as exc:
            # An invalid binding cannot own the session, but diagnostics must
            # distinguish no enrollment from a configured hook that failed.
            try:
                status = ('TASK_CONTINUATION_EPISODE_BUSY' if isinstance(exc, executor_proactive_hook.reask.EpisodeBusy)
                          else 'TASK_CONTINUATION_CADENCE_ACTIVE' if isinstance(exc, executor_proactive_hook.CadenceActive)
                          else 'TASK_CONTINUATION_UNAVAILABLE')
                runtime_audit.record(payload, status,
                                     original_hook=hook, mode='advisory')
            except Exception:
                pass

    # File fallback must have a reader. Keep discovery independent of telemetry
    # so a failed audit write cannot hide a valid request. Only this registered
    # event authenticates the current supervisor and offers bounded, deduplicated
    # context. No legacy checker, terminal sender, reask loop or task gate runs.
    if (hook == 'cmux_supervisor_report_guard.py'
            and payload.get('hook_event_name') == 'PostToolUse'):
        try:
            import supervisor_inbox
            supervisor_inbox.discover(payload, emit=supervisor_inbox.emit_context)
        except BaseException:
            pass
    if (hook == 'cmux_native_delivery_guard.py'
            and payload.get('hook_event_name') == 'PostToolUse'):
        try:
            import claude_maintenance_context
            claude_maintenance_context.discover(payload)
        except BaseException:
            pass
    if payload.get('hook_event_name') == 'PostToolUse':
        try:
            if hook == 'executor_reply_advisory.py':
                import executor_reply_advisory
                executor_reply_advisory.discover(payload)
            elif hook == 'callback_settlement_advisory.py':
                import callback_settlement_advisory
                callback_settlement_advisory.discover(payload)
            elif hook == 'handshake_advisory.py':
                import handshake_advisory
                handshake_advisory.discover(payload)
        except BaseException:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

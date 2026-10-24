"""Bounded receipt observation and diagnostics; this module never sends input."""
import hashlib
import math
from pathlib import Path
import sys
import time


DEFAULT_WAIT_SECONDS = 30.0
MAX_WAIT_SECONDS = 60.0
POLL_SECONDS = 1.0


def wait_budget(value=None, *, reconcile_only=False):
    """Validate before creating a journal, reserving an attempt or sending input."""
    if value is None:
        return 0.0 if reconcile_only else DEFAULT_WAIT_SECONDS
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value <= MAX_WAIT_SECONDS):
        raise ValueError("CONFIRMATION_WINDOW_INVALID: expected finite seconds in [0, 60]")
    return float(value)


def annotate_error(exc, **fields):
    details = getattr(exc, "details", {})
    exc.details = dict(details if isinstance(details, dict) else {}, **fields)
    return exc


def observe_receipt(observe, wait_seconds=None, *, reconcile_only=False,
                    stop_when_queued=False):
    """Observe one original intent, with both a time and an iteration ceiling.

    Synchronous identity/file I/O can extend elapsed wall time. Report its actual
    duration; neither a deadline nor a queued record establishes non-reception.
    The iteration ceiling also terminates if sleep/monotonic is stalled or mocked.
    """
    seconds = wait_budget(wait_seconds, reconcile_only=reconcile_only)
    started = time.monotonic()
    maximum = math.ceil(seconds / POLL_SECONDS) + 1
    count = 0

    def statistics(reason):
        return dict(requested_seconds=seconds,
                    elapsed_seconds=round(max(0.0, time.monotonic() - started), 6),
                    observation_count=count, max_observations=maximum,
                    poll_seconds=POLL_SECONDS, stop_reason=reason)

    for index in range(maximum):
        count += 1
        try:
            result = observe()
            if not isinstance(result, dict):
                raise ValueError("NATIVE_OBSERVATION_RESULT_INVALID")
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            raise annotate_error(exc, confirmation_observation=statistics("OBSERVATION_ERROR"))
        if result.get("confirmed") is True:
            reason = "NATIVE_RECEIVED"
        elif stop_when_queued and (result.get("state") == "NATIVE_QUEUED"
                                  or result.get("queued_at_receiver") is True):
            reason = "QUEUED_NOT_RECEIVED"
        elif seconds == 0:
            reason = "SINGLE_OBSERVATION"
        elif time.monotonic() - started >= seconds:
            reason = "OBSERVATION_WINDOW_EXHAUSTED"
        elif index + 1 == maximum:
            reason = "OBSERVATION_COUNT_EXHAUSTED"
        else:
            left = seconds - max(0.0, time.monotonic() - started)
            if left <= 0:
                reason = "OBSERVATION_WINDOW_EXHAUSTED"
            else:
                time.sleep(min(POLL_SECONDS, left))
                continue
        return dict(result, confirmation_observation=statistics(reason))


def controller_provenance(entrypoint):
    """Record the actual executing source for NEW attempts only.

    This is provenance, not a claim that the entire release has been verified.
    Never insert CURRENT into an old attempt that did not record its controller.
    """
    path = Path(entrypoint).resolve(strict=True)
    return dict(source=str(path.parent.parent), entrypoint=str(path),
                entrypoint_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                python=str(Path(sys.executable).resolve()))


def attempt_details(result, attempt_path, attempt, *, reconcile_only=False):
    """Carry original RPC diagnostics and zero-input guidance through exit 75."""
    details = dict(result or {})
    controller = attempt.get("controller_provenance")
    controller = controller if isinstance(controller, dict) else None
    details.update(attempt=str(attempt_path),
                   controller_provenance=controller or {"status": "UNKNOWN_LEGACY_CONTROLLER"},
                   business_accepted=False,
                   reconcile_guidance=dict(mode="--reconcile-only", input_operations=0,
                       reuse_original_parameters=True,
                       controller_source=controller.get("source") if controller else None,
                       instruction="Use the original controller, payload, target and nonce; "
                                   "observe the original attempt without resending."))
    events = attempt.get("events", [])
    rpc = [row for row in events if row.get("phase") == "NATIVE_INPUT_RESULT"]
    if rpc:
        # input_event_fields nests the exact, bounded diagnostic in evidence.
        evidence = rpc[-1].get("native_result", {})
        if evidence:
            details.setdefault("native_rpc_diagnostic", evidence)
            details.setdefault("native_rpc_outcome", evidence.get("rpc_outcome"))
    intents = [row for row in events if row.get("phase") == "NATIVE_INPUT_INTENT"]
    if intents:
        request = intents[-1].get("native_request", {})
        for name, key in (("native_metadata", "native_metadata"),
                          ("transport", "transport"), ("native_method", "method")):
            if key in request:
                details.setdefault(name, request[key])
    details.setdefault("original_attempt_phase", attempt.get("phase", "UNKNOWN"))
    if reconcile_only:
        details.update(input_operations=0, terminal_input_operations=0,
                       reconciled_read_only=True)
    return details


def annotate_attempt_error(exc, attempt_path, attempt, *, reconcile_only=False):
    return annotate_error(exc, **attempt_details(getattr(exc, "details", {}),
        attempt_path, attempt, reconcile_only=reconcile_only))


def observe_attempt(observe, attempt_path, attempt, wait_seconds=None, *,
                    reconcile_only=False, stop_when_queued=False):
    """Observe without editing the original attempt, retaining its diagnostics."""
    try:
        result = observe_receipt(observe, wait_seconds, reconcile_only=reconcile_only,
                                 stop_when_queued=stop_when_queued)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise annotate_attempt_error(exc, attempt_path, attempt,
                                     reconcile_only=reconcile_only)
    return attempt_details(result, attempt_path, attempt, reconcile_only=reconcile_only)


# Callback receipts intentionally select their fields instead of copying a scan.
# Preserve these diagnostic layers without replacing the task/report proof.
RECEIPT_DIAGNOSTICS = (
    "transport", "native_method", "native_rpc_outcome", "native_rpc_diagnostic",
    "native_metadata", "native_turn_id", "native_result_journal_error",
    "terminal_input_operations", "input_operations", "elapsed_ms",
    "confirmation_observation", "controller_provenance", "reconcile_guidance",
    "business_accepted", "original_attempt_phase", "native_turn_diagnostic",
)

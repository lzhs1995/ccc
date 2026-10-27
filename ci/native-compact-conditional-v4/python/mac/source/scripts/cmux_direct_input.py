"""One literal terminal paste, immediate Enter, and native marker receipts.

The caller owns the original journal and input lock. Busy work and compaction
are not admission gates. No draft is cleared or pasted twice. A single extra
Enter is permitted only after a pending receipt and visible residual/compaction.
"""
import math
import json
import re
import time

from cmux_confirmation import annotate_error, observe_receipt, wait_budget

TRANSPORT = "terminal-direct-marker-v1"


class ProtectedSubmitRefused(RuntimeError):
    """The CLI's exact pre-paste draft/dialog refusal, not arbitrary rc=1."""


def protected_submit_refusal(result, surface):
    if result.returncode != 1 or (result.stdout or '').strip():
        return None
    message = (result.stderr or '').strip()
    if message.startswith('Error: '):
        message = message[len('Error: '):]
    endings = (
        f'paste: the agent prompt in {surface} holds text someone is typing, so nothing was sent. Retry when the prompt is clear, or pass --force to send anyway.',
        f'paste: {surface} is waiting on a question or dialog, so nothing was sent. Retry once it is answered, or pass --force to send anyway.',
    )
    if message not in endings:
        return None  # Localized/unknown diagnostics stay uncertain.
    return ProtectedSubmitRefused('PROTECTED_SUBMIT_REFUSED: zero input; preserve draft/dialog')


def protected_submit_result(raw):
    """A host acknowledgement is never a receiver user-message receipt."""
    value = json.loads(raw)
    if isinstance(value, dict) and isinstance(value.get('result'), dict):
        value = value['result']
    if isinstance(value, dict) and value.get('delivery') == 'queued':
        return dict(submitted=False, delivery='queued')
    if not isinstance(value, dict) or type(value.get('submitted')) is not bool:
        raise RuntimeError('PROTECTED_SUBMIT_OUTCOME_UNKNOWN: observe original; no resend')
    return dict(submitted=value['submitted'], delivery=value.get('delivery'),
                submit_error=value.get('submit_error'))


def empty_claude_input(screen):
    """Only a complete, empty Claude input frame; no activity/footer gate."""
    rows = screen.splitlines()
    prompts = [i for i, row in enumerate(rows) if re.match(r'^\s*❯(?:\s|$)', row)]
    if not prompts:
        return False
    start = prompts[-1]
    if start == 0 or not re.fullmatch(r'─{8,}', rows[start - 1].strip()):
        return False
    border = rows[start - 1].strip()
    end = next((i for i in range(start + 1, len(rows)) if rows[i].strip() == border), None)
    if end is None:
        return False
    body = [re.sub(r'^\s*❯[ \u00a0]?', '', rows[start], count=1), *rows[start + 1:end]]
    return not '\n'.join(body).strip(' \t\r\n')


def require_prompt_state(raw, surface, workspace, *, read_claude_screen=None):
    """Do not inherit the CLI's fail-open behavior on an unavailable query."""
    value = json.loads(raw)
    if isinstance(value, dict) and isinstance(value.get('result'), dict):
        value = value['result']
    if (not isinstance(value, dict) or value.get('surface_id') != surface
            or value.get('workspace_id') != workspace
            or type(value.get('agent')) is not bool or value.get('terminal') is not True
            or type(value.get('blocks_typing')) is not bool
            or not isinstance(value.get('state'), str)):
        raise RuntimeError('PROMPT_STATE_UNKNOWN: zero text input')
    if value['blocks_typing'] or value['state'] != 'empty':
        raise RuntimeError('PROMPT_NOT_EMPTY: zero text input; preserve receiver draft')
    if value['agent'] is False:
        # Live Claude 2.1.303 can be absent from cmux's agent registry while our
        # independent native process/session binding is valid. The app then
        # returns an uninformative empty default, so require the real frame.
        if read_claude_screen is None or not empty_claude_input(read_claude_screen()):
            raise RuntimeError('PROMPT_STATE_UNKNOWN: zero text input')
        value = dict(value, draft_check_source='authenticated_claude_empty_frame')
    return value


def _screen_regions(screen):
    rows = screen.splitlines()
    prompts = [i for i, row in enumerate(rows) if re.match(r"^\s*[›❯>](?:\s|$)", row)]
    if not prompts:
        return rows, ""
    index = prompts[-1]
    return rows[:index], "\n".join(rows[index:])


def is_compacting(screen):
    """Inspect the latest activity row, excluding the current input body."""
    before, _ = _screen_regions(screen)
    activities = [row.strip() for row in before
                  if re.match(r"^\s*[•·⏺✻✢✳✶✽◐◑◒◓](?:\s|$)", row)
                  and not re.search(r"Queued follow-up|Messages to be submitted", row, re.I)]
    rows = activities[-1:] if activities else [row.strip() for row in before]
    return any(re.match(r"(?:[•·✻✢✳✶✽◐◑◒◓]\s+)?Compacting\b"
        r"(?:\s+(?:context|conversation))?(?:…|\.\.\.)?(?:\s*\([^\n]*\))?", row, re.I)
        for row in rows)


def residual_marker(screen, marker):
    # Recovery only: a marker in chat history does not authorize submitting a
    # different draft. No full composer parser/stability or initial-send gate.
    return marker in _screen_regions(screen)[1]


def owned_draft(screen, text):
    """A marker alone never authorizes submitting additional receiver text.

    Reuse measured rendering equivalence, without waiting for a stable screen
    or an idle turn. The simple one-row form has no footer/body ambiguity.
    """
    from cmux_bridge import _exact_pending_text
    if _exact_pending_text(screen, text):
        return True
    region = _screen_regions(screen)[1]
    rows = region.splitlines()
    return (len(rows) == 1 and
            re.sub(r"^\s*[›❯>][ \u00a0]?", "", rows[0], count=1) == text)


def recovery_draft_reason(screen, text):
    if is_compacting(screen):
        return "COMPACTION_STILL_ACTIVE"
    if not owned_draft(screen, text):
        return "ORIGINAL_DRAFT_NOT_OWNED"
    return None


def recover_once(text, marker, *, original, recheck, emit, key, observe,
                 read_screen, wait_seconds=None):
    """Spend an unused extra Enter on the original current-contract attempt.

    The caller holds its original locks and persists emit before every key.
    A deferred observation spends no budget; an uncertain key spends it forever.
    No paste, original-screen comparison, new fence or native RPC is permitted.
    """
    seconds = wait_budget(wait_seconds)
    binding = original.get("native_binding") or {}
    if (binding.get("receipt_contract") != "native-user-marker-v1"
            or binding.get("receipt_marker") != marker or not marker or marker not in text):
        raise ValueError("DIRECT_ORIGINAL_MARKER_CONTRACT_REQUIRED")
    events = original.get("events", [])
    phases = [event.get("phase") for event in events]
    if (any(phases.count(p) != 1 for p in
            ("PASTE_INTENT", "PASTED", "ENTER_INTENT"))
            or phases.count("ENTER_SENT") + phases.count("ENTER_NOT_SENT") != 1
            or any("TAB" in str(p) or p in ("EXTRA_ENTER_INTENT", "EXTRA_ENTER_SENT",
                    "NATIVE_INPUT_INTENT") for p in phases)):
        raise ValueError("DIRECT_RECOVERY_BUDGET_UNAVAILABLE: reconcile original")
    operations = 0
    try:
        recheck()
        result = observe()
        if not result.get("confirmed") and result.get("state") == "NATIVE_PENDING":
            screen = read_screen()
            emit("DIRECT_RECOVERY_OBSERVATION", screen)
            reason = recovery_draft_reason(screen, text)
            if reason:
                result = dict(result, extra_enter_deferred=reason)
            else:
                result = observe()
                if not result.get("confirmed") and result.get("state") == "NATIVE_PENDING":
                    recheck()
                    screen = read_screen()
                    emit("DIRECT_RECOVERY_OBSERVATION", screen)
                    reason = recovery_draft_reason(screen, text)
                    if reason:
                        result = dict(result, extra_enter_deferred=reason)
                    else:
                        emit("EXTRA_ENTER_INTENT", screen)
                        recheck()
                        operations = 1
                        key("enter")
                        emit("EXTRA_ENTER_SENT")
                        result = observe_receipt(observe, seconds, stop_when_queued=True)
        return dict(result, transport=TRANSPORT, recovered="enter" if operations else "observed",
                    input_operations=operations, terminal_input_operations=operations,
                    extra_enter=operations, retries=0)
    except BaseException as exc:
        raise annotate_error(exc, transport=TRANSPORT, input_operations=operations,
            terminal_input_operations=operations, extra_enter=operations, no_repaste=True)


def send_once(text, marker, *, recheck, emit, paste, key, observe, read_screen,
              wait_seconds=None, submit=None, preflight=None):
    if not isinstance(marker, str) or not marker or marker not in text:
        raise ValueError("DIRECT_UNIQUE_MARKER_REQUIRED")
    seconds = wait_budget(wait_seconds)
    started = time.monotonic()
    operations = 0
    extra = False
    host_queued = False
    refused = False
    try:
        recheck()
        if preflight is not None:
            preflight()  # Read-only; failure precedes every input intent.
        # Empty screen records that no composer inspection was required. The
        # caller captures/fsyncs the original transcript fence in this event.
        emit("PASTE_INTENT", "")
        recheck()
        if submit is not None:
            # Both intents precede the one host request. A refused/uncertain
            # command never falls through to a separate send-key operation.
            emit("ENTER_INTENT", "")
            recheck()
            operations += 2
            try:
                acknowledgement = submit(text)
            except ProtectedSubmitRefused:
                # Preserve both intent records. This terminal event describes
                # the exact CLI pre-paste refusal, not permission to retry.
                emit('PROTECTED_SUBMIT_REFUSED')
                operations = 0
                refused = True
                raise
            if (not isinstance(acknowledgement, dict)
                    or type(acknowledgement.get("submitted")) is not bool):
                raise RuntimeError("PROTECTED_SUBMIT_OUTCOME_UNKNOWN: observe original")
            host_queued = acknowledgement.get('delivery') == 'queued'
            if host_queued:
                emit('PASTE_QUEUED')  # Host PTY queue is not native reception.
            else:
                emit("PASTED")
                emit("ENTER_SENT" if acknowledgement["submitted"] else "ENTER_NOT_SENT")
        else:
            operations += 1
            paste(text)
            emit("PASTED")
            emit("ENTER_INTENT", "")
            recheck()
            operations += 1
            key("enter")
            emit("ENTER_SENT")
        result = observe_receipt(observe, seconds, stop_when_queued=True)
        if host_queued and not result.get('confirmed'):
            result = dict(result, extra_enter_deferred='HOST_INPUT_QUEUED')
        if not host_queued and not result.get("confirmed") and result.get("state") == "NATIVE_PENDING":
            # Observation here is solely for the extra-key budget, never for
            # initial admission. A queue already accepted input needs no key.
            screen = read_screen()
            emit("DIRECT_RECOVERY_OBSERVATION", screen)
            recovery_started = time.monotonic()
            # Initial sending never waits for compaction. Only the single
            # recovery key waits: pressing it during compaction can swallow it.
            for index in range(math.ceil(seconds) + 1):
                if not is_compacting(screen):
                    break
                result = observe()
                if result.get("confirmed") or result.get("state") != "NATIVE_PENDING":
                    break
                left = seconds - (time.monotonic() - recovery_started)
                if left <= 0 or index == math.ceil(seconds):
                    break
                time.sleep(min(1.0, left))
                screen = read_screen()
                emit("DIRECT_RECOVERY_OBSERVATION", screen)
            if is_compacting(screen):
                result = dict(result, extra_enter_deferred="COMPACTION_STILL_ACTIVE")
            elif owned_draft(screen, text):
                result = observe()  # close the receipt race before one extra key
                if not result.get("confirmed") and result.get("state") == "NATIVE_PENDING":
                    recheck()
                    screen = read_screen()
                    emit("DIRECT_RECOVERY_OBSERVATION", screen)
                    reason = recovery_draft_reason(screen, text)
                    if reason:
                        result = dict(result, extra_enter_deferred=reason)
                    else:
                        emit("EXTRA_ENTER_INTENT", screen)
                        recheck()
                        extra = True
                        operations += 1
                        key("enter")
                        emit("EXTRA_ENTER_SENT")
                        result = observe_receipt(observe, seconds, stop_when_queued=True)
            else:
                result = dict(result, extra_enter_deferred="ORIGINAL_DRAFT_NOT_OWNED")
        return dict(result, transport=TRANSPORT, input_operations=operations,
                    terminal_input_operations=operations,
                    submission_key="agent-aware-return" if submit is not None else "enter",
                    protected_submit=submit is not None,
                    extra_enter=int(extra), retries=0,
                    elapsed_ms=round((time.monotonic() - started) * 1000, 3))
    except BaseException as exc:
        raise annotate_error(exc, transport=TRANSPORT,
            input_operations=operations, terminal_input_operations=operations,
            input_operation_semantics='confirmed_zero' if refused else 'intent_budget',
            input_intents_recorded=2 if refused else operations,
            actual_input_operations=0 if refused or operations == 0 else None,
            input_outcome='REFUSED_BEFORE_PASTE' if refused else ('NO_INPUT' if operations == 0 else 'UNKNOWN'),
            extra_enter=int(extra), no_repaste=True)

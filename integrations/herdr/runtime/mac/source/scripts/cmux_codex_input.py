"""Native input to the bound Codex thread, with pre-input terminal fallback.

An active turn uses turn/steer with its exact id; an idle thread uses turn/start
without changing any settings. One durable intent authorizes one RPC request.
An ambiguous mutation is observed, never retried or converted to terminal input.
Only the original native transcript can prove reception.
"""
import contextlib
import hashlib
import json
from pathlib import Path
import time
import uuid

import cmux_native_delivery as native
from cmux_codex_rpc import DaemonRPC, RPCError
from cmux_confirmation import annotate_error, observe_receipt, wait_budget

TRANSPORT = "codex-native-input-v1"
INPUT_PHASE = "NATIVE_INPUT_INTENT"


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _turn_id(value):
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise native.NativeDeliveryError("CODEX_ACTIVE_TURN_INVALID")
    return value


def _bounded(value, depth=0):
    """Keep structured error fields bounded without inferring HTTP from prose."""
    if depth > 5:
        return "METADATA_DEPTH_LIMIT"
    if isinstance(value, dict):
        return {str(k)[:120]: _bounded(v, depth + 1) for k, v in list(value.items())[:24]}
    if isinstance(value, list):
        return [_bounded(v, depth + 1) for v in value[:24]]
    if isinstance(value, str):
        return value[:1500]
    return value if value is None or type(value) in (bool, int, float) else type(value).__name__


def rpc_error_details(exc):
    value = dict(error_type=type(exc).__name__, error=str(exc)[:1500])
    if isinstance(exc, RPCError):
        value.update(method=exc.method, rpc_error=_bounded(exc.error))
    return value


@contextlib.contextmanager
def _metadata_client():
    """Connection failure precedes any input; later failures keep their phase."""
    with contextlib.ExitStack() as stack:
        try:
            client = stack.enter_context(DaemonRPC(timeout=8))
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            raise annotate_error(exc, transport=TRANSPORT, input_operations=0,
                terminal_input_operations=0, native_metadata=dict(
                    metadata_rpc_status="UNAVAILABLE", metadata_phase="CONNECT",
                    metadata_rpc_error=rpc_error_details(exc)))
        yield client


def read_metadata(client, binding, text):
    """Two metadata RPCs only; no composer, historical hydration or mutation."""
    native._validate_binding(binding, text)
    if binding["provider"] != "codex":
        raise native.NativeDeliveryError("CODEX_NATIVE_PROVIDER_REQUIRED")
    thread_id = binding["session_id"]
    thread = client.request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
    if not isinstance(thread, dict) or (thread.get("id") != thread_id
            or thread.get("path") != binding["transcript"]["path"]):
        raise native.NativeDeliveryError("CODEX_NATIVE_THREAD_NOT_ACCEPTING: no terminal fallback")
    # Re-read the latest turn instead of inferring activity from a TUI spinner.
    turns = client.request("thread/turns/list", {"threadId": thread_id, "limit": 1,
        "sortDirection": "desc", "itemsView": "notLoaded"})["data"]
    if not isinstance(turns, list) or len(turns) > 1 or any(not isinstance(t, dict) for t in turns):
        raise native.NativeDeliveryError("CODEX_NATIVE_TURNS_INVALID")
    status_value = thread.get("status")
    status = status_value.get("type") if isinstance(status_value, dict) else None
    latest = turns[0] if turns else None
    metadata = dict(thread_id=thread_id, thread_status=status,
        can_accept_direct_input=thread.get("canAcceptDirectInput"),
        latest_turn_id=latest.get("id") if latest else None,
        latest_turn_status=latest.get("status") if latest else None,
        latest_turn_interrupted=latest.get("status") == "interrupted" if latest else False,
        latest_turn_error=_bounded(latest.get("error")) if latest else None,
        provider_error_source="latest_turn.error" if latest and latest.get("error") else None,
        metadata_rpc_status="AVAILABLE", observed_at_epoch=time.time())
    if status == "active" and latest and latest.get("status") == "inProgress":
        method = "turn/steer"
    elif status == "idle" and (latest is None or latest.get("status") in
                               ("completed", "interrupted", "failed")):
        method = "turn/start"
    else:
        method = None
    metadata["available_input_method"] = method if thread.get("canAcceptDirectInput") is True else None
    if metadata["available_input_method"] is None:
        metadata["resume_guidance"] = (
            "Input is unavailable in the observed native state. If the original client "
            "shows 'Goal stalled (/goal resume)', the user must resume that Goal in "
            "the original session; this probe neither resumes nor sends input. "
            "Unknown metadata alone does not establish Goal or provider failure.")
    return metadata


def prepare(client, binding, text):
    """Bounded metadata only: never hydrate histories or resume another thread."""
    try:
        metadata = read_metadata(client, binding, text)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise annotate_error(exc, native_metadata=dict(metadata_rpc_status="UNAVAILABLE",
            metadata_rpc_error=rpc_error_details(exc)), input_operations=0)
    method = metadata["available_input_method"]
    if method is None:
        reason = ("CODEX_NATIVE_THREAD_NOT_ACCEPTING" if metadata["can_accept_direct_input"] is not True
                  else "CODEX_NATIVE_TURN_CHANGED_OR_UNAVAILABLE")
        raise annotate_error(native.NativeDeliveryError(reason + ": native method unavailable before input"),
                             native_metadata=metadata, input_operations=0)
    params = {"threadId": binding["session_id"], "input": [{"type": "text", "text": text}]}
    if method == "turn/steer":
        params["expectedTurnId"] = _turn_id(metadata["latest_turn_id"])
    # One same-thread start also handles a new active turn in the narrow race;
    # no settings overrides and no second request are ever allowed.
    return dict(transport=TRANSPORT, method=method, params=params,
                socket=dict(client.socket_identity), observed_status=metadata["thread_status"],
                observed_turn_id=metadata["latest_turn_id"], native_metadata=metadata,
                observed_at_epoch=time.time())


def probe_target(bridge, surface):
    """No-attempt read-only diagnostic for an authenticated existing receiver."""
    text = "CODEX_METADATA_PROBE_ONLY_NO_INPUT"
    binding = native.bind_target(bridge, surface, text)
    native.require_bound(bridge, surface, binding, text)
    if binding["provider"] != "codex":
        raise native.NativeDeliveryError("CODEX_NATIVE_PROVIDER_REQUIRED")
    try:
        with DaemonRPC(timeout=8) as client:
            metadata = read_metadata(client, binding, text)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        metadata = dict(metadata_rpc_status="UNAVAILABLE", metadata_rpc_error=rpc_error_details(exc))
    native.require_bound(bridge, surface, binding, text)
    return dict(state="READ_ONLY_NATIVE_PROBE", native_metadata=metadata,
                receiver_session_id=binding["session_id"], receiver_identity=binding["identity"],
                input_operations=0, journal_created=False, native_receipt_established=False)


def validate_request(value, binding, text):
    """No setting overrides, alternate thread, changed payload or unpinned peer."""
    if not isinstance(value, dict) or value.get("transport") != TRANSPORT:
        raise native.NativeDeliveryError("ORIGINAL_NATIVE_REQUEST_REQUIRED")
    method, params = value.get("method"), value.get("params")
    expected = {"threadId": binding["session_id"], "input": [{"type": "text", "text": text}]}
    if method == "turn/steer":
        expected["expectedTurnId"] = _turn_id(params.get("expectedTurnId")) if isinstance(params, dict) else None
    elif method != "turn/start":
        raise native.NativeDeliveryError("NATIVE_INPUT_METHOD_INVALID")
    if binding["provider"] != "codex" or params != expected:
        raise native.NativeDeliveryError("NATIVE_INPUT_REQUEST_CHANGED")
    import os
    peer = value.get("socket", {})
    if (not isinstance(peer, dict) or peer.get("uid") != os.geteuid()
            or peer.get("peer_uid") != os.geteuid() or peer.get("mode") != 0o600
            or type(peer.get("device")) is not int or type(peer.get("inode")) is not int
            or not isinstance(peer.get("path"), str) or not Path(peer["path"]).is_absolute()):
        raise native.NativeDeliveryError("NATIVE_INPUT_SOCKET_PROOF_REQUIRED")
    return hashlib.sha256(_canonical(value)).hexdigest()


def _send_native_once(binding, text, *, recheck, emit, observe, wait_seconds=None):
    """Callers hold their existing journal locks and fsync emit() before input."""
    seconds = wait_budget(wait_seconds)
    recheck()
    started = time.monotonic()
    outcome = "UNKNOWN"
    returned_turn = None
    journal_error = None
    with _metadata_client() as client:
        request = prepare(client, binding, text)
        validate_request(request, binding, text)
        recheck()
        emit(INPUT_PHASE, evidence={"native_request": request})
        # A failed check after intent consumes the attempt too. No retroactive
        # NO_INPUT, even if the native mutation has not yet been attempted.
        recheck()
        try:
            result = client.request(request["method"], request["params"])
            returned_turn = (result.get("turnId") if request["method"] == "turn/steer"
                             else result.get("turn", {}).get("id"))
            _turn_id(returned_turn)
            if request["method"] == "turn/steer" and returned_turn != request["params"]["expectedTurnId"]:
                raise native.NativeDeliveryError("NATIVE_INPUT_RESPONSE_TURN_CHANGED")
            outcome = "RPC_ACCEPTED"
            diagnostic = {"method": request["method"], "turn_id": returned_turn,
                          "rpc_outcome": outcome}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            # Even a timeout may have delivered input. No second mutation,
            # new nonce, key, or queue fallback is authorized by this error.
            outcome = "RPC_ERROR_OR_UNKNOWN"
            diagnostic = dict(rpc_error_details(exc), method=request["method"], rpc_outcome=outcome)
        # The durable original intent remains the authority. A diagnostic
        # write failure must neither retry input nor skip receipt observation.
        try:
            emit("NATIVE_INPUT_RESULT", evidence=diagnostic)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            journal_error = {"error_type": type(exc).__name__, "error": str(exc)[:1500]}
    fields = dict(transport=TRANSPORT, native_method=request["method"],
        native_rpc_outcome=outcome, native_rpc_diagnostic=diagnostic,
        native_metadata=request["native_metadata"], native_turn_id=returned_turn,
        native_result_journal_error=journal_error, terminal_input_operations=0,
        input_operations=1, submission_key=None, retries=0)
    try:
        proof = observe_receipt(observe, seconds)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        raise annotate_error(exc, **fields)
    return dict(proof, **fields, elapsed_ms=round((time.monotonic() - started) * 1000, 3))


def send_once(binding, text, *, recheck, emit, observe, wait_seconds=None, fallback=None):
    """Prefer native input; unavailable transport before intent may use terminal.

    Never fall back after an input intent, RPC timeout, uncertain result or
    changed receiver identity. The fallback keeps this same original journal.
    """
    intended = False
    def record(phase, *, evidence):
        nonlocal intended
        if phase == INPUT_PHASE:
            intended = True
        emit(phase, evidence=evidence)
    try:
        return _send_native_once(binding, text, recheck=recheck, emit=record,
                                 observe=observe, wait_seconds=wait_seconds)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        import errno
        metadata = getattr(exc, "details", {}).get("native_metadata", {})
        unavailable = (isinstance(exc, OSError) and exc.errno in
                       (errno.ENOENT, errno.ECONNREFUSED)
                       and metadata.get("metadata_phase") == "CONNECT")
        unavailable = unavailable or (metadata.get("metadata_rpc_status") == "AVAILABLE"
            and metadata.get("available_input_method") is None)
        if fallback is None or intended or not unavailable:
            raise
        recheck()
        return dict(fallback(), native_fallback_reason=rpc_error_details(exc))

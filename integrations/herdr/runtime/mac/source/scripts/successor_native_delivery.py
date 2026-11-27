"""Native delivery for the explicitly authorized successor pair.

The ordinary native-delivery-v1 binding is intentionally untouched.  This
module carries both workspace identities and uses the successor bridge for
every target paste/key operation.
"""
from __future__ import annotations

import hashlib
import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

import cmux_native_delivery as native
import successor_rebind
import cmux_prompt_reference as prompt_reference
from cmux_evidence_io import open_regular_lock, read_bytes
from cmux_confirmation import (wait_budget, observe_receipt, controller_provenance,
                               attempt_details, annotate_attempt_error)


SCHEMA = "native-successor-delivery-v1"


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _target_row(bridge, target_uuid):
    tree = json.loads(bridge._run("tree", "--all", "--json", "--id-format", "both"))
    rows = []
    for window in tree.get("windows", []):
        for workspace in window.get("workspaces", []):
            for pane in workspace.get("panes", []):
                for surface in pane.get("surfaces", []):
                    if str(surface.get("id", "")).upper() == str(target_uuid).upper():
                        rows.append({"workspace_uuid": workspace.get("id"),
                                     "target_surface_uuid": surface.get("id"),
                                     "target_pane_uuid": pane.get("id"),
                                     "tty": surface.get("tty"),
                                     "kind": surface.get("type"),
                                     "dock": surface.get("dock_scope")})
    if len(rows) != 1 or rows[0]["kind"] != "terminal" or rows[0]["dock"] == "global" or not rows[0]["tty"]:
        raise successor_rebind.SuccessorRebindError("SUCCESSOR_REBIND_DENIED: target terminal changed")
    return rows[0]


def bind_target(bridge, artifact, text):
    if not isinstance(text, str) or not text:
        raise native.NativeDeliveryError("NATIVE_EMPTY_PAYLOAD")
    pair = bridge.pin_successor_pair(artifact)
    row = _target_row(bridge, pair["target_surface_uuid"])
    if (row["workspace_uuid"].upper() != pair["target_workspace_uuid"].upper()
            or row["target_pane_uuid"].upper() != pair["target_pane_uuid"].upper()):
        raise native.NativeDeliveryError("NATIVE_SUCCESSOR_TARGET_WORKSPACE_OR_PANE_CHANGED")
    process = native._target_process(row)
    provider = native._provider(process)
    session = native._live_session(process, provider)
    _require_native_transport(artifact, provider, session)
    path = native._transcript_path(provider, session)
    binding = {
        "schema": SCHEMA,
        "binding_mode": artifact["binding_mode"],
        "artifact_sha256": _hash(_canonical(artifact)),
        "identity": {
            "caller_workspace_uuid": pair["caller_workspace_uuid"],
            "caller_surface_uuid": pair["caller_surface_uuid"],
            "caller_pane_uuid": pair["caller_pane_uuid"],
            "target_workspace_uuid": pair["target_workspace_uuid"],
            "target_surface_uuid": pair["target_surface_uuid"],
            "target_pane_uuid": pair["target_pane_uuid"],
        },
        "target_tty": row["tty"],
        "process": native._public_process(process),
        "provider": provider,
        "session_id": session,
        "transcript": native._pin_file(path, provider, session),
        "bound_at_epoch": time.time(),
        "payload_sha256": _hash(text.encode()),
    }
    require_bound(bridge, artifact, binding, text)
    return binding


def _require_native_transport(artifact, provider, session):
    if artifact.get("transport") == successor_rebind.CODEX_SAME_PANE:
        if (provider != "codex" or str(session).lower() != artifact["target_thread_id"].lower()
                or str(session).lower() == artifact["caller_thread_id"].lower()):
            raise native.NativeDeliveryError("NATIVE_SAME_PANE_CODEX_THREAD_REQUIRED: zero terminal input")


def _legacy_binding(binding):
    """Use the existing strict record matcher for the target transcript only."""
    result = dict(binding, schema="native-delivery-v1")
    result["identity"] = {
        "workspace_uuid": binding["identity"]["target_workspace_uuid"],
        "caller_surface_uuid": binding["identity"]["caller_surface_uuid"],
        "target_surface_uuid": binding["identity"]["target_surface_uuid"],
        "target_pane_uuid": binding["identity"]["target_pane_uuid"],
    }
    return result


def require_bound(bridge, artifact, binding, text):
    if not isinstance(binding, dict) or binding.get("schema") != SCHEMA:
        raise native.NativeDeliveryError("ORIGINAL_SUCCESSOR_BINDING_REQUIRED")
    if (binding.get("artifact_sha256") != _hash(_canonical(artifact))
            or binding.get("binding_mode") != artifact.get("binding_mode")):
        raise native.NativeDeliveryError("NATIVE_SUCCESSOR_AUTHORIZATION_CHANGED")
    native._validate_binding(_legacy_binding(binding), text)
    _require_native_transport(artifact, binding.get("provider"), binding.get("session_id"))
    pair = bridge.pin_successor_pair(artifact)
    identity = binding.get("identity", {})
    expected = {
        "caller_workspace_uuid": pair["caller_workspace_uuid"],
        "caller_surface_uuid": pair["caller_surface_uuid"],
        "caller_pane_uuid": pair["caller_pane_uuid"],
        "target_workspace_uuid": pair["target_workspace_uuid"],
        "target_surface_uuid": pair["target_surface_uuid"],
        "target_pane_uuid": pair["target_pane_uuid"],
    }
    if identity != expected or binding.get("payload_sha256") != _hash(text.encode()):
        raise native.NativeDeliveryError("NATIVE_SUCCESSOR_IDENTITY_CHANGED")
    row = _target_row(bridge, pair["target_surface_uuid"])
    if (row["workspace_uuid"].upper() != pair["target_workspace_uuid"].upper()
            or row["target_pane_uuid"].upper() != pair["target_pane_uuid"].upper()
            or row["tty"] != binding.get("target_tty")):
        raise native.NativeDeliveryError("NATIVE_SUCCESSOR_TARGET_MOVED")
    process = native.identity.process(binding["process"]["pid"])
    if native._public_process(process) != binding["process"]:
        raise native.NativeDeliveryError("NATIVE_SUCCESSOR_PROCESS_CHANGED")
    tty = native.subprocess.run(["/bin/ps", "-p", str(process["pid"]), "-o", "tty="],
                                capture_output=True, text=True, timeout=3, check=True).stdout.strip()
    if tty != binding["target_tty"]:
        raise native.NativeDeliveryError("NATIVE_SUCCESSOR_PROCESS_TTY_CHANGED")
    if process["env"].get("CMUX_SURFACE_ID", "").upper() != pair["target_surface_uuid"].upper() or process["env"].get("CMUX_WORKSPACE_ID", "").upper() != pair["target_workspace_uuid"].upper():
        raise native.NativeDeliveryError("NATIVE_SUCCESSOR_PROCESS_SCOPE_CHANGED")
    if native._provider(process) != binding["provider"] or native._live_session(process, binding["provider"]) != binding["session_id"]:
        raise native.NativeDeliveryError("NATIVE_SUCCESSOR_SESSION_CHANGED")
    with native._open(binding["transcript"]["path"]) as handle:
        native._check_file(handle, _legacy_binding(binding))
    return pair


def scan_received(bridge, artifact, binding, text, *, paste_fence=None, not_before=None):
    """Read complete native records after the original persisted paste fence."""
    require_bound(bridge, artifact, binding, text)
    legacy = _legacy_binding(binding)
    result = native.probe(legacy, text, paste_fence=paste_fence, not_before=not_before)
    require_bound(bridge, artifact, binding, text)
    return dict(result, observed_at=time.time(), binding=binding)


def _exclusive_json(path, value):
    """Immutable original evidence is fsynced before the next terminal action."""
    path = Path(path)
    raw = _canonical(value) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _paths(artifact, marker, state_root=None):
    root = Path(state_root) if state_root is not None else Path.home() / ".local/state/multi-agent-collaboration"
    if not root.is_absolute():
        raise ValueError("SUCCESSOR_JOURNAL_REQUIRES_ABSOLUTE_ROOT")
    caller = successor_rebind._uuid(artifact["successor_surface_uuid"], "successor_surface_uuid")
    key = _hash(_canonical([caller, marker]))
    return root, root / "successor-dispatch-v2" / key


@contextlib.contextmanager
def _locked(root, journal, artifact):
    """Serialize with ordinary senders, task dispatch and sibling target tabs."""
    legacy = root / "deliveries-v1"
    task = root / "task-dispatch-v1"
    for directory in (legacy, task, journal):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = successor_rebind._uuid(artifact["target_workspace_uuid"], "target_workspace_uuid")
    pane = successor_rebind._uuid(artifact["target_pane_uuid"], "target_pane_uuid")
    target = successor_rebind._uuid(artifact["target_executor_uuid"], "target_executor_uuid")
    locks = (legacy / ("pane-" + _hash((workspace + ":" + pane).encode()) + ".lock"),
             legacy / ("target-" + _hash(target.encode()) + ".lock"),
             task / ("target-" + _hash(target.encode()) + ".lock"), journal / "delivery.lock")
    with contextlib.ExitStack() as stack:
        for path in locks:
            lock = stack.enter_context(open_regular_lock(path))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise native.NativeDeliveryError("SUCCESSOR_DELIVERY_IN_PROGRESS: zero input") from exc
        yield


def _event(journal, phase, **fields):
    paths = list(journal.glob("event-*.json"))
    value = dict(fields, phase=phase, at_epoch=time.time())
    _exclusive_json(journal / ("event-%04d.json" % (len(paths) + 1)), value)
    return value


def _original(journal, artifact, text, marker):
    path = journal / "attempt.json"
    attempt = json.loads(read_bytes(path))
    if (attempt.get("schema") != "successor-attempt-v2"
            or attempt.get("artifact") != artifact
            or attempt.get("artifact_sha256") != _hash(_canonical(artifact))
            or attempt.get("marker") != marker or attempt.get("body") != text
            or attempt.get("body_sha256") != _hash(text.encode())):
        raise native.NativeDeliveryError("SUCCESSOR_ORIGINAL_ATTEMPT_CHANGED")
    wire = attempt["wire"]
    if attempt.get("wire_sha256") != _hash(wire.encode()):
        raise native.NativeDeliveryError("SUCCESSOR_WIRE_CHANGED")
    reference = attempt.get("body_reference")
    if reference is not None:
        if (prompt_reference.wire_reference(wire) != reference
                or prompt_reference.verify(reference, text) != attempt.get("body_pin")):
            raise native.NativeDeliveryError("SUCCESSOR_ORIGINAL_BODY_CHANGED")
    elif wire != text or attempt.get("body_pin") is not None:
        raise native.NativeDeliveryError("SUCCESSOR_ORIGINAL_BODY_CHANGED")
    events = [json.loads(read_bytes(p)) for p in sorted(journal.glob("event-*.json"))]
    if not any(event.get("phase") in ("PASTE_INTENT", "NATIVE_INPUT_INTENT") for event in events):
        return attempt, None, None
    intent, fence = native.original_input_event(events,
        _legacy_binding(attempt["native_binding"]), wire, screen_hash_length=64)
    return attempt, intent, fence


def _attempt_evidence(journal, attempt):
    events = [json.loads(read_bytes(p)) for p in sorted(journal.glob("event-*.json"))]
    return dict(attempt, events=events, phase=events[-1].get("phase", "UNKNOWN") if events else "NO_INPUT")


def _scan_original(bridge, journal, artifact, attempt, intent, fence):
    """Keep the cursor separate from the frozen attempt and original events."""
    from cmux_callback_journal import write_json
    binding, wire = attempt["native_binding"], attempt["wire"]
    require_bound(bridge, artifact, binding, wire)
    legacy = _legacy_binding(binding)
    path = journal / "native-scan.json"
    pins = dict(schema="successor-native-scan-v1", attempt=str(journal / "attempt.json"),
                attempt_sha256=_hash(read_bytes(journal / "attempt.json")),
                binding_sha256=_hash(_canonical(binding)),
                intent_sha256=_hash(_canonical([intent, fence])))
    old = json.loads(read_bytes(path)) if path.exists() else {}
    if not isinstance(old, dict) or (old and any(old.get(k) != v for k, v in pins.items())):
        raise native.NativeDeliveryError("SUCCESSOR_NATIVE_SCAN_BINDING_CHANGED")
    if old.get("confirmed") is True:
        if not native.validate_proof(legacy, wire, old.get("native_proof"),
                                     paste_fence=fence, not_before=intent["at_epoch"]):
            raise native.NativeDeliveryError("SUCCESSOR_NATIVE_PROOF_CHANGED")
        result = old
    else:
        result = native.probe(legacy, wire, cursor=old.get("next_offset"),
                              paste_fence=fence, not_before=intent["at_epoch"])
        if not result.get("queued_proof") and old.get("queued_proof"):
            result["queued_proof"] = old["queued_proof"]
            if result.get("state") == "NATIVE_PENDING":
                result["state"] = "NATIVE_QUEUED"
        write_json(path, dict(result, **pins))
    from cmux_turn_diagnostic import observe as observe_turn
    events = _attempt_evidence(journal, attempt)['events']
    diagnostic = observe_turn(legacy, wire, events, journal / 'attempt.json',
                              journal / 'turn-scan.json', screen_hash_length=64)
    require_bound(bridge, artifact, binding, wire)
    return dict(result, observed_at=time.time(), binding=binding,
                native_turn_diagnostic=diagnostic)


def _observe(bridge, journal, artifact, text, marker):
    attempt, intent, fence = _original(journal, artifact, text, marker)
    if intent is None:
        require_bound(bridge, artifact, attempt["native_binding"], attempt["wire"])
        result = dict(confirmed=False, state="NO_INPUT", input_operations=0)
    else:
        result = _scan_original(bridge, journal, artifact, attempt, intent, fence)
        if result.get("confirmed") is True and not native.validate_proof(
                _legacy_binding(attempt["native_binding"]), attempt["wire"], result.get("native_proof"),
                paste_fence=fence, not_before=intent["at_epoch"]):
            raise native.NativeDeliveryError("SUCCESSOR_NATIVE_PROOF_CHANGED")
    result.update(journal=str(journal), attempt=str(journal / "attempt.json"),
                  confirmation_scope="reference_notice" if attempt.get("body_reference") else "native_user_message",
                  body_read_confirmed=False, business_accepted=False, reconciled_read_only=True,
                  input_operations=0, marker=marker)
    if attempt.get("body_reference"):
        result["body_reference"] = attempt["body_reference"]
    return attempt_details(result, journal / "attempt.json", _attempt_evidence(journal, attempt),
                           reconcile_only=True)


def _publish_observation(bridge, journal, artifact, text, marker, result):
    """Publish once after observation, including its complete RPC/window layers."""
    attempt, intent, fence = _original(journal, artifact, text, marker)
    require_bound(bridge, artifact, attempt["native_binding"], attempt["wire"])
    if result.get("confirmed") is True and (intent is None or not native.validate_proof(
            _legacy_binding(attempt["native_binding"]), attempt["wire"], result.get("native_proof"),
            paste_fence=fence, not_before=intent["at_epoch"])):
        raise native.NativeDeliveryError("SUCCESSOR_NATIVE_PROOF_CHANGED")
    observation = journal / ("observation-" + str(time.time_ns()) + ".json")
    _exclusive_json(observation, result)
    result["observation"] = str(observation)
    receipt = journal / "receipt.json"
    if result.get("confirmed") is True:
        if receipt.exists():
            saved = json.loads(read_bytes(receipt))
            if (saved.get("binding") != attempt["native_binding"]
                    or saved.get("marker") != marker
                    or saved.get("confirmation_scope") != result["confirmation_scope"]
                    or not native.validate_proof(_legacy_binding(attempt["native_binding"]),
                        attempt["wire"], saved.get("native_proof"),
                        paste_fence=fence, not_before=intent["at_epoch"])):
                raise native.NativeDeliveryError("SUCCESSOR_SAVED_RECEIPT_CHANGED")
        else:
            _exclusive_json(receipt, result)
        result["receipt"] = str(receipt)
    return result


def _secondary_error(original, operation, secondary):
    """Diagnostics are best effort after failure; they cannot replace its cause."""
    try:
        note = f'{operation} failed: {type(secondary).__name__}: {secondary}'
        add_note = getattr(original, 'add_note', None)
        if callable(add_note):
            add_note(note)
        else:
            # Keep diagnostics available to the journal on Python 3.10 too.
            original.__notes__ = [*getattr(original, '__notes__', ()), note]
    except BaseException:
        pass


def _observe_bounded(bridge, journal, artifact, text, marker, seconds):
    try:
        return observe_receipt(lambda: _observe(bridge, journal, artifact, text, marker),
                               seconds, reconcile_only=True)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        try:
            attempt, _, _ = _original(journal, artifact, text, marker)
            annotate_attempt_error(exc, journal / "attempt.json",
                                   _attempt_evidence(journal, attempt), reconcile_only=True)
        except BaseException as secondary:
            _secondary_error(exc, 'Original-attempt annotation', secondary)
        raise


def reconcile(bridge, artifact, text, *, marker, state_root=None, wait_seconds=None):
    """Zero terminal input, using only the original durable binding and fence."""
    seconds = wait_budget(wait_seconds, reconcile_only=True)
    successor_rebind.validate_artifact(artifact)
    root, journal = _paths(artifact, marker, state_root)
    with _locked(root, journal, artifact):
        result = _observe_bounded(bridge, journal, artifact, text, marker, seconds)
        return _publish_observation(bridge, journal, artifact, text, marker, result)


def recover(bridge, artifact, text, *, marker, state_root=None, wait_seconds=None):
    """One unused direct extra Enter; retain the frozen attempt and original fence."""
    from cmux_direct_input import recover_once
    seconds = wait_budget(wait_seconds)
    successor_rebind.validate_artifact(artifact)
    root, journal = _paths(artifact, marker, state_root)
    with _locked(root, journal, artifact):
        attempt, _, _ = _original(journal, artifact, text, marker)
        binding, wire = attempt['native_binding'], attempt['wire']
        def recheck():
            _original(journal, artifact, text, marker)
            require_bound(bridge, artifact, binding, wire)
        def emit(phase, screen=None):
            recheck()
            fields = dict(recovery='direct_marker')
            if screen is not None:
                fields.update(screen=screen, screen_sha256=_hash(screen.encode()))
            _event(journal, phase, **fields)
        try:
            result = recover_once(wire, marker, original=_attempt_evidence(journal, attempt),
                recheck=recheck, emit=emit,
                key=lambda value: bridge._run_successor('send-key', '--surface',
                    binding['identity']['target_surface_uuid'], '--', value, artifact=artifact,
                    receipt_contract=binding.get('receipt_contract')),
                observe=lambda: _observe(bridge, journal, artifact, text, marker),
                read_screen=lambda: bridge.read_screen_successor(artifact), wait_seconds=seconds)
            result['reconciled_read_only'] = result['input_operations'] == 0
            return _publish_observation(bridge, journal, artifact, text, marker, result)
        except BaseException as exc:
            annotate_attempt_error(exc, journal / 'attempt.json', _attempt_evidence(journal, attempt))
            raise


def submit(bridge, artifact, text, *, marker=None, key=None, state_root=None, wait_seconds=None):
    """Persist one native input (Codex) or paste (Claude); later calls reconcile.

    Old tasks, callbacks and attempts are never read or rewritten. A reference
    notice proves only receipt of that exact notice, not reading/acceptance of
    its body. The caller must supply a stable marker for long messages.
    """
    seconds = wait_budget(wait_seconds)
    successor_rebind.validate_artifact(artifact)
    if not isinstance(text, str) or not text:
        raise native.NativeDeliveryError("NATIVE_EMPTY_PAYLOAD")
    marker = marker or "SUCCESSOR_" + _hash(text.encode())[:32]
    if marker not in text:
        raise ValueError("MESSAGE_REFERENCE_MARKER_REQUIRED")
    if key not in (None, "enter", "tab"):
        raise ValueError("SUCCESSOR_UNSUPPORTED_KEY")
    prompt_reference._ordinary(text)
    root, journal = _paths(artifact, marker, state_root)
    with _locked(root, journal, artifact):
        if (journal / "attempt.json").exists():
            result = _observe_bounded(bridge, journal, artifact, text, marker,
                                      wait_budget(wait_seconds, reconcile_only=True))
            return _publish_observation(bridge, journal, artifact, text, marker, result)
        planned = prompt_reference.plan(text, marker, root / "message-bodies-v1")
        wire, reference = planned["text"], planned["reference"]
        body_pin = prompt_reference.persist(reference, text)
        binding = bind_target(bridge, artifact, wire)
        binding = native.marker_binding(binding, wire, marker)
        attempt = dict(schema="successor-attempt-v2", artifact=artifact,
                       artifact_sha256=_hash(_canonical(artifact)), marker=marker,
                       body=text, body_sha256=_hash(text.encode()), wire=wire,
                       wire_sha256=_hash(wire.encode()), body_reference=reference,
                       body_pin=body_pin, native_binding=binding, created_at_epoch=time.time(),
                       controller_provenance=controller_provenance(__file__))
        _exclusive_json(journal / "attempt.json", attempt)
        operations = 0
        try:
            def recheck():
                require_bound(bridge, artifact, binding, wire)
                if reference is not None and prompt_reference.verify(reference, text) != body_pin:
                    raise native.NativeDeliveryError("SUCCESSOR_ORIGINAL_BODY_CHANGED")
            def emit(phase, screen=None, *, evidence=None):
                nonlocal operations
                recheck()
                fields = native.input_event_fields(_legacy_binding(binding), wire, phase, evidence)
                if phase == "PASTE_INTENT":
                    fields["native_paste_fence"] = native.capture_paste_fence(_legacy_binding(binding))
                if screen is not None:
                    fields.update(screen=screen, screen_sha256=_hash(screen.encode()))
                _event(journal, phase, **fields)
                if phase in ("NATIVE_INPUT_INTENT", "PASTE_INTENT", "ENTER_INTENT", "EXTRA_ENTER_INTENT"):
                    operations += 1
            def terminal():
                from cmux_direct_input import (send_once as direct_send,
                    protected_submit_result, require_prompt_state)
                def prompt_preflight():
                    target = binding['identity']['target_surface_uuid']
                    workspace = binding['identity']['target_workspace_uuid']
                    raw = bridge._run_successor('rpc', 'surface.input_state',
                        json.dumps(dict(surface_id=target, workspace_id=workspace)),
                        artifact=artifact, receipt_contract=binding.get('receipt_contract'))
                    return require_prompt_state(raw, target, workspace,
                        read_claude_screen=(lambda: bridge.read_screen_successor(artifact))
                            if binding['provider'] == 'claude' else None)
                return direct_send(wire, marker, recheck=recheck, emit=emit,
                    preflight=prompt_preflight,
                    submit=lambda value: protected_submit_result(bridge._run_successor(
                        "paste", "--submit", "--surface", binding["identity"]["target_surface_uuid"],
                        "--", value, artifact=artifact, receipt_contract=binding.get("receipt_contract"),
                        json_output=True)),
                    paste=lambda value: bridge._run_successor("rpc", "terminal.paste", json.dumps({
                        "text": value, "submit_key": "none",
                        "workspace_id": binding["identity"]["target_workspace_uuid"],
                        "surface_id": binding["identity"]["target_surface_uuid"]}, ensure_ascii=False), artifact=artifact,
                        receipt_contract=binding.get("receipt_contract")),
                    key=lambda value: bridge._run_successor("send-key", "--surface",
                        binding["identity"]["target_surface_uuid"], "--", value, artifact=artifact,
                        receipt_contract=binding.get("receipt_contract")),
                    observe=lambda: _observe(bridge, journal, artifact, text, marker),
                    read_screen=lambda: bridge.read_screen_successor(artifact), wait_seconds=seconds)
            if binding["provider"] == "codex":
                from cmux_codex_input import send_once
                result = send_once(_legacy_binding(binding), wire, recheck=recheck, emit=emit,
                    observe=lambda: _observe(bridge, journal, artifact, text, marker),
                    wait_seconds=seconds, fallback=terminal)
            else:
                result = terminal()
            result["reconciled_read_only"] = False
            return _publish_observation(bridge, journal, artifact, text, marker, result)
        except BaseException as exc:
            try:
                annotate_attempt_error(exc, journal / "attempt.json", _attempt_evidence(journal, attempt))
            except BaseException as secondary:
                _secondary_error(exc, 'Original-attempt annotation', secondary)
            try:
                _event(journal, "ERROR", error=str(exc), error_details=getattr(exc, 'details', {}),
                       diagnostic_notes=getattr(exc, '__notes__', []),
                       input_operations_attempted=operations)
            except BaseException as secondary:
                _secondary_error(exc, 'ERROR event publication', secondary)
            raise

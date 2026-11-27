"""Read-only binding from a cmux-agent invocation to its original native receipt.

No state is created here: not intents, locks, scan cursors, or receipts. The
helper's output and screen are never sources of authority.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
from uuid import UUID

from cmux_message_journal import verified_receipt
from cmux_evidence_io import snapshot as evidence_snapshot
import cmux_prompt_reference as prompt_reference

SCHEMA = "cmux-agent-bridge-adapter-v1"
IDENTITY_KEYS = ("workspace_uuid", "caller_surface_uuid", "target_surface_uuid", "target_pane_uuid")


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _uuid(value):
    if not isinstance(value, str) or not value:
        raise ValueError("helper identity is missing")
    return str(UUID(value))


def _identity(value):
    return {key: _uuid(value.get(key)) for key in IDENTITY_KEYS}


def _live(bridge, surface, caller, workspace):
    identity = _identity(bridge.pin_workspace(surface))
    if (identity["caller_surface_uuid"] != _uuid(caller)
            or identity["workspace_uuid"] != _uuid(workspace)):
        raise ValueError("helper caller/workspace differs from native hook caller")
    return identity


def _snapshot(path):
    raw, identity = evidence_snapshot(path)
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
            or (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != identity):
        raise ValueError("helper evidence is not an original regular file")
    return raw, {"path": str(path), "device": st.st_dev, "inode": st.st_ino,
                 "bytes": st.st_size, "identity": list(identity),
                 "sha256": hashlib.sha256(raw).hexdigest()}


def _pin(path):
    return _snapshot(path)[1]


def _read(path, pins):
    raw, pin = _snapshot(path)
    value = json.loads(raw)
    if not isinstance(value, dict) or _pin(path) != pin:
        raise ValueError("helper evidence changed while reading")
    pins.append(pin)
    return value


def _unchanged(pins):
    for pin in pins:
        path = Path(pin["path"])
        if pin.get("absent"):
            if path.exists() or path.is_symlink():
                raise ValueError("helper pending appeared during native verification")
        elif _pin(path) != pin:
            raise ValueError("helper evidence changed during native verification")


def _request(identity, mode, message, request_id):
    # First locate the original marker; the saved intent chooses its format.
    return prompt_reference.helper_request(identity, mode, message, request_id, reference=False)


def _channel(root, identity):
    key = _digest(_encoded([identity["workspace_uuid"], identity["target_surface_uuid"]]))
    return root / "targets" / key


def _intent(root, identity, request, pins, explicit=None):
    channel = _channel(root, identity)
    expected_path = channel / "intents" / (request["marker"] + ".json")
    if explicit is not None and Path(explicit) != expected_path:
        raise ValueError("reconcile intent is not the original caller/target request")
    saved = _read(expected_path, pins)
    expected = prompt_reference.helper_request(identity, request['mode'], request['message'],
        request['request_id'], home=root.parents[3], reference='body_reference' in saved)
    if saved != expected:
        raise ValueError("original helper intent does not match invocation")
    request = saved
    body = request.get('body_reference')
    if body:
        verified = prompt_reference.verify(body, prompt_reference.original_helper_payload(request))
        pin = _pin(Path(body['path']))
        if pin['identity'] != verified['identity'] or pin['sha256'] != verified['sha256']:
            raise ValueError('helper body changed during verification')
        pins.append(pin)
    pending = channel / "pending.json"
    if pending.exists() or pending.is_symlink():
        pointer = _read(pending, pins)
        if pointer != dict(marker=request["marker"], identity=identity):
            raise ValueError("helper has a different pending request; do not replace it")
    else:
        pins.append({"path": str(pending), "absent": True})
    return {"kind": "helper_text", "surface": identity["target_surface_uuid"].upper(),
            "text": request["payload"], "marker": request["marker"],
            "helper_identity": identity, "helper_pins": list(pins),
            "confirmation_scope": "reference_notice" if body else "inline_payload",
            "body_read_confirmed": False if body else None,
            "body_reference": body,
            "helper_intent": str(expected_path)}


def resolve(call, bridge, caller, workspace, home=None):
    """Resolve only the literal invocation, never any old receipt on a surface."""
    root = (Path.home() if home is None else Path(home)) / ".local/state/multi-agent-collaboration/cmux-agent-adapter-v1"
    operation = call.get("operation")
    pins, calls = [], []
    if operation == "broadcast":
        caller_pin = dict(workspace_uuid=_uuid(workspace), caller_surface_uuid=_uuid(caller))
        key = _digest(_encoded([caller_pin, call.get("text"), call.get("request_id", "")]))
        specification = _read(root / "broadcasts" / (key + ".json"), pins)
        identities = specification.get("identities")
        expected = dict(schema=SCHEMA, caller=caller_pin, message=call.get("text"),
                        request_id=call.get("request_id", ""), identities=identities)
        if specification != expected or not isinstance(identities, list) or not identities:
            raise ValueError("original broadcast specification is missing or changed")
        targets = []
        for identity in identities:
            if not isinstance(identity, dict) or identity != _identity(identity):
                raise ValueError("original broadcast identity is invalid")
            if any(identity[key] != value for key, value in caller_pin.items()):
                raise ValueError("broadcast belongs to another caller/workspace")
            surface = identity["target_surface_uuid"]
            if _live(bridge, surface, caller, workspace) != identity:
                raise ValueError("original broadcast target changed")
            targets.append(surface)
        if targets != sorted(set(targets)):
            raise ValueError("broadcast targets are reordered or duplicated")
        for identity in identities:
            request = _request(identity, "broadcast", call.get("text"), call.get("request_id", ""))
            calls.append(_intent(root, identity, request, pins))
    else:
        surface = call.get("surface")
        if not surface:
            raise ValueError("literal original helper target is required")
        identity = _live(bridge, surface, caller, workspace)
        if operation == "reconcile":
            explicit = call.get("intent")
            if not isinstance(explicit, str) or not Path(explicit).is_absolute():
                raise ValueError("reconcile requires --intent with the original absolute request path")
            path = Path(explicit)
            if path.parent != _channel(root, identity) / "intents" or not re.fullmatch(r"CMUX_HELPER_[0-9a-f]{64}\.json", path.name):
                raise ValueError("reconcile intent belongs to a different channel")
            saved = _read(path, pins)
            request = _request(identity, saved.get("mode"), saved.get("message"), saved.get("request_id"))
            calls.append(_intent(root, identity, request, pins, explicit))
        elif operation in ("ask", "send"):
            request = _request(identity, operation, call.get("text"), call.get("request_id", ""))
            calls.append(_intent(root, identity, request, pins))
        else:
            raise ValueError("unsupported helper operation")
    _unchanged(pins)
    # Every member carries all pins so a group change invalidates all success.
    for result in calls:
        result["helper_pins"] = list(pins)
        result["helper_group_identities"] = [item["helper_identity"] for item in calls]
    return calls


def verify(call, bridge):
    """Revalidate exact native user reception and the unchanged original intent."""
    pins = call["helper_pins"]
    identity = call["helper_identity"]
    identities = call.get("helper_group_identities", [identity])
    def unchanged_targets():
        for expected in identities:
            if _identity(bridge.pin_workspace(expected["target_surface_uuid"])) != expected:
                raise ValueError("original helper group target changed during verification")
    _unchanged(pins)
    unchanged_targets()
    proof = verified_receipt(bridge, call["surface"], call["text"], call["marker"])
    _unchanged(pins)
    unchanged_targets()
    if not proof or proof.get("source") != "revalidated_message_dispatch_v1" or _identity(proof.get("identity", {})) != identity:
        return None
    return dict(source="revalidated_helper_original_intent", intent=call["helper_intent"],
                confirmation_scope=call.get('confirmation_scope', 'inline_payload'),
                body_read_confirmed=call.get('body_read_confirmed'),
                intent_pins=pins, native_message_proof=proof)

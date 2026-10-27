"""Explicitly authorized supervisor communication bindings.

This module is deliberately separate from the ordinary same-workspace guard.
It validates an append-only, user-authorized transition artifact and a live
caller/receiver pair.  It never changes an old task pack, callback, nonce or
native-delivery binding.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path
from cmux_evidence_io import read_bytes


SCHEMA = "successor-rebind-v1"
MODE = "successor_cross_workspace_v1"
SCHEMA_V2 = "successor-rebind-v2"
MODE_V2 = "user_authorized_maintenance_v2"
CODEX_SAME_PANE = "codex_native_same_pane_v1"
PAIR_FIELDS = ("successor_surface_uuid", "successor_workspace_uuid", "successor_pane_uuid",
               "target_executor_uuid", "target_workspace_uuid", "target_pane_uuid")


class SuccessorRebindError(RuntimeError):
    pass


def _deny(reason: str):
    raise SuccessorRebindError("SUCCESSOR_REBIND_DENIED: " + reason)


def _uuid(value, field):
    try:
        return str(uuid.UUID(str(value))).upper()
    except (ValueError, TypeError, AttributeError):
        _deny(f"invalid {field}")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path) -> str:
    p = Path(path)
    if not p.is_absolute() or not p.is_file() or p.is_symlink():
        _deny(f"evidence path is not a regular absolute file: {p}")
    return sha256_bytes(read_bytes(p))


def _read_evidence(ref, label):
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
        _deny(f"{label} reference missing")
    path = Path(ref["path"])
    raw = read_bytes(path)
    actual = sha256_bytes(raw)
    if actual != ref.get("sha256"):
        _deny(f"{label} hash changed")
    try:
        value = json.loads(raw)
    except (OSError, ValueError) as exc:
        _deny(f"{label} is not JSON evidence: {type(exc).__name__}")
    if not isinstance(value, dict):
        _deny(f"{label} must be a JSON object")
    return value, actual


def _finite_epoch(value, field):
    if type(value) not in (int, float) or not math.isfinite(value):
        _deny(f"invalid {field}")
    return float(value)


def validate_artifact(artifact, *, now=None, consume=False):
    """Validate authorization, supervisor failure, freeze and immutable pins."""
    if isinstance(artifact, dict) and artifact.get("schema") == SCHEMA_V2:
        return _validate_v2(artifact, now=now, consume=consume)
    if not isinstance(artifact, dict) or artifact.get("schema") != SCHEMA:
        _deny("schema")
    if artifact.get("binding_mode") != MODE:
        _deny("binding mode")
    if artifact.get("authorization_source") != "user_message":
        _deny("explicit user authorization required")
    if artifact.get("writes_frozen") is not True:
        _deny("writes_frozen must be true")
    if artifact.get("consumed") is True or artifact.get("status") in {"CONSUMED", "REVOKED"}:
        _deny("authorization already consumed or revoked")
    now = time.time() if now is None else now
    expires = _finite_epoch(artifact.get("expires_at"), "expires_at")
    if expires <= now:
        _deny("authorization expired")

    for field in ("old_supervisor_uuid", "successor_surface_uuid", "target_executor_uuid",
                  "old_workspace_uuid", "successor_workspace_uuid", "target_workspace_uuid",
                  "successor_pane_uuid", "target_pane_uuid"):
        _uuid(artifact.get(field), field)
    if artifact["old_workspace_uuid"] == artifact["successor_workspace_uuid"]:
        _deny("successor workspace must be explicit and may differ")
    for field in ("task_id", "episode_id", "old_task_pack_sha256", "old_attempt_sha256",
                  "successor_of_binding_sha256", "authorization_record_sha256"):
        if not isinstance(artifact.get(field), str) or not artifact[field]:
            _deny(f"missing {field}")

    failure, failure_sha = _read_evidence(artifact.get("supervisor_failure_receipt"), "supervisor failure")
    if failure.get("status") != "SUPERVISOR_FAILED":
        _deny("supervisor failure is not independently proven")
    if failure.get("identity_unresolved") is True:
        _deny("identity unresolved is not supervisor failure")
    if failure.get("old_supervisor_uuid") != artifact["old_supervisor_uuid"]:
        _deny("failure supervisor identity mismatch")

    freeze, freeze_sha = _read_evidence(artifact.get("freeze_evidence"), "freeze")
    if (freeze.get("executor_frozen") is not True
            or freeze.get("sentinel_stopped") is not True
            or freeze.get("active_writers") != []):
        _deny("old attempt is not frozen")
    if freeze.get("old_attempt_sha256") != artifact["old_attempt_sha256"]:
        _deny("freeze attempt hash mismatch")

    auth, auth_sha = _read_evidence(artifact.get("authorization_record"), "authorization")
    if artifact.get("authorization_record_sha256") != auth_sha:
        _deny("authorization record hash mismatch")
    if auth.get("authorization_source") != "user_message":
        _deny("authorization record is not a user message")
    if _uuid(auth.get("successor_surface_uuid"), "authorization.successor_surface_uuid") != _uuid(artifact["successor_surface_uuid"], "successor_surface_uuid"):
        _deny("authorization successor mismatch")
    if _uuid(auth.get("target_executor_uuid"), "authorization.target_executor_uuid") != _uuid(artifact["target_executor_uuid"], "target_executor_uuid"):
        _deny("authorization target mismatch")

    result = dict(artifact)
    result.update(supervisor_failure_sha256=failure_sha, freeze_evidence_sha256=freeze_sha,
                  authorization_record_sha256=auth_sha)
    if consume:
        result["consumed"] = True
        result["status"] = "CONSUMED"
        result["consumed_at"] = now
    return result


def _validate_v2(artifact, *, now=None, consume=False):
    """Communication authority is independent of old tasks and failed peers.

    The exact user message is pinned in the authorization record. This route
    allows a maintenance handshake only; it neither replays old work nor gives
    any shared-write authority. No particular resume command or old supervisor
    is part of this authorization.
    """
    if artifact.get("binding_mode") != MODE_V2:
        _deny("binding mode")
    if artifact.get("authorization_source") != "user_message":
        _deny("explicit user authorization required")
    if (artifact.get("scope") != "maintenance_handshake"
            or artifact.get("communication_only") is not True
            or artifact.get("allow_task_replay") is not False
            or artifact.get("allow_shared_writes") is not False):
        _deny("maintenance communication scope required")
    if artifact.get("consumed") is True or artifact.get("status") in {"CONSUMED", "REVOKED"}:
        _deny("authorization already consumed or revoked")
    now = time.time() if now is None else now
    if "expires_at" in artifact and _finite_epoch(artifact["expires_at"], "expires_at") <= now:
        _deny("authorization expired")
    for field in PAIR_FIELDS:
        _uuid(artifact.get(field), field)
    auth, auth_sha = _read_evidence(artifact.get("authorization_record"), "authorization")
    if artifact.get("authorization_record_sha256") != auth_sha:
        _deny("authorization record hash mismatch")
    if (auth.get("authorization_source") != "user_message"
            or auth.get("scope") != "maintenance_handshake"
            or auth.get("communication_only") is not True):
        _deny("authorization record must authorize maintenance communication")
    message = auth.get("user_message")
    if not isinstance(message, str) or not message.strip():
        _deny("exact user authorization message required")
    if auth.get("user_message_sha256") != sha256_bytes(message.encode("utf-8")):
        _deny("user authorization message hash changed")
    for field in PAIR_FIELDS:
        if _uuid(auth.get(field), "authorization." + field) != _uuid(artifact[field], field):
            _deny("authorization pair mismatch: " + field)
    transport = artifact.get("transport")
    if transport is not None or auth.get("transport") is not None:
        if transport != CODEX_SAME_PANE or auth.get("transport") != transport:
            _deny("unsupported or unpinned transport")
        for field in ("caller_thread_id", "target_thread_id"):
            if _uuid(auth.get(field), "authorization." + field) != _uuid(artifact.get(field), field):
                _deny("authorization thread mismatch: " + field)
        if _uuid(artifact["caller_thread_id"], "caller_thread_id") == _uuid(artifact["target_thread_id"], "target_thread_id"):
            _deny("native caller and target threads must differ")
    if _uuid(artifact["successor_surface_uuid"], "successor_surface_uuid") == _uuid(artifact["target_executor_uuid"], "target_executor_uuid"):
        _deny("caller and target must be distinct terminals")
    result = dict(artifact, authorization_record_sha256=auth_sha)
    if consume:
        result.update(consumed=True, status="CONSUMED", consumed_at=now)
    return result


def _rows(tree):
    rows = []
    for window in tree.get("windows", []):
        for workspace in window.get("workspaces", []):
            for pane in workspace.get("panes", []):
                for surface in pane.get("surfaces", []):
                    rows.append({
                        "surface_ref": surface.get("ref"), "surface_uuid": surface.get("id"),
                        "workspace_ref": workspace.get("ref"), "workspace_uuid": workspace.get("id"),
                        "pane_ref": pane.get("ref"), "pane_uuid": pane.get("id"),
                        "surface_type": surface.get("type"), "dock_scope": surface.get("dock_scope"),
                        "tty": surface.get("tty"),
                    })
    return rows


def _find(rows, selector, label):
    matches = [r for r in rows if selector in (r["surface_ref"], r["surface_uuid"])
               or (isinstance(selector, str) and selector.upper() == str(r["surface_uuid"]).upper())]
    if len(matches) != 1:
        _deny(f"{label} missing or ambiguous")
    row = dict(matches[0])
    for key in ("workspace_uuid", "surface_uuid", "pane_uuid"):
        row[key] = _uuid(row.get(key), label + "." + key)
    if row["surface_type"] != "terminal" or row.get("dock_scope") == "global":
        _deny(f"{label} is not a terminal workspace member")
    return row


def resolve_pair(identity, tree, artifact, *, env=None, now=None):
    """Resolve the live caller and target against a validated artifact."""
    validated = validate_artifact(artifact, now=now)
    caller = identity.get("caller") if isinstance(identity, dict) else None
    if not isinstance(caller, dict) or not caller.get("surface_ref"):
        _deny("live caller missing")
    rows = _rows(tree)
    me = _find(rows, caller["surface_ref"], "caller")
    target = _find(rows, artifact["target_executor_uuid"], "target")
    if caller.get("workspace_ref") != me["workspace_ref"] or caller.get("pane_ref") != me["pane_ref"]:
        _deny("caller/tree disagreement")
    if me["surface_uuid"] != _uuid(artifact["successor_surface_uuid"], "successor_surface_uuid"):
        _deny("current caller is not the authorized successor")
    if me["workspace_uuid"] != _uuid(artifact["successor_workspace_uuid"], "successor_workspace_uuid"):
        _deny("successor workspace drift")
    if target["workspace_uuid"] != _uuid(artifact["target_workspace_uuid"], "target_workspace_uuid"):
        _deny("target workspace drift")
    if me["pane_uuid"] != _uuid(artifact["successor_pane_uuid"], "successor_pane_uuid"):
        _deny("successor pane drift")
    if target["pane_uuid"] != _uuid(artifact["target_pane_uuid"], "target_pane_uuid"):
        _deny("target pane drift")
    if artifact["schema"] == SCHEMA and me["workspace_uuid"] == target["workspace_uuid"]:
        _deny("successor route must be explicitly cross-workspace")
    native_same_pane = (artifact["schema"] == SCHEMA_V2
                        and artifact.get("transport") == CODEX_SAME_PANE)
    if native_same_pane and (me["workspace_uuid"] != target["workspace_uuid"]
                             or me["pane_uuid"] != target["pane_uuid"]):
        _deny("native same-pane transport requires the pinned shared pane")
    if me["surface_uuid"] == target["surface_uuid"]:
        _deny("caller and target must be distinct terminals")
    if me["pane_uuid"] == target["pane_uuid"] and (
            me["workspace_uuid"] != target["workspace_uuid"]
            or me["pane_ref"] != target["pane_ref"]):
        _deny("shared pane identity disagreement")
    if (me["workspace_uuid"] == target["workspace_uuid"]
            and me["pane_ref"] == target["pane_ref"]
            and me["pane_uuid"] != target["pane_uuid"]):
        _deny("pane reference/UUID disagreement")
    env = env or {}
    if env.get("CMUX_SURFACE_ID") and _uuid(env["CMUX_SURFACE_ID"], "CMUX_SURFACE_ID") != me["surface_uuid"]:
        _deny("environment/live caller disagreement")
    if env.get("CMUX_WORKSPACE_ID") and _uuid(env["CMUX_WORKSPACE_ID"], "CMUX_WORKSPACE_ID") != me["workspace_uuid"]:
        _deny("environment/live workspace disagreement")
    return {"mode": artifact["binding_mode"], "artifact": validated, "caller": me, "target": target}


def append_record(path, artifact, *, now=None):
    """Create a new append-only successor receipt; never edits the old record."""
    result = validate_artifact(artifact, now=now)
    p = Path(path)
    if not p.is_absolute() or p.exists():
        _deny("successor receipt must be a new absolute path")
    p.parent.mkdir(parents=True, exist_ok=True)
    record = dict(result, status="PENDING", created_at=time.time() if now is None else now)
    raw = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(p, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    return record

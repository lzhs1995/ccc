#!/usr/bin/env python3
"""Availability, authorization and ownership checks shared by all entry points."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

from resource_broker import os_lock

STATES = ("ACTIVE", "EXECUTOR_DEGRADED", "UNAVAILABLE_AUTH", "UNAVAILABLE_BILLING",
          "UNAVAILABLE_QUOTA", "SOLO_TAKEOVER", "HANDOFF_READY")
UNAVAILABLE = set(STATES[1:5])
TRANSITIONS = {"ACTIVE": UNAVAILABLE, "SOLO_TAKEOVER": {"HANDOFF_READY"}, "HANDOFF_READY": {"ACTIVE"},
               **{s: {"SOLO_TAKEOVER", "HANDOFF_READY"} for s in UNAVAILABLE}}


class AvailabilityError(ValueError):
    pass


def state_path(task_id):
    root = Path(os.environ.get("MULTI_AGENT_AVAILABILITY_ROOT", "/tmp/multi-agent-collaboration/executor-availability"))
    root.mkdir(parents=True, exist_ok=True)
    name = task_id if re.fullmatch(r"[A-Za-z0-9_.-]+", task_id) else hashlib.sha256(task_id.encode()).hexdigest()
    return root / (name + ".json")


def load(path):
    return json.loads(Path(path).read_text()) if Path(path).exists() else {}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".availability-", delete=False) as f:
        json.dump(value, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
        temporary = f.name
    os.replace(temporary, path)


def evidence(value):
    if not isinstance(value, dict) or not value.get("path") or not value.get("sha256"):
        raise AvailabilityError("EVIDENCE_REFERENCE_REQUIRED")
    p = Path(value["path"])
    if not p.is_absolute() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != value["sha256"]:
        raise AvailabilityError("EVIDENCE_MISSING_OR_CHANGED")
    return p


def initial(task_id, workspace_uuid, executor_uuid, executor_surface, *, ttl_seconds=86400,
            authorization_source="none", authorization_record=None,
            fallback_policy="manual", protected_paths=()):
    if not task_id or not executor_surface:
        raise AvailabilityError("TASK_AND_EXECUTOR_REQUIRED")
    uuid.UUID(workspace_uuid)
    uuid.UUID(executor_uuid)
    if not 1 <= ttl_seconds <= 7 * 86400:
        raise AvailabilityError("INVALID_TTL")
    if fallback_policy == "automatic_user_authorized":
        if authorization_source != "user_message":
            raise AvailabilityError("USER_AUTHORIZATION_REQUIRED")
        evidence(authorization_record)
    now = time.time()
    return {"contract_version": 2, "task_id": task_id, "workspace_uuid": workspace_uuid,
            "executor_uuid": executor_uuid, "executor_surface": executor_surface, "state": "ACTIVE",
            "ttl_seconds": ttl_seconds, "updated_at": now, "expires_at": now + ttl_seconds,
            "authorization_source": authorization_source, "authorization_record": authorization_record,
            "fallback_policy": fallback_policy, "protected_paths": list(protected_paths),
            "history": [], "revision": 0, "review_provenance": "dual_pending_review"}


def classify_failure(payload, attempts=0, now=None, last_attempt_at=None):
    """Transport uncertainty and supervisor mistakes never become billing errors."""
    now = time.time() if now is None else now
    text = str(payload.get("message", "")).lower()
    transport = payload.get("submission_state")
    if transport in {"DELIVERY_UNVERIFIED_BY_DETECTOR", "DELIVERY_QUEUED_AT_RECEIVER",
                     "SUPERVISOR_DID_NOT_SUBMIT", "SUBMISSION_ABORTED_BUSY", "COMPOSE_OCCUPIED"}:
        return {"category": transport, "takeover": False, "retry": False}
    if payload.get("budget_below_phase_minimum"):
        return {"category": "SUPERVISOR_BUDGET_TOO_SHORT", "takeover": False, "retry": False}
    code = payload.get("http_status")
    # Some providers prepend "/login" even when their actual error is a balance
    # failure. Classify the explicit cause before the generic 401/403 fallback.
    if ("billing" in text or "payment" in text or "no-account" in text
            or re.search(r"\binsufficient(?:\s+account)?\s+balance\b", text)):
        return {"category": "UNAVAILABLE_BILLING", "takeover": True, "retry": False}
    if code in (401, 403):
        return {"category": "UNAVAILABLE_AUTH", "takeover": True, "retry": False}
    if code == 429 or "quota" in text or "usage limit" in text:
        return {"category": "UNAVAILABLE_QUOTA", "takeover": True, "retry": False}
    if code in (502, 503, 504, 524) and payload.get("retryable") is True:
        exhausted = attempts >= 3
        eligible = last_attempt_at is None or now - last_attempt_at >= 60
        return {"category": "EXECUTOR_DEGRADED", "takeover": exhausted,
                "retry": not exhausted and eligible, "retry_after_seconds": max(0, 60 - (now - last_attempt_at)) if last_attempt_at is not None else 0}
    if payload.get("kind") in {"COMPACT_FAILED", "SURFACE_LOST", "REPEATED_NO_PROGRESS"}:
        return {"category": "EXECUTOR_DEGRADED", "takeover": True, "retry": False}
    return {"category": "UNCLASSIFIED", "takeover": False, "retry": False}


def transition(current, target, *, executor_surface="", reason="", failure_receipt=None,
               takeover_manifest=None, handoff="", handshake=None, identity=None,
               recovery_confirmed=False, now=None):
    now = time.time() if now is None else now
    if current.get("contract_version") != 2:
        raise AvailabilityError("INITIALIZE_V2_STATE_REQUIRED")
    if current["expires_at"] <= now:
        raise AvailabilityError("STATE_EXPIRED_REOBSERVE_IDENTITY")
    source = current["state"]
    if target not in TRANSITIONS.get(source, set()):
        raise AvailabilityError(f"INVALID_TRANSITION: {source}->{target}")
    if executor_surface and executor_surface != current["executor_surface"]:
        raise AvailabilityError("EXECUTOR_CONTINUITY_VIOLATION")
    result = dict(current)
    if target in UNAVAILABLE:
        evidence(failure_receipt)
        result["failure_receipt"] = failure_receipt
    if target == "SOLO_TAKEOVER":
        if current.get("fallback_policy") != "automatic_user_authorized" or current.get("authorization_source") != "user_message":
            raise AvailabilityError("USER_AUTHORIZATION_REQUIRED")
        evidence(current.get("authorization_record"))
        evidence(current.get("failure_receipt"))
        manifest = json.loads(evidence(takeover_manifest).read_text())
        if manifest.get("task_id") != current["task_id"] or manifest.get("executor_uuid") != current["executor_uuid"]:
            raise AvailabilityError("TAKEOVER_IDENTITY_MISMATCH")
        for k in ("checkpoint", "freeze_evidence"):
            evidence(manifest.get(k))
        freeze = json.loads(evidence(manifest["freeze_evidence"]).read_text())
        if freeze.get("executor_frozen") is not True or freeze.get("sentinel_stopped") is not True or freeze.get("active_writers") != []:
            raise AvailabilityError("EXECUTOR_NOT_DRAINED")
        if "active_nonces" not in manifest or not manifest.get("resume_phase") or set(manifest.get("protected_paths", [])) != set(current["protected_paths"]):
            raise AvailabilityError("INCOMPLETE_TAKEOVER_MANIFEST")
        result.update(takeover_manifest=takeover_manifest, review_provenance="solo_self_review")
    if target == "HANDOFF_READY":
        h = json.loads(evidence(handoff).read_text())
        if h.get("phase_boundary") is not True or h.get("active_writers") != [] or h.get("task_id") != current["task_id"]:
            raise AvailabilityError("SAFE_HANDOFF_BOUNDARY_REQUIRED")
        if source in UNAVAILABLE and not recovery_confirmed:
            raise AvailabilityError("RECOVERY_NOT_CONFIRMED")
        result["handoff_path"] = handoff
        result["handoff_at"] = now
    if target == "ACTIVE":
        ack = json.loads(evidence(handshake).read_text())
        gate = json.loads(evidence(identity).read_text())
        stamp = ack.get("updated_at", "")
        try:
            ack_time = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError):
            ack_time = 0
        if ack.get("task_id") != current["task_id"] or ack.get("status") != "PASS" or ack.get("executor_ack") is not True or ack_time < current.get("handoff_at", now):
            raise AvailabilityError("FRESH_HANDSHAKE_REQUIRED")
        if gate.get("status") != "PASS" or gate.get("workspace_uuid") != current["workspace_uuid"] or gate.get("executor_surface_uuid") != current["executor_uuid"]:
            raise AvailabilityError("EXECUTOR_CONTINUITY_VIOLATION")
        result["review_provenance"] = "dual_pending_review"
    result.update(state=target, updated_at=now, expires_at=now + current["ttl_seconds"], revision=current["revision"] + 1)
    result["history"] = current["history"] + [{"from": source, "to": target, "at": now, "reason": reason}]
    result.update(sentinel_allowed=target == "ACTIVE", executor_dispatch_allowed=target == "ACTIVE",
                  replacement_session_allowed=False, model_switch_allowed=False,
                  api_retry_allowed=False)
    return result


def require_action(task_id, action, pack=None):
    pack = pack or {}
    if not pack:
        registry = Path(os.environ.get("MULTI_AGENT_REGISTRY_ROOT", "/tmp/multi-agent-collaboration/_registry")) / (task_id + ".json")
        entry = load(registry)
        if entry.get("artifact_root"):
            root = Path(entry["artifact_root"])
            pack = load(root / "task-pack.json")
            if not pack and (root / "executor-availability.json").exists():
                pack = {"availability_state": str(root / "executor-availability.json"), "availability_required": True}
    path = Path(pack.get("availability_state") or state_path(task_id))
    current = load(path)
    if not current:
        if pack.get("availability_required"):
            raise AvailabilityError("AVAILABILITY_STATE_MISSING")
        return {"status": "LEGACY_UNMANAGED"}
    if current.get("task_id") != task_id or current.get("contract_version") != 2:
        raise AvailabilityError("AVAILABILITY_IDENTITY_MISMATCH")
    if current.get("expires_at", 0) <= time.time():
        raise AvailabilityError("AVAILABILITY_EXPIRED")
    if current.get("state") != "ACTIVE":
        raise AvailabilityError(f"{action.upper()}_BLOCKED_BY_{current.get('state')}")
    if pack.get("executor_uuid") and pack["executor_uuid"] != current.get("executor_uuid"):
        raise AvailabilityError("EXECUTOR_CONTINUITY_VIOLATION")
    return current


def reobserve(current, identity, *, now=None):
    """Refresh a stale observation without changing availability or authorization."""
    now = time.time() if now is None else now
    gate = json.loads(evidence(identity).read_text())
    if (current.get("contract_version") != 2 or gate.get("status") != "PASS"
            or gate.get("task_id") != current.get("task_id")
            or gate.get("workspace_uuid") != current.get("workspace_uuid")
            or gate.get("executor_surface_uuid") != current.get("executor_uuid")):
        raise AvailabilityError("EXECUTOR_CONTINUITY_VIOLATION")
    observed = gate.get("observed_at")
    if not isinstance(observed, (int, float)) or not 0 <= now - observed <= 300:
        raise AvailabilityError("FRESH_IDENTITY_OBSERVATION_REQUIRED")
    result = dict(current)
    result.update(updated_at=now, expires_at=now + current["ttl_seconds"],
                  revision=current["revision"] + 1, identity_observation=identity,
                  executor_surface=gate.get("executor_surface", current["executor_surface"]))
    result["history"] = current["history"] + [{"event": "REOBSERVE", "at": now, "state": current["state"]}]
    return result


def migrate(legacy_reference, task_id, **identity_and_policy):
    original_path = evidence(legacy_reference)
    old = json.loads(original_path.read_text())
    if old.get("contract_version") == 2 or old.get("task_id", task_id) != task_id:
        raise AvailabilityError("NOT_A_MATCHING_V1_STATE")
    result = initial(task_id, **identity_and_policy)
    if old.get("executor_surface") != result["executor_surface"]:
        raise AvailabilityError("EXECUTOR_CONTINUITY_VIOLATION")
    backup = original_path.with_name(original_path.name + ".v1-" + legacy_reference["sha256"][:12])
    if not backup.exists():
        backup.write_bytes(original_path.read_bytes())
    result.update(state="EXECUTOR_DEGRADED", sentinel_allowed=False, executor_dispatch_allowed=False,
                  failure_receipt={"path": str(backup), "sha256": legacy_reference["sha256"]},
                  migration={"previous_state": old.get("state"), "requires_fresh_boundary_and_handshake": True})
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["show", "init", "transition", "check", "classify", "migrate", "reobserve"])
    p.add_argument("--task-id", required=True)
    p.add_argument("--state")
    p.add_argument("--input", help="JSON arguments, references or failure payload")
    p.add_argument("--to", choices=STATES)
    p.add_argument("--action", default="dispatch")
    args = p.parse_args()
    path = Path(args.state) if args.state else state_path(args.task_id)
    value = json.loads(Path(args.input).read_text()) if args.input else {}
    try:
        if args.command == "classify":
            result = classify_failure(**value)
        elif args.command == "check":
            result = require_action(args.task_id, args.action, {"availability_state": str(path), "availability_required": True})
        elif args.command == "show":
            result = load(path)
        else:
            with os_lock(str(path) + ".lock"):
                if args.command == "migrate":
                    result = migrate(task_id=args.task_id, **value)
                elif args.command == "reobserve":
                    result = reobserve(load(path), **value)
                elif args.command == "init":
                    if path.exists():
                        raise AvailabilityError("STATE_ALREADY_EXISTS")
                    result = initial(args.task_id, **value)
                else:
                    result = transition(load(path), args.to, **value)
                save(path, result)
        print(json.dumps(result, indent=2))
        return 0
    except (AvailabilityError, OSError, ValueError) as e:
        print(json.dumps({"status": "BLOCKED", "reason": str(e)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

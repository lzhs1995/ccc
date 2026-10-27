#!/usr/bin/env python3
"""
cmux_consensus_stop_guard.py — armed-task Stop guard.

This is the only hook type that catches the failure mode where an agent simply
*declares* multi-agent collaboration ("we did 3 rounds and agreed") at the end
of a turn without ever running the harness. PreToolUse guards cannot see this:
they only fire when a tool is actually invoked.

Design (armed-only, zero-friction by default):
  - If NO armed-task marker exists for this workspace → pass through. So casual
    mentions of codex/claude in ordinary chat are never blocked.
  - A task is armed by `mac_harness.py identity-gate` (writes
    /tmp/multi-agent-collaboration/_active/<workspace_id>.json).
  - When armed AND the turn's final assistant message asserts collaboration or
    consensus, the guard requires PASS evidence:
      * <artifact_root>/validation.json  status == PASS
      * <artifact_root>/consensus-validation.json status == PASS
        (which itself requires >=3 rounds with proven executor nonce evidence)
    Missing/!PASS → block turn-end (exit 2) with remediation steps.
  - Stale markers self-expire via ttl_seconds so a forgotten marker can't wedge
    a session permanently.

Exit codes: 0 = allow turn-end, 2 = block turn-end (Claude must act/retract).
"""
from __future__ import annotations

import fcntl
import json
import hashlib
import math
import os
import cmux_hook_identity as hook_identity
import cmux_hook_scope as hook_scope
import re
import stat
import sys
from executor_closeout import (
    terminal_report, closeout_instructions, superseded,
)
from cmux_evidence_io import read_bytes, snapshot, attempt_paths
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ACTIVE_DIR = Path("/tmp/multi-agent-collaboration/_active")

# Assertions that imply another agent participated / consensus was reached.
# Kept deliberately specific to reduce false positives; matched case-insensitively.
# Evidence-shaped claim patterns.
#
# These deliberately do NOT match bare topic nouns. A bare noun cannot
# distinguish an assertion from its denial, from a bug report about this guard,
# from the guard's own source filenames, from a supervisor-assigned delivery
# marker, from the assigned output filename, or from a count of prior blocks --
# all six were measured blocking honest turn-ends, and the sixth blocked a
# message reporting how often this guard had fired. A lexical test standing in
# for a semantic property is simultaneously too strict and too permissive:
# vagueness passed while precision was blocked, which inverts the intent.
#
# What blocks now is a *positive claim about evidence state* that the artifacts
# on disk contradict. See _contradicts_disk: the verdict comes from the files
# this guard exists to protect, not from vocabulary.
EVIDENCE_CLAIM_PATTERNS = [
    r"consensus[-\s]?check\s+(?:is\s+)?PASS",
    r"consensus[-\s]?validation(?:\.json)?\s+(?:is\s+)?PASS",
    r"validation(?:\.json)?\s+(?:is\s+)?PASS",
    r"(?:rounds?|轮)\s*(?:recorded|completed|记录)\s*[:=]?\s*(\d+)",
    r"(\d+)\s*(?:of|/)\s*(\d+)\s+rounds?\s+(?:recorded|completed|are\s+recorded)",
    r"all\s+(?:required\s+)?rounds?\s+(?:are\s+)?(?:recorded|complete)",
    r"strict\s+validation\s+PASS",
    r"共识\s*(?:已)?(?:达成|通过)",
    r"达成(?:了)?(?:共识|一致)",
]
_EVIDENCE_CLAIM_RE = re.compile("|".join(EVIDENCE_CLAIM_PATTERNS), re.IGNORECASE)

# A negation/retraction governing the matched span clears the claim. Denials,
# retractions, bug reports and pending-state notes must never block.
NEGATION_MARKERS = [
    r"\bno\b", r"\bnot\b", r"\bnever\b", r"\bzero\b", r"\babsent\b",
    r"\bmissing\b", r"\bpending\b", r"\bunresolved\b", r"\bfail(?:ed|s)?\b",
    r"\bblocked\b", r"\bretract(?:ed|ing|ion)?\b", r"\bwithdraw(?:n|ing)?\b",
    r"\bcannot\b", r"\bwithout\b", r"\bawaiting\b", r"\bwould\b", r"\bif\b",
    r"未", r"没有", r"尚未", r"缺少", r"不是", r"撤回",
]
_NEGATION_RE = re.compile("|".join(NEGATION_MARKERS), re.IGNORECASE)

# Characters of context scanned around a matched claim for a governing negation.
NEGATION_WINDOW_CHARS = 160


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(read_bytes(path))
    except Exception:
        return None


def _workspace_key(payload: dict[str, Any]) -> str:
    return hook_identity.identity(payload)[0]


def _surface_key(payload: dict[str, Any]) -> str | None:
    return hook_identity.identity(payload)[1]


def _marker_fresh(marker: dict[str, Any]) -> bool:
    """Honour TTL: an expired marker is treated as disarmed."""
    armed_at = marker.get("armed_at")
    ttl = marker.get("ttl_seconds")
    if armed_at and ttl:
        try:
            age = (_now() - datetime.fromisoformat(armed_at)).total_seconds()
            if age > float(ttl):
                return False
        except Exception:
            pass
    return True


def _has_active_markers() -> bool:
    """Check jurisdiction before discovering a caller, across all workspaces.

    A managed caller can inherit a different workspace, so inherited env is
    not sufficient for this precheck. Use the same v1/v2 and TTL rules as the
    resolved-workspace scan; hidden v2 staging files are not armed markers.
    """
    for pattern in ("*.json", "*/*.json"):
        for path in ACTIVE_DIR.glob(pattern):
            if path.name.startswith("."):
                continue
            marker = _read_json(path)
            if isinstance(marker, dict) and _marker_fresh(marker):
                return True
    return False


def _scope_candidates():
    """Read marker evidence without claiming jurisdiction over its caller."""
    markers = []
    for pattern in ("*.json", "*/*.json"):
        for path in ACTIVE_DIR.glob(pattern):
            if path.name.startswith('.'):
                continue
            marker = _read_json(path)
            if isinstance(marker, dict) and _marker_fresh(marker):
                workspace = path.parent.name if path.parent != ACTIVE_DIR else path.stem
                markers.append(dict(marker, _scope_workspace=workspace))
    return markers


def _active_markers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every fresh marker for this workspace: v2 directory files, then v1 file.

    The v2 contract stores one file per collaboration under
    _active/<workspace>/<collaboration_id>.json; the v1 single file stays
    read-compatible.  Each fresh marker is judged independently, because with
    concurrent collaborations a final message can assert evidence that any of
    the armed artifact trees refutes.
    """
    selected = hook_scope.current()
    if selected is not None:
        return selected
    ws = _workspace_key(payload)
    surface = _surface_key(payload)
    markers: list[dict[str, Any]] = []
    d = ACTIVE_DIR / ws
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            if p.name.startswith("."):
                continue
            m = _read_json(p)
            if isinstance(m, dict) and _marker_fresh(m) and hook_scope.applies(m, payload, ws, surface):
                markers.append(m)
    legacy = _read_json(ACTIVE_DIR / f"{ws}.json")
    if isinstance(legacy, dict) and _marker_fresh(legacy) and hook_scope.applies(legacy, payload, ws, surface):
        markers.append(legacy)
    return markers


def _final_message(payload: dict[str, Any]) -> str:
    """Best-effort extraction of the turn's final assistant text from a Stop payload."""
    for key in ("last_assistant_message", "final_message", "message", "text", "assistant_message"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v
    # Claude Code Stop payloads may carry a transcript path instead of inline text.
    transcript = payload.get("transcript_path") or payload.get("transcript")
    if isinstance(transcript, str) and Path(transcript).exists():
        try:
            # 长寿会话可能已有数 GB；只读有界尾部，丢弃跨截断点的半行。
            with Path(transcript).open('rb') as stream:
                stream.seek(0, os.SEEK_END)
                start = max(0, stream.tell() - 1024 * 1024)
                stream.seek(start)
                tail = stream.read(1024 * 1024)
            if start:
                tail = tail.partition(b'\n')[2]
            lines = tail.decode('utf-8', 'replace').splitlines()
            texts = []
            for line in lines[-50:]:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get('type') == 'response_item' and isinstance(obj.get('payload'), dict):
                    obj = obj['payload']
                if obj.get("role") == "assistant" or obj.get("type") == "assistant":
                    message = obj.get("message")
                    message = message if isinstance(message, dict) else obj
                    content = message.get("content") or message.get("text") or ""
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    if isinstance(content, str):
                        texts.append(content)
            if texts:
                return texts[-1]
        except Exception:
            pass
    return ""


def _negated(text: str, start: int, end: int) -> bool:
    """True when a negation/retraction governs the span [start, end)."""
    lo = max(0, start - NEGATION_WINDOW_CHARS)
    hi = min(len(text), end + NEGATION_WINDOW_CHARS)
    return bool(_NEGATION_RE.search(text[lo:hi]))


def _positive_evidence_claims(text: str) -> list[str]:
    """Evidence-shaped claims in `text` that are not governed by a negation."""
    out: list[str] = []
    for m in _EVIDENCE_CLAIM_RE.finditer(text or ""):
        if not _negated(text, m.start(), m.end()):
            out.append(m.group(0))
    return out


def _claims_callback_delivery(text, evidence):
    """核验明确的送达声明；普通说明、否认与固定模板无关。"""
    callback = re.compile(r'^\s*DONE\|' + re.escape(evidence['task_id'])
                          + r'\|' + re.escape(evidence['completion_nonce'])
                          + r'(?:\||\s|$)', re.MULTILINE)
    if callback.search(text or ''):
        return True
    positive = re.compile(
        r'\bCALLBACK_CONFIRMED\b|\bNATIVE_RECEIVED\b|'
        r'\bcallback[\s_-]+(?:is[\s_-]+)?(?:confirmed|delivered|received)\b|'
        r'回调(?:已|已经)?(?:送达|确认成功|收到|发送成功)', re.IGNORECASE)
    for match in positive.finditer(text or ''):
        # 未验收等其他句子的否定词不能替当前的送达声明背书。
        before = re.split(r'[\n.!?;。！？；]', text[:match.start()])[-1]
        after = re.split(r'[\n.!?;。！？；]', text[match.end():])[0]
        if not _NEGATION_RE.search(before[-80:] + match.group(0) + after[:80]):
            return True
    return False


def _protocol_callback_evidence(
    marker: dict[str, Any], payload: dict[str, Any], final: str,
) -> bool:
    """Permit only fresh, receiver-bound control ACKs before task completion."""
    match = re.fullmatch(
        r"ROUND_ACK\|([A-Za-z0-9_-]+)\|([A-Za-z0-9_-]+)\|"
        r"([A-Za-z0-9_-]+):identity\|"
        r"(PASS|PASS_WITH_CHANGES|PASS_WITH_P2|CONDITIONAL_PASS|FAIL)\|([A-Za-z0-9_-]+)",
        final.strip(),
    )
    handshake = re.fullmatch(
        r"PREFLIGHT_ACK\|([A-Za-z0-9_-]+)\|([A-Za-z0-9_-]+):identity"
        r"\|READY\|INLINE\|([A-Za-z0-9_-]+)", final.strip(),
    )
    if not match and not handshake:
        return False
    if match:
        task, round_id, provider, verdict, nonce = match.groups()
    else:
        task, provider, nonce = handshake.groups()
    root = Path(str(marker.get("artifact_root") or ""))
    if task != marker.get("task_id") or not root.is_absolute():
        return False
    surface_uuid = _surface_key(payload)
    peers = [row for row in marker.get("participants", []) if isinstance(row, dict)
             and row.get("surface_uuid") == surface_uuid
             and str(row.get("role", "")).startswith("executor")
             and row.get("provider") == provider]
    if len(peers) != 1 or not peers[0].get("surface_ref"):
        return False
    if match:
        receipt = _read_json(root / "round-receipts" / f"{round_id}-{nonce}.json")
        if not isinstance(receipt, dict) or any((
            str(receipt.get("round_id")) != round_id,
            receipt.get("round_nonce") != nonce,
            verdict not in (receipt.get("allowed_verdicts") or []),
            receipt.get("status") not in {"AWAITING_EXECUTOR_ACK", "PASS"},
        )):
            return False
    else:
        doc = _read_json(root / "handshake-receipt.json")
        if not isinstance(doc, dict):
            return False
        entries = doc.get("executors", [doc])
        if not isinstance(entries, list):
            return False
        receipts = [entry for entry in entries if isinstance(entry, dict)
                    and entry.get("ack_nonce") == nonce
                    and entry.get("executor") == peers[0]["surface_ref"]]
        if len(receipts) != 1:
            return False
        receipt = receipts[0]
        if (receipt.get("lifecycle") not in {"PENDING", "ACKED"}
                or receipt.get("status") not in {"HELLO_SENT", "PASS"}
                or receipt.get("ack_line_expected") != final.strip()):
            return False
    if (receipt.get("task_id") != task or receipt.get("executor_provider") != provider
            or receipt.get("executor") != peers[0]["surface_ref"]
            or not receipt.get("dispatch_submitted_at")):
        return False
    try:
        created = datetime.fromisoformat(receipt["created_at"])
        age = (_now() - created).total_seconds()
        budget = receipt["budget_seconds"]
        # Handshake receipts carry the harness's configured observation budget.
        # Its 600-second default is not an upper limit: rejecting a 900-second
        # receipt here blocks even an immediate, otherwise valid ACK.
        if (type(budget) not in (int, float) or not math.isfinite(budget)
                or budget <= 0 or not 0 <= age <= budget):
            return False
        if handshake:
            return True
        if budget > 600:
            return False  # Preserve the separate review-round freshness limit.
        review = receipt["requested_review"]
        artifact = Path(review["artifact"])
        return (
            artifact.is_absolute()
            and artifact.resolve().is_relative_to(root.resolve())
            and hashlib.sha256(artifact.read_bytes()).hexdigest() == review["artifact_sha256"]
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _asserts_collaboration(text: str) -> bool:
    """Retained name: does the text make an unnegated evidence-shaped claim?

    This is only the first of three conditions. A true result is NOT sufficient
    to block; the claim must also contradict on-disk state. See evaluate().
    """
    return bool(_positive_evidence_claims(text))


def _contradicts_disk(marker: dict[str, Any], claims: list[str]) -> tuple[bool, str]:
    """Compare positive claims against the artifacts this guard protects.

    The load-bearing inversion: the guard reads `rounds.json` and the validation
    artifacts and decides from facts, instead of guessing from vocabulary. A
    message claiming more recorded rounds than exist contradicts disk; a message
    denying them agrees with it.
    """
    raw_root = marker.get("artifact_root")
    root = Path(raw_root) if isinstance(raw_root, str) and raw_root else None
    if root is None or not root.is_absolute():
        return True, "armed task has no absolute artifact_root; refusing to trust cwd-relative evidence"
    task_id = marker.get("task_id", "")

    validation = _read_json(root / "validation.json") or {}
    consensus = _read_json(root / "consensus-validation.json") or {}
    rounds_doc = _read_json(root / "rounds.json") or {}
    completed = [
        r for r in rounds_doc.get("rounds", [])
        if r.get("round_id") and r.get("speaker") and r.get("verdict")
    ]

    v_pass = validation.get("status") == "PASS" and validation.get("task_id") == task_id
    c_pass = consensus.get("status") == "PASS" and consensus.get("task_id") == task_id

    for claim in claims:
        low = claim.lower()
        if "consensus" in low and not c_pass:
            return True, (
                f"final message asserts {claim!r} but consensus-validation.json is "
                f"missing/not PASS for task {task_id} at {root}"
            )
        if "validation" in low and "consensus" not in low and not v_pass:
            return True, (
                f"final message asserts {claim!r} but validation.json is "
                f"missing/not PASS for task {task_id} at {root}"
            )
        nums = [int(n) for n in re.findall(r"\d+", claim)]
        if nums and max(nums) > len(completed):
            return True, (
                f"final message asserts {claim!r} but only {len(completed)} "
                f"completed round(s) are recorded in {root / 'rounds.json'}"
            )
        if "达成" in claim or "共识" in claim:
            if not c_pass:
                return True, (
                    f"final message asserts {claim!r} but consensus-validation.json "
                    f"is missing/not PASS at {root}"
                )
    return False, "positive claims are consistent with on-disk evidence"


def _evidence_ok(marker: dict[str, Any]) -> tuple[bool, str]:
    raw_root = marker.get("artifact_root")
    root = Path(raw_root) if isinstance(raw_root, str) and raw_root else None
    if root is None or not root.is_absolute():
        return False, "armed task has no absolute artifact_root; refusing cwd-relative evidence"
    task_id = marker.get("task_id", "")
    validation = _read_json(root / "validation.json")
    if not validation or validation.get("status") != "PASS" or validation.get("task_id") != task_id:
        return False, f"validation.json missing/not PASS for task {task_id} at {root}"
    consensus = _read_json(root / "consensus-validation.json")
    if not consensus or consensus.get("status") != "PASS" or consensus.get("task_id") != task_id:
        return False, f"consensus-validation.json missing/not PASS (run consensus-check) at {root}"
    return True, "consensus + validation evidence present"


def _current_participant_is_executor(marker: dict[str, Any], payload: dict[str, Any]) -> bool:
    surface_uuid = _surface_key(payload)
    if not surface_uuid:
        return False
    return any(
        isinstance(row, dict)
        and str(row.get("role", "")).startswith("executor")
        and row.get("surface_uuid") == surface_uuid
        for row in marker.get("participants", [])
    )


def _lifecycle_snapshot(path: Path) -> tuple[bytes, tuple[int, ...]]:
    """Pin a regular original file; symlinks and concurrent changes are unknown."""
    return snapshot(path)


def _task_dispatch_state(marker: dict[str, Any], pack_raw: bytes, executor: str) -> str:
    """Read-only lifecycle of the supervisor's task prompt for this exact pack.

    A finalized pack is not a delivered task. Only one state proves the task
    never reached this executor: the original dispatch journal exists, every
    attempt is NO_INPUT for this pack/executor, no receipt exists, and the
    sender holds no delivery lock. Missing, legacy, malformed, in-flight,
    pasted, queued or confirmed evidence is never treated as "not started".
    """
    try:
        supervisors = [p for p in marker.get("participants", [])
                       if isinstance(p, dict) and p.get("role") == "supervisor"]
        if len(supervisors) != 1 or not supervisors[0].get("surface_uuid"):
            return "UNKNOWN"
        task_id = marker.get("task_id")
        pack = json.loads(pack_raw)
        workspace = str(marker.get("workspace_uuid") or "").upper()
        if (not workspace or pack.get("task_id") != task_id
                or str(pack.get("executor_uuid", "")).upper() != executor.upper()):
            return "UNKNOWN"
        key = hashlib.sha256(json.dumps(
            [supervisors[0]["surface_uuid"], task_id]).encode()).hexdigest()
        journal = (Path.home() / ".local/state/multi-agent-collaboration"
                   / "task-dispatch-v1" / key)
        if journal.is_symlink() or not journal.is_dir():
            return "UNKNOWN"
        if os.path.lexists(journal / "receipt.json"):
            return "CONFIRMED_OR_RECONCILED"
        attempts = attempt_paths(journal)
        if not attempts:
            return "UNKNOWN"
        lock_path = journal / "delivery.lock"
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return "IN_FLIGHT"
            held = os.fstat(lock.fileno())
            lock_identity = {"device": held.st_dev, "inode": held.st_ino}
            def original_lock():
                now = lock_path.lstat()
                return (stat.S_ISREG(now.st_mode) and stat.S_ISREG(held.st_mode)
                        and (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino))
            if not original_lock():
                return "UNKNOWN"
            pack_sha = hashlib.sha256(pack_raw).hexdigest()
            pins = {}
            for path in attempts:
                pins[path] = _lifecycle_snapshot(path)
                attempt = json.loads(pins[path][0])
                binding = attempt["binding"]
                identity = binding["identity"]
                if (binding.get("task_id") != task_id
                        or binding.get("task_pack_sha256") != pack_sha
                        or str(identity.get("target_surface_uuid", "")).upper()
                        != executor.upper()
                        or str(identity.get("workspace_uuid", "")).upper() != workspace
                        or str(identity.get("caller_surface_uuid", "")).upper()
                        != str(supervisors[0]["surface_uuid"]).upper()
                        or not identity.get("target_pane_uuid")):
                    return "UNKNOWN"
                if attempt.get("phase") != "NO_INPUT" or attempt.get("events") != []:
                    return "SUBMITTED_OR_UNKNOWN"
                # Older writers did not pin the lock inode. They cannot prove
                # the unlocked path is the original sender's lock.
                pin = attempt.get("delivery_lock_identity")
                if (pin != lock_identity or not isinstance(pin, dict)
                        or any(type(pin.get(k)) is not int for k in lock_identity)):
                    return "UNKNOWN"
            if (not original_lock() or os.path.lexists(journal / "receipt.json")
                    or attempt_paths(journal) != attempts
                    or any(_lifecycle_snapshot(p) != v for p, v in pins.items())):
                return "UNKNOWN"
        return "NO_INPUT"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "UNKNOWN"


def _completion_callback_evidence(
    marker: dict[str, Any], payload: dict[str, Any]
) -> tuple[bool, str]:
    """A finalized executor pack may not end without a confirmed callback."""
    if not _current_participant_is_executor(marker, payload):
        return True, "current participant is not this task's executor"
    raw_root = marker.get("artifact_root")
    root = Path(raw_root) if isinstance(raw_root, str) and raw_root else None
    if root is None or not root.is_absolute():
        return False, "executor task has no absolute artifact_root"
    pack = _read_json(root / "task-pack.json")
    if not isinstance(pack, dict) or pack.get("draft") is not False:
        return True, "no finalized executor task pack is active"

    receipt_value = pack.get("completion_receipt")
    if not isinstance(receipt_value, str):
        return False, "finalized task pack has no completion_receipt"
    receipt_path = Path(receipt_value)
    if not receipt_path.is_absolute() or receipt_path.parent.resolve() != root.resolve():
        return False, "completion_receipt is not bound directly under artifact_root"
    receipt = _read_json(receipt_path)
    if not isinstance(receipt, dict):
        # An executor-side callback attempt means work was reported; never
        # release it through the dispatch side. Otherwise only a proven
        # NO_INPUT dispatch shows the finalized task never reached us.
        attempts = receipt_path.with_name(receipt_path.stem + "-attempts")
        report_value = pack.get("report")
        report = Path(report_value) if isinstance(report_value, str) else None
        if report is None or not report.is_absolute() or report.parent.resolve() != root.resolve():
            return False, "completion report is not bound directly under artifact_root"
        pack_raw = read_bytes(root / "task-pack.json")
        state = "CALLBACK_ATTEMPTED" if os.path.lexists(attempts) else \
            "REPORT_WITHOUT_CALLBACK" if os.path.lexists(report) else \
            _task_dispatch_state(marker, pack_raw, _surface_key(payload) or "")
        if state == "NO_INPUT":
            if (os.path.lexists(report) or os.path.lexists(attempts)
                    or os.path.lexists(receipt_path)
                    or read_bytes(root / "task-pack.json") != pack_raw):
                return False, "task lifecycle changed during NO_INPUT observation"
            return True, ("finalized task pack was never delivered (dispatch NO_INPUT); "
                          "no completion callback is owed yet")
        return False, (f"completion callback receipt missing/unreadable at {receipt_path} "
                       f"(task dispatch state: {state})")

    report_value = pack.get("report")
    report = Path(report_value) if isinstance(report_value, str) else None
    if report is None or not report.is_file():
        return False, "completion report is missing"
    digest = hashlib.sha256(read_bytes(report)).hexdigest()
    expected = {
        "task_id": pack.get("task_id"),
        "task_pack_sha256": hashlib.sha256(read_bytes(root / "task-pack.json")).hexdigest(),
        "completion_nonce": pack.get("completion_nonce"),
        "completion_callback": pack.get("completion_callback"),
        "callback_target": pack.get("callback_target"),
        "report": str(report),
        "report_sha256": digest,
        "report_bytes": report.stat().st_size,
        "confirmed": True,
    }
    mismatches = [key for key, value in expected.items() if receipt.get(key) != value]
    if mismatches:
        return False, "completion callback receipt mismatch: " + ", ".join(mismatches)
    import cmux_bridge as bridge
    from cmux_callback_journal import verified_receipt
    if verified_receipt(bridge, pack['callback_target'], root / 'task-pack.json') is None:
        return False, "completion callback receipt lacks revalidated native user-message proof"
    return True, "confirmed completion callback receipt matches original native reception, pack and report"


def _evaluate_resolved(
    payload: dict[str, Any],
) -> tuple[bool, str, dict[str, Any] | None]:
    """Block only an unnegated evidence-shaped claim that on-disk state refutes.

    Three conditions, all required:
      1. a multi-agent task is armed;
      2. the final message makes a positive, evidence-shaped claim that is not
         governed by a negation/retraction;
      3. the artifacts on disk actually contradict that claim.

    Condition 3 is what makes this a check rather than a vocabulary filter. It
    also means denials, retractions, bug reports about this guard, quoted
    filenames, delivery markers, and block counts all pass, because none of them
    asserts a state that disk refutes.
    """
    # End Stop-hook recursion without confirming delivery or disarming tasks.
    if (payload.get("hook_event_name") in ("Stop", "SubagentStop")
            and payload.get("stop_hook_active") is True):
        return True, "Stop hook reentry; task and callback remain unconfirmed", None

    markers = _active_markers(payload)
    if not markers:
        return True, "no armed multi-agent task — pass through", None

    final = _final_message(payload)
    protocol_ack = any(
        _current_participant_is_executor(marker, payload)
        and _protocol_callback_evidence(marker, payload, final)
        for marker in markers
    )
    if protocol_ack:
        return True, "fresh bound protocol ACK; this does not complete the task", None
    workspace, surface = _workspace_key(payload), _surface_key(payload)
    current_markers = []
    for marker in markers:
        terminal = terminal_report(marker, workspace, surface)
        if terminal and superseded(terminal, markers, workspace, surface):
            # The later task owns both callback and evidence-claim checks; the
            # old report/receipt remain unchanged and unconfirmed.
            continue
        current_markers.append(marker)
        if terminal and not _claims_callback_delivery(final, terminal):
            # 报告已冻结且原调用返回：普通说明也能结束。共识声明仍走下方门禁。
            # 不创建 receipt，不要求固定 STATUS 文本，不触发自动重发。
            continue
        callback_ok, callback_msg = _completion_callback_evidence(marker, payload)
        if not callback_ok:
            if terminal:
                callback_msg = ("EXECUTOR_CLOSEOUT: " + callback_msg
                                + "; original attempt returned. End without more "
                                "tools. " + closeout_instructions(terminal))
            return False, callback_msg, marker

    claims = _positive_evidence_claims(final)
    if not claims:
        return True, (
            "armed, but the final message makes no unnegated evidence-shaped claim "
            "beyond any already-verified terminal callback"
        ), None

    # With concurrent collaborations, block when ANY armed artifact tree
    # contradicts the claim; allow only when every armed task's evidence holds.
    last_msg = ""
    for marker in current_markers:
        contradicted, why = _contradicts_disk(marker, claims)
        if contradicted:
            return False, why, marker

        # Claims are consistent with rounds.json; still require the summary
        # artifacts to be genuinely PASS before letting a positive claim stand.
        ok, msg = _evidence_ok(marker)
        if not ok:
            return False, msg, marker
        last_msg = msg
    return True, last_msg, None


def _evaluate_with_marker(payload, stop_output=None):
    # Reentry must terminate even if identity discovery is currently unavailable.
    if (payload.get("hook_event_name") in ("Stop", "SubagentStop")
            and payload.get("stop_hook_active") is True):
        if stop_output is not None:
            stop_output['value'] = _waiting_supervisor_stop_output(payload)
        return True, "Stop hook reentry; task and callback remain unconfirmed", None
    try:
        with hook_scope.evaluation(payload, _scope_candidates()) as markers:
            if not markers:
                return True, "no task enrollment for this session — pass through", None
            result = _evaluate_resolved(payload)
            if result[0] and stop_output is not None:
                stop_output['value'] = _waiting_supervisor_stop_output(payload, resolved=True)
            return result
    except hook_identity.ERRORS as exc:
        return False, "TASK_EVIDENCE_INVALID: " + str(exc), None


def evaluate(payload: dict[str, Any]) -> tuple[bool, str]:
    """Preserve the public verdict API; diagnostics use the same evaluation."""
    ok, message, _marker = _evaluate_with_marker(payload)
    return ok, message


def _block(message: str, marker_hint: str) -> int:
    if message.startswith("TASK_EVIDENCE_INVALID:"):
        sys.stderr.write(f"Task evidence could not be evaluated: {message}\n")
        return 2
    # A returned original attempt: transport recovery advice would contradict
    # the PreToolUse seal and invite a duplicate callback.
    if message.startswith("EXECUTOR_CLOSEOUT:"):
        sys.stderr.write(
            "cmux executor closeout Stop guard blocked turn-end.\n"
            f"{message}\n\n"
            "The report is frozen and the original callback attempt returned. "
            "Do not resend, edit the frozen report, or start a retry loop. "
            "The exact task-bound zero-input reconcile command remains "
            "available; otherwise use the honest handoff and wait for the "
            "supervisor to accept/disarm that task. This block is not "
            "callback confirmation and does not complete the task.\n"
            f"  (armed marker: {marker_hint})\n"
        )
        return 2
    # Callback transport failures are not failed plan-consensus rounds.
    # Keep evaluate() and its evidence requirements unchanged; give the
    # executor the recovery action for the actual failing gate.
    if any(term in message for term in (
        "completion callback", "completion_receipt", "completion report",
        "executor task has no absolute artifact_root",
    )):
        sys.stderr.write(
            "cmux completion callback Stop guard blocked turn-end.\n"
            f"{message}\n\n"
            "Preserve the report and original callback attempt. Inspect its "
            "submission/queue/receipt evidence before any resend. If nothing "
            "was submitted, repair the exact transport failure and use the "
            "task-pack callback entrypoint. If submitted or queued, reconcile "
            "that original attempt; do not send a duplicate or fabricate a "
            "confirmed receipt. Notify the supervisor through the task evidence "
            "directory if transport is unavailable.\n"
            "This callback failure does not require a new handshake or three "
            "plan-consensus rounds. Do not disarm merely to bypass this gate.\n"
            f"  (armed marker: {marker_hint})\n"
        )
        return 2
    sys.stderr.write(
        "cmux multi-agent consensus Stop guard blocked turn-end.\n"
        f"{message}\n\n"
        "Your final message claims multi-agent collaboration/consensus while a "
        "multi-agent task is ARMED, but the evidence does not back it up.\n"
        "Do ONE of:\n"
        "  1) Produce real evidence: run the harness handshake + at least 3 "
        "record-round (executor rounds carry nonce evidence) + consensus-check, "
        "then validate to PASS.\n"
        "  2) Retract the collaboration claim from your message.\n"
        "  3) If the task is genuinely finished/aborted, run: "
        f"python3 {Path(__file__).with_name('mac_harness.py')} disarm\n"
        f"  (armed marker: {marker_hint})\n"
        "For an executor task with a finalized pack, first write the report and "
        "call cmux_bridge.submit_completion_callback(task_pack_path). A local "
        "DONE line without completion-callback-receipt.json cannot end the turn.\n"
    )
    return 2


def _waiting_supervisor_stop_output(payload, resolved=False):
    """仅在现有 Stop 验证已放行后调用；验证报告边界，不改变 Goal 或回执。"""
    if payload.get("hook_event_name") != "Stop":
        return None
    try:
        # main 已在同次身份上下文内时不重新解析，避免两个2.5秒预算串联。
        from contextlib import nullcontext
        context = nullcontext(hook_scope.current()) if resolved else hook_scope.evaluation(payload, _scope_candidates())
        with context as selected:
            if not selected:
                return None
            workspace, surface = _workspace_key(payload), _surface_key(payload)
            markers = _active_markers(payload)
            for marker in markers:
                evidence = terminal_report(marker, workspace, surface)
                if evidence and not superseded(evidence, markers, workspace, surface):
                    # 递归 Stop 同样返回此协议；普通 exit 0 无法覆盖 native Goal 的 block。
                    return {
                        "continue": False,
                        "suppressOutput": True,
                        "stopReason": (
                            "WAITING_SUPERVISOR: report frozen; original callback "
                            "returned. Await supervisor receipt reconciliation and "
                            "acceptance. Delivery and task completion remain unconfirmed."
                        ),
                    }
    except hook_identity.ERRORS:
        # 保留原验证器的身份错误处理；此辅助出口不把未知身份认定为报告已交。
        return None
    return None



def main() -> int:
    # CLI self-test mode
    if "--check-file" in sys.argv:
        idx = sys.argv.index("--check-file")
        payload = _read_json(Path(sys.argv[idx + 1])) or {}
        ok, msg = evaluate(payload)
        print(("ALLOW: " if ok else "BLOCK: ") + msg)
        return 0 if ok else 2

    raw = sys.stdin.read()
    if not raw.strip():
        return 0  # nothing to judge → allow
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0  # non-JSON → don't wedge the session
    if not isinstance(payload, dict):
        return 0
    output = {}
    ok, msg, marker = _evaluate_with_marker(payload, output)
    if ok:
        stop_output = output.get('value')
        if stop_output is not None:
            print(json.dumps(stop_output, ensure_ascii=False))
        return 0
    marker = marker or {}
    return _block(msg, f"task={marker.get('task_id')} root={marker.get('artifact_root')}")


if __name__ == "__main__":
    raise SystemExit(main())

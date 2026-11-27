#!/usr/bin/env python3
"""PreToolUse guard: refuse a write that another owner holds an exclusive lease on.

This is the enforcement half of the lease design. An advisory record that nothing
checks is a comment, so the schema and the harness records only become a real
constraint here.

Scope is deliberately narrow, matching the finalized task pack's enumerated
absolute paths. A path nobody leased is not blocked -- this guard exists to stop
two owners writing the same declared file concurrently, not to gate all file I/O.

Root resolution never uses the caller's cwd: the round guard's cwd-derived path
doubled when run from inside the artifact tree and then blamed a file that
existed and read PASS. An explicit --artifact-root adds to every active-task
root, with the registry as fallback for a marker's missing root.
"""
from __future__ import annotations

import json
import os
import cmux_hook_identity as hook_identity
import cmux_hook_scope as hook_scope
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVE_DIR = Path("/tmp/multi-agent-collaboration/_active")
REGISTRY_DIR = Path("/tmp/multi-agent-collaboration/_registry")

# Tools whose invocation implies mutating a path.
WRITE_TOOLS = {"write", "edit", "notebookedit", "multiedit", "apply_patch", "str_replace"}

# Shell verbs that mutate a file given as an argument.
SHELL_MUTATORS = re.compile(
    r"\b(?:tee|dd|truncate|sed\s+-i|perl\s+-i|install|mv|cp|rm|chmod|chown|"
    r"ln|touch|patch|shred)\b"
)
REDIRECT_RE = re.compile(r"(?:^|\s)(?:>>?|\d>)\s*(?P<path>/[^\s;|&]+)")


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _workspace_key(payload: dict[str, Any]) -> str:
    return hook_identity.identity(payload)[0]


def _has_workspace_markers() -> bool:
    """Lease jurisdiction uses any parseable v1/v2 marker, without Stop TTL.

    Inspect all workspaces before resolving the caller: inherited workspace
    variables can belong to a managed daemon rather than its native client.
    """
    for pattern in ("*.json", "*/*.json"):
        for path in ACTIVE_DIR.glob(pattern):
            if path.name.startswith("."):
                continue
            if isinstance(_read_json(path), dict):
                return True
    return False


def _workspace_markers(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every parseable marker for this workspace: v2 directory, then v1 file.

    The v2 contract (marker_version 2) stores one file per collaboration under
    _active/<workspace>/<collaboration_id>.json; the v1 single file
    _active/<workspace>.json stays read-compatible.  A guard that only read
    the old path would silently stop seeing armed tasks after the writer
    migrated — that is why both shapes are consulted here.
    """
    selected = hook_scope.current()
    if selected is not None:
        return selected
    ws, surface = hook_identity.identity(payload)
    markers: list[dict[str, Any]] = []
    d = ACTIVE_DIR / ws
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            if p.name.startswith("."):
                continue
            m = _read_json(p)
            if isinstance(m, dict) and hook_scope.applies(m, payload, ws, surface):
                markers.append(m)
    legacy = _read_json(ACTIVE_DIR / f"{ws}.json")
    if isinstance(legacy, dict) and hook_scope.applies(legacy, payload, ws, surface):
        markers.append(legacy)
    return markers


def _artifact_roots(
    payload: dict[str, Any], tokens: list[str], *, markers=None,
) -> list[Path]:
    """Every absolute artifact root the guard must enforce leases under.

    An explicit --artifact-root contributes its root without bypassing other
    armed collaborations. Passing markers=[] supports explicit-root checking
    without workspace discovery when no applicable marker exists anywhere.
    """
    roots: list[Path] = []
    if "--artifact-root" in tokens:
        i = tokens.index("--artifact-root")
        if i + 1 < len(tokens):
            candidate = Path(tokens[i + 1])
            if not candidate.is_absolute():
                return []
            roots.append(candidate)
        else:
            return []
    if markers is None:
        markers = _workspace_markers(payload)
    for marker in markers:
        root = marker.get("artifact_root")
        if isinstance(root, str) and root:
            candidate = Path(root)
            if candidate.is_absolute() and candidate not in roots:
                roots.append(candidate)
            continue
        task_id = marker.get("task_id")
        if isinstance(task_id, str) and task_id:
            entry = _read_json(REGISTRY_DIR / f"{task_id.replace(os.sep, '_')}.json") or {}
            reg = entry.get("artifact_root")
            if isinstance(reg, str) and reg:
                candidate = Path(reg)
                if candidate.is_absolute() and candidate not in roots:
                    roots.append(candidate)
    return roots


def _invalid_root_reason(
    payload: dict[str, Any], tokens: list[str], *, markers=None,
) -> str | None:
    """Explain an explicitly supplied relative root instead of failing open."""
    if "--artifact-root" in tokens:
        i = tokens.index("--artifact-root")
        if i + 1 >= len(tokens):
            return "--artifact-root is missing its value"
        value = tokens[i + 1]
        if not Path(value).is_absolute():
            return f"--artifact-root {value!r} is not absolute"
    if markers is None:
        markers = _workspace_markers(payload)
    for marker in markers:
        value = marker.get("artifact_root")
        if isinstance(value, str) and value:
            if not Path(value).is_absolute():
                return f"active task marker artifact_root {value!r} is not absolute"
            continue
        task_id = marker.get("task_id")
        if isinstance(task_id, str) and task_id:
            entry = _read_json(REGISTRY_DIR / f"{task_id.replace(os.sep, '_')}.json") or {}
            value = entry.get("artifact_root")
            if isinstance(value, str) and value and not Path(value).is_absolute():
                return f"registry artifact_root {value!r} is not absolute"
            if not isinstance(value, str) or not value:
                return f"active task {task_id!r} has no registered artifact_root"
    return None


def _tool_name(payload: dict[str, Any]) -> str:
    for key in ("tool_name", "toolName", "tool", "name"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return ""


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("tool_input", "toolInput", "input", "arguments"):
        v = payload.get(key)
        if isinstance(v, dict):
            return v
    return {}


def _candidate_paths(payload: dict[str, Any]) -> list[str]:
    """Absolute paths this invocation would mutate."""
    out: list[str] = []
    tool = _tool_name(payload)
    ti = _tool_input(payload)

    if tool in WRITE_TOOLS:
        for key in ("file_path", "path", "filePath", "notebook_path", "target"):
            v = ti.get(key)
            if isinstance(v, str) and v.startswith("/"):
                out.append(v)

    command = ti.get("command") or payload.get("command") or ""
    if isinstance(command, str) and command.strip():
        for m in REDIRECT_RE.finditer(command):
            out.append(m.group("path"))
        if SHELL_MUTATORS.search(command):
            try:
                toks = shlex.split(command, posix=True)
            except ValueError:
                toks = command.split()
            out.extend(t for t in toks if t.startswith("/"))
    # De-duplicate while preserving order.
    seen, uniq = set(), []
    for p in out:
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            uniq.append(rp)
    return uniq


def _owner_role() -> str:
    """This agent's machine role, if the environment declares one."""
    return (os.environ.get("CMUX_AGENT_ROLE")
            or os.environ.get("MULTI_AGENT_ROLE") or "").strip().lower()


def _lease_is_stale(rec: dict[str, Any], now: datetime | None = None) -> bool:
    """Treat expired or malformed TTLs as stale, never as absent."""
    now = now or datetime.now(timezone.utc)
    raw = rec.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return True
    try:
        expires = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= now


def _evaluate_resolved(payload: dict[str, Any], *, markers=None) -> tuple[bool, str]:
    tokens: list[str] = []
    cmd = _tool_input(payload).get("command") or payload.get("command") or ""
    if isinstance(cmd, str) and cmd:
        try:
            tokens = shlex.split(cmd, posix=True)
        except ValueError:
            tokens = cmd.split()

    if markers is None:
        markers = _workspace_markers(payload)
    invalid = _invalid_root_reason(payload, tokens, markers=markers)
    if invalid:
        return False, (
            "ARTIFACT_ROOT_NOT_ABSOLUTE — refusing to evaluate the lease "
            f"guard with {invalid}; a relative root must never fall back to cwd."
        )
    roots = _artifact_roots(payload, tokens, markers=markers)
    if not roots:
        return True, "no armed task root resolvable — lease guard inactive"

    lease_paths: list[Path] = []
    for root in roots:
        leases_dir = root / "leases"
        if leases_dir.exists():
            lease_paths.extend(sorted(leases_dir.glob("*.json")))
    if not lease_paths:
        return True, "no leases recorded — nothing to enforce"

    targets = _candidate_paths(payload)
    if not targets:
        return True, "no absolute mutation target detected"

    me = _owner_role()
    conflicts = []
    for path in lease_paths:
        rec = _read_json(path)
        if not isinstance(rec, dict) or rec.get("released_at"):
            continue
        holder = (rec.get("owner_role") or "").lower()
        held = {os.path.realpath(p) for p in rec.get("paths") or []}
        overlap = sorted(set(targets) & held)
        if not overlap:
            continue
        # Expiry is checked before owner/mode handling. An expired record must
        # be surfaced as LEASE_STALE even to its former owner and even if it
        # was shared; it is never silently reclaimed or ignored.
        if _lease_is_stale(rec):
            conflicts.append((holder, rec.get("owner_surface"), overlap,
                              rec.get("expires_at"), "LEASE_STALE"))
            continue
        if rec.get("mode") != "exclusive":
            continue
        if me and holder == me:
            continue          # own live lease never blocks its owner
        conflicts.append((holder, rec.get("owner_surface"), overlap,
                          rec.get("expires_at"), "LEASE_CONFLICT"))

    if not conflicts:
        return True, "no exclusive lease covers the target path(s)"

    lines = ["cmux lease guard blocked this write."]
    for holder, surface, overlap, expires, verdict in conflicts:
        if verdict == "LEASE_STALE":
            lines.append(
                f"  LEASE_STALE: {overlap} is covered by an expired or malformed "
                f"lease from {holder or 'another owner'} ({surface}), expires {expires}. "
                "Release it explicitly; it was not reclaimed."
            )
        else:
            lines.append(
                f"  LEASE_CONFLICT: {overlap} is held exclusively by "
                f"{holder or 'another owner'} ({surface}), expires {expires}"
            )
    lines.append(
        "Coordinate with that owner or wait for release. Do not bypass: a "
        "concurrent write to a declared path is the exact failure this exists to "
        "prevent, and the losing writer's work disappears without an error."
    )
    if not me:
        lines.append(
            "  Note: CMUX_AGENT_ROLE is unset, so this guard cannot tell whether "
            "you are the lease owner. Set it to your machine role to avoid "
            "blocking yourself."
        )
    return False, "\n".join(lines)


def evaluate(payload):
    # Only known read tools skip discovery. A shell command with no extracted
    # write target can still carry an invalid explicit artifact root.
    if not payload or _tool_name(payload) in ("read", "glob", "grep"):
        return True, "no absolute mutation target detected"
    try:
        from cmux_consensus_stop_guard import _marker_fresh
        candidates = []
        for pattern in ('*.json', '*/*.json'):
            for path in ACTIVE_DIR.glob(pattern):
                if path.name.startswith('.'):
                    continue
                marker = _read_json(path)
                if isinstance(marker, dict) and _marker_fresh(marker):
                    workspace = path.parent.name if path.parent != ACTIVE_DIR else path.stem
                    candidates.append(dict(marker, _scope_workspace=workspace))
        with hook_scope.evaluation(payload, candidates) as markers:
            return _evaluate_resolved(payload, markers=markers)
    except hook_identity.ERRORS as exc:
        return False, "TASK_EVIDENCE_INVALID: " + str(exc)


def main() -> int:
    if "--check-command" in sys.argv:
        i = sys.argv.index("--check-command")
        cmd = sys.argv[i + 1] if i + 1 < len(sys.argv) else ""
        ok, msg = evaluate({"tool_name": "bash", "tool_input": {"command": cmd}})
        print(("ALLOW: " if ok else "BLOCK: ") + msg)
        return 0 if ok else 2

    raw = sys.stdin.read()
    if not raw.strip():
        print(json.dumps({}))
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print(json.dumps({}))
        return 0
    ok, msg = evaluate(payload)
    if ok:
        print(json.dumps({}))
        return 0
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

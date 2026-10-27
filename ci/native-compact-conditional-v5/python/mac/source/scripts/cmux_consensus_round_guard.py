#!/usr/bin/env python3
"""
Guard consensus evidence consumption.

This hook backs the multi-agent-collaboration rule that important plan
consensus requires at least three completed review rounds unless explicitly
waived by the user. It is intentionally narrow: it blocks attempts to use the
mac_harness consensus-check command when strict handshake validation or
rounds.json is incomplete. The harness command also enforces the same rule; the
hook gives Claude/Codex a pre-tool-use failure before stale evidence is treated
as acceptable.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any


HARNESS_NAMES = {"mac_harness.py"}
CHECKED_COMMANDS = {"consensus-check"}
FINAL_OK_VERDICTS = {
    "PASS",
    "APPROVE",
    "APPROVED",
    "PASS_WITH_P2",
    "APPROVE_WITH_P2",
}


def _flatten_json_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out.extend(_flatten_json_strings(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(_flatten_json_strings(v))
    return out


def _extract_command(payload: dict[str, Any]) -> str:
    candidates = [
        payload.get("command"),
        payload.get("tool_input", {}).get("command") if isinstance(payload.get("tool_input"), dict) else None,
        payload.get("toolInput", {}).get("command") if isinstance(payload.get("toolInput"), dict) else None,
        payload.get("input", {}).get("command") if isinstance(payload.get("input"), dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    for text in _flatten_json_strings(payload):
        if "mac_harness.py" in text:
            return text
    return ""


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _arg_value(tokens: list[str], name: str, default: str = "") -> str:
    if name not in tokens:
        return default
    idx = tokens.index(name)
    if idx + 1 >= len(tokens):
        return default
    return tokens[idx + 1]


def _subcommand(tokens: list[str]) -> str:
    for i, token in enumerate(tokens):
        if Path(token).name in HARNESS_NAMES and i + 1 < len(tokens):
            return tokens[i + 1]
    return ""


REGISTRY_DIR = Path("/tmp/multi-agent-collaboration/_registry")


def _artifact_root(tokens: list[str]) -> Path | None:
    """Resolve the artifact root without ever consulting the caller's cwd.

    Order: explicit --artifact-root, then the task registry. Returns None when
    neither is available, so the caller can report ROOT_NOT_FOUND rather than
    inventing a path.

    Composing this from Path.cwd() was a measured defect: run from inside the
    artifact tree it produced
    <root>/handoff/multi-agent-artifacts/<TASK_ID>/handoff/multi-agent-artifacts/<TASK_ID>/
    -- a doubled path -- and then reported validation.json as missing when that
    file existed and read PASS. The verdict stayed BLOCK either way, so this was
    never a security hole; the harm was a false diagnosis that sent readers after
    a nonexistent problem instead of the real one.
    """
    explicit = _arg_value(tokens, "--artifact-root")
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_absolute() else None

    task_id = _arg_value(tokens, "--task-id", "")
    if not task_id:
        return None
    safe_id = task_id.replace(os.sep, "_")

    entry = _read_json(REGISTRY_DIR / f"{safe_id}.json") or {}
    registered = entry.get("artifact_root")
    if isinstance(registered, str) and registered:
        candidate = Path(registered)
        return candidate if candidate.is_absolute() else None
    return None


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def consensus_is_valid(root: Path, task_id: str) -> tuple[bool, str]:
    validation = _read_json(root / "validation.json") or {}
    if validation.get("task_id") != task_id or validation.get("status") != "PASS":
        return False, f"validation.json is missing/PASS-mismatch at {root / 'validation.json'}"
    strict = validation.get("checks", {}).get("handshake_strict", {})
    if strict.get("pass") is not True:
        return False, "handshake_strict.pass is not true"

    rounds_doc = _read_json(root / "rounds.json") or {}
    rounds = rounds_doc.get("rounds", [])
    completed = [
        r for r in rounds
        if r.get("round_id") and r.get("speaker") and r.get("verdict")
    ]
    minimum = int(rounds_doc.get("minimum_required_rounds") or 3)
    if len(completed) < minimum:
        return False, f"only {len(completed)} completed rounds; require at least {minimum}"
    resolved = {
        round_id
        for entry in completed
        for round_id in (entry.get("resolves_rounds") or [])
        if round_id
    }
    blockers = [
        r for r in completed
        if r.get("blocks_consensus") is True
        and r.get("round_id") not in resolved
    ]
    if blockers:
        return False, f"{len(blockers)} consensus blocker round(s) remain"
    final_verdict = completed[-1].get("verdict", "")
    if final_verdict not in FINAL_OK_VERDICTS:
        return False, f"final verdict {final_verdict!r} is not an approving verdict"
    return True, "strict consensus evidence present"


def validate_command(command: str) -> tuple[bool, str]:
    command = command.strip()
    if not command or "mac_harness.py" not in command:
        return True, "not a mac_harness command"
    tokens = _tokens(command)
    subcommand = _subcommand(tokens)
    if subcommand not in CHECKED_COMMANDS:
        return True, "harness command does not consume consensus evidence"
    task_id = _arg_value(tokens, "--task-id", "multi-agent-task")
    root = _artifact_root(tokens)
    if root is None:
        # Fail closed, but name the actual missing input instead of blaming a
        # file at a path we guessed.
        return False, (
            f"ROOT_NOT_FOUND for task {task_id!r}: pass --artifact-root, or register "
            f"the task at {REGISTRY_DIR / (task_id.replace(os.sep, '_') + '.json')} "
            "with an artifact_root field. The caller's cwd is deliberately not used."
        )
    return consensus_is_valid(root, task_id)


def _hook_response(ok: bool, message: str) -> int:
    if ok:
        print(json.dumps({}))
        return 0
    print(
        "cmux multi-agent consensus round guard blocked this command.\n"
        f"{message}\n"
        "Policy: important multi-agent consensus requires strict handshake validation "
        "and at least three completed review rounds.",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-command")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.check_command is not None:
        ok, message = validate_command(args.check_command)
        if args.json:
            print(json.dumps({"ok": ok, "message": message}, ensure_ascii=False))
        else:
            print(("ALLOW: " if ok else "BLOCK: ") + message)
        return 0 if ok else 2

    raw = sys.stdin.read()
    if not raw.strip():
        return _hook_response(True, "empty hook payload")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _hook_response(True, "non-json hook payload")
    ok, message = validate_command(_extract_command(payload))
    return _hook_response(ok, message)


if __name__ == "__main__":
    raise SystemExit(main())

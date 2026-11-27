#!/usr/bin/env python3
"""
Guard use of multi-agent handshake receipts.

This hook blocks commands that try to use receipt/guard-check/task-pack evidence
from a legacy handshake that did not prove an executor-generated nonce ACK.
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
CHECKED_COMMANDS = {"receipt", "guard-check", "task-pack"}


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


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _artifact_root(tokens: list[str]) -> Path | None:
    """Resolve the artifact root without consulting the caller's cwd.

    Order: explicit --artifact-root, then the task registry. Returns None when
    neither resolves, so the caller reports the real missing input instead of
    blaming a file at a path this function invented.

    Composing from Path.cwd() doubled the path when run from inside the artifact
    tree -- <root>/handoff/multi-agent-artifacts/<TASK_ID>/handoff/... -- and then
    reported validation.json as missing while that file existed and read PASS.
    Same defect as the round guard carried; fixed here for the same reason.
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


def validation_is_strict(root: Path) -> tuple[bool, str]:
    path = root / "validation.json"
    if not path.exists():
        return False, f"validation.json not found at {path}"
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return False, f"validation.json unreadable: {exc}"
    if data.get("status") != "PASS":
        return False, "validation.json is not PASS"
    strict = data.get("checks", {}).get("handshake_strict", {})
    if strict.get("pass") is not True:
        return False, "handshake_strict.pass is not true; rerun handshake and validate"
    return True, "strict handshake validation present"


def validate_command(command: str) -> tuple[bool, str]:
    command = command.strip()
    if not command or "mac_harness.py" not in command:
        return True, "not a mac_harness command"
    tokens = _tokens(command)
    subcommand = _subcommand(tokens)
    if subcommand not in CHECKED_COMMANDS:
        return True, "harness command does not consume final evidence"
    root = _artifact_root(tokens)
    if root is None:
        task_id = _arg_value(tokens, "--task-id", "")
        safe_id = task_id.replace(os.sep, "_") if task_id else "<task-id>"
        return False, (
            f"ROOT_NOT_FOUND for task {task_id!r}: pass --artifact-root, or register "
            f"the task at {REGISTRY_DIR / (safe_id + '.json')} with an artifact_root "
            "field. The caller's cwd is deliberately not used."
        )
    ok, message = validation_is_strict(root)
    if ok:
        return True, message
    return False, (
        "Blocked: multi-agent evidence cannot be used because Claude/Codex did not "
        f"produce a nonce-proven executor ACK. {message}"
    )


def _hook_response(ok: bool, message: str) -> int:
    if ok:
        print(json.dumps({}))
        return 0
    print(
        "cmux multi-agent handshake receipt guard blocked this command.\n"
        f"{message}\n"
        "Policy: a handshake only counts when the executor actually replies with the nonce ACK.",
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

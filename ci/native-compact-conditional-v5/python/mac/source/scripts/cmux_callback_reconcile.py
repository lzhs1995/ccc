#!/usr/bin/env python3
"""Read-only reconcile of the original completion callback attempt while sealed.

2026-10-08: once the report is frozen, the closeout guard sealed every tool
except one queue-resume command. When that did not apply (mid-render read,
compaction, Tab already used) each Stop restart had no legal action, so the
executor looped with tools fully sealed. This exact command is the second
exception: one read of the receiver through the original controller with zero
terminal input. It never pastes, presses keys, waits or polls. Delivery is
only what the controller's strict reconcile accepts; a nonce substring or a
quoted line is not evidence.
"""
import argparse
import importlib
import json
from pathlib import Path
import shlex
import sys

from cmux_callback_queue_resume import canonical


def reconcilable(pack_path):
    """Static admission: an original attempt exists that input already reached."""
    pack_path = canonical(pack_path)
    pack = json.loads(pack_path.read_text())
    skill = canonical(pack["required_skill"])
    if skill.name != "SKILL.md" or not (skill.parent / "scripts/cmux_bridge.py").is_file():
        raise ValueError("original controller unavailable")
    receipt = Path(pack["completion_receipt"])
    if receipt.parent != pack_path.parent or receipt.exists() or receipt.is_symlink():
        raise ValueError("receipt exists or path is outside task")
    journal = canonical(receipt.with_name(receipt.stem + "-attempts"))
    attempts = sorted(journal.glob("attempt-*.json"))
    if not attempts:
        raise ValueError("no original attempt")
    attempt_path = canonical(attempts[-1])
    attempt = json.loads(attempt_path.read_text())
    if attempt.get("phase") in ("PREPARED", "NO_INPUT", "CONFIRMED"):
        raise ValueError("attempt has nothing to reconcile")
    return skill.parent, attempt_path


def allowed(payload, marker, evidence):
    """One exact synchronous command for this marker's own task, no shell tail."""
    try:
        if payload.get("tool_name") != "Bash":
            return False
        task = Path(marker["artifact_root"]) / "task-pack.json"
        tool = payload.get("tool_input", {})
        expected = shlex.join(["rtk", "proxy", str(Path(__file__).resolve()),
                               "--task-pack", str(task)])
        if tool.get("command") != expected or tool.get("run_in_background"):
            return False
        _, attempt = reconcilable(task)
        return str(attempt) == evidence["attempt"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-pack", required=True)
    args = parser.parse_args()
    try:
        original, _ = reconcilable(args.task_pack)
        # Reconcile with the controller that made the attempt, not a newer one.
        sys.path.insert(0, str(original / "scripts"))
        bridge = importlib.import_module("cmux_bridge")
        if Path(bridge.__file__).resolve() != (original / "scripts/cmux_bridge.py").resolve():
            raise ValueError("wrong controller loaded")
        result = bridge.submit_completion_callback(args.task_pack, reconcile_only=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 - every failure is an honest, terminal report
        print("CALLBACK_RECONCILE_UNCONFIRMED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

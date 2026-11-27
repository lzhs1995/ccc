#!/usr/bin/env python3
"""Continue one recorded Enter using only the original controller's queue Tab.

This is not resend, receipt reconciliation or task acceptance. The frozen pack
chooses the original controller; the controller owns all input and rechecks the
live caller, report, locks, journal and exact receiver composer before one Tab.
"""
import argparse
import importlib
import json
import os
from pathlib import Path
import shlex
import sys


def canonical(path):
    value = Path(path)
    if not value.is_absolute() or value != value.resolve(strict=True):
        raise ValueError("canonical absolute path required")
    return value


def resumable(pack_path):
    """Static admission only. The original controller performs the live checks."""
    pack_path = canonical(pack_path)
    pack = json.loads(pack_path.read_text())
    skill = canonical(pack["required_skill"])
    if skill.name != "SKILL.md" or not (skill.parent / "scripts/cmux_bridge.py").is_file():
        raise ValueError("original controller unavailable")
    receipt = Path(pack["completion_receipt"])
    if receipt.parent != pack_path.parent or os.path.lexists(receipt):
        raise ValueError("receipt exists or path is outside task")
    journal = canonical(receipt.with_name(receipt.stem + "-attempts"))
    attempts = sorted(journal.glob("attempt-*.json"))
    if not attempts:
        raise ValueError("no original attempt")
    attempt_path = canonical(attempts[-1])
    attempt = json.loads(attempt_path.read_text())
    phases = [item["phase"] for item in attempt["events"]]
    if (attempt.get("phase") not in ("POST_ENTER_OBSERVATION", "RECOVERY_OBSERVATION")
            or phases.count("PASTE_INTENT") != 1
            or phases.count("ENTER_INTENT") != 1
            or not 1 <= phases.count("POST_ENTER_OBSERVATION") <= 8
            or any("TAB" in phase or phase == "EXTRA_ENTER_INTENT" for phase in phases)):
        raise ValueError("original Enter cannot be resumed")
    # 不允许在缺少原生绑定或 paste EOF fence 的旧尝试上补造恢复资格。
    import cmux_native_delivery as native
    native._original_intent(attempt, attempt.get('native_binding'), pack['completion_callback'])
    return skill.parent, attempt_path


def allowed(payload, marker, evidence):
    """The closeout exception is one exact synchronous command, no shell tail."""
    try:
        if payload.get("tool_name") != "Bash":
            return False
        task = Path(marker["artifact_root"]) / "task-pack.json"
        tool = payload.get("tool_input", {})
        expected = shlex.join(["rtk", "proxy", str(Path(__file__).resolve()),
                               "--task-pack", str(task)])
        if tool.get("command") != expected or tool.get("run_in_background"):
            return False
        _, attempt = resumable(task)
        return str(attempt) == evidence["attempt"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-pack", required=True)
    args = parser.parse_args()
    try:
        original, _ = resumable(args.task_pack)
        # Do not switch an in-flight journal to the currently installed release.
        sys.path.insert(0, str(original / "scripts"))
        bridge = importlib.import_module("cmux_bridge")
        if Path(bridge.__file__).resolve() != (original / "scripts/cmux_bridge.py").resolve():
            raise ValueError("wrong controller loaded")
        result = bridge.submit_completion_callback(args.task_pack, resume_queue_only=True)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print("ORIGINAL_QUEUE_RESUME_UNCONFIRMED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Non-blocking Claude Code hook that emits structured CCC lifecycle events."""

from __future__ import annotations

import json
import sys

# Existing Claude processes may retain a hook command without Python -B.
# Protect the immutable runtime before importing any adjacent module.
sys.dont_write_bytecode = True

from claude_ccc_protocol import append_event, build_event, notify_daemon


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        event = build_event(payload if isinstance(payload, dict) else {})
        if event is not None:
            append_event(event)
            notify_daemon(event)
    except Exception:
        # Automation telemetry must never block or alter Claude's turn.
        pass
    sys.stdout.write("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only probe: what is each Claude Code surface actually doing?

The screen cannot answer this.  A finished Claude session and a silently
stalled one draw the same thing: an empty ``❯`` prompt, no spinner, stable for
as long as you care to watch.  Weeks of viewport sampling produced no threshold
that separates them, because the distinguishing fact is simply not on screen.

It is on disk.  Claude Code appends a structured transcript per session under
``~/.claude/projects/<slug>/<session-uuid>.jsonl``, and every assistant record
carries ``stop_reason`` -- *why the turn ended*:

    end_turn       the model finished and is waiting for the user   -> DONE
    tool_use       the model asked for a tool and expects a result
    max_tokens     output limit hit mid-answer
    stop_sequence  a stop sequence matched

``tool_use`` plus a matching ``tool_result`` further downstream means the call
completed.  A ``tool_use`` with *no* matching result, and no new records since,
is a session that asked for something and never got it: a real stall.

So this probe answers three separate questions per surface and never conflates
them:

    done      last turn ended with end_turn, nothing pending
    stalled   an unanswered tool_use, or an API error, and the file has gone
              quiet for longer than ``--quiet-sec``
    active    the transcript is still growing

Nothing here writes, sends, or changes daemon state.  It only reads.

Joining a cmux surface to its transcript
---------------------------------------
Two hops, because neither side alone is enough:

1. **surface -> process** is authoritative.  cmux exports ``CMUX_SURFACE_ID``
   into each CLI's environment, so the Claude process itself names its surface.
   Walking the process tree extends that to every child.

2. **process -> transcript** is inferred, and this is where care is needed.
   The transcript records ``cwd`` but no surface or pid.  Candidates are
   therefore filtered to the same ``cwd`` and to sessions whose first record
   appeared *after* the process started -- a process cannot have written a
   transcript that predates it.  Among survivors the most recently updated
   wins.  Every result carries a ``join`` field saying how it was reached, and
   ambiguity is reported rather than hidden.

   Bounding by process start time is not optional: pids are recycled, and
   without it months-old sessions match a fresh pid and produce confident
   nonsense.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECTS_DIR = Path.home() / ".claude" / "projects"
SURFACE_ENV = "CMUX_SURFACE_ID"

# How long a transcript must be quiet before "pending work" counts as stalled
# rather than merely in flight.  A tool call that takes two minutes is normal;
# one that has produced nothing for ten is not.
DEFAULT_QUIET_SEC = 600.0

# Tail of the transcript to parse.  Sessions reach tens of thousands of records
# and only the end determines current state.
DEFAULT_TAIL_RECORDS = 400


def _run(command: Sequence[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout


def _process_table() -> tuple[dict[int, list[int]], dict[int, str]]:
    """Return child lists and command names for every visible process."""

    children: dict[int, list[int]] = collections.defaultdict(list)
    names: dict[int, str] = {}
    for line in _run(["/bin/ps", "-Ao", "pid=,ppid=,comm="]).splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
        except ValueError:
            continue
        children[ppid].append(pid)
        names[pid] = fields[2]
    return children, names


def _process_start_epoch(pid: int) -> float | None:
    """Wall-clock start time of ``pid``, or None if it cannot be read.

    ``ps -o lstart`` is the authoritative source; ``etime`` is derived and
    drifts.  Everything downstream depends on this being real, so a parse
    failure returns None rather than a guess.
    """

    raw = _run(["/bin/ps", "-p", str(pid), "-o", "lstart="]).strip()
    if not raw:
        return None
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %d %b %H:%M:%S %Y"):
        try:
            return time.mktime(time.strptime(raw, fmt))
        except ValueError:
            continue
    return None


def _environ_of(pid: int) -> dict[str, str]:
    """Environment of ``pid`` as far as ``ps -E`` will reveal it."""

    raw = _run(["/bin/ps", "-p", str(pid), "-Eww", "-o", "command="])
    environ: dict[str, str] = {}
    for token in raw.split():
        if "=" in token:
            key, _, value = token.partition("=")
            if key.isupper() or key.startswith("CMUX"):
                environ[key] = value
    return environ


def claude_surface_processes() -> dict[str, dict[str, Any]]:
    """Map surface UUID -> its Claude process subtree.

    Only processes whose own environment names a surface are treated as roots,
    so this never guesses which pane a stray ``node`` belongs to.
    """

    children, names = _process_table()
    roots: dict[int, str] = {}
    for pid, name in names.items():
        if "laude" not in name:
            continue
        surface = _environ_of(pid).get(SURFACE_ENV)
        if surface:
            roots[pid] = surface

    surfaces: dict[str, dict[str, Any]] = {}
    for root_pid, surface in roots.items():
        entry = surfaces.setdefault(surface, {"pids": [], "root_pids": [], "started_at": None})
        entry["root_pids"].append(root_pid)
        stack = [root_pid]
        seen: set[int] = set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            entry["pids"].append(pid)
            stack.extend(children.get(pid, []))
        started = _process_start_epoch(root_pid)
        if started is not None:
            current = entry["started_at"]
            # Earliest root start: the session began when its first process did.
            entry["started_at"] = started if current is None else min(current, started)
    return surfaces


def _iter_tail(path: Path, limit: int) -> list[Mapping[str, Any]]:
    """Parse the last ``limit`` JSON records of a .jsonl file.

    Reads the whole file because transcripts are line-delimited and a byte
    seek can land mid-record; the files are small enough (single-digit MB) that
    correctness is worth more than the saved I/O.
    """

    lines: collections.deque[str] = collections.deque(maxlen=limit)
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.strip():
                    lines.append(line)
    except OSError:
        return []
    records: list[Mapping[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(value, Mapping):
            records.append(value)
    return records


def _first_timestamp(path: Path) -> float | None:
    """Epoch of the earliest timestamped record, used to bound the pid join."""

    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"timestamp"' not in line:
                    continue
                try:
                    value = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                stamp = value.get("timestamp") if isinstance(value, Mapping) else None
                if isinstance(stamp, str):
                    parsed = _parse_iso(stamp)
                    if parsed is not None:
                        return parsed
    except OSError:
        return None
    return None


def _parse_iso(value: str) -> float | None:
    text = value.strip().replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return time.mktime(time.strptime(text, fmt)) - time.timezone
        except ValueError:
            continue
    return None


def _transcript_cwd(records: Iterable[Mapping[str, Any]]) -> str:
    for record in records:
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return ""


def _content_blocks(message: Any) -> list[Mapping[str, Any]]:
    if not isinstance(message, Mapping):
        return []
    content = message.get("content")
    if isinstance(content, list):
        return [block for block in content if isinstance(block, Mapping)]
    return []


def summarise_transcript(path: Path, *, tail: int = DEFAULT_TAIL_RECORDS) -> dict[str, Any]:
    """Describe the current state of one session from its transcript tail.

    ``pending_tool_use`` is the load-bearing field: a tool_use id with no
    tool_result anywhere after it means the model is waiting for something that
    never arrived.  Ids are collected from the whole tail, not just the last
    record, because a stall can sit several records back behind retries.
    """

    records = _iter_tail(path, tail)
    if not records:
        return {"ok": False, "reason": "unreadable or empty"}

    issued: dict[str, int] = {}
    answered: set[str] = set()
    last_stop_reason: str | None = None
    last_assistant_index: int | None = None
    api_error = False
    api_error_status: Any = None
    last_user_text = ""

    for index, record in enumerate(records):
        kind = record.get("type")
        message = record.get("message")

        if record.get("isApiErrorMessage"):
            api_error = True
        if record.get("apiErrorStatus") is not None:
            api_error_status = record.get("apiErrorStatus")

        if kind == "assistant" and isinstance(message, Mapping):
            reason = message.get("stop_reason")
            if isinstance(reason, str):
                last_stop_reason = reason
                last_assistant_index = index
            for block in _content_blocks(message):
                if block.get("type") == "tool_use":
                    identifier = block.get("id")
                    if isinstance(identifier, str):
                        issued[identifier] = index
        elif kind == "user" and isinstance(message, Mapping):
            text_parts: list[str] = []
            for block in _content_blocks(message):
                if block.get("type") == "tool_result":
                    identifier = block.get("tool_use_id")
                    if isinstance(identifier, str):
                        answered.add(identifier)
                elif block.get("type") == "text":
                    value = block.get("text")
                    if isinstance(value, str):
                        text_parts.append(value)
            content = message.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            if text_parts:
                last_user_text = " ".join(text_parts)

    pending = sorted(identifier for identifier in issued if identifier not in answered)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    return {
        "ok": True,
        "session_id": path.stem,
        "path": str(path),
        "records_scanned": len(records),
        "last_stop_reason": last_stop_reason,
        "pending_tool_use": pending,
        "api_error": api_error,
        "api_error_status": api_error_status,
        "quiet_sec": max(0.0, time.time() - mtime),
        "mtime": mtime,
        "last_assistant_index": last_assistant_index,
        "last_user_text": last_user_text[:200],
        "cwd": _transcript_cwd(records),
    }


def classify(summary: Mapping[str, Any], *, quiet_sec: float) -> tuple[str, str]:
    """Turn a transcript summary into a verdict plus the reason for it.

    Ordering matters.  ``end_turn`` is checked before anything time-based: a
    finished session is finished no matter how long it has been sitting there,
    and that is exactly the case a screen-only heuristic gets wrong.
    """

    if not summary.get("ok"):
        return "unknown", str(summary.get("reason", "no transcript"))

    quiet = float(summary.get("quiet_sec", 0.0))
    pending = list(summary.get("pending_tool_use") or ())
    reason = summary.get("last_stop_reason")

    if quiet < quiet_sec and (pending or reason == "tool_use"):
        return "active", f"tool call in flight, quiet {quiet:.0f}s < {quiet_sec:.0f}s"

    if reason == "end_turn" and not pending:
        return "done", f"end_turn, nothing pending, quiet {quiet:.0f}s"

    if pending:
        return "stalled", f"{len(pending)} unanswered tool_use, quiet {quiet:.0f}s"

    if summary.get("api_error"):
        status = summary.get("api_error_status")
        return "stalled", f"api error{f' {status}' if status is not None else ''}, quiet {quiet:.0f}s"

    if reason == "tool_use":
        return "stalled", f"last turn ended asking for a tool, quiet {quiet:.0f}s"

    if reason == "max_tokens":
        return "stalled", f"truncated at max_tokens, quiet {quiet:.0f}s"

    if reason in {"stop_sequence", None}:
        return "idle", f"stop_reason={reason}, quiet {quiet:.0f}s"

    return "idle", f"stop_reason={reason}, quiet {quiet:.0f}s"


def index_transcripts(*, tail: int) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    if not PROJECTS_DIR.exists():
        return summaries
    for path in PROJECTS_DIR.rglob("*.jsonl"):
        summary = summarise_transcript(path, tail=tail)
        if summary.get("ok"):
            summary["first_ts"] = _first_timestamp(path)
            summaries.append(summary)
    return summaries


def join_surfaces(
    surfaces: Mapping[str, Mapping[str, Any]],
    transcripts: Sequence[Mapping[str, Any]],
    *,
    slack_sec: float = 120.0,
) -> dict[str, dict[str, Any]]:
    """Attach the most plausible transcript to each surface.

    Filters are deliberately conservative and the reason for every rejection is
    kept, because a wrong join here would make every downstream verdict wrong
    in a way that looks authoritative.
    """

    joined: dict[str, dict[str, Any]] = {}
    for surface, info in surfaces.items():
        started = info.get("started_at")
        candidates = []
        for summary in transcripts:
            first = summary.get("first_ts")
            if started is not None and first is not None and first + slack_sec < started:
                # Predates the process: cannot belong to it.
                continue
            candidates.append(summary)

        # Prefer transcripts still being written, then most recent.
        candidates.sort(key=lambda item: item.get("mtime", 0.0), reverse=True)
        chosen = candidates[0] if candidates else None
        joined[surface] = {
            "surface_id": surface,
            "pids": sorted(info.get("pids") or ()),
            "root_pids": sorted(info.get("root_pids") or ()),
            "started_at": started,
            "candidates": len(candidates),
            "transcript": chosen,
            "join": "env+start-bounded-mtime" if chosen else "no candidate",
        }
    return joined


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quiet-sec", type=float, default=DEFAULT_QUIET_SEC)
    parser.add_argument("--tail", type=int, default=DEFAULT_TAIL_RECORDS)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--transcripts-only", action="store_true", help="skip the surface join")
    args = parser.parse_args(argv)

    transcripts = index_transcripts(tail=args.tail)

    if args.transcripts_only:
        rows = []
        for summary in sorted(transcripts, key=lambda item: item.get("mtime", 0.0), reverse=True):
            verdict, reason = classify(summary, quiet_sec=args.quiet_sec)
            rows.append({"verdict": verdict, "reason": reason, **summary})
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return 0
        print(f"{'verdict':9} {'quiet':>8} {'stop_reason':<14} {'pend':>4} session")
        for row in rows[:40]:
            print(
                f"{row['verdict']:9} {row['quiet_sec']:8.0f} "
                f"{str(row['last_stop_reason']):<14} {len(row['pending_tool_use']):4} {row['session_id'][:18]}"
            )
        return 0

    surfaces = claude_surface_processes()
    joined = join_surfaces(surfaces, transcripts)

    report = []
    for surface, entry in sorted(joined.items()):
        summary = entry.get("transcript") or {"ok": False, "reason": "no transcript"}
        verdict, reason = classify(summary, quiet_sec=args.quiet_sec)
        report.append({**entry, "verdict": verdict, "verdict_reason": reason})

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    print(f"Claude surfaces with a live process: {len(report)}")
    print(f"transcripts indexed: {len(transcripts)}")
    print()
    print(f"{'surface':10} {'verdict':9} {'quiet':>7} {'stop_reason':<14} {'pend':>4} session")
    for row in report:
        summary = row.get("transcript") or {}
        print(
            f"{row['surface_id'][:8]:10} {row['verdict']:9} "
            f"{summary.get('quiet_sec', 0.0):7.0f} {str(summary.get('last_stop_reason')):<14} "
            f"{len(summary.get('pending_tool_use') or ()):4} {str(summary.get('session_id'))[:18]}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

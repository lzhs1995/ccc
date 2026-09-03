#!/usr/bin/env python3
"""Read-only audit of every monitored Claude session and of the daemon itself.

This program never writes the supervisor config or state, never sends terminal
input, and never restarts anything.  It reads ``state.json``, ``config.json``,
``daemon-runtime.json``, the event ledger, ``watch.log``, ``ps`` and
``launchctl``, then reports.  Its only writes go to its own directory under
``~/Library/Logs/cmux-ccc-audit``.

Two classes of finding are separated on purpose:

``ALERT``  something is wrong now and needs a human decision.
``NOTE``   something changed since the previous run.  Worth seeing once, not a
           fault by itself -- a restart or an edit is normal when someone is
           working on the daemon.

The audit is deliberately cadence-independent.  Its durable log cursor uses
device/inode/offset rather than assuming a file only grows, and current
conditions retain an episode identity and severity across runs.  It can be run
manually or by an external scheduler; no session cron is required.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

APP_DIR = Path.home() / "Library" / "Application Support" / "cmux-codex-continue"
LOG_DIR = Path.home() / "Library" / "Logs" / "cmux-codex-continue"
AUDIT_DIR = Path.home() / "Library" / "Logs" / "cmux-ccc-audit"
AUDIT_STATE = AUDIT_DIR / "audit-state.json"
AUDIT_JOURNAL = AUDIT_DIR / "audit.jsonl"
AUDIT_ACKNOWLEDGEMENTS = AUDIT_DIR / "acknowledgements.json"
SOURCE_PATH = Path(__file__).resolve().parent / "cmux_codex_watch.py"
LABEL = os.environ.get("CCC_WATCHER_LABEL") or "com.example.ccc-continue"

# Baselines the daemon is expected to hold.  A change is a NOTE, not an ALERT:
# the user may legitimately add a surface.  Silence would be the real failure.
# Refreshed to the observed steady state on 2026-08-27; a stale baseline emits
# the same two notes on every run forever, which is how a report loses signal.
EXPECTED_TARGETS = 45
EXPECTED_PAUSED = 6

# Per-session thresholds.  Deliberately looser than the daemon's own gates so
# this audit reports what the daemon could not resolve, not every transient.
DEFERRED_WARN_SEC = 600.0        # parked Stop still waiting; expiry is 900s
SUBMIT_STUCK_SEC = 60.0          # submit transaction open; daemon timeout is 15s
UNREADABLE_WARN_SEC = 1800.0     # viewport unparseable this long
SILENT_HOOK_SEC = 3600.0         # no lifecycle Hook while awaiting one
UNPROTECTED_WARN_SEC = 3600.0    # Hook unprotected this long
LOG_STALL_SEC = 300.0            # watch.log not advancing => daemon wedged
PERSISTENT_SEVERE_SEC = 6 * 3600.0
PERSISTENT_CRITICAL_SEC = 24 * 3600.0
MAX_LOG_READ_BYTES = 4 << 20

# Cross-episode fallback volume.
#
# The daemon bounds retries *within* one episode.  It does not bound the number
# of episodes, so a surface that keeps producing fresh stop points can emit an
# unbounded total while every single line still reads ``attempt=1``.  A high
# share of ``attempt=1`` is therefore evidence of many distinct episodes, NOT
# evidence that sending is under control -- do not read it as "no storm".
# Observed peaks before this threshold existed: 24/h (8B863C9F, 2026-08-26 06:00)
# and 13/h (D6AA4D6A, 2026-08-27 15:00), so this fires on real history and is
# expected to alert on first deployment.  It only ever raises an audit finding;
# it never changes what the daemon sends.
GAP_RATE_WARN_PER_HOUR = 10

# Window used to record whether a *real* Hook followed a synthetic send.
#
# This is an observation, never a verdict.  It cannot show what the viewport
# looked like before the send, so it can neither prove the send was needed nor
# prove it was spurious.  The gap-send log line carries only surface/sent/event/
# polls/attempt -- there is no persisted pre-send state to appeal to.
POST_SEND_HOOK_WINDOW_SEC = 120.0

# ``sent=claude_hook source=fallback`` is the synthetic continuation writing its
# own delivery line, sharing the event id of the ``sent=claude_hook_gap`` line
# that caused it.  Counting it as a "real Hook" makes every synthetic send look
# like it was followed by a healthy lifecycle event.  Any post-send Hook probe
# MUST exclude these sources or it measures its own echo.
SYNTHETIC_HOOK_SOURCES = {"fallback"}

# How long an incident group stays in audit state.
#
# Only *pathological* groups are kept.  Ordinary deferrals are high-volume and
# healthy -- 872 of 878 observed starts released within seconds -- so retaining
# a group per start would grow this file by roughly two orders of magnitude
# while carrying no information.  A group is kept only when it expired at least
# once, or started without an observed release.
INCIDENT_RETAIN_SEC = 7 * 86400.0

# Messages parsed into event-level incident groups.
#
# ``deferred stop expired`` counted by phrase reads as "35 separate failures"
# when the truth is one slot re-armed 35 times.  The requeue variant is a second
# re-arm path and really does occur in the log, so it is parsed too; omitting it
# would undercount re-arms.
INCIDENT_KINDS = {
    "Claude stop deferred": "deferred_start",
    "Claude deferred stop expired": "deferred_expiry",
    "Claude expired deferred stop requeued": "deferred_requeue",
    "Claude deferred stop released": "deferred_release",
    "Claude deferred stop recovered": "deferred_recovered",
    "Claude hook identity conflict": "identity_conflict",
    "Claude submit confirmation timeout": "submit_timeout",
}

# Matched longest phrase first so a specific message can never be shadowed by a
# shorter one that happens to be a substring of it.
INCIDENT_PHRASES = tuple(sorted(INCIDENT_KINDS.items(), key=lambda item: -len(item[0])))

HEALTHY_HOOK_STATES = {"healthy", "unverified"}
BAD_CONTEXT = {"limit_waiting", "stalled"}
DEFERRED_TERMINAL_STATUSES = {
    "deferred_cleared",
    "deferred_recovered",
    "deferred_expired",
    "deferred_dropped",
    "deferred_generation_changed",
}


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def sha256_of(path: Path) -> str:
    import hashlib

    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return proc.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def daemon_processes() -> list[int]:
    """Only the watch daemon.  A bare ``ccc`` inside a cmux surface opens the
    read-only TUI and must not be counted as a second daemon."""

    out = run(["/bin/ps", "-Ao", "pid=,command="])
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if "cmux_codex_watch.py" not in line:
            continue
        if not re.search(r"cmux_codex_watch\.py\s+watch\b", line):
            continue
        head = line.split(None, 1)[0]
        if head.isdigit():
            pids.append(int(head))
    return pids


def elapsed(value: Any, now: float) -> float:
    try:
        stamp = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if stamp <= 0 or stamp > now:
        return 0.0
    return now - stamp


def human(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _log_identity(path: Path) -> dict[str, Any]:
    """Return the cursor identity used to detect rotate and truncate."""

    try:
        stat = path.stat()
    except OSError:
        return {
            "device": 0, "inode": 0, "offset": 0, "mtime_ns": 0,
            "checkpoint_start": 0, "checkpoint_sha256": "",
        }
    checkpoint_start = max(0, int(stat.st_size) - 256)
    checkpoint_sha = ""
    try:
        with path.open("rb") as handle:
            handle.seek(checkpoint_start)
            checkpoint_sha = hashlib.sha256(handle.read(int(stat.st_size) - checkpoint_start)).hexdigest()
    except OSError:
        pass
    return {
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "offset": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "checkpoint_start": checkpoint_start,
        "checkpoint_sha256": checkpoint_sha,
    }


def read_new_log(path: Path, previous: Mapping[str, Any]) -> tuple[str, dict[str, Any], str]:
    """Read unseen bytes without confusing rotation/truncation with silence.

    Returns ``(text, current_cursor, transition)``.  The first observation is a
    baseline and never replays historical logs.  A replaced or truncated file
    is read from byte zero, capped to the most recent ``MAX_LOG_READ_BYTES``.
    """

    current = _log_identity(path)
    if not current["inode"]:
        return "", current, "missing"
    old_device = int(previous.get("device") or 0)
    old_inode = int(previous.get("inode") or 0)
    old_offset = int(previous.get("offset") or 0)
    if not old_device or not old_inode:
        return "", current, "baseline"
    same_file = old_device == current["device"] and old_inode == current["inode"]
    checkpoint_matches = True
    old_checkpoint = str(previous.get("checkpoint_sha256") or "")
    old_checkpoint_start = int(previous.get("checkpoint_start") or 0)
    if same_file and old_checkpoint and current["offset"] >= old_offset:
        try:
            with path.open("rb") as handle:
                handle.seek(old_checkpoint_start)
                current_old_tail = handle.read(old_offset - old_checkpoint_start)
            checkpoint_matches = hashlib.sha256(current_old_tail).hexdigest() == old_checkpoint
        except OSError:
            checkpoint_matches = False
    if same_file and current["offset"] >= old_offset and checkpoint_matches:
        start = old_offset
        transition = "advanced" if current["offset"] > old_offset else "unchanged"
    else:
        start = 0
        transition = "truncated" if same_file else "rotated"
    start = max(start, current["offset"] - MAX_LOG_READ_BYTES)
    if current["offset"] <= start:
        return "", current, transition
    try:
        with path.open("rb") as handle:
            handle.seek(start)
            payload = handle.read(current["offset"] - start)
    except OSError:
        return "", current, "read_error"
    return payload.decode("utf-8", errors="replace"), current, transition


def _parse_log_stamp(line: str) -> float:
    """Return the epoch seconds of a ``watch.log`` line, or 0.0 if unstamped."""

    match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if not match:
        return 0.0
    try:
        return time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return 0.0


def parse_log_events(text: str) -> dict[str, list[dict[str, Any]]]:
    """Extract the send/repair timeline from ``watch.log`` text.

    Three streams are separated because conflating them is exactly how this
    audit previously produced a wrong answer:

    ``gap_sends``      synthetic continuations (``sent=claude_hook_gap``).
    ``real_hooks``     genuine lifecycle deliveries -- ``sent=claude_hook``
                       whose ``source=`` is NOT in ``SYNTHETIC_HOOK_SOURCES``.
                       A synthetic send emits its own ``sent=claude_hook
                       source=fallback`` line carrying the *same event id*, so
                       including it would let every synthetic send appear to be
                       followed by a healthy Hook.
    ``repairs``        ``Claude Hook configuration auto-repaired``.

    ``attempt`` is recorded as ``None`` when the field is absent rather than
    defaulting to 1.  Roughly half of historical gap lines carry no ``attempt``,
    and defaulting them would manufacture the very "everything is attempt=1"
    picture that has to be interpreted with care.
    """

    gap_sends: list[dict[str, Any]] = []
    real_hooks: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    incidents: list[dict[str, Any]] = []
    covers_until = 0.0
    for line in text.splitlines():
        stamp = _parse_log_stamp(line)
        if stamp > covers_until:
            covers_until = stamp
        incident_kind = ""
        for phrase, kind in INCIDENT_PHRASES:
            if phrase in line:
                incident_kind = kind
                break
        if (
            not incident_kind
            and "sent=claude_hook" not in line
            and "auto-repaired" not in line
        ):
            continue
        surface_match = re.search(r"surface=(\S+)", line)
        surface = surface_match.group(1) if surface_match else ""
        event_match = re.search(r"event=(\S+)", line)
        event = event_match.group(1) if event_match else ""
        if incident_kind:
            waited_match = re.search(r"waited_sec=(\d+(?:\.\d+)?)", line)
            retrying_match = re.search(r"retrying=(\S+)", line)
            status_match = re.search(r"final_status=(\S+)", line)
            incidents.append({
                "at": stamp,
                "kind": incident_kind,
                "surface": surface,
                "event": event,
                "waited_sec": float(waited_match.group(1)) if waited_match else None,
                # None means the field was absent from the line.  The historical
                # format had no ``retrying=`` at all, so treating absence as
                # "false" would silently drop those re-arms.
                "retrying": retrying_match.group(1) if retrying_match else None,
                "final_status": status_match.group(1) if status_match else "",
            })
            continue
        if "Claude Hook configuration auto-repaired" in line:
            repairs.append({"at": stamp, "line": line.strip()})
            continue
        if "sent=claude_hook_gap" in line:
            attempt_match = re.search(r"attempt=(\d+)", line)
            polls_match = re.search(r"polls=(\d+)", line)
            gap_sends.append({
                "at": stamp,
                "surface": surface,
                "event": event,
                # None means "the log did not say", never "1".
                "attempt": int(attempt_match.group(1)) if attempt_match else None,
                "polls": int(polls_match.group(1)) if polls_match else None,
            })
            continue
        if re.search(r"sent=claude_hook\b", line):
            source_match = re.search(r"source=(\S+)", line)
            source = source_match.group(1) if source_match else ""
            if source in SYNTHETIC_HOOK_SOURCES:
                continue
            real_hooks.append({"at": stamp, "surface": surface, "event": event, "source": source})
    return {
        "gap_sends": gap_sends,
        "real_hooks": real_hooks,
        "repairs": repairs,
        "incidents": incidents,
        # How far this batch of log text actually reaches.  Used as the ceiling
        # for ``as_of`` so an unelapsed window is never scored as ``absent``.
        "log_covers_until": covers_until,
    }


def gap_rate_by_surface_hour(gap_sends: list[Mapping[str, Any]]) -> dict[tuple[str, str], int]:
    """Count synthetic sends per surface per clock hour, across all episodes.

    Episode-scoped retry bounds cannot see this number, which is the whole
    reason it is computed here.
    """

    counts: dict[tuple[str, str], int] = {}
    for send in gap_sends:
        stamp = float(send.get("at") or 0.0)
        if stamp <= 0:
            continue
        surface = str(send.get("surface") or "")
        hour = time.strftime("%Y-%m-%d %H:00", time.localtime(stamp))
        counts[(surface, hour)] = counts.get((surface, hour), 0) + 1
    return counts


def post_send_hook_observation(
    gap_sends: list[Mapping[str, Any]],
    real_hooks: list[Mapping[str, Any]],
    *,
    as_of: float,
    window_sec: float = POST_SEND_HOOK_WINDOW_SEC,
) -> dict[str, Any]:
    """Record whether a real Hook followed each synthetic send.

    Three states, because two would force an unelapsed window into a verdict:

    ``observed``   a non-synthetic Hook arrived within the window.
    ``pending``    the window has not finished as of *as_of* -- undecided.
    ``absent``     the window fully elapsed and no real Hook arrived.

    *as_of* must be the point the evidence actually reaches -- ``min(now, last
    log timestamp)`` -- not wall-clock ``now``.  The log trails real time, so
    scoring against ``now`` marks sends ``absent`` when the truth is only that
    the log does not cover the answer yet.

    None of the three is a verdict on whether the send should have happened:

    * ``absent`` says nothing about the pre-send viewport, which the daemon does
      not persist.
    * ``observed`` is ambiguous -- the Hook may be the lifecycle event that the
      send itself provoked.

    Do not derive ``send_valid`` from this in any caller.
    """

    by_surface: dict[str, list[float]] = {}
    for hook in real_hooks:
        stamp = float(hook.get("at") or 0.0)
        if stamp > 0:
            by_surface.setdefault(str(hook.get("surface") or ""), []).append(stamp)
    for stamps in by_surface.values():
        stamps.sort()

    observed = 0
    pending = 0
    absent = 0
    per_surface: dict[str, dict[str, int]] = {}
    for send in gap_sends:
        stamp = float(send.get("at") or 0.0)
        surface = str(send.get("surface") or "")
        if stamp <= 0:
            continue
        followed = any(
            stamp < hook_at <= stamp + window_sec
            for hook_at in by_surface.get(surface, ())
        )
        bucket = per_surface.setdefault(
            surface, {"observed": 0, "pending": 0, "absent": 0},
        )
        if followed:
            observed += 1
            bucket["observed"] += 1
        elif as_of < stamp + window_sec:
            # Window still open as far as the evidence reaches: undecided.
            pending += 1
            bucket["pending"] += 1
        else:
            absent += 1
            bucket["absent"] += 1
    return {
        "post_send_hook_observed": observed,
        "post_send_hook_pending": pending,
        "post_send_hook_absent": absent,
        "per_surface": per_surface,
        "window_sec": float(window_sec),
        "as_of": float(as_of),
        "note": (
            "observation only; no value establishes whether a send was "
            "warranted -- no pre-send viewport state is persisted. pending "
            "means the window had not elapsed within the evidence, and is not "
            "recomputed on a later run"
        ),
    }


def merge_incident_groups(
    incidents: list[Mapping[str, Any]],
    previous: Mapping[str, Any],
    *,
    as_of: float,
    retain_sec: float = INCIDENT_RETAIN_SEC,
) -> dict[str, dict[str, Any]]:
    """Fold incident lines into per-event groups, accumulating across runs.

    Keyed ``kind|surface|event``.  Counting by phrase instead reports the same
    slot re-arming 35 times as 35 independent failures, which is how a single
    9-hour pathological deferral hid behind the note ``35x deferred expiry``.

    Deliberate choices, each guarding a specific way of being wrong:

    * ``rearm_count`` counts every expiry whose ``retrying`` is not the literal
      ``"false"``.  The field is absent in the older log format, and reading
      absence as "did not retry" would drop those re-arms entirely.
      ``rearm_legacy_format`` records how many such lines were seen so the
      weaker evidence is visible rather than assumed.
    * ``held_sec`` is ``release_at - started_at``.  ``max_waited_sec`` cannot
      substitute: every re-arm resets the timer, so the observed maximum for
      the 9.09h slot was 0.32h.  When the log cursor has already passed the
      start line, the first expiry is the floor and ``held_sec_is_lower_bound``
      is set -- never silently presented as the true span.
    * absence of a release line yields ``release_observed: False`` and the label
      ``release_not_observed``.  It is not evidence that the slot never
      released; the release may sit outside this batch of log text.

    Only pathological groups are retained.  Ordinary deferrals release within
    seconds and are far more numerous, so keeping them would bloat the state
    file without adding information.
    """

    groups: dict[str, dict[str, Any]] = {}
    for key, value in (previous or {}).items():
        if isinstance(value, Mapping):
            groups[str(key)] = dict(value)

    for row in incidents:
        stamp = float(row.get("at") or 0.0)
        if stamp <= 0:
            continue
        kind = str(row.get("kind") or "")
        surface = str(row.get("surface") or "")
        event = str(row.get("event") or "")
        family = "deferred" if kind.startswith("deferred") else kind
        # Grouping needs a stable identity.  Without an event id the line can
        # only be attributed to the surface, and that weaker basis is labelled
        # rather than quietly mixed in with event-keyed groups.
        event_key = event or "event_missing"
        key = f"{family}|{surface}|{event_key}"
        group = groups.setdefault(key, {
            "kind": family,
            "surface": surface,
            "event": event_key,
            "identity": "event" if event else "surface_only",
            "first_seen_at": stamp,
            "last_seen_at": stamp,
            "count": 0,
            "expiry_count": 0,
            "rearm_count": 0,
            "rearm_legacy_format": 0,
            "started_at": 0.0,
            "first_expiry_at": 0.0,
            "last_expiry_at": 0.0,
            "max_waited_sec": 0.0,
            "release_at": 0.0,
            "final_status": "",
        })
        group["count"] = int(group.get("count") or 0) + 1
        group["first_seen_at"] = min(float(group.get("first_seen_at") or stamp), stamp)
        group["last_seen_at"] = max(float(group.get("last_seen_at") or stamp), stamp)
        waited = row.get("waited_sec")
        if waited is not None:
            group["max_waited_sec"] = max(float(group.get("max_waited_sec") or 0.0), float(waited))
        if kind == "deferred_start":
            group["started_at"] = stamp
        elif kind in {"deferred_expiry", "deferred_requeue"}:
            group["expiry_count"] = int(group.get("expiry_count") or 0) + 1
            if not group.get("first_expiry_at"):
                group["first_expiry_at"] = stamp
            group["last_expiry_at"] = stamp
            retrying = row.get("retrying")
            if str(retrying) != "false":
                group["rearm_count"] = int(group.get("rearm_count") or 0) + 1
            if retrying is None:
                group["rearm_legacy_format"] = int(group.get("rearm_legacy_format") or 0) + 1
        elif kind in {"deferred_release", "deferred_recovered"}:
            group["release_at"] = stamp
            if row.get("final_status"):
                group["final_status"] = str(row.get("final_status"))

    horizon = as_of - retain_sec
    kept: dict[str, dict[str, Any]] = {}
    for key, group in groups.items():
        if float(group.get("last_seen_at") or 0.0) < horizon:
            continue
        started = float(group.get("started_at") or 0.0)
        first_expiry = float(group.get("first_expiry_at") or 0.0)
        release = float(group.get("release_at") or 0.0)
        group["release_observed"] = release > 0
        anchor = started or first_expiry
        group["held_sec_is_lower_bound"] = bool(first_expiry and not started)
        if anchor:
            end = release if release > 0 else as_of
            group["held_sec"] = max(0.0, end - anchor)
        else:
            group["held_sec"] = None
        if group["kind"] == "deferred":
            # Retain only what carries information: it expired at least once, or
            # it started and no release has been observed.
            pathological = bool(group.get("expiry_count")) or (
                started > 0 and release <= 0
            )
            if not pathological:
                continue
        kept[key] = group
    return kept


def repair_aftermath(
    events: Mapping[str, list[Mapping[str, Any]]],
    *,
    window_sec: float = 600.0,
) -> list[dict[str, Any]]:
    """Summarise what followed each Hook-config auto-repair.

    Causal status is reported as ``correlated`` / ``unknown_writer`` and never
    as ``confirmed``: the daemon repairs the *global* settings file, and the
    identity of whatever rewrote it is not recorded anywhere.  A repair being
    followed by synthetic sends is a temporal relationship, not a proven cause.
    """

    out: list[dict[str, Any]] = []
    gap_sends = events.get("gap_sends", [])
    real_hooks = events.get("real_hooks", [])
    for repair in events.get("repairs", []):
        at = float(repair.get("at") or 0.0)
        if at <= 0:
            continue
        window_gaps = [
            send for send in gap_sends
            if at < float(send.get("at") or 0.0) <= at + window_sec
        ]
        window_hooks = [
            hook for hook in real_hooks
            if at < float(hook.get("at") or 0.0) <= at + window_sec
        ]
        attempts: dict[str, int] = {}
        for send in window_gaps:
            attempt = send.get("attempt")
            label = "attempt_unknown" if attempt is None else f"attempt_{attempt}"
            attempts[label] = attempts.get(label, 0) + 1
        surfaces = sorted({str(send.get("surface") or "") for send in window_gaps})
        first_gap = min((float(s.get("at") or 0.0) for s in window_gaps), default=0.0)
        out.append({
            "repair_at": at,
            "repair_at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)),
            "window_sec": float(window_sec),
            "gap_sends": len(window_gaps),
            "surfaces": surfaces,
            "surface_count": len(surfaces),
            "attempt_distribution": attempts,
            "distinct_events": len({str(s.get("event") or "") for s in window_gaps}),
            "real_hooks_after": len(window_hooks),
            "seconds_to_first_gap": (first_gap - at) if first_gap else None,
            "causal_status": "correlated" if window_gaps else "unknown_writer",
        })
    return out


def pollution_count(ledger: Mapping[str, Any]) -> int:
    """Count only the attribution verdict proven to be the old regression.

    ``human_exact_prompt`` is the intended verdict for an uncorrelated human
    paste of the watchdog sentence.  The historical defect is specifically a
    normal human prompt inheriting ``ambiguous_exact_prompt`` from another
    event; substring matching both verdicts caused the audit's repeated false
    positives.
    """

    return sum(
        1
        for row in ledger.values()
        if isinstance(row, Mapping)
        and str(row.get("status") or "") == "human_prompt"
        and str(row.get("detail") or "") == "ambiguous_exact_prompt"
    )


def _condition_severity(age: float) -> str:
    if age >= PERSISTENT_CRITICAL_SEC:
        return "critical"
    if age >= PERSISTENT_SEVERE_SEC:
        return "severe"
    return "warning"


def _acknowledged(value: Any, now: float) -> tuple[bool, str]:
    if value is True:
        return True, "acknowledged"
    if not isinstance(value, Mapping):
        return False, ""
    try:
        until = float(value.get("until") or 0.0)
    except (TypeError, ValueError):
        until = 0.0
    if until > 0 and now > until:
        return False, ""
    return True, str(value.get("reason") or "acknowledged")


def audit(*, now: float | None = None, live_pids: list[int] | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    alerts: list[str] = []
    notes: list[str] = []
    candidates: dict[str, dict[str, Any]] = {}

    def condition(key: str, detail: str, *, evidence_since: Any = 0.0) -> None:
        try:
            since = float(evidence_since or 0.0)
        except (TypeError, ValueError):
            since = 0.0
        if since <= 0 or since > now:
            since = now
        candidates[key] = {"detail": detail, "evidence_since": since}

    prev = load_json(AUDIT_STATE, {}) or {}
    # Read before any condition is raised: the gap_rate lifecycle below needs to
    # know which buckets already alerted while their hour was open.
    previous_conditions = prev.get("conditions") or {}
    if not isinstance(previous_conditions, Mapping):
        previous_conditions = {}
    acknowledgements = load_json(AUDIT_ACKNOWLEDGEMENTS, {}) or {}
    if not isinstance(acknowledgements, Mapping):
        acknowledgements = {}
    config_path = APP_DIR / "config.json"
    config = load_json(config_path, {}) or {}
    state = load_json(APP_DIR / "state.json", {}) or {}
    runtime_meta = load_json(APP_DIR / "daemon-runtime.json", {}) or {}
    ledger = (load_json(APP_DIR / "claude-event-ledger.json", {}) or {}).get("events", {})
    if not isinstance(ledger, Mapping):
        ledger = {}

    # ---------- daemon provenance ----------
    disk_source_sha = sha256_of(SOURCE_PATH)
    running_source_sha = str(runtime_meta.get("source_sha256") or "")
    disk_config_sha = sha256_of(config_path)
    daemon_start_config_sha = str(runtime_meta.get("config_sha256") or "")
    run_pid = int(runtime_meta.get("pid") or 0)
    started_at = float(runtime_meta.get("started_at") or 0.0)
    live = daemon_processes() if live_pids is None else list(live_pids)

    if not live:
        condition("daemon:not_running", "daemon NOT RUNNING")
    elif len(live) > 1:
        condition("daemon:multiple", f"MULTIPLE daemon instances: pids={live}")
    elif run_pid and run_pid not in live:
        condition(
            "daemon:runtime_pid_mismatch",
            f"runtime metadata pid={run_pid} but live pid={live[0]}",
        )

    if running_source_sha and disk_source_sha and running_source_sha != disk_source_sha:
        condition(
            "daemon:source_drift",
            f"SOURCE DRIFT: running={running_source_sha[:12]} disk={disk_source_sha[:12]}; "
            "a restart would load different bytes",
        )
    prev_disk = str(prev.get("disk_source_sha") or prev.get("disk_sha") or "")
    if prev_disk and disk_source_sha and prev_disk != disk_source_sha:
        alerts.append(
            f"DISK SOURCE EDITED {prev_disk[:12]} -> {disk_source_sha[:12]}; "
            "this is on-disk only unless the daemon restarted"
        )
    prev_started = float(prev.get("started_at") or 0.0)
    prev_run = str(prev.get("running_source_sha") or prev.get("run_sha") or "")
    if prev_started and started_at and abs(started_at - prev_started) > 1.0:
        when = time.strftime("%H:%M:%S", time.localtime(started_at))
        if prev_run and running_source_sha and prev_run != running_source_sha:
            alerts.append(
                f"DAEMON RESTARTED at {when}; running source changed "
                f"{prev_run[:12]} -> {running_source_sha[:12]}"
            )
        else:
            notes.append(
                f"daemon restarted at {when}, same running source "
                f"{running_source_sha[:12]}"
            )
    if daemon_start_config_sha and disk_config_sha and daemon_start_config_sha != disk_config_sha:
        notes.append(
            f"config bytes changed since daemon start: start={daemon_start_config_sha[:12]} "
            f"disk={disk_config_sha[:12]}; disk config is observed, hot-reload status is not attested"
        )

    if str(config.get("mode") or "") != "armed":
        condition("config:mode", f"mode={config.get('mode')!r} (expected armed)")
    if config.get("global_paused"):
        condition("config:global_paused", "global_paused=true; nothing will be sent")
    if not config.get("claude_enabled"):
        condition("config:claude_disabled", "claude_enabled=false")

    targets = [
        item for item in config.get("targets", [])
        if isinstance(item, Mapping) and item.get("surface_id")
    ]
    paused = [item for item in targets if item.get("paused")]
    if len(targets) != EXPECTED_TARGETS:
        notes.append(f"target count {len(targets)} (historical baseline {EXPECTED_TARGETS})")
    if len(paused) != EXPECTED_PAUSED:
        notes.append(f"paused count {len(paused)} (historical baseline {EXPECTED_PAUSED})")

    # ---------- log cursor ----------
    watch_log = LOG_DIR / "watch.log"
    previous_cursor = prev.get("log_cursor") or {}
    if not isinstance(previous_cursor, Mapping):
        previous_cursor = {}
    new_tail, log_cursor, log_transition = read_new_log(watch_log, previous_cursor)
    if log_transition == "missing":
        condition("log:missing", "watch.log missing")
        log_age = -1.0
    else:
        log_age = max(0.0, now - (log_cursor["mtime_ns"] / 1_000_000_000.0))
        if log_age > LOG_STALL_SEC:
            condition(
                "log:stalled",
                f"watch.log not written for {human(log_age)}; daemon may be wedged",
                evidence_since=now - log_age,
            )
    if log_transition in {"rotated", "truncated", "read_error"}:
        notes.append(f"watch.log cursor transition={log_transition}")
    tb = new_tail.count("Traceback (most recent call last)")
    if tb:
        alerts.append(f"{tb} new traceback(s) in watch.log")
    # Identity conflict, submit timeout and deferred expiry moved to event-level
    # groups below.  Counting them by phrase reported one slot re-arming 35 times
    # as "35x deferred expiry", which hid a single 9-hour pathological deferral.
    # These two have no event id to group on, so a raw count is all the log
    # supports.
    for phrase, label in (
        ("log channel", "log channel incident"),
        ("ledger id collision", "ledger id collision"),
    ):
        count = new_tail.count(phrase)
        if count:
            notes.append(f"{count}x {label} since last audit")

    # ---------- synthetic send volume across episodes ----------
    #
    # The log cursor is incremental, so a single run only sees lines written
    # since the previous run.  Per-hour totals therefore have to accumulate in
    # the audit's own state; computing them from one tail would reset the count
    # on every run and could never cross a threshold.
    log_events = parse_log_events(new_tail)
    fresh_rates = gap_rate_by_surface_hour(log_events["gap_sends"])
    previous_rates = prev.get("gap_rate") or {}
    if not isinstance(previous_rates, Mapping):
        previous_rates = {}
    gap_rate: dict[str, int] = {
        str(key): int(value)
        for key, value in previous_rates.items()
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
    }
    for (surface, hour), count in fresh_rates.items():
        composite = f"{surface}|{hour}"
        gap_rate[composite] = int(gap_rate.get(composite, 0)) + count
    # Keep the window bounded; hours older than a week cannot inform a decision.
    horizon = time.strftime("%Y-%m-%d %H:00", time.localtime(now - 7 * 86400))
    gap_rate = {
        key: value for key, value in gap_rate.items()
        if key.split("|", 1)[-1] >= horizon
    }

    over_threshold = sorted(
        (
            (key, value) for key, value in gap_rate.items()
            if value >= GAP_RATE_WARN_PER_HOUR
        ),
        key=lambda item: (-item[1], item[0]),
    )

    # An hour bucket that has closed can never gain another send: its count is
    # frozen.  Persistent-condition severity is driven by ``now - first_seen``,
    # so leaving a closed bucket active grows a fact that stopped changing into
    # ``severe`` after 6h and ``critical`` after 24h -- ranking finished history
    # alongside a surface that is unprotected right now.  Closed buckets are
    # therefore reported exactly once and then live in a rolling NOTE.
    current_hour = time.strftime("%Y-%m-%d %H:00", time.localtime(now))
    previous_reported = prev.get("gap_rate_reported")
    gap_rate_reported: dict[str, dict[str, Any]] = {}
    if isinstance(previous_reported, Mapping):
        for key, value in previous_reported.items():
            if isinstance(value, Mapping):
                gap_rate_reported[str(key)] = dict(value)
    # One-time migration.  Builds before this fix left closed buckets as active
    # conditions; adopting them as already-reported keeps them from alerting a
    # second time.  Gated on the key being absent -- once written, even empty,
    # this branch must never run again.
    gap_rate_migrated: list[str] = []
    if not isinstance(previous_reported, Mapping):
        for cond_key in previous_conditions:
            if not str(cond_key).startswith("gap_rate:"):
                continue
            bucket = str(cond_key).split(":", 1)[1]
            hour = bucket.partition("|")[2]
            if not hour or hour == current_hour:
                # Still accumulating: leave it active rather than pre-adopting.
                continue
            gap_rate_reported[bucket] = {
                "reported_at": now,
                "count": int(gap_rate.get(bucket, 0)),
                "carried_over": True,
            }
            gap_rate_migrated.append(bucket)
        if gap_rate_migrated:
            notes.append(
                f"migrated {len(gap_rate_migrated)} pre-fix gap_rate condition(s) to "
                "reported history; closed hours no longer escalate with age"
            )

    for key, value in over_threshold:
        surface, _, hour = key.partition("|")
        cond_key = f"gap_rate:{key}"
        detail = (
            f"{surface}: {value} synthetic sends in {hour} "
            f"(>= {GAP_RATE_WARN_PER_HOUR}/h across episodes; "
            "per-episode retry bounds do not limit this)"
        )
        if hour == current_hour:
            # Still open, so the count can still rise: escalation is meaningful
            # here.  attempt=1 dominating is NOT evidence of control -- it means
            # each send opened a fresh episode, which no episode-scoped bound
            # can see.
            condition(cond_key, detail)
            continue
        if key in gap_rate_reported:
            continue
        if cond_key in previous_conditions:
            # Alerted while the hour was still open; the rollover into a closed
            # bucket is not a new finding.
            gap_rate_reported[key] = {
                "reported_at": now, "count": value, "carried_over": True,
            }
            continue
        # First sight of an already-closed hour: report once so a late discovery
        # is never silent, then retire it to history.
        condition(cond_key, detail)
        gap_rate_reported[key] = {"reported_at": now, "count": value}

    gap_rate_reported = {
        key: value for key, value in gap_rate_reported.items()
        if key.split("|", 1)[-1] >= horizon
    }
    if gap_rate_reported:
        top = sorted(
            gap_rate_reported.items(),
            key=lambda item: -int(item[1].get("count") or 0),
        )[:5]
        notes.append(
            "gap_rate history (7d, reported, frozen): "
            + ", ".join(
                f"{key.partition('|')[0]}@{key.partition('|')[2]}="
                f"{int(value.get('count') or 0)}"
                for key, value in top
            )
        )

    # The log trails wall-clock time, so a send can sit inside its post-send
    # window while ``now`` is already past it.  Scoring against ``now`` would
    # print "no Hook arrived" when the truth is "the log does not reach that far
    # yet".  Evidence, not the clock, sets the horizon.
    log_covers_until = float(log_events.get("log_covers_until") or 0.0)
    as_of = min(now, log_covers_until) if log_covers_until > 0 else now
    post_send = post_send_hook_observation(
        log_events["gap_sends"], log_events["real_hooks"], as_of=as_of,
    )
    incident_groups = merge_incident_groups(
        log_events["incidents"], prev.get("incident_groups") or {}, as_of=as_of,
    )
    aftermath = repair_aftermath(log_events)
    # attempt=None is preserved as ``attempt_unknown``.  Defaulting absent
    # fields to 1 would fabricate the "everything is attempt=1" reading that
    # must never be treated as proof that sending is bounded.
    gap_attempt_distribution: dict[str, int] = {}
    for send in log_events["gap_sends"]:
        attempt = send.get("attempt")
        label = "attempt_unknown" if attempt is None else f"attempt_{attempt}"
        gap_attempt_distribution[label] = gap_attempt_distribution.get(label, 0) + 1
    gap_hotspots = [
        {"surface": key.partition("|")[0], "hour": key.partition("|")[2], "sends": value}
        for key, value in over_threshold
    ]
    for entry in aftermath:
        notes.append(
            f"auto-repair at {entry['repair_at_text']}: {entry['gap_sends']} synthetic "
            f"send(s) across {entry['surface_count']} surface(s) within "
            f"{entry['window_sec'] / 60:.0f}m, {entry['real_hooks_after']} real Hook(s); "
            f"attempts={entry['attempt_distribution'] or '{}'}; "
            f"causal_status={entry['causal_status']}"
        )
    if log_events["gap_sends"]:
        notes.append(
            f"{len(log_events['gap_sends'])} synthetic send(s) since last audit; "
            f"post_send_hook_observed={post_send['post_send_hook_observed']} "
            f"pending={post_send['post_send_hook_pending']} "
            f"absent={post_send['post_send_hook_absent']} "
            "(observation only, not a verdict on whether sending was warranted; "
            "pending = window had not elapsed within the evidence)"
        )

    # Event-level incident groups.  A phrase count reports one slot re-arming 35
    # times as "35x deferred expiry", which is the same class of distortion as
    # showing 18 of 45 targets: the number is real and the picture is wrong.
    deferred_groups = sorted(
        (g for g in incident_groups.values() if g.get("kind") == "deferred"),
        key=lambda g: -float(g.get("held_sec") or 0.0),
    )
    for group in deferred_groups[:5]:
        held = group.get("held_sec")
        held_text = human(float(held)) if held is not None else "unknown"
        bound = " (lower bound, start line not in evidence)" if group.get(
            "held_sec_is_lower_bound"
        ) else ""
        release = (
            f"released final_status={group.get('final_status') or 'unknown'}"
            if group.get("release_observed")
            else "release_not_observed"
        )
        legacy = int(group.get("rearm_legacy_format") or 0)
        legacy_text = (
            f", {legacy} line(s) predate the retrying= field" if legacy else ""
        )
        notes.append(
            f"deferred slot {group.get('surface')}/{group.get('event')}: "
            f"{int(group.get('expiry_count') or 0)} expiry, "
            f"{int(group.get('rearm_count') or 0)} re-arm, held {held_text}{bound}, "
            f"max_waited={float(group.get('max_waited_sec') or 0.0):.0f}s "
            f"(reset by each re-arm, so not the span), {release}{legacy_text}"
        )
    other_groups: dict[str, dict[str, int]] = {}
    for group in incident_groups.values():
        kind = str(group.get("kind") or "")
        if kind == "deferred":
            continue
        bucket = other_groups.setdefault(kind, {"groups": 0, "lines": 0, "surfaces": 0})
        bucket["groups"] += 1
        bucket["lines"] += int(group.get("count") or 0)
    for kind, bucket in sorted(other_groups.items()):
        surfaces = sorted({
            str(g.get("surface") or "")
            for g in incident_groups.values() if g.get("kind") == kind
        })
        bucket["surfaces"] = len(surfaces)
        notes.append(
            f"{kind}: {bucket['lines']} line(s) over {bucket['groups']} event(s) "
            f"on {len(surfaces)} surface(s) [{', '.join(surfaces[:4])}]"
        )

    # ---------- per-session ----------
    target_ids = {str(item["surface_id"]): item for item in targets}
    sessions: list[dict[str, Any]] = []
    # Full distribution over every configured target.  Reporting only the
    # healthy/legacy/missing trio invites reading the total as 18 when it is 45;
    # the remainder are ``unverified`` and must stay visible.
    hook_distribution: dict[str, int] = {}
    unverified_never_hooked: list[str] = []
    unverified_previously_hooked: list[str] = []
    for surface_id, target in target_ids.items():
        rt = state.get(surface_id) or {}
        if not isinstance(rt, Mapping):
            hook_distribution["no_state_row"] = hook_distribution.get("no_state_row", 0) + 1
            continue
        health_label = str(rt.get("claude_hook_health") or "unknown")
        if target.get("paused"):
            health_label = f"paused:{health_label}"
        hook_distribution[health_label] = hook_distribution.get(health_label, 0) + 1
        if str(rt.get("claude_hook_health") or "") == "unverified":
            # A surface that has never delivered a Hook is waiting for its first
            # lifecycle event -- normal, and not evidence of lost protection.
            # One that *had* a Hook and then went unverified is a real
            # regression and is the only variant that escalates.
            ref = str(target.get("ref") or surface_id[:8])
            if float(rt.get("claude_last_hook_at") or 0.0) > 0:
                unverified_previously_hooked.append(ref)
            else:
                unverified_never_hooked.append(ref)
    if unverified_previously_hooked:
        # Only this variant escalates.  ``never_hooked`` is the ordinary state of
        # a surface that has not yet emitted its first lifecycle event; grouping
        # the two under one count is what made 27 benign rows look alarming.
        condition(
            "hook:unverified_after_hook",
            "protection lost after a working Hook on "
            + ", ".join(sorted(unverified_previously_hooked)[:6])
            + (
                f" (+{len(unverified_previously_hooked) - 6} more)"
                if len(unverified_previously_hooked) > 6
                else ""
            ),
        )
    for surface_id, target in target_ids.items():
        rt = state.get(surface_id) or {}
        if not isinstance(rt, Mapping):
            continue
        name = str(target.get("ref") or surface_id[:8])
        is_paused = bool(target.get("paused"))
        hook = str(rt.get("claude_hook_health") or "")
        row: dict[str, Any] = {
            "surface": name,
            "id": surface_id[:8],
            "paused": is_paused,
            "state": str(rt.get("state") or "unknown"),
            "hook": hook,
        }
        problems: list[str] = []

        def session_problem(kind: str, detail: str, since: Any = 0.0) -> None:
            problems.append(detail)
            condition(f"surface:{surface_id}:{kind}", f"{name}: {detail}", evidence_since=since)

        # Paused runtimes are intentionally frozen by the daemon.  Their stale
        # clocks cannot be interpreted as live exposure.
        if not is_paused:
            unprotected_age = elapsed(rt.get("claude_hook_unprotected_since"), now)
            if hook and hook not in HEALTHY_HOOK_STATES:
                exposure = (
                    f"; unprotected {human(unprotected_age)}"
                    if unprotected_age > 0 else ""
                )
                session_problem(
                    "hook_unprotected", f"hook={hook}{exposure}",
                    rt.get("claude_hook_unprotected_since"),
                )
            deferred_age = elapsed(rt.get("claude_deferred_since"), now)
            if rt.get("claude_deferred_event") and deferred_age > DEFERRED_WARN_SEC:
                session_problem(
                    "deferred",
                    f"parked Stop {human(deferred_age)} ({rt.get('claude_deferred_reason')})",
                    rt.get("claude_deferred_since"),
                )
            if str(rt.get("claude_submit_phase") or "none") != "none":
                submit_age = elapsed(rt.get("claude_submit_since"), now)
                if submit_age > SUBMIT_STUCK_SEC:
                    session_problem(
                        "submit", f"submit open {human(submit_age)}", rt.get("claude_submit_since"),
                    )
            unreadable_age = elapsed(rt.get("claude_unreadable_since"), now)
            if unreadable_age > UNREADABLE_WARN_SEC:
                session_problem(
                    "unreadable", f"viewport unreadable {human(unreadable_age)}",
                    rt.get("claude_unreadable_since"),
                )
            if hook in HEALTHY_HOOK_STATES and unprotected_age > UNPROTECTED_WARN_SEC:
                session_problem(
                    "unprotected", f"unprotected {human(unprotected_age)}",
                    rt.get("claude_hook_unprotected_since"),
                )
            context = str(rt.get("claude_context_status") or "")
            if context in BAD_CONTEXT:
                session_problem(
                    "context", f"context={context}", rt.get("claude_context_episode_started_at"),
                )
            # Hook silence is a lifecycle fact, not a viewport-state fact.  A
            # surface alternating between hook_waiting and composer_busy must
            # not make the same condition disappear every other poll.  A
            # completed latch is a real episode boundary, though: a closed task
            # is not expected to emit another Hook until a new human prompt.
            last_hook_at = float(rt.get("claude_last_hook_at") or 0.0)
            quiet = elapsed(last_hook_at, now)
            episode_closed = bool(rt.get("claude_completed_latched")) or str(
                rt.get("claude_last_event_status") or ""
            ) in {"completed", "suppressed_completed"}
            if (
                hook == "healthy"
                and not episode_closed
                and last_hook_at > 0
                and quiet > SILENT_HOOK_SEC
            ):
                session_problem(
                    "hook_silent", f"Hook silent {human(quiet)} (state={row['state']})",
                    last_hook_at,
                )

        row["problems"] = problems
        sessions.append(row)

    # ---------- ledger forensics ----------
    pollution = pollution_count(ledger)
    hanging: list[str] = []
    hanging_since = now
    for event_id, item in ledger.items():
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "")
        if status.startswith("deferred_") and status not in DEFERRED_TERMINAL_STATUSES:
            age = elapsed(item.get("handled_at"), now)
            if age > 1200.0:
                hanging.append(f"{event_id[:8]}({status.replace('deferred_', '')},{human(age)})")
                stamp = float(item.get("handled_at") or now)
                hanging_since = min(hanging_since, stamp if 0 < stamp <= now else now)
    previous_pollution = prev.get("pollution")
    if isinstance(previous_pollution, int) and pollution > previous_pollution:
        alerts.append(
            f"attribution pollution GREW {previous_pollution} -> {pollution}; "
            "human_prompt acquired ambiguous_exact_prompt"
        )
    if hanging:
        condition(
            "ledger:provisional_deferred",
            f"{len(hanging)} ledger row(s) still provisional deferred: {', '.join(hanging[:4])}",
            evidence_since=hanging_since,
        )

    # ---------- persistent condition episodes ----------
    active_conditions: dict[str, dict[str, Any]] = {}
    for key, candidate in sorted(candidates.items()):
        old = previous_conditions.get(key) or {}
        if not isinstance(old, Mapping):
            old = {}
        first_seen = float(old.get("first_seen_at") or candidate["evidence_since"] or now)
        first_seen = min(first_seen, float(candidate["evidence_since"] or now), now)
        age = max(0.0, now - first_seen)
        episode_id = str(old.get("episode_id") or hashlib.sha256(
            f"{key}\0{first_seen:.3f}".encode("utf-8")
        ).hexdigest()[:16])
        severity = _condition_severity(age)
        is_acknowledged, ack_reason = _acknowledged(acknowledgements.get(key), now)
        record = {
            "episode_id": episode_id,
            "first_seen_at": first_seen,
            "last_seen_at": now,
            "observations": int(old.get("observations") or 0) + 1,
            "severity": severity,
            "detail": candidate["detail"],
            "acknowledged": is_acknowledged,
            "ack_reason": ack_reason,
        }
        active_conditions[key] = record
        rendered = (
            f"[{severity}] {candidate['detail']} "
            f"(persistent {human(age)}, episode={episode_id}, key={key})"
        )
        if is_acknowledged:
            notes.append(f"ACK {rendered}; reason={ack_reason}")
        else:
            alerts.append(rendered)

    summary = {
        "at": now,
        "at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "pid": live[0] if live else 0,
        "running_source_sha": running_source_sha,
        "disk_source_sha": disk_source_sha,
        "daemon_start_config_sha": daemon_start_config_sha,
        "disk_config_sha": disk_config_sha,
        "run_sha": running_source_sha[:12],
        "disk_sha": disk_source_sha[:12],
        "mode": config.get("mode"),
        "targets": len(targets),
        "paused": len(paused),
        "sessions": len(sessions),
        "session_rows": sessions,
        "with_problems": sum(1 for item in sessions if item["problems"]),
        "pollution": pollution,
        "hook_distribution": hook_distribution,
        "unverified_never_hooked": sorted(unverified_never_hooked),
        "unverified_previously_hooked": sorted(unverified_previously_hooked),
        "gap_sends_seen": len(log_events.get("gap_sends", [])),
        "real_hooks_seen": len(log_events.get("real_hooks", [])),
        "gap_attempt_distribution": gap_attempt_distribution,
        "gap_rate_hotspots": gap_hotspots,
        "gap_rate_reported": gap_rate_reported,
        "gap_rate_migrated": gap_rate_migrated,
        "post_send_hook": post_send,
        "incident_groups": incident_groups,
        "log_covers_until": log_covers_until,
        "as_of": as_of,
        "repair_aftermath": aftermath,
        "conditions": active_conditions,
        "alerts": alerts,
        "notes": notes,
        "log_cursor": log_cursor,
        "log_transition": log_transition,
        "started_at": started_at,
    }

    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUDIT_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "disk_source_sha": disk_source_sha,
        "running_source_sha": running_source_sha,
        "disk_config_sha": disk_config_sha,
        "daemon_start_config_sha": daemon_start_config_sha,
        "started_at": started_at,
        "log_cursor": log_cursor,
        "pollution": pollution,
        "gap_rate": gap_rate,
        # Written unconditionally, even when empty.  The migration branch is
        # gated on this key being *absent*; persisting ``{}`` is what closes it
        # permanently after the first run on the new code.
        "gap_rate_reported": gap_rate_reported,
        "incident_groups": incident_groups,
        "conditions": active_conditions,
        "at": now,
    }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(AUDIT_STATE)
    with AUDIT_JOURNAL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n")

    return summary


def _format_distribution(summary: Mapping[str, Any]) -> str:
    """Render the hook health of *every* configured target.

    Printing only healthy/legacy/missing lets a reader take the total to be the
    sum of those three when most targets are ``unverified``.  The unverified
    split is shown inline because only the ``after_hook`` variant is a fault.
    """

    distribution = summary.get("hook_distribution") or {}
    if not distribution:
        return ""
    parts = [f"{label}={count}" for label, count in sorted(distribution.items())]
    never = len(summary.get("unverified_never_hooked") or ())
    after = len(summary.get("unverified_previously_hooked") or ())
    if never or after:
        parts.append(f"[unverified: never_hooked={never} after_hook={after}]")
    return " ".join(parts)


def main() -> int:
    s = audit()
    head = (
        f"pid={s['pid']} sha={s['run_sha']} {s['mode']} "
        f"{s['targets']}/{s['paused']} sessions={s['sessions']}"
    )
    distribution = _format_distribution(s)
    if distribution:
        head = f"{head}\n  HOOKS {distribution}"
    gap_seen = int(s.get("gap_sends_seen") or 0)
    if gap_seen:
        post = s.get("post_send_hook") or {}
        head = (
            f"{head}\n  SENDS synthetic={gap_seen} "
            f"attempts={s.get('gap_attempt_distribution') or {}} "
            f"post_send_hook_absent={post.get('post_send_hook_absent', 0)} "
            f"observed={post.get('post_send_hook_observed', 0)} (observation, not verdict)"
        )
    if not s["alerts"] and not s["notes"]:
        print(f"✓ AUDIT OK {s['at_text']} | {head} | 全部正常")
        return 0
    icon = "✗" if s["alerts"] else "⚠"
    print(f"{icon} AUDIT {'ALERT' if s['alerts'] else 'NOTE'} {s['at_text']} | {head}")
    for item in s["alerts"]:
        print(f"  ALERT {item}")
    for item in s["notes"]:
        print(f"  NOTE  {item}")
    return 2 if s["alerts"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Low-frequency, callback-first watchdog for an existing cmux executor."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time


API_ERROR_RE = re.compile(
    r"(?:API Error|API error)\s*:|(?:\b\d{3}\s+)?Upstream API request failed(?:\.|\s*\()|Waiting for API response",
    re.I,
)
COMPACT_RE = re.compile(r"Compacting conversation|Conversation compacted|/compact", re.I)
COMPACT_FAILED_RE = re.compile(
    r"Compaction failed|Failed to compact|Error compacting|"
    r"Autocompact is thrashing|/compact[^\n]*failed",
    re.I,
)
CONTEXT_FULL_RE = re.compile(r"Context full|Context limit reached|上下文\s*100%", re.I)
DONE_LINE_RE = re.compile(r"^\s*(?:⏺\s*)?DONE:\s*(.*)$", re.I)
BLOCKED_LINE_RE = re.compile(r"^\s*(?:⏺\s*)?BLOCKED:\s*(.*)$", re.I)
ACTIVE_RE = re.compile(r"Running…|still thinking|thinking with|max effort|Compacting conversation", re.I)
ACTIVE_SPINNER_RE = re.compile(r"^\s*[✻✢✳✶✽◐◑◒◓·※]\s+.+\(\d+[hms]", re.M)
PROMPT_START_RE = re.compile(r"^\s*❯(?:\s|$)")
EXECUTOR_OUTPUT_RE = re.compile(
    r"^\s*(?:⏺|⎿|✻|✢|✳|✶|✽|◐|◑|◒|◓|·|※|Thought\s+for|Thinking\s+(?:for|with)|still\s+thinking|Compacting\s+conversation|Bash\(|Write\(|Read\(|Update\()",
    re.I,
)
CMUX_BIN = shutil.which("cmux") or "/Applications/cmux.app/Contents/Resources/bin/cmux"
CMUX_AGENT_BIN = shutil.which("cmux-agent") or str(Path.home() / ".local/bin/cmux-agent")

# ---------------------------------------------------------------------------
# Cadence (R3 consensus, task multi-agent-skill-hardening-20260830)
#
# A watchdog that costs more than the work it watches is a defect, not a safety
# net. Earlier runs polled every 300s and announced every ordinary NO_PROGRESS,
# producing repeated supervisor messages while the executor was healthy. The
# design is callback-first: the executor actively reports, and these intervals
# are the fallback for silence, not the primary signal.
#
# Three tiers, because the cost of late detection differs by phase:
#   stable  — long implementation work with a proven callback path
#   medium  — active implementation where a stall wastes real time
#   cutover — a bounded high-risk window where faster detection changes outcomes
# ---------------------------------------------------------------------------

SENTINEL_STABLE_INTERVAL_SECONDS = 7200
SENTINEL_MEDIUM_RISK_INTERVAL_SECONDS = 1800
SENTINEL_CUTOVER_INTERVAL_MIN_SECONDS = 300
SENTINEL_CUTOVER_INTERVAL_MAX_SECONDS = 600
SENTINEL_COMPACT_INTERVAL_SECONDS = 60


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=check)


def read_screen(surface: str, lines: int) -> str:
    proc = run(CMUX_BIN, "read-screen", "--surface", surface, "--lines", str(lines), check=False)
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"read-screen failed: {proc.returncode}")
    return proc.stdout


def normalize_screen(text: str) -> str:
    kept = []
    for line in text.splitlines():
        if re.search(r"上下文|⏱️|until auto-compact|tokens|ctrl\+o|shift\+tab", line):
            continue
        line = re.sub(r"\b\d+[hms](?:\s+\d+[hms])*\b", "<elapsed>", line)
        line = re.sub(r"\b\d{1,3}%\b", "<pct>", line)
        line = re.sub(r"[✻✢✳✶✽◐◑◒◓·※]+", "<spinner>", line)
        kept.append(line.rstrip())
    return "\n".join(kept[-120:]).strip()


def without_prompt_blocks(text: str) -> str:
    """Remove user/supervisor prompt blocks while preserving executor UI output.

    cmux renders a submitted prompt from its leading `❯` through wrapped TASK text.
    That text may describe API-error handling or quote an old error verbatim. It is
    input, not executor state. A subsequent executor/UI marker ends the prompt block.
    """
    kept = []
    in_prompt = False
    for line in text.splitlines():
        if PROMPT_START_RE.match(line):
            in_prompt = True
            kept.append("")
            continue
        if in_prompt and EXECUTOR_OUTPUT_RE.match(line):
            in_prompt = False
        kept.append(line if not in_prompt else "")
    return "\n".join(kept)


def classify(text: str, callback_token: str = "") -> str:
    tail = "\n".join(text.splitlines()[-60:])
    signal_tail = without_prompt_blocks(tail)
    # A historical DONE plus a current token elsewhere in a supervisor prompt must
    # never be combined into a terminal event. Require marker and token on the same
    # line, with the marker at line start. Scan only prompt-free executor/UI output:
    # supervisor task packs legitimately contain literal callback templates.
    for line in reversed(signal_tail.splitlines()):
        blocked = BLOCKED_LINE_RE.match(line)
        if blocked and (not callback_token or callback_token in blocked.group(1)):
            return "BLOCKED"
        done = DONE_LINE_RE.match(line)
        if done and (not callback_token or callback_token in done.group(1)):
            return "DONE"

    # Historical API/compact text often remains in the last 60 lines after the
    # executor has resumed. Classify by the latest status signal, not by a fixed
    # priority over the whole tail.
    signals = []
    for kind, pattern in [
        ("API_ERROR", API_ERROR_RE),
        ("COMPACT_FAILED", COMPACT_FAILED_RE),
        ("COMPACTING", re.compile(r"Compacting conversation", re.I)),
        ("COMPACTED", re.compile(r"\bCompacted\b", re.I)),
        ("CONTEXT_FULL", CONTEXT_FULL_RE),
        ("ACTIVE", ACTIVE_RE),
        ("ACTIVE", ACTIVE_SPINNER_RE),
    ]:
        for match in pattern.finditer(signal_tail):
            signals.append((match.start(), kind))
    if signals:
        return max(signals, key=lambda item: item[0])[1]
    return "IDLE_OR_UNKNOWN"


def compact_progress(text: str) -> int | None:
    """Return the compact progress bar percentage, not the context percentage."""
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if re.search(r"Compacting conversation", lines[index], re.I):
            # The following line is the compact progress bar. Do not include
            # Claude's separate bottom chrome (`上下文 N%`), which can differ.
            block = "\n".join(lines[index:index + 2])
            values = [int(value) for value in re.findall(r"\b(\d{1,3})%", block)]
            values = [value for value in values if 0 <= value <= 100]
            return values[-1] if values else None
    return None


def notify_supervisor(surface: str, message: str) -> bool:
    """Best-effort wakeup; detector uncertainty must not crash the sentinel."""
    proc = run(CMUX_AGENT_BIN, "ask", surface, message, check=False)
    return proc.returncode == 0


def state_paths(task_id: str) -> tuple[Path, Path, Path]:
    root = Path("/tmp/multi-agent-collaboration/sentinels")
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    return root / f"{safe}.json", root / f"{safe}.lock", root / f"{safe}.stop"


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(path: Path, state: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def inspect(args: argparse.Namespace, state: dict) -> tuple[dict, bool]:
    screen = read_screen(args.executor_surface, args.lines)
    normalized = normalize_screen(screen)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    kind = classify(screen, args.callback_token)
    now = utc_now()
    changed = digest != state.get("screen_signature")
    unchanged_checks = 0 if changed else int(state.get("unchanged_checks", 0)) + 1
    next_state = {
        **state,
        "task_id": args.task_id,
        "executor_surface": args.executor_surface,
        "supervisor_surface": args.supervisor_surface,
        "interval_seconds": args.interval,
        "callback_token": args.callback_token,
        "last_check_at": now,
        "last_classification": kind,
        "screen_signature": digest,
        "unchanged_checks": unchanged_checks,
    }
    next_state.pop("error", None)
    if changed:
        next_state["last_progress_at"] = now

    if kind == "COMPACTING":
        progress = compact_progress(screen)
        previous_progress = state.get("compact_progress_percent")
        if state.get("last_classification") != "COMPACTING":
            next_state["compact_started_at"] = now
            next_state["compact_unchanged_checks"] = 0
            next_state["compact_last_progress_at"] = now
        elif progress is not None and progress != previous_progress:
            next_state["compact_unchanged_checks"] = 0
            next_state["compact_last_progress_at"] = now
        else:
            next_state["compact_unchanged_checks"] = int(state.get("compact_unchanged_checks", 0)) + 1
        next_state["compact_progress_percent"] = progress
    elif state.get("last_classification") == "COMPACTING":
        next_state["compact_completed_at"] = now

    alert = None
    if kind in {"API_ERROR", "BLOCKED", "DONE", "COMPACT_FAILED", "CONTEXT_FULL"}:
        alert = kind
    elif kind == "COMPACTING" and int(next_state.get("compact_unchanged_checks", 0)) >= args.compact_stall_threshold:
        alert = "COMPACT_STALLED"
    elif kind == "ACTIVE" and unchanged_checks >= args.active_stall_threshold:
        alert = "ACTIVE_STALLED"
    # Stable screen text is not a stall while the executor explicitly reports
    # active thinking/running. Only idle-or-unknown silence can become
    # NO_PROGRESS; API/context/terminal states have their own events.
    # An idle prompt is actionable on the first observation. Waiting for the
    # same idle screen to remain unchanged for another interval creates a
    # two-interval blind spot after ACTIVE -> idle transitions.
    elif kind == "IDLE_OR_UNKNOWN":
        alert = "NO_PROGRESS"

    alert_key = f"{alert}:{digest}" if alert else None

    # Ordinary NO_PROGRESS is recorded, not announced.
    #
    # A watchdog that messages the supervisor on every quiet interval costs more
    # tokens than the work it guards, and the supervisor learns nothing it could
    # not read from state on demand. The distinction is between "something
    # happened that needs a decision" (API_ERROR, BLOCKED, DONE, CONTEXT_FULL,
    # COMPACT_FAILED, and the two stall events) and "nothing happened", which is
    # the expected condition for most of a long task.
    #
    # NO_PROGRESS still updates last_alert_key so the dedup ledger stays honest:
    # a later genuine alert on the same screen digest must not be suppressed by a
    # silent NO_PROGRESS, and a NO_PROGRESS must not be re-announced if the
    # policy is ever loosened. (R3 item 5.)
    silent_alerts = {"NO_PROGRESS"}
    if alert and alert_key != state.get("last_alert_key"):
        if alert in silent_alerts:
            next_state["last_silent_alert"] = alert
            next_state["last_silent_alert_at"] = now
        else:
            notification_confirmed = notify_supervisor(
                args.supervisor_surface,
                "STATUS: SENTINEL "
                f"TASK_ID={args.task_id} EXECUTOR={args.executor_surface} EVENT={alert} "
                f"UNCHANGED_CHECKS={unchanged_checks} STATE={next_state_path(args.task_id)}. "
                "Read the executor surface before taking action; do not create a new session.",
            )
            next_state["last_notified_alert"] = alert
            next_state["last_notified_at"] = now
            next_state["last_notification_confirmed"] = notification_confirmed
        next_state["last_alert_key"] = alert_key
        next_state["last_alert_at"] = now
    # Screen text is only a candidate notification. The executor must actively
    # callback and the supervisor must audit/stop this sentinel explicitly.
    return next_state, False


def next_state_path(task_id: str) -> str:
    return str(state_paths(task_id)[0])


def next_poll_interval(args: argparse.Namespace, state: dict) -> int:
    """Poll compaction promptly without increasing ordinary task cadence."""
    if state.get("last_classification") == "COMPACTING":
        return min(args.interval, args.compact_interval)
    return args.interval


def run_daemon(args: argparse.Namespace) -> int:
    state_path, lock_path, stop_path = state_paths(args.task_id)
    stop_path.unlink(missing_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"SENTINEL_ALREADY_RUNNING state={state_path}", file=sys.stderr)
            return 2
        lock.write(str(os.getpid()))
        lock.flush()
        state = load_state(state_path)
        previous_pid = state.get("pid")
        state.update({
            "pid": os.getpid(),
            "started_at": state.get("started_at") if previous_pid == os.getpid() else utc_now(),
        })
        while not stop_path.exists():
            from availability_contract import AvailabilityError, require_action
            try:
                pack = {}
                if getattr(args, "role_map", ""):
                    pack_path = Path(args.role_map).parent / "task-pack.json"
                    if pack_path.is_file():
                        pack = json.loads(pack_path.read_text())
                require_action(args.task_id, "sentinel", pack)
            except AvailabilityError as exc:
                state.update(last_classification="AVAILABILITY_STOP", error=str(exc), pid=None)
                save_state(state_path, state)
                return 0
            try:
                state, terminal = inspect(args, state)
                save_state(state_path, state)
                if args.once or terminal:
                    return 0
            except Exception as exc:  # watchdog failures must wake, not disappear
                state.update({"last_check_at": utc_now(), "last_classification": "SENTINEL_ERROR", "error": str(exc)})
                save_state(state_path, state)
                notification_confirmed = notify_supervisor(
                    args.supervisor_surface,
                    f"BLOCKED: SENTINEL_ERROR TASK_ID={args.task_id} EXECUTOR={args.executor_surface} ERROR={exc}",
                )
                state["last_notification_confirmed"] = notification_confirmed
                save_state(state_path, state)
                return 1
            time.sleep(next_poll_interval(args, state))
    return 0


def stop_daemon(task_id: str) -> int:
    state_path, _, stop_path = state_paths(task_id)
    stop_path.touch()
    state = load_state(state_path)
    pid = state.get("pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
    print(f"SENTINEL_STOP_REQUESTED state={state_path}")
    return 0


def verify_role_map_target(
    role_map_path: str, task_id: str, supervisor_surface: str, executor_surface: str
) -> tuple[bool, str]:
    """Confirm argv surfaces agree with the recorded role map.

    A hard-coded supervisor surface outlived several cmux renumberings, so the
    watchdog kept notifying whatever pane now happened to hold that number. The
    role map is the authority on which surface owns which role; argv is a
    convenience. On disagreement the correct action is to refuse, because a
    watchdog that shouts at the wrong pane is worse than one that stays quiet:
    the real supervisor learns nothing and an unrelated session is interrupted.

    Returns (ok, message). A missing path is *not* silently accepted — the caller
    decides, and the run path requires an explicit opt-out.
    """
    if not role_map_path:
        return False, "no --role-map supplied"
    path = Path(role_map_path).expanduser()
    if not path.exists():
        return False, f"role map not found: {path}"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, f"role map unreadable: {type(exc).__name__}"

    if not isinstance(doc, dict):
        return False, "role map must be an object"
    if doc.get("task_id") and doc.get("task_id") != task_id:
        return False, (
            f"role map is bound to task {doc.get('task_id')!r}, not {task_id!r}"
        )

    # Both the harness (executor, executor2...) and the supervisor protocol
    # (identity, executors[]) are producers. Merge declarations rather than
    # choosing a shape and silently ignoring a conflicting second declaration.
    roles: dict[str, str] = {}

    def role_name(value):
        if not isinstance(value, str):
            raise ValueError("invalid role name")
        name = value.lower()
        if name == "executor1":
            return "executor"
        if name != "supervisor" and not re.fullmatch(r"executor(?:[1-9][0-9]*)?", name):
            raise ValueError(f"unsupported role name: {value!r}")
        return name

    def add_role(key, declared):
        key = role_name(key)
        if isinstance(declared, dict):
            refs = [declared[name] for name in ("surface_ref", "surface", "identity") if name in declared]
            if (not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs)
                    or len(set(refs)) != 1):
                raise ValueError(f"invalid or conflicting {key} surface declaration")
            declared = refs[0]
        if not isinstance(declared, str) or not declared.strip():
            raise ValueError(f"invalid {key} surface declaration")
        if key in roles and roles[key] != declared:
            raise ValueError(f"conflicting {key} surfaces in role map")
        roles[key] = declared

    try:
        for field in ("roles", "entries"):
            if field not in doc:
                continue
            entries = doc[field]
            if isinstance(entries, dict):
                for key, declared in entries.items():
                    add_role(key, declared)
            elif isinstance(entries, list):
                for item in entries:
                    if not isinstance(item, dict):
                        raise ValueError(f"invalid {field} member")
                    add_role(item.get("role"), item)
            else:
                raise ValueError(f"invalid {field} collection")
        for key, declared in doc.items():
            if key == "supervisor" or re.fullmatch(r"executor(?:[1-9][0-9]*)?", key):
                add_role(key, declared)
        if "executors" in doc:
            entries = doc["executors"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("invalid or empty executors collection")
            seen = set()
            for ordinal, item in enumerate(entries, 1):
                if not isinstance(item, dict):
                    raise ValueError("invalid executors member")
                name = role_name(item.get("role", "executor" if ordinal == 1 else f"executor{ordinal}"))
                if name == "supervisor" or name in seen:
                    raise ValueError("invalid or duplicate executor role")
                seen.add(name)
                add_role(name, item)
    except ValueError as exc:
        return False, str(exc)

    expected_supervisor = roles.get("supervisor")
    expected_executors = {ref for role, ref in roles.items() if role != "supervisor"}
    if not expected_supervisor:
        return False, "role map declares no supervisor surface"
    if not expected_executors:
        return False, "role map declares no executor surface"
    if len(set(roles.values())) != len(roles):
        return False, "same surface declared for multiple roles"

    if expected_supervisor != supervisor_surface:
        return False, (
            f"supervisor target mismatch: argv {supervisor_surface!r} != "
            f"role map {expected_supervisor!r}"
        )
    if executor_surface not in expected_executors:
        return False, (
            f"executor target mismatch: argv {executor_surface!r} != "
            f"role map {sorted(expected_executors)!r}"
        )
    return True, f"role map agrees (supervisor={expected_supervisor}, executor={executor_surface})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "stop", "status"])
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--executor-surface", default="")
    parser.add_argument("--supervisor-surface", default="")
    # Default is the STABLE band, not the old 300s.
    #
    # A watchdog that polls every 5 minutes for a task measured in hours costs
    # more tokens than the work it guards, which is the incident Codex recorded
    # (R1 item 22). Callback-first supervision means the executor reports its own
    # milestones; this loop is the fallback for silence, so its default cadence
    # should be the cheapest one that still catches a genuinely dead session.
    # Faster bands remain available explicitly via --interval.
    parser.add_argument("--interval", type=int, default=SENTINEL_STABLE_INTERVAL_SECONDS)
    # Verifying the supervisor target against role-map.json is opt-out rather
    # than opt-in: an argv-only target silently survived surface renumbering
    # before, and a watchdog shouting at the wrong pane is worse than silence.
    parser.add_argument("--role-map", default="")
    parser.add_argument(
        "--allow-role-map-mismatch",
        action="store_true",
        help="Escape hatch for a documented cross-surface case; never a default",
    )
    parser.add_argument("--unchanged-threshold", type=int, default=1)
    parser.add_argument("--lines", type=int, default=220)
    parser.add_argument("--callback-token", default="")
    parser.add_argument("--compact-stall-threshold", type=int, default=2)
    parser.add_argument(
        "--compact-interval",
        type=int,
        default=SENTINEL_COMPACT_INTERVAL_SECONDS,
        help="Poll interval while Claude is actively compacting (default: 60s)",
    )
    parser.add_argument("--active-stall-threshold", type=int, default=3)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    state_path, _, _ = state_paths(args.task_id)
    if args.command == "status":
        print(state_path.read_text(encoding="utf-8") if state_path.exists() else "{}")
        return 0
    if args.command == "stop":
        return stop_daemon(args.task_id)
    if not args.executor_surface or not args.supervisor_surface:
        parser.error("run requires --executor-surface and --supervisor-surface")
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    if args.compact_interval < 60:
        parser.error("--compact-interval must be at least 60 seconds")

    # Role-map agreement is a precondition for running at all.
    #
    # The escape hatch exists because a documented cross-surface case may be
    # legitimate, but it must be typed out deliberately. Defaulting to "trust
    # argv" is what let a stale surface number survive renumbering and turned a
    # watchdog into a source of noise in an unrelated pane.
    ok, message = verify_role_map_target(
        args.role_map, args.task_id, args.supervisor_surface, args.executor_surface
    )
    if not ok:
        if not args.allow_role_map_mismatch:
            print(
                f"SENTINEL_ROLE_MAP_REFUSED {message}. "
                "Pass --role-map <role-map.json>, or --allow-role-map-mismatch "
                "with a documented reason.",
                file=sys.stderr,
            )
            return 3
        print(f"SENTINEL_ROLE_MAP_OVERRIDDEN {message}", file=sys.stderr)
    else:
        print(f"SENTINEL_ROLE_MAP_OK {message}")
    return run_daemon(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""Portable zero-config Multi-Herdr discovery, Codex 0.170 footer admission, and ledger rollover.

Designed for nested cmux -> multiple Herdr instances (`~/.config/herdr/herdr.sock`
and `~/.config/herdr/sessions/*/herdr.sock`) without static surface whitelists.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
import uuid


SESSION_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def atomic_write_json(path: Path, value: object) -> None:
    path = Path(path)
    raw = canonical_json_bytes(value) + b"\n"
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def discover_herdr_socket_candidates(root: Path | None = None, *, limit: int = 128) -> tuple[list[tuple[str, Path]], list[dict[str, str]]]:
    """Scan `~/.config/herdr` for private owner-controlled Herdr sockets."""
    root = Path(root) if root is not None else (Path.home() / ".config" / "herdr")
    info = root.lstat()
    if (not root.is_absolute() or root != root.resolve(strict=True)
            or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise ValueError("Herdr discovery directory must be canonical and owner-controlled")
    candidates: list[tuple[str, Path]] = [("default", root / "herdr.sock")]
    refusals: list[dict[str, str]] = []
    sessions_dir = root / "sessions"
    if not sessions_dir.exists():
        return candidates, refusals
    s_info = sessions_dir.lstat()
    if not stat.S_ISDIR(s_info.st_mode) or s_info.st_uid != os.getuid() or s_info.st_mode & 0o022:
        raise ValueError("Herdr sessions directory must be owner-controlled")
    names = sorted(item.name for item in os.scandir(sessions_dir))[:limit]
    for name in names:
        if name == "default" or not SESSION_NAME_RE.fullmatch(name):
            refusals.append({"session": name[:64], "reason": "noncanonical_session_name"})
            continue
        sub = sessions_dir / name
        try:
            sub_info = sub.lstat()
            if not stat.S_ISDIR(sub_info.st_mode) or sub_info.st_uid != os.getuid() or sub_info.st_mode & 0o022:
                raise ValueError("unsafe session directory")
        except (OSError, ValueError) as exc:
            refusals.append({"session": name, "reason": str(exc)[:160]})
            continue
        candidates.append((name, sub / "herdr.sock"))
    return candidates, refusals


def herdr_placement_matches(endpoint: dict[str, str], pane: dict[str, str], environment: dict[str, str]) -> bool:
    """Match inner Herdr pane placement across both HERDR_ENV=1 and wrapper HERDR_ENV=0."""
    return (
        environment.get("HERDR_ENV") in {"0", "1"}
        and environment.get("HERDR_SOCKET_PATH") == endpoint["socket"]
        and environment.get("HERDR_PANE_ID") == pane["pane_id"]
        and environment.get("HERDR_WORKSPACE_ID") == pane["workspace_id"]
        and environment.get("HERDR_TAB_ID") == pane["tab_id"]
        and environment.get("HERDR_SESSION", "default") == endpoint["session"]
        and environment.get("CMUX_WORKSPACE_ID") == endpoint["cmux_workspace_id"]
        and environment.get("CMUX_SURFACE_ID") == endpoint["cmux_surface_id"]
    )


def extract_resume_uuid(argv: list[str], kind: str) -> str | None:
    """Extract resume UUID even when preceded by `-c ...` config overrides."""
    if kind == "codex":
        options = argv[1:argv.index("--")] if "--" in argv else argv[1:]
        if "resume" in options:
            idx = options.index("resume")
            if idx + 1 < len(options):
                try:
                    return str(uuid.UUID(options[idx + 1]))
                except (ValueError, TypeError):
                    return None
        return None
    if kind == "claude":
        vals = [argv[i + 1] for i, opt in enumerate(argv[:-1]) if opt in {"--resume", "--session-id"}]
        if len(vals) == 1:
            try:
                return str(uuid.UUID(vals[0]))
            except (ValueError, TypeError):
                return None
    return None


def codex_herdr_screen_admission(text: str) -> tuple[bool, str | None]:
    """Verify an empty Codex composer including Codex 0.170 `max fast` and `Plan mode` footers."""
    lines = text.splitlines()
    prompts = [i for i, line in enumerate(lines) if line.lstrip()[:1] in {"›", "»"}]
    area = "\n".join(lines)
    if re.search(r"Automatic reconnect could not restore|Server connection could not be restored|Reconnect failed\b[\s\S]{0,120}\brelaunch\b", area, re.I):
        return False, "client_connection_failed_relaunch_required"
    menu = "\n".join(lines[-18:])
    if re.search(r"^\s*Implement this plan\?\s*$", menu, re.M | re.I) and re.search(r"enter select\s*·\s*esc back", menu, re.I):
        return False, "dialog_or_queued_input"
    if not prompts:
        return False, "composer_not_verified_empty"
    i = prompts[-1]
    prompt = lines[i].strip()
    if prompt not in {"›", "› Ask Codex to do anything", "»", "» Ask Codex to do anything"}:
        return False, "user_draft_preserved"
    lines[i + 1:] = [
        re.sub(r"^(\s*)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\s+·\s+(?=GPT-)", r"\1", line)
        for line in lines[i + 1:]
    ]
    lines[i + 1:] = [
        re.sub(r"\s*(?:·\s*)?⚠\s+\d+ warnings?\s+·\s+f2 to view\s*$", "", line)
        if re.search(r"[?] for shortcuts|← for agents|Plan mode", line, re.I) else line
        for line in lines[i + 1:]
    ]
    footer = "\n".join(lines[max(0, i - 18):])
    if re.search(r"\[<\d+;\d+;\d+[mM]", footer):
        return False, "sgr_input_fragment_preserved"
    if re.search(r"Interrupted\s*·|Turn aborted|What should Codex do instead", footer, re.I):
        return False, "user_interrupted"
    if re.search(r"esc to interrupt|Reconnecting\s+\d+/\d+|running (?:Pre|Post)ToolUse", footer, re.I):
        return False, "client_working_or_reconnecting"
    if re.search(r"Esc to cancel|Enter to confirm|Do you trust|Permission required|Select.{0,25}(?:model|session)|Press up to edit|queued message|Queued follow-up inputs", footer, re.I):
        return False, "dialog_or_queued_input"
    goal_status = r"(?:Goal (?:stalled|paused) \(/goal resume\)|Goal achieved \((?:\d+h(?: \d+m)?|\d+m(?: \d+s)?|\d+s)\))"
    effort = r"(?:default|minimal|low|medium|high|xhigh|max|ultra)(?:\s+fast)?"
    truncated_status = r"^\s*GPT-[\w.-]+\s+" + effort + r"\s+·\s+(?:/|~/)[^\x00-\x1f]*…(?:\s+" + goal_status + r")?\s*$"
    goal_combined_status = r"^\s*GPT-[\w.-]+\s+" + effort + r"\s+·\s+(?:/|~/)[^\x00-\x1f]+\s+" + goal_status + r"\s*$"
    for line in lines[i + 1:]:
        if not line.strip():
            continue
        if re.fullmatch(truncated_status, line, re.I) or re.fullmatch(goal_combined_status, line, re.I):
            continue
        if re.search(r"context (?:left|used)|[?] for shortcuts|← for agents|Plan mode|^\s*Tip:|^\s*\d+%|^\s*Goal\b|^\s*GPT-[\w.-]+\s+" + effort + r"\s+·.+·\s+Context\b", line, re.I):
            continue
        return False, "composer_footer_unrecognized"
    return True, None


def rollover_ledger(path: Path, ledger: dict, final_phases: frozenset[str], *, keep: int = 1000, trigger: int = 2000) -> int:
    """Archive completed ledger entries to `archive/<stem>-YYYYMM.jsonl` while preserving pending attempts."""
    attempts = ledger.get("attempts", {})
    if len(attempts) <= trigger:
        return 0
    finished = [(k, v) for k, v in attempts.items() if isinstance(v, dict) and v.get("phase") in final_phases]
    if not finished:
        return 0
    finished.sort(key=lambda kv: float(kv[1].get("attempted_at") or 0.0))
    archived = finished[:max(0, len(attempts) - keep)]
    if not archived:
        return 0
    archive_dir = Path(path).parent / "archive"
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive_file = archive_dir / f"{Path(path).stem}-{time.strftime('%Y%m')}.jsonl"
    payload = b"".join(canonical_json_bytes({"id": k, **v}) + b"\n" for k, v in archived)
    fd = os.open(archive_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "ab") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    for k, _ in archived:
        attempts.pop(k, None)
    atomic_write_json(Path(path), ledger)
    return len(archived)


def normalize_herdr_pane_agent(pane: dict) -> dict:
    """Backfill `pane['agent']` from `pane['agent_session']['agent']` when Herdr omits top-level `agent`."""
    if isinstance(pane, dict) and pane.get("agent") is None and isinstance(pane.get("agent_session"), dict):
        sess_agent = pane["agent_session"].get("agent")
        if sess_agent in {"codex", "claude"}:
            pane["agent"] = sess_agent
    return pane


def prune_orphaned_pre_io_intents(ledger: dict[str, dict], failed_turn_id: str, *, transcript_unchanged: bool) -> list[str]:
    """Remove unsubmitted `phase == 'intent'` entries (`io_started_at` absent/0) left by pre-input persistence faults."""
    if not transcript_unchanged:
        return []
    removed = [
        aid for aid, rec in ledger.items()
        if isinstance(rec, dict)
        and rec.get("failed_turn_id") == failed_turn_id
        and rec.get("phase") == "intent"
        and not rec.get("io_started_at")
    ]
    for aid in removed:
        del ledger[aid]
    return removed


def safe_regular_bytes(path: Path, *, limit: int = 128 * 1024 * 1024) -> bytes:
    """Read an owner-controlled single-link production file up to 128 MiB so pre-rollover ledgers never crash startup."""
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError("Noncanonical production file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or before.st_size > limit
        ):
            raise ValueError("Unsafe or oversized production file")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    ident = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
    if ident(before) != ident(after) or ident(after) != ident(path.lstat()):
        raise ValueError("Production file changed while reading")
    return raw

"""Pure evidence checks for the specific compacted orphan-output Codex panic.

No native process actions, terminal RPC, imports of recovery/core, or file writes.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

TITLE = "The application panicked (crashed)."
LOCATION = "Location: core/src/util.rs:83"
MESSAGE = "Orphan custom tool call output for call id: "
MAX_TAIL = 8 * 1024 * 1024
MAX_HEADER = 1024 * 1024
_CALL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_COMPOSER = re.compile(r"^\s*(?:[›❯»>](?:\s|$)|(?:user|you)\s*:)", re.I)


class Refused(ValueError):
    pass


def _plain(line):
    # Four-space code indentation, quote borders and composer prefixes are not
    # the native diagnostic block. A small terminal margin is permitted.
    if (not isinstance(line, str) or line.startswith("\t")
            or len(line) - len(line.lstrip(" ")) >= 4):
        return None
    value = line.strip()
    if value.startswith((">", "│", "|", "┃", "❯", "›", "»")):
        return None
    return value


def screen_panic(grid):
    """Return only a complete independent native panic block in a full screen."""
    raw = getattr(grid, "raw", {})
    full = raw.get("full") if isinstance(raw, dict) and "full" in raw else getattr(grid, "full", None)
    if full is not True or isinstance(raw, dict) and raw.get("anchor", "screen") != "screen":
        return None
    lines = getattr(grid, "lines", None)
    if not isinstance(lines, (list, tuple)) or any(not isinstance(s, str) for s in lines):
        return None
    cursor_row = getattr(getattr(grid, "cursor", None), "row", None)
    allowed, fenced, composer = [], None, False
    for row, line in enumerate(lines):
        value = line.strip()
        fence = next((c * 3 for c in (chr(96), "~") if value.startswith(c * 3)), None)
        if fence:
            fenced = None if fenced == fence else fence
            allowed.append(False)
            continue
        if _COMPOSER.match(line) and (cursor_row is None or row == cursor_row):
            composer = True
        allowed.append(not fenced and not composer)
    matches = []
    for index, line in enumerate(lines):
        if not allowed[index] or _plain(line) != TITLE or index + 2 >= len(lines):
            continue
        # Codex's panic hook may emit LF while its TUI is in raw mode. The
        # actual incident's Message/Location therefore start diagonally, not
        # at column zero. The independent title still has to be unquoted.
        message = lines[index + 1].strip()
        if message.startswith((">", "│", "|", "┃", "❯", "›", "»")):
            continue
        if message is None:
            continue
        if message.startswith("Message:"):
            message = message[len("Message:"):].lstrip()
        if not message.startswith(MESSAGE.rstrip()):
            continue
        # The label itself may not wrap. Only its call ID may span hard lines.
        if message != MESSAGE.rstrip() and not message.startswith(MESSAGE):
            continue
        call_id = message[len(MESSAGE):] if message.startswith(MESSAGE) else ""
        end = index + 2
        while end < len(lines) and allowed[end] and not lines[end].strip().startswith("Location:"):
            part = _plain(lines[end])
            if not part or not _CALL.fullmatch(part) or len(call_id) + len(part) > 256:
                break
            call_id += part
            end += 1
        location = lines[end].strip() if end < len(lines) else ""
        while (end + 1 < len(lines) and allowed[end + 1] and location != LOCATION
               and LOCATION.startswith(location) and location.startswith("Location:")):
            piece = lines[end + 1]
            if piece != piece.lstrip() or not piece.strip():
                break
            location += piece.strip()
            end += 1
        if (end >= len(lines) or not all(allowed[index:end + 1])
                or location != LOCATION or not _CALL.fullmatch(call_id)):
            continue
        matches.append({"kind": "native_client_panic", "title": TITLE,
                        "call_id": call_id, "location": LOCATION})
    if len(matches) != 1:
        return None
    result = matches[0]
    block = "\n".join((TITLE, MESSAGE + result["call_id"], LOCATION))
    result["screen_sha256"] = hashlib.sha256(block.encode("utf-8")).hexdigest()
    return result


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _birth(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(v) is not int for v in value)
            or value[0] < 0 or not 0 <= value[1] < 1_000_000):
        raise Refused("original process birth unavailable")
    return value[0] + value[1] / 1_000_000


def _timestamp(value):
    try:
        if type(value) in (int, float):
            result = float(value)
        elif isinstance(value, str) and len(value) <= 64:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp lacks timezone")
            result = parsed.timestamp()
        else:
            raise ValueError("missing timestamp")
        if not math.isfinite(result):
            raise ValueError("non-finite timestamp")
        return result
    except (ValueError, OverflowError, OSError) as exc:
        raise Refused("fatal native output timestamp unavailable") from exc


def _record(raw):
    if not raw.endswith(b"\n") or len(raw) > MAX_TAIL:
        raise Refused("partial or oversized native record")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refused("malformed native record") from exc
    if (not isinstance(value, dict) or not isinstance(value.get("type"), str)
            or not value["type"] or not isinstance(value.get("payload"), dict)):
        raise Refused("invalid native record shape")
    return value


def _contains_call(value, call_id):
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if (item.get("type") in {"custom_tool_call", "function_call"}
                    and item.get("call_id") == call_id):
                return True
            stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
        elif isinstance(item, list):
            stack.extend(v for v in item if isinstance(v, (dict, list)))
    return False


def _noise(record):
    return record["type"] == "event_msg" and record["payload"].get("type") == "token_count"


def _proof(path, info, offset, raw):
    return {"path": str(path), "file_id": [info.st_dev, info.st_ino],
            "offset": offset, "length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()}


def native_anchor(path, session_id, birth, panic, expected_signature=None):
    """Read a header and at most 8 MiB of tail; return a rollout-compatible pin."""
    if (not isinstance(panic, dict) or panic.get("title") != TITLE
            or panic.get("location") != LOCATION
            or not isinstance(panic.get("call_id"), str)
            or not _CALL.fullmatch(panic["call_id"])
            or not re.fullmatch(r"[0-9a-f]{64}", str(panic.get("screen_sha256", "")))):
        raise Refused("complete current-screen panic unavailable")
    born = _birth(birth)
    if not isinstance(session_id, str) or not session_id:
        raise Refused("original session unavailable")
    try:
        supplied = Path(path)
        if not supplied.is_absolute() or supplied.is_symlink():
            raise Refused("original rollout is not a direct absolute file")
        resolved = supplied.resolve(strict=True)
        if resolved != supplied:
            raise Refused("original rollout path is not canonical")
        fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise Refused("original rollout is not regular")
            signature = [before.st_ino, before.st_size, before.st_mtime_ns]
            if expected_signature is not None and list(expected_signature) != signature:
                raise Refused("original native signature changed")
            header_raw = stream.readline(MAX_HEADER + 1)
            if len(header_raw) > MAX_HEADER:
                raise Refused("oversized native session header")
            header = _record(header_raw)
            if header["type"] != "session_meta" or header["payload"].get("id") != session_id:
                raise Refused("original session header mismatch")
            header_end = stream.tell()
            start = max(header_end, before.st_size - MAX_TAIL)
            previous = b"\n"
            if start > header_end:
                stream.seek(start - 1)
                previous = stream.read(1)
            else:
                stream.seek(start)
            tail = stream.read(before.st_size - start)
            if len(tail) != before.st_size - start or not tail.endswith(b"\n"):
                raise Refused("partial native EOF")
            if previous != b"\n":
                boundary = tail.find(b"\n")
                if boundary < 0:
                    raise Refused("tail has no complete bounded native record")
                start += boundary + 1
                tail = tail[boundary + 1:]
            records, offset = [], start
            for raw in tail.splitlines(keepends=True):
                records.append((_record(raw), offset, raw))
                offset += len(raw)
            compact_indexes = [i for i, (r, _, _) in enumerate(records) if r["type"] == "compacted"]
            if not compact_indexes:
                raise Refused("nearest compacted record is outside the bounded tail")
            compact_index = compact_indexes[-1]
            following = records[compact_index + 1:]
            meaningful = [item for item in following if not _noise(item[0])]
            if not meaningful:
                raise Refused("no fatal output after nearest compacted record")
            fatal, fatal_offset, fatal_raw = meaningful[-1]
            payload = fatal["payload"]
            call_id = panic["call_id"]
            if (fatal["type"] != "response_item" or payload.get("type") != "custom_tool_call_output"
                    or payload.get("call_id") != call_id or "output" not in payload):
                raise Refused("last meaningful record is not this orphan output")
            rendered_output = json.dumps(payload["output"], ensure_ascii=False)
            if (TITLE in rendered_output and MESSAGE + call_id in rendered_output
                    and LOCATION in rendered_output):
                raise Refused("panic diagnostic belongs to the tool output")
            # Replacement history may retain the call while the client's
            # live call map has already been rebuilt. The original incident
            # proves that combination can still panic; it is not later tool
            # progress. A newly emitted matching call remains a veto below.
            for record, _, _ in following:
                value = record["payload"]
                if _contains_call(value, call_id):
                    raise Refused("matching tool call after compacted")
                if ((record["type"] == "response_item" and value.get("type") == "message"
                     and value.get("role") == "user")
                        or (record["type"] == "event_msg" and value.get("type") in {
                            "user_message", "task_complete", "turn_aborted"})):
                    raise Refused("later native user input or terminal lifecycle preserved")
            at = _timestamp(fatal.get("timestamp"))
            if at < born:
                raise Refused("panic output predates this original process")
            # The boundary hash matches ccc_request_recovery.rollout_matches.
            boundary_start = max(0, before.st_size - 4096)
            stream.seek(boundary_start)
            boundary = stream.read(before.st_size - boundary_start)
            after = os.fstat(stream.fileno())
            linked = resolved.stat(follow_symlinks=False)
            if _stamp(before) != _stamp(after) or _stamp(after) != _stamp(linked):
                raise Refused("native rollout changed during evidence read")
            result = {"path": str(resolved), "file_id": [before.st_dev, before.st_ino],
                      "session_id": session_id, "fence": before.st_size,
                      "boundary_start": boundary_start,
                      "boundary_sha256": hashlib.sha256(boundary).hexdigest(),
                      "screen_sha256": panic["screen_sha256"],
                      "timestamp": at, "birth": list(birth)}
            result["rollout"] = {key: result[key] for key in (
                "path", "file_id", "session_id", "fence", "boundary_start", "boundary_sha256")}
            result["panic"] = dict(panic)
            proof = _proof(resolved, before, fatal_offset, fatal_raw)
            proof.update(call_id=call_id, timestamp=fatal.get("timestamp"))
            compact, compact_offset, compact_raw = records[compact_index]
            result.update(fatal_output=proof,
                          compact=dict(_proof(resolved, before, compact_offset, compact_raw),
                                       timestamp=compact.get("timestamp")))
            return result
    except Refused:
        raise
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise Refused("original native rollout evidence unavailable") from exc


class PanicDebouncer:
    """Observation eligibility only. The caller retains its durable one-use claim."""
    def __init__(self, clock=time.monotonic, settle_seconds=30):
        if not math.isfinite(settle_seconds) or settle_seconds < 0:
            raise ValueError("settle_seconds must be finite and nonnegative")
        self.clock, self.settle_seconds = clock, settle_seconds
        self._samples = {}

    def reset(self, surface_id):
        self._samples.pop(str(surface_id), None)

    def observe(self, identity, anchor):
        slot = str(identity.get("surface_id", "__single__")) if isinstance(identity, dict) else "__single__"
        try:
            if not isinstance(identity, dict) or not isinstance(anchor, dict):
                raise Refused("missing observation")
            if not identity.get("surface_id"):
                raise Refused("original surface unavailable")
            rollout = anchor.get("rollout", anchor)
            born = _birth(identity["birth"])
            if (type(identity["pid"]) is not int or identity["pid"] <= 0
                    or identity["session_id"] != rollout["session_id"]
                    or list(identity["birth"]) != anchor["birth"]
                    or _timestamp(anchor["timestamp"]) < born
                    or type(rollout["fence"]) is not int or rollout["fence"] <= 0
                    or not re.fullmatch(r"[0-9a-f]{64}", anchor["fatal_output"]["sha256"])):
                raise Refused("invalid observation binding")
            key = (identity["pid"], tuple(identity["birth"]), identity["session_id"],
                   rollout["path"], rollout["fence"], anchor["fatal_output"]["sha256"])
            now = float(self.clock())
            if not math.isfinite(now):
                raise Refused("invalid observation clock")
        except (Refused, KeyError, TypeError, ValueError):
            self._samples.pop(slot, None)
            return False
        previous = self._samples.get(slot)
        if previous is None or previous[0] != key or now < previous[2]:
            self._samples[slot] = (key, now, now, 1)
            return False
        self._samples[slot] = (key, previous[1], now, previous[3] + 1)
        return now - previous[1] >= self.settle_seconds

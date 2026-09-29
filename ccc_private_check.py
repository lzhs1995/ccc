"""Keep a new b check's retry text only while its original task is provable.

The origin is a lookup hint, never input authorization. Native identity,
current failed turn, the complete check history and our durable sends must
agree. A user task, successful response, missing proof or an N marker opts out.
"""
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid

from ccc_codex_queue import epoch

POLICY = "fixed-check-v1"
HISTORY_BYTES = 8 * 1024 * 1024


def _generation(path):
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _read(path, limit):
    before = _generation(path)
    if before[2] > limit:
        raise ValueError("private check evidence exceeds read limit")
    data = path.read_bytes()
    if len(data) != before[2] or _generation(path) != before:
        raise ValueError("private check evidence changed while reading")
    return data, before


def _json(path, limit=1024 * 1024, *, generations=None):
    data, generation = _read(path, limit)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("private check evidence is not an object")
    if generations is not None:
        generations[path] = generation
    return value


def _unchanged(generations):
    for path, expected in generations.items():
        try:
            actual = _generation(path)
        except FileNotFoundError:
            actual = None
        if actual != expected:
            return False
    return True


def _directory(config_path, surface_id):
    uuid.UUID(surface_id)
    return Path(config_path).parent / "private-checks" / surface_id


def _identity(config_path, job, slot):
    from ccc_workspace_batch import EMPTY_CWD_POLICY, PROMPT, startup_mode
    confirmation = slot.get("confirmation", {})
    if (job.get("check_retry_policy") != POLICY or job.get("cwd_policy") != EMPTY_CWD_POLICY
            or job.get("initial_prompt") != PROMPT or startup_mode(job, config_path) != "private_check"
            or any(str(key).startswith("access_") for key in job)
            or any(str(key).startswith("access_") for key in slot)
            or slot.get("phase") != "confirmed" or confirmation.get("confirmed") is not True
            or confirmation.get("started") is not True or confirmation.get("prompt") is not True
            or confirmation.get("blocked")
            or not confirmation.get("task_id") or not confirmation.get("task_at")
            or not slot.get("launch_id") or not slot.get("transcript")
            or type(slot.get("pid")) is not int or slot["pid"] <= 0
            or type(slot.get("process_start")) not in {int, float} or slot["process_start"] <= 0
            or not slot["process_start"] < float("inf")
            or type(slot.get("index")) is not int or not 0 <= slot["index"] < 50
            or not isinstance(confirmation.get("identity"), list) or len(confirmation["identity"]) != 2
            or confirmation.get("session_id") != slot.get("session_id")):
        return None
    for value in (job["id"], job["workspace_id"], slot["surface_id"], slot["session_id"], slot["launch_id"]):
        uuid.UUID(value)
    return {"version": 1, "job_id": job["id"], "workspace_id": job["workspace_id"],
            "surface_id": slot["surface_id"], "index": slot["index"], "launch_id": slot["launch_id"],
            "session_id": slot["session_id"], "pid": slot["pid"], "process_start": slot["process_start"],
            "transcript": str(Path(slot["transcript"]).resolve()), "identity": confirmation["identity"],
            "first_turn_id": confirmation["task_id"], "first_turn_at": epoch(confirmation["task_at"])}


def record_origin(config_path, job, slot):
    """An optional immutable lookup, published only after first-task proof.

    Failure to publish cannot withhold a proven startup hold. Old b sessions
    without an explicit policy keep their original continuation behavior.
    """
    try:
        origin = _identity(config_path, job, slot)
        if origin is None:
            return False
        from cmux_codex_watch import ensure_app_dir
        directory = _directory(config_path, slot["surface_id"])
        ensure_app_dir(directory)
        path = directory / "origin.json"
        data = (json.dumps(origin, sort_keys=True) + "\n").encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _json(path, 16384) == origin
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return False


def _turns(data, session_id, message):
    """Fail closed on other input/output, an incomplete turn or an aborted task."""
    from ccc_workspace_batch import _startup_context
    if not data.endswith(b"\n"):
        return None
    lines = data.splitlines()
    meta = json.loads(lines[0])
    if meta.get("type") != "session_meta" or meta.get("payload", {}).get("id") != session_id:
        return None
    turns, current, pending = [], None, {}
    for line in lines[1:]:
        event = json.loads(line)
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
            return None
        payload = event["payload"]
        kind = payload.get("type") if event.get("type") == "event_msg" else ""
        if event.get("type") == "session_meta" or kind in {"turn_aborted", "task_aborted"} or kind.startswith("goal_"):
            return None
        if event.get("type") == "response_item":
            if payload.get("role") == "assistant" or payload.get("type") in {
                "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output",
            }:
                return None
            if payload.get("role") == "user":
                content = payload.get("content")
                if not isinstance(content, list) or not content or any(
                        not isinstance(p, dict) or p.get("type") != "input_text"
                        or not isinstance(p.get("text"), str) for p in content):
                    return None
                text = "\n".join(p["text"] for p in content)
                if _startup_context(text):
                    continue
                if text != message:
                    return None
                destination = current if current is not None else pending
                destination["response_count"] = destination.get("response_count", 0) + 1
                if destination["response_count"] > 1:
                    return None
        if kind == "task_started":
            if current is not None or not payload.get("turn_id") or len(turns) >= 2048:
                return None
            current = {**pending, "id": payload["turn_id"], "started_at": epoch(event["timestamp"])}
            pending = {}
        elif kind == "user_message":
            if (payload.get("message") != message
                    or any(payload.get(key) for key in ("images", "local_images", "text_elements"))):
                return None
            destination = current if current is not None else pending
            destination["user_count"] = destination.get("user_count", 0) + 1
            if destination["user_count"] > 1:
                return None
        elif kind == "item_completed":
            item = payload.get("item", {})
            content = item.get("content") if isinstance(item, dict) else None
            if (current is None or payload.get("turn_id") != current["id"]
                    or item.get("type") != "UserMessage" or not isinstance(content, list) or not content
                    or any(not isinstance(p, dict) or p.get("type") != "text"
                           or not isinstance(p.get("text"), str) or p.get("text_elements") for p in content)
                    or "\n".join(p["text"] for p in content) != message):
                return None
            current["item_count"] = current.get("item_count", 0) + 1
            if current["item_count"] > 1:
                return None
        elif kind in {"agent_message", "agent_reasoning"} and any(
                payload.get(key) for key in ("message", "text")):
            return None
        elif kind == "task_complete":
            if (current is None or payload.get("turn_id") != current["id"]
                    or not any(current.get(key) == 1 for key in ("user_count", "response_count", "item_count"))
                    or not isinstance(payload.get("error"), dict) or not payload["error"]
                    or payload.get("last_agent_message")):
                return None
            current["at"] = epoch(event["timestamp"])
            if current["at"] < current["started_at"]:
                return None
            turns.append(current)
            current = None
    if current is not None or pending or not turns or len({t["id"] for t in turns}) != len(turns):
        return None
    return turns


class PrivateChecks:
    def __init__(self, config_path, sessions_root):
        self.config_path, self.sessions_root = Path(config_path), Path(sessions_root).resolve()
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.documents = OrderedDict()
        self.document_bytes = 0
        self.lock = threading.Lock()

    def _document(self, path, limit, generations):
        # Cache parsed bytes, never permission. Every use checks the complete
        # file generation, and select rechecks all generations after the sends
        # ledger read. Fifty slots can share a job parse without sharing their
        # native identity, turn proof or mutable sends ledger.
        before = _generation(path)
        if before[2] > limit:
            raise ValueError("private check evidence exceeds read limit")
        with self.lock:
            cached = self.documents.get(path)
            if cached and cached[0] == before:
                self.documents.move_to_end(path)
                generations[path] = before
                return cached[1]
        data, signature = _read(path, limit)
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError("private check evidence is not an object")
        with self.lock:
            old = self.documents.pop(path, None)
            if old:
                self.document_bytes -= old[0][2]
            self.documents[path] = (signature, value)
            self.document_bytes += signature[2]
            while len(self.documents) > 4096 or self.document_bytes > 32 * 1024 * 1024:
                _, removed = self.documents.popitem(last=False)
                self.document_bytes -= removed[0][2]
        generations[path] = signature
        return value

    def _ledger_path(self, target):
        return _directory(self.config_path, target["surface_id"]) / "sends.json"

    def _ledger(self, target, *, generations=None):
        path = self._ledger_path(target)
        try:
            value = _json(path, generations=generations)
        except FileNotFoundError:
            if generations is not None:
                generations[path] = None
            return {}
        except (ValueError, TypeError) as exc:
            raise RuntimeError("private check sends unavailable") from exc
        if not all(isinstance(v, dict) for v in value.values()):
            raise ValueError("invalid private check sends")
        return value

    def _history(self, path, session_id, message):
        before = _generation(path)
        with self.lock:
            cached = self.cache.pop(path, None)
            if cached and cached[0] == before and cached[1] == session_id:
                self.cache[path] = cached
                return cached[2], before
            if cached:
                self.cache_bytes -= cached[0][2]
        data, signature = _read(path, HISTORY_BYTES)
        turns = _turns(data, session_id, message)
        with self.lock:
            previous = self.cache.pop(path, None)
            if previous:
                self.cache_bytes -= previous[0][2]
            self.cache[path] = (signature, session_id, turns)
            self.cache_bytes += signature[2]
            while len(self.cache) > 2048 or self.cache_bytes > 32 * 1024 * 1024:
                _, removed = self.cache.popitem(last=False)
                self.cache_bytes -= removed[0][2]
        return turns, signature

    def select(self, target, read_turn, *, snapshot=None):
        """Return one immutable message proof, or leave ordinary semantics alone."""
        try:
            generations = {}
            directory = _directory(self.config_path, target["surface_id"])
            origin = self._document(directory / "origin.json", 16384, generations)
            if (origin.get("workspace_id") != target["workspace_id"]
                    or origin.get("surface_id") != target["surface_id"]):
                return None
            from ccc_workspace_batch import job_path, PROMPT
            path = job_path(self.config_path, origin["job_id"])
            job = self._document(path, 2 * 1024 * 1024, generations)
            slots = [s for s in job.get("slots", []) if s.get("surface_id") == target["surface_id"]]
            if len(slots) != 1 or _identity(self.config_path, job, slots[0]) != origin:
                return None
            descriptor = path.parent / "access.json"
            if descriptor.exists():
                return None
            generations[descriptor] = None
            receipt = self._document(path.parent / f"surface-{origin['index']}.json", 16384, generations)
            if any(receipt.get(key) != origin[key] for key in ("workspace_id", "surface_id", "launch_id")):
                return None
            turn = read_turn()
            if (not isinstance(turn, dict) or turn.get("kind") != "task_complete"
                    or not isinstance(turn.get("error"), dict) or not turn["error"]
                    or any(turn.get(key) != origin[key] for key in ("pid", "process_start", "session_id"))):
                return None
            path = Path(origin["transcript"])
            if path.resolve() != path or not path.is_relative_to(self.sessions_root):
                return None
            turns, generation = self._history(path, origin["session_id"], PROMPT)
            if (not turns or origin["identity"] != list(generation[:2])
                    or turn.get("signature") != [generation[1], generation[2], generation[3]]
                    or _generation(path) != generation
                    or (turns[0]["id"], turns[0]["started_at"]) != (origin["first_turn_id"], origin["first_turn_at"])
                    or (turns[-1]["id"], turns[-1]["at"]) != (turn.get("turn_id"), turn.get("at"))):
                return None
            generations[path] = generation
            sends = self._ledger(target, generations=generations)
            # Index this freshly read ledger only; never cache authorization.
            # Preserve all attempts for a turn so duplicates still veto input.
            sends_by_turn = {}
            for record in sends.values():
                failed_id = record.get("failed_turn_id")
                if isinstance(failed_id, str):
                    sends_by_turn.setdefault(failed_id, []).append(record)
            for previous, following in zip(turns, turns[1:]):
                matched = [r for r in sends_by_turn.get(previous["id"], ()) if r.get("origin") == origin
                           and r.get("failed_turn_id") == previous["id"] and r.get("failed_at") == previous["at"]
                           and r.get("message") == PROMPT and r.get("phase") == "accepted"
                           and type(r.get("io_started_at")) in {int, float}
                           and previous["at"] <= r["io_started_at"] <= following["started_at"] + .001]
                if len(matched) != 1:
                    return None
            # The sends ledger can block on I/O after the history was read.
            # A user message/goal may already be logged before task_started;
            # every source, including absence of an N descriptor, must still
            # describe the same snapshot when this proof is returned.
            if not _unchanged(generations):
                return None
            if snapshot is not None:
                snapshot.clear()
                snapshot.update(generations)
            return {"origin": origin, "failed_turn_id": turn["turn_id"], "failed_at": turn["at"], "message": PROMPT,
                    "signature": turn["signature"], "transcript_generation": list(generation),
                    "chain_sha256": hashlib.sha256(json.dumps(turns, sort_keys=True).encode()).hexdigest()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            return None

    def valid(self, proof, target, read_turn, *, snapshot=None):
        return bool(proof) and self.select(target, read_turn, snapshot=snapshot) == proof

    @staticmethod
    def snapshot_current(snapshot):
        try:
            return bool(snapshot) and _unchanged(snapshot)
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def matches_turn(proof, turn):
        try:
            return (isinstance(turn, dict) and turn.get("kind") == "task_complete"
                    and all(turn.get(key) == proof["origin"].get(key) for key in ("pid", "process_start", "session_id"))
                    and turn.get("turn_id") == proof.get("failed_turn_id") and turn.get("at") == proof.get("failed_at")
                    and turn.get("signature") == proof["signature"]
                    and list(_generation(Path(proof["origin"]["transcript"]))) == proof["transcript_generation"])
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def accepted(self, proof, attempt_id):
        try:
            record = self._ledger(proof["origin"]).get(attempt_id, {})
            return (record.get("phase") == "accepted"
                    and all(record.get(key) == value for key, value in proof.items()))
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return False

    def reserve(self, proof, attempt_id):
        target = proof["origin"]
        ledger = self._ledger(target)
        if any(r.get("failed_turn_id") == proof["failed_turn_id"]
               and r.get("phase") not in {"not_sent", "failed"} for r in ledger.values()):
            raise RuntimeError("原短检查发送已有回执或尚待核验；不重复投递")
        ledger[attempt_id] = {**proof, "phase": "intent", "at": time.time()}
        from cmux_codex_watch import atomic_write_json
        atomic_write_json(self._ledger_path(target), ledger)

    def finish(self, proof, attempt_id, phase, *, io_started_at=0):
        target = proof["origin"]
        ledger = self._ledger(target)
        original = ledger.get(attempt_id)
        if not original or original.get("origin") != target or original.get("phase") != "intent":
            raise RuntimeError("private check send receipt changed")
        ledger[attempt_id] = {**original, "phase": phase, "io_started_at": io_started_at, "finished_at": time.time()}
        from cmux_codex_watch import atomic_write_json
        atomic_write_json(self._ledger_path(target), ledger)

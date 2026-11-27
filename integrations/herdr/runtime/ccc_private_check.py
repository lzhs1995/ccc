"""Keep a new b check's retry text only while its original task is provable.

The origin is a lookup hint, never input authorization. Native identity,
current failed turn, the complete check history and our durable sends must
agree. A user task, successful response, missing proof or an N marker opts out.
"""
from collections import OrderedDict
import hashlib
import json
import math
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


def _completed_check(data, origin, message):
    """Read actual OK completion of the original check, never an idle label.

    All non-context user turns must still be this exact short check. An OK in
    a tool output, reasoning, quoted user text or a different later task is not
    a successful check. A new unfinished turn invalidates the displayed proof.
    """
    from ccc_workspace_batch import _startup_context
    if not data.endswith(b"\n"):
        return None
    lines = data.splitlines()
    meta = json.loads(lines[0])
    if meta.get("type") != "session_meta" or meta.get("payload", {}).get("id") != origin["session_id"]:
        return None
    current, pending, turns, seen = None, {}, [], set()
    for line in lines[1:]:
        event = json.loads(line)
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return None
        kind = payload.get("type", "") if event.get("type") == "event_msg" else ""
        if event.get("type") == "session_meta" or kind in {"turn_aborted", "task_aborted"} or kind.startswith("goal_"):
            return None
        if event.get("type") == "response_item":
            item_type, role = payload.get("type"), payload.get("role")
            if item_type in {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}:
                return None
            if role in {"user", "assistant"}:
                content = payload.get("content")
                expected = "input_text" if role == "user" else "output_text"
                if not isinstance(content, list) or not content or any(
                        not isinstance(p, dict) or p.get("type") != expected
                        or not isinstance(p.get("text"), str) for p in content):
                    return None
                text = "\n".join(p["text"] for p in content)
                if role == "user":
                    if _startup_context(text):
                        continue
                    if text != message:
                        return None
                    destination = current if current is not None else pending
                    destination["response_count"] = destination.get("response_count", 0) + 1
                    if destination["response_count"] != 1:
                        return None
                elif current is None or text.strip() != "OK" or payload.get("channel") not in (None, "final"):
                    return None
        if kind == "task_started":
            turn_id = payload.get("turn_id")
            if current is not None or not turn_id or turn_id in seen or len(turns) >= 2048:
                return None
            seen.add(turn_id)
            current = {**pending, "turn_id": turn_id, "started_at": epoch(event["timestamp"])}
            pending = {}
        elif kind == "user_message":
            if payload.get("message") != message or any(payload.get(key) for key in ("images", "local_images", "text_elements")):
                return None
            destination = current if current is not None else pending
            destination["user_count"] = destination.get("user_count", 0) + 1
            if destination["user_count"] != 1:
                return None
        elif kind == "item_completed":
            item = payload.get("item", {})
            content = item.get("content") if isinstance(item, dict) else None
            if (current is None or payload.get("turn_id") != current["turn_id"]
                    or payload.get("thread_id", origin["session_id"]) != origin["session_id"]):
                return None
            if isinstance(item, dict) and item.get("type") == "Reasoning":
                continue
            if isinstance(item, dict) and item.get("type") == "AgentMessage":
                if (item.get("phase") != "final_answer" or not isinstance(content, list)
                        or len(content) != 1 or not isinstance(content[0], dict)
                        or content[0].get("type") != "Text" or content[0].get("text") != "OK"):
                    return None
                continue
            if (not isinstance(item, dict)
                    or item.get("type") != "UserMessage" or not isinstance(content, list) or not content
                    or any(not isinstance(p, dict) or p.get("type") != "text"
                           or not isinstance(p.get("text"), str) or p.get("text_elements") for p in content)
                    or "\n".join(p["text"] for p in content) != message):
                return None
            current["item_count"] = current.get("item_count", 0) + 1
            if current["item_count"] != 1:
                return None
        elif kind == "agent_message" and payload.get("message"):
            if current is None or str(payload["message"]).strip() != "OK":
                return None
        elif kind == "task_complete":
            if (current is None or payload.get("turn_id") != current["turn_id"]
                    or not any(current.get(key) == 1 for key in ("user_count", "response_count", "item_count"))):
                return None
            at = epoch(event["timestamp"])
            if at < current["started_at"]:
                return None
            ok = not payload.get("error") and payload.get("last_agent_message", "").strip() == "OK"
            if not ok and (not isinstance(payload.get("error"), dict) or not payload["error"]
                           or payload.get("last_agent_message")):
                return None
            turns.append({**current, "at": at, "ok": ok})
            current = None
    if (current is not None or pending or not turns or not turns[-1]["ok"]
            or any(turn["ok"] for turn in turns[:-1])
            or (turns[0]["turn_id"], turns[0]["started_at"]) != (origin["first_turn_id"], origin["first_turn_at"])):
        return None
    return {"turn_id": turns[-1]["turn_id"], "completed_at": turns[-1]["at"], "message": "OK"}


class PrivateChecks:
    def __init__(self, config_path, sessions_root):
        self.config_path, self.sessions_root = Path(config_path), Path(sessions_root).resolve()
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.documents = OrderedDict()
        self.document_bytes = 0
        self.completions = OrderedDict()
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

    def completed(self, target, session_id):
        """Read-only UI evidence bound to a confirmed batch slot and session."""
        try:
            generations = {}
            directory = _directory(self.config_path, target["surface_id"])
            origin = self._document(directory / "origin.json", 16384, generations)
            if any(origin.get(key) != target.get(key) for key in ("workspace_id", "surface_id")) or origin.get("session_id") != session_id:
                return None
            from ccc_workspace_batch import job_path, PROMPT
            job = self._document(job_path(self.config_path, origin["job_id"]), 2 * 1024 * 1024, generations)
            slots = [slot for slot in job.get("slots", []) if slot.get("surface_id") == target["surface_id"]]
            if len(slots) != 1 or _identity(self.config_path, job, slots[0]) != origin:
                return None
            receipt = self._document(job_path(self.config_path, origin["job_id"]).parent / f"surface-{origin['index']}.json", 16384, generations)
            if any(receipt.get(key) != origin[key] for key in ("workspace_id", "surface_id", "launch_id")):
                return None
            path = Path(origin["transcript"])
            if path.resolve() != path or not path.is_relative_to(self.sessions_root):
                return None
            signature = _generation(path)
            if origin["identity"] != list(signature[:2]) or signature[2] > HISTORY_BYTES:
                return None
            with self.lock:
                cached = self.completions.get(path)
            if cached and cached[:2] == (signature, origin):
                completed = cached[2]
            else:
                data, signature = _read(path, HISTORY_BYTES)
                completed = _completed_check(data, origin, PROMPT)
                with self.lock:
                    self.completions[path] = (signature, origin, completed)
                    self.completions.move_to_end(path)
                    while len(self.completions) > 2048:
                        self.completions.popitem(last=False)
            generations[path] = signature
            if completed and _unchanged(generations):
                return {"origin": origin, **completed, "transcript_generation": list(signature)}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, IndexError):
            pass
        return None

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
            # A late, complete native successor with this exact batch prompt
            # permits its NEW failed turn to be considered. It neither attributes
            # that message to an uncertain sender nor settles/replays the old
            # attempt: the original intent/unknown receipt remains unchanged.
            for previous, following in zip(turns, turns[1:]):
                matched = [r for r in sends_by_turn.get(previous["id"], ()) if r.get("origin") == origin
                           and r.get("failed_turn_id") == previous["id"] and r.get("failed_at") == previous["at"]
                           and r.get("message") == PROMPT
                           and ((r.get("phase") == "accepted"
                                 and type(r.get("io_started_at")) in {int, float}
                                 and previous["at"] <= r["io_started_at"] <= following["started_at"] + .001)
                                or (r.get("phase") in {"intent", "unknown"}
                                    and type(r.get("io_intent_at")) in {int, float}
                                    and previous["at"] <= r["io_intent_at"] <= following["started_at"] + .001))]
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

    @staticmethod
    def _receipt_matches(ledger, proof, attempt_id, phase):
        record = ledger.get(attempt_id, {})
        return (record.get("phase") == phase
                and all(record.get(key) == value for key, value in proof.items())
                and (phase != "not_sent" or (type(record.get("io_started_at")) in {int, float}
                                            and record["io_started_at"] == 0))
                and not any(key != attempt_id and value.get("failed_turn_id") == proof["failed_turn_id"]
                            for key, value in ledger.items()))

    def receipt_matches(self, proof, attempt_id, phase):
        try:
            return self._receipt_matches(self._ledger(proof["origin"]), proof, attempt_id, phase)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return False

    @staticmethod
    def _zero_input_delivery(proof, attempt_id, read_delivery):
        """Bind a freshly read durable zero-input head to this original attempt.

        The caller holds the original surface/input lock. A timestamp missing
        from sends.json (or an unchanged rollout) is not proof of zero input:
        a process can die after host I/O and before publishing its receipt.
        """
        if not callable(read_delivery):
            raise RuntimeError("private check requires original durable zero-input proof")
        from ccc_delivery import DeliveryStore
        # Freeze the callback result before validating it. It must be read from
        # the delivery store, never reconstructed from mutable runtime state.
        data = json.dumps(read_delivery(), sort_keys=True, allow_nan=False)
        value = json.loads(data)
        surface_id, saved = DeliveryStore.validate(value)
        origin = proof["origin"]
        zero = saved.get("codex_input_not_sent", {})
        identity = zero.get("identity", {})
        if (surface_id != origin["surface_id"]
                or saved.get("send_attempt_id") != attempt_id
                or saved.get("delivery_status") != "retryable"
                or saved.get("codex_input_phase") != "input_not_sent"
                or saved.get("codex_private_check") != proof
                or saved.get("codex_goal_resume", False)
                or zero.get("attempt_id") != attempt_id
                or zero.get("private_check") != proof
                or not isinstance(identity, dict)
                or any(identity.get(key) != origin.get(key) for key in (
                    "workspace_id", "surface_id", "session_id", "pid", "process_start"))
                or identity.get("turn_id") != proof["failed_turn_id"]
                or identity.get("at") != proof["failed_at"]
                or not isinstance(identity.get("model_provider"), str)
                or not identity["model_provider"].strip()
                or type(identity.get("pid")) is not int or identity["pid"] <= 0
                or any(type(identity.get(key)) not in (int, float)
                       or not math.isfinite(identity[key]) or identity[key] <= 0
                       for key in ("process_start", "at"))):
            raise RuntimeError("private check zero-input proof belongs to another attempt or identity")
        turn_key = f"{identity['session_id']}:{identity['turn_id']}:{identity['at']}"
        if zero.get("turn_key") != turn_key or saved.get("codex_sent_turn_key") != turn_key:
            raise RuntimeError("private check zero-input proof belongs to another turn")
        return {"delivery_revision": saved["delivery_revision"],
                "delivery_sha256": hashlib.sha256(data.encode()).hexdigest()}

    def reserve(self, proof, attempt_id, *, reuse=False, read_delivery=None):
        target = proof["origin"]
        ledger = self._ledger(target)
        # A private retry needs the *same* attempt and the matching durable
        # zero-input receipt. A missing delivery proof, an old failed row or a
        # second attempt must never turn a partial two-store write into input.
        if (reuse and not self._receipt_matches(ledger, proof, attempt_id, "not_sent")) or (
                not reuse and (attempt_id in ledger or any(
                    r.get("failed_turn_id") == proof["failed_turn_id"] for r in ledger.values()))):
            raise RuntimeError("原短检查发送已有回执或尚待核验；不重复投递")
        if reuse:
            self._zero_input_delivery(proof, attempt_id, read_delivery)
        ledger[attempt_id] = {**proof, "phase": "intent", "at": time.time()}
        from cmux_codex_watch import atomic_write_json
        atomic_write_json(self._ledger_path(target), ledger)

    def begin_io(self, proof, attempt_id, at):
        """Persist the original input boundary before calling any transport.

        This is intent, not a claim that input arrived. Missing/failed ACKs keep
        the same attempt; only a separate durable zero-input proof can rearm it.
        """
        ledger = self._ledger(proof["origin"])
        if (not self._receipt_matches(ledger, proof, attempt_id, "intent")
                or type(at) not in (int, float) or not math.isfinite(at) or at <= 0
                or ledger[attempt_id].get("io_intent_at")):
            raise RuntimeError("private check input intent changed")
        ledger[attempt_id] = {**ledger[attempt_id], "io_intent_at": at}
        from cmux_codex_watch import atomic_write_json
        atomic_write_json(self._ledger_path(proof["origin"]), ledger)

    def rollback_pre_io(self, proof, attempt_id, *, read_delivery=None):
        """Retain the attempt; settle only explicit durable zero-input evidence.

        Missing evidence leaves the exact intent untouched. This also permits
        crash recovery between the delivery proof commit and private finish,
        without deleting or replacing the original attempt.
        """
        try:
            self.finish(proof, attempt_id, "not_sent", read_delivery=read_delivery)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return False
        return True

    def finish(self, proof, attempt_id, phase, *, io_started_at=0, read_delivery=None):
        target = proof["origin"]
        ledger = self._ledger(target)
        original = ledger.get(attempt_id)
        if not self._receipt_matches(ledger, proof, attempt_id, "intent"):
            raise RuntimeError("private check send receipt changed")
        zero = self._zero_input_delivery(proof, attempt_id, read_delivery) if phase == "not_sent" else None
        if zero is not None and (type(io_started_at) not in (int, float) or io_started_at != 0):
            raise RuntimeError("private zero-input receipt cannot contain an I/O start")
        ledger[attempt_id] = {**original, "phase": phase, "io_started_at": io_started_at, "finished_at": time.time()}
        if zero is not None:
            ledger[attempt_id]["zero_input_delivery"] = zero
        from cmux_codex_watch import atomic_write_json
        atomic_write_json(self._ledger_path(target), ledger)

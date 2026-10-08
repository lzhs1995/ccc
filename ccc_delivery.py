"""Small per-surface durable input records, independent of fleet snapshots."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import Future
from contextlib import closing


FIELDS = (
    "delivery_revision", "delivery_status", "state", "send_attempt_id",
    "send_started_at", "send_io_started_at", "send_completed_at", "send_attempt_evidence",
    "last_send_error", "last_send_at", "send_count", "awaiting", "awaiting_suppressed",
    "sent_fingerprint", "sent_screen_signature", "codex_sent_turn_key",
    "codex_observed_turn_key", "codex_goal_resume", "codex_goal_proof", "codex_private_check", "codex_input_phase", "codex_input_not_sent",
    "episode_id", "episode_started_at", "error_type",
)


class DeliveryStore:
    def __init__(self, root, *, daemon_threads=True):
        self.root = Path(root)
        self.daemon_threads = daemon_threads
        self.faults = set()
        self.database_error = None
        self._pending = queue.Queue()
        self._writer = None
        self._lifecycle = threading.Lock()
        self._phase = "new"
        self._ready = None

    @staticmethod
    def validate(value):
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("invalid durable delivery version")
        sid, saved = value.get("surface_id"), value.get("runtime")
        if (not isinstance(sid, str) or not sid or not isinstance(saved, dict)
                or type(saved.get("delivery_revision")) is not int
                or saved["delivery_revision"] < 1
                or not isinstance(saved.get("send_attempt_id"), str) or not saved["send_attempt_id"]):
            raise ValueError("invalid durable delivery identity")
        for key in ("send_started_at", "send_io_started_at", "send_completed_at", "last_send_at", "episode_started_at"):
            if key in saved and (type(saved[key]) not in (float, int)
                    or not math.isfinite(saved[key]) or saved[key] < 0):
                raise ValueError("invalid durable delivery clock: " + key)
        for key in ("send_count", "awaiting_suppressed"):
            if key in saved and (type(saved[key]) is not int or saved[key] < 0):
                raise ValueError("invalid durable delivery count: " + key)
        for key in ("awaiting", "codex_goal_resume"):
            if key in saved and type(saved[key]) is not bool:
                raise ValueError("invalid durable delivery flag: " + key)
        for key in ("state", "delivery_status", "last_send_error", "codex_sent_turn_key", "codex_observed_turn_key"):
            if key in saved and not isinstance(saved[key], str):
                raise ValueError("invalid durable delivery text: " + key)
        for key in ("send_attempt_evidence", "sent_fingerprint", "sent_screen_signature", "episode_id", "error_type"):
            if key in saved and saved[key] is not None and not isinstance(saved[key], str):
                raise ValueError("invalid durable delivery evidence: " + key)
        if "codex_private_check" in saved and not isinstance(saved["codex_private_check"], dict):
            raise ValueError("invalid durable delivery private check")
        if "codex_goal_proof" in saved and not isinstance(saved["codex_goal_proof"], dict):
            raise ValueError("invalid durable delivery goal proof")
        if "codex_input_not_sent" in saved and not isinstance(saved["codex_input_not_sent"], dict):
            raise ValueError("invalid durable zero-write proof")
        if saved.get("codex_input_phase") == "input_not_sent":
            proof = saved.get("codex_input_not_sent", {})
            if (saved.get("delivery_status") != "retryable"
                    or proof.get("attempt_id") != saved["send_attempt_id"]
                    or not saved.get("codex_sent_turn_key")
                    or proof.get("turn_key") != saved["codex_sent_turn_key"]
                    or not isinstance(proof.get("identity"), dict)
                    or proof["identity"].get("surface_id") != sid):
                raise ValueError("invalid durable zero-write binding")
        if saved.get("delivery_status", "") not in {"", "sending", "accepted", "confirmed", "failed", "unknown", "cancelled", "retryable"}:
            raise ValueError("invalid durable delivery status")
        if saved.get("codex_input_phase", "") not in {"", "text_pending", "enter_pending", "enter_acknowledged", "text_retained",
                                                        "paste_submit_pending", "paste_submit_acknowledged", "input_not_sent"}:
            raise ValueError("invalid durable native input phase")
        return sid, saved

    @staticmethod
    def key(surface_id):
        return hashlib.sha256(str(surface_id).encode()).hexdigest()

    def restore(self, runtimes, factory):
        def apply(value, expected=None):
            sid, saved = self.validate(value)
            if expected is not None and expected != self.key(sid):
                raise ValueError("invalid durable delivery identity")
            runtime = runtimes.setdefault(sid, factory())
            if saved["delivery_revision"] > runtime.delivery_revision:
                for key in FIELDS:
                    if key in saved:
                        setattr(runtime, key, saved[key])

        for path in self.root.glob("*.json"):
            try:
                value = json.loads(path.read_bytes())
                apply(value, path.stem)
            except (OSError, ValueError, TypeError, KeyError):
                # A broken row affects its own UUID only; never replay an
                # uncertain input or stall unrelated surfaces behind it.
                self.faults.add(path.stem)
        database = self.root / "delivery.sqlite3"
        if database.exists():
            try:
                with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                    for sid, revision, data in connection.execute("SELECT surface_id, revision, record FROM delivery"):
                        try:
                            value = json.loads(data)
                            actual_sid, saved = self.validate(value)
                            if actual_sid != sid or saved["delivery_revision"] != revision:
                                raise ValueError("durable row identity changed")
                            apply(value, self.key(sid))
                        except (ValueError, TypeError, KeyError):
                            self.faults.add(self.key(sid))
            except (OSError, sqlite3.Error) as exc:
                self.database_error = str(exc)

    def blocked(self, surface_id):
        return bool(self.database_error) or self.key(surface_id) in self.faults

    def start(self):
        """Group independent records into one FULL-synchronous WAL commit.

        No sender proceeds until its own row is committed. The payload is
        proportional to pending inputs, not the full fleet's UI state.
        """
        with self._lifecycle:
            if self.database_error or self._phase in {"stopping", "closed", "failed"}:
                raise RuntimeError("durable delivery database is unavailable")
            if self._phase == "new":
                self.root.mkdir(parents=True, exist_ok=True)
                self._phase, self._ready = "starting", Future()
                self._writer = threading.Thread(target=self._run, name="ccc-delivery", daemon=self.daemon_threads)
                self._writer.start()
            ready = self._ready
        ready.result()

    def _commit_batch(self, connection, batch):
        accepted = []
        with connection:
            for sid, revision, data, _ in batch:
                cursor = connection.execute("INSERT INTO delivery VALUES (?, ?, ?) ON CONFLICT(surface_id) DO UPDATE SET revision=excluded.revision,record=excluded.record WHERE excluded.revision > delivery.revision", (sid, revision, data))
                accepted.append(cursor.rowcount == 1)
        return accepted

    def _run(self):
        connection, batch = None, []
        try:
            connection = sqlite3.connect(self.root / "delivery.sqlite3", timeout=2)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE IF NOT EXISTS delivery (surface_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, record BLOB NOT NULL)")
            connection.commit()
            with self._lifecycle:
                if self._phase == "starting":
                    self._phase = "running"
            self._ready.set_result(None)
            while True:
                first = self._pending.get()
                if first is None:
                    return
                batch, end, stopping = [first], time.monotonic() + .002, False
                while len(batch) < 2048:
                    try:
                        item = self._pending.get(timeout=max(0, end - time.monotonic()))
                    except queue.Empty:
                        break
                    if item is None:
                        stopping = True
                        break
                    batch.append(item)
                try:
                    accepted = self._commit_batch(connection, batch)
                except sqlite3.Error as exc:
                    for *_, waiter in batch:
                        waiter.set_exception(RuntimeError("delivery commit failed: " + str(exc)))
                else:
                    for item, committed in zip(batch, accepted):
                        if committed:
                            item[-1].set_result(None)
                        else:
                            item[-1].set_exception(RuntimeError("durable delivery revision is stale"))
                batch = []
                if stopping:
                    return
        except Exception as exc:
            # Enqueue and terminal state are published under the same lock.
            # A sender cannot enqueue after the final drain and wait forever.
            with self._lifecycle:
                self.database_error, self._phase = str(exc), "failed"
                if not self._ready.done():
                    self._ready.set_exception(exc)
                while True:
                    try:
                        item = self._pending.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        batch.append(item)
                for *_, waiter in batch:
                    if not waiter.done():
                        waiter.set_exception(RuntimeError("delivery writer stopped: " + str(exc)))
        finally:
            with self._lifecycle:
                if self._phase != "failed":
                    self._phase = "closed"
            if connection is not None:
                connection.close()

    def close(self):
        with self._lifecycle:
            if self._phase in {"starting", "running"}:
                self._phase = "stopping"
                self._pending.put(None)
            elif self._phase == "new":
                self._phase = "closed"
            writer = self._writer
        if writer is not None:
            writer.join()

    def persist(self, surface_id, runtime):
        if self.blocked(surface_id):
            raise RuntimeError("original durable delivery record is unreadable")
        revision = runtime.delivery_revision + 1
        saved = {key: getattr(runtime, key) for key in FIELDS}
        saved["delivery_revision"] = revision
        value = {"version": 1, "surface_id": surface_id, "runtime": saved}
        self.validate(value)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        committed = None
        with self._lifecycle:
            if self._phase == "running":
                committed = Future()
                self._pending.put((surface_id, revision, data, committed))
            elif self._phase != "new":
                raise RuntimeError("durable delivery writer is not accepting records")
        if committed is not None:
            committed.result()
            runtime.delivery_revision = revision
            return
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / (self.key(surface_id) + ".json")
        fd, temporary = tempfile.mkstemp(prefix=".delivery-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            runtime.delivery_revision = revision
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

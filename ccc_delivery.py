"""Small per-surface durable input records, independent of fleet snapshots."""
from __future__ import annotations

import fcntl
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
from contextlib import closing, contextmanager


FIELDS = (
    "delivery_revision", "delivery_status", "state", "send_attempt_id",
    "send_started_at", "send_io_started_at", "send_completed_at", "send_attempt_evidence",
    "last_send_error", "last_send_at", "send_count", "awaiting", "awaiting_suppressed",
    "sent_fingerprint", "sent_screen_signature", "codex_sent_turn_key",
    "codex_observed_turn_key", "codex_goal_resume", "codex_goal_proof", "codex_private_check", "codex_input_phase", "codex_input_not_sent",
    "episode_id", "episode_started_at", "error_type",
)


class PreInputRecoveryError(RuntimeError):
    """The original live pre-input transaction cannot authorize recovery."""


class _PreInputAttempt:
    """A process-local capability; its context must end before terminal I/O."""

    def __init__(self, store, surface_id, runtime, identity):
        self.store, self.surface_id, self.runtime = store, surface_id, runtime
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.active, self.phase, self.admitted = True, 'new', False
        self.previous = None
        self.revision, self.data = store._serialize(surface_id, runtime)
        self.saved = json.loads(self.data)['runtime']
        self.identity = self._identity_json(identity) if identity else None
        self.recovery = None
        if (self.saved['delivery_status'] != 'sending'
                or self.saved['codex_input_phase'] not in {'text_pending', 'paste_submit_pending'}
                or self.saved['send_io_started_at'] != 0 or self.saved['send_completed_at'] != 0
                or self.saved['codex_input_not_sent']
                or not self.saved['codex_sent_turn_key']
                or self.saved['codex_sent_turn_key'] != self.saved['codex_observed_turn_key']):
            raise PreInputRecoveryError('pre-input context requires a fresh pending reservation')

    def __reduce__(self):
        raise TypeError('a live pre-input attempt cannot be serialized')

    def _live(self):
        if (not self.active or os.getpid() != self.pid
                or threading.get_ident() != self.thread
                or self.runtime.send_attempt_id != self.saved['send_attempt_id']
                or self.runtime.send_io_started_at != 0 or self.runtime.send_completed_at != 0):
            raise PreInputRecoveryError('original pre-input control flow is no longer active')

    def persist(self):
        self._live()
        if self.phase != 'new':
            raise PreInputRecoveryError('original pending write was already attempted')
        self.phase = 'persisting'
        try:
            revision, _ = self.store._persist_data(self.surface_id, self.revision, self.data,
                                                  pre_input=(self, 'pending'))
        except BaseException:
            self.phase = 'failed'
            raise
        self.runtime.delivery_revision = revision
        self.phase = 'committed'

    @staticmethod
    def _identity_json(identity):
        try:
            return json.dumps(identity, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            # Legacy rows may contain NaN or other identities which a new
            # reservation cannot serialize. Reject this surface only; never
            # let proof validation terminate the shared SQLite writer.
            raise PreInputRecoveryError('zero-input proof contains an invalid native identity') from exc

    def _check_proof(self, proof):
        identity = proof.get('identity') if isinstance(proof, dict) else None
        if (not isinstance(identity, dict) or not self.identity
                or self._identity_json(identity) != self.identity
                or identity.get('surface_id') != self.surface_id
                or proof.get('attempt_id') != self.saved['send_attempt_id']
                or proof.get('turn_key') != self.saved['codex_sent_turn_key']):
            raise PreInputRecoveryError('zero-input proof does not match the original pending identity')
        private = self.saved['codex_private_check']
        if proof.get('private_check', {}) != private:
            raise PreInputRecoveryError('zero-input proof does not match the original private check')
        if private and (not isinstance(private.get('origin'), dict)
                or self.saved['codex_goal_resume']
                or private.get('failed_turn_id') != identity.get('turn_id')
                or private.get('failed_at') != identity.get('at')
                or any(private['origin'].get(field) != identity.get(field)
                       for field in ('workspace_id', 'surface_id', 'session_id', 'pid', 'process_start'))):
            raise PreInputRecoveryError('zero-input private check lacks its original native binding')
        for key in ('workspace_id', 'session_id', 'turn_id', 'model_provider'):
            if not isinstance(identity.get(key), str) or not identity[key].strip():
                raise PreInputRecoveryError('zero-input proof lacks its original native identity')
        if type(identity.get('pid')) is not int or identity['pid'] <= 0:
            raise PreInputRecoveryError('zero-input proof lacks its original process')
        for key in ('at', 'process_start'):
            try:
                valid = (type(identity.get(key)) in (int, float)
                         and math.isfinite(identity[key]) and identity[key] > 0)
            except OverflowError:
                valid = False
            if not valid:
                raise PreInputRecoveryError('zero-input proof lacks its original native clock')
        key = f"{identity['session_id']}:{identity['turn_id']}:{identity['at']}"
        if self.saved['codex_goal_resume']:
            goal = self.saved['codex_goal_proof']
            if (not identity.get('goal_id') or identity['goal_id'] != goal.get('goal_id')
                    or any(identity[field] != goal.get(field) for field in ('session_id', 'turn_id', 'at'))):
                raise PreInputRecoveryError('zero-input proof does not match the original Goal')
            key = f"goal:{identity['session_id']}:{identity['goal_id']}:{identity['turn_id']}:{identity['at']}"
        if key != self.saved['codex_sent_turn_key']:
            raise PreInputRecoveryError('zero-input proof does not match the original failed turn')

    def recover_input_not_sent(self, proof, *, error='pending persistence failed; input not sent'):
        self._live()
        if self.phase != 'failed':
            raise PreInputRecoveryError('only a failed initial pending write can be recovered')
        self._check_proof(proof)
        # Compare every captured field, except diagnostics written by the
        # caller's exception branch and the possibly acknowledged revision.
        current_revision, current_data = self.store._serialize(self.surface_id, self.runtime)
        current = json.loads(current_data)['runtime']
        for key in ('delivery_revision', 'delivery_status', 'state', 'last_send_error'):
            current[key] = self.saved[key]
        if current != self.saved or current_revision not in {self.revision, self.revision + 1}:
            raise PreInputRecoveryError('original pending runtime changed before recovery')
        value = json.loads(self.data)
        value['runtime'].update(delivery_status='retryable', state='send_failed',
                                codex_input_phase='input_not_sent', codex_input_not_sent=proof,
                                last_send_error=str(error))
        self.recovery = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        self.store.validate(json.loads(self.recovery))
        self.phase = 'recovering'
        try:
            _, data = self.store._persist_data(self.surface_id, self.revision, self.recovery,
                                               pre_input=(self, 'proof'))
        finally:
            # A failed proof write grants nothing and cannot escape this live
            # context as a later runtime-based claim that input was not sent.
            self.phase = 'settled'
            self.recovery = None
        for key, value in json.loads(data)['runtime'].items():
            if key in FIELDS:
                setattr(self.runtime, key, value)

    def resolve(self, operation, previous):
        # Runs under BEGIN IMMEDIATE or the stable per-surface file lock.
        if not self.active or os.getpid() != self.pid:
            raise PreInputRecoveryError('original pre-input transaction expired')
        if operation == 'pending':
            if (previous[0] if previous else 0) != self.revision - 1:
                raise PreInputRecoveryError('durable delivery revision is stale')
            if previous:
                old = json.loads(previous[1])['runtime']
                if old['send_attempt_id'] == self.saved['send_attempt_id']:
                    if old.get('codex_input_phase') != 'input_not_sent':
                        raise PreInputRecoveryError('historical same-attempt input cannot become pre-input proof')
                    self._check_proof(old.get('codex_input_not_sent'))
            self.previous, self.admitted = previous, True
            return self.revision, self.data
        if operation != 'proof' or not self.recovery or self.phase != 'recovering':
            raise PreInputRecoveryError('original pre-input proof is unavailable')
        if previous is None and self.revision == 1 and self.previous is None:
            revision = 1
        elif self.admitted and previous == (self.revision, self.data):
            revision = self.revision + 1
        elif (self.admitted and previous and previous == self.previous
                and json.loads(previous[1])['runtime']['send_attempt_id'] == self.saved['send_attempt_id']):
            # The failed write rolled back to this same attempt's already
            # authenticated durable input_not_sent record, never unknown.
            revision = self.revision
        else:
            raise PreInputRecoveryError('durable head does not exactly match this pending attempt')
        value = json.loads(self.recovery)
        value['runtime']['delivery_revision'] = revision
        return revision, json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


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
                    or proof.get("private_check", {}) != saved.get("codex_private_check", {})
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

    @staticmethod
    def _record_bytes(data):
        # SQLite may return a legacy TEXT record instead of a BLOB. Compare
        # its exact UTF-8 bytes, never a normalized JSON representation.
        return data.encode('utf-8') if isinstance(data, str) else data

    def restore(self, runtimes, factory, *, surface_ids=None):
        # The parent restores all history. Native lanes need only the exact
        # assignments they own; never materialize every fleet row per lane.
        selected = None if surface_ids is None else tuple(dict.fromkeys(surface_ids))
        if selected == ():
            return

        def apply(value, expected=None):
            sid, saved = self.validate(value)
            if expected is not None and expected != self.key(sid):
                raise ValueError("invalid durable delivery identity")
            runtime = runtimes.setdefault(sid, factory())
            if saved["delivery_revision"] >= runtime.delivery_revision:
                defaults = factory()
                for key in FIELDS:
                    # Fleet snapshots can observe an uncommitted mutation at
                    # this revision. The durable record owns all send fields,
                    # including defaults absent from an older record format.
                    setattr(runtime, key, saved.get(key, getattr(defaults, key)))

        file_heads = {}
        paths = (self.root.glob("*.json") if selected is None else
                 (self.root / (self.key(sid) + ".json") for sid in selected))
        for path in paths:
            try:
                try:
                    data = path.read_bytes()
                except FileNotFoundError:
                    if selected is not None:
                        continue
                    raise
                value = json.loads(data)
                apply(value, path.stem)
                file_heads[value['surface_id']] = (value['runtime']['delivery_revision'], data)
            except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
                # A broken row affects its own UUID only; never replay an
                # uncertain input or stall unrelated surfaces behind it.
                self.faults.add(path.stem)
        database = self.root / "delivery.sqlite3"
        if database.exists():
            try:
                with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                    if selected is None:
                        rows = connection.execute("SELECT surface_id, revision, record FROM delivery")
                    else:
                        # Indexed per-surface reads avoid full scans and the
                        # SQLite variable limit for a large explicit selection.
                        rows = (row for sid in selected for row in connection.execute(
                            "SELECT surface_id, revision, record FROM delivery WHERE surface_id=?", (sid,)))
                    for sid, revision, data in rows:
                        try:
                            value = json.loads(data)
                            actual_sid, saved = self.validate(value)
                            if actual_sid != sid or saved["delivery_revision"] != revision:
                                raise ValueError("durable row identity changed")
                            file_head = file_heads.get(sid)
                            if (file_head and file_head[0] == revision
                                    and file_head[1] != self._record_bytes(data)):
                                raise ValueError('conflicting durable delivery records at the same revision')
                            apply(value, self.key(sid))
                        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
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

    def _migrate_file_heads(self, connection):
        """Import legacy durable heads before the writer can authorize input.

        Source files and their archives remain untouched. Each surface moves
        atomically with any displaced SQLite record; ambiguous evidence blocks
        that surface without preventing independent surfaces from starting.
        """
        for path in self.root.glob('*.json'):
            if path.stem in self.faults:
                continue
            try:
                sid, _ = self.validate(json.loads(path.read_bytes()))
                if path.stem != self.key(sid):
                    raise ValueError('invalid legacy durable delivery identity')
                # A still-active file writer is not an upgrade source. Do not
                # stall every surface behind its lock or import a moving head.
                with self._file_lock(sid, blocking=False), connection:
                    data = path.read_bytes()
                    actual_sid, saved = self.validate(json.loads(data))
                    if actual_sid != sid:
                        raise ValueError('legacy durable delivery identity changed')
                    revision = saved['delivery_revision']
                    connection.execute('BEGIN IMMEDIATE')
                    previous = connection.execute(
                        'SELECT revision,record FROM delivery WHERE surface_id=?', (sid,)).fetchone()
                    archived = connection.execute(
                        'SELECT record FROM delivery_history WHERE surface_id=? AND revision=?',
                        (sid, revision)).fetchone()
                    if archived and self._record_bytes(archived[0]) != data:
                        raise ValueError('legacy durable delivery conflicts with its archived revision')
                    if previous:
                        previous_sid, previous_runtime = self.validate(json.loads(previous[1]))
                        if previous_sid != sid or previous_runtime['delivery_revision'] != previous[0]:
                            raise ValueError('durable row identity changed')
                        if previous[0] == revision:
                            if self._record_bytes(previous[1]) != data:
                                raise ValueError('conflicting durable delivery records at the same revision')
                            continue
                        if previous[0] > revision:
                            continue
                        self._archive_record(connection, sid, previous)
                    connection.execute(
                        'INSERT INTO delivery VALUES (?, ?, ?) ON CONFLICT(surface_id) DO UPDATE '
                        'SET revision=excluded.revision,record=excluded.record', (sid, revision, data))
            except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
                self.faults.add(path.stem)

    def _archive_record(self, connection, surface_id, previous):
        # A legacy import can leave the exact head in history already. Accept
        # that evidence without replacing it, including its TEXT/BLOB type.
        archived = connection.execute(
            'SELECT record FROM delivery_history WHERE surface_id=? AND revision=?',
            (surface_id, previous[0])).fetchone()
        if archived:
            if self._record_bytes(archived[0]) != self._record_bytes(previous[1]):
                raise ValueError('original delivery archive changed')
        else:
            connection.execute('INSERT INTO delivery_history VALUES (?, ?, ?)', (surface_id, *previous))

    def _commit_batch(self, connection, batch):
        accepted = []
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            for item in batch:
                sid, revision, data = item[:3]
                pre_input = item[3] if len(item) == 5 else None
                previous = connection.execute('SELECT revision,record FROM delivery WHERE surface_id=?', (sid,)).fetchone()
                if previous:
                    try:
                        previous_sid, previous_runtime = self.validate(json.loads(previous[1]))
                        if previous_sid != sid or previous_runtime['delivery_revision'] != previous[0]:
                            raise ValueError('durable row identity changed')
                    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
                        # Keep the unreadable original untouched. Its own
                        # waiter fails without stopping independent surfaces.
                        self.faults.add(self.key(sid))
                        accepted.append(RuntimeError('original durable delivery record is unreadable: ' + str(exc)))
                        continue
                if pre_input:
                    try:
                        revision, data = pre_input[0].resolve(pre_input[1], previous)
                    except PreInputRecoveryError as exc:
                        accepted.append(exc)
                        continue
                if previous and previous[0] >= revision:
                    accepted.append(False)
                    continue
                if (previous and previous_runtime['send_attempt_id']
                        != json.loads(data)['runtime']['send_attempt_id']):
                    # Preserve the exact old outcome, including unknown input.
                    # Archive and head replacement succeed or roll back together.
                    try:
                        self._archive_record(connection, sid, previous)
                    except ValueError as exc:
                        self.faults.add(self.key(sid))
                        accepted.append(RuntimeError(str(exc)))
                        continue
                cursor = connection.execute("INSERT INTO delivery VALUES (?, ?, ?) ON CONFLICT(surface_id) DO UPDATE SET revision=excluded.revision,record=excluded.record WHERE excluded.revision > delivery.revision", (sid, revision, data))
                accepted.append((revision, data) if pre_input and cursor.rowcount == 1 else cursor.rowcount == 1)
        return accepted

    def _run(self):
        connection, batch = None, []
        try:
            connection = sqlite3.connect(self.root / "delivery.sqlite3", timeout=2)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE IF NOT EXISTS delivery (surface_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, record BLOB NOT NULL)")
            connection.execute('CREATE TABLE IF NOT EXISTS delivery_history (surface_id TEXT NOT NULL, revision INTEGER NOT NULL, record BLOB NOT NULL, PRIMARY KEY(surface_id,revision))')
            connection.commit()
            self._migrate_file_heads(connection)
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
                        if isinstance(committed, Exception):
                            item[-1].set_exception(committed)
                        elif committed:
                            item[-1].set_result(committed if isinstance(committed, tuple) else None)
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

    @contextmanager
    def pre_input(self, surface_id, runtime, *, identity=None):
        """Scope only the first durable pending write, before any terminal I/O.

        A failed pending.persist() may be followed by one live
        pending.recover_input_not_sent(proof). Exit this context before input;
        the token cannot be retained, serialized or reconstructed on restart.
        Neither method publishes an observer snapshot.
        """
        pending = _PreInputAttempt(self, surface_id, runtime, identity)
        try:
            yield pending
        finally:
            pending.active = False
            pending.recovery = None

    def _serialize(self, surface_id, runtime):
        revision = runtime.delivery_revision + 1
        saved = {key: getattr(runtime, key) for key in FIELDS}
        saved["delivery_revision"] = revision
        value = {"version": 1, "surface_id": surface_id, "runtime": saved}
        self.validate(value)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        return revision, data

    def persist(self, surface_id, runtime):
        revision, data = self._serialize(surface_id, runtime)
        revision, _ = self._persist_data(surface_id, revision, data)
        runtime.delivery_revision = revision

    def _persist_data(self, surface_id, revision, data, *, pre_input=None):
        if self.blocked(surface_id):
            raise RuntimeError("original durable delivery record is unreadable")
        committed = None
        with self._lifecycle:
            if self._phase == "running":
                committed = Future()
                # Keep the waiter last for shutdown/drain handling. Ordinary
                # writes retain the original four-item batching contract.
                self._pending.put((surface_id, revision, data, pre_input, committed)
                                  if pre_input else (surface_id, revision, data, committed))
            elif self._phase != "new":
                raise RuntimeError("durable delivery writer is not accepting records")
        if committed is not None:
            result = committed.result()
            return result if result is not None else (revision, data)
        with self._lifecycle:
            if self._phase != 'new':
                raise RuntimeError('durable delivery writer changed during file commit')
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / (self.key(surface_id) + '.json')
            with self._file_lock(surface_id):
                try:
                    previous_data = path.read_bytes()
                except FileNotFoundError:
                    previous_data = None
                previous = None
                if previous_data is not None:
                    try:
                        previous_sid, previous = self.validate(json.loads(previous_data))
                        if previous_sid != surface_id:
                            raise ValueError('original durable delivery identity changed')
                    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
                        self.faults.add(self.key(surface_id))
                        raise RuntimeError('original durable delivery record is unreadable: ' + str(exc)) from exc
                if pre_input:
                    revision, data = pre_input[0].resolve(pre_input[1],
                        (previous['delivery_revision'], previous_data) if previous else None)
                saved = json.loads(data)['runtime']
                if previous is not None:
                    if previous['delivery_revision'] >= revision:
                        raise RuntimeError('durable delivery revision is stale')
                    if previous['send_attempt_id'] != saved['send_attempt_id']:
                        archive = self.root / 'history' / self.key(surface_id) / (str(previous['delivery_revision']) + '.json')
                        # If head replacement failed previously, preserve the
                        # existing archive rather than overwriting its evidence.
                        if archive.exists():
                            if archive.read_bytes() != previous_data:
                                raise RuntimeError('original delivery archive changed')
                        else:
                            self._write_file(archive, previous_data)
                        # A leaf fsync does not persist newly created parent
                        # links. Retry this bounded chain even for an archive
                        # left by a failed directory fsync or head replacement.
                        for directory in (archive.parent, archive.parent.parent, self.root):
                            self._fsync_directory(directory)
                self._write_file(path, data)
                return revision, data

    @contextmanager
    def _file_lock(self, surface_id, *, blocking=True):
        # The lock inode must survive head replacement and individual writers.
        # An instance lock alone cannot serialize another store or process.
        with (self.root / (self.key(surface_id) + '.lock')).open('a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _fsync_directory(path):
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @staticmethod
    def _write_file(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.delivery-', dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            DeliveryStore._fsync_directory(path.parent)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

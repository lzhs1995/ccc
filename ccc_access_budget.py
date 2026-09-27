"""Per-batch admission accounting for explicitly selected API access checks.

This module neither sends input nor signals a native process. Reservations are
durable before dispatch. The completion gate is an in-memory operation, so slow
storage and a large CCC inventory cannot let another queued request escape.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid


class AdmissionClosed(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    workspace_id: str
    job_id: str
    slots: int = 50
    max_attempts: int = 1000
    max_output_tokens: int = 128

    def __post_init__(self):
        for value in (self.workspace_id, self.job_id):
            uuid.UUID(value)
        if self.slots != 50 or type(self.slots) is not int:
            raise ValueError('access checks retain exactly 50 concurrent slots')
        if type(self.max_attempts) is not int or not 50 <= self.max_attempts <= 10000:
            raise ValueError('invalid finite attempt limit')
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 512:
            raise ValueError('invalid output limit')


@dataclass(frozen=True)
class Reservation:
    number: int
    slot: int
    session_id: str


class AccessBudget:
    """One owner per job; no global fleet lock or per-request history scan."""

    def __init__(self, path, policy, *, create=False):
        self.path, self.policy = Path(path), policy
        self._lock, self._storage_lock = threading.Lock(), threading.Lock()
        self._active, self._blocked_slots, self._dispatched = {}, set(), set()
        self._active_slots, self._sessions = {}, {}
        self._known_success = {}
        self._durable, self._finishing, self._seen_numbers = set(), set(), set()
        self._attempts, self._success, self._fault = 0, None, ''
        self._closed = False
        self._fd = None
        flags = os.O_RDWR | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0)
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('access journal must be private and owned')
            self._fd = fd
            if create:
                self._append({'kind': 'policy', 'version': 1, **asdict(policy)})
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            else:
                if not 0 < info.st_size <= 16 * 1024 * 1024:
                    raise ValueError('access journal missing or oversized')
                raw = os.pread(fd, info.st_size, 0)
                if not raw.endswith(b'\n'):
                    raise ValueError('unfinished access journal; no new requests authorized')
                lines = [json.loads(line) for line in raw.splitlines()]
                if lines[0] != {'kind': 'policy', 'version': 1, **asdict(policy)}:
                    raise ValueError('access journal belongs to another job or policy')
                for event in lines[1:]:
                    self._replay(event)
                if (self._attempts > policy.max_attempts
                        or self._seen_numbers != set(range(1, self._attempts + 1))):
                    raise ValueError('access attempt history is incomplete')
                # Completion may have closed the memory gate immediately before
                # a crash, without a durable completion record. An unresolved
                # reservation therefore closes this whole job on recovery.
                # Merely closing its old slot would let another slot spend.
                if self._active:
                    self._fault = 'previous launch has unresolved requests; this batch stays closed'
                self._blocked_slots.update(r.slot for r in self._active.values())
                self._active.clear()
                self._active_slots.clear()
        except BaseException:
            os.close(fd)
            self._fd = None
            raise

    def _append(self, event):
        raw = (json.dumps(event, sort_keys=True, separators=(',', ':')) + '\n').encode()
        try:
            with self._storage_lock:
                view = memoryview(raw)
                while view:
                    count = os.write(self._fd, view)
                    if count <= 0:
                        raise OSError('incomplete access journal write')
                    view = view[count:]
                os.fsync(self._fd)
        except BaseException:
            with self._lock:
                self._fault = 'access accounting unavailable'
            raise

    def _replay(self, event):
        if event.get('kind') == 'reserved':
            number, slot = event.get('number'), event.get('slot')
            if type(number) is not int or number <= 0 or type(slot) is not int or not 0 <= slot < self.policy.slots:
                raise ValueError('invalid access reservation')
            session_id = event.get('session_id')
            if (number in self._seen_numbers or not isinstance(session_id, str)
                    or not session_id or len(session_id) > 128
                    or slot in self._active_slots
                    or slot in self._sessions and self._sessions[slot] != session_id):
                raise ValueError('duplicate access reservation')
            self._seen_numbers.add(number)
            self._active[number] = Reservation(number, slot, session_id)
            self._active_slots[slot] = number
            self._sessions[slot] = session_id
            self._attempts = max(self._attempts, number)
        elif event.get('kind') == 'finished':
            reservation = self._active.pop(event.get('number'), None)
            if reservation is None:
                raise ValueError('access completion has no reservation')
            self._active_slots.pop(reservation.slot)
            outcome = event.get('outcome')
            if outcome == 'complete':
                if not isinstance(event.get('response_id'), str) or not event['response_id']:
                    raise ValueError('access completion lacks upstream response identity')
                self._success = self._success or event
            elif outcome == 'uncertain':
                self._blocked_slots.add(reservation.slot)
            elif outcome not in {'rejected', 'cancelled_before_dispatch'}:
                raise ValueError('unknown access outcome')
        else:
            raise ValueError('unknown access accounting event')

    def reserve(self, slot, session_id):
        if type(slot) is not int or not 0 <= slot < self.policy.slots:
            raise ValueError('invalid access slot')
        if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
            raise ValueError('invalid native session identity')
        with self._lock:
            reason = self._closed_reason(slot)
            if reason:
                raise AdmissionClosed(reason)
            if slot in self._active_slots:
                raise AdmissionClosed('this slot already has an in-flight request')
            if slot in self._sessions and self._sessions[slot] != session_id:
                raise AdmissionClosed('this slot belongs to another native session; auxiliary requests are closed')
            self._attempts += 1
            reservation = Reservation(self._attempts, slot, session_id)
            self._active[reservation.number] = reservation
            self._active_slots[slot] = reservation.number
            self._sessions[slot] = session_id
        # The small memory lock is released during storage; note_success must
        # stay responsive even when an fsync or worker queue is slow.
        self._append({'kind': 'reserved', **asdict(reservation), 'at': time.time()})
        with self._lock:
            self._durable.add(reservation.number)
        return reservation

    def _closed_reason(self, slot):
        if self._closed:
            return 'access accounting is closed'
        if self._fault:
            return self._fault
        if self._success is not None:
            return 'this batch has a completed API check; no new check requests'
        if slot in self._blocked_slots:
            return 'the previous request outcome is uncertain; this slot stays closed'
        if self._attempts >= self.policy.max_attempts:
            return 'this batch reached its finite HTTP attempt limit'
        return ''

    def check_admission(self, slot, session_id):
        """Fast precheck; reserve and begin_dispatch repeat the authoritative gate."""
        with self._lock:
            reason = self._closed_reason(slot)
            if reason:
                raise AdmissionClosed(reason)
            if slot in self._active_slots:
                raise AdmissionClosed('this slot already has an in-flight request')
            if slot in self._sessions and self._sessions[slot] != session_id:
                raise AdmissionClosed('this slot belongs to another native session')

    def begin_dispatch(self, reservation):
        """Call immediately before writing request bytes, after all awaits."""
        with self._lock:
            if (self._closed or self._fault or self._success is not None or reservation.slot in self._blocked_slots
                    or self._active.get(reservation.number) != reservation
                    or reservation.number not in self._durable
                    or reservation.number in self._finishing
                    or reservation.number in self._dispatched):
                raise AdmissionClosed(self._closed_reason(reservation.slot) or 'reservation cannot be dispatched')
            self._dispatched.add(reservation.number)

    def note_success(self, reservation, response_id):
        """Immediate, bounded gate; caller has parsed a full real model reply."""
        if not isinstance(response_id, str) or not response_id:
            raise ValueError('missing upstream response identity')
        with self._lock:
            if (self._closed or self._active.get(reservation.number) != reservation
                    or reservation.number in self._finishing
                    or reservation.number not in self._dispatched):
                raise ValueError('success is not bound to a dispatched request')
            known = self._known_success.get(reservation.number)
            if known is not None and known != response_id:
                raise ValueError('successful response identity changed')
            self._known_success[reservation.number] = response_id
            if self._success is None:
                self._success = {'response_id': response_id, 'number': reservation.number,
                                 'observed_at': time.time(), 'observed_monotonic': time.monotonic()}

    def finish(self, reservation, outcome, *, response_id='', usage=None):
        if outcome not in {'complete', 'rejected', 'uncertain', 'cancelled_before_dispatch'}:
            raise ValueError('unknown request outcome')
        if outcome == 'complete' and (not isinstance(response_id, str) or not response_id):
            raise ValueError('missing upstream response identity')
        event = {'kind': 'finished', 'number': reservation.number, 'outcome': outcome,
                 'response_id': response_id, 'usage': usage, 'at': time.time()}
        with self._lock:
            if self._active.get(reservation.number) != reservation:
                raise ValueError('request was already completed')
            if reservation.number in self._finishing:
                raise ValueError('request completion is already being persisted')
            if self._closed or reservation.number not in self._durable:
                raise ValueError('request completion lacks live durable accounting')
            dispatched = reservation.number in self._dispatched
            if (outcome == 'cancelled_before_dispatch') == dispatched:
                raise ValueError('request outcome contradicts its dispatch state')
            known = self._known_success.get(reservation.number)
            if known is not None and (outcome != 'complete' or known != response_id):
                raise ValueError('observed success is irreversible')
            if outcome == 'complete' and self._success is None:
                self._success = {'response_id': response_id, 'number': reservation.number,
                                 'observed_at': time.time(), 'observed_monotonic': time.monotonic()}
            if outcome == 'complete':
                self._known_success[reservation.number] = response_id
            self._finishing.add(reservation.number)
            if outcome == 'uncertain':
                self._blocked_slots.add(reservation.slot)
        self._append(event)
        with self._lock:
            self._active.pop(reservation.number)
            self._active_slots.pop(reservation.slot)
            self._dispatched.discard(reservation.number)
            self._durable.discard(reservation.number)
            self._finishing.discard(reservation.number)

    def snapshot(self):
        with self._lock:
            return {'workspace_id': self.policy.workspace_id, 'job_id': self.policy.job_id,
                    'attempts': self._attempts, 'max_attempts': self.policy.max_attempts,
                    'in_flight': len(self._active), 'blocked_slots': sorted(self._blocked_slots),
                    'first_complete': self._success, 'fault': self._fault,
                    'closed': self._closed,
                    'max_output_tokens': self.policy.max_output_tokens}

    def close(self):
        with self._lock:
            self._closed = True
        with self._storage_lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

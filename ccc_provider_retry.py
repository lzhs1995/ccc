"""Durable provider-error remedies, independent of UI episodes and turn IDs.

Reservations precede input. An uncertain input consumes its reservation; neither
an idle frame nor a process restart refunds it. This module never sends input.
"""
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sqlite3
import time


RETRYABLE = frozenset({'rate_limit', 'http_500', 'http_502', 'http_503', 'http_504', 'http_524'})


def retry_after(message, observed_at):
    """Return the server's absolute floor (seconds or RFC HTTP date)."""
    deadlines = []
    for match in re.finditer(r'(?im)\bretry-after\s*["\']?\s*[:=]\s*["\']?([^\r\n"\'}]+)', str(message)):
        value = match.group(1).strip()
        try:
            if re.fullmatch(r'\d+(?:\.\d+)?', value):
                deadline = observed_at + float(value)
            else:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    continue
                deadline = parsed.timestamp()
            if math.isfinite(deadline):
                deadlines.append(max(observed_at, deadline))
        except (ValueError, OverflowError, TypeError):
            continue
    return max(deadlines, default=0.0)


class ProviderRetryStore:
    def __init__(self, path, *, clock=time.time, jitter=None):
        self.path = Path(path)
        self.clock = clock
        self.jitter = jitter or (lambda: random.SystemRandom().uniform(0, .1))

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=2)
        try:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('CREATE TABLE IF NOT EXISTS episodes (identity TEXT PRIMARY KEY, record TEXT NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS cooldowns (provider TEXT PRIMARY KEY, until REAL NOT NULL)')
            connection.execute('CREATE TABLE IF NOT EXISTS server_floors (provider TEXT PRIMARY KEY, until REAL NOT NULL)')
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def identity(session, provider):
        if not isinstance(session, str) or not session or not isinstance(provider, str) or not provider:
            raise ValueError('provider retry requires original session and provider identity')
        return hashlib.sha256(json.dumps([session, provider]).encode()).hexdigest()

    @staticmethod
    def _save(connection, key, record):
        connection.execute('INSERT OR REPLACE INTO episodes VALUES (?, ?)', (key, json.dumps(record)))

    def observe(self, session, provider, turn, error_type, message, at):
        """Observe one original failure; repeated polls never move its deadline."""
        if error_type not in RETRYABLE:
            return None
        if not turn or not math.isfinite(at) or at <= 0:
            raise ValueError('provider retry requires dated original failed turn')
        key = self.identity(session, provider)
        with self.transaction() as connection:
            row = connection.execute('SELECT record FROM episodes WHERE identity=?', (key,)).fetchone()
            record = json.loads(row[0]) if row else dict(count=0, failure_at=0, success_at=0, seen={})
            if at <= record['success_at']:
                return None
            stamp = hashlib.sha256(json.dumps([turn, float(at), error_type, message]).encode()).hexdigest()
            if stamp not in record['seen']:
                if len(record['seen']) >= 256:
                    raise ValueError('provider failure history limit reached; success proof required')
                now = self.clock()
                delay = (15, 30, 60, 120)[min(record['count'], 3)]
                noise = float(self.jitter())
                if not math.isfinite(noise) or not 0 <= noise <= .1:
                    raise ValueError('invalid retry jitter')
                server = retry_after(message, at)
                deadline = max(now + delay * (1 + noise), server)
                record['seen'][stamp] = deadline
                record['due'] = max(record.get('due', 0), deadline)
                record['failure_at'] = max(record['failure_at'], at)
                if server:
                    connection.execute('INSERT INTO cooldowns VALUES (?, ?) ON CONFLICT(provider) DO UPDATE SET until=MAX(until,excluded.until)', (provider, server))
                    connection.execute('INSERT INTO server_floors VALUES (?, ?) ON CONFLICT(provider) DO UPDATE SET until=MAX(until,excluded.until)', (provider, server))
                self._save(connection, key, record)
            return {'identity': key, 'provider': provider, 'stamp': stamp}

    def reserve(self, evidence, attempt):
        """Atomically consume one remedy and serialize same-provider attempts."""
        if not attempt:
            raise ValueError('missing delivery attempt')
        return self._admit(evidence, attempt)

    def ready(self, evidence, attempt=None):
        return self._admit(evidence, attempt, dry_run=True)

    def _admit(self, evidence, attempt, *, dry_run=False):
        with self.transaction() as connection:
            row = connection.execute('SELECT record FROM episodes WHERE identity=?', (evidence['identity'],)).fetchone()
            if not row:
                return False
            record = json.loads(row[0])
            now = self.clock()
            floor = connection.execute('SELECT until FROM server_floors WHERE provider=?', (evidence['provider'],)).fetchone()
            if floor and now < floor[0]:
                return False
            # Idempotency is only for the same in-flight operation (paste/Enter).
            if attempt and record.get('attempt') == attempt:
                return record.get('reserved_stamp') == evidence['stamp']
            cooldown = connection.execute('SELECT until FROM cooldowns WHERE provider=?', (evidence['provider'],)).fetchone()
            if (evidence['stamp'] not in record['seen'] or record['count'] >= 4
                    or now < record.get('due', float('inf')) or cooldown and now < cooldown[0]):
                return False
            if attempt is None or dry_run:
                return True
            record['count'] += 1
            record['attempt'] = attempt
            record['reserved_stamp'] = evidence['stamp']
            record['due'] = now + (15, 30, 60, 120)[min(record['count'], 3)]
            self._save(connection, evidence['identity'], record)
            connection.execute('INSERT INTO cooldowns VALUES (?, ?) ON CONFLICT(provider) DO UPDATE SET until=MAX(until,excluded.until)', (evidence['provider'], record['due']))
            return True

    def success(self, session, provider, *, at, completed_turn, last_agent_message, error):
        """Reset only on a later native completion containing an actual answer.

        Callers must bind this observation to the original live session. Empty
        task_complete, Working, user echoes, timers and ACKs are not success.
        """
        if not completed_turn or error or not isinstance(last_agent_message, str) or not last_agent_message.strip():
            return False
        if not math.isfinite(at):
            return False
        key = self.identity(session, provider)
        with self.transaction() as connection:
            row = connection.execute('SELECT record FROM episodes WHERE identity=?', (key,)).fetchone()
            if not row:
                return False
            record = json.loads(row[0])
            if at <= max(record['failure_at'], record['success_at']):
                return False
            self._save(connection, key, dict(count=0, failure_at=record['failure_at'], success_at=at, seen={}))
            # A successful session cannot cancel another session's cooldown.
            return True

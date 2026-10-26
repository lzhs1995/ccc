"""Optional JEV assist for current native failures that local rules leave UNKNOWN.

Local classification always runs first and is final when it returns a code.
Deterministic gates are checked before any request. At most one bounded worker
asks JEV once per fingerprint; the answer only selects an allowlisted existing
retry code and never becomes an action. Any failure keeps UNKNOWN.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import queue
import threading

MODEL = 'jev-1.14.0'
ENDPOINT_HOST = 'api.typesafe.ai'
ENDPOINT_PATH = '/v1/systemone'
MAX_EXCERPT = 600
MIN_CONFIDENCE = 0.8
TIMEOUT_SECONDS = 6.0
QUEUE_LIMIT = 8
# JEV choice -> existing ccc_provider_retry code. Anything else stays UNKNOWN.
ALLOWED = {'RATE_LIMIT': 'rate_limit', 'UPSTREAM_UNAVAILABLE': 'http_503', 'UPSTREAM_INVALID': 'upstream_invalid'}
CHOICES = sorted(ALLOWED) + ['UNKNOWN']


@dataclass(frozen=True)
class Gates:
    current_native_error: bool
    identity_bound: bool
    composer_empty: bool
    user_aborted: bool = False
    compacting: bool = False
    busy_or_queued: bool = False
    previous_delivery_unknown: bool = False

    def allow(self):
        return (self.current_native_error and self.identity_bound and self.composer_empty
                and not (self.user_aborted or self.compacting or self.busy_or_queued
                         or self.previous_delivery_unknown))


def fingerprint(client, model, text):
    excerpt = str(text)[:MAX_EXCERPT]
    return hashlib.sha256(json.dumps([client, model, excerpt]).encode()).hexdigest()


def parse_answer(body):
    """Return an allowlisted retry code or None; malformed/low confidence is None."""
    try:
        answer = (body.get('answers') or {}).get('diagnosis') or {}
        choice, confidence = answer.get('choice'), answer.get('confidence')
    except AttributeError:
        return None
    if choice not in ALLOWED or type(confidence) not in (int, float) or confidence < MIN_CONFIDENCE:
        return None
    return ALLOWED[choice]


def http_transport(request, timeout):
    import http.client
    key = os.environ.get('TYPESAFE_API_KEY')
    if not key:
        return None
    conn = http.client.HTTPSConnection(ENDPOINT_HOST, timeout=timeout)
    try:
        conn.request('POST', ENDPOINT_PATH, body=json.dumps(request, ensure_ascii=False).encode(),
                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        return json.loads(response.read(64 * 1024)) if response.status == 200 else None
    finally:
        conn.close()


class JevUnknownClassifier:
    def __init__(self, transport=http_transport, *, start=True):
        self.transport = transport
        self.cache = {}
        self.pending = set()
        self.queue = queue.Queue(QUEUE_LIMIT)
        self.lock = threading.Lock()
        self.calls = 0
        if start:
            threading.Thread(target=self._worker, daemon=True, name='ccc-jev').start()

    def cached(self, client, model, text):
        """Poll-path lookup: cache only, never schedules or calls."""
        return self.cache.get(fingerprint(client, model, text))

    def resolve(self, local_code, gates, client, model, text):
        """Known local code wins; UNKNOWN schedules at most one gated request."""
        if local_code is not None:
            return local_code
        if not gates.allow():
            return None
        key = fingerprint(client, model, text)
        with self.lock:
            if key in self.cache:
                return self.cache[key]
            if key in self.pending:
                return None
            try:
                self.queue.put_nowait((key, client, model, str(text)[:MAX_EXCERPT]))
            except queue.Full:
                return None
            self.pending.add(key)
        return None

    def run_once(self):
        """Process one queued item (worker body; tests call it directly)."""
        key, client, model, excerpt = self.queue.get()
        result = None
        try:
            self.calls += 1
            request = dict(model=MODEL, state=dict(client=client, model=model, error=excerpt),
                           questions={'diagnosis': dict(type='choice', choices=CHOICES,
                               instructions='Classify this provider failure text. Choose UNKNOWN if unsure.')})
            result = parse_answer(self.transport(request, TIMEOUT_SECONDS) or {})
        except Exception:
            result = None
        with self.lock:
            self.cache[key] = result
            self.pending.discard(key)

    def _worker(self):
        while True:
            self.run_once()

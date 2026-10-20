"""Durable, read-only accounting for native inputs before compact control.

This does not settle a MAC delivery. A native Submission is an input fence;
only one exact, complete user response_item later than that fence can discharge
it. Screen text, RPC returns, event_msg and another newer input cannot do so.
Unknown inputs and lost evidence veto control, including after a restart.
"""
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import math

from ccc_codex_goal import _debug_fields
from ccc_compact_support import sync_directory, owned_file, OwnedDirectory

MAX_BODY = 65536
MAX_ROWS = 2048
MAX_ENTRIES = 8192
LEDGER_VERSION = 3


def text_identity(text):
    raw = text.encode('utf-8')
    return hashlib.sha256(raw).hexdigest(), len(raw)


def parse_submission(body, sid):
    """Parse the actual envelope; never search for fields inside input text."""
    prefix = f'session_loop{{thread_id={sid}}}: Submission sub='
    if not isinstance(body, str) or not body.startswith(prefix) or len(body) > MAX_BODY:
        raise ValueError('unknown or oversized submission envelope')
    submission = _debug_fields(body[len(prefix):], 'Submission')
    if submission.get('residency_guard') == 'None':
        submission.pop('residency_guard')
    if set(submission) != {'id', 'op', 'trace', 'parent_turn_id', 'root_turn_id'}:
        raise ValueError('unknown submission fields')
    op = _debug_fields(submission['op'], 'TurnInput')
    if set(op) != {'request', 'mode', 'reply'}:
        raise ValueError('unknown input fields')
    request = _debug_fields(op['request'], 'TurnInputRequest')
    if set(request) != {'input', 'thread_settings', 'start', 'additional_context',
                        'responsesapi_client_metadata', 'trace'}:
        raise ValueError('unknown request fields')
    source = 'user'
    kinds = None
    if request['input'].startswith('UserInput {'):
        user = _debug_fields(request['input'], 'UserInput')
        if set(user) != {'content', 'client_id'}:
            raise ValueError('unknown user input fields')
        content = user['content']
        item_name = 'Text'
    elif request['input'].startswith('ResponseItem(') and request['input'].endswith(')'):
        user = _debug_fields(request['input'][13:-1], 'Message')
        if (set(user) != {'id', 'role', 'content', 'phase',
                          'internal_chat_message_metadata_passthrough'}
                or user['id'] != 'None' or user['phase'] != 'None'
                or json.loads(user['role']) != 'user'):
            raise ValueError('unsupported response item')
        meta = user['internal_chat_message_metadata_passthrough']
        if not meta.startswith('Some(') or not meta.endswith(')'):
            raise ValueError('missing internal input provenance')
        meta = _debug_fields(meta[5:-1], 'InternalChatMessageMetadataPassthrough')
        if (set(meta) != {'turn_id', 'create_time', 'content_item_kinds', 'cell_id',
                          'executed_tool_calls', 'tool_calls_complete'}
                or any(meta[k] != 'None' for k in meta if k != 'content_item_kinds')
                or meta['content_item_kinds'] != 'Some([ContentItemKind("goal.internal_context")])'):
            raise ValueError('unknown internal input provenance')
        start = _debug_fields(request['start'], 'TurnStartOptions')
        if op['mode'] != 'StartIfIdle' or start.get('turn_trigger') != 'Some("goal")':
            raise ValueError('Goal source and mode disagree')
        source, kinds = 'goal.internal_context', ['goal.internal_context']
        content, item_name = user['content'], 'InputText'
    else:
        raise ValueError('unknown input representation')
    if not content.startswith('[') or not content.endswith(']'):
        raise ValueError('unrecognized input content')
    item = _debug_fields(content[1:-1].strip(), item_name)
    fields = {'text', 'text_elements'} if item_name == 'Text' else {'text'}
    if set(item) != fields or item.get('text_elements', '[]') != '[]':
        raise ValueError('nonliteral or multiple input elements')
    text = json.loads(item['text'])
    submission_id = json.loads(submission['id'])
    if not isinstance(text, str) or not isinstance(submission_id, str) or not submission_id:
        raise ValueError('invalid text or submission id')
    expected = None
    if op['mode'].startswith('Steer {'):
        mode = _debug_fields(op['mode'], 'Steer')
        if set(mode) != {'expected_turn_id'}:
            raise ValueError('unknown steer mode')
        expected = json.loads(mode['expected_turn_id'])
        if not isinstance(expected, str) or not expected:
            raise ValueError('missing expected turn')
    elif op['mode'] not in {'Start', 'StartIfIdle'}:
        raise ValueError('unknown input mode')
    if op['mode'] == 'StartIfIdle' and source != 'goal.internal_context':
        raise ValueError('unproven StartIfIdle source')
    digest, size = text_identity(text)
    return dict(submission_id=submission_id, expected_turn_id=expected,
                receipt_turn_id=expected or submission_id,
                mode='Steer' if expected else op['mode'], source=source,
                content_item_kinds=kinds, payload_sha256=digest, payload_bytes=size)


def read_submissions(sid, cursor, db_path, *, limit=MAX_ROWS):
    """Scan a bounded rowid range, including late rows with older timestamps.

    Filtering before LIMIT can scan a whole idle thread on every tick. Instead
    advance over at most limit thread rows, fetching bodies only for the two
    native submission targets. The global high watermark is read in the same
    transaction so a caught-up idle thread need not retain its last input's id.
    """
    with closing(sqlite3.connect(Path(db_path).as_uri() + '?mode=ro', uri=True,
                                 timeout=.15)) as db:
        deadline = time.monotonic() + .8
        db.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        db.execute('BEGIN')
        first = db.execute('SELECT id,ts,ts_nanos FROM logs ORDER BY id LIMIT 1').fetchone()
        thread_first = db.execute(
            'SELECT id,ts,ts_nanos FROM logs INDEXED BY idx_logs_thread_id '
            'WHERE thread_id=? ORDER BY id LIMIT 1', (sid,)).fetchone()
        high = db.execute('SELECT id FROM logs ORDER BY id DESC LIMIT 1').fetchone()
        high = high[0] if high else 0
        rows = db.execute(
            'SELECT id,ts,ts_nanos,length(feedback_log_body),'
            "CASE WHEN target IN ('codex_core::session::handlers','codex_core::codex') "
            'THEN substr(feedback_log_body,1,?) END '
            'FROM logs INDEXED BY idx_logs_thread_id '
            'WHERE thread_id=? AND id>? AND id<=? ORDER BY id LIMIT ?',
            (MAX_BODY + 1, sid, cursor, high, limit + 1)).fetchall()
    caught_up = len(rows) <= limit
    rows = rows[:limit]
    return dict(rows=rows, caught_up=caught_up,
                cursor=high if caught_up else rows[-1][0], high=high,
                thread_first_id=thread_first[0] if thread_first else None,
                thread_first_at=thread_first[1] + thread_first[2] / 1e9 if thread_first else None,
                first_id=first[0] if first else None,
                first_at=first[1] + first[2] / 1e9 if first else None)


def receipt_matches(entry, receipt):
    """Full payload identity is selected by the caller; enforce its provenance."""
    meta = receipt.get('metadata')
    if not isinstance(meta, dict) or meta.get('turn_id') != entry.get('receipt_turn_id'):
        return False
    kinds = meta.get('content_item_kinds')
    if (entry.get('source') == 'goal.internal_context' and kinds != ['goal.internal_context']
            or entry.get('source') == 'user' and kinds not in (None, [], ['user.text'])):
        return False
    # Native JSONL timestamps truncate to milliseconds; metadata retains the
    # real creation time. Do not admit a pre-intent echo by adding a time skew.
    at = meta.get('create_time', receipt.get('at'))
    if (not isinstance(at, (int, float)) or isinstance(at, bool)
            or not math.isfinite(at) or at < entry['at']):
        return False
    return receipt.get('offset', -1) >= entry.get('minimum_offset', 0)


def verify_receipt(path, file_id, entry, receipt):
    """Re-read the original JSONL record; a cached receipt is not proof."""
    start, end = receipt.get('offset'), receipt.get('end_offset')
    if (type(start) is not int or type(end) is not int or start < 0
            or not 0 < end - start <= 16 * 1024 * 1024):
        raise ValueError('invalid native receipt extent')
    with owned_file(path) as (stream, st):
        if [st.st_dev, st.st_ino] != list(file_id) or st.st_size < end:
            raise ValueError('original native receipt file changed')
        if start:
            stream.seek(start - 1)
            if stream.read(1) != b'\n':
                raise ValueError('native receipt is not at a record boundary')
        stream.seek(start)
        raw = stream.read(end - start)
        if (not raw.endswith(b'\n') or b'\n' in raw[:-1]
                or hashlib.sha256(raw).hexdigest() != receipt.get('record_sha256')):
            raise ValueError('original native receipt bytes changed')
        value = json.loads(raw)
        payload = value.get('payload') or {}
        content = payload.get('content')
        if (value.get('type') != 'response_item' or payload.get('type') != 'message'
                or payload.get('role') != 'user' or not isinstance(content, list)
                or len(content) != 1 or not isinstance(content[0], dict)
                or set(content[0]) != {'type', 'text'} or content[0]['type'] != 'input_text'
                or not isinstance(content[0]['text'], str)):
            raise ValueError('original record is not one complete native user input')
        if (text_identity(content[0]['text']) != (entry['payload_sha256'], entry['payload_bytes'])
                or payload.get('internal_chat_message_metadata_passthrough') != receipt.get('metadata')
                or value.get('timestamp') != receipt.get('timestamp')
                or not receipt_matches(entry, receipt)):
            raise ValueError('native receipt payload or provenance mismatch')
    return True


class PendingInputs:
    """Keep every undisclosed/undelivered input; eviction never clears one."""
    def __init__(self, path, sid, rollout, db_path, *, intent_index=None,
                 allow_authenticated_rebind=False):
        self.path, self.sid = Path(path), sid
        self.rollout, self.db_path = rollout, Path(db_path)
        self.intent_index = intent_index
        st = self.db_path.stat()
        binding = dict(session_id=sid, path=str(rollout.path),
                       file_id=list(rollout.file_id), db_file_id=[st.st_dev, st.st_ino])
        self.persisted = None
        self.pending_binding = None
        if self.path.exists():
            with owned_file(self.path) as (stream, info):
                if info.st_size > 64 * 1024 * 1024:
                    raise ValueError('pending-input ledger exceeds its bound')
                raw = stream.read(64 * 1024 * 1024 + 1)
                if len(raw) != info.st_size:
                    raise ValueError('pending-input ledger changed during read')
            self.state = json.loads(raw)
            if self.state.get('version') not in (2, LEDGER_VERSION):
                raise RuntimeError('pending-input binding changed; observation only')
            if self.state.get('binding') != binding:
                prior = self.state.get('binding') or {}
                if (not allow_authenticated_rebind
                        or prior.get('session_id') != sid
                        or prior.get('path') != binding['path']):
                    raise RuntimeError('pending-input binding changed; observation only')
                # Construction is not authorization. Keep the old ledger intact
                # until Worker authenticates the current loaded native thread.
                self.pending_binding = binding
            elif self.state['version'] == 2:
                self.migrate_v2(raw)
        else:
            self.state = dict(version=LEDGER_VERSION, binding=binding, cursor=0,
                              entries={}, intents={}, unknown=[], initial_coverage=False)

    def adopt_binding(self, proof, facts):
        """Preserve every old input across an authenticated device remount.

        This only updates observation identity. It never establishes coverage,
        settles a pending input, or grants control over an old operation.
        """
        if self.pending_binding is None:
            return
        prior, current = self.state['binding'], self.pending_binding
        st = self.db_path.stat()
        thread, observed = proof.get('thread') or {}, proof.get('binding') or {}
        process = observed.get('process') or {}
        if (facts.get('session_id') != self.sid or not facts.get('caught_up')
                or facts.get('path') != prior['path']
                or list(facts.get('file_id') or []) != current['file_id']
                or [st.st_dev, st.st_ino] != current['db_file_id']
                or len(prior.get('file_id', [])) != 2
                or len(prior.get('db_file_id', [])) != 2
                or prior['file_id'][1] != current['file_id'][1]
                or prior['db_file_id'][1] != current['db_file_id'][1]
                or thread.get('id') != self.sid or thread.get('sessionId') != self.sid
                or thread.get('path') != prior['path']
                or thread.get('canAcceptDirectInput') is not True
                or type(process.get('pid')) is not int
                or not isinstance(process.get('birth'), list) or len(process['birth']) != 2):
            raise RuntimeError('storage adoption lacks current original-session proof')
        with owned_file(self.path) as (stream, _):
            raw = stream.read()
        if json.loads(raw) != self.state:
            raise RuntimeError('pending ledger changed during authentication')
        digest = hashlib.sha256(raw).hexdigest()
        backup = self.path.with_name(self.path.name + '.binding-' + digest + '.json')
        try:
            with backup.open('xb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(backup.parent)
        except FileExistsError:
            with owned_file(backup) as (stream, _):
                if stream.read() != raw:
                    raise RuntimeError('original input ledger backup changed')
        updated = copy.deepcopy(self.state)
        updated.setdefault('binding_adoptions', []).append(dict(
            at=time.time(), previous=copy.deepcopy(prior), current=copy.deepcopy(current),
            proof=copy.deepcopy(proof), original_path=str(backup), original_sha256=digest,
            prior_initial_coverage=self.state.get('initial_coverage')))
        updated.update(binding=copy.deepcopy(current), version=LEDGER_VERSION,
                       initial_coverage=False)
        reason = 'storage binding adopted; coverage across remount unproven'
        if reason not in updated['unknown']:
            updated['unknown'].append(reason)
        original, self.state = self.state, updated
        try:
            self.save()
        except BaseException:
            self.state = original
            raise
        self.pending_binding = None

    def migrate_v2(self, raw):
        # v2 admitted initial coverage using another thread's retained row.
        # Keep its exact evidence and all pending entries, but never inherit
        # that permission. Reopening the file must not silently bless it.
        digest = hashlib.sha256(raw).hexdigest()
        backup = self.path.with_name(self.path.name + '.v2-' + digest + '.json')
        try:
            with backup.open('xb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            sync_directory(backup.parent)
        except FileExistsError:
            with owned_file(backup) as (stream, _):
                if stream.read() != raw:
                    raise RuntimeError('legacy input ledger backup changed')
        self.state['coverage_migration'] = dict(
            from_version=2, original_path=str(backup), original_sha256=digest,
            prior_initial_coverage=self.state.get('initial_coverage'))
        self.state['version'] = LEDGER_VERSION
        self.state['initial_coverage'] = False
        reason = 'legacy v2 initial coverage lacks per-thread boundary proof'
        if reason not in self.state['unknown']:
            self.state['unknown'].append(reason)
        self.save()

    def save(self):
        raw = (json.dumps(self.state, sort_keys=True, ensure_ascii=False) + '\n').encode()
        if raw == self.persisted:
            return
        with OwnedDirectory(self.path.parent) as directory:
            directory.atomic_bytes(self.path.name, raw)
        self.persisted = raw

    def refresh(self, *, final=False):
        if self.intent_index is None:
            return self._refresh(final=final)
        # Hold the shared index lock from the first local ledger change through
        # persistence. Another target cannot rewrite a native reader's input pin.
        with self.intent_index.lock:
            if self.intent_index.control_is_pending():
                raise RuntimeError('original native control receipt pending; input evidence pinned')
            return self._refresh(final=final)

    def _refresh(self, *, final=False):
        if self.pending_binding is not None:
            raise RuntimeError('pending-input binding needs authenticated adoption; no control')
        st = self.db_path.stat()
        if [st.st_dev, st.st_ino] != self.state['binding']['db_file_id']:
            raise RuntimeError('input log replaced; pending evidence retained')
        facts = self.rollout.snapshot()
        if facts['file_id'] != self.state['binding']['file_id']:
            raise RuntimeError('input rollout replaced; pending evidence retained')
        batch = read_submissions(self.sid, self.state['cursor'], self.db_path)
        rows, caught_up = batch['rows'], batch['caught_up']
        unknowns = self.state['unknown']

        def veto(reason):
            if reason not in unknowns:
                unknowns.append(reason)

        if facts.get('session_id') != self.sid or facts.get('session_created_at') is None:
            veto('unproven rollout session boundary')
        elif self.state['cursor'] == 0:
            if batch['first_at'] is None or batch['first_at'] > facts['session_created_at']:
                veto('input log does not cover session creation')
            # An older row from another thread says nothing about this thread's
            # retained inputs. Only its own boundary can prove initial coverage.
            elif (batch['thread_first_at'] is None
                  or batch['thread_first_at'] > facts['session_created_at']):
                veto('thread input log does not cover session creation')
            else:
                self.state['initial_coverage'] = True
        elif batch['first_id'] is None or batch['first_id'] > self.state['cursor'] + 1:
            veto('input log retention passed unobserved cursor')
        # Codex prunes per thread as well as globally. Another thread's old
        # row cannot prove that this thread retained its unread submissions.
        last_thread = self.state.get('last_thread_row')
        if last_thread is not None and (batch['thread_first_id'] is None
                                       or batch['thread_first_id'] > last_thread):
            veto('thread input log retention passed observed boundary')
        if batch['high'] < self.state['cursor']:
            veto('input log high watermark regressed')
        if facts.get('receipt_overflow'):
            veto('native receipt overflow')
        entries = self.state['entries']
        for log_id, sec, nano, size, body in rows:
            if body is None or not body.startswith(f'session_loop{{thread_id={self.sid}}}: Submission sub='):
                continue
            # Inspect only the operation field in the native prefix. Quoted
            # text can contain TurnInput and must not create an extra input.
            try:
                outer = _debug_fields(body.split('Submission sub=', 1)[1], 'Submission')
                if not outer.get('op', '').startswith('TurnInput {'):
                    continue
            except (ValueError, TypeError):
                pass  # A truncated native submission remains an unknown fence.
            if len(entries) >= MAX_ENTRIES and str(log_id) not in entries:
                if 'entry limit' not in self.state['unknown']:
                    self.state['unknown'].append('entry limit')
                caught_up = False
                break
            entry = dict(log_id=log_id, at=sec + nano / 1e9,
                         fence={'log_id': log_id, 'seconds': sec, 'nanos': nano},
                         receipt=None)
            try:
                if size > MAX_BODY:
                    raise ValueError('oversized native input')
                entry.update(parse_submission(body, self.sid))
            except (TypeError, ValueError, KeyError) as exc:
                entry['unknown'] = str(exc)
            previous = entries.get(str(log_id))
            if previous and any(previous.get(k) != entry.get(k) for k in
                                ('fence', 'payload_sha256', 'payload_bytes', 'unknown')):
                raise RuntimeError('original input evidence changed')
            entries.setdefault(str(log_id), entry)
        if caught_up or len(entries) < MAX_ENTRIES:
            self.state['cursor'] = batch['cursor']
            if rows:
                self.state['last_thread_row'] = rows[-1][0]
        used = {e['receipt']['offset'] for e in entries.values() if e.get('receipt')}
        for entry in sorted(entries.values(), key=lambda e: (e['at'], e['log_id'])):
            if entry.get('unknown'):
                continue
            if entry.get('receipt'):
                try:
                    verify_receipt(self.rollout.path, facts['file_id'], entry, entry['receipt'])
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    veto(f"original input receipt {entry['log_id']} invalid: {exc}")
                continue
            identity = (entry['payload_sha256'], entry['payload_bytes'])
            for receipt in self.rollout.native_inputs.get(identity, []):
                # Do not use event_msg, substrings, a pre-fence echo or a receipt
                # already assigned to another identical submission.
                if receipt['offset'] not in used and receipt_matches(entry, receipt):
                    try:
                        verify_receipt(self.rollout.path, facts['file_id'], entry, receipt)
                        entry['receipt'] = dict(receipt)
                        used.add(receipt['offset'])
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        veto(f"native input receipt {entry['log_id']} invalid: {exc}")
                    break
        # Any native input without a surviving source row makes initial
        # coverage unproven. An old receipt must not hide a pruned pending row.
        unmatched_receipts = (facts['caught_up'] and caught_up and any(
            r['offset'] not in used for values in self.rollout.native_inputs.values() for r in values))
        intent_guard = (self.intent_index.observe(self.sid, self.rollout, submissions=entries, final=final)
                        if self.intent_index is not None else
                        dict(safe=False, caught_up=False, entries={}, unknown=True))
        self.state['intents'] = intent_guard['entries']
        self.save()  # Failed persistence must never return permission to control.
        pending = [e['log_id'] for e in entries.values() if not e.get('receipt')]
        unknown = bool(self.state['unknown'] or any(e.get('unknown') for e in entries.values())
                       or unmatched_receipts or intent_guard['unknown'])
        # Cursor advancement due to ordinary logging does not change the input
        # token. An additional source, receipt or uncertainty always does.
        identity = {k: self.state[k] for k in ('binding', 'entries', 'intents', 'unknown', 'initial_coverage')}
        token = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        current = caught_up and facts['caught_up'] and intent_guard['caught_up']
        return dict(safe=current and self.state['initial_coverage'] and not pending
                    and not unknown and intent_guard['safe'], caught_up=current,
                    pending=pending, unknown=unknown, token=token, entries=len(entries),
                    initial_coverage=self.state['initial_coverage'],
                    unmatched_native_receipts=bool(unmatched_receipts),
                    intent_pending=intent_guard.get('pending', []))


def clear_guard(snapshot):
    guard = snapshot.get('input_guard')
    return bool(isinstance(guard, dict) and guard.get('safe') is True
                and guard.get('caught_up') is True and guard.get('token')
                and not guard.get('pending') and not guard.get('unknown'))

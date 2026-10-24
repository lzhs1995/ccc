"""Shared, read-only index of MAC input intents for compact admission.

The index never settles a delivery, replays input, or reads arbitrary task trees.
Journal discovery is bounded and incremental. In-place attempt changes and new
successor event files invalidate a completed sweep. Unresolved callback roots
survive reservation release and process restart.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time

from ccc_compact_inputs import receipt_matches, text_identity, verify_receipt
from ccc_compact_support import sync_directory, owned_file, OwnedDirectory, validate_native_receipt_fields

STATE_ROOT = Path.home() / '.local/state/multi-agent-collaboration'
ACTIVE_ROOT = Path('/tmp/multi-agent-collaboration/_active')
JOURNALS = ('message-dispatch-v1', 'task-dispatch-v1', 'successor-dispatch-v2')
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_FILES = 32768
MAX_ENTRIES = 16384
MAX_EVENTS = 256
INDEX_VERSION = 2
DERIVED_PINS = frozenset(('fence', 'binding_fence', 'journal_file_id', 'journal_parent_id'))
ATTEMPT = re.compile(r'attempt-(\d{4,})\.json\Z')
EVENT = re.compile(r'event-(\d{4,})\.json\Z')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode()


def signature(path):
    st = path.lstat()
    if (st.st_uid != os.geteuid() or st.st_mode & 0o022
            or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode))):
        raise ValueError('untrusted journal file or directory')
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def read_json(path, *, limit=MAX_FILE_BYTES):
    before = signature(path)
    if before[2] > limit:
        raise ValueError('journal record exceeds bounded read')
    with owned_file(path) as (stream, held):
        if [held.st_dev, held.st_ino] != before[:2]:
            raise ValueError('journal identity changed')
        raw = stream.read(limit + 1)
    if signature(path) != before or len(raw) > limit:
        raise ValueError('journal changed during read')
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    if not isinstance(value, dict):
        raise ValueError('journal object required')
    return value, before


def legacy_binding(binding):
    if binding.get('schema') != 'native-successor-delivery-v1':
        return binding
    result = dict(binding, schema='native-delivery-v1')
    identity = binding['identity']
    result['identity'] = {k: identity[v] for k, v in (
        ('workspace_uuid', 'target_workspace_uuid'), ('caller_surface_uuid', 'caller_surface_uuid'),
        ('target_surface_uuid', 'target_surface_uuid'), ('target_pane_uuid', 'target_pane_uuid'))}
    return result


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def fence_shape(pin):
    offset = pin.get('offset')
    if (type(offset) is not int or offset < 0
            or type(pin.get('device')) is not int or type(pin.get('inode')) is not int
            or type(pin.get('at_record_boundary')) is not bool
            or pin.get('prefix_size') != min(offset, 4096)
            or pin.get('tail_at') != max(0, offset - 4096)
            or any(not isinstance(pin.get(k), str) or not re.fullmatch(r'[0-9a-f]{64}', pin[k])
                   for k in ('prefix_sha256', 'tail_sha256'))):
        raise ValueError('invalid original transcript fence')


def verify_fences(entry):
    with owned_file(entry['path']) as (stream, st):
        if [st.st_dev, st.st_ino] != entry['file_id']:
            raise ValueError('original intent transcript replaced')
        for pin in (entry['binding_fence'], entry['fence']):
            fence_shape(pin)
            if st.st_size < pin['offset']:
                raise ValueError('original intent transcript truncated')
            stream.seek(0)
            if digest(stream.read(pin['prefix_size'])) != pin['prefix_sha256']:
                raise ValueError('original transcript prefix changed')
            stream.seek(pin['tail_at'])
            tail = stream.read(pin['offset'] - pin['tail_at'])
            if digest(tail) != pin['tail_sha256']:
                raise ValueError('original transcript fence tail changed')
            if pin['at_record_boundary'] and tail and not tail.endswith(b'\n'):
                raise ValueError('original transcript fence not at record boundary')


def normalize_intent(path, binding, event):
    """Check the original native request and its original pre-input EOF fence."""
    # successor_native_delivery.input_event_fields receives _legacy_binding,
    # so its persisted fence hashes that exact projection, not the outer
    # successor authorization object. Keep the projection byte-compatible.
    binding = legacy_binding(binding)
    request, fence = event['native_request'], event['native_input_fence']
    params, peer = request['params'], request['socket']
    content = params.get('input')
    if (binding.get('schema') != 'native-delivery-v1' or binding.get('provider') != 'codex'
            or request.get('transport') != 'codex-native-input-v1'
            or params.get('threadId') != binding.get('session_id')
            or not isinstance(content, list) or len(content) != 1
            or set(content[0]) != {'type', 'text'} or content[0]['type'] != 'text'
            or not isinstance(content[0]['text'], str)):
        raise ValueError('unproven native request identity or payload')
    expected = None
    if request.get('method') == 'turn/steer':
        expected = params.get('expectedTurnId')
        if not isinstance(expected, str) or not expected or set(params) != {'threadId', 'input', 'expectedTurnId'}:
            raise ValueError('unproven original steer turn')
    elif request.get('method') != 'turn/start' or set(params) != {'threadId', 'input'}:
        raise ValueError('unsupported native input operation')
    if (peer.get('uid') != os.geteuid() or peer.get('peer_uid') != os.geteuid()
            or peer.get('mode') != 0o600 or type(peer.get('device')) is not int
            or type(peer.get('inode')) is not int or not Path(peer.get('path', '')).is_absolute()):
        raise ValueError('missing authenticated control socket proof')
    at, captured, bound = event['at_epoch'], fence.get('captured_at_epoch'), binding.get('bound_at_epoch')
    if (not all(finite(v) for v in (at, captured, bound)) or not bound <= captured <= at
            or fence.get('schema') != 'native-input-fence-v1'
            or fence.get('binding_sha256') != digest(json.dumps(binding, sort_keys=True).encode())
            or fence.get('native_request_sha256') != digest(canonical(request))):
        raise ValueError('native input intent hash or time fence mismatch')
    pin, original = fence['transcript'], binding['transcript']
    fence_shape(pin)
    fence_shape(original)
    if (any(pin.get(k) != original.get(k) for k in ('path', 'device', 'inode'))
            or pin.get('at_record_boundary') is not True
            or type(pin.get('offset')) is not int or pin['offset'] < original['offset']
            or not Path(pin.get('path', '')).is_absolute()):
        raise ValueError('native input EOF fence changed')
    sha, size = text_identity(content[0]['text'])
    if binding.get('payload_sha256') != sha:
        raise ValueError('native input body changed')
    return dict(session_id=binding['session_id'], path=pin['path'],
                file_id=[pin['device'], pin['inode']], source='user',
                at=at, minimum_offset=pin['offset'], receipt_turn_id=expected,
                method=request['method'], payload_sha256=sha, payload_bytes=size,
                intent_sha256=digest(canonical([binding, event])),
                journal=str(path), fence=pin, binding_fence=original, receipt=None)


class IntentIndex:
    def __init__(self, path, *, state_root=STATE_ROOT, active_root=ACTIVE_ROOT,
                 max_files=MAX_FILES, max_steps=256, budget_seconds=.2):
        self.path, self.root = Path(path), Path(state_root)
        # /tmp itself is a platform symlink on macOS; the owned active tree is
        # inspected separately and no task-directory recursion is permitted.
        self.active = Path(active_root).resolve()
        self.max_files, self.max_steps, self.budget = max_files, max_steps, budget_seconds
        self.lock = threading.RLock()
        binding = dict(root=str(self.root), active=str(self.active))
        self.state = dict(version=INDEX_VERSION, binding=binding, entries={},
                          callback_roots={}, unknown={}, migration_pending=[])
        if self.path.exists():
            saved, _ = read_json(self.path, limit=MAX_INDEX_BYTES)
            if saved.get('version') not in (1, INDEX_VERSION) or saved.get('binding') != binding:
                raise ValueError('intent index binding changed')
            if any(not isinstance(saved.get(k), dict) for k in ('entries', 'callback_roots', 'unknown')):
                raise ValueError('invalid persisted intent index')
            if saved['version'] == 1:
                # Never discard pending or settled originals on upgrade. A v1
                # entry stays unsafe until the same journal and transcript
                # supply its missing derived pins and validate its receipt.
                saved['migration_pending'] = list(saved['entries'])
                saved['version'] = INDEX_VERSION
            migrating = saved.get('migration_pending')
            if (not isinstance(migrating, list)
                    or any(not isinstance(k, str) or k not in saved['entries'] for k in migrating)):
                raise ValueError('invalid intent index migration')
            self.state = saved
        self.files, self.directories = {}, {}
        self.scan = None
        self.validation = None
        self.successors = {}
        self.ready = False
        self.saved = None
        self.last_scan = 0.0
        self.errors = {}

    def freeze_for_control(self, request):
        """Keep shared input evidence fixed until this original native request ends.

        The caller holds ``lock`` through final authority creation. A durable
        lease also blocks observation writes after service restart; it never
        creates another submission budget or expires an unknown request.
        """
        key = request.get('attemptId')
        if (not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{24}', key)
                or not Path(request.get('intentPath', '')).is_absolute()):
            raise ValueError('control freeze requires the original bound request')
        directory = self.path.parent / 'native-control-pins-v4'
        directory.mkdir(mode=0o700, exist_ok=True)
        with OwnedDirectory(directory) as owned:
            owned.write_once(key + '.json', dict(schema='ccc-native-control-pin-v4',
                client_request=request, replay_authorized=False))

    def control_is_pending(self):
        """No shared index writes while a native reader can still validate its pins."""
        directory = self.path.parent / 'native-control-pins-v4'
        if not directory.exists():
            return False
        with OwnedDirectory(directory):
            paths = list(directory.iterdir())
            if len(paths) > 1024:
                raise ValueError('native control pin ledger exceeds its bound')
            for path in paths:
                if not re.fullmatch(r'[0-9a-f]{24}\.json', path.name):
                    raise ValueError('unknown native control pin entry')
                lease, _ = read_json(path)
                request = lease.get('client_request') or {}
                key = path.stem
                if (lease.get('schema') != 'ccc-native-control-pin-v4'
                        or request.get('attemptId') != key or lease.get('replay_authorized') is not False):
                    raise ValueError('native control pin binding changed')
                intent = Path(request.get('intentPath', ''))
                if (not intent.is_absolute() or intent.parent.parent != self.path.parent
                        or intent.name != key + '-interrupt.json'):
                    raise ValueError('native control pin is outside the enrolled journal')
                receipt_path = intent.with_name(key + '-compact-started.json')
                if not receipt_path.exists():
                    denial = None
                    for phase in ('not-interrupted', 'compact-denied'):
                        candidate = intent.with_name(key + '-' + phase + '.json')
                        if candidate.exists():
                            denial, _ = read_json(candidate)
                            break
                    if denial is None:
                        return True
                    fields = denial.get('fields')
                    if (denial.get('episode_key') != key
                            or denial.get('original_intent') != str(intent)
                            or not isinstance(fields, dict)
                            or fields.get('turnId') != request.get('expectedTurnId')
                            or fields.get('attemptId') != key):
                        raise ValueError('native denial receipt is not for the original request')
                    validate_native_receipt_fields(fields, phase, request)
                    continue
                receipt, _ = read_json(receipt_path)
                fields = receipt.get('fields')
                if (receipt.get('episode_key') != key
                        or receipt.get('original_intent') != str(intent)
                        or not isinstance(fields, dict) or 'turnId' not in fields):
                    raise ValueError('native terminal receipt is not for the original request')
                validate_native_receipt_fields(fields, 'compact-started', request)
        return False

    def save(self):
        with self.lock:
            raw = canonical(self.state) + b'\n'
            if len(raw) > MAX_INDEX_BYTES:
                raise ValueError('intent index limit; no partial persistence')
            if raw == self.saved:
                return
            if self.control_is_pending():
                raise RuntimeError('original native control receipt pending; input evidence pinned')
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with OwnedDirectory(self.path.parent) as directory:
                directory.atomic_bytes(self.path.name, raw)
            self.saved = raw

    def _children(self, path):
        try:
            pin = signature(path)
        except FileNotFoundError:
            self.directories[str(path)] = None
            return
        self.directories[str(path)] = pin
        with os.scandir(path) as stream:
            for child in stream:
                yield Path(child.path)

    def _walk(self):
        # Depth and filenames are structural protocol limits, not a recursive
        # search of Documents or a fresh scan per watched session.
        for family in JOURNALS:
            for directory in self._children(self.root / family):
                yield None
                if directory.is_dir() and not directory.is_symlink():
                    for path in self._children(directory):
                        if ATTEMPT.fullmatch(path.name) or (family == 'successor-dispatch-v2'
                                                           and path.name == 'attempt.json'):
                            yield ('attempt', path)
                        elif family == 'successor-dispatch-v2' and EVENT.fullmatch(path.name):
                            yield ('event', path)
                        else:
                            yield None
                    if family == 'successor-dispatch-v2':
                        yield ('successor', directory)
        for directory in self._children(self.active):
            yield None
            if directory.is_dir() and not directory.is_symlink():
                for marker in self._children(directory):
                    yield ('marker', marker) if marker.suffix == '.json' else None
        for root in list(self.state['callback_roots']):
            for path in self._children(Path(root)):
                yield ('attempt', path) if ATTEMPT.fullmatch(path.name) else None

    def _callback(self, path, value):
        if value.get('marker_version') != 2:
            raise ValueError('unsupported active reservation marker')
        if (value.get('collaboration_id') != path.stem
                or value.get('workspace_uuid') != path.parent.name):
            raise ValueError('active reservation identity mismatch')
        participants = value.get('participants')
        if not isinstance(participants, list):
            raise ValueError('missing active reservation participants')
        supervisors = [p for p in participants if p.get('role') == 'supervisor' and p.get('provider') == 'codex']
        if not supervisors:
            return
        if len(supervisors) != 1 or not supervisors[0].get('native_session_id'):
            raise ValueError('ambiguous callback supervisor')
        sid = supervisors[0]['native_session_id']
        root = Path(value['artifact_root'])
        if not root.is_absolute():
            raise ValueError('callback artifact root must be absolute')
        pack_path = root / 'task-pack.json'
        # A preflight reservation can precede the formal task pack. Track the
        # missing path so later pack creation invalidates the completed sweep.
        if not pack_path.exists():
            self.files[str(pack_path)] = None
            return
        pack, pin = read_json(pack_path)
        self.files[str(pack_path)] = pin
        if pack.get('task_id') != value['task_id']:
            raise ValueError('task pack disagrees with active reservation')
        receipt = Path(pack['completion_receipt'])
        if not receipt.is_absolute() or not receipt.is_relative_to(root):
            raise ValueError('callback receipt outside authenticated artifact root')
        journal = receipt.with_name(receipt.stem + '-attempts')
        old = self.state['callback_roots'].get(str(journal))
        record = dict(session_id=sid, artifact_root=str(root), task_id=value['task_id'],
                      marker=str(path), marker_sha256=digest(canonical(value)),
                      pack_sha256=digest(canonical(pack)))
        # Marker activity timestamps change; the original task/endpoint do not.
        if old and any(old.get(k) != record.get(k) for k in ('session_id', 'task_id', 'artifact_root', 'pack_sha256')):
            raise ValueError('callback discovery binding changed')
        self.state['callback_roots'].setdefault(str(journal), record)

    def _attempt(self, path, attempt, *, events=None):
        key = str(path)
        previous = self.state['entries'].get(key)
        binding = attempt.get('native_binding')
        if binding and binding.get('provider') == 'claude':
            if previous:
                raise ValueError('original native intent provider changed')
            return
        if events is None:
            events = attempt.get('events', [])
        if not isinstance(events, list) or len(events) > MAX_EVENTS:
            raise ValueError('invalid intent event sequence')
        original = [event for event in events if event.get('phase') in ('NATIVE_INPUT_INTENT', 'PASTE_INTENT')]
        if not original:
            if previous:
                raise ValueError('original native intent event disappeared')
            # No operation was authorized yet. The file remains watched for
            # the in-place addition of an intent before transport begins.
            return
        if len(original) != 1 or original[0].get('phase') != 'NATIVE_INPUT_INTENT' or not binding:
            raise ValueError('legacy or ambiguous input requires original evidence')
        entry = normalize_intent(path, binding, original[0])
        entry['journal_file_id'] = self.files[str(path)][:2]
        entry['journal_parent_id'] = signature(path.parent)[:2]
        if previous:
            migrating = key in self.state['migration_pending']
            if any(previous.get(k) != v for k, v in entry.items()
                   if k not in ('receipt', 'receipt_turn_id')
                   and not (migrating and k in DERIVED_PINS and k not in previous)):
                raise ValueError('original native intent changed in place')
            if entry['receipt_turn_id'] and previous['receipt_turn_id'] != entry['receipt_turn_id']:
                raise ValueError('original steer turn changed')
            if migrating:
                restored = dict(entry, **previous)
                verify_fences(restored)
                if restored.get('receipt'):
                    verify_receipt(restored['path'], restored['file_id'], restored, restored['receipt'])
                self.state['entries'][key] = restored
                self.state['migration_pending'].remove(key)
        elif len(self.state['entries']) >= MAX_ENTRIES:
            raise ValueError('intent entry limit; no eviction')
        else:
            self.state['entries'][key] = entry
        self.state['unknown'].pop(key, None)

    def _inspect(self, kind, path):
        if len(self.files) >= self.max_files and str(path) not in self.files:
            raise ValueError('journal discovery limit; no partial safe result')
        try:
            if kind == 'successor':
                group = self.successors[str(path)]
                value = group['attempt']
                self._attempt(path / 'attempt.json', value,
                              events=[e for _, e in sorted(group['events'].items())])
                return
            value, pin = read_json(path)
            self.files[str(path)] = pin
            if kind == 'marker':
                self._callback(path, value)
            elif kind == 'event':
                # Read each immutable event once, then normalize this attempt
                # once after its directory traversal. No sibling reread loop.
                group = self.successors.setdefault(str(path.parent), {'events': {}})
                if len(group['events']) >= MAX_EVENTS:
                    raise ValueError('successor event limit')
                group['events'][path.name] = value
            elif path.name == 'attempt.json':
                self.successors.setdefault(str(path.parent), {'events': {}})['attempt'] = value
            else:
                self._attempt(path, value)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            sid = None
            if 'value' in locals():
                sid = (value.get('native_binding') or {}).get('session_id')
            if sid is None:
                sid = self.state['entries'].get(str(path), {}).get('session_id')
            if sid is None:
                sid = self.state['callback_roots'].get(str(path.parent), {}).get('session_id')
            self.errors[str(path)] = dict(session_id=sid, reason=str(exc))

    def _pins(self):
        return itertools.chain(self.files.items(), self.directories.items())

    @staticmethod
    def _same(name, pin):
        try:
            current = signature(Path(name))
        except FileNotFoundError:
            current = None
        return current == pin

    def _start_scan(self):
        self.files, self.directories, self.errors, self.successors = {}, {}, {}, {}
        self.scan, self.validation, self.ready = self._walk(), None, False

    def refresh(self, *, final=False):
        if self.scan is None and self.validation is None:
            if self.ready:
                self.validation = self._pins()
            else:
                self._start_scan()
        deadline = time.monotonic() + self.budget
        for _ in range(self.max_steps):
            if time.monotonic() > deadline:
                break
            try:
                if self.scan is not None:
                    item = next(self.scan)
                    if item:
                        self._inspect(*item)
                elif not self._same(*next(self.validation)):
                    self._start_scan()
                    break
            except StopIteration:
                if self.scan is not None:
                    self.scan = None
                    self.validation = self._pins()
                    # Settled records still need their original evidence after
                    # restart; deleting a journal is not a settlement operation.
                    for name, entry in self.state['entries'].items():
                        if name not in self.files:
                            self.state['unknown'][name] = dict(session_id=entry['session_id'],
                                reason='original intent disappeared')
                else:
                    self.validation = None
                    self.last_scan, self.ready = time.monotonic(), True
                    break
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.errors['discovery'] = dict(session_id=None, reason=str(exc))
                self.scan, self.validation, self.ready = None, None, False
                break
        current = self.ready and self.scan is None
        if current and final:
            # Recovery calls this while holding both input writer locks. The
            # background sweep may span ticks; it alone cannot authorize input
            # interruption. Validate the entire completed index under the lock.
            deadline = time.monotonic() + 2
            for name, pin in self._pins():
                if time.monotonic() > deadline or not self._same(name, pin):
                    self._start_scan()
                    return False
            self.validation = None
        return current

    def observe(self, sid, rollout, *, submissions=None, final=False):
        with self.lock:
            if self.control_is_pending():
                raise RuntimeError('original native control receipt pending; input evidence pinned')
            caught_up = self.refresh(final=final)
            facts = rollout.snapshot()
            entries = {key: value for key, value in self.state['entries'].items()
                       if value['session_id'] == sid}
            errors = {k: v for k, v in {**self.state['unknown'], **self.errors}.items()
                      if v.get('session_id') in (None, sid)}
            for key in self.state['migration_pending']:
                if self.state['entries'][key].get('session_id') == sid:
                    errors[key] = dict(reason='original evidence required for index migration')
            used = {entry['receipt']['offset'] for entry in entries.values() if entry.get('receipt')}
            source_used = {entry.get('submission_log_id') for entry in entries.values()
                           if entry.get('submission_log_id') is not None}
            for key, entry in sorted(entries.items(), key=lambda pair: (pair[1]['at'], pair[0])):
                if entry['path'] != facts['path'] or entry['file_id'] != facts['file_id']:
                    errors[key] = dict(reason='original intent rollout identity changed')
                    continue
                try:
                    verify_fences(entry)
                    if (signature(Path(entry['journal']))[:2] != entry['journal_file_id']
                            or signature(Path(entry['journal']).parent)[:2] != entry['journal_parent_id']):
                        raise ValueError('original intent journal replaced')
                    if entry.get('receipt'):
                        verify_receipt(entry['path'], entry['file_id'], entry, entry['receipt'])
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    errors[key] = dict(reason=str(exc))
                    continue
                if entry.get('receipt'):
                    continue
                matches = [e for e in (submissions or {}).values()
                           if e.get('source') == 'user' and not e.get('unknown')
                           and e.get('payload_sha256') == entry['payload_sha256']
                           and e.get('payload_bytes') == entry['payload_bytes']
                           and e['at'] >= entry['at'] and e['log_id'] not in source_used
                           and (entry['receipt_turn_id'] is None
                                or e.get('receipt_turn_id') == entry['receipt_turn_id'])]
                if matches:
                    source = min(matches, key=lambda e: (e['at'], e['log_id']))
                    entry['submission_log_id'] = source['log_id']
                    source_used.add(source['log_id'])
                    entry['receipt_turn_id'] = source['receipt_turn_id']
                if not entry['receipt_turn_id']:
                    continue  # turn/start needs the actual native Submission.
                identity = (entry['payload_sha256'], entry['payload_bytes'])
                for receipt in rollout.native_inputs.get(identity, []):
                    if receipt['offset'] not in used and receipt_matches(entry, receipt):
                        try:
                            verify_receipt(entry['path'], entry['file_id'], entry, receipt)
                            entry['receipt'] = dict(receipt)
                            used.add(receipt['offset'])
                        except (OSError, ValueError, KeyError, TypeError) as exc:
                            errors[key] = dict(reason=str(exc))
                        break
            self.save()
            pending = [key for key, entry in entries.items() if not entry.get('receipt')]
            unknown = bool(errors or facts.get('receipt_overflow'))
            return dict(safe=caught_up and facts['caught_up'] and not pending and not unknown,
                        caught_up=caught_up, entries=entries, pending=pending,
                        unknown=unknown, reasons=errors)

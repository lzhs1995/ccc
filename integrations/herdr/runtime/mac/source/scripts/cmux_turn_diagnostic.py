"""Observe interruption of the original steer target; never infer message loss.

The receipt scanner has its own cursor and remains the sole receipt authority.
This separate, bounded cursor starts at the original input-intent fence. An
abort belongs to the request's expectedTurnId, not whichever turn is now live.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import time

import cmux_native_delivery as native
from cmux_evidence_io import read_bytes

SCHEMA = 'original-codex-turn-diagnostic-v1'
MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 256


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def _abort(raw, offset, binding, event):
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(record, dict) or record.get('type') != 'event_msg':
        return None
    body = record.get('payload')
    expected = event['native_request']['params']['expectedTurnId']
    if (not isinstance(body, dict) or body.get('type') != 'turn_aborted'
            or body.get('turn_id') != expected):
        return None
    stamp = native._timestamp(record.get('timestamp'))
    earliest = event['at_epoch']
    # Native records can truncate timestamps to milliseconds. The original EOF
    # fence excludes all records that already existed at intent publication.
    precision = re.search(r'\.(\d{3,6})(?:Z|[+-]\d{2}:\d{2})$',
                          str(record.get('timestamp', '')))
    if precision:
        scale = 10 ** len(precision[1])
        earliest = math.floor(earliest * scale) / scale
    if stamp is None or stamp < earliest or stamp > time.time() + 2:
        return None
    pin = binding['transcript']
    reason = body.get('reason')
    return dict(session_id=binding['session_id'], expected_turn_id=expected,
                path=pin['path'], device=pin['device'], inode=pin['inode'],
                offset=offset, length=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                timestamp=record['timestamp'], event_type='turn_aborted',
                reason=reason[:256] if isinstance(reason, str) else None)


def _scan(binding, text, event, fence, old, *, max_bytes):
    fenced = native._fenced_binding(binding, text, fence, event['at_epoch'])
    pin = fenced['transcript']
    start = old.get('next_offset', pin['offset'])
    if type(start) is not int or start < pin['offset']:
        raise ValueError('TURN_DIAGNOSTIC_CURSOR_INVALID')
    with native._open(pin['path']) as stream:
        native._check_file(stream, binding)
        end = native._check_file(stream, fenced)
        if start > end:
            raise ValueError('TURN_DIAGNOSTIC_CURSOR_TRUNCATED')
        proof = old.get('abort_proof')
        if proof is not None:
            if (not isinstance(proof, dict) or type(proof.get('offset')) is not int
                    or proof['offset'] < pin['offset']
                    or type(proof.get('length')) is not int
                    or not 0 < proof['length'] <= native.MAX_RECORD
                    or proof['offset'] + proof['length'] > end):
                raise ValueError('TURN_DIAGNOSTIC_PROOF_INVALID')
            stream.seek(proof['offset'] - 1)
            if stream.read(1) != b'\n':
                raise ValueError('TURN_DIAGNOSTIC_PROOF_BOUNDARY_CHANGED')
            raw = stream.read(proof['length'])
            if (not raw.endswith(b'\n')
                    or _abort(raw, proof['offset'], binding, event) != proof):
                raise ValueError('TURN_DIAGNOSTIC_PROOF_CHANGED')
            native._check_file(stream, binding)
            native._check_file(stream, fenced)
            return dict(status='ORIGINAL_TURN_ABORTED', abort_proof=proof,
                        next_offset=start, scanned_bytes=0, scanned_records=0)
        if start > pin['offset']:
            stream.seek(start - 1)
            if stream.read(1) != b'\n':
                raise ValueError('TURN_DIAGNOSTIC_CURSOR_BOUNDARY_CHANGED')
        stream.seek(start)
        scanned, records = 0, 0
        status, proof = 'NO_ABORT_OBSERVED', None
        if start == pin['offset'] and not pin['at_record_boundary']:
            raw = stream.readline(native.MAX_RECORD + 1)
            if len(raw) > native.MAX_RECORD:
                raise ValueError('TURN_DIAGNOSTIC_RECORD_LIMIT')
            if not raw.endswith(b'\n'):
                stream.seek(start)
                status = 'PARTIAL_RECORD'
            else:
                scanned += len(raw)
        while status != 'PARTIAL_RECORD' and stream.tell() < end:
            if scanned >= max_bytes or records >= MAX_RECORDS:
                status = 'SCAN_BUDGET_EXHAUSTED'
                break
            offset = stream.tell()
            # Finish at most one record beyond the byte budget; never skip
            # part of a long record and thereby lose the next event boundary.
            raw = stream.readline(min(native.MAX_RECORD + 1, end - offset))
            if len(raw) > native.MAX_RECORD:
                raise ValueError('TURN_DIAGNOSTIC_RECORD_LIMIT')
            if not raw.endswith(b'\n'):
                stream.seek(offset)
                status = 'PARTIAL_RECORD'
                break
            scanned += len(raw)
            records += 1
            proof = _abort(raw, offset, binding, event)
            if proof:
                status = 'ORIGINAL_TURN_ABORTED'
                break
        next_offset = stream.tell()
        native._check_file(stream, binding)
        native._check_file(stream, fenced)
    return dict(status=status, abort_proof=proof, next_offset=next_offset,
                scanned_bytes=scanned, scanned_records=records)


def observe(binding, text, events, attempt_path, state_path, *,
            screen_hash_length=16, max_bytes=MAX_SCAN_BYTES):
    """Called under the original controller's journal lock and live identity.

Only the new cursor may be written. Diagnostic failure cannot downgrade a
validated receipt or create authority for a second input.
"""
    result = dict(schema=SCHEMA, input_operations=0, proves_non_reception=False,
                  replay_authorized=False,
                  instruction='Observe the original attempt only. A turn abort does not '
                              'prove permanent message loss or authorize replay.')
    try:
        event, fence = native.original_input_event(events, binding, text,
                                                   screen_hash_length=screen_hash_length)
        if (binding['provider'] != 'codex' or event['phase'] != 'NATIVE_INPUT_INTENT'
                or event['native_request']['method'] != 'turn/steer'):
            return dict(result, status='NOT_APPLICABLE', reason='ORIGINAL_STEER_REQUIRED')
        if type(max_bytes) is not int or not 0 < max_bytes <= MAX_SCAN_BYTES:
            raise ValueError('TURN_DIAGNOSTIC_BUDGET_INVALID')
        expected = event['native_request']['params']['expectedTurnId']
        result.update(session_id=binding['session_id'], expected_turn_id=expected,
                      native_request_sha256=fence['native_request_sha256'])
        path, cursor = Path(attempt_path), Path(state_path)
        if not path.is_absolute() or not cursor.is_absolute() or cursor == path:
            raise ValueError('TURN_DIAGNOSTIC_SEPARATE_ABSOLUTE_CURSOR_REQUIRED')
        pins = dict(schema=SCHEMA, attempt=str(path), binding_sha256=_digest(binding),
                    original_intent_sha256=_digest(event),
                    native_request_sha256=fence['native_request_sha256'])
        old = json.loads(read_bytes(cursor)) if cursor.exists() else {}
        if not isinstance(old, dict) or (old and any(old.get(k) != v for k, v in pins.items())):
            raise ValueError('TURN_DIAGNOSTIC_BINDING_CHANGED')
        observed = _scan(binding, text, event, fence, old, max_bytes=max_bytes)
        from cmux_callback_journal import write_json
        write_json(cursor, dict(observed, **pins))
        return dict(result, **observed)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        return dict(result, status='OBSERVATION_UNAVAILABLE', error_type=type(exc).__name__)

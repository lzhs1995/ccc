"""Describe retained original control evidence without authorizing recovery."""
import json
import time
from pathlib import Path

from ccc_compact_runtime import ControlLockBusy
from ccc_compact_support import OwnedDirectory, validate_native_receipt_fields


class ControlEvidenceBusy(ControlLockBusy):
    """Another original request pins shared evidence; this target consumed nothing."""


def record_not_transmitted(worker, request, error):
    episode = getattr(worker, 'state', {}).get('episode') or {}
    if episode.get('phase') != 'control_pending' or episode.get('key') != request.get('attemptId'):
        return
    # A pre-existing submit record may belong to an earlier attempted transport.
    # Its uncertainty must not be relabeled by a later failed duplicate call.
    if worker.journal_directory._file_identity(request['attemptId'] + '-client-submit.json') is not None:
        return
    value = dict(schema='ccc-native-control-not-transmitted-v1',
                 client_request=request, status='NOT_TRANSMITTED',
                 reason=str(error)[:500], at=time.time(), replay_authorized=False)
    name = request['attemptId'] + '-client-not-sent.json'
    try:
        worker.journal_directory.write_once(name, value)
    except FileExistsError:
        raw = worker.journal_directory.read_bytes(name, limit=2 * 1024 * 1024)
        old = json.loads(raw)
        if (old.get('schema') != value['schema'] or old.get('client_request') != request
                or old.get('status') != 'NOT_TRANSMITTED' or old.get('replay_authorized') is not False):
            raise ValueError('original untransmitted marker changed')
    worker.journal.record('CONDITIONAL_NOT_TRANSMITTED_EVIDENCE_RETAINED',
                         key=request['attemptId'], reason=str(error)[:500],
                         replay_authorized=False, evidence_released=False)


def control_health(index, *, worker=None):
    """Report why active shared pins remain frozen, including before-send failure.

    This is separate operator health, never a native terminal receipt. A marker
    written before transport does not release input evidence or refund an intent.
    """
    if index is None:
        return None
    with index.lock:
        if not index.control_is_pending():
            return _local_control_health(worker)
        active = index.path.parent / 'native-control-pins-v4'
        with OwnedDirectory(active) as owned:
            for path in sorted(active.iterdir()):
                lease = json.loads(owned.read_bytes(path.name, limit=2 * 1024 * 1024))
                request = lease['client_request']
                key = request['attemptId']
                intent = Path(request['intentPath'])
                terminal = False
                with OwnedDirectory(intent.parent) as journal:
                    for phase in ('compact-started', 'not-interrupted', 'compact-denied'):
                        try:
                            receipt = json.loads(journal.read_bytes(key + '-' + phase + '.json',
                                                                    limit=2 * 1024 * 1024))
                        except FileNotFoundError:
                            continue
                        if receipt.get('episode_key') != key or receipt.get('original_intent') != str(intent):
                            raise ValueError('control health terminal binding changed')
                        validate_native_receipt_fields(receipt.get('fields'), phase, request)
                        terminal = True
                        break
                    if terminal:
                        continue
                    try:
                        marker = json.loads(journal.read_bytes(key + '-client-not-sent.json',
                                                               limit=2 * 1024 * 1024))
                    except FileNotFoundError:
                        marker = None
                    if marker is not None:
                        if (marker.get('schema') != 'ccc-native-control-not-transmitted-v1'
                                or marker.get('client_request') != request
                                or marker.get('status') != 'NOT_TRANSMITTED'
                                or marker.get('replay_authorized') is not False):
                            raise ValueError('control health untransmitted binding changed')
                    submit = journal._file_identity(key + '-client-submit.json') is not None
                return dict(state=('pinned_not_transmitted' if marker is not None
                                   else 'pinned_before_submission' if not submit
                                   else 'native_receipt_pending'),
                            key=key, session_id=request.get('threadId'),
                            client_submit_present=submit, input_evidence_frozen=True,
                            replay_authorized=False, evidence_released=False)
        # Native writers do not take the ledger lock. A terminal receipt
        # can land between the pending scan and these reads.
        return _local_control_health(worker)


def _local_control_health(worker):
    # A consumed intent may fail before the durable shared pin is published.
    # Keep that target observe-only and expose its retained local evidence.
    if worker is None:
        return None
    episode = getattr(worker, 'state', {}).get('episode') or {}
    if episode.get('phase') != 'control_pending':
        return None
    key = episode.get('key')
    with OwnedDirectory(worker.directory) as journal:
        raw = journal.read_bytes(key + '-interrupt.json', limit=2 * 1024 * 1024)
        original = json.loads(raw)
        request = original.get('client_request')
        if (not isinstance(request, dict) or request.get('attemptId') != key
                or request.get('intentPath') != str(worker.directory / (key + '-interrupt.json'))
                or request.get('threadId') != worker.target['session_id']):
            raise ValueError('local consumed intent binding changed')
        for phase in ('compact-started', 'not-interrupted', 'compact-denied'):
            try:
                receipt = json.loads(journal.read_bytes(key + '-' + phase + '.json',
                                                       limit=2 * 1024 * 1024))
            except FileNotFoundError:
                continue
            if receipt.get('episode_key') != key or receipt.get('original_intent') != request['intentPath']:
                raise ValueError('local consumed intent terminal binding changed')
            validate_native_receipt_fields(receipt.get('fields'), phase, request)
            return None
        try:
            marker = json.loads(journal.read_bytes(key + '-client-not-sent.json',
                                                   limit=2 * 1024 * 1024))
        except FileNotFoundError:
            marker = None
        if marker is not None and (
                marker.get('schema') != 'ccc-native-control-not-transmitted-v1'
                or marker.get('client_request') != request
                or marker.get('status') != 'NOT_TRANSMITTED'
                or marker.get('replay_authorized') is not False):
            raise ValueError('local untransmitted marker binding changed')
        submit = journal._file_identity(key + '-client-submit.json') is not None
    return dict(state=('pinned_not_transmitted' if marker is not None
                       else 'pinned_before_submission' if not submit else 'native_receipt_pending'),
                key=key, session_id=request['threadId'], client_submit_present=submit,
                input_evidence_frozen=False, original_intent_retained=True,
                replay_authorized=False, evidence_released=False)

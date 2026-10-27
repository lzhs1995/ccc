"""Validate native reception against an existing callback journal; never send.

validate() only produces evidence. Explicit reconcile_received() may publish a
receipt under the original journal lock after live receiver authentication.
Neither path sends input, migrates a controller, changes availability or disarms.
"""
import hashlib
import json
import os
import fcntl
import stat
from pathlib import Path

from delivery_receipts import ReceiptError, native_user_record, snapshot
import cmux_native_delivery as delivery


def reconcile_received(pack_path, bridge, *, transcript, line):
    """Receiver-side settlement of the existing journal, with zero terminal input.

    Preserve all attempts. Publish only after authenticating the live receiver,
    its native session, and immutable original journal evidence under its lock.
    This is an explicit operation, never a fallback in the send path.
    """
    from delivery_receipts import bind, publish
    from availability_contract import require_action
    session = os.environ.get('CODEX_THREAD_ID')
    transcript = Path(transcript)
    if (not session or not transcript.is_absolute() or transcript.is_symlink()
            or not transcript.resolve().is_relative_to((Path.home() / '.codex/sessions').resolve())):
        raise ReceiptError('authenticated receiver native session required')
    pack_path = Path(pack_path)
    pack = bridge.validate_task_pack_contract(pack_path)
    require_action(pack['task_id'], 'callback', pack)
    live = bind(pack, bridge, receiver=True)
    receipt = Path(pack['completion_receipt'])
    journal = receipt.with_name(receipt.stem + '-attempts')
    if Path(str(receipt) + '.pending.json').exists():
        raise ReceiptError('legacy pending cannot be migrated')
    # Open the original inode only. Never create a parallel delivery lock.
    lock_path = journal / 'delivery.lock'
    fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        def check_lock():
            held, current = os.fstat(fd), lock_path.lstat()
            if (not stat.S_ISREG(current.st_mode)
                    or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
                raise ReceiptError('original callback lock was replaced')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReceiptError('original callback delivery still in progress') from exc
        check_lock()
        if receipt.exists():
            raise ReceiptError('receipt already exists; preserve it')
        attempts = sorted(journal.glob('attempt-*.json'))
        if not attempts:
            raise ReceiptError('original callback attempt required')
        evidence = validate(pack_path, attempts[-1], transcript, line, session)
        old = json.loads(attempts[-1].read_text())
        delivery.require_bound(bridge, pack['callback_target'], old['native_binding'],
                               pack['completion_callback'], read_only=True)
        original = old['binding']['identity']
        if (live['caller_surface_uuid'] != original['target_surface_uuid']
                or live['target_surface_uuid'] != original['caller_surface_uuid']
                or live['workspace_uuid'] != original['workspace_uuid']):
            raise ReceiptError('live receiver differs from original participants')
        if (bind(pack, bridge, receiver=True) != live
                or validate(pack_path, attempts[-1], transcript, line, session) != evidence):
            raise ReceiptError('native settlement evidence changed')
        require_action(pack['task_id'], 'callback', pack)
        check_lock()
        value = {k: v for k, v in old['binding'].items() if k != 'identity'}
        delivery.require_bound(bridge, pack['callback_target'], old['native_binding'],
                               pack['completion_callback'], read_only=True)
        value.update(confirmed=True, confirmation_source='native_user_message_v1',
                     reconciled_read_only=True, attempt=str(attempts[-1]),
                     native_evidence=evidence, receiver_identity=live, input_operations=0,
                     native_binding=old['native_binding'], native_proof=evidence['native_proof'])
        publish(receipt, value)
        return value
    finally:
        os.close(fd)


def validate(pack_path, attempt_path, transcript, line, session_id):
    pack_path, attempt_path = Path(pack_path), Path(attempt_path)
    pack_pin = snapshot(pack_path)
    pack = json.loads(pack_path.read_text())
    report_pin = snapshot(pack['report'])
    attempt_pin = snapshot(attempt_path)
    receipt = Path(pack['completion_receipt'])
    journal = receipt.with_name(receipt.stem + '-attempts')
    attempts = sorted(journal.glob('attempt-*.json'))
    if not attempts or attempts[-1] != attempt_path:
        raise ReceiptError('must validate latest original callback attempt')
    attempt = json.loads(attempt_path.read_text())
    try:
        intent_at, fence = delivery._original_intent(
            attempt, attempt.get('native_binding'), pack['completion_callback'])
    except delivery.NativeDeliveryError as exc:
        raise ReceiptError(str(exc)) from exc
    original_native = attempt['native_binding']
    if (original_native['session_id'] != session_id
            or original_native['transcript']['path'] != str(transcript)):
        raise ReceiptError('native transcript differs from original binding')
    binding = attempt.get('binding', {})
    expected = dict(task_id=pack['task_id'], completion_nonce=pack['completion_nonce'],
                    completion_callback=pack['completion_callback'],
                    callback_target=pack['callback_target'],
                    task_pack_sha256=pack_pin['sha256'], report=pack['report'],
                    report_sha256=report_pin['sha256'], report_bytes=report_pin['bytes'])
    if any(binding.get(k) != v for k, v in expected.items()):
        raise ReceiptError('original callback binding changed')
    gate_pin = snapshot(pack['identity_gate'])
    gate = json.loads(Path(pack['identity_gate']).read_text())
    identity = binding.get('identity', {})
    if (gate.get('status') != 'PASS' or gate.get('task_id') != pack['task_id']
            or identity.get('caller_surface_uuid') != pack['executor_uuid']
            or identity.get('target_surface_uuid') != gate.get('supervisor_surface_uuid')
            or identity.get('workspace_uuid') != gate.get('workspace_uuid')
            or not identity.get('target_pane_uuid')):
        raise ReceiptError('original participant binding changed')
    events = attempt.get('events', [])
    if fence['schema'] == 'native-paste-fence-v1':
        # Historical terminal attempts retain their own original evidence.
        pastes = [e for e in events if e.get('phase') == 'PASTE_INTENT']
        enters = [e for e in events if e.get('phase') == 'ENTER_INTENT']
        if (len(pastes) != 1 or len(enters) != 1
                or events.index(pastes[0]) >= events.index(enters[0])):
            raise ReceiptError('original paste and Enter intent required')
        if hashlib.sha256(pastes[0]['screen'].encode('utf-8', 'replace')).hexdigest()[:16] != pastes[0].get('screen_sha256'):
            raise ReceiptError('original screen hash mismatch')
    native = native_user_record(transcript, line, session_id=session_id,
                                exact_text=pack['completion_callback'], after=pack['finalized_at'])
    proof = dict(schema=delivery.SCHEMA, session_id=session_id,
                 **{k: native[k] for k in ('path', 'device', 'inode', 'offset', 'length')},
                 sha256=native['record_sha256'], reception_kind='native_user_message',
                 payload_sha256=original_native['payload_sha256'])
    if not delivery.validate_proof(original_native, pack['completion_callback'], proof,
                                   not_before=intent_at, paste_fence=fence):
        raise ReceiptError('original native input fence does not prove this user record')
    if (snapshot(pack_path) != pack_pin or snapshot(pack['report']) != report_pin
            or snapshot(attempt_path) != attempt_pin or snapshot(pack['identity_gate']) != gate_pin
            or sorted(journal.glob('attempt-*.json')) != attempts):
        raise ReceiptError('evidence changed during validation')
    return dict(status='EXACT_NATIVE_RECEPTION', pack=pack_pin, report=report_pin,
                attempt=attempt_pin, gate=gate_pin, native=native, native_proof=proof,
                input_operations=0, receipt_created=False,
                scope='Reception evidence only; original controller settlement remains required')

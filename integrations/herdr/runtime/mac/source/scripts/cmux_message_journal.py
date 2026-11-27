"""Durable ordinary messages: ambiguous sends may only be reconciled read-only."""
import contextlib
import fcntl
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from cmux_callback_journal import write_json
import cmux_native_delivery as native
from cmux_evidence_io import read_bytes, attempt_paths, open_regular_lock, snapshot
import cmux_prompt_reference as prompt_reference


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def original_body_pin(text, attempt):
    """Recheck the body's original file identity; never re-pin a saved attempt."""
    reference = prompt_reference.wire_reference(text)
    if reference is None:
        if 'body_reference' in attempt or 'body_pin' in attempt:
            raise ValueError('MESSAGE_BODY_BINDING_CHANGED')
        return None
    if (attempt.get('body_reference') != reference
            or not isinstance(attempt.get('body_pin'), dict)):
        raise ValueError('MESSAGE_BODY_ORIGINAL_PIN_REQUIRED')
    pin = prompt_reference.validate_wire_body(text)
    if pin != attempt['body_pin']:
        raise ValueError('MESSAGE_BODY_CHANGED: preserve the original attempt')
    return pin


def verified_receipt(bridge, surface, text, marker):
    """Read only: revalidate original dispatch evidence, never synthesize a receipt."""
    from delivery_receipts import snapshot
    try:
        if not isinstance(marker, str) or not marker or marker not in text:
            return None
        live = bridge.pin_workspace(surface)
        identity = {k: live[k] for k in ('workspace_uuid', 'caller_surface_uuid',
                                        'target_surface_uuid', 'target_pane_uuid')}
        if not all(isinstance(v, str) and v for v in identity.values()):
            return None
        binding = dict(identity=identity, marker=marker, payload_sha256=digest(text))
        root = Path.home() / ".local/state/multi-agent-collaboration/message-dispatch-v1"
        journal = root / digest(json.dumps([identity["caller_surface_uuid"], marker], sort_keys=True))
        receipt_path = journal / 'receipt.json'
        receipt_pin = snapshot(receipt_path)
        receipt = json.loads(read_bytes(receipt_path))
        attempts = attempt_paths(journal)
        if not attempts or receipt.get('attempt') != str(attempts[-1]):
            return None
        attempt_path = attempts[-1]
        attempt_pin = snapshot(attempt_path)
        attempt = json.loads(read_bytes(attempt_path))
        if (receipt.get('confirmed') is not True or receipt.get('binding') != binding
                or attempt.get('binding') != binding):
            return None
        body_pin = original_body_pin(text, attempt)
        if body_pin is not None and (
                receipt.get('body_reference') != attempt['body_reference']
                or receipt.get('confirmation_scope') != 'reference_notice'
                or receipt.get('body_read_confirmed') is not False):
            return None
        native._original_intent(attempt, attempt.get('native_binding'), text)
        pins = [(receipt_path, receipt_pin), (attempt_path, attempt_pin)]
        if receipt.get('reconciled_read_only') is True:
            observation_path = Path(receipt['observation'])
            if observation_path.parent != journal or not observation_path.name.startswith('observation-'):
                return None
            observation_pin = snapshot(observation_path)
            observation = json.loads(read_bytes(observation_path))
            if type(observation.get('input_operations')) is not int or observation['input_operations'] != 0:
                return None
            pins.append((observation_path, observation_pin))
        received_at = native.receipt_received_at(bridge, surface, text, attempt, receipt)
        if received_at is None:
            return None
        if (attempt_paths(journal) != attempts or any(snapshot(p) != pin for p, pin in pins)
                or bridge.pin_workspace(surface) != live
                or original_body_pin(text, attempt) != body_pin):
            return None
        proof = dict(source='revalidated_message_dispatch_v1', identity=identity,
                     attempt=attempt_pin, receipt=receipt_pin,
                     native_received_at_epoch=received_at)
        if body_pin is not None:
            proof.update(body_pin=body_pin, confirmation_scope='reference_notice',
                         body_read_confirmed=False)
        return proof
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return None


def verified_no_input(bridge, surface, text, marker, *, commit=None):
    """Prove an original message never reached input intent; never retry it.

    Absence of evidence is not NO_INPUT. Every original attempt must positively
    record NO_INPUT with no events, under the unchanged request binding. Hold the
    existing journal lock and recheck the complete evidence set before returning.
    An optional adapter-bookkeeping commit runs under that same lock; a failed
    commit propagates, rather than authorizing a new delivery attempt.
    """
    committing = False
    try:
        if not isinstance(marker, str) or not marker or marker not in text:
            return None
        live = bridge.pin_workspace(surface)
        identity = {k: live[k] for k in ('workspace_uuid', 'caller_surface_uuid',
                                        'target_surface_uuid', 'target_pane_uuid')}
        if not all(isinstance(v, str) and v for v in identity.values()):
            return None
        binding = dict(identity=identity, marker=marker, payload_sha256=digest(text))
        root = Path.home() / '.local/state/multi-agent-collaboration'
        key = digest(json.dumps([identity['caller_surface_uuid'], marker], sort_keys=True))
        journal = root / 'message-dispatch-v1' / key
        legacy = root / 'deliveries-v1' / (key + '.json')
        receipt = journal / 'receipt.json'
        lock_path = journal / 'delivery.lock'
        # Read-only open: a missing journal/lock cannot manufacture zero-input
        # proof or overwrite a historical controller's ownership.
        with os.fdopen(os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as lock:
            held, current = os.fstat(lock.fileno()), lock_path.lstat()
            if (not stat.S_ISREG(held.st_mode) or held.st_nlink != 1
                    or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
                return None
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if os.path.lexists(legacy) or os.path.lexists(receipt):
                return None
            paths = attempt_paths(journal)
            if not paths or len(paths) > 2 or paths != [
                    journal / ('attempt-%04d.json' % n) for n in range(1, len(paths) + 1)]:
                return None
            pins, proofs, body_pins = [], [], []
            for path in paths:
                raw, pin = snapshot(path)
                attempt = json.loads(raw)
                if (attempt.get('binding') != binding or attempt.get('phase') != 'NO_INPUT'
                        or attempt.get('events') != [] or 'result' in attempt):
                    return None
                body_pins.append((attempt, original_body_pin(text, attempt)))
                pins.append((path, raw, pin))
                proofs.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                    device=pin[0], inode=pin[1], size=pin[2], mtime_ns=pin[3], ctime_ns=pin[4]))
            current = lock_path.lstat()
            if (attempt_paths(journal) != paths or os.path.lexists(legacy)
                    or os.path.lexists(receipt) or bridge.pin_workspace(surface) != live
                    or (held.st_dev, held.st_ino, 1) != (current.st_dev, current.st_ino, current.st_nlink)
                    or any(snapshot(path) != (raw, pin) for path, raw, pin in pins)
                    or any(original_body_pin(text, attempt) != pin for attempt, pin in body_pins)):
                return None
            proof = dict(source='revalidated_message_no_input_v1', binding=binding,
                         attempts=proofs, input_operations=0)
            if commit is not None:
                committing = True
                commit(proof)
            return proof
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        if committing:
            raise
        return None


def deliver(bridge, surface, text, marker, confirm_lines=200, *, reconcile_only=False,
            recover_stranded=False, wait_seconds=None):
    from cmux_confirmation import (wait_budget, controller_provenance, attempt_details,
                                   annotate_attempt_error, observe_attempt)
    seconds = wait_budget(wait_seconds, reconcile_only=reconcile_only)
    if not isinstance(marker, str) or not marker or marker not in text:
        raise bridge.TaskPackContractError('MESSAGE_MARKER_REQUIRED')
    if bridge._looks_like_task_dispatch(text):
        raise bridge.TaskPackContractError('TASK_PACK_REQUIRED')
    proof = bridge.pin_workspace(surface)
    keys = ('workspace_uuid', 'caller_surface_uuid', 'target_surface_uuid', 'target_pane_uuid')
    identity = {k: proof[k] for k in keys}
    if not all(isinstance(v, str) and v for v in identity.values()):
        raise bridge.TaskPackContractError('MESSAGE_IDENTITY_REQUIRED')
    binding = dict(identity=identity, marker=marker, payload_sha256=digest(text))
    body_reference = prompt_reference.wire_reference(text)
    body_pin = None
    root = Path.home() / '.local/state/multi-agent-collaboration'
    key = digest(json.dumps([identity['caller_surface_uuid'], marker], sort_keys=True))
    journal = root / 'message-dispatch-v1' / key
    journal.mkdir(parents=True, exist_ok=True, mode=0o700)

    def recheck():
        live = bridge.pin_workspace(surface)
        if any(live.get(k) != v for k, v in identity.items()):
            raise bridge.TaskPackContractError('MESSAGE_IDENTITY_CHANGED')
        try:
            if body_pin is not None and prompt_reference.validate_wire_body(text) != body_pin:
                raise ValueError('MESSAGE_BODY_CHANGED')
        except (OSError, ValueError) as exc:
            raise bridge.TaskPackContractError(str(exc)) from exc

    with contextlib.ExitStack() as stack:
        # Share the existing generic sender lock, so old and new controllers
        # cannot race on the same receiver. Old attempts retain their controller.
        legacy_root = root / 'deliveries-v1'
        legacy_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        task_root = root / 'task-dispatch-v1'
        task_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (legacy_root / ('pane-' + digest(identity['workspace_uuid'] + ':' + identity['target_pane_uuid']) + '.lock'),
                     legacy_root / ('target-' + digest(identity['target_surface_uuid']) + '.lock'),
                     task_root / ('target-' + digest(identity['target_surface_uuid']) + '.lock'),
                     journal / 'delivery.lock'):
            lock = stack.enter_context(open_regular_lock(path))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise bridge.TaskPackContractError('MESSAGE_IN_PROGRESS') from exc
        legacy = legacy_root / (key + '.json')
        if legacy.exists():
            raise bridge.TaskPackContractError('ORIGINAL_DELIVERY_CONTROLLER_REQUIRED: ' + str(legacy))
        attempts = attempt_paths(journal)
        old = json.loads(read_bytes(attempts[-1])) if attempts else None
        if old and old.get('binding') != binding:
            raise bridge.TaskPackContractError('MESSAGE_BINDING_CHANGED')
        if old:
            try:
                body_pin = original_body_pin(text, old)
            except (OSError, ValueError) as exc:
                raise bridge.TaskPackContractError(str(exc)) from exc
        if (journal / 'receipt.json').exists():
            raise bridge.TaskPackContractError('MESSAGE_RECEIPT_EXISTS: no duplicate send')
        recheck()
        if recover_stranded:
            # Enter is not delivery: an earlier attempt pasted once and pressed
            # Enter, but the payload stayed in the receiver composer. Allow one
            # extra key on that same attempt, never a second paste.
            if reconcile_only or not old:
                raise bridge.TaskPackContractError('NO_STRANDED_MESSAGE')
            phases = [e['phase'] for e in old.get('events', [])]
            submitted_or_declined = ('ENTER_SENT' in phases or
                ((old.get('native_binding') or {}).get('receipt_contract') == 'native-user-marker-v1'
                 and 'ENTER_NOT_SENT' in phases))
            if (phases.count('PASTE_INTENT') != 1 or not submitted_or_declined
                    or old.get('phase') in ('PREPARED', 'NO_INPUT', 'CONFIRMED')):
                raise bridge.TaskPackContractError('NO_STRANDED_MESSAGE')
            if 'QUEUE_TAB_INTENT' in phases or 'EXTRA_ENTER_INTENT' in phases:
                raise bridge.TaskPackContractError('STRANDED_RECOVERY_ALREADY_USED: reconcile_only')
            paste = next(e for e in old['events'] if e['phase'] == 'PASTE_INTENT')
            if paste.get('screen_sha256') != bridge.screen_hash(paste['screen']):
                raise bridge.TaskPackContractError('MESSAGE_EVIDENCE_CHANGED')
            attempt_path, attempt = attempts[-1], old

            def observe_recovery(phase, screen=None):
                recheck()
                event = dict(phase=phase, at_epoch=time.time(), recovery='stranded_compose')
                if screen is not None:
                    event.update(screen=screen, screen_sha256=bridge.screen_hash(screen))
                attempt['events'].append(event)
                attempt['phase'] = phase
                write_json(attempt_path, attempt)

            try:
                result = bridge.recover_stranded_once(surface, text, marker, paste['screen'],
                    confirm_lines=confirm_lines, delivery_observer=observe_recovery,
                    native_binding=old.get('native_binding'), native_attempt=attempt_path,
                    wait_seconds=seconds)
            except BaseException as exc:
                annotate_attempt_error(exc, attempt_path, attempt)
                attempt['recovery_error'] = str(exc)
                attempt['recovery_error_details'] = exc.details
                write_json(attempt_path, attempt)
                raise
            attempt['phase'] = 'CONFIRMED'
            write_json(attempt_path, attempt)
        elif reconcile_only:
            if not old or old.get('phase') in ('PREPARED', 'NO_INPUT'):
                raise bridge.TaskPackContractError('NO_SUBMITTED_MESSAGE')
            native._original_intent(old, old.get('native_binding'), text)
            result = observe_attempt(lambda: native.scan_original(
                bridge, surface, text, attempts[-1], old.get('native_binding')),
                attempts[-1], old, seconds, reconcile_only=True)
            observation = journal / ('observation-' + str(time.time_ns()) + '.json')
            write_json(observation, dict(result, input_operations=0, at_epoch=time.time()))
            if result.get('confirmed') is not True:
                raise bridge.DispatchUnconfirmed('MESSAGE_NOT_YET_CONFIRMED: no resend',
                    state=bridge.native_submission_state(result), details=result)
            attempt_path = attempts[-1]
            result = dict(result, reconciled_read_only=True, observation=str(observation))
        else:
            if old and old.get('phase') != 'NO_INPUT':
                raise bridge.TaskPackContractError('MESSAGE_ATTEMPT_EXISTS: use reconcile_only')
            if len(attempts) >= 2:
                raise bridge.TaskPackContractError('MESSAGE_RETRY_BUDGET_EXHAUSTED')
            # Fresh ordinary messages must fit before any paste. Existing
            # native reconciliation and dedicated task/callback paths keep
            # their original exact payload and contract.
            try:
                prompt_reference.require_inline(text)
                body_pin = prompt_reference.validate_wire_body(text)
            except (OSError, ValueError) as exc:
                raise bridge.TaskPackContractError(str(exc)) from exc
            attempt_path = journal / ('attempt-%04d.json' % (len(attempts) + 1))
            attempt = dict(binding=binding, phase='PREPARED', events=[],
                           controller_provenance=controller_provenance(__file__))
            if body_reference is not None:
                attempt.update(body_reference=body_reference, body_pin=body_pin)
            write_json(attempt_path, attempt)

            def observe(phase, screen=None, *, evidence=None):
                recheck()
                # Refuse stale nonce before the very first mutation.
                if phase == 'PASTE_INTENT' and ''.join(marker.split()) in ''.join(screen.split()):
                    raise bridge.TaskPackContractError('MESSAGE_MARKER_ALREADY_VISIBLE')
                fence = native.capture_paste_fence(attempt['native_binding']) if phase == 'PASTE_INTENT' else None
                fields = native.input_event_fields(attempt['native_binding'], text, phase, evidence)
                event = dict(fields, phase=phase, at_epoch=time.time())
                if fence is not None:
                    event['native_paste_fence'] = fence
                if screen is not None:
                    event.update(screen=screen, screen_sha256=bridge.screen_hash(screen))
                attempt['events'].append(event)
                attempt['phase'] = phase
                write_json(attempt_path, attempt)

            try:
                attempt['native_binding'] = native.bind_target(bridge, surface, text)
                attempt['native_binding'] = native.marker_binding(attempt['native_binding'], text, marker)
                write_json(attempt_path, attempt)
                result = bridge._submit_text_once(surface, text, marker=marker,
                    confirm_lines=confirm_lines, force_compose=False, delivery_observer=observe,
                    native_binding=attempt['native_binding'], native_attempt=attempt_path,
                    wait_seconds=seconds)
                if result.get('confirmed') is not True:
                    raise bridge.DispatchUnconfirmed('MESSAGE_NOT_CONFIRMED',
                        state=bridge.native_submission_state(result), details=result)
            except BaseException as exc:
                if attempt['phase'] == 'PREPARED':
                    attempt['phase'] = 'NO_INPUT'
                annotate_attempt_error(exc, attempt_path, attempt)
                attempt['error'] = str(exc)
                attempt['error_details'] = exc.details
                write_json(attempt_path, attempt)
                raise
            attempt['phase'] = 'CONFIRMED'
            write_json(attempt_path, attempt)
        recheck()
        original = json.loads(read_bytes(attempt_path))
        result = attempt_details(result, attempt_path, original, reconcile_only=reconcile_only)
        if not native.receipt_evidence(bridge, surface, text, original, result):
            raise bridge.DispatchUnconfirmed('MESSAGE_NATIVE_PROOF_REQUIRED',
                state=bridge.native_submission_state(result), details=result)
        recheck()
        result = dict(result, binding=binding, attempt=str(attempt_path), at_epoch=time.time())
        if body_reference is not None:
            result.update(confirmation_scope='reference_notice', body_read_confirmed=False,
                          body_reference=body_reference)
        receipt = journal / 'receipt.json'
        write_json(receipt, result)
        return dict(result, receipt=str(receipt))

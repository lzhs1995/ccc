"""Durable callback delivery; reconciliation never writes to a terminal."""
import fcntl
import contextlib
import hashlib
import json
import os
import time
from pathlib import Path
import cmux_native_delivery as native
from cmux_evidence_io import read_bytes, attempt_paths, open_regular_lock


def write_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def verified_receipt(bridge, surface, task_pack_path):
    """Validate the original journal read-only; never send or create a receipt."""
    from delivery_receipts import snapshot
    from cmux_submit_confirmation_guard import submission_target
    try:
        path = Path(task_pack_path)
        if not path.is_absolute():
            return None
        pins = {path: snapshot(path)}
        pack = bridge.validate_task_pack_contract(path)
        if submission_target(pack['callback_target']) != surface:
            return None
        report = Path(pack['report'])
        pins[report] = snapshot(report)
        binding = dict(task_id=pack['task_id'], completion_nonce=pack['completion_nonce'],
                       completion_callback=pack['completion_callback'],
                       callback_target=pack['callback_target'], task_pack_sha256=pins[path]['sha256'],
                       report=str(report), report_sha256=pins[report]['sha256'],
                       report_bytes=report.stat().st_size)
        receipt_path = Path(pack['completion_receipt'])
        pins[receipt_path] = snapshot(receipt_path)
        receipt = json.loads(read_bytes(receipt_path))
        journal = receipt_path.with_name(receipt_path.stem + '-attempts')
        attempts = attempt_paths(journal)
        if not attempts or receipt.get('attempt') != str(attempts[-1]):
            return None
        pins[attempts[-1]] = snapshot(attempts[-1])
        attempt = json.loads(read_bytes(attempts[-1]))
        identity = attempt['binding']['identity']
        if (set(identity) != set(native.IDENTITY_KEYS)
                or not all(isinstance(v, str) and v for v in identity.values())
                or identity['caller_surface_uuid'].upper() != pack['executor_uuid'].upper()):
            return None
        live = native.observer_identity(bridge, surface, identity)
        if (receipt.get('confirmed') is not True
                or any(receipt.get(k) != v for k, v in binding.items())
                or attempt.get('binding') != dict(binding, identity=identity)):
            return None
        native._original_intent(attempt, attempt.get('native_binding'), pack['completion_callback'])
        if receipt.get('confirmation_source') != 'native_user_message_v1':
            return None
        if receipt.get('reconciled_read_only') is True and 'native_evidence' in receipt:
            from callback_native_evidence import validate
            evidence = receipt['native_evidence']
            record = evidence['native']
            receiver = receipt['receiver_identity']
            if (type(receipt.get('input_operations')) is not int
                    or receipt['input_operations'] != 0
                    or receiver['caller_surface_uuid'] != identity['target_surface_uuid']
                    or receiver['target_surface_uuid'] != identity['caller_surface_uuid']
                    or receiver['workspace_uuid'] != identity['workspace_uuid']
                    or validate(path, attempts[-1], record['path'], record['line'],
                                record['session_id']) != evidence):
                return None
        elif receipt.get('reconciled_read_only') is True:
            observed = Path(receipt['observation'])
            if observed.parent != journal or not observed.name.startswith('observation-'):
                return None
            pins[observed] = snapshot(observed)
            observation = json.loads(read_bytes(observed))
            if type(observation.get('input_operations')) is not int or observation['input_operations'] != 0:
                return None
        if not native.receipt_evidence(bridge, surface, pack['completion_callback'], attempt, receipt, read_only=True):
            return None
        if (attempt_paths(journal) != attempts or any(snapshot(p) != pin for p, pin in pins.items())
                or native.observer_identity(bridge, surface, identity) != live):
            return None
        return dict(source='revalidated_callback_journal', identity=identity,
                    pack=pins[path], report=pins[report], receipt=pins[receipt_path])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        return None


def deliver(bridge, task_pack_path, confirm_lines=200, *, reconcile_only=False,
            resume_queue_only=False, wait_seconds=None, recover_stranded=False):
    from cmux_confirmation import (wait_budget, controller_provenance, attempt_details,
                                   annotate_attempt_error, observe_attempt, RECEIPT_DIAGNOSTICS)
    seconds = wait_budget(wait_seconds, reconcile_only=reconcile_only)
    if sum(map(bool, (reconcile_only, resume_queue_only, recover_stranded))) > 1:
        raise bridge.TaskPackContractError("CONFLICTING_RECOVERY_MODES")
    from availability_contract import require_action
    pack = bridge.validate_task_pack_contract(task_pack_path)
    require_action(pack['task_id'], 'callback', pack)
    report = Path(pack['report'])
    if not report.is_file():
        raise bridge.TaskPackContractError('COMPLETION_CALLBACK_REFUSED: report must exist first')
    receipt_path = Path(pack['completion_receipt'])
    journal = receipt_path.with_name(receipt_path.stem + '-attempts')
    journal.mkdir(exist_ok=True)
    # Persistent inode; a second process must never race the same callback.
    with contextlib.ExitStack() as stack:
        lock_path = journal / 'delivery.lock'
        lock = stack.enter_context(open_regular_lock(lock_path))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise bridge.TaskPackContractError('CALLBACK_IN_PROGRESS: preserve original attempt') from exc
        if receipt_path.exists():
            raise bridge.TaskPackContractError('COMPLETION_RECEIPT_EXISTS: refusing duplicate callback')
        # Pre-journal releases persisted this file before any terminal input.
        # Its presence means delivery may already have happened, even if its
        # contents are truncated. Never silently migrate by sending again.
        legacy = Path(str(receipt_path) + '.pending.json')
        if legacy.exists():
            raise bridge.TaskPackContractError(
                'LEGACY_CALLBACK_PENDING: preserve original; supervisor must '
                'review actual receipt evidence without resending')
        attempts = attempt_paths(journal)
        old = json.loads(read_bytes(attempts[-1])) if attempts else None

        def observe_original_identity(original_identity):
            try:
                return native.observer_identity(bridge, pack['callback_target'], original_identity)
            except native.NativeDeliveryError as exc:
                raise bridge.TaskPackContractError('CALLBACK_BINDING_CHANGED: ' + str(exc)) from exc

        if reconcile_only and old:
            identity = old['binding']['identity']
            if set(identity) != set(native.IDENTITY_KEYS):
                raise bridge.TaskPackContractError('CALLBACK_ORIGINAL_IDENTITY_REQUIRED')
            proof = observe_original_identity(identity)
        else:
            proof = bridge.pin_workspace(pack['callback_target'])
            identity = {k: proof[k] for k in native.IDENTITY_KEYS}

        def current_identity():
            if reconcile_only:
                return observe_original_identity(identity)
            return bridge.pin_workspace(pack['callback_target'])
        target_root = Path.home() / '.local/state/multi-agent-collaboration/deliveries-v1'
        target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        pane_key = hashlib.sha256((identity['workspace_uuid'] + ':' + identity['target_pane_uuid']).encode()).hexdigest()
        pane_lock_path = target_root / ('pane-' + pane_key + '.lock')
        pane_lock = stack.enter_context(open_regular_lock(pane_lock_path))
        try:
            fcntl.flock(pane_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise bridge.TaskPackContractError('CALLBACK_PANE_IN_PROGRESS') from exc
        target_key = hashlib.sha256(identity['target_surface_uuid'].encode()).hexdigest()
        target_lock_path = target_root / ('target-' + target_key + '.lock')
        target_lock = stack.enter_context(open_regular_lock(target_lock_path))
        try:
            fcntl.flock(target_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise bridge.TaskPackContractError('CALLBACK_TARGET_IN_PROGRESS') from exc
        if (not pack.get('executor_uuid') or
                identity['caller_surface_uuid'].upper() != pack['executor_uuid'].upper()):
            raise bridge.TaskPackContractError('CALLBACK_WRONG_EXECUTOR: exact original executor required')
        binding = {
            'task_id': pack['task_id'], 'completion_nonce': pack['completion_nonce'],
            'completion_callback': pack['completion_callback'],
            'callback_target': pack['callback_target'],
            'task_pack_sha256': bridge._sha256_file(task_pack_path),
            'report': str(report), 'report_sha256': bridge._sha256_file(report),
            'report_bytes': report.stat().st_size,
            'identity': identity,
        }
        if old and old['binding'] != binding:
            raise bridge.TaskPackContractError('CALLBACK_BINDING_CHANGED: preserve previous attempt')
        if recover_stranded:
            if not old or (old.get('native_binding') or {}).get('receipt_contract') != 'native-user-marker-v1':
                raise bridge.TaskPackContractError('DIRECT_ORIGINAL_MARKER_CONTRACT_REQUIRED')
            attempt_path = attempts[-1]
            def observe_recovery(phase, screen=None):
                if (bridge.pin_workspace(pack['callback_target']) != proof
                        or bridge._sha256_file(report) != binding['report_sha256']
                        or bridge._sha256_file(task_pack_path) != binding['task_pack_sha256']):
                    raise bridge.TaskPackContractError('RECOVERY_BINDING_CHANGED')
                require_action(pack['task_id'], 'callback', pack)
                for held, held_path in ((lock, lock_path), (pane_lock, pane_lock_path),
                                        (target_lock, target_lock_path)):
                    opened, current = os.fstat(held.fileno()), os.lstat(held_path)
                    if (opened.st_dev, opened.st_ino, opened.st_nlink) != (current.st_dev, current.st_ino, 1):
                        raise bridge.TaskPackContractError('RECOVERY_LOCK_CHANGED')
                if json.loads(read_bytes(attempt_path)) != old:
                    raise bridge.TaskPackContractError('RECOVERY_JOURNAL_CHANGED')
                event = dict(phase=phase, at_epoch=time.time(), recovery='direct_marker')
                if screen is not None:
                    event.update(screen=screen, screen_sha256=bridge.screen_hash(screen))
                old['events'].append(event)
                old['phase'] = phase
                write_json(attempt_path, old)
            try:
                result = bridge.recover_stranded_once(pack['callback_target'], pack['completion_callback'],
                    pack['completion_nonce'], '', confirm_lines=confirm_lines,
                    delivery_observer=observe_recovery, native_binding=old['native_binding'],
                    native_attempt=attempt_path, wait_seconds=seconds)
            except BaseException as exc:
                annotate_attempt_error(exc, attempt_path, old)
                old.update(recovery_error=str(exc), recovery_error_details=exc.details)
                write_json(attempt_path, old)
                raise
            old.update(phase='CONFIRMED', result=result, ended_at_epoch=time.time())
            write_json(attempt_path, old)
            evidence = {'reconciled_read_only': False, 'attempt': str(attempt_path)}
        elif resume_queue_only:
            # 原生核验先于任何恢复意图；旧版本无基线不能事后补造。
            if not old:
                raise bridge.TaskPackContractError('NO_RECOVERABLE_ENTER_ATTEMPT')
            prior = native.scan_original(bridge, pack['callback_target'], pack['completion_callback'],
                                         attempts[-1], old.get('native_binding'))
            if prior.get('confirmed') is True:
                raise bridge.TaskPackContractError('ALREADY_RECEIVED: use --reconcile-only; no input')
            if prior.get('state') == 'NATIVE_QUEUED':
                raise bridge.DispatchUnconfirmed('ALREADY_QUEUED: use --reconcile-only; no input')
            # Resume only a recorded Enter that left this exact payload in the
            # measured Codex composer. Never paste, Enter again, or retry Tab.
            events = old.get('events', []) if old else []
            phases = [e.get('phase') for e in events]
            if (not old or phases.count('PASTE_INTENT') != 1
                    or phases.count('ENTER_INTENT') != 1
                    or 'POST_ENTER_OBSERVATION' not in phases
                    or any('TAB' in str(p) or p == 'EXTRA_ENTER_INTENT' for p in phases)):
                raise bridge.TaskPackContractError('NO_RECOVERABLE_ENTER_ATTEMPT')
            if any(not isinstance(e.get('screen'), str) or
                   e.get('screen_sha256') != bridge.screen_hash(e['screen'])
                   for e in events if 'screen' in e or 'screen_sha256' in e):
                raise bridge.TaskPackContractError('RECOVERY_SCREEN_EVIDENCE_CHANGED')
            attempt_path = attempts[-1]

            def observe_recovery(phase, screen=None):
                # 未知、变化或排队的观察同样落盘，恢复原样也不能重新取得资格。
                if json.loads(read_bytes(attempt_path)) != old:
                    raise bridge.TaskPackContractError('RECOVERY_JOURNAL_CHANGED')
                event = dict(phase=phase, at_epoch=time.time(), recovery='queue_resume')
                if screen is not None:
                    event.update(screen=screen, screen_sha256=bridge.screen_hash(screen))
                old.setdefault('original_ended_at_epoch', old.get('ended_at_epoch'))
                old['events'].append(event)
                old['phase'] = phase
                old['ended_at_epoch'] = event['at_epoch']
                write_json(attempt_path, old)

            screen = bridge.read_screen(pack['callback_target'], lines=confirm_lines)
            observe_recovery('RECOVERY_OBSERVATION', screen)
            structure = bridge.require_original_draft(old, screen, pack['completion_callback'])
            if not bridge._codex_tab_queue_allowed(screen, pack['completion_callback']):
                raise bridge.DispatchUnconfirmed('ORIGINAL_COMPOSER_NOT_RECOVERABLE: no input')
            screen = bridge._stable_owned_draft(pack['callback_target'], pack['completion_callback'],
                confirm_lines, observe_recovery, expected_structure=structure)
            if not bridge._codex_tab_queue_allowed(screen, pack['completion_callback']):
                raise bridge.DispatchUnconfirmed('ORIGINAL_COMPOSER_NOT_RECOVERABLE: no input')
            # Revalidate identity/report immediately before persisting key intent.
            if bridge.pin_workspace(pack['callback_target']) != proof or bridge._sha256_file(report) != binding['report_sha256']:
                raise bridge.TaskPackContractError('RECOVERY_BINDING_CHANGED')
            # fdopen 文件的 name 是 fd；用原锁路径核对持有 inode，拒绝锁替换。
            for held, held_path in ((lock, lock_path), (pane_lock, pane_lock_path),
                                    (target_lock, target_lock_path)):
                opened = os.fstat(held.fileno())
                current = os.lstat(held_path)
                if (opened.st_dev, opened.st_ino, opened.st_nlink) != (current.st_dev, current.st_ino, 1):
                    raise bridge.TaskPackContractError('RECOVERY_LOCK_CHANGED')
            if bridge._sha256_file(task_pack_path) != binding['task_pack_sha256']:
                raise bridge.TaskPackContractError('RECOVERY_TASK_CHANGED')
            attempt_path = attempts[-1]
            if json.loads(read_bytes(attempt_path)) != old:
                raise bridge.TaskPackContractError('RECOVERY_JOURNAL_CHANGED')
            native.require_bound(bridge, pack['callback_target'], old.get('native_binding'), pack['completion_callback'])
            observe_recovery('QUEUE_TAB_INTENT', screen)
            native.require_bound(bridge, pack['callback_target'], old.get('native_binding'), pack['completion_callback'])
            bridge.send_key(pack['callback_target'], 'tab')
            after = bridge.read_screen(pack['callback_target'], lines=confirm_lines)
            observe_recovery('POST_QUEUE_TAB_OBSERVATION', after)
            result = native.scan_original(bridge, pack['callback_target'], pack['completion_callback'],
                                          attempt_path, old.get('native_binding'))
            if result.get('confirmed') is not True:
                raise bridge.DispatchUnconfirmed('QUEUE_ACTION_UNCONFIRMED: observe original; no more input')
            result = dict(result, retries=0, queue_key='tab')
            old.update(phase='CONFIRMED', result=result, ended_at_epoch=time.time())
            write_json(attempt_path, old)
            evidence = {'reconciled_read_only': False, 'attempt': str(attempt_path)}
        elif reconcile_only:
            if not old or old['phase'] in ('PREPARED', 'NO_INPUT'):
                raise bridge.TaskPackContractError('NO_SUBMITTED_ATTEMPT: cannot manufacture receipt')
            result = observe_attempt(lambda: native.scan_original(bridge, pack['callback_target'],
                pack['completion_callback'], attempts[-1], old.get('native_binding'), read_only=True),
                attempts[-1], old, seconds, reconcile_only=True)
            observation = dict(result, recorded_at_epoch=time.time(), input_operations=0)
            observed = journal / ('observation-' + str(time.time_ns()) + '.json')
            write_json(observed, observation)
            if result.get('confirmed') is not True:
                raise bridge.DispatchUnconfirmed(
                    'CALLBACK_NOT_YET_CONFIRMED: read-only observation saved; do not resend',
                    state=bridge.DELIVERY_UNVERIFIED_BY_DETECTOR, details=result)
            result = dict(result, retries=old.get('extra_enter', 0))
            evidence = {'reconciled_read_only': True, 'observation': str(observed),
                        'attempt': str(attempts[-1])}
        else:
            if old and old['phase'] != 'NO_INPUT':
                raise bridge.TaskPackContractError(
                    'CALLBACK_ATTEMPT_EXISTS: use --reconcile-only; never repaste uncertain delivery')
            # One explicit retry is allowed only after recorded zero input and
            # a fresh identity/composer check. Persistent failure becomes local
            # work, not an endless transport retry loop.
            if len(attempts) >= 2:
                raise bridge.TaskPackContractError('CALLBACK_RETRY_BUDGET_EXHAUSTED: supervisor must review')
            attempt_path = journal / f'attempt-{len(attempts)+1:04d}.json'
            attempt = {'binding': binding, 'phase': 'PREPARED', 'events': [],
                       'started_at_epoch': time.time(),
                       'controller_provenance': controller_provenance(__file__)}
            write_json(attempt_path, attempt)

            def observe(phase, screen=None, *, evidence=None):
                if (current_identity() != proof
                        or bridge._sha256_file(report) != binding['report_sha256']
                        or bridge._sha256_file(task_pack_path) != binding['task_pack_sha256']):
                    raise bridge.TaskPackContractError('CALLBACK_BINDING_CHANGED_BEFORE_INPUT')
                fence = native.capture_paste_fence(attempt['native_binding']) if phase == 'PASTE_INTENT' else None
                fields = native.input_event_fields(attempt['native_binding'],
                                                  pack['completion_callback'], phase, evidence)
                event = dict(fields, phase=phase, at_epoch=time.time())
                if fence is not None:
                    event['native_paste_fence'] = fence
                if screen is not None:
                    event.update(screen=screen, screen_sha256=bridge.screen_hash(screen))
                attempt['events'].append(event)
                attempt['phase'] = phase
                if phase == 'EXTRA_ENTER_INTENT':
                    attempt['extra_enter'] = 1
                write_json(attempt_path, attempt)

            try:
                attempt['native_binding'] = native.bind_target(bridge, pack['callback_target'], pack['completion_callback'])
                attempt['native_binding'] = native.marker_binding(attempt['native_binding'], pack['completion_callback'], pack['completion_nonce'])
                write_json(attempt_path, attempt)
                result = bridge.submit_text(
                    pack['callback_target'], pack['completion_callback'],
                    marker=pack['completion_nonce'], confirm_lines=confirm_lines,
                    delivery_observer=observe, native_binding=attempt['native_binding'],
                    native_attempt=attempt_path, wait_seconds=seconds)
                if result.get('confirmed') is not True:
                    raise bridge.DispatchUnconfirmed('callback not confirmed', details=result)
            except BaseException as exc:
                if attempt['phase'] == 'PREPARED':
                    attempt['phase'] = 'NO_INPUT'
                annotate_attempt_error(exc, attempt_path, attempt)
                attempt.update(error=str(exc), error_details=exc.details,
                               delivery_state=getattr(exc, 'state', None),
                               ended_at_epoch=time.time())
                write_json(attempt_path, attempt)
                raise
            attempt.update(phase='CONFIRMED', result=result, ended_at_epoch=time.time())
            write_json(attempt_path, attempt)
            evidence = {'reconciled_read_only': False, 'attempt': str(attempt_path)}
        # The report must remain the exact document whose callback was sent.
        if bridge._sha256_file(report) != binding['report_sha256']:
            raise bridge.TaskPackContractError('REPORT_CHANGED_DURING_CALLBACK: receipt refused')
        if bridge._sha256_file(task_pack_path) != binding['task_pack_sha256'] or current_identity() != proof:
            raise bridge.TaskPackContractError('CALLBACK_BINDING_CHANGED_BEFORE_RECEIPT')
        original = json.loads(read_bytes(Path(evidence['attempt'])))
        result = attempt_details(result, evidence['attempt'], original, reconcile_only=reconcile_only)
        if not native.receipt_evidence(bridge, pack['callback_target'], pack['completion_callback'],
                                      original, result,
                                      read_only=reconcile_only):
            raise bridge.DispatchUnconfirmed('CALLBACK_NATIVE_PROOF_REQUIRED', details=result)
        receipt = {**{k: v for k, v in binding.items() if k != 'identity'},
                   'confirmed': True, 'bridge_retries': result.get('retries', 0),
                   'native_binding': result['native_binding'], 'native_proof': result['native_proof'],
                   'confirmation_source': result['confirmation_source'],
                   'recorded_at_epoch': time.time(), **evidence}
        for name in RECEIPT_DIAGNOSTICS:
            if name in result:
                receipt[name] = result[name]
        encoded = (json.dumps(receipt, ensure_ascii=False, indent=2) + '\n').encode()
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return receipt

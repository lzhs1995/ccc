"""Only the capabilities needed to recover an existing, bound Codex session.

This module deliberately has no terminal lifecycle, history, model or daemon
management operation. Unknown RPCs/extra fields fail before any request.
"""
from pathlib import Path
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
import stat
import sys
import time

# The watchdog is part of the complete CCC release. Importing an older release
# here silently discards current input, identity and wakeup fixes.
CCC = Path(__file__).resolve().parent
MAC = CCC / 'mac' / 'source' / 'scripts'
CONFIG = Path.home() / 'Library' / 'Application Support' / 'cmux-codex-continue' / 'config.json'


class ControlLockBusy(RuntimeError):
    """No control was attempted; retain the original deadline and retry later."""


def _paired_foreground_alias(loaded, mac_path):
    """One identical helper may already come from the same complete CCC bundle.

    This is package identity, not an approval or migration of a MAC controller.
    Every other module and every other release retains the exact path check.
    """
    if mac_path.stem != 'ccc_foreground_lease':
        return False
    root_path = CCC / 'ccc_foreground_lease.py'
    spec = getattr(loaded, '__spec__', None)
    if (getattr(loaded, '__file__', None) != str(root_path)
            or getattr(spec, 'origin', None) != str(root_path)
            or getattr(spec, 'loader', None) is None
            or root_path.is_symlink() or mac_path.is_symlink()
            or CCC.is_symlink() or MAC.is_symlink()):
        return False
    manifest_path = CCC / 'manifest.json'
    if manifest_path.is_symlink():
        return False
    try:
        raw_manifest = manifest_path.read_bytes()
        manifest = json.loads(raw_manifest)
        files = manifest['files']
        root_pin = files['ccc_foreground_lease.py']
        mac_pin = files['mac/source/scripts/ccc_foreground_lease.py']
        if (manifest.get('version') != 2 or not root_pin or root_pin != mac_pin
                or hashlib.sha256(root_path.read_bytes()).hexdigest() != root_pin
                or hashlib.sha256(mac_path.read_bytes()).hexdigest() != mac_pin):
            return False
        import cmux_codex_watch as ccc
        ccc_path = CCC / 'cmux_codex_watch.py'
        if (getattr(ccc, '__file__', None) != str(ccc_path)
                or getattr(getattr(ccc, '__spec__', None), 'origin', None) != str(ccc_path)):
            return False
        ccc.validate_runtime_release(CCC)
        return manifest_path.read_bytes() == raw_manifest
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def libraries():
    if not MAC.is_dir():
        raise RuntimeError('complete bundled MAC source is missing from CCC runtime')
    # A shared controller imported earlier must not silently replace this
    # release's endpoint, identity or input implementation.
    for path in MAC.glob('*.py'):
        loaded = sys.modules.get(path.stem)
        spec = getattr(loaded, '__spec__', None)
        # Resolving the paths first would turn a symlink to the CCC helper
        # into an apparent exact MAC origin and skip the paired validation.
        # Keep the declared origin and the regular member path independent.
        if (path.is_symlink()
                or (loaded is not None
                    and (getattr(loaded, '__file__', None) != str(path)
                         or getattr(spec, 'origin', None) != str(path)
                         or getattr(spec, 'loader', None) is None)
                    and not _paired_foreground_alias(loaded, path))):
            raise RuntimeError('mixed MAC controller import: ' + path.stem)
    sys.path[:0] = [str(CCC), str(MAC)]
    import cmux_bridge
    import cmux_native_delivery as native
    import cmux_codex_watch as ccc
    import cmux_input_mutex
    from cmux_codex_rpc import DaemonRPC
    from ccc_compact_endpoint import socket_path_for_binding

    class BoundDaemonRPC(DaemonRPC):
        def __init__(self, *, binding, timeout=20):
            endpoint_binding = dict(binding, process={k: v for k, v in binding['process'].items()
                                                     if k != 'tty'})
            super().__init__(socket_path=socket_path_for_binding(endpoint_binding), timeout=timeout)

    return cmux_bridge, native, ccc, cmux_input_mutex, BoundDaemonRPC


@contextmanager
def control_locks(lib, target, config_path=CONFIG):
    """Use CCC's workspace lock, then the real MAC pane/target flock.

    Every writer uses the original authenticated UUID spelling. Lowercasing it
    would create a different lock and let input race an interrupt.
    """
    lock = lib[2].workspace_input_lock(config_path, target['workspace_id'])
    lock.timeout_sec = 2
    with ExitStack() as stack:
        try:
            stack.enter_context(lock)
        except RuntimeError as exc:
            if str(exc).startswith('timed out waiting for '):
                raise ControlLockBusy('workspace input lock busy; zero input') from exc
            raise
        workspace_stat = os.fstat(lock.handle.fileno())

        def check_workspace():
            current = lock.path.stat(follow_symlinks=False)
            if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.geteuid()
                    or current.st_nlink != 1
                    or (current.st_dev, current.st_ino) !=
                    (workspace_stat.st_dev, workspace_stat.st_ino)):
                raise RuntimeError('workspace input lock replaced; no control')

        check_workspace()
        try:
            check_mac = stack.enter_context(lib[3].hold(target['identity']))
        except lib[3].InputMutexError as exc:
            if str(exc).startswith('INPUT_CHANNEL_BUSY:'):
                raise ControlLockBusy('pane/target input lock busy; zero input') from exc
            raise
        def check():
            check_workspace()
            check_mac()

        # Errors after acquisition are not contention and must never be retried
        # as an unattempted operation.
        check()
        yield check


def binding(target, bridge, native, *, allow_relocated=False):
    row = native._tree_row(bridge, target['surface_id'])
    if (row['target_surface_uuid'].upper() != target['surface_id'].upper()
            or row['workspace_uuid'].upper() != target['workspace_id'].upper()
            or (not allow_relocated
                and row['target_pane_uuid'].upper() != target['pane_id'].upper())
            or row.get('kind') != 'terminal' or row.get('dock')):
        raise RuntimeError('original surface moved; no control')
    process = native._target_process(row)
    if (native._provider(process) != 'codex'
            or native._live_session(process, 'codex') != target['session_id']):
        raise RuntimeError('original session changed; no control')
    return {'identity': row, 'process': dict(native._public_process(process), tty=row['tty'])}


def metadata(rpc, sid):
    th = rpc.request('thread/read', {'threadId': sid, 'includeTurns': False})['thread']
    turns = rpc.request('thread/turns/list', {'threadId': sid, 'limit': 1,
                        'sortDirection': 'desc', 'itemsView': 'notLoaded'})['data']
    if th['id'] != sid:
        raise RuntimeError('native thread mismatch')
    return th, turns[0] if turns else None


def goal_identity(value):
    goal = value.get('goal')
    if not goal:
        return None
    return {key: goal.get(key) for key in ('threadId', 'objective', 'status', 'tokenBudget',
                                         'tokensUsed', 'timeUsedSeconds', 'createdAt', 'updatedAt')}


def control_request(rpc, sid, method, params, *, request=None):
    """Never close/restart/recreate a surface or edit/revert its history."""
    fields = {
        'thread/compact/interrupt': {'threadId', 'expectedTurnId', 'attemptId', 'intentPath',
                                     'clientPid', 'compactionOccurrence'},
    }
    if method not in fields or set(params) != fields[method]:
        raise PermissionError('operation outside preserve-surface compact policy')
    if not sid or params.get('threadId') != sid:
        raise PermissionError('operation not bound to original session')
    if method == 'thread/compact/interrupt':
        if not params.get('expectedTurnId') or not params.get('compactionOccurrence') or request is not None:
            raise PermissionError('conditional interrupt requires the original occurrence and typed request')
        return rpc.request(method, params)


def terminal_command(request, client_epoch):
    import json
    import uuid
    if str(uuid.UUID(client_epoch)) != client_epoch:
        raise ValueError('original native client epoch must be canonical')
    payload = json.dumps(dict(clientPid=request['clientPid'], clientEpoch=client_epoch,
                              request=request), ensure_ascii=False, separators=(',', ':'))
    if len(payload.encode()) > 2048:
        raise ValueError('bound conditional compact command exceeds the native limit')
    return '/compact --interrupt-stalled ' + payload


def conditional_submit(worker, request, snapshot, check_locks, recheck, prepare):
    """One transport budget, consumed before the first possible mutation.

    Endpoint absence can select the protected TUI command before submission.
    Once a request is attempted, no fallback or extra Return is permitted.
    """
    target = worker.target
    rpc = None
    submission_recorded = False
    try:
        try:
            rpc = worker.lib[4](binding=dict(identity=target['identity'],
                process=target['process'], session_id=target['session_id']), timeout=20)
        except (FileNotFoundError, ConnectionError, TimeoutError):
            rpc = None
        transport = 'native-conditional-rpc' if rpc is not None else 'native-bound-tui-command'
        text = terminal_command(request, snapshot['client_epoch']) if rpc is None else None
        worker.lib[0].require_prompt_free(target['surface_id'], provider='codex')
        fresh = recheck()
        if (fresh.get('foreground') != snapshot.get('foreground')
                or fresh.get('client_epoch') != snapshot.get('client_epoch')):
            raise ValueError('native client selection changed before transport intent')
        check_locks()
        # Pin the ledger only after endpoint initialization and the final input read;
        # ordinary log cursor movement must not invalidate our own just-written pin.
        prepare(fresh)
        check_locks()
        worker.journal_directory.write_once(request['attemptId'] + '-client-submit.json',
            dict(status='UNKNOWN', transport=transport, client_request=request,
                 client_epoch=snapshot['client_epoch'], text=text, attempted_at=time.time(),
                 replay_authorized=False))
        submission_recorded = True
        check_locks()
        if rpc is not None:
            result = control_request(rpc, target['session_id'], 'thread/compact/interrupt', request)
            return dict(transport=transport, result=result, input_operations=0, replay_authorized=False)
        # The host protects drafts and dialogs again; host receipt is not native admission.
        acknowledgement = worker.lib[0].submit_text_protected(target['surface_id'], text)
        return dict(transport=transport, host=acknowledgement, input_operations=2,
                    replay_authorized=False)
    except Exception as exc:
        if not submission_recorded:
            try:
                from ccc_compact_control_health import record_not_transmitted
                record_not_transmitted(worker, request, exc)
            except Exception as marker_error:
                # Failure to add health evidence never replaces the original
                # control error or authorizes a second transport attempt.
                try:
                    worker.journal.record('CONDITIONAL_NOT_TRANSMITTED_MARKER_FAILED',
                        key=request['attemptId'], original_error=str(exc)[:500],
                        marker_error=str(marker_error)[:500], replay_authorized=False)
                except Exception as journal_error:
                    if hasattr(exc, 'add_note'):
                        exc.add_note('Control health/journal failed: ' + str(journal_error)[:300])
                if hasattr(exc, 'add_note'):
                    exc.add_note('Control health marker failed: ' + str(marker_error)[:300])
        raise
    finally:
        if rpc is not None:
            rpc.close()

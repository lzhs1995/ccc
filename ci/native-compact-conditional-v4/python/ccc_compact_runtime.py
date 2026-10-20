"""Only the capabilities needed to recover an existing, bound Codex session.

This module deliberately has no terminal lifecycle, history, model or daemon
management operation. Unknown RPCs/extra fields fail before any request.
"""
from pathlib import Path
from contextlib import contextmanager, ExitStack
import os
import stat
import sys
import time

# The watchdog is part of the complete CCC release. Importing an older release
# here silently discards current input, identity and wakeup fixes.
CCC = Path(__file__).resolve().parent
MAC = CCC / 'mac' / 'source' / 'scripts'
CONFIG = Path('/Users/lzhs/Library/Application Support/cmux-codex-continue/config.json')


class ControlLockBusy(RuntimeError):
    """No control was attempted; retain the original deadline and retry later."""


def libraries():
    if not MAC.is_dir():
        raise RuntimeError('complete bundled MAC source is missing from CCC runtime')
    # A shared controller imported earlier must not silently replace this
    # release's endpoint, identity or input implementation.
    for path in MAC.glob('*.py'):
        loaded = sys.modules.get(path.stem)
        if loaded is not None and Path(getattr(loaded, '__file__', '')).resolve() != path.resolve():
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
        check_locks()
        if rpc is not None:
            result = control_request(rpc, target['session_id'], 'thread/compact/interrupt', request)
            return dict(transport=transport, result=result, input_operations=0, replay_authorized=False)
        # The host protects drafts and dialogs again; host receipt is not native admission.
        acknowledgement = worker.lib[0].submit_text_protected(target['surface_id'], text)
        return dict(transport=transport, host=acknowledgement, input_operations=2,
                    replay_authorized=False)
    finally:
        if rpc is not None:
            rpc.close()

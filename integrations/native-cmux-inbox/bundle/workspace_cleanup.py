"""One original workspace close, complete receipts, and bounded absence reads."""
import base64
import json
import subprocess
import time
import uuid

import fixture_common as f

MAX_REPLY_BYTES = 1024 * 1024
MAX_TREE_OBSERVATIONS = 50
CLOSE_AND_OBSERVE_SECONDS = 10
FINAL_CLEANUP_RESERVE_SECONDS = 25


def _uuid(value):
    f.need(isinstance(value, str), 'actual UUID string required')
    return str(uuid.UUID(value)).upper()


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            f.need(key not in result, 'duplicate JSON field in host reply')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    f.need(remaining > 0, 'original workspace cleanup observation deadline expired')
    return remaining


def _bytes(value):
    if value is None:
        return b''
    return value.encode('utf-8') if isinstance(value, str) else value


def _output(raw):
    # Preserve raw output even when it is invalid UTF-8 or JSON. Oversized output
    # fails acceptance; the prefix plus complete byte count/hash remain evidence.
    value = dict(bytes=len(raw), sha256=f.sha(raw),
                 base64=base64.b64encode(raw[:MAX_REPLY_BYTES]).decode(),
                 complete=len(raw) <= MAX_REPLY_BYTES)
    try:
        value['utf8'] = raw[:MAX_REPLY_BYTES].decode('utf-8')
    except UnicodeDecodeError:
        pass
    return value


def _call(root, gui, alias, args, label, deadline):
    _remaining(deadline)
    before = gui.stable_peer()
    alias.check()
    alias_before = list(alias.identity)
    timeout = min(5, _remaining(deadline))
    argv = [str(gui.cli), '--socket', str(gui.socket), *args]
    began = time.monotonic()
    f.save_once(root / (label + '-INTENT.json'), dict(
        argv=argv, peer=before, alias_identity=alias_before, began_monotonic=began,
        remaining_seconds=timeout, deadline_monotonic=deadline,
        timeout_seconds=timeout, invocations=1, retries=0))
    try:
        timeout = min(timeout, _remaining(deadline))
        reply = subprocess.run(argv, env=gui.env, capture_output=True, timeout=timeout)
    except BaseException as error:
        f.save_once(root / (label + '-RESULT.json'), dict(
            status='UNKNOWN_NO_REPLAY', error_type=type(error).__name__, error=str(error),
            stdout=_output(_bytes(getattr(error, 'stdout', None))),
            stderr=_output(_bytes(getattr(error, 'stderr', None))),
            began_monotonic=began, ended_monotonic=time.monotonic(), returncode=None, replay_allowed=False))
        raise
    stdout, stderr = _bytes(reply.stdout), _bytes(reply.stderr)
    result = dict(returncode=reply.returncode, stdout=_output(stdout), stderr=_output(stderr),
                  began_monotonic=began, actual_timeout_seconds=timeout,
                  ended_monotonic=time.monotonic(), replay_allowed=False)
    result_sha = f.save_once(root / (label + '-RESULT.json'), result)
    after = gui.stable_peer()
    alias.check()
    f.save_once(root / (label + '-PEER.json'), dict(before=before, after=after,
        alias_before=alias_before, alias_after=list(alias.identity), result_sha256=result_sha))
    f.need(after == before and list(alias.identity) == alias_before,
           'original GUI/socket/alias changed across cleanup command')
    _remaining(deadline)
    f.need(len(stdout) <= MAX_REPLY_BYTES and len(stderr) <= MAX_REPLY_BYTES, 'host cleanup reply exceeds evidence bound')
    f.need(reply.returncode == 0, 'original workspace cleanup CLI refused; no replay')
    return _json(stdout), result_sha


def _tree_ids(value):
    f.need(isinstance(value, dict) and isinstance(value.get('windows'), list)
           and 'error' not in value, 'actual complete host tree required')
    windows, workspaces = set(), set()
    for window in value['windows']:
        f.need(isinstance(window, dict) and isinstance(window.get('workspaces'), list),
               'complete workspace list required for each host window')
        window_id = _uuid(window.get('id'))
        f.need(window_id not in windows, 'duplicate host window')
        windows.add(window_id)
        for workspace in window['workspaces']:
            f.need(isinstance(workspace, dict), 'actual workspace row required')
            workspace_id = _uuid(workspace.get('id'))
            f.need(workspace_id not in workspaces, 'duplicate host workspace')
            workspaces.add(workspace_id)
    return workspaces


def close_workspace(root, gui, alias, workspace, cleanup_deadline):
    """A successful close reply is required before bounded read-only observation."""
    workspace = _uuid(workspace)
    began = time.monotonic()
    deadline = min(began + CLOSE_AND_OBSERVE_SECONDS,
                   cleanup_deadline - FINAL_CLEANUP_RESERVE_SECONDS)
    state = dict(workspace=workspace, began_monotonic=began, deadline_monotonic=deadline,
                 cleanup_deadline_monotonic=cleanup_deadline, final_reserve_seconds=FINAL_CLEANUP_RESERVE_SECONDS,
                 close_request_limit=1, retries=0, tree_observations=[], passed=False, replay_allowed=False)
    try:
        _remaining(deadline)
        created = _json(f.strict_file(root / 'WORKSPACE-CREATE-REPLY.json'))
        f.need(created.get('returncode') == 0, 'original successful workspace create reply required')
        created_payload = _json(created['stdout'])
        f.need(_uuid(created_payload.get('workspace_id')) == workspace, 'original created workspace differs')
        window = _uuid(created_payload.get('window_id'))
        before_tree, before_sha = _call(root, gui, alias,
            ['--json', '--id-format', 'both', 'tree', '--all'], 'WORKSPACE-BEFORE-CLOSE', deadline)
        before_ids = _tree_ids(before_tree)
        f.save_once(root / 'WORKSPACE-BEFORE-CLOSE-PARSED.json', dict(
            tree=before_tree, result_sha256=before_sha, workspace_ids=sorted(before_ids),
            original_present=workspace in before_ids, observed_monotonic=time.monotonic()))
        f.need(workspace in before_ids, 'original workspace absent in pre-close tree; no close issued')
        state['before_close_result_sha256'] = before_sha
        # --id-format both changes only output projection, preserving UUIDs for
        # comparison with the original actual create reply. No --force or retry.
        closed, close_sha = _call(root, gui, alias,
            ['--json', '--id-format', 'both', 'workspace', 'close', '--workspace', workspace],
            'WORKSPACE-CLOSE', deadline)
        f.need(isinstance(closed, dict) and set(closed) == {
            'window_id', 'window_ref', 'workspace_id', 'workspace_ref'}, 'exact successful workspace close payload required')
        f.need(_uuid(closed['workspace_id']) == workspace and _uuid(closed['window_id']) == window,
               'close reply does not identify original created workspace/window')
        state['close_result_sha256'] = close_sha
        for index in range(MAX_TREE_OBSERVATIONS):
            label = 'WORKSPACE-AFTER-CLOSE-%02d' % index
            tree, result_sha = _call(root, gui, alias,
                ['--json', '--id-format', 'both', 'tree', '--all'], label, deadline)
            remaining = _tree_ids(tree)
            observed = dict(index=index, observed_monotonic=time.monotonic(),
                            result_sha256=result_sha, original_present=workspace in remaining,
                            workspace_ids=sorted(remaining))
            f.save_once(root / (label + '-PARSED.json'), dict(**observed, tree=tree))
            state['tree_observations'].append(observed)
            _remaining(deadline)
            if workspace not in remaining:
                state['passed'] = True
                break
            time.sleep(min(.2, _remaining(deadline)))
        f.need(state['passed'], 'owned workspace remains after bounded observation')
        return dict(passed=True, close_invocations=1, tree_queries=len(state['tree_observations']),
                    close_result_sha256=close_sha, original_workspace_absent=True, replay_allowed=False)
    except BaseException as error:
        state['error'] = dict(type=type(error).__name__, error=str(error))
        raise
    finally:
        state['ended_monotonic'] = time.monotonic()
        f.save_once(root / 'WORKSPACE-CLOSE-OBSERVATION.json', state)

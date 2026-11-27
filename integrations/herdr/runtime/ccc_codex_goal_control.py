"""Journal one authenticated native Goal resume; never paste or replay input.

The caller owns CCC's shared workspace lock. This module additionally owns the
MAC pane/target lock; it must not nest the exclusive workspace compact lock.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from ccc_compact_runtime import binding, libraries, metadata


class GoalInputNotSent(RuntimeError):
    """No control RPC began, with durable evidence when an intent exists."""


class GoalDeliveryUnknown(RuntimeError):
    """Intent may exist or control may have arrived. Never replay this attempt."""


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       allow_nan=False, indent=2) + '\n').encode('utf-8')


def _write_once(path, value):
    raw = _json_bytes(value)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _require(condition, message):
    if not condition:
        raise GoalInputNotSent(message)


def _journal_directory(root, surface_id, attempt_id):
    """Make every new directory entry durable before a control intent exists."""
    parent = Path(root)
    for name in ('native-goal-v1', surface_id, attempt_id):
        child = parent / name
        try:
            child.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = child.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid(),
                 'native Goal journal is not an owned directory')
        fd = os.open(parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        parent = child
    return parent


def _evidence(path):
    """Read an owned, bounded regular file without following a link."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_size > 1048576:
            raise GoalDeliveryUnknown('invalid original Goal evidence')
        raw = stream.read(1048577)
        stable = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if stable(os.fstat(stream.fileno())) != stable(info):
            raise GoalDeliveryUnknown('original Goal evidence changed while reading')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise GoalDeliveryUnknown('invalid original Goal evidence object')
    return value, hashlib.sha256(raw).hexdigest()


def _transaction(root, attempt_id, proof=None, live_binding=None, fence=None):
    try:
        return _inspect_transaction(root, attempt_id, proof, live_binding, fence)
    except (GoalDeliveryUnknown, GoalInputNotSent):
        raise
    except Exception as exc:
        raise GoalDeliveryUnknown('original native Goal history cannot be verified; observe only') from exc


def _inspect_transaction(root, attempt_id, proof=None, live_binding=None, fence=None):
    """Continue only a hash-linked transaction proven not to have started RPC.

    Historical intents and all partial/uncertain writes remain observe-only.
    This inspection never creates a transaction or changes old evidence. It is
    also performed before live preflight, so a previously sent/UNKNOWN attempt
    cannot become retryable merely because its current Goal or process changed.
    The final selection and all writes hold the original pane/target locks.
    """
    # Refuse symlinks in each original path component before reading evidence.
    for directory in (root.parent.parent.parent, root.parent.parent, root.parent, root):
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return root, None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise GoalDeliveryUnknown('invalid original Goal journal directory')
    children = {p.name for p in root.iterdir() if p.name.startswith('retry-')}
    expected = {f'retry-{ordinal:04d}' for ordinal in range(1, len(children) + 1)}
    if children != expected or len(children) > 64:
        raise GoalDeliveryUnknown('original native Goal retry chain is incomplete')
    previous = None
    for ordinal in range(len(children) + 2):
        current = root if ordinal == 0 else root / f'retry-{ordinal:04d}'
        if ordinal:
            try:
                info = current.lstat()
            except FileNotFoundError:
                if ordinal > 64:
                    raise GoalInputNotSent('original native Goal reached its bounded pre-RPC retry limit')
                return current, previous
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
                raise GoalDeliveryUnknown('invalid original Goal transaction directory')
        names = {p.name for p in current.iterdir()}
        allowed = {'intent.json', 'not-sent.json', 'uncertain.json',
                   'rpc-started.json', 'rpc-result.json', 'observation.json'}
        if names - allowed - (children if ordinal == 0 else set()):
            raise GoalDeliveryUnknown('unrecognized original native Goal evidence')
        if names & {'uncertain.json', 'rpc-started.json', 'rpc-result.json', 'observation.json'}:
            raise GoalDeliveryUnknown('original native Goal may have reached RPC; observe only')
        if 'intent.json' not in names:
            if names or ordinal < len(children):
                raise GoalDeliveryUnknown('original native Goal transaction is incomplete')
            return current, previous
        try:
            intent, intent_sha = _evidence(current / 'intent.json')
            no_rpc, no_rpc_sha = _evidence(current / 'not-sent.json')
        except Exception as exc:
            raise GoalDeliveryUnknown('original native Goal has no complete no-RPC proof; observe only') from exc
        if (intent.get('schema') != 'native-goal-control-v2'
                or intent.get('attempt_id') != attempt_id or intent.get('previous') != previous
                or no_rpc.get('event') != 'NATIVE_GOAL_INPUT_NOT_SENT'
                or no_rpc.get('intent_sha256') != intent_sha
                or no_rpc.get('attempt_id') != attempt_id
                or no_rpc.get('rpc_started') is not False
                or no_rpc.get('exception_type') != 'GoalInputNotSent'
                or no_rpc.get('binding') != intent.get('binding')
                or no_rpc.get('proof') != intent.get('proof')):
            raise GoalDeliveryUnknown('original native Goal no-RPC proof is not bound to its intent')
        _require((proof is None or intent.get('proof') == proof)
                 and (live_binding is None or intent.get('binding') == live_binding)
                 and (fence is None or intent.get('rollout_fence') == fence),
                 'proven no-input transaction no longer matches the original Goal/binding/fence')
        previous = dict(directory=current.name, intent_sha256=intent_sha,
                        not_sent_sha256=no_rpc_sha)
    raise GoalInputNotSent('original native Goal reached its bounded pre-RPC retry limit')


def _create_transaction_directory(root, current):
    if current == root:
        return
    try:
        current.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = current.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise GoalDeliveryUnknown('invalid original Goal transaction directory')
    fd = os.open(root, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _state(rpc, sid, proof):
    thread, turn = metadata(rpc, sid)
    goal = rpc.request('thread/goal/get', {'threadId': sid})['goal']
    _require(thread.get('id') == thread.get('sessionId') == sid
             and thread.get('canAcceptDirectInput') is True
             and thread.get('modelProvider') == proof['model_provider']
             and isinstance(thread.get('path'), str) and thread['path'],
             'original native thread/provider is not available')
    _require(isinstance(turn, dict) and turn.get('id') == proof['turn_id']
             and turn.get('status') in {'failed', 'completed'}
             and (turn.get('error') or {}).get('message') == proof['error']['message'],
             'original failed turn is no longer current or is running')
    _require(isinstance(goal, dict) and goal.get('threadId') == sid
             and goal.get('status') == 'blocked'
             and type(goal.get('updatedAt')) in (int, float)
             and math.isfinite(goal['updatedAt'])
             and int(goal['updatedAt']) == int(proof['at']),
             'original blocked Goal changed or was paused')
    return thread, turn, goal


def resume_goal(workspace_id, surface_id, *, proof, attempt_id, journal_root,
                authorize, lib=None):
    """One goal/set status=active; StartIfIdle owns any next native turn.

    No turn/start, interruption, terminal key, settings change or transport
    fallback is permitted. After intent, only a durable no-RPC proof can permit
    another hash-linked transaction within this same original attempt.
    """
    intent_started = False
    intent_durable = False
    rpc_started = False
    intent = None
    intent_sha = None
    journal = None
    try:
        uuid.UUID(workspace_id)
        uuid.UUID(surface_id)
        _require(isinstance(attempt_id, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', attempt_id),
                 'invalid durable attempt identity')
        journal_base = Path(journal_root) / 'native-goal-v1' / surface_id / attempt_id
        _transaction(journal_base, attempt_id)
        _require(isinstance(proof, dict) and proof.get('kind') == 'goal_blocked'
                 and callable(authorize), 'missing authenticated Goal proof')
        sid = str(uuid.UUID(proof['session_id']))
        uuid.UUID(proof['turn_id'])
        _require(type(proof.get('pid')) is int and proof['pid'] > 0
                 and isinstance(proof.get('birth'), (list, tuple)) and len(proof['birth']) == 2
                 and proof.get('process_start') == proof['birth'][0]
                 and type(proof.get('at')) in (int, float) and math.isfinite(proof['at'])
                 and proof['at'] > 0 and isinstance(proof.get('goal_id'), str) and proof['goal_id']
                 and isinstance(proof.get('model_provider'), str) and proof['model_provider'],
                 'incomplete original Goal/process identity')
        journal = _journal_directory(journal_root, surface_id, attempt_id)
        lib = libraries() if lib is None else lib
        bridge, native, _, mutex, rpc_factory = lib
        row = native._tree_row(bridge, surface_id)
        _require(row.get('target_surface_uuid') == surface_id
                 and row.get('workspace_uuid') == workspace_id
                 and not row.get('dock') and row.get('kind') == 'terminal',
                 'original main surface moved or is unavailable')
        target = dict(surface_id=surface_id, workspace_id=workspace_id,
                      pane_id=row['target_pane_uuid'], session_id=sid)
        # The outer CCC workspace shared lock is already held. Never take an
        # exclusive workspace lock here: that would deadlock our own writer.
        with mutex.hold(row) as recheck:
            _transaction(journal_base, attempt_id)
            original_binding = binding(target, bridge, native)
            process = original_binding['process']
            _require(original_binding['identity'] == row and process.get('pid') == proof['pid']
                     and process.get('birth') == list(proof['birth']),
                     'original process generation changed')
            _require(authorize(), 'Goal/configuration/backoff authorization changed')
            with rpc_factory(binding=dict(original_binding, session_id=sid), timeout=8) as rpc:
                before = _state(rpc, sid, proof)
                _require(binding(target, bridge, native) == original_binding,
                         'original process binding changed before control')
                _require(_state(rpc, sid, proof) == before, 'native state changed during checks')
                thread, turn, goal = before
                fence = native._pin_file(Path(thread['path']), 'codex', sid)
                _require(fence.get('at_record_boundary') is True,
                         'native rollout ends in an incomplete record')
                _require(authorize(), 'Goal/configuration/backoff authorization changed')
                recheck()
                journal, previous = _transaction(journal_base, attempt_id, proof, original_binding, fence)
                _create_transaction_directory(journal_base, journal)
                params = {'threadId': sid, 'status': 'active'}
                request = {'id': rpc._id + 1, 'method': 'thread/goal/set', 'params': params}
                raw = json.dumps(request, ensure_ascii=False, separators=(',', ':'))
                # Partial intent writes never mint a no-input proof. Complete
                # intent plus a typed refusal before the RPC boundary can.
                intent_started = True
                intent = dict(
                    schema='native-goal-control-v2', previous=previous,
                    event='NATIVE_GOAL_RESUME_INTENT', at=time.time(), attempt_id=attempt_id,
                    request=request, request_utf8=raw,
                    request_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest(),
                    binding=original_binding, proof=proof, thread_before=thread,
                    turn_before=turn, goal_before=goal, rollout_fence=fence,
                    socket_identity=getattr(rpc, 'socket_identity', None),
                    semantics='status only; native StartIfIdle owns continuation; never replay RPC')
                intent_sha = hashlib.sha256(_json_bytes(intent)).hexdigest()
                _write_once(journal / 'intent.json', intent)
                intent_durable = True
                recheck()
                _require(authorize(), 'authorization changed after durable intent')
                _require(binding(target, bridge, native) == original_binding,
                         'original binding changed after durable intent')
                _require(native._pin_file(Path(thread['path']), 'codex', sid) == fence,
                         'new native input or rollout replacement after durable intent')
                _require(rpc._id + 1 == request['id'], 'RPC envelope changed after durable intent')
                # Mark the irreversible boundary before writing or sending it.
                # A partial marker or missing response remains UNKNOWN.
                rpc_started = True
                _write_once(journal / 'rpc-started.json', dict(
                    event='NATIVE_GOAL_RPC_STARTED', at=time.time(), attempt_id=attempt_id,
                    intent_sha256=intent_sha, request_sha256=intent['request_sha256']))
                result = rpc.request(request['method'], params)
                _write_once(journal / 'rpc-result.json', dict(at=time.time(), result=result))
                after_goal = rpc.request('thread/goal/get', {'threadId': sid})['goal']
                after_thread, after_turn = metadata(rpc, sid)
                invariant_fields = ('threadId', 'objective', 'tokenBudget', 'createdAt')
                observation = dict(at=time.time(), goal_after=after_goal, turn_after=after_turn,
                    original_goal_preserved=all(after_goal.get(k) == goal.get(k) for k in invariant_fields),
                    session_settings_preserved=all(after_thread.get(k) == thread.get(k)
                        for k in ('id', 'sessionId', 'path', 'model', 'modelProvider', 'reasoningEffort')),
                    business_progress_verified=False)
                _write_once(journal / 'observation.json', observation)
                if not observation['original_goal_preserved'] or not observation['session_settings_preserved']:
                    raise GoalDeliveryUnknown('post-control identity changed; observation required')
                if after_goal.get('status') != 'active':
                    raise GoalDeliveryUnknown('Goal did not remain active; observation required')
                return observation
    except BaseException as exc:
        if intent_durable and not rpc_started and isinstance(exc, GoalInputNotSent):
            try:
                _write_once(journal / 'not-sent.json', dict(
                    event='NATIVE_GOAL_INPUT_NOT_SENT', at=time.time(), attempt_id=attempt_id,
                    intent_sha256=intent_sha, rpc_started=False,
                    binding=intent['binding'], proof=intent['proof'],
                    exception_type='GoalInputNotSent', error=str(exc)[:1000]))
            except BaseException as persistence_error:
                exc = persistence_error
            else:
                raise
        if intent_started or isinstance(exc, GoalDeliveryUnknown):
            if journal is not None and intent_started:
                try:
                    _write_once(journal / 'uncertain.json', dict(at=time.time(), no_replay=True,
                        exception_type=type(exc).__name__, error=str(exc)[:1000]))
                except BaseException:
                    pass
            if isinstance(exc, GoalDeliveryUnknown):
                raise
            raise GoalDeliveryUnknown('native Goal intent retained; do not replay: ' + str(exc)) from exc
        if isinstance(exc, GoalInputNotSent):
            raise
        raise GoalInputNotSent('native Goal preflight refused before intent: ' + str(exc)) from exc

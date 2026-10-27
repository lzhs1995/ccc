"""Authenticate the original foreground executor before consuming file replies.

Only explicit foreground start captures this identity. File schedulers and
advisory sidecars neither create it nor confer consumption authority.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid

import cmux_native_delivery as native
import cmux_workspace_guard as workspace
import executor_ready as ready

SCHEMA = 'executor-reask-consumer-identity-v1'
AUTH_SCHEMA = 'executor-reply-consumer-proof-v1'
MAX_ANCESTORS = 16
SHELLS = {'sh', 'bash', 'zsh', 'rtk'}


def _episode(state):
    from executor_reask_protocol import state_controller
    controller = state_controller(state)
    if controller is None:
        raise ValueError('original inquiry controller is required')
    return dict({key: copy.deepcopy(state[key]) for key in (
        'caller_surface_uuid', 'workspace_uuid', 'supervisor', 'task_id', 'episode_id')},
        controller=controller)


def _same_uuid(left, right):
    try:
        return uuid.UUID(left) == uuid.UUID(right)
    except (ValueError, TypeError, AttributeError):
        return False


def _caller_uuid(caller, name, expected):
    # Native caller_snapshot uses *_id. Historical normalized fixtures and
    # callers used *_uuid; conflicting aliases must never pick a winner.
    values = [caller[key] for key in (name + '_id', name + '_uuid') if key in caller]
    return bool(values) and all(_same_uuid(value, expected) for value in values)


def _codex_proof(binding):
    from executor_reask_protocol import sha256_shape
    resolved, _tree, _env, proof = workspace.caller_snapshot()
    caller = resolved.get('caller', {})
    managed = (isinstance(proof, dict) and proof.get('source') is None
               and type(proof.get('daemon_pid')) is int and proof['daemon_pid'] > 1)
    embedded = (isinstance(proof, dict) and proof.get('source') == 'embedded-native-client-v1'
                and proof.get('daemon_pid') is None)
    if (not isinstance(proof, dict)
            or not (managed or embedded)
            or type(proof.get('pid')) is not int or proof['pid'] <= 1
            or not sha256_shape(proof.get('argv_sha256'))
            or not isinstance(proof.get('foreground'), dict)
            or proof.get('session') != binding['session_id']
            or proof.get('foreground', {}).get('thread_id') != binding['session_id']
            or proof.get('pid') != binding['process']['pid']
            or proof.get('birth') != binding['process']['birth']
            or proof.get('executable') != binding['process']['executable']
            or proof.get('tty') != binding['tty']
            or not _same_uuid(proof.get('surface_uuid'), binding['caller_surface_uuid'])
            or not _caller_uuid(caller, 'surface', binding['caller_surface_uuid'])
            or not _caller_uuid(caller, 'workspace', binding['workspace_uuid'])
            or not _caller_uuid(caller, 'pane', binding['pane_uuid'])
            or caller.get('surface_type') != 'terminal'):
        raise ValueError('consume requires the original authenticated Codex foreground thread')
    # Preserve the original runtime kind and client/argv pins. A new embedded
    # proof does not migrate an episode captured under a managed daemon.
    if 'caller_proof' in binding and proof != binding['caller_proof']:
        raise ValueError('original Codex consumer process proof changed')
    return proof


def _claude_ancestry(binding):
    """A CLI tool may have shell/proxy parents; another model is not a proxy."""
    pid, chain = os.getppid(), []
    for _ in range(MAX_ANCESTORS):
        if pid <= 1 or any(row['pid'] == pid for row in chain):
            raise ValueError('original Claude consumer ancestry is unavailable')
        row = native.identity.process(pid, validate_argv=False)
        chain.append(row)
        if row['pid'] == binding['process']['pid']:
            if native._public_process(row) != binding['process']:
                raise ValueError('original Claude consumer process changed')
            for original in chain:
                if native.identity.process(original['pid'], validate_argv=False) != original:
                    raise ValueError('Claude consumer ancestry changed during authentication')
            return dict(kind='ORIGINAL_CLAUDE_ANCESTRY',
                        ancestors=[native._public_process(item) for item in chain])
        if Path(row['executable']).name not in SHELLS:
            raise ValueError('consumer is not a tool of the original Claude session')
        pid = row['ppid']
    raise ValueError('Claude consumer ancestry exceeds the bounded check')


def check_live(binding):
    """Check a pinned client and journal; this alone does not prove the caller."""
    if (not isinstance(binding, dict) or binding.get('schema') != SCHEMA
            or binding.get('provider') not in ('claude', 'codex')
            or str(uuid.UUID(binding['session_id'])) != binding['session_id']):
        raise ValueError('original foreground consumer identity is required')
    provider = binding['provider']
    path = Path(binding['transcript']['path'])
    if (native._native_root(provider).resolve() not in path.parents
            or not path.name.endswith(binding['session_id'] + '.jsonl')):
        raise ValueError('consumer transcript is outside the original native session')
    process = native.identity.process(binding['process']['pid'], validate_argv=False)
    if (native._public_process(process) != binding['process']
            or native._provider(process) != provider
            or not _same_uuid(process['env'].get('CMUX_SURFACE_ID'), binding['caller_surface_uuid'])
            or not _same_uuid(process['env'].get('CMUX_WORKSPACE_ID'), binding['workspace_uuid'])):
        raise ValueError('original consumer process, birth, surface or workspace changed')
    if native._live_session(process, provider, require_native=True) != binding['session_id']:
        raise ValueError('original consumer native session changed')
    tty = subprocess.run(['/bin/ps', '-p', str(process['pid']), '-o', 'tty='],
                         capture_output=True, text=True, timeout=3, check=True).stdout.strip()
    row = native._tree_row(ready._bridge(), binding['caller_surface_uuid'])
    if (tty != binding['tty'] or row['tty'] != binding['tty']
            or not _same_uuid(row['target_surface_uuid'], binding['caller_surface_uuid'])
            or not _same_uuid(row['workspace_uuid'], binding['workspace_uuid'])
            or not _same_uuid(row['target_pane_uuid'], binding['pane_uuid'])):
        raise ValueError('original consumer terminal binding changed')
    with native._open(path) as stream:
        native._check_file(stream, binding)
    return process


def _caller(binding):
    return (_codex_proof(binding) if binding['provider'] == 'codex'
            else _claude_ancestry(binding))


def capture_executor(live, *, state):
    """Bind the actual caller and this new episode before registration commits."""
    surface, workspace_id = live['caller_surface_uuid'], live['workspace_uuid']
    row = native._tree_row(ready._bridge(), surface)
    if (not _same_uuid(row['target_surface_uuid'], surface)
            or not _same_uuid(row['workspace_uuid'], workspace_id)
            or (live.get('caller_pane_uuid') is not None
                and not _same_uuid(row['target_pane_uuid'], live['caller_pane_uuid']))):
        raise ValueError('consumer moved during inquiry registration')
    process = native._target_process(row)
    provider = native._provider(process)
    if provider not in ('claude', 'codex'):
        raise ValueError('a native Claude or Codex consumer is required')
    session = native._live_session(process, provider, require_native=True)
    result = dict(schema=SCHEMA, provider=provider, session_id=session,
                  caller_surface_uuid=surface, workspace_uuid=workspace_id,
                  pane_uuid=row['target_pane_uuid'], tty=row['tty'],
                  process=native._public_process(process), episode_binding=_episode(state),
                  captured_epoch=time.time())
    if (not _same_uuid(state['caller_surface_uuid'], surface)
            or not _same_uuid(state['workspace_uuid'], workspace_id)):
        raise ValueError('new episode belongs to another consumer')
    result['caller_proof'] = _caller(result)
    result['transcript'] = native._pin_file(
        native._transcript_path(provider, session), provider, session)
    check_live(result)
    _caller(result)
    return result


def authenticate(state):
    """Called under the episode lock, before any reply or episode write."""
    binding = state.get('executor_identity')
    if (not isinstance(binding, dict) or binding.get('schema') != SCHEMA
            or binding.get('episode_binding') != _episode(state)
            or not _same_uuid(binding.get('caller_surface_uuid'), state['caller_surface_uuid'])
            or not _same_uuid(binding.get('workspace_uuid'), state['workspace_uuid'])):
        raise ValueError('original episode has no matching foreground consumer identity')
    check_live(binding)
    proof = _caller(binding)
    # Recheck client/session/journal after caller resolution. A resume or process
    # replacement while observing the caller cannot authorize the write.
    check_live(binding)
    digest = hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()
    return dict(schema=AUTH_SCHEMA, provider=binding['provider'],
                session_id=binding['session_id'], process=copy.deepcopy(binding['process']),
                identity_sha256=digest, episode_id=state['episode_id'],
                caller_proof=proof)

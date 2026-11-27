"""One resolved caller per hook evaluation, without rewriting process environment."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import subprocess

import cmux_workspace_guard as workspace
import cmux_identity_budget as budget

_current = ContextVar('cmux_hook_identity', default=None)
_proof = ContextVar('cmux_hook_native_proof', default=None)


def resolve(payload):
    # Reserve headroom under the registered 5-second Stop hook timeout. All ps,
    # cmux and process checks share this deadline; a later command cannot reset it.
    with budget.limit(2.5):
        return _resolve(payload)


def _resolve(payload):
    _proof.set(None)
    collector = lambda: workspace.daemon_identity.collect_hook(os.environ, payload)
    proof = collector()
    if proof is not None:
        # Use the same authenticated native caller as transport and marker writes.
        _identity, _tree, env, proof = workspace.caller_snapshot(
            collector=collector, initial_proof=proof)
        if proof is None:
            workspace.deny('managed hook caller disappeared during resolution')
        ws, surface = env.get('CMUX_WORKSPACE_ID'), env.get('CMUX_SURFACE_ID')
        if not ws or not surface:
            workspace.deny('resolved hook caller lacks workspace or surface')
        _proof.set(proof)
        return ws, surface
    return (os.environ.get('CMUX_WORKSPACE_ID') or payload.get('workspace_id') or 'default',
            os.environ.get('CMUX_SURFACE_ID') or payload.get('surface_id')
            or payload.get('surface_uuid'))


def identity(payload):
    value = _current.get()
    return value if value is not None else resolve(payload)


def native_proof():
    return _proof.get()


@contextmanager
def evaluation(payload):
    # PostToolUse reconciliation descends through bridge.pin_workspace() to
    # caller_snapshot() again. Carry the native hook source into that call;
    # otherwise a managed daemon can become the caller halfway through the
    # same evaluation. Each nested snapshot obtains fresh process evidence.
    collector = lambda: workspace.daemon_identity.collect_hook(os.environ, payload)
    with workspace.caller_collection(collector):
        proof_token = _proof.set(None)
        try:
            token = _current.set(resolve(payload))
            try:
                yield
            finally:
                _current.reset(token)
        finally:
            _proof.reset(proof_token)


ERRORS = (workspace.WorkspaceScopeError, workspace.daemon_identity.IdentityError,
          OSError, ValueError, subprocess.SubprocessError)

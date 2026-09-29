"""One atomic, guarded native activation input through the advertised endpoint."""
from __future__ import annotations

import cmux_codex_watch as core
from ccc_native_standby import identifier


def send_initial(client, original, prompt, input_id, *, write_guard):
    """The live ledger supplies the one-shot guard; never fall back or retry.

    Input identity belongs to the durable ledger. The controller receives only
    its existing terminal.paste schema, with one Enter in the same operation.
    """
    identifier(input_id)
    for key in ('workspace_id', 'surface_id'):
        identifier(original[key])
    if not isinstance(prompt, str) or not prompt or '\0' in prompt or not callable(write_guard):
        raise ValueError('invalid standby activation input')
    result = client._control_rpc('terminal.paste', {
        'workspace_id': original['workspace_id'], 'surface_id': original['surface_id'],
        'text': prompt, 'submit_key': 'enter'}, write_guard=write_guard)
    if result is None:
        raise core.InputNotSentError('standby paste unavailable; no fallback permitted')
    return {'input_id': input_id, 'acknowledgement': result}

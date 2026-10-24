"""Test-only native metadata fixture; production entrypoints do not import it."""
import copy
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT_THREAD = '11111111-1111-4111-8111-111111111111'


@contextmanager
def native_processes(fixture=None, caller=None):
    """Replace kernel/foreground/tree inputs, retaining real identity resolution."""
    import cmux_daemon_identity as daemon
    import cmux_workspace_guard as workspace_guard
    import test_cmux_daemon_identity as inputs
    if fixture is None:
        fixture = inputs.DaemonTests()
        fixture.setUp()
    processes = copy.deepcopy(fixture.processes)
    if caller is not None:
        processes[30]['env']['CMUX_SURFACE_ID'] = caller
    tty = 'ttys2' if caller == inputs.T else 'ttys1'

    def live_json(*args):
        if args == ('identify', '--json'):
            return copy.deepcopy(fixture.identity)
        if args == ('tree', '--all', '--json', '--id-format', 'both'):
            return copy.deepcopy(fixture.tree)
        raise AssertionError('unexpected cmux read: ' + repr(args))

    environment = dict(fixture.env, HOME=str(Path.home()))
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        stack.enter_context(patch.object(daemon.sys, 'platform', 'darwin'))
        stack.enter_context(patch.object(daemon.os, 'getppid', return_value=10))
        stack.enter_context(patch.object(
            daemon, 'process', side_effect=lambda pid, **_: copy.deepcopy(processes[pid])))
        stack.enter_context(patch.object(daemon, 'client_candidates', return_value=['10', '30']))
        stack.enter_context(patch.object(
            daemon.foreground, 'read',
            side_effect=lambda process: {'thread_id': inputs.S} if process['pid'] == 30 else None))
        stack.enter_context(patch.object(
            daemon.subprocess, 'run', return_value=SimpleNamespace(stdout=tty)))
        stack.enter_context(patch.object(workspace_guard, '_read_json_command', side_effect=live_json))
        yield


def native_thread(home, actual=ROOT_THREAD, *, inherited=None, parent=None):
    directory = Path(home) / '.codex/sessions/2026/10/12'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ('rollout-2026-10-12T00-00-00-' + actual + '.jsonl')
    meta = dict(id=actual, source='cli')
    if parent:
        meta['source'] = dict(subagent=dict(thread_spawn=dict(parent_thread_id=parent)))
    path.write_text(json.dumps(dict(type='session_meta', payload=meta)) + '\n')
    payload = dict(hook_event_name='PostToolUse', session_id=inherited or actual,
                   transcript_path=str(path), tool_name='exec_command')
    proof = dict(session=actual, foreground=dict(thread_id=actual))
    return payload, proof

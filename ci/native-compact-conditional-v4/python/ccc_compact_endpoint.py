"""Select the Unix endpoint declared by the already-bound live Codex TUI."""
import os
from pathlib import Path

import cmux_native_delivery as native
from cmux_codex_rpc import DEFAULT_SOCKET
from ccc_client_thread_observation import read_foreground
from ccc_request_observation_policy import request_observation_directory_matches


def socket_path_for_binding(binding):
    saved = binding['process']
    live = native.identity.process(saved['pid'])
    if native._public_process(live) != saved:
        raise native.NativeDeliveryError('CODEX_ENDPOINT_PROCESS_CHANGED')
    pins = binding['identity']
    if (live['env'].get('CMUX_SURFACE_ID', '').upper() != pins['target_surface_uuid'].upper()
            or live['env'].get('CMUX_WORKSPACE_ID', '').upper() != pins['workspace_uuid'].upper()
            or read_foreground(saved['pid'], request_observation_directory_matches)[:2]
               != ('ok', binding['session_id'])):
        raise native.NativeDeliveryError('CODEX_ENDPOINT_SESSION_OR_SURFACE_CHANGED')
    argv, endpoints, index = live['argv'], [], 1
    while index < len(argv):
        arg = argv[index]
        # `resume` has its own --remote option too. Only the explicit option
        # terminator makes following strings positional prompt text.
        if arg == '--':
            break
        if arg in ('-c', '--config'):
            index += 2
            continue
        if arg == '--remote':
            if index + 1 >= len(argv):
                raise native.NativeDeliveryError('CODEX_ENDPOINT_ARGUMENT_MISSING')
            endpoints.append(argv[index + 1])
            index += 2
            continue
        if arg.startswith('--remote='):
            endpoints.append(arg.partition('=')[2])
        index += 1
    if not endpoints:
        selected = DEFAULT_SOCKET
    elif len(endpoints) == 1 and endpoints[0].startswith('unix:///'):
        raw = endpoints[0][len('unix://'):]
        selected = Path(raw)
        if (not selected.name or len(os.fsencode(raw)) >= 104
                or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
            raise native.NativeDeliveryError('CODEX_ENDPOINT_PATH_INVALID')
    else:
        raise native.NativeDeliveryError('CODEX_ENDPOINT_NOT_UNIQUE_LOCAL_UNIX')
    after = native.identity.process(saved['pid'])
    if (after != live or read_foreground(saved['pid'], request_observation_directory_matches)[:2]
            != ('ok', binding['session_id'])):
        raise native.NativeDeliveryError('CODEX_ENDPOINT_PROCESS_CHANGED_DURING_READ')
    # DaemonRPC checks the selected socket inode, owner, mode and peer uid.
    # The existing native thread/path check still precedes the single mutation.
    return selected

"""Pure prelaunch config construction. This module never writes or launches."""
import hashlib
import json
from pathlib import Path
import shlex


def prepare(root, source, python, rtk):
    root, source, python, rtk = map(Path, (root, source, python, rtk))
    if not all(p.is_absolute() for p in (root, source, python, rtk)):
        raise ValueError('explicit absolute runner paths required')
    hooks_path = root / 'h/.codex/hooks.json'
    script = source / 'host_probe.py'
    hook_argv = [str(python), '-BE', str(script), '--root', str(root), '--mode', 'hook']
    command = 'exec ' + shlex.join(hook_argv)
    handler = {'type': 'command', 'command': command, 'timeout': 20, 'async': False}
    normalized = {'event_name': 'post_tool_use', 'matcher': 'Bash', 'hooks': [handler]}
    raw = json.dumps(normalized, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    trust = 'sha256:' + hashlib.sha256(raw).hexdigest()
    state_key = str(hooks_path) + ':post_tool_use:0:0'
    handler_id = 'post-tool-use:0:' + str(hooks_path)  # one actual configured handler, no Pre handler
    state_toml = ('[hooks.state.' + json.dumps(state_key) + ']\nenabled=true\ntrusted_hash='
                  + json.dumps(trust) + '\n')
    bootstrap_argv = [str(rtk), 'proxy', str(python), '-BE', str(script),
                      '--root', str(root), '--mode', 'bootstrap']
    return dict(hooks={'hooks': {'PostToolUse': [{'matcher': 'Bash', 'hooks': [handler]}]}},
        hooks_path=str(hooks_path), handler_id=handler_id, normalized=normalized,
        trusted_hash=trust, state_key=state_key, state_toml=state_toml,
        bootstrap_command=shlex.join(bootstrap_argv), child_command=':',
        needed_features={'hooks': True, 'multi_agent_v2': True},
        native_pin_keys=['schema', 'binary_path', 'binary_sha256', 'handler_id'],
        registration_is_loading_proof=False, writes_performed=False)

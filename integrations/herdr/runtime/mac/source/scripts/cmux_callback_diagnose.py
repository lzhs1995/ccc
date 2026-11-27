#!/usr/bin/env python3
"""任务内有界只读诊断：原报告与尝试保持冻结，绝不发送或补按键。"""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys

import cmux_hook_identity as hook_identity
from cmux_evidence_io import read_bytes
from executor_closeout import terminal_report


def allowed(payload, marker, evidence):
    """只允许当前封口任务的一条同步诊断命令，无 shell 尾部。"""
    try:
        task = Path(marker['artifact_root']) / 'task-pack.json'
        tool = payload.get('tool_input', {})
        command = shlex.join(['rtk', 'proxy', str(Path(__file__).resolve()),
                              '--task-pack', str(task)])
        return (payload.get('tool_name') == 'Bash'
                and tool.get('command') == command
                and not tool.get('run_in_background')
                and evidence['task_id'] == marker['task_id']
                and evidence['artifact_root'] == str(task.parent))
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def diagnose(pack_path, payload=None):
    # 身份与任务都来自当前进程和 active marker；命令行只选任务，不授予身份。
    from cmux_consensus_stop_guard import _active_markers
    payload = payload or {}
    task = Path(pack_path)
    if not task.is_absolute() or task.is_symlink() or task.name != 'task-pack.json':
        raise ValueError('an absolute original task-pack path is required')
    with hook_identity.evaluation(payload):
        workspace, caller = hook_identity.identity(payload)
        matches = []
        for marker in _active_markers(payload):
            if Path(marker.get('artifact_root', '')) / 'task-pack.json' != task:
                continue
            peers = marker.get('participants', [])
            supervisors = [p for p in peers if isinstance(p, dict)
                           and p.get('role') == 'supervisor']
            if len(supervisors) != 1:
                continue
            for peer in peers:
                if not isinstance(peer, dict) or not str(peer.get('role', '')).startswith('executor'):
                    continue
                if caller not in (peer.get('surface_uuid'), supervisors[0].get('surface_uuid')):
                    continue
                evidence = terminal_report(marker, workspace, peer.get('surface_uuid'))
                if evidence:
                    matches.append((evidence, marker, peer.get('surface_uuid')))
        if len(matches) != 1:
            raise ValueError('caller has no unique frozen task at this path')
        evidence, marker, executor = matches[0]
        raw = read_bytes(Path(evidence['attempt']))
        if hashlib.sha256(raw).hexdigest() != evidence['attempt_sha256']:
            raise ValueError('original attempt changed during diagnosis')
        attempt = json.loads(raw)
        pastes = [event for event in attempt.get('events', [])
                  if event.get('phase') == 'PASTE_INTENT']
        inputs = [event for event in attempt.get('events', [])
                  if event.get('phase') == 'NATIVE_INPUT_INTENT']
        if terminal_report(marker, workspace, executor) != evidence:
            raise ValueError('frozen report evidence changed during diagnosis')
        return dict(state='WAITING_SUPERVISOR', input_operations=0,
                    receipt_created=False, accepted=False, disarmed=False,
                    delivery_proof='NOT_CHECKED',
                    original_native_binding_present=isinstance(attempt.get('native_binding'), dict),
                    original_paste_fence_present=(len(pastes) == 1 and
                        isinstance(pastes[0].get('native_paste_fence'), dict)),
                    original_native_input_fence_present=(len(inputs) == 1 and
                        isinstance(inputs[0].get('native_input_fence'), dict)),
                    transport='codex-native-input-v1' if inputs else 'terminal',
                    **evidence)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-pack', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(diagnose(args.task_pack), ensure_ascii=False))
        return 0
    except Exception as exc:
        print('CALLBACK_DIAGNOSTIC_UNAVAILABLE: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

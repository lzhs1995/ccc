#!/usr/bin/env python3
"""Observe executor idle once, then allow Stop without asking or sending input.

Only an executor previously observed in an armed task receives an idle notice.
The notice is durable for supervisor discovery. It neither clears that task nor
asserts delivery, and never requires a background process or a follow-up turn.
"""
import json
import sys
import time

import cmux_consensus_stop_guard as stop_guard
import cmux_hook_identity as hook_identity
import cmux_hook_scope as hook_scope
import executor_ready

OPEN_STATES = ('BOUND', 'IDLE_ASKING')
IDLE = 'IDLE_WAITING_DEPENDENCY'


def bindings_dir():
    return executor_ready.idle_root() / 'bindings'


def _binding_path(workspace, surface):
    return bindings_dir() / (executor_ready.digest(json.dumps([workspace, surface])) + '.json')


def _executor_binding(markers, surface):
    for marker in markers:
        participants = marker.get('participants', [])
        if not isinstance(participants, list):
            continue
        for row in participants:
            if (isinstance(row, dict) and str(row.get('role', '')).startswith('executor')
                    and row.get('surface_uuid') == surface):
                supervisor = next((p for p in participants if isinstance(p, dict)
                                   and p.get('role') == 'supervisor'), {})
                return dict(task_id=marker.get('task_id', ''),
                            supervisor_ref=supervisor.get('surface_ref', ''),
                            supervisor_uuid=supervisor.get('surface_uuid', ''))
    return None


def _evaluate_resolved(payload):
    workspace, surface = hook_identity.identity(payload)
    if not surface:
        return True, ''
    path = _binding_path(workspace, surface)
    state = executor_ready.read_json(path) or {}
    session = hook_scope.session_id(payload)
    if session and state.get('native_session_id') != session:
        # A new conversation in a reused pane is not the old executor.
        state = {}
    now = time.time()
    bound = _executor_binding(stop_guard._active_markers(payload), surface)
    if bound:
        loop = executor_ready.read_json(executor_ready.loop_path(surface)) or {}
        if (loop.get('caller_surface_uuid') == surface
                and loop.get('state') in (executor_ready.ASKING, executor_ready.WAITING_DEPENDENCY)):
            executor_ready.stop_loop(surface, 'DISPATCHED')
        # 同一绑定只记一次；Stop 重入或反复结束不刷新 idle 生命周期。
        if (state.get('state') != 'BOUND' or any(state.get(k) != v for k, v in bound.items())
                or state.get('surface_uuid') != surface):
            executor_ready.write_json(path, dict(bound, workspace_uuid=workspace,
                                                 surface_uuid=surface, state='BOUND',
                                                 native_session_id=session,
                                                 last_bound_epoch=now))
        return True, ''
    if state.get('state') in OPEN_STATES:
        # 仅保存发现信息；请求、回复、等待超时由独立文件表达，均不阻塞 Stop。
        episode = executor_ready.digest(json.dumps(
            [workspace, surface, state.get('task_id'), state.get('last_bound_epoch')]))
        executor_ready.write_json(path, dict(
            state, version=3, workspace_uuid=workspace, state=IDLE,
            lifecycle_state=executor_ready.WAITING_DEPENDENCY,
            idle_since_epoch=now, episode_id=episode,
            stop_allowed=True, automatic_input_operations=0,
            loop_record=str(executor_ready.loop_path(surface)),
            mailbox=str(executor_ready.mailbox_dir(surface)),
            next_action='SUPERVISOR_REVIEW_OR_NEW_DISPATCH'))
    return True, ''


def evaluate(payload):
    if not isinstance(payload, dict) or payload.get('hook_event_name') != 'Stop':
        return True, ''
    if payload.get('stop_hook_active') is True:
        return True, ''
    try:
        if not stop_guard._has_active_markers() and not bindings_dir().is_dir():
            return True, ''
        with hook_identity.evaluation(payload):
            return _evaluate_resolved(payload)
    except (hook_identity.ERRORS + (TypeError, KeyError)):
        # idle 是观察器，身份/文件不可读也不能制造 Stop 重试循环。
        return True, ''


def main():
    try:
        raw = sys.stdin.read(executor_ready.MAX_JSON_BYTES + 1)
        if len(raw) > executor_ready.MAX_JSON_BYTES:
            return 0
        payload = json.loads(raw)
    except (ValueError, OSError):
        return 0
    evaluate(payload)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

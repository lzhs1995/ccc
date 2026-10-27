#!/usr/bin/env python3
"""Legacy explicit Stop diagnostic; automatic hooks use the advisory wrapper.

- 求派段 WAITING_REPLY：一律拦 Stop（含 stop_hook_active 重入），要求运行
  `executor_reask.py run`；只有主管答复、新派发或 operator stop 放行。
- 段内已收到答复 ANSWERED：保留原状态；只有执行端显式核全文与 hash 后可消费。
- 无段但 idle binding 显示本 executor 刚空闲（原任务 marker 已 disarm）：自动开段并拦。
hook 自身只读写状态文件，不读屏、不发送任何输入。
"""
import json
import os
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import executor_ready as ready  # noqa: E402
import executor_reask as reask  # noqa: E402
import cmux_hook_identity as hook_identity  # noqa: E402

SELF = Path(__file__).resolve().parent / 'executor_reask.py'
PY = sys.executable or 'python3'
IDLE_STATES = ('IDLE_WAITING_DEPENDENCY', 'IDLE_ASKING')


def _run_cmd(caller):
    return (f'{shlex.quote(PY)} -B {shlex.quote(str(SELF))} run '
            f'--caller-uuid {shlex.quote(caller)}')


def _idle_binding(caller, workspace=''):
    """本 executor 的 idle binding（由 cmux_executor_idle_guard 写）。"""
    return reask.idle_binding(caller, workspace)


def decide(payload, caller, now=None, workspace=''):
    if not isinstance(payload, dict) or payload.get('hook_event_name') != 'Stop' or not caller:
        return False, ''
    try:
        with reask.episode_lock(caller):
            return decide_locked(payload, caller, now, workspace)
    except BlockingIOError:
        # The existing controller owns the episode; do not create another one.
        return True, '[executor-reask] 原求派控制器仍持有状态锁；核原控制器结果，不另开段。'


def decide_locked(payload, caller, now=None, workspace=''):
    """返回 (block: bool, reason: str)。"""
    now = time.time() if now is None else now
    if not isinstance(payload, dict) or payload.get('hook_event_name') != 'Stop' or not caller:
        return False, ''
    state = reask.load(caller)
    if state and workspace and state.get('workspace_uuid') not in ('', None, workspace):
        return False, ''
    transcript = payload.get('transcript_path') or ''
    if state and state.get('state') == reask.WAITING:
        if transcript and not state.get('transcript'):
            try:
                state.update(transcript=transcript, tail_position=ready.Tail(transcript).position())
                reask.save(state)
            except (OSError, ValueError):
                pass
        # 只核答复，不消耗求派轮次；发送只在 run 里做。
        reask.poll_locked(state, now)
        reask.save(state)
    if state and state.get('state') == reask.ANSWERED:
        reply = state.get('reply') or {}
        return False, ('[executor-reask] 主管回件已发现但尚未消费（来源 %s%s）。'
                      '原执行端须读取完整回件并显式 consume --reply-sha256；'
                      'Stop 检查不代读、不消费、不重启求派。\n正文 SHA256：%s'
                      % (reply.get('source', '?'),
                         (' ' + reply['path']) if reply.get('path') else '',
                         reply.get('body_sha256', 'UNKNOWN')))
    if reask.stop_file(caller).exists() or state and state.get('state') == reask.STOPPED:
        return False, ''
    if not state or state.get('state') == reask.CONSUMED:
        binding = _idle_binding(caller, workspace)
        started = float(state.get('started_epoch', 0)) if state else 0.0
        if (binding and binding.get('state') in IDLE_STATES and binding.get('supervisor_uuid')
                and float(binding.get('idle_since_epoch') or 0) > started):
            state = reask.start_locked(caller, binding['supervisor_uuid'], binding.get('task_id', ''),
                                      transcript, executor_ref=caller, now=now, reason='AUTO_IDLE',
                                      workspace=workspace or binding.get('workspace_uuid', ''))
        else:
            return False, ''
    if state.get('state') != reask.WAITING:
        return False, ''
    last = state['asks'][-1] if state.get('asks') else {}
    return True, (
        '[executor-reask] 你是空闲 executor，Codex 主管(%s)尚未答复；禁止停下或空等。'
        '已写出/投递请求 %d 次，上次 %s（%s）。立刻运行：\n%s\n'
        '它每 %d 秒新建一次文件求派，明确报告失败；原终端请求未核收时只走文件通道，'
        '直到主管答复/新派发；返回仍 WAITING_REPLY 就再运行一次。只有 operator 可用 '
        '`executor_reask.py stop` 结束。' % (
            state['supervisor'][:8], state.get('ask_count', 0),
            time.strftime('%H:%M:%SZ', time.gmtime(float(last['at_epoch']))) if last else '尚未',
            last.get('outcome', '-'), _run_cmd(caller), int(reask.INTERVAL)))


def main():
    try:
        payload = json.loads(sys.stdin.read(ready.MAX_JSON_BYTES + 1) or '{}')
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict) or payload.get('hook_event_name') != 'Stop':
        return 0
    try:
        with hook_identity.evaluation(payload):
            workspace, caller = hook_identity.identity(payload)
            if not caller or workspace == 'default':
                return 0
            block, reason = decide(payload, caller, workspace=workspace)
    except hook_identity.ERRORS as exc:
        # An unauthenticated daemon caller is an expected refusal, not a crash.
        print(f'[executor-reask] IDENTITY_REFUSED {type(exc).__name__}', file=sys.stderr)
        return 0
    except Exception as exc:  # 状态损坏时不制造无提示死循环，但要在 stderr 留证
        print(f'[executor-reask] INTERNAL_ERROR {type(exc).__name__}', file=sys.stderr)
        return 0
    if block:
        print(json.dumps(dict(decision='block', reason=reason), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

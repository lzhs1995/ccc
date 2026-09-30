"""Route the existing b/N UI action to its original live standby owner.

No launch, preparation, disk recovery or retry occurs on this path. The live
owner still checks readiness and authorization at its actual input boundary.
"""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import cmux_codex_watch as core
import ccc_workspace_batch as batch
import ccc_standby_launch as launch
import ccc_batch_timing as ui
from ccc_native_standby import identifier
from ccc_standby_service import request
from ccc_standby_timing import _origin, _read


def owner_path(config_path, job_id):
    return batch.job_path(config_path, identifier(job_id)).parent / 'standby' / 'owner.json'


def activate_existing(config_path, job, *, mode, origin):
    """Called while batch.start holds the selected workspace start lock."""
    config_path = Path(config_path).resolve(strict=True)
    jobfile = batch.job_path(config_path, identifier(job['id']))
    current, job_raw = _read(jobfile)
    if current != job:
        raise RuntimeError('待机原批次已变化；未创建或重发任务')
    selected = launch.policy(job, config_path)
    if mode != selected['mode'] or origin is None:
        raise RuntimeError('待机激活须使用原模式及完整的按钮确认计时')
    origin = copy.deepcopy(_origin(origin, selected))
    if origin['source_hashes'] != ui.source_hashes():
        raise RuntimeError('待机按钮与当前运行版本不一致')
    path = owner_path(config_path, job['id'])
    binding, raw = _read(path)
    if (binding.get('version') != 1 or binding.get('kind') != 'standby_owner_binding'
            or any(binding.get(k) != v for k, v in selected.items())):
        raise RuntimeError('待机服务不属于原批次；未启动替代服务')
    spec_path = Path(binding['spec_path'])
    spec, spec_raw = _read(spec_path)
    sha = hashlib.sha256(spec_raw).hexdigest()
    if (sha != binding.get('spec_sha256') or spec.get('kind') != 'standby_live_owner'
            or any(spec.get(k) != v for k, v in selected.items())):
        raise RuntimeError('待机服务描述与原批次绑定不一致')

    def check():
        latest, latest_raw = _read(jobfile)
        if (latest_raw != job_raw or _read(path)[1] != raw
                or _read(spec_path)[1] != spec_raw
                or ui.boot_id() != selected['boot_id']):
            raise RuntimeError('原待机批次、服务或开机身份已变化')
        config = core.ConfigStore(config_path).load()
        rule = core.workspace_rule_by_id(config, selected['workspace_id'])
        if (not batch.allowed(config, latest)
                or rule.get('last_batch_id') != selected['job_id']):
            raise RuntimeError('待机原批次已暂停、取消或被替换')

    def validate(result):
        if (not isinstance(result, dict)
                or any(result.get(k) != v for k, v in selected.items())
                or result.get('job_terminal') is not False
                or result.get('run_terminal') is not False):
            raise RuntimeError('待机服务返回了其他批次或错误的终态')
        return result

    check()
    state = validate(request(spec_path, sha, 'status'))
    check()
    # A repeated click reports the original action. It never forwards a new
    # action to an already consumed cohort, even when the first RPC lost ACK.
    if state.get('action_id') is None and state.get('state') == 'ready':
        state = validate(request(spec_path, sha, 'activate', origin=origin))
        if state.get('action_id') != origin['action_id']:
            raise RuntimeError('待机激活返回未绑定原按钮动作；请查询原状态')
        check()
    return {'job_id': selected['job_id'], 'workspace_id': selected['workspace_id'],
        'startup_mode': 'private_check', 'new_job': False, 'standby': state,
        'message': ('正在准备原生待机，尚未提交任务' if state.get('state') in {'admitted', 'preparing'}
                    else '原待机批次状态；首任务与续跑结果分别验收')}

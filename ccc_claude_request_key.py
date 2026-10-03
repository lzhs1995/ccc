"""Read Claude request headers captured by its opt-in Bun preload observer."""
import json
import os
from pathlib import Path
import stat
import time
import uuid

import ccc_guard_scope as scope


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def observe(result):
    result.api_key_observed = ''
    result.api_key_observation_historical = False
    result.api_key_observation_status = 'unverified'
    result.api_key_observation_note = '尚未采集该 Claude 进程的实际请求；不会以全局或独立配置值替代。'
    if not result.ok or result.agent_kind != 'claude':
        return
    try:
        pid = result.pid
        before = scope.birth(pid)
        argv, env = scope.arguments(pid)
        directory = env.get('CCC_CLAUDE_REQUEST_OBSERVATIONS_DIR')
        if not before or not argv or not directory:
            return
        directory = Path(directory)
        root = directory.lstat()
        if (not directory.is_absolute() or not stat.S_ISDIR(root.st_mode)
                or root.st_uid != os.getuid() or root.st_mode & 0o077):
            return
        path = directory / f'claude-{pid}-request.json'
        initial = path.lstat()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 16384
                    or _identity(initial) != _identity(info)):
                return
            raw = stream.read(16385)
            if len(raw) > 16384 or _identity(os.fstat(stream.fileno())) != _identity(info):
                return
        data = json.loads(raw)
        if (not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1
                or data.get('purpose') != 'claude_request_attempt'
                or type(data.get('pid')) is not int or data['pid'] != pid
                or data.get('session_id') != result.session_id
                or type(data.get('sequence')) is not int or data['sequence'] < 1
                or not isinstance(data.get('observer_epoch'), str)
                or str(uuid.UUID(data['observer_epoch'])) != data['observer_epoch']):
            return
        stamp = data.get('observed_at_ms')
        if (type(stamp) is not int or stamp < before[0] * 1000 + before[1] / 1000
                or stamp > time.time() * 1000 + 1000):
            return
        if data.get('credential_scope') != 'first_hop_headers':
            result.api_key_observation_note = '最近请求发生重定向，无法确认末跳 Key；已清除旧值。'
            return
        authorization, key = data.get('authorization'), data.get('api_key')
        token = ''
        if isinstance(authorization, str):
            scheme, separator, value = authorization.partition(' ')
            if separator and scheme.lower() == 'bearer':
                token = value
            elif authorization:
                return
        values = [v for v in (token, key) if v]
        if not values or any(not isinstance(v, str) or not 0 < len(v) <= 4096
                             or not all(32 < ord(c) < 127 for c in v) for v in values):
            return
        if (scope.birth(pid) != before or scope.arguments(pid) != (argv, env)
                or _identity(path.lstat()) != _identity(info)
                or _identity(directory.lstat())[:4] != _identity(root)[:4]):
            return
        result.api_key_observed = (f'Bearer {token}; x-api-key {key}'
                                   if token and key and token != key else values[0])
        result.api_key_observation_status = 'observed'
        when = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stamp / 1000))
        result.api_key_observation_note = (
            f'Claude 最近实际请求 {when}；PID {pid}；采自首跳认证请求头，'
            '不代表服务端接受或下一次请求。全局与 ccp-new 配置均以实际发送值为准。')
    except FileNotFoundError:
        result.api_key_observation_status = 'absent'
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        result.api_key_observation_note = 'Claude 请求记录读取或进程身份核验失败；已清除旧值。'

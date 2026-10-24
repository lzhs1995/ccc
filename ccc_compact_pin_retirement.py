"""Retire exactly receipted native control pins into a write-once settled archive.

未决 pin 永久保留为 active：不过期、不重放、不退还、不清理。只有原始请求的
typed 终态回执（compact-started / not-interrupted / compact-denied）通过与基线
相同的精确核验后，才把原 pin 字节与回执字节完整写入只写一次的 settled 归档，
然后只移除本方核过 inode 的 active pin。终态退役不等于输入或模型完成，
也不产生任何新的提交预算。所有合法写入方由单一 service FileLock + 共享 RLock 覆盖。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from ccc_compact_support import OwnedDirectory, validate_native_receipt_fields

ACTIVE = 'native-control-pins-v4'
SETTLED = 'native-control-pins-settled-v1'
PIN_SCHEMA = 'ccc-native-control-pin-v4'
SETTLED_SCHEMA = 'ccc-native-control-pin-settled-v1'
PIN_NAME = re.compile(r'([0-9a-f]{24})\.json\Z')
MAX_UNRESOLVED = 1024      # 同时未决的原始控制上限；已结算的不计入，不是终身上限
MAX_LISTING = 4096         # active 目录列举的内存上界（基线遗留目录最多 1025 项）
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


def _json(raw):
    # 与 read_json 相同：拒绝非有限数，必须是对象
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    if not isinstance(value, dict):
        raise ValueError('journal object required')
    return value


def _strict_identity(directory, name):
    """经持有的目录 fd 取 nofollow 身份；常规文件、本 uid、非组写、nlink==1。"""
    return directory._file_identity(name)


def terminal_receipt(index_parent, key, request):
    """返回 (phase, receipt_name, receipt_raw)；无回执返回 None；不合格回执抛错（fail-closed）。"""
    intent = Path(request.get('intentPath', ''))
    if (not intent.is_absolute() or intent.parent.parent != index_parent
            or intent.name != key + '-interrupt.json'):
        raise ValueError('native control pin is outside the enrolled journal')
    with OwnedDirectory(intent.parent) as journal:
        # 顺序与基线一致：先 compact-started，再 not-interrupted、compact-denied
        phase = None
        for candidate in ('compact-started', 'not-interrupted', 'compact-denied'):
            if _strict_identity(journal, key + '-' + candidate + '.json') is not None:
                phase = candidate
                break
        if phase is None:
            return None
        name = key + '-' + phase + '.json'
        raw = journal.read_bytes(name, limit=MAX_RECORD_BYTES)
    value = _json(raw)
    fields = value.get('fields')
    if (value.get('episode_key') != key or value.get('original_intent') != str(intent)
            or not isinstance(fields, dict)):
        raise ValueError('native terminal receipt is not for the original request')
    if phase == 'compact-started':
        if 'turnId' not in fields:
            raise ValueError('native terminal receipt is not for the original request')
    elif fields.get('turnId') != request.get('expectedTurnId') or fields.get('attemptId') != key:
        raise ValueError('native denial receipt is not for the original request')
    validate_native_receipt_fields(fields, phase, request)
    return phase, name, raw


def settled_record(key, pin_raw, phase, receipt_name, receipt_raw):
    """确定性归档字节：无时间戳，崩溃后重算得到同一字节，才能幂等恢复。"""
    def blob(name, raw):
        return dict(name=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                    raw_base64=base64.b64encode(raw).decode('ascii'))
    value = dict(schema=SETTLED_SCHEMA, attempt_id=key, terminal_phase=phase,
                 pin=blob(key + '.json', pin_raw), receipt=blob(receipt_name, receipt_raw),
                 replay_authorized=False, submission_budget_created=False,
                 meaning='original typed native terminal receipt observed; not input or model completion')
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')) + '\n').encode()


# 崩溃注入点：测试可替换为抛错，以模拟各步之间的进程死亡
def _crash(point):
    return None


def _publish_temp(name):
    return '.' + name + '.publish-' + secrets.token_hex(12) + '.tmp'


def _is_publish_temp(entry, name):
    head, tail = '.' + name + '.publish-', '.tmp'
    return (entry.startswith(head) and entry.endswith(tail)
            and re.fullmatch(r'[0-9a-f]{24}', entry[len(head):-len(tail)]) is not None)


def _read_linked(directory, name, limit):
    """只用于恢复 link→unlink 窗口：允许 nlink==2，其他属性仍严格。"""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory.fds[-1])
    with os.fdopen(fd, 'rb') as stream:
        st = os.fstat(stream.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022
                or st.st_size > limit):
            raise ValueError('untrusted settled archive')
        raw = stream.read(limit + 1)
        if len(raw) != st.st_size or os.fstat(stream.fileno()).st_mtime_ns != st.st_mtime_ns:
            raise ValueError('settled archive changed during read')
    return st, raw


def _recover_link_window(directory, name, raw):
    """最终名 nlink==2：只有恰好一个本方发布临时名指向同一 inode 且字节相同才收尾。"""
    final = os.stat(name, dir_fd=directory.fds[-1], follow_symlinks=False)
    if not stat.S_ISREG(final.st_mode) or final.st_nlink != 2:
        raise ValueError('untrusted settled archive')
    twins = []
    for entry in os.listdir(directory.fds[-1]):
        if _is_publish_temp(entry, name):
            st = os.stat(entry, dir_fd=directory.fds[-1], follow_symlinks=False)
            if (st.st_dev, st.st_ino) == (final.st_dev, final.st_ino):
                twins.append(entry)
    if len(twins) != 1:
        raise ValueError('settled archive has a foreign second link')
    _, current = _read_linked(directory, name, MAX_ARCHIVE_BYTES)
    if current != raw:
        raise ValueError('settled archive differs from the original evidence')
    # 两个名字是同一 inode：去掉临时名不删除任何唯一证据
    os.unlink(twins[0], dir_fd=directory.fds[-1])
    os.fsync(directory.fds[-1])
    directory.verify()
    _strict_identity(directory, name)


def publish_once(directory, name, raw):
    """原子、不替换地发布内容寻址/只写一次的文件；返回 True=本次发布，False=已有同字节。

    写临时名→fsync→link(不替换)→fsync 目录→unlink 临时名。最终名只会以完整字节出现；
    崩溃留在 link 与 unlink 之间时最终名 nlink==2，由 _recover_link_window 精确收尾，
    不放宽常规单链接检查。最终名已有不同字节或外来文件时 fail-closed。
    """
    name = directory._name(name)
    directory.verify()
    try:
        existing = os.stat(name, dir_fd=directory.fds[-1], follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISREG(existing.st_mode) and existing.st_nlink == 2:
            _recover_link_window(directory, name, raw)
        if directory.read_bytes(name, limit=MAX_ARCHIVE_BYTES) != raw:
            raise ValueError('settled archive differs from the original evidence')
        return False
    temporary = _publish_temp(name)
    directory.write_once_bytes(temporary, raw)
    prepared = _strict_identity(directory, temporary)
    _crash('after_temp')
    os.link(temporary, name, src_dir_fd=directory.fds[-1], dst_dir_fd=directory.fds[-1],
            follow_symlinks=False)
    os.fsync(directory.fds[-1])
    _crash('after_link')
    linked = os.stat(name, dir_fd=directory.fds[-1], follow_symlinks=False)
    if (linked.st_dev, linked.st_ino) != (prepared.st_dev, prepared.st_ino):
        raise ValueError('settled archive replaced during publication')
    os.unlink(temporary, dir_fd=directory.fds[-1])
    os.fsync(directory.fds[-1])
    directory.verify()
    _strict_identity(directory, name)
    return True


def settled_exists(index_parent, key):
    """已结算的原始控制不能再被冻结成新 pin（不产生新预算）。"""
    path = Path(index_parent) / SETTLED
    if not path.exists():
        return False
    with OwnedDirectory(path) as settled:
        try:
            os.stat(key + '.json', dir_fd=settled.fds[-1], follow_symlinks=False)
        except FileNotFoundError:
            return False
    return True


def _retire_active(active, name, held, raw):
    """只移除本方核过 inode 与字节的 active pin；被替换则 fail-closed 保留。"""
    current = _strict_identity(active, name)
    if current is None or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
        raise ValueError('active control pin replaced before retirement')
    if active.read_bytes(name, limit=MAX_RECORD_BYTES) != raw:
        raise ValueError('active control pin changed before retirement')
    _crash('before_unlink')
    os.unlink(name, dir_fd=active.fds[-1])
    os.fsync(active.fds[-1])
    active.verify()


def control_is_pending(index_path):
    """True=仍有未收到原始终态回执的控制；每个精确终态 pin 先归档再退役一次。

    调用方须持有 IntentIndex.lock（且进程持有 service FileLock）。每次只读 active 目录；
    已结算历史不在常规 tick 中重读，累计结算数量不受限制。
    """
    parent = Path(index_path).parent
    path = parent / ACTIVE
    if not path.exists():
        return False
    pending = 0
    settled = None
    try:
        with OwnedDirectory(path) as active:
            names = os.listdir(active.fds[-1])
            if len(names) > MAX_LISTING:
                raise ValueError('native control pin listing exceeds its memory bound')
            for name in sorted(names):
                match = PIN_NAME.fullmatch(name)
                if match is None:
                    raise ValueError('unknown native control pin entry')
                key = match[1]
                held = _strict_identity(active, name)
                if held is None:
                    raise ValueError('native control pin disappeared')
                raw = active.read_bytes(name, limit=MAX_RECORD_BYTES)
                lease = _json(raw)
                request = lease.get('client_request') or {}
                if (lease.get('schema') != PIN_SCHEMA or request.get('attemptId') != key
                        or lease.get('replay_authorized') is not False):
                    raise ValueError('native control pin binding changed')
                receipt = terminal_receipt(parent, key, request)
                if receipt is None:
                    pending += 1
                    if pending > MAX_UNRESOLVED:
                        raise ValueError('unresolved native control pins exceed their bound')
                    continue
                phase, receipt_name, receipt_raw = receipt
                if settled is None:
                    (parent / SETTLED).mkdir(mode=0o700, exist_ok=True)
                    settled = OwnedDirectory(parent / SETTLED)
                publish_once(settled, key + '.json',
                             settled_record(key, raw, phase, receipt_name, receipt_raw))
                _crash('after_archive')
                _retire_active(active, name, held, raw)
    finally:
        if settled is not None:
            settled.close()
    return pending > 0

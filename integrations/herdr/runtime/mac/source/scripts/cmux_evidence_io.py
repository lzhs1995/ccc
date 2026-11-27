"""有界、只读的本地证据读取；特殊文件与并发替换不算稳定证据。"""
import os
from pathlib import Path
import stat

MAX_EVIDENCE_BYTES = 8 * 1024 * 1024
MAX_ATTEMPTS = 128


def open_regular_lock(path):
    """非阻塞打开锁文件；FIFO、链接和替换不能卡死发送/核收。"""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('lock path must be absolute')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        held, current = os.fstat(fd), path.lstat()
        if (not stat.S_ISREG(held.st_mode) or held.st_nlink != 1
                or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('lock is not the original single-link regular file')
        return os.fdopen(fd, 'a+b')
    except BaseException:
        os.close(fd)
        raise


def snapshot(path, limit=MAX_EVIDENCE_BYTES):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('evidence path must be absolute')
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError('evidence is not a bounded regular file')
        raw = stream.read(limit + 1)
        after, current = os.fstat(stream.fileno()), path.lstat()
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)
    if (len(raw) > limit or not stat.S_ISREG(current.st_mode)
            or identity(before) != identity(after)
            or identity(before) != identity(current)):
        raise ValueError('evidence changed during the bounded read')
    return raw, identity(before)


def read_bytes(path, limit=MAX_EVIDENCE_BYTES):
    return snapshot(path, limit)[0]


def attempt_paths(journal):
    paths = []
    for path in Path(journal).glob('attempt-*.json'):
        paths.append(path)
        if len(paths) > MAX_ATTEMPTS:
            raise ValueError('attempt count exceeds bounded evidence budget')
    return sorted(paths)

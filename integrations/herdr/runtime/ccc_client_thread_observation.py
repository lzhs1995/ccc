"""Read native foreground-thread evidence without reading credential configuration."""
import json
import hashlib
import os
from pathlib import Path
import stat
import time
import uuid

import ccc_guard_scope as scope
import ccc_foreground_lease as foreground_lease

# Losing a record must not resurrect the startup argv in this panel process.
_seen = set()


_authenticated_generations = set()


LEASE_MS = 5000
_leased_generations = {}


def _monotonic_ns():
    # Explicit shared OS clock; Python's platform-dependent monotonic() and
    # Rust Instant remain local timers, never wire-clock substitutes.
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("ambiguous foreground observation")
        result[name] = value
    return result


def _lease_candidate(data, key, epoch, stamp, thread, root_identity, directory):
    return foreground_lease.validate(
        data, key, epoch, stamp, thread, root_identity, directory,
        cache=_leased_generations, wall_ms=time.time() * 1000,
        local_mono=time.monotonic(), wire_ns=_monotonic_ns())


def read_foreground(pid, opt_in, directory=None, *, _record=None):
    """Return (status, thread_id, evidence).

    A null thread is an explicit selection clear, not permission to use argv.
    Evidence contains no key and can be compared again at display publication.
    Observer-enabled launchers set CODEX_CLIENT_THREAD_OBSERVER=1 in the exec
    environment. This is available even if the very first publication fails,
    and survives panel restarts without relying on this module's seen cache.
    """
    if _record is not None:
        # v2 合同：入口清空调用方容器；只有全部复核通过的 ok 分支写入，其余出口一律为空。
        _record.clear()
    generation = scope.birth(pid, codex=True)
    # A failed identity read is not evidence of PID reuse or a legacy client.
    # Otherwise a missing record can revive the startup thread and its Key.
    if generation is None:
        return "invalid", None, None
    key = (pid, tuple(generation))
    try:
        argv, env = scope.arguments(pid)
        # A native app-server inherits the client's observer flag, but never
        # publishes a TUI selection. Bind this role to live argv and birth;
        # callers retain the evidence and recheck it at publication time.
        # Match the subcommand position, never a prompt/config value.
        if len(argv) > 1 and argv[1] == "app-server":
            if (scope.birth(pid, codex=True) != generation
                    or scope.arguments(pid)[0] != argv
                    or scope.birth(pid, codex=True) != generation):
                return "invalid", None, None
            return "nonforeground", None, (key, tuple(argv))
        required = env.get("CODEX_CLIENT_THREAD_OBSERVER")
        if required not in (None, "1"):
            return "invalid", None, None
        # Native TUI publish() uses config.codex_home/credential-observations
        # and its enabled-v1 marker. The transport's explicit request-directory
        # override does not relocate this foreground-selection record or waive
        # that marker, so validate against the home rule independently.
        foreground_env = dict(env)
        foreground_env.pop("CODEX_CREDENTIAL_OBSERVATIONS_DIR", None)
        home = Path(env.get("CODEX_HOME") or str(Path(env.get("HOME") or Path.home()) / ".codex"))
        directory = Path(directory) if directory is not None else home / "credential-observations"
        path = directory / f"client-{pid}-thread.json"
        try:
            initial = path.lstat()
        except FileNotFoundError:
            return ("invalid" if key in _seen
                    else "absent"), None, None
        if generation is None or not argv:
            return "invalid", None, None
        if len(_seen) >= 4096 and key not in _seen:
            return "invalid", None, None
        _seen.add(key)
        if not opt_in(foreground_env, directory):
            return "invalid", None, None
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        root = directory.lstat()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or before.st_size > 4096):
                return "invalid", None, None
            raw = stream.read(4097)
            after = os.fstat(stream.fileno())
        if (identity(initial) != identity(before) or identity(before) != identity(after)
                or identity(after) != identity(path.lstat()) or len(raw) > 4096):
            return "invalid", None, None
        data = json.loads(raw, object_pairs_hook=_unique_object)
        if (not isinstance(data, dict) or type(data.get("schema")) is not int
                or data["schema"] not in (1, 2) or type(data.get("pid")) is not int
                or data["pid"] != pid or data.get("purpose") != "client_foreground_thread"):
            return "invalid", None, None
        if data["schema"] == 1 and key in _leased_generations:
            return "invalid", None, None
        epoch = data.get("client_epoch")
        if not isinstance(epoch, str) or str(uuid.UUID(epoch)) != epoch:
            return "invalid", None, None
        stamp = data.get("published_at_ms")
        if (type(stamp) is not int or stamp < generation[0] * 1000 + generation[1] / 1000
                or stamp > time.time() * 1000 + 1000):
            return "invalid", None, None
        thread = data["thread_id"]
        if thread is not None and (not isinstance(thread, str) or str(uuid.UUID(thread)) != thread):
            return "invalid", None, None
        lease = lease_evidence = None
        if data["schema"] == 2:
            lease, lease_evidence = _lease_candidate(
                data, key, epoch, stamp, thread, identity(root), directory)
        if (scope.birth(pid, codex=True) != generation
                or not opt_in(foreground_env, directory)
                or identity(root)[:4] != identity(directory.lstat())[:4]
                or identity(after) != identity(path.lstat())
                or (data["schema"] == 2 and scope.arguments(pid) != (argv, env))):
            return "invalid", None, None
        if lease is not None:
            final_now = time.time() * 1000
            if (data["expires_at_ms"] <= final_now or stamp > final_now + 1000
                    or data["monotonic_expires_ns"] <= _monotonic_ns()
                    or time.monotonic() >= lease["deadline"]):
                return "invalid", None, None
        _authenticated_generations.add(key)
        if lease is not None:
            _leased_generations[key] = lease
        result = "ok", thread, (lease_evidence if lease is not None
                                else (key, epoch, stamp, identity(after), str(directory)))
        if _record is not None:
            # 同一 fd 读出、全部复核通过后的同一份记录；不二次打开/解析。返回值先算好再写入。
            _record.update(data)
            _record['_source_sha256'] = hashlib.sha256(raw).hexdigest()
            _record['_source_path'] = str(path)
        return result
    except (OSError, ValueError, TypeError, KeyError):
        if _record is not None:
            _record.clear()  # 异常出口同样不留半份记录
        return "invalid", None, None

def stale_optional_foreground(pid, opt_in):
    """Private proof of a complete record from a previous PID generation.

    This does not change read_foreground's invalid/clear contract. Only a
    separately authenticated standalone writer may use it as a locator veto
    exception, and must obtain the identical proof again after reading.
    """
    try:
        generation = scope.birth(pid, codex=True)
        if generation is None:
            return None
        key = (pid, tuple(generation))
        if key in _authenticated_generations or len(_authenticated_generations) >= 4096:
            return None
        argv, env = scope.arguments(pid)
        if not argv or env.get('CODEX_CLIENT_THREAD_OBSERVER') not in (None, '1'):
            return None
        home = Path(env.get('CODEX_HOME') or str(Path(env.get('HOME') or Path.home()) / '.codex'))
        directory = home / 'credential-observations'
        foreground_env = dict(env)
        foreground_env.pop('CODEX_CREDENTIAL_OBSERVATIONS_DIR', None)
        if not opt_in(foreground_env, directory):
            return None
        path = directory / f'client-{pid}-thread.json'
        root = directory.lstat()
        initial = path.lstat()
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_mode & 0o077 or before.st_size > 4096):
                return None
            raw = handle.read(4097)
            after = os.fstat(handle.fileno())
        if (identity(initial) != identity(before) or identity(before) != identity(after)
                or identity(after) != identity(path.lstat()) or len(raw) > 4096):
            return None
        data = json.loads(raw)
        if (not isinstance(data, dict) or type(data.get('schema')) is not int
                or data['schema'] != 1 or data.get('purpose') != 'client_foreground_thread'
                or type(data.get('pid')) is not int or data['pid'] != pid):
            return None
        epoch, thread, stamp = data.get('client_epoch'), data.get('thread_id'), data.get('published_at_ms')
        if (not isinstance(epoch, str) or str(uuid.UUID(epoch)) != epoch
                or not isinstance(thread, str) or str(uuid.UUID(thread)) != thread
                or type(stamp) is not int or not 0 <= stamp < generation[0] * 1000 + generation[1] / 1000):
            return None
        if (scope.birth(pid, codex=True) != generation or scope.arguments(pid) != (argv, env)
                or key in _authenticated_generations or len(_authenticated_generations) >= 4096
                or not opt_in(foreground_env, directory)
                or identity(root)[:4] != identity(directory.lstat())[:4]
                or identity(after) != identity(path.lstat())):
            return None
        return {'pid': pid, 'birth': list(generation), 'path': str(path),
                'identity': identity(after), 'sha256': hashlib.sha256(raw).hexdigest(),
                'previous_thread': thread, 'previous_epoch': epoch, 'published_at_ms': stamp}
    except (OSError, ValueError, TypeError, KeyError):
        return None


COMPACT_INTERRUPT_CAPABILITY = "thread-compact-interrupt-v4"


def read_compact_capability(pid, opt_in, directory=None):
    """Return (status, thread_id, evidence, capable). capable 仅当同一认证记录携带精确常量。

    缺失/未知/旧记录一律 False；不看 argv、不看旧记录、不看环境变量。
    """
    record = {}
    status, thread, evidence = read_foreground(pid, opt_in, directory, _record=record)
    capable = (status == "ok" and thread is not None
               and record.get("compact_interrupt_capability") == COMPACT_INTERRUPT_CAPABILITY)
    return status, thread, evidence, capable

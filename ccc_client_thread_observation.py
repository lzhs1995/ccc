"""Read native foreground-thread evidence without reading credential configuration."""
import json
import os
from pathlib import Path
import stat
import time
import uuid

import ccc_guard_scope as scope

# Losing a record must not resurrect the startup argv in this panel process.
_seen = set()


def read_foreground(pid, opt_in, directory=None):
    """Return (status, thread_id, evidence).

    A null thread is an explicit selection clear, not permission to use argv.
    Evidence contains no key and can be compared again at display publication.
    Observer-enabled launchers set CODEX_CLIENT_THREAD_OBSERVER=1 in the exec
    environment. This is available even if the very first publication fails,
    and survives panel restarts without relying on this module's seen cache.
    """
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
            return ("invalid" if required == "1" or key in _seen
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
        data = json.loads(raw)
        if (not isinstance(data, dict) or type(data.get("schema")) is not int
                or data["schema"] != 1 or type(data.get("pid")) is not int
                or data["pid"] != pid or data.get("purpose") != "client_foreground_thread"):
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
        if (scope.birth(pid, codex=True) != generation
                or not opt_in(foreground_env, directory)
                or identity(root)[:4] != identity(directory.lstat())[:4]
                or identity(after) != identity(path.lstat())):
            return "invalid", None, None
        return "ok", thread, (key, epoch, stamp, identity(after), str(directory))
    except (OSError, ValueError, TypeError, KeyError):
        return "invalid", None, None

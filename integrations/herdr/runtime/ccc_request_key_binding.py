"""Read-only Darwin socket association for native credential observations."""
import ctypes
import re
import struct
import subprocess
import sys

import ccc_guard_scope as scope
import ccc_codex_queue as native


def native_unix_pairs(pid):
    """Read opaque socket/peer handles, including named accepted sockets.

    Darwin lsof prints the bound path instead of the peer for accepted sockets.
    libproc exposes both handles. Layout is Darwin's public socket_fdinfo ABI
    (792 bytes); a different/partial ABI is rejected, never guessed.
    """
    if sys.platform != 'darwin' or native._proc_pidinfo is None or native._proc_pidfdinfo is None:
        raise OSError('native socket identity unavailable')
    item_size = ctypes.sizeof(native._FdInfo)
    needed = native._proc_pidinfo(pid, 1, 0, None, 0)
    if needed <= 0 or needed % item_size or needed // item_size > 16320:
        raise OSError('invalid descriptor inventory')
    entries = (native._FdInfo * (needed // item_size + 64))()
    count = native._proc_pidinfo(pid, 1, 0, entries, ctypes.sizeof(entries))
    if count <= 0 or count >= ctypes.sizeof(entries) or count % item_size:
        raise OSError('incomplete descriptor inventory')
    pairs = set()
    for entry in entries[:count // item_size]:
        if entry.kind != 2:  # PROX_FDTYPE_SOCKET
            continue
        buf = ctypes.create_string_buffer(792)
        if native._proc_pidfdinfo(pid, entry.fd, 3, buf, len(buf)) != len(buf):
            raise OSError('incomplete socket identity')
        raw = buf.raw
        if struct.unpack_from('=i', raw, 184)[0] != 1 or struct.unpack_from('=i', raw, 256)[0] != 3:
            continue  # AF_UNIX, SOCKINFO_UN
        own = struct.unpack_from('=Q', raw, 160)[0]
        peer = struct.unpack_from('=Q', raw, 264)[0]
        if own and peer and own != peer:
            pairs.add((own, peer))
    return pairs


def unix_pairs(raw):
    """Parse lsof field output into PID -> connected Unix endpoint pairs."""
    pairs = {}
    pid = None
    item = {}

    def finish():
        address = item.get("d", "")
        peer = item.get("n", "")
        if (pid is not None and item.get("t") == "unix"
                and re.fullmatch(r"0x[0-9a-fA-F]+", address)
                and re.fullmatch(r"->0x[0-9a-fA-F]+", peer)):
            endpoints = (int(address, 16), int(peer[2:], 16))
            if all(endpoints) and endpoints[0] != endpoints[1]:
                pairs.setdefault(pid, set()).add(endpoints)

    for line in raw.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field in ("p", "f"):
            finish()
            item = {}
        if field == "p":
            pid = int(value) if value.isdecimal() else None
        elif field in ("t", "d", "n"):
            item[field] = value
    finish()
    return pairs


def connected_writer(cli_pid, writer_pid, cli_birth, writer_birth, *, runner=None):
    """Require the same process or a reciprocal live Unix socket connection.

    Socket path names, a matching thread in another daemon, and process names
    alone do not establish association. Both births bracket this read. The
    caller repeats it before publishing to reject disconnects during parsing.
    """
    if (type(cli_pid) is not int or cli_pid <= 0 or not cli_birth
            or scope.birth(cli_pid, codex=True) != cli_birth
            or scope.birth(writer_pid, codex=True) != writer_birth):
        return False
    if cli_pid == writer_pid:
        return cli_birth == writer_birth
    try:
        if runner is None and sys.platform == 'darwin':
            first_cli = native_unix_pairs(cli_pid)
            first_writer = native_unix_pairs(writer_pid)
            shared = {(b, a) for a, b in first_cli} & first_writer
            if not shared:
                return False
            # Confirm the exact connection in both processes after the first
            # reads. Unrelated socket churn need not invalidate this pair.
            last_cli = native_unix_pairs(cli_pid)
            last_writer = native_unix_pairs(writer_pid)
            stable = shared & last_writer & {(b, a) for a, b in last_cli}
            return (bool(stable) and scope.birth(cli_pid, codex=True) == cli_birth
                    and scope.birth(writer_pid, codex=True) == writer_birth)
        result = (runner or subprocess.run)(
            ["/usr/sbin/lsof", "-nP", "-a", "-p", f"{cli_pid},{writer_pid}",
             "-U", "-F", "pftdn"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 2 * 1024 * 1024:
            return False
        pairs = unix_pairs(result.stdout)
        shared = {(b, a) for a, b in pairs.get(cli_pid, set())} & pairs.get(writer_pid, set())
        return (bool(shared) and scope.birth(cli_pid, codex=True) == cli_birth
                and scope.birth(writer_pid, codex=True) == writer_birth)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False

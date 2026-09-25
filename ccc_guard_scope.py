"""Exact local process ownership for B guards; no name-based signalling.

Darwin's microsecond process birth identity is checked on both sides of every
argv/environment read. Only Codex processes with a real cmux surface UUID are
returned. Current workspace membership always comes from cmux, separately.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import sys
import uuid

import ccc_codex_queue as native


def birth(pid, *, codex=False):
    if native._proc_pidinfo is None or type(pid) is not int or not 1 < pid < 2**31:
        return None
    info = native._BsdInfo()
    size = ctypes.sizeof(info)
    if (native._proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size
            or info.pid != pid or info.status == 5 or not info.start_sec
            or (codex and (info.name or info.comm) != b"codex")):
        return None
    return [info.start_sec, info.start_usec]


def arguments(pid):
    if native._procargs_sysctl is None or not 0 < native._procargs_bytes <= 2 * 1024 * 1024:
        raise RuntimeError("native process ownership inspection is unavailable")
    buffer = ctypes.create_string_buffer(native._procargs_bytes)
    length = ctypes.c_size_t(len(buffer))
    mib = (ctypes.c_int * 3)(1, 49, pid)
    if native._procargs_sysctl(mib, 3, buffer, ctypes.byref(length), None, 0) != 0:
        raise ProcessLookupError(pid)
    return parse_arguments(buffer.raw[:length.value])


def parse_arguments(data):
    """Darwin appends Apple startup metadata after the environment terminator."""
    argc = ctypes.c_int.from_buffer_copy(data[:4]).value
    if not 1 <= argc <= 65536:
        raise ValueError("invalid native argv")
    offset = data.index(b"\0", 4) + 1
    while offset < len(data) and data[offset] == 0:
        offset += 1
    argv = []
    for _ in range(argc):
        end = data.index(b"\0", offset)
        argv.append(data[offset:end].decode("utf-8"))
        offset = end + 1
    env = {}
    for entry in data[offset:].split(b"\0"):
        if not entry:
            break
        key, sep, value = entry.partition(b"=")
        if not sep:
            continue
        key, value = key.decode("utf-8"), value.decode("utf-8")
        if key in env:
            raise ValueError("ambiguous process environment")
        env[key] = value
    return argv, env


def cwd(pid):
    # PROC_PIDVNODEPATHINFO: two vnode_info_path structs (152 + MAXPATHLEN).
    buffer = ctypes.create_string_buffer(2352)
    if native._proc_pidinfo(pid, 9, 0, buffer, len(buffer)) != len(buffer):
        raise RuntimeError("original process working directory unavailable")
    value = buffer.raw[152:1176].split(b"\0", 1)[0].decode("utf-8")
    if not value or not Path(value).is_dir():
        raise RuntimeError("original working directory no longer exists")
    return value


def process(pid, *, launch=False):
    generation = birth(pid, codex=True)
    if generation is None:
        return None
    try:
        argv, env = arguments(pid)
        if not argv or Path(argv[0]).name != "codex":
            return None
        sid = str(uuid.UUID(env.get("CMUX_SURFACE_ID", ""))).upper()
        wid = str(uuid.UUID(env.get("CMUX_WORKSPACE_ID", ""))).upper()
        value = {"pid": pid, "birth": generation, "process_start": generation[0],
                 "surface_id": sid, "environment_workspace_id": wid,
                 "remote": "--remote" in argv,
                 "backend": "app-server" in argv and env.get("CCC_GUARD_BACKEND") == "1"}
        if "--remote" in argv:
            index = argv.index("--remote")
            value["remote_address"] = argv[index + 1] if index + 1 < len(argv) else ""
        if launch:
            value.update(argv=argv, environment=env, cwd=cwd(pid))
        return value if birth(pid, codex=True) == generation else None
    except (OSError, ValueError, UnicodeError):
        return None


def scan():
    """One bounded local snapshot; never invoke slow ps/top on the stop path."""
    if native._proc_listpids is None or sys.platform != "darwin":
        raise RuntimeError("B realtime process coverage requires Darwin libproc")
    size = native._proc_listpids(1, 0, None, 0)  # PROC_ALL_PIDS
    if not 0 < size < 4 * 1024 * 1024:
        raise RuntimeError("native process inventory unavailable")
    capacity = size + 16384
    values = (ctypes.c_int * (capacity // 4))()
    count = native._proc_listpids(1, 0, values, ctypes.sizeof(values))
    if count <= 0 or count >= ctypes.sizeof(values) or count % 4:
        raise RuntimeError("native process inventory incomplete")
    records = []
    for pid in values[:count // 4]:
        row = process(pid)
        if row:
            records.append(row)
    return records


def matches(record):
    current = process(record.get("pid"))
    return bool(current and all(current.get(k) == record.get(k) for k in
        ("birth", "surface_id", "environment_workspace_id")))


def send(record, sig):
    """Caller must also verify current cmux membership immediately beforehand."""
    if not matches(record):
        return False
    try:
        os.kill(record["pid"], sig)
        return True
    except ProcessLookupError:
        return False


def records(tree):
    result = {}
    for window in tree.get("windows", []):
        for workspace in window.get("workspaces", []):
            for pane in workspace.get("panes", []):
                for surface in pane.get("surfaces", []):
                    try:
                        sid = str(uuid.UUID(surface["id"])).upper()
                        wid = str(uuid.UUID(workspace["id"])).upper()
                        if sid in result and result[sid]["workspace_id"] != wid:
                            raise RuntimeError("ambiguous current workspace membership")
                        result[sid] = {"surface_id": sid, "workspace_id": wid,
                            "window_id": window["id"], "pane_id": pane["id"],
                            "type": surface.get("type"), "ref": surface.get("ref", "")}
                    except (KeyError, ValueError, TypeError) as exc:
                        raise RuntimeError("incomplete cmux surface identity") from exc
    return result

"""Read-only local Codex discovery, independent of cmux's GUI process scan.

These short-lived hints never authorize input. The watcher still joins a
current cmux surface/workspace and verifies the original native writer and
failed turn before sending. No shell process or model request is started.
"""
from __future__ import annotations

import threading
import time


def display_surface_records(tree, records):
    """Add snapshot ordinals and native join inputs without changing selectors."""
    from collections.abc import Mapping
    import uuid

    def objects(value):
        return [v for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []

    metadata, workspace_number = {}, 0
    for window in objects(tree.get("windows")):
        for workspace in objects(window.get("workspaces")):
            workspace_number += 1
            for pane_number, pane in enumerate(objects(workspace.get("panes")), 1):
                for surface_number, surface in enumerate(objects(pane.get("surfaces")), 1):
                    sid = str(surface.get("id") or surface.get("surface_id") or surface.get("ref") or "")
                    target = None
                    if (pane.get("dock_scope") is None and surface.get("dock_scope") is None
                            and surface.get("type", surface.get("kind")) == "terminal"):
                        try:
                            target = {key: str(uuid.UUID(value)).upper() for key, value in (
                                ("surface_id", sid),
                                ("workspace_id", workspace.get("id") or workspace.get("workspace_id")),
                                ("pane_id", pane.get("id") or pane.get("pane_id")),
                                ("window_id", window.get("id") or window.get("window_id")))}
                            target["tty"] = surface.get("tty")
                        except (ValueError, TypeError, AttributeError):
                            target = None
                    metadata.setdefault(sid, []).append({
                        "display_location": f"w#{workspace_number}/p#{pane_number}/s#{surface_number}",
                        "native_display_target": target,
                    })
    result = []
    for number, record in enumerate(records, 1):
        matches = metadata.get(str(record.get("surface_id") or ""), [])
        extra = dict(matches[0]) if len(matches) == 1 else {}
        if len(extra.get("display_location", "")) > 14:
            extra["display_location"] = f"row#{number}"
        result.append({**record, **extra})
    return result


def _display_kernel(pid):
    """Reuse the libproc ABI; retain only birth, name and controlling-TTY facts."""
    import ctypes
    import ccc_codex_queue as native

    if native._proc_pidinfo is None or type(pid) is not int or not 1 < pid < 2**31:
        return None
    info = native._BsdInfo()
    size = ctypes.sizeof(info)
    if (native._proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size
            or info.pid != pid or info.status == 5 or not info.start_sec):
        return None
    name = (info.name or info.comm).decode("utf-8", "replace")
    kernel = {}
    if name not in {"codex", "claude"}:
        if not _versioned_claude_path(pid, name, info.ids[0]):
            return None
        # The native installer names the executable by version. Keep that
        # kernel name so display_process binds argv0 to the same trusted path.
        name, kernel = "claude", {"kernel_name": name}
    return {"pid": pid, "birth": (info.start_sec, info.start_usec), "agent_kind": name,
            "pgid": info.misc[1], "tdev": info.misc[3], "tpgid": info.misc[4], **kernel}


_libproc_pidpath = None


def _pidpath(pid):
    """Kernel executable path (PROC_PIDPATHINFO); None when unavailable."""
    import ctypes
    import sys
    global _libproc_pidpath

    if sys.platform != "darwin":
        return None
    if _libproc_pidpath is None:
        function = ctypes.CDLL("/usr/lib/libproc.dylib").proc_pidpath
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_int
        _libproc_pidpath = function
    buffer = ctypes.create_string_buffer(4096)  # PROC_PIDPATHINFO_MAXSIZE
    length = _libproc_pidpath(pid, buffer, len(buffer))
    return buffer.raw[:length].decode("utf-8") if 0 < length < len(buffer) else None


def _home(uid):
    import pwd

    return pwd.getpwuid(uid).pw_dir


def _versioned_claude_path(pid, name, uid):
    """Trusted native-installer Claude only; a version-shaped name is not identity.

    The kernel executable must be exactly <home of uid>/.local/share/claude/
    versions/<X.Y.Z> for the current user: a regular non-symlink file in a
    real directory, both owned by that user and not group/other writable.
    """
    import os
    import re
    import stat

    if not re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}", name) or uid != os.getuid():
        return None
    try:
        root = os.path.join(_home(uid), ".local", "share", "claude", "versions")
        path = os.path.join(root, name)
        if _pidpath(pid) != path:
            return None
        for item, is_kind in ((root, stat.S_ISDIR), (path, stat.S_ISREG)):
            info = os.lstat(item)
            if not is_kind(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
                return None
        return path
    except (OSError, KeyError, ValueError, TypeError):
        return None


def display_process(pid):
    """Stable local interactive CLI identity, with no argv or environment leak."""
    import hashlib
    import json
    import os
    from pathlib import Path
    import uuid
    import ccc_guard_scope as scope

    before = _display_kernel(pid)
    if not before:
        return None
    try:
        argv, env = scope.arguments(pid)
        kind = before["agent_kind"]
        if "kernel_name" in before:
            # Version-named binary: argv0 must be the exact trusted path, not a
            # basename, so a renamed copy elsewhere cannot pass.
            if not argv or argv[0] != _versioned_claude_path(pid, before["kernel_name"], os.getuid()):
                return None
        elif not argv or Path(argv[0]).name != kind:
            return None
        options = argv[1:argv.index("--")] if "--" in argv else argv[1:]
        if kind == "codex" and (
                any(a == "--remote" or a.startswith("--remote=") for a in options)
                or set(options) & {"exec", "e", "review", "mcp", "mcp-server", "app-server",
                                   "login", "logout", "completion", "sandbox", "debug", "apply",
                                   "cloud", "features", "--version", "-V", "--help", "-h"}):
            return None
        if kind == "claude" and (
                any(a == "--print" or a.startswith("--print=") for a in options)
                or set(options) & {"-p", "--version", "-v", "--help", "-h", "mcp",
                                   "install", "update", "doctor", "setup-token"}):
            return None
        row = {**before,
               "surface_id": str(uuid.UUID(env.get("CMUX_SURFACE_ID", ""))).upper(),
               # Launch workspace is evidence, not current placement after a move.
               "environment_workspace_id": str(uuid.UUID(env.get("CMUX_WORKSPACE_ID", ""))).upper(),
               "argv_fingerprint": hashlib.sha256(json.dumps(argv).encode()).hexdigest()}
        if scope.arguments(pid) != (argv, env) or _display_kernel(pid) != before:
            return None
        return row
    except (OSError, ValueError, TypeError, RuntimeError, UnicodeError):
        return None


def _display_tty_device(target):
    import os
    import stat

    tty = str(target.get("tty") or "").removeprefix("/dev/")
    if not tty.startswith("tty") or not tty.isascii() or not tty.isalnum():
        return None
    try:
        info = os.stat("/dev/" + tty, follow_symlinks=False)
        return info.st_rdev if stat.S_ISCHR(info.st_mode) and info.st_rdev else None
    except OSError:
        return None


def display_identity_matches(proof):
    """Recheck the original generation around session and request observation."""
    try:
        row, target = proof["process"], proof["target"]
        # Pending input proofs survive a JSON round trip. Kernel birth remains
        # a pair of integers; JSON's tuple -> list conversion is not a new PID.
        row = {**row, "birth": tuple(row["birth"])}
        return (row["surface_id"] == target["surface_id"]
                and row["pgid"] == row["tpgid"] and row["pgid"] > 0
                and _display_tty_device(target) == row["tdev"]
                and display_process(row["pid"]) == row)
    except (KeyError, TypeError, OSError, ValueError, RuntimeError):
        return False


def display_process_labels(records, classified, top, *, processes=None, agent_kind=None,
                           verify_classified=False):
    """Verify display identities; never return a send authorization.

    The TUI also verifies an agreeing GUI label so its session cache keeps the
    same native generation when the GUI inventory recovers.
    """
    import ccc_guard_scope as scope
    import ccc_observation as observation
    import cmux_codex_watch as core

    output = dict(classified)
    populated = set()
    for item, _ in observation.surface_entries(top):
        if item.get("processes"):
            populated.update(str(item.get(k) or "") for k in ("id", "surface_id", "ref"))
    wanted = []
    for record in records:
        label = core.surface_process_label(classified, record)
        target = record.get("native_display_target")
        missing = (not {record.get("surface_id"), record.get("ref")} & populated
                   and label.get("agent_kind", "unknown") == "unknown"
                   and not label.get("agent_pids"))
        agreeing_gui = (verify_classified
                        and label.get("agent_kind") in {"codex", "claude"}
                        and bool(label.get("agent_pids")))
        if (target and target.get("tty") and not label.get("identity_conflicts")
                and (missing or agreeing_gui)):
            wanted.append((record, target))
    if not wanted:
        return output
    try:
        groups = {}
        for row in (scope.scan(reader=display_process) if processes is None else processes):
            groups.setdefault(row["surface_id"], []).append(row)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError):
        return output
    for record, target in wanted:
        group = groups.get(target["surface_id"], [])
        if len(group) == 1:
            row = group[0]
        else:
            # An inherited surface can have a background Codex on the same
            # TTY. Keep mixed-agent, duplicate and foreign-TTY groups ambiguous.
            # Only a single foreground Codex may supply missing observation;
            # input authorization still performs its own live identity checks.
            if (agent_kind not in {None, "codex"} or not group
                    or any(item.get("agent_kind") != "codex" for item in group)):
                continue
            device = _display_tty_device(target)
            foreground = [item for item in group
                          if item.get("pgid", 0) > 0
                          and item.get("pgid") == item.get("tpgid")
                          and item.get("tdev") == device]
            if len(foreground) != 1:
                continue
            row = foreground[0]
            if (len({item.get("pid") for item in group}) != len(group)
                    or any(item.get("tdev") != device
                           or item.get("tpgid") != row["pgid"]
                           or (item is not row and item.get("pgid") == row["pgid"])
                           for item in group)
                    or any(display_process(item["pid"]) != item
                           for item in group if item is not row)):
                continue
        # Other agent kinds were never filtered out of the group above.
        if agent_kind is not None and row["agent_kind"] != agent_kind:
            continue
        label = core.surface_process_label(classified, record)
        if label.get("agent_kind", "unknown") != "unknown" and (
                label.get("agent_kind") != row["agent_kind"]
                or row["pid"] not in label.get("agent_pids", [])
                or not set(label.get("agent_pids", [])).issubset(
                    {item["pid"] for item in group})):
            continue
        proof = {"process": row, "target": target}
        if not display_identity_matches(proof):
            continue
        output[record["surface_id"]] = {
            "agent_kind": row["agent_kind"], "agent_pid": row["pid"],
            "agent_pids": [row["pid"]], "native_identity": proof,
            "identity_verified_pids": [row["pid"]], "identity_conflicts": [],
            "launch_workspaces": {row["pid"]: row["environment_workspace_id"]},
            "summary": row["agent_kind"].title() + "（原生身份）",
        }
    return output


def foreground_generation(pid):
    """Small native selection hint; publication timestamps are not a generation."""
    from ccc_client_thread_observation import read_foreground
    from ccc_request_observation_policy import request_observation_directory_matches
    status, thread, evidence = read_foreground(pid, request_observation_directory_matches)
    return (status, thread, evidence[1] if status == "ok" else None)


def local_processes():
    import ccc_guard_scope as scope

    rows = []
    for row in scope.scan():
        if row.get("remote") or row.get("backend"):
            continue
        try:
            argv, _ = scope.arguments(row["pid"])
        except (OSError, ValueError, RuntimeError):
            continue
        options = argv[1:argv.index("--")] if "--" in argv else argv[1:]
        # Conservative filtering also excludes noninteractive child commands
        # launched by another agent in the same terminal. A missed hint keeps
        # the existing cmux discovery path; it is never proof of process exit.
        if (any(a == "--remote" or a.startswith("--remote=") for a in options)
                or set(options) & {"exec", "e", "review", "mcp", "mcp-server", "app-server",
                                   "login", "logout", "completion", "sandbox", "debug", "apply",
                                   "cloud", "features", "--version", "-V", "--help", "-h"}
                or scope.birth(row["pid"], codex=True) != row["birth"]):
            continue
        rows.append({**row, "foreground_generation": foreground_generation(row["pid"])})
    return rows


class NativeProcessIndex:
    """One independent, bounded refresh; readers only copy an in-memory map."""

    def __init__(self, *, loader=local_processes, clock=time.monotonic, interval=2, max_age=5):
        self.loader, self.clock = loader, clock
        self.interval, self.max_age = interval, max_age
        self._lock = threading.Lock()
        self._snapshot, self._started = None, float("-inf")
        self.stop = threading.Event()
        self.thread = None

    def refresh(self):
        started = self.clock()
        try:
            rows = self.loader()
            groups = {}
            for row in rows:
                sid, wid = row["surface_id"], row["environment_workspace_id"]
                groups.setdefault(sid, []).append((wid, row))
            snapshot = {
                sid: {"workspace_id": group[0][0], "agent_kind": "codex",
                      "agent_pids": [group[0][1]["pid"]],
                      "agent_birth": tuple(group[0][1].get("birth") or ()),
                      "foreground_generation": tuple(group[0][1].get("foreground_generation") or ()),
                      "summary": "local native process hint"}
                for sid, group in groups.items() if len(group) == 1
            }
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            snapshot = None
        with self._lock:
            # Age from the start: a slow/incomplete refresh cannot publish old
            # ownership as fresh when it finally returns.
            self._snapshot, self._started = snapshot, started

    def snapshot(self):
        with self._lock:
            if self._snapshot is None or not 0 <= self.clock() - self._started <= self.max_age:
                return None
            return dict(self._snapshot)

    def lookup(self, target):
        # Single-target checks are frequent while GUI discovery is stalled.
        # Avoid copying the whole fleet map for each original-session check.
        with self._lock:
            if self._snapshot is None or not 0 <= self.clock() - self._started <= self.max_age:
                return None
            row = self._snapshot.get(str(target.get("surface_id")))
            if row and row["workspace_id"] == str(target.get("workspace_id")):
                return dict(row)
        return None

    def start(self):
        def run():
            while not self.stop.is_set():
                self.refresh()
                self.stop.wait(self.interval)
        self.thread = threading.Thread(target=run, name="ccc-native-discovery", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=3)

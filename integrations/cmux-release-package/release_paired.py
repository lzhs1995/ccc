"""Verify or use one separately reviewed full cmux local Release package.

No installation, selector changes, reload, stop, signal or snapshot mutation.
Launch uses the ordinary LaunchServices entry; it does not repair bad package
environment data with an external launcher.
"""
from __future__ import annotations
import argparse
import ctypes as C
import errno
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import socket
import stat
import subprocess
import sys
import time

import release_package as package
import source_contract as shared

need = package.need
digest = package.digest


def stable_socket_path(home=None):
    # Matches CmuxStateDirectory: actual account home, never inherited HOME.
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir) if home is None else Path(home)
    need(home.is_absolute(), "actual account home must be absolute")
    return str(home / ".local/state/cmux/cmux.sock")


def validate_pin(pin):
    need(isinstance(pin, dict) and pin.get("schema") == package.SCHEMA
         and pin.get("passed") is True, "invalid signed-package pin")
    need(pin.get("original_source_app_pin_sha256") == shared.SOURCE_PIN_SHA256
         and pin.get("source_tree") == shared.GREEN_TREE and pin.get("source_upstream") == shared.UPSTREAM,
         "unreviewed original source tree")
    need(pin.get("bundle_id") == package.BUNDLE_ID and pin.get("configuration") == "Release"
         and pin.get("signature") == "ad-hoc" and pin.get("official_release") is False
         and pin.get("bundle_executable") == "cmux" and pin.get("app_name") == package.APP_NAME
         and pin.get("architecture") == ["arm64"], "package identity/signature class changed")
    need(pin.get("required_install_path") == package.INSTALL_PATH
         and pin.get("socket_rule") == package.SOCKET_RULE, "package route changed")
    need(pin.get("main_entitlements") == package.MAIN_ENTITLEMENTS, "local entitlement contract changed")
    need(pin.get("signed_environment") == package.ENVIRONMENT, "signed environment contract changed")
    need(pin.get("status") == "SIGNED_ARCHIVE_RUNTIME_NOT_YET_ACCEPTED"
         and all(pin.get(key) is False for key in ("runtime_verified", "business_adoption_ready",
                 "installed_on_user_machine", "upstream_team_features_verified")),
         "source package must not claim runtime or business acceptance")
    for key in ("manifest_sha256", "inventory_canonical_sha256", "signature_closure_sha256",
                "application_sha256", "cli_sha256", "info_plist_sha256", "build_result_sha256",
                "resolved_settings_sha256", "unsigned_manifest_sha256", "native_code_canonical_sha256",
                "transform_sha256"):
        need(re.fullmatch(r"[0-9a-f]{64}", pin.get(key, "")), "invalid package hash: " + key)
    need(re.fullmatch(r"[0-9a-f]{40}", pin.get("code_directory_hash", "")), "invalid main CDHash")
    run = pin.get("run")
    need(isinstance(run, dict) and run.get("repository") == "lzhs1995/ccc"
         and re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))
         and all(type(run.get(k)) is int and run[k] > 0 for k in ("run_id", "run_attempt")),
         "invalid original package run identity")
    archive = pin.get("archive")
    need(isinstance(archive, dict) and archive.get("name") == "cmux-release.zip"
         and re.fullmatch(r"[0-9a-f]{64}", archive.get("sha256", ""))
         and type(archive.get("bytes")) is int and 0 < archive["bytes"] <= 3 * 1024**3,
         "invalid complete archive pin")
    return pin


def validate_native_manifest(pin, rows):
    members = package.validate_manifest(rows)
    need(type(pin.get("member_count")) is int and len(rows) == pin["member_count"], "member count differs")
    need(package.hashlib.sha256(package.canonical(rows)).hexdigest() == pin["inventory_canonical_sha256"],
         "canonical inventory differs")
    native = pin.get("native_code")
    need(isinstance(native, list) and native and all(isinstance(row, dict) for row in native),
         "native code inventory missing")
    need(package.hashlib.sha256(package.canonical(native)).hexdigest() == pin["native_code_canonical_sha256"],
         "native code inventory differs")
    paths = [row.get("path") for row in native]
    need(all(isinstance(path, str) for path in paths) and paths == sorted(set(paths), key=package.PurePosixPath), "native paths not unique")
    for row in native:
        need(set(row) == {"path", "sha256", "architectures"}, "native fields differ")
        member = members.get(row["path"], {})
        arches = row["architectures"]
        need(member.get("type") == "file" and member.get("sha256") == row["sha256"]
             and isinstance(arches, list) and arches and all(a in ("arm64", "x86_64", "i386", "arm") for a in arches)
             and arches == sorted(set(arches)), "native member does not match complete inventory")
    code = {row["path"]: row for row in native}
    for path, key in (("Contents/MacOS/cmux", "application_sha256"),
                      ("Contents/Resources/bin/cmux", "cli_sha256")):
        need(code.get(path, {}).get("architectures") == ["arm64"]
             and code.get(path, {}).get("sha256") == pin[key], "primary app/CLI pin differs")
    need(members.get("Contents/Info.plist", {}).get("type") == "file"
         and members["Contents/Info.plist"]["sha256"] == pin["info_plist_sha256"], "plist manifest differs")
    signing = pin.get("signing_targets")
    need(isinstance(signing, list) and len(signing) == len(code)
         and all(isinstance(row, dict) and set(row) == {"target", "executable"} for row in signing),
         "invalid signing target inventory")
    need(all(isinstance(row["target"], str) and isinstance(row["executable"], str) for row in signing),
         "invalid signing target values")
    need(len({row["target"] for row in signing}) == len(signing)
         and {row["executable"] for row in signing} == set(code), "signature target/native coverage differs")
    for row in signing:
        target, executable = row["target"], row["executable"]
        if target == ".":
            need(executable == "Contents/MacOS/cmux", "main signing target executable differs")
        else:
            member = members.get(target)
            need(member is not None and member["type"] in ("file", "directory"),
                 "signing target is absent, unsafe or a symlink")
            if member["type"] == "file":
                need(target == executable, "standalone signing target differs from its code")
            else:
                need(package.PurePosixPath(target).suffix in
                     (".app", ".framework", ".xpc", ".appex", ".plugin", ".systemextension", ".bundle")
                     and executable.startswith(target + "/"), "bundle signing target/code differs")
    need(sum(row["target"] == "." for row in signing) == 1 and signing[-1]["target"] == ".",
         "main signing target must be last and unique")
    return code


def read_pin(path, expected_sha256, manifest):
    need(re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or ""), "external new-package pin is required")
    pin = validate_pin(package.decode_json(package.read_pinned(path, expected_sha256, maximum=16 * 1024**2)))
    rows = package.decode_json(package.read_pinned(manifest, pin["manifest_sha256"]))
    code = validate_native_manifest(pin, rows)
    root = Path(manifest).parent
    closure = package.decode_json(package.read_pinned(root / "SIGNATURE-CLOSURE.json", pin["signature_closure_sha256"]))
    targets = closure.get("targets")
    need(closure.get("all_native_code_enumerated") is True and closure.get("main_entitlements") == package.MAIN_ENTITLEMENTS
         and closure.get("upstream_team_features_verified") is False and closure.get("nested_components_removed") == 0
         and isinstance(targets, list) and len(targets) == len(code), "incomplete signature closure")
    need(all(isinstance(row, dict) for row in targets)
         and len({row.get("target") for row in targets}) == len(targets)
         and len({row.get("executable") for row in targets}) == len(targets), "duplicate signature target")
    for row in targets:
        expected = code.get(row.get("executable"))
        need(expected and row.get("after_sha256") == expected["sha256"]
             and row.get("architectures") == expected["architectures"]
             and re.fullmatch(r"[0-9a-f]{40}", row.get("cdhash", "")), "signature/native closure mismatch")
    need([{key: row.get(key) for key in ("target", "executable")} for row in targets]
         == pin["signing_targets"], "signature target inventory differs")
    main = [row for row in targets if row.get("target") == "."]
    need(len(main) == 1 and main[0].get("executable") == "Contents/MacOS/cmux"
         and main[0].get("cdhash") == pin["code_directory_hash"]
         and main[0].get("resigned_locally") is True, "main signature receipt differs")
    transform = package.decode_json(package.read_pinned(root / "TRANSFORM.json", pin["transform_sha256"]))
    need(transform.get("source_preserved") is True
         and transform.get("source_build_result_sha256") == pin["build_result_sha256"], "source transform receipt differs")
    return pin, rows


def verify_app(app, pin, rows, *, timeout=90):
    need(0 < timeout <= 300, "signature verification timeout out of bounds")
    deadline = time.monotonic() + timeout
    def remaining():
        value = deadline - time.monotonic()
        need(value > 0, "complete app verification budget exhausted")
        return value
    validate_pin(pin)
    validate_native_manifest(pin, rows)
    app = Path(app)
    need(str(app) == package.INSTALL_PATH and app.resolve(strict=True) == app and not app.is_symlink(),
         "app is not at its signed final location")
    need(package.original_admission().inventory(app) == rows, "installed complete bundle differs")
    raw = package.read_pinned(app / "Contents/Info.plist", pin["info_plist_sha256"])
    need(package.hashlib.sha256(raw).hexdigest() == pin["info_plist_sha256"], "installed plist differs")
    info = plistlib.loads(raw)
    need(info["CFBundleIdentifier"] == pin["bundle_id"] and info["LSEnvironment"] == package.ENVIRONMENT,
         "signed metadata differs")
    need(digest(app / "Contents/MacOS" / pin["bundle_executable"]) == pin["application_sha256"]
         and digest(app / "Contents/Resources/bin/cmux") == pin["cli_sha256"], "compiled app/CLI differs")
    verify = package.command(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], timeout=remaining())
    display = package.command(["/usr/bin/codesign", "--display", "--verbose=4", str(app)], timeout=remaining())
    text = display["stdout"] + display["stderr"]
    need(re.findall(r"^CDHash=(.+)$", text, re.M) == [pin["code_directory_hash"]]
         and re.findall(r"^Identifier=(.+)$", text, re.M) == [package.BUNDLE_ID]
         and "Signature=adhoc" in text
         and re.findall(r"^TeamIdentifier=(.+)$", text, re.M) == ["not set"], "signature differs")
    entitlements = package.command(["/usr/bin/codesign", "--display", "--entitlements", ":-", str(app)], timeout=remaining())
    need(plistlib.loads(entitlements["stdout"].encode()) == package.MAIN_ENTITLEMENTS,
         "effective local main entitlements differ")
    need(package.native_code_inventory(app) == pin["native_code"], "installed native architecture/bytes differ")
    actual_targets = [{"target": target.relative_to(app).as_posix(),
                       "executable": executable.relative_to(app).as_posix()}
                      for target, executable in package.signing_targets(app)]
    need(actual_targets == pin["signing_targets"], "actual nested signing targets differ")
    need(package.original_admission().inventory(app) == rows, "bundle changed during signature inspection")
    remaining()
    return {"passed": True, "complete_members": len(rows), "verify": verify, "display": display,
            "entitlements": entitlements, "native_code": pin["native_code"]}


def override_paths(home, environment):
    paths = []
    tag = environment.get("CMUX_TAG", "")
    if tag and re.fullmatch(r"[A-Za-z0-9._-]+", tag):
        paths.append(Path(home) / ".config/cmux/dev-profiles" / (tag + ".env"))
    paths.append(Path(home) / ".cmux-dev.env")
    return paths


def require_environment(home, environment):
    # The fixed Release AuthEnvironment does not read Debug override files.
    # Existing files stay intact. Inherited explicit socket routes still matter.
    inherited = environment.get("CMUX_SOCKET_PATH")
    need(not inherited or inherited == stable_socket_path(home),
         "explicit inherited socket belongs to an older/different peer; no automatic reroute")
    need(not environment.get("CMUX_TAG"), "inherited development tag requires effective-environment review")
    for key, value in environment.items():
        if key.startswith("CMUX_TEST_") or key.startswith("CMUX_UI_TEST_"):
            need(not value, "test-only environment is not a normal launch: " + key)


def _library():
    need(sys.platform == "darwin", "native package process checks require macOS")
    lib = C.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pidinfo.argtypes = [C.c_int, C.c_int, C.c_uint64, C.c_void_p, C.c_int]
    lib.proc_pidinfo.restype = C.c_int
    lib.proc_pidpath.argtypes = [C.c_int, C.c_void_p, C.c_uint32]
    lib.proc_pidpath.restype = C.c_int
    return lib


def process_identity(pid):
    lib = _library()
    # Darwin proc_bsdinfo: 12 uint32, comm[16], name[32], 6 uint32,
    # then start_tvsec/start_tvusec. Require the full ABI before using it.
    raw = C.create_string_buffer(136)
    if lib.proc_pidinfo(int(pid), 3, 0, raw, C.sizeof(raw)) != 136:
        return None
    path = C.create_string_buffer(4096)
    if lib.proc_pidpath(int(pid), path, C.sizeof(path)) <= 0:
        return None
    data = raw.raw
    need(int.from_bytes(data[12:16], sys.byteorder) == int(pid), "native pid changed")
    return {"pid": int(pid), "ppid": int.from_bytes(data[16:20], sys.byteorder),
            "uid": int.from_bytes(data[20:24], sys.byteorder),
            "birth": [int.from_bytes(data[120:128], sys.byteorder),
                      int.from_bytes(data[128:136], sys.byteorder)],
            "executable": os.fsdecode(path.value)}


def installed_gui_processes():
    lib = _library()
    lib.proc_listallpids.argtypes = [C.c_void_p, C.c_int]
    lib.proc_listallpids.restype = C.c_int
    count = lib.proc_listallpids(None, 0)
    need(0 < count < 65536, "unexpected process count")
    pids = (C.c_int * (count + 512))()
    used = lib.proc_listallpids(pids, C.sizeof(pids))
    need(0 <= used < len(pids), "process inventory changed beyond bound")
    rows = []
    for pid in pids[:used]:
        buffer = C.create_string_buffer(4096)
        if lib.proc_pidpath(pid, buffer, C.sizeof(buffer)) > 0:
            path = os.fsdecode(buffer.value)
            if path.startswith(package.INSTALL_PATH + "/Contents/MacOS/"):
                row = process_identity(pid)
                if row:
                    rows.append(row)
    return rows


class PeerObservationChanged(ValueError):
    """Retain the exact failed comparison; it grants no retry or admission."""
    def __init__(self, observation):
        super().__init__('GUI generation/socket changed during executable check')
        self.observation = observation


def socket_file_observation(info):
    return {name: getattr(info, name) for name in (
        'st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
        'st_size', 'st_mtime_ns', 'st_ctime_ns')}


def socket_peer_observation_key(info):
    # cmux reapplies permissions through a private hard link. Even same-mode
    # chmod updates ctime on Darwin without replacing the listener. Keep ctime
    # in diagnostics, but not this socket comparison. Ordinary file receipts
    # retain package.file_identity's full metadata-race check.
    # The other fields remain conservative: a transient hard-link count change
    # still refuses this observation rather than granting an automatic retry.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns)


def peer(pin, expected=None, *, timeout=3):
    path = Path(stable_socket_path())
    before = path.lstat()
    need(stat.S_ISSOCK(before.st_mode) and before.st_uid == os.geteuid()
         and not before.st_mode & 0o077, "unsafe repair socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        pid = int.from_bytes(connection.getsockopt(0, 2, 4), sys.byteorder, signed=True)
    after = path.lstat()
    row = process_identity(pid)
    need(row and row["uid"] == os.geteuid()
         and row["executable"] == package.INSTALL_PATH + "/Contents/MacOS/" + pin["bundle_executable"],
         "repair socket does not belong to the selected app")
    need(socket_peer_observation_key(before) == socket_peer_observation_key(after), "socket changed during peer check")
    need(digest(row["executable"]) == pin["application_sha256"], "peer executable does not match new package")
    # Keep process checks and the original short-circuit observation order.
    # A failure now identifies which native observation changed.
    checked_process = process_identity(pid)
    if checked_process != row:
        raise PeerObservationChanged({'changed': 'process', 'process_before': row,
            'process_after': checked_process, 'socket_before_hash': socket_file_observation(after),
            'socket_after_hash': None, 'socket_after_hash_status': 'not_read_after_process_mismatch',
            'executable_sha256_verified': pin['application_sha256'], 'automatic_retry': False})
    checked_socket = path.lstat()
    if socket_peer_observation_key(checked_socket) != socket_peer_observation_key(after):
        raise PeerObservationChanged({'changed': 'socket', 'process_before': row,
            'process_after': checked_process, 'socket_before_hash': socket_file_observation(after),
            'socket_after_hash': socket_file_observation(checked_socket),
            'executable_sha256_verified': pin['application_sha256'], 'automatic_retry': False})
    result = row | {"socket_device": after.st_dev, "socket_inode": after.st_ino}
    if expected is not None:
        need(all(key in expected and expected[key] == value for key, value in result.items()),
             "original GUI generation/socket changed")
    return result


def cli_argv(app, arguments):
    need(arguments and all(isinstance(value, str) for value in arguments), "CLI command required")
    need(not any(value == "--socket" or value.startswith("--socket=") for value in arguments),
         "paired CLI owns the exact socket option")
    return [str(Path(app) / "Contents/Resources/bin/cmux"), "--socket", stable_socket_path(), *arguments]


def require_quiescent_socket(path):
    path = Path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return {"path": str(path), "status": "absent"}
    need(stat.S_ISSOCK(before.st_mode) and before.st_uid == os.geteuid()
         and not before.st_mode & 0o077, "existing socket is not safely owned: " + str(path))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        try:
            connection.connect(str(path))
        except OSError as error:
            need(error.errno == errno.ECONNREFUSED, "existing socket liveness unknown: " + str(path))
        else:
            raise ValueError("a live prior peer remains: " + str(path))
    after = path.lstat()
    need((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino), "socket changed during preflight")
    # Leave the stale inode for the application's normal listener reclamation.
    return {"path": str(path), "status": "owned_refused_connection", "device": after.st_dev, "inode": after.st_ino}


def safe_launch_environment(environment):
    keys = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "__CF_USER_TEXT_ENCODING")
    return {key: environment[key] for key in keys if key in environment} | {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SHELL": "/bin/zsh"}


def first_adoption_snapshot_check(home):
    path = Path(home) / "Library/Application Support/cmux/session-com.cmuxterm.app.json"
    try:
        raw = package.read_pinned(path, maximum=16 * 1024**2)
    except FileNotFoundError:
        # A dangling leaf/ancestor must not be mistaken for absent snapshot.
        need(not any(parent.is_symlink() for parent in (path, *path.parents)), "saved snapshot path has a symlink")
        return {"path": str(path), "status": "absent"}
    snapshot = package.decode_json(raw)
    need(isinstance(snapshot, dict) and snapshot.get("version") == 1
         and isinstance(snapshot.get("windows"), list), "unknown saved-session structure")
    blockers = []
    count = 0
    def panels(values, owner):
        nonlocal count
        need(isinstance(values, list), "unknown saved surface structure")
        for panel in values:
            count += 1
            need(count <= 100000 and isinstance(panel, dict), "oversized or invalid saved panel")
            terminal = panel.get("terminal")
            need(panel.get("type") == "terminal" and isinstance(terminal, dict)
                 and panel.get("agentSession") is None and panel.get("browser") is None,
                 "non-shell or unknown saved panel requires original-session ownership admission")
            fields = [key for key in ("resumeBinding", "managedAgentResumeBinding", "agent",
                      "tmuxStartCommand", "claudeBackgroundViewer", "hibernation", "restoreRecovery")
                      if terminal.get(key) is not None]
            if terminal.get("isRemoteTerminal") is True:
                fields.append("isRemoteTerminal")
            if fields:
                blockers.append({"owner": owner, "surface": panel.get("id"), "fields": fields})
    def dock(value, owner):
        if value is not None:
            need(isinstance(value, dict), "unknown saved dock structure")
            panels(value.get("panels"), owner)
    for window in snapshot["windows"]:
        need(isinstance(window, dict) and isinstance(window.get("tabManager"), dict), "unknown saved window")
        workspaces = window["tabManager"].get("workspaces")
        need(isinstance(workspaces, list), "unknown saved workspace structure")
        dock(window.get("dock"), window.get("windowId"))
        for workspace in workspaces:
            need(isinstance(workspace, dict), "unknown saved workspace")
            for field in ("remote", "cloudVM", "environment", "surfaceProjections", "cloudMachineTeams"):
                if workspace.get(field):
                    blockers.append({"owner": workspace.get("workspaceId"), "field": field})
            panels(workspace.get("panels"), workspace.get("workspaceId"))
            dock(workspace.get("dock"), workspace.get("workspaceId"))
    # File import's manual bindings do not disable rebuilt built-in agents.
    # This helper only admits a fresh CI shell topology; it cannot perform the
    # separately reviewed original-user first-adoption preference/ownership plan.
    need(not blockers, "existing saved agent/command state requires original-session ownership admission before first adoption")
    return {"path": str(path), "status": "no_saved_agent_or_command_state",
            "sha256": package.hashlib.sha256(raw).hexdigest(), "panels_checked": count}


# Supported by the fixed Release App and its Dock plugin; pass on every launch.
BUNDLE_ICON_PERSISTENCE_ARGUMENT = "--cmux-disable-bundle-icon-persistence"
INVENTORY_DETAIL_LIMIT = 32
INVENTORY_DETAIL_BYTES = 16 * 1024


def inventory_observation(pin, expected_rows, actual_rows):
    """Describe the original pre-verification rows; never rescan or admit them."""
    validate_native_manifest(pin, expected_rows)
    expected = package.validate_manifest(expected_rows)
    actual = package.validate_manifest(actual_rows)
    counts = {"added": 0, "removed": 0, "changed": 0}
    details, detail_bytes = [], 0
    for name in sorted(expected.keys() | actual.keys(), key=package.PurePosixPath):
        before, after = expected.get(name), actual.get(name)
        if before == after:
            continue
        kind = "added" if before is None else "removed" if after is None else "changed"
        counts[kind] += 1
        entry = {"kind": kind, "path": name,
                 "expected": None if before is None else {k: v for k, v in before.items() if k != "path"},
                 "actual": None if after is None else {k: v for k, v in after.items() if k != "path"}}
        size = len(package.canonical(entry))
        if len(details) < INVENTORY_DETAIL_LIMIT and detail_bytes + size <= INVENTORY_DETAIL_BYTES:
            details.append(entry)
            detail_bytes += size
    return {"schema": "cmux-launch-inventory-observation-v1", "status": "CAPTURED",
            "observation": "original_preverify_inventory_no_rescan",
            "expected_source": "authenticated_package_manifest",
            "expected_count": len(expected_rows), "actual_count": len(actual_rows),
            "expected_inventory_sha256": pin["inventory_canonical_sha256"],
            "actual_inventory_sha256": package.hashlib.sha256(package.canonical(actual_rows)).hexdigest(),
            "difference_counts": counts, "details": details,
            "omitted_details": sum(counts.values()) - len(details),
            "detail_limit": INVENTORY_DETAIL_LIMIT, "detail_bytes_limit": INVENTORY_DETAIL_BYTES,
            "detail_bytes": detail_bytes, "actor_established": False}


def launch(app, pin, environment, *, timeout=30, expected_rows=None):
    need(0 < timeout <= 300, "launch timeout out of bounds")
    deadline = time.monotonic() + timeout
    def remaining():
        value = deadline - time.monotonic()
        need(value > 0, "complete launch budget exhausted")
        return value
    validate_pin(pin)
    app = Path(app)
    need(str(app) == package.INSTALL_PATH and app.resolve(strict=True) == app, "launch app location differs")
    need(digest(app / "Contents/MacOS/cmux") == pin["application_sha256"]
         and digest(app / "Contents/Info.plist") == pin["info_plist_sha256"], "launch package differs")
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=True)
    need(Path(environment.get("HOME", str(home))).resolve(strict=True) == home, "inherited HOME is not the actual account")
    require_environment(home, environment)
    snapshot_admission = first_adoption_snapshot_check(home)
    need(not installed_gui_processes(), "original GUI is still alive; wait for its natural exit")
    paths = ["/tmp/cmux.sock", "/tmp/cmux-" + str(os.geteuid()) + ".sock",
             home / ".local/state/cmux/cmux.sock", home / (".local/state/cmux/cmux-" + str(os.geteuid()) + ".sock")]
    prior = [require_quiescent_socket(path) for path in paths]
    # Caller-side admission is not enough: repeat whole-bundle, architecture,
    # nested target, CDHash and effective-entitlement checks immediately before
    # LaunchServices. External concurrent writers remain outside this read/open
    # boundary; no OS compare-and-swap or runtime code-signing bypass is claimed.
    rows = package.original_admission().inventory(app)
    try:
        verified = verify_app(app, pin, rows, timeout=remaining())
    except Exception as error:
        if expected_rows is not None:
            try:
                error.bundle_inventory_observation = inventory_observation(pin, expected_rows, rows)
            except Exception as diagnostic_error:
                error.bundle_inventory_observation = {"status": "UNAVAILABLE",
                    "observation": "original_preverify_inventory_no_rescan",
                    "error": repr(diagnostic_error)[:512]}
        raise
    # No --env overrides. LSEnvironment inside the sealed app must work itself.
    argv = ["/usr/bin/open", "-a", str(app), "--args", BUNDLE_ICON_PERSISTENCE_ARGUMENT]
    result = subprocess.run(argv, env=safe_launch_environment(environment), capture_output=True, text=True, timeout=remaining())
    record = {"argv": argv, "returncode": result.returncode, "stdout": result.stdout,
              "stderr": result.stderr, "prior_sockets": prior, "snapshot_admission": snapshot_admission,
              "immediate_full_app_verification": verified,
              "bundle_icon_persistence_policy": "disabled_by_official_launch_argument"}
    need(result.returncode == 0, "LaunchServices refused: " + json.dumps(record))
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, required=True)
    parser.add_argument("--pin-sha256", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--app", type=Path, default=Path(package.INSTALL_PATH))
    parser.add_argument("operation", choices=("verify", "launch", "cli"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    pin, rows = read_pin(args.pin, args.pin_sha256, args.manifest)
    verified = verify_app(args.app, pin, rows)
    if args.operation == "verify":
        print(json.dumps(verified))
    elif args.operation == "launch":
        print(json.dumps(launch(args.app, pin, os.environ, expected_rows=rows)))
    else:
        require_environment(Path.home(), os.environ)
        before = peer(pin)
        argv = cli_argv(args.app, args.arguments)
        result = subprocess.run(argv, timeout=45)
        peer(pin, before)
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

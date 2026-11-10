"""One fresh GitHub Mac: accept the new full local Release package.

This is a new package acceptance case, not the previous nine-agent test suite.
It uses an empty disposable account and one synthetic shell workspace. It never
reads a user's saved session, installs locally, runs reload.sh, or starts CCC.
"""
from __future__ import annotations
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

import release_package as package
import source_contract as shared
import release_paired as paired

need = package.need
save = package.save
ROOT = Path(__file__).resolve().parent
LIMIT = 16 * 1024 * 1024
RUNTIME_SECONDS = 180


def error_record(error):
    record = {"error": repr(error), "status": "UNKNOWN", "automatic_retry": False}
    if isinstance(error, subprocess.TimeoutExpired):
        import base64
        record.update(argv=error.cmd, timeout_seconds=error.timeout)
        for key, value in (("stdout", error.stdout), ("stderr", error.stderr)):
            raw = value.encode() if isinstance(value, str) else value or b""
            record[key + "_partial_base64"] = base64.b64encode(raw).decode()
            record[key + "_partial_sha256"] = hashlib.sha256(raw).hexdigest()
    return record


def read_json(path):
    path = Path(path)
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_size <= LIMIT and info.st_nlink == 1,
         "bounded regular JSON required")
    return json.loads(path.read_text())


def snapshot_capture(path, workspace, surfaces):
    """Derive raw evidence, projection and fence from one stable file read."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_size <= LIMIT
             and before.st_uid == os.geteuid(),
             "bounded regular snapshot required")
        raw = stream.read(LIMIT + 1)
        after = os.fstat(stream.fileno())
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    need(len(raw) == before.st_size <= LIMIT and all(getattr(before, k) == getattr(after, k) for k in fields),
         "snapshot changed during the original read")
    projection = workspace_projection(json.loads(raw), workspace, surfaces)
    return raw, projection, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw), "device": before.st_dev, "inode": before.st_ino,
        "mtime_ns": before.st_mtime_ns, "ctime_ns": before.st_ctime_ns}


def fresh_saved_snapshot(path, workspace, surfaces, previous, generation_started_ns):
    """An old valid snapshot is not proof that the new GUI saved anything."""
    raw, projection, fence = snapshot_capture(path, workspace, surfaces)
    replaced = (fence["device"], fence["inode"]) != (previous["device"], previous["inode"])
    newer = (fence["mtime_ns"] > previous["mtime_ns"] or fence["ctime_ns"] > previous["ctime_ns"])
    need((replaced or newer) and max(fence["mtime_ns"], fence["ctime_ns"]) >= generation_started_ns,
         "new GUI has not produced a fresh natural snapshot write")
    return raw, projection, fence


def workspace_projection(snapshot, workspace, surfaces):
    values = [w for window in snapshot.get("windows", [])
              for w in window.get("tabManager", {}).get("workspaces", [])]
    wanted = str(uuid.UUID(workspace))
    selected = [w for w in values if str(uuid.UUID(w["workspaceId"])) == wanted]
    need(len(selected) == 1, "original saved workspace is missing or duplicated")
    row = selected[0]
    actual = {str(uuid.UUID(panel["id"])) for panel in row["panels"]}
    need(actual == {str(uuid.UUID(value)) for value in surfaces}, "saved surface identities differ")
    need(isinstance(row.get("stableId"), str) and row["stableId"], "saved stable workspace ID missing")
    return {"workspace_id": wanted, "stable_id": row["stableId"], "layout": row["layout"],
            "panels": [{"id": str(uuid.UUID(panel["id"])), "stable_id": panel["stableSurfaceId"],
                        "directory": panel["directory"], "type": panel["type"]}
                       for panel in row["panels"]]}


def tree_workspace(tree, workspace):
    rows = [w for window in tree.get("windows", []) for w in window.get("workspaces", [])
            if str(uuid.UUID(w["id"])) == str(uuid.UUID(workspace))]
    need(len(rows) == 1, "live original workspace is missing or duplicated")
    return rows[0]


def process_environment(identity):
    """Same original Darwin parser, loaded only after its byte pin is checked."""
    before = paired.process_identity(identity["pid"])
    need(before is not None and before == {key: identity[key] for key in before}, "GUI changed before environment read")
    libc = C.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.sysctl.argtypes = [C.POINTER(C.c_int), C.c_uint, C.c_void_p,
                            C.POINTER(C.c_size_t), C.c_void_p, C.c_size_t]
    libc.sysctl.restype = C.c_int
    mib = (C.c_int * 3)(1, 49, identity["pid"])
    size = C.c_size_t(0)
    need(libc.sysctl(mib, 3, None, C.byref(size), None, 0) == 0 and 4 < size.value <= 8 * 1024**2,
         "native GUI environment size unknown")
    data = C.create_string_buffer(size.value)
    need(libc.sysctl(mib, 3, data, C.byref(size), None, 0) == 0, "native GUI environment unavailable")
    original = ROOT / "prerequisites/original_args.py"
    pins = read_json(ROOT / "prerequisites/ORIGINAL-ARGS-PIN.json")
    need(package.digest(original) == pins["sha256"] == "629f677fd25f57e358675859ae798defe668e7085156cc1e2ec2fd1c6f18d7ed",
         "original Darwin environment parser changed")
    import importlib.util
    spec = importlib.util.spec_from_file_location("original_args", original)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    argv, environment = module.parse_arguments(data.raw[:size.value])
    need(paired.process_identity(identity["pid"]) == before, "GUI changed during environment read")
    need(argv and Path(argv[0]).name == Path(identity["executable"]).name,
         "GUI argv does not name the selected bundle executable")
    # Never publish the rest of the native environment (it may contain tokens).
    return {key: environment.get(key) for key in package.ENVIRONMENT}, {
        key: environment.get(key) for key in ("CMUX_TAG", "HOME", "CMUX_TEST_ISOLATED_SOCKET_DISCOVERY",
                                              "CMUX_SOCKET_PATH", "CMUX_ALLOW_SOCKET_OVERRIDE")}


class Smoke:
    def __init__(self, root, pin, app, old_cli):
        self.root, self.pin, self.app = root, pin, app
        self.started = time.monotonic()
        self.deadline = self.started + RUNTIME_SECONDS
        self.socket_path = paired.stable_socket_path()
        self.cached_socket_path = self.socket_path
        self.old_cli = Path(old_cli)
        self.launch_fence = None
        self.identity = None
        self.generation = 0
        self.sequence = 0
        self.environment = paired.safe_launch_environment(os.environ)
        self.cli = root / "bin/cmux"
        self.cli.parent.mkdir(mode=0o700)
        self.cli.symlink_to(app / "Contents/Resources/bin/cmux")

    def remaining(self, cap=10):
        value = min(cap, self.deadline - time.monotonic())
        need(value > 0, "shared 180-second package runtime budget expired")
        return value

    def wait_for(self, callback, cap=35):
        deadline = min(self.deadline, time.monotonic() + cap)
        last = None
        while time.monotonic() < deadline:
            try:
                return callback()
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
                last = str(error)
                time.sleep(min(.25, max(0, deadline - time.monotonic())))
        raise TimeoutError("bounded observation did not pass: " + str(last))

    def call(self, arguments, *, env=None, explicit=False, expect_failure=False, old=False):
        before = paired.peer(self.pin, self.identity, timeout=self.remaining(3))
        argv = [str(self.old_cli if old else self.cli)]
        if explicit:
            argv += ["--socket", self.socket_path]
        argv += list(arguments)
        self.sequence += 1
        path = self.root / ("CLI-%03d.json" % self.sequence)
        record = {"argv": argv, "returncode": None, "status": "UNKNOWN", "peer": before,
                  "old_fixed_cli": old, "explicit_environment_socket": (env or {}).get("CMUX_SOCKET_PATH")}
        save(self.root / ("CLI-%03d-INTENT.json" % self.sequence), record)
        started = time.monotonic()
        try:
            result = subprocess.run(argv, env=self.environment if env is None else env,
                                    capture_output=True, text=True, timeout=self.remaining(10))
        except subprocess.TimeoutExpired as error:
            # Python may return bytes for partial text-mode output. Preserve it
            # losslessly; no automatic retry, Enter or request replay.
            import base64
            for key, value in (("stdout", error.stdout), ("stderr", error.stderr)):
                data = value.encode() if isinstance(value, str) else value or b""
                record[key + "_partial_base64"] = base64.b64encode(data).decode()
                record[key + "_partial_sha256"] = hashlib.sha256(data).hexdigest()
            save(path, record | {"error": repr(error), "elapsed_seconds": time.monotonic() - started})
            raise
        record.update(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr,
                      status="COMPLETED", elapsed_seconds=time.monotonic() - started)
        save(path, record)
        paired.peer(self.pin, before, timeout=self.remaining(3))
        need((result.returncode != 0) if expect_failure else (result.returncode == 0),
             "unexpected CLI outcome: " + str(self.sequence))
        return result

    def capabilities(self):
        response = json.loads(self.call(["--json", "capabilities"]).stdout)
        need(response.get("protocol") == "cmux-socket" and response.get("version") == 2
             and response.get("socket_path") == self.socket_path
             and response.get("access_mode") == "automation", "CCC route/access contract differs")
        need({"system.tree", "system.top", "surface.read_text", "terminal.replay",
              "surface.send_text", "surface.send_key"}.issubset(response.get("methods", [])),
             "core CCC methods missing")
        need({"terminal.render_grid.v1", "terminal.replay.v1"}.issubset(response.get("capabilities", [])),
             "original CCC required capabilities missing")
        return response

    def start(self):
        need(self.generation < 2 and self.identity is None, "only two original package generations")
        self.generation += 1
        prefix = "GEN-" + str(self.generation)
        self.launch_fence = time.time_ns() // 1000
        save(self.root / (prefix + "-LAUNCH-INTENT.json"),
             {"app": str(self.app), "launch_services": True, "external_cmux_environment_overrides": False})
        try:
            opened = paired.launch(self.app, self.pin, self.environment, timeout=self.remaining(30))
        except BaseException as error:
            save(self.root / (prefix + "-OPEN-UNKNOWN.json"), error_record(error) | {"launch_fence_us": self.launch_fence})
            raise
        save(self.root / (prefix + "-OPEN.json"), opened)
        self.identity = self.wait_for(lambda: paired.peer(self.pin), 40)
        actual, extra = process_environment(self.identity)
        need(actual == package.ENVIRONMENT, "signed LSEnvironment did not reach the real GUI")
        need(not extra["CMUX_TAG"] and extra["HOME"] == str(Path.home())
             and not extra["CMUX_TEST_ISOLATED_SOCKET_DISCOVERY"]
             and (not extra["CMUX_SOCKET_PATH"] or extra["CMUX_SOCKET_PATH"] == self.socket_path)
             and not extra["CMUX_ALLOW_SOCKET_OVERRIDE"], "foreign launch identity/environment")
        save(self.root / (prefix + "-PEER-ENVIRONMENT.json"),
             {"peer": self.identity, "signed_environment": actual, "safe_extra": extra})
        caps = self.capabilities()
        save(self.root / (prefix + "-CAPABILITIES.json"), caps)
        def ready_tree():
            tree = json.loads(self.call(["--json", "--id-format", "both", "tree", "--all"]).stdout)
            need(tree.get("windows"), "GUI has not committed an initial window")
            return tree
        save(self.root / (prefix + "-READY-TREE.json"), self.wait_for(ready_tree, 20))
        return caps

    def stop(self, *, cold=False):
        if self.identity is None:
            return None
        identity = self.identity
        self.sequence += 1
        before = paired.process_identity(identity["pid"])
        if not cold and (before is None or before["birth"] != identity["birth"]):
            result = {"pid": identity["pid"], "birth": identity["birth"], "exact_generation_absent": True,
                      "signal_sent": False}
            save(self.root / ("STOP-%03d-EXIT.json" % self.sequence), result)
            self.identity = None
            return result
        need(before is not None and before == {key: identity[key] for key in before},
             "cleanup cannot use a changed GUI generation")
        sig = signal.SIGKILL if cold else signal.SIGTERM
        save(self.root / ("STOP-%03d-INTENT.json" % self.sequence),
             {"peer": identity, "signal": sig.name, "scope": "owned disposable GitHub GUI only",
              "physical_power_loss": False})
        os.kill(identity["pid"], sig)
        def absent():
            current = paired.process_identity(identity["pid"])
            need(current is None or current["birth"] != identity["birth"], "original GUI still alive")
            return {"pid": identity["pid"], "birth": identity["birth"], "exact_generation_absent": True}
        # Final cleanup has its own bounded 20 s; it cannot extend acceptance.
        if cold:
            result = self.wait_for(absent, 20)
        else:
            cleanup_deadline = time.monotonic() + 20
            while True:
                try:
                    result = absent()
                    break
                except ValueError:
                    need(time.monotonic() < cleanup_deadline, "owned cleanup timed out")
                    time.sleep(.25)
        save(self.root / ("STOP-%03d-EXIT.json" % self.sequence), result)
        self.identity = None
        return result

    def read_viewport(self, workspace, surface):
        before = paired.peer(self.pin, self.identity, timeout=self.remaining(3))
        request = {"id": str(uuid.uuid4()), "method": "surface.read_text",
                   "params": {"workspace_id": workspace, "surface_id": surface, "scrollback": False}}
        prefix = "CCC-VIEWPORT-GEN-%d" % self.generation
        record = {"request": request, "peer": before, "cached_path": self.cached_socket_path,
                  "controller_reconfigured": False}
        save(self.root / (prefix + "-INTENT.json"), record)
        data = b""
        started = time.monotonic()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(self.remaining(10))
                channel.connect(self.cached_socket_path)
                pid = int.from_bytes(channel.getsockopt(0, 2, 4), sys.byteorder, signed=True)
                need(pid == before["pid"], "viewport peer changed")
                channel.sendall((json.dumps(request) + "\n").encode())
                while b"\n" not in data:
                    channel.settimeout(self.remaining(10))
                    chunk = channel.recv(min(65536, LIMIT + 1 - len(data)))
                    need(chunk, "incomplete viewport reply")
                    data += chunk
                    need(len(data) <= LIMIT, "oversized viewport reply")
        except BaseException as error:
            import base64
            save(self.root / (prefix + "-UNKNOWN.json"), record | {"error": repr(error), "status": "UNKNOWN",
                "partial_base64": base64.b64encode(data).decode(), "partial_sha256": hashlib.sha256(data).hexdigest(),
                "elapsed_seconds": time.monotonic() - started})
            raise
        reply = json.loads(data.split(b"\n", 1)[0])
        save(self.root / (prefix + ".json"), record | {"reply": reply, "status": "COMPLETED",
                                                       "elapsed_seconds": time.monotonic() - started})
        need(reply.get("id") == request["id"] and reply.get("ok") is True
             and isinstance(reply.get("result"), dict), "CCC viewport route failed")
        paired.peer(self.pin, before, timeout=self.remaining(3))


def require_arm64_runner():
    machine = os.uname().machine
    need(machine == "arm64", "new Release runtime requires an actual arm64 runner before build")
    return machine


def runtime_preflight():
    package.original_admission().runner_only()
    machine = require_arm64_runner()
    need(Path("/dev/console").stat().st_uid == os.geteuid() and os.geteuid() > 0, "real non-root console owner required")
    manager = package.command(["/bin/launchctl", "managername"], timeout=5)["stdout"].strip()
    need(manager == "Aqua", "runtime must enter the real Aqua bootstrap")
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=True)
    need(home == Path.home().resolve(strict=True), "HOME differs from the real disposable account")
    absent = [home / "Library/Application Support/cmux", home / ".local/state/cmux", home / ".config/cmux",
              home / "Library/Preferences/com.cmuxterm.app.plist",
              home / "Library/Saved Application State/com.cmuxterm.app.savedState",
              Path(package.INSTALL_PATH), Path(paired.stable_socket_path())]
    need(all(not p.exists() and not p.is_symlink() for p in absent), "fresh disposable account/application required")
    paired.require_environment(home, os.environ)
    return {"home": str(home), "absent": list(map(str, absent)), "bootstrap": manager,
            "run": shared.run_identity(), "real_console_owner_uid": os.geteuid(), "machine": machine}


def runtime(root, package_root, old_cli):
    preflight = runtime_preflight()
    home = Path(preflight["home"])
    root.mkdir(mode=0o700)
    save(root / "FRESH-ACCOUNT.json", preflight)
    pin_path = package_root / "PACKAGE-PIN.json"
    pin, rows = paired.read_pin(pin_path, package.digest(pin_path), package_root / "PACKAGE-MANIFEST.json")
    app = Path(package.INSTALL_PATH)
    package.command(["/usr/bin/ditto", str(package_root / package.APP_NAME), str(app)])
    save(root / "INSTALLED-CI-ADMISSION.json", paired.verify_app(app, pin, rows))
    need(package.digest(old_cli) == shared.original_pin()["cli_sha256"], "old fixed CLI changed")
    smoke = Smoke(root, pin, app, old_cli)
    snapshot = home / "Library/Application Support/cmux/session-com.cmuxterm.app.json"
    failure = None
    result = None
    try:
        capabilities = smoke.start()
        explicit = json.loads(smoke.call(["--json", "capabilities"], explicit=True).stdout)
        inherited = json.loads(smoke.call(["--json", "capabilities"],
            env=smoke.environment | {"CMUX_SOCKET_PATH": paired.stable_socket_path()}).stdout)
        fields = ("protocol", "version", "socket_path", "access_mode", "methods", "capabilities")
        need(all(explicit.get(key) == capabilities.get(key) == inherited.get(key) for key in fields),
             "paired/implicit/inherited capabilities disagree")
        old_default = json.loads(smoke.call(["--json", "capabilities"], old=True).stdout)
        old_explicit = json.loads(smoke.call(["--json", "capabilities"], old=True, explicit=True).stdout)
        need(all(old_default.get(key) == capabilities.get(key) == old_explicit.get(key) for key in fields),
             "old fixed CLI and new Release do not agree on the stable peer")
        # An unrelated explicit socket must be refused instead of redirected.
        foreign = str(root / "foreign-absent.sock")
        smoke.call(["--json", "capabilities"], env=smoke.environment | {"CMUX_SOCKET_PATH": foreign}, expect_failure=True)
        marker = home / ".local/state/cmux/last-socket-path"
        info = marker.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
             and info.st_nlink == 1 and info.st_size <= 4096, "unsafe stable marker")
        marker_raw = marker.read_bytes()
        need(marker_raw.decode().strip() == paired.stable_socket_path(), "stable marker does not identify the new peer")
        save(root / "STABLE-MARKER.json", {"path": str(marker), "sha256": hashlib.sha256(marker_raw).hexdigest(),
                                         "socket_path": marker_raw.decode().strip()})
        before = json.loads(smoke.call(["--json", "--id-format", "both", "tree", "--all"]).stdout)
        ids = {w["id"] for win in before["windows"] for w in win["workspaces"]}
        smoke.call(["--json", "--id-format", "both", "workspace", "create",
                    "--name", "cmux-local-package-smoke", "--cwd", str(root), "--focus", "false"])
        tree = json.loads(smoke.call(["--json", "--id-format", "both", "tree", "--all"]).stdout)
        created = [w for win in tree["windows"] for w in win["workspaces"] if w["id"] not in ids]
        need(len(created) == 1, "synthetic workspace creation ambiguous")
        workspace = created[0]["id"]
        surfaces = [s["id"] for pane in created[0]["panes"] for s in pane["surfaces"]]
        need(len(surfaces) == 1, "one synthetic shell surface required")
        save(root / "EXPECTED-WORKSPACE.json", {"workspace_id": workspace, "surfaces": surfaces, "tree": tree})
        smoke.read_viewport(workspace, surfaces[0])
        raw_before, projection, fence = smoke.wait_for(lambda: snapshot_capture(snapshot, workspace, surfaces), 40)
        (root / "GEN-1-SNAPSHOT.json").write_bytes(raw_before)
        save(root / "SAVE-FENCE.json", fence | {"projection": projection, "writer": smoke.identity,
                                               "harness_snapshot_writes": 0})
        old_identity = dict(smoke.identity)
        smoke.stop(cold=True)
        actual_input, actual_projection, actual_fence = snapshot_capture(snapshot, workspace, surfaces)
        need(actual_projection == projection,
             "durable saved projection changed before restore")
        (root / "ACTUAL-RESTORE-INPUT.json").write_bytes(actual_input)
        save(root / "ACTUAL-RESTORE-INPUT-FENCE.json", actual_fence)
        smoke.start()
        need(smoke.identity["birth"] != old_identity["birth"], "second GUI generation not new")
        def restored_tree():
            tree = json.loads(smoke.call(["--json", "--id-format", "both", "tree", "--all"]).stdout)
            tree_workspace(tree, workspace)
            return tree
        tree2 = smoke.wait_for(restored_tree, 30)
        restored = tree_workspace(tree2, workspace)
        restored_surfaces = {str(uuid.UUID(s["id"])) for pane in restored["panes"] for s in pane["surfaces"]}
        need(restored_surfaces == {str(uuid.UUID(s)) for s in surfaces}, "original live surface UUIDs not restored")
        def post_restore_save():
            observed = fresh_saved_snapshot(snapshot, workspace, surfaces, actual_fence, smoke.launch_fence * 1000)
            writer = paired.process_identity(smoke.identity["pid"])
            need(writer is not None and writer == {key: smoke.identity[key] for key in writer},
                 "new writer generation changed during save observation")
            return observed
        after_raw, after_projection, after_fence = smoke.wait_for(post_restore_save, 30)
        need(after_projection == projection, "saved workspace layout/cwd/stable identities changed after restore")
        (root / "GEN-2-SNAPSHOT.json").write_bytes(after_raw)
        save(root / "GEN-2-SNAPSHOT-FENCE.json", after_fence | {"previous_restore_input_fence": actual_fence,
             "writer": smoke.identity, "generation_started_ns": smoke.launch_fence * 1000,
             "fresh_natural_write_observed": True, "harness_snapshot_writes": 0})
        save(root / "GEN-2-TREE.json", tree2)
        smoke.read_viewport(workspace, surfaces[0])
        old_after = json.loads(smoke.call(["--json", "capabilities"], old=True).stdout)
        need(all(old_after.get(key) == capabilities.get(key) for key in fields), "old CLI route changed after cold restore")
        save(root / "POST-RUNTIME-APP-ADMISSION.json", paired.verify_app(app, pin, rows, timeout=smoke.remaining(30)))
        elapsed = time.monotonic() - smoke.started
        need(elapsed <= RUNTIME_SECONDS, "acceptance exceeded the shared 180-second runtime budget")
        result = {"schema": "cmux-local-release-package-runtime-v1", "passed": True,
                  "run": shared.run_identity(), "configuration": "Release",
                  "package_pin_sha256": package.digest(pin_path), "complete_app_verified": True,
                  "launch_services_lsenvironment_verified": True, "default_cli_stable_route_verified": True,
                  "old_fixed_cli_default_and_explicit_stable_route_verified": True,
                  "old_fixed_cli_sha256": package.digest(old_cli), "old_fixed_cli_not_user_current_binary": True,
                  "unchanged_cached_path_across_generations_verified": True,
                  "actual_socket_path": smoke.socket_path, "socket_rule": package.SOCKET_RULE,
                  "explicit_new_socket_verified": True, "inherited_new_socket_verified": True,
                  "explicit_foreign_socket_refused": True, "ccc_viewport_contract_verified": True,
                  "same_production_namespace_saved_and_restored": True, "original_workspace_surface_uuids_restored": True,
                  "post_restore_natural_snapshot_write_verified": True,
                  "gui_generations": 2, "synthetic_workspaces_created": 1, "actual_native_agent_roots": 0,
                  "post_restart_harness_workspace_creations": 0, "snapshot_or_trust_writes_by_harness": 0,
                  "physical_power_loss_verified": False, "original_nine_agent_tests_rerun": 0,
                  "ccc_auto_continue_business_verified": False, "original_four_herdr_bindings_repaired": False,
                  "local_user_install": False, "shared_runtime_budget_seconds": RUNTIME_SECONDS,
                  "acceptance_elapsed_seconds": elapsed, "business_adoption_ready": False}
    except BaseException as error:
        failure = error
        save(root / "FAILURE.json", error_record(error) | {"generation": smoke.generation, "peer": smoke.identity})
    finally:
        cleanup_started = time.monotonic()
        try:
            if smoke.identity is None and smoke.launch_fence is not None:
                candidates = paired.installed_gui_processes()
                need(len(candidates) <= 1, "ambiguous owned GUI after launch outcome")
                if candidates:
                    candidate = candidates[0]
                    need(candidate["uid"] == os.geteuid() and candidate["birth"][0] * 1000000 + candidate["birth"][1]
                         >= smoke.launch_fence and package.digest(candidate["executable"]) == pin["application_sha256"],
                         "cannot bind cleanup to this isolated launch")
                    smoke.identity = candidate
                    save(root / "CLEANUP-RECOVERED-PEER.json", candidate)
            cleanup = smoke.stop(cold=False)
            need(not paired.installed_gui_processes(), "owned GUI remains after cleanup")
            cleanup_elapsed = time.monotonic() - cleanup_started
            need(cleanup_elapsed <= 20, "cleanup exceeded separate 20-second budget")
            save(root / "CLEANUP.json", {"passed": True, "owned_gui_exit": cleanup, "retained_snapshots": True,
                "cleanup_elapsed_seconds": cleanup_elapsed, "cleanup_budget_seconds": 20,
                "all_installed_ci_gui_generations_absent": True})
        except BaseException as error:
            save(root / "CLEANUP-UNKNOWN.json", error_record(error) | {"peer": smoke.identity})
            failure = failure or error
    if failure:
        raise failure
    need(read_json(root / "CLEANUP.json")["passed"], "cleanup remains unproven")
    save(root / "RESULT.json", result)
    return result


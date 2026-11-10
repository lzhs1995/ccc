"""Seal a separate full local Release package from this run's exact build.

No original package is modified. No installation or GUI operation is performed.
The main signature is local ad-hoc; existing valid nested signatures are kept,
and missing nested seals are created inside-out with no Team entitlements.
"""
from __future__ import annotations
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import stat
import struct
import subprocess
import time
import zipfile

import source_contract as shared
import release_build

need, save, digest = shared.need, shared.save, shared.digest
original_admission, canonical, command = shared.original_admission, shared.canonical, shared.command
BUNDLE_ID = "com.cmuxterm.app"
INSTALL_PATH = "/Applications/cmux.app"
APP_NAME = "cmux.app"
SOCKET_RULE = "<actual-account-home>/.local/state/cmux/cmux.sock"
SCHEMA = "cmux-local-release-package-pin-v1"
ENVIRONMENT = {
    "CMUX_BUNDLE_ID": BUNDLE_ID,
    "CMUX_BUNDLED_CLI_PATH": INSTALL_PATH + "/Contents/Resources/bin/cmux",
    "CMUX_SHELL_INTEGRATION_DIR": INSTALL_PATH + "/Contents/Resources/shell-integration",
    "CMUX_SOCKET_ENABLE": "1", "CMUX_SOCKET_MODE": "automation",
    "CMUX_REMOTE_DAEMON_ALLOW_LOCAL_BUILD": "0",
    "CMUX_AUTH_ENVIRONMENT": "production", "CMUX_AUTH_CALLBACK_SCHEME": "cmux",
    "CMUX_API_BASE_URL": "https://cmux.com", "CMUX_VM_API_BASE_URL": "https://cmux.com",
    "CMUX_AUTH_WWW_ORIGIN": "https://cmux.com", "CMUX_WWW_ORIGIN": "https://cmux.com",
    "CMUX_IROH_BROKER_BASE_URL": "https://cmux.com",
    "CMUX_IROH_V2_ENVIRONMENT": "production",
    "CMUX_IROH_V2_BASE_URL": "https://cmux-v2.debussy.workers.dev", "CMUX_IROH_V2_FORCE_RELAY": "0",
}
MAIN_ENTITLEMENTS = {"com.apple.security.personal-information." + key: True for key in
                     ("addressbook", "calendars", "location", "photos-library")}
RESOURCE_ENTITLEMENTS_SHA256 = "885d57276cecaa416e20a06c9662d237637cb18e1e111c9bdf33b63427fc94eb"
MACHO = {bytes.fromhex(x) for x in ("feedface", "cefaedfe", "feedfacf", "cffaedfe",
                                    "cafebabe", "bebafeca", "cafebabf", "bfbafeca")}


def file_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def read_pinned(path, expected=None, *, maximum=32 * 1024**2):
    """Read one bounded regular receipt without following links or racing replacement."""
    path = Path(path).absolute()
    need(path.resolve(strict=True) == path, "receipt path contains a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
             and before.st_uid in (0, os.geteuid()) and not before.st_mode & 0o022
             and before.st_size <= maximum, "unsafe or oversized regular receipt")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            need(total <= maximum, "receipt grew beyond bound")
            chunks.append(chunk)
        after = os.fstat(fd)
        need(file_identity(before) == file_identity(after) == file_identity(path.lstat()),
             "receipt changed during read")
        data = b"".join(chunks)
        need(len(data) == before.st_size, "receipt size changed")
        if expected is not None:
            need(re.fullmatch(r"[0-9a-f]{64}", expected or "") is not None
                 and hashlib.sha256(data).hexdigest() == expected, "pinned receipt differs")
        return data
    finally:
        os.close(fd)


def decode_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda value: need(False, "nonfinite JSON number"))


def validate_manifest(rows):
    need(isinstance(rows, list) and 0 < len(rows) <= 100000, "invalid complete manifest")
    seen = {}
    total = 0
    for row in rows:
        need(isinstance(row, dict), "invalid manifest row")
        name = row.get("path")
        need(isinstance(name, str) and name and "\\" not in name and "\x00" not in name
             and all(part not in ("", ".", "..") for part in name.split("/"))
             and not PurePosixPath(name).is_absolute() and name not in seen, "unsafe or duplicate manifest path")
        need(type(row.get("mode")) is int and 0 <= row["mode"] <= 0o777, "invalid member mode")
        kind = row.get("type")
        fields = {"path", "mode", "type"}
        if kind == "file":
            need(type(row.get("size")) is int and 0 <= row["size"] <= 3 * 1024**3
                 and re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", "")), "invalid file member")
            fields |= {"size", "sha256"}
            total += row["size"]
        elif kind == "symlink":
            target = row.get("target")
            need(isinstance(target, str) and target and "\\" not in target and "\x00" not in target
                 and not PurePosixPath(target).is_absolute(), "unsafe manifest symlink")
            parts = name.split("/")[:-1]
            for bit in target.split("/"):
                if bit == "..":
                    need(parts, "manifest symlink escapes bundle")
                    parts.pop()
                elif bit not in ("", "."):
                    parts.append(bit)
            need(parts, "manifest symlink resolves to bundle root")
            fields.add("target")
        else:
            need(kind == "directory", "special manifest member")
        need(set(row) == fields, "unexpected manifest member fields")
        seen[name] = row
    need(total <= 3 * 1024**3 and list(seen) == sorted(seen, key=PurePosixPath), "oversized or unordered manifest")
    for name in seen:
        for parent in PurePosixPath(name).parents:
            if str(parent) != ".":
                need(str(parent) in seen and seen[str(parent)]["type"] == "directory",
                     "manifest parent missing or not a directory")
    return seen


def macho_architectures(path):
    """Inspect bounded native headers; never execute lipo or the candidate."""
    path = Path(path).absolute()
    need(path.resolve(strict=True) == path, "native code path contains a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
             "native code is not one regular file")
        head = os.read(fd, 4096)
        need(file_identity(before) == file_identity(os.fstat(fd))
             == file_identity(path.lstat()), "native header changed")
    finally:
        os.close(fd)
    magic = head[:4]
    cpu_names = {7: "i386", 12: "arm", 0x01000007: "x86_64", 0x0100000c: "arm64"}
    thin = {bytes.fromhex("feedface"): (">", 28), bytes.fromhex("cefaedfe"): ("<", 28),
            bytes.fromhex("feedfacf"): (">", 32), bytes.fromhex("cffaedfe"): ("<", 32)}
    if magic in thin:
        endian, minimum = thin[magic]
        need(len(head) >= minimum, "truncated Mach-O header")
        cpus = [struct.unpack_from(endian + "I", head, 4)[0]]
    else:
        fat = {bytes.fromhex("cafebabe"): (">", 20), bytes.fromhex("bebafeca"): ("<", 20),
               bytes.fromhex("cafebabf"): (">", 32), bytes.fromhex("bfbafeca"): ("<", 32)}
        need(magic in fat and len(head) >= 8, "unknown native code header")
        endian, width = fat[magic]
        count = struct.unpack_from(endian + "I", head, 4)[0]
        need(0 < count <= 64 and 8 + count * width <= len(head), "truncated universal header")
        cpus, intervals = [], []
        for index in range(count):
            start = 8 + index * width
            cpus.append(struct.unpack_from(endian + "I", head, start)[0])
            offset, size = struct.unpack_from(endian + ("II" if width == 20 else "QQ"), head, start + 8)
            need(offset >= 8 + count * width and size >= 28 and offset + size <= before.st_size,
                 "universal architecture range invalid")
            intervals.append((offset, offset + size))
        intervals.sort()
        need(all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:])), "overlapping native slices")
    need(all(cpu in cpu_names for cpu in cpus) and len(set(cpus)) == len(cpus), "unknown or duplicate CPU type")
    return sorted(cpu_names[cpu] for cpu in cpus)


def native_code_inventory(app):
    return [{"path": path.relative_to(app).as_posix(), "sha256": digest(path),
             "architectures": macho_architectures(path)} for path in macho_files(app)]


def transform_plist(raw):
    before = plistlib.loads(raw)
    need(before.get("CFBundleIdentifier") == BUNDLE_ID and before.get("CFBundleExecutable") == "cmux",
         "input is not the original native Release product")
    need(before.get("CMUXIrohRelayPolicyKeyID") == "cmux-production-relay-policy-2026-07",
         "input did not resolve the production Release configuration")
    after = copy.deepcopy(before)
    after.update(CFBundleDisplayName="cmux Local Restore", CFBundleName="cmux Local Restore",
                 LSEnvironment=dict(ENVIRONMENT), CMUXCloudDogfoodEnabled=False,
                 SUEnableAutomaticChecks=False, SUAutomaticallyUpdate=False,
                 CMUX_IROH_V2_ENVIRONMENT="production",
                 CMUX_IROH_V2_BASE_URL=ENVIRONMENT["CMUX_IROH_V2_BASE_URL"], CMUX_IROH_V2_FORCE_RELAY="0",
                 CMUXLocalRepair={"configuration": "Release", "signature": "ad-hoc", "officialRelease": False,
                                  "sourceTree": shared.GREEN_TREE, "requiredInstallPath": INSTALL_PATH,
                                  "socketRule": SOCKET_RULE})
    schemes = [r for r in after.get("CFBundleURLTypes", []) if r.get("CFBundleURLName") == BUNDLE_ID + ".auth"]
    need(len(schemes) == 1 and schemes[0].get("CFBundleURLSchemes") == ["cmux"], "wrong Release auth callback")
    output = plistlib.dumps(after, fmt=plistlib.FMT_XML, sort_keys=True)
    forbidden = (b"/Users/", b"http://localhost:", b"cmux-staging.vercel.app",
                 b"cmux-v2-development.", b"nestedrestore.", b"$(", b"allowAll")
    need(not any(token in output for token in forbidden), "unresolved CI/dev package metadata remains")
    need("CMUX_SOCKET_PATH" not in after["LSEnvironment"] and "HOME" not in after["LSEnvironment"],
         "per-account stable path must be selected by native Release code")
    return output, {key: {"before": before.get(key), "after": after.get(key)}
                    for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}


def macho_files(app):
    result = []
    for path in sorted(Path(app).rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as stream:
            magic = stream.read(4)
        if magic in MACHO:
            result.append(path)
    need(result, "bundle has no Mach-O code")
    return result


def signing_targets(app):
    app = Path(app).resolve(strict=True)
    files = macho_files(app)
    bundles, executables = {}, set()
    for path in [app, *[p for p in app.rglob("*") if p.is_dir() and not p.is_symlink()]]:
        if path.suffix not in (".app", ".framework", ".xpc", ".appex", ".plugin", ".systemextension", ".bundle"):
            continue
        candidates = [path / "Contents/Info.plist", path / "Resources/Info.plist", path / "Info.plist"]
        info_path = next((p for p in candidates if p.is_file()), None)
        if info_path is None:
            continue
        info = plistlib.loads(info_path.read_bytes())
        name = info.get("CFBundleExecutable")
        if not name:
            continue
        need(isinstance(name, str) and "/" not in name and name not in (".", ".."), "unsafe nested executable")
        candidates = [path / "Contents/MacOS" / name, path / name]
        executable = next((p.resolve(strict=True) for p in candidates if p.is_file()), None)
        need(executable is not None and executable.is_relative_to(app) and executable in files,
             "nested executable is not contained native code: " + str(path))
        bundles[path] = executable
        executables.add(executable)
    targets = [(path, path) for path in files if path not in executables] + list(bundles.items())
    return sorted(targets, key=lambda row: (-len(row[0].parts), str(row[0])))


def sign_closure(app, root, commands):
    empty = root / "EMPTY-ENTITLEMENTS.plist"
    main = root / "LOCAL-MAIN-ENTITLEMENTS.plist"
    empty.write_bytes(plistlib.dumps({})); main.write_bytes(plistlib.dumps(MAIN_ENTITLEMENTS))
    app = Path(app).resolve(strict=True)
    targets = signing_targets(app)
    need(sum(target == app for target, _ in targets) == 1 and targets[-1][0] == app,
         "inside-out signing must end with the single main app")
    need({executable for _, executable in targets} == set(macho_files(app)),
         "signing closure omits native code")
    receipts = []
    for target, executable in targets:
        before = digest(executable)
        architectures = macho_architectures(executable)
        verify = commands.run(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", target],
                              allow_failure=True)
        resign = target == app or verify["returncode"] != 0
        if resign:
            argv = ["/usr/bin/codesign", "--force", "--sign", "-", "--timestamp=none",
                    "--generate-entitlement-der", "--entitlements", main if target == app else empty]
            if target == app:
                argv += ["--identifier", BUNDLE_ID]
            commands.run(argv + [target])
        commands.run(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", target])
        display = commands.run(["/usr/bin/codesign", "--display", "--verbose=4", target])
        entitlements = commands.run(["/usr/bin/codesign", "--display", "--entitlements", ":-", target])
        text = (commands.root / display["stderr"]["file"]).read_text()
        hashes = re.findall(r"^CDHash=(.+)$", text, re.M)
        need(len(hashes) == 1, "nested signature has no unique code-directory hash")
        if target == app:
            effective = (commands.root / entitlements["stdout"]["file"]).read_bytes()
            need(plistlib.loads(effective) == MAIN_ENTITLEMENTS and "Signature=adhoc" in text
                 and re.findall(r"^Identifier=(.+)$", text, re.M) == [BUNDLE_ID]
                 and re.findall(r"^TeamIdentifier=(.+)$", text, re.M) == ["not set"],
                 "main signature is not the declared local Release signature")
        after = digest(executable)
        # Existing manifest-pinned SSH payloads must retain their exact signed
        # bytes; do not silently invalidate their bundled content contracts.
        if "cmux-tui-ssh" in executable.parts:
            need(before == after, "SSH payload needs its original manifest-aware signing path")
        receipts.append({"target": target.relative_to(app).as_posix(),
                         "executable": executable.relative_to(app).as_posix(), "before_sha256": before,
                         "after_sha256": after, "resigned_locally": resign, "cdhash": hashes[0],
                         "architectures": architectures,
                         "display": display, "entitlements": entitlements})
    commands.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", app])
    save(root / "SIGNATURE-CLOSURE.json", {"targets": receipts, "all_native_code_enumerated": True,
        "main_entitlements": MAIN_ENTITLEMENTS, "hardened_runtime_claimed": False,
        "upstream_team_features_verified": False, "nested_components_removed": 0})
    return receipts


def package(build_root, output):
    shared.original_admission().runner_only()
    shared.original_pin()
    resources = shared.PRE / "Release-Resources.entitlements"
    need(digest(resources) == RESOURCE_ENTITLEMENTS_SHA256, "fixed Release entitlement source changed")
    requested = plistlib.loads(resources.read_bytes())
    need({k: v for k, v in requested.items() if k != "keychain-access-groups"} == MAIN_ENTITLEMENTS,
         "local main entitlement derivation differs")
    build_root = Path(build_root).resolve(strict=True)
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    need(build_root.is_relative_to(temporary), "build must be this private CI attempt")
    built = shared.read_json(build_root / "RESULT.json")
    need(built.get("build_passed") is True and built.get("source_tree") == shared.GREEN_TREE
         and built.get("configuration") == "Release" and built.get("run") == shared.run_identity(), "unaccepted build")
    settings_path = build_root / "RESOLVED-RELEASE-SETTINGS.json"
    need(digest(settings_path) == built["resolved_settings_sha256"], "resolved compile settings changed")
    release_build.validate_settings([{"target": "cmux", "buildSettings": shared.read_json(settings_path)}])
    manifest_path = build_root / "UNSIGNED-BUNDLE-MANIFEST.json"
    need(digest(manifest_path) == built["unsigned_manifest_sha256"], "unsigned build manifest changed")
    before = decode_json(read_pinned(manifest_path, built["unsigned_manifest_sha256"]))
    validate_manifest(before)
    source = build_root / "derived/Build/Products/Release/cmux.app"
    need(source.resolve(strict=True) == source and str(source) == built["app"], "unexpected build output path")
    admission = shared.original_admission()
    need(admission.inventory(source) == before, "complete compiled bundle changed")
    root = shared.fresh_runner_output(output)
    commands = shared.Commands(root / "commands")
    app = root / APP_NAME
    commands.run(["/usr/bin/ditto", source, app], timeout=300)
    need(admission.inventory(app) == before, "independent full copy differs")
    raw, delta = transform_plist((app / "Contents/Info.plist").read_bytes())
    (app / "Contents/Info.plist").write_bytes(raw)
    receipts = sign_closure(app, root, commands)
    after = admission.inventory(app)
    validate_manifest(after)
    native = native_code_inventory(app)
    primary = {row["path"]: row["architectures"] for row in native}
    need(primary.get("Contents/MacOS/cmux") == ["arm64"]
         and primary.get("Contents/Resources/bin/cmux") == ["arm64"], "app/paired CLI must be arm64")
    need({row["executable"]: row["after_sha256"] for row in receipts}
         == {row["path"]: row["sha256"] for row in native}, "signature and native code closure differs")
    prior, current = {row["path"]: row for row in before}, {row["path"]: row for row in after}
    need(prior.keys() <= current.keys(), "complete package lost original members")
    changed = [name for name in prior if prior[name] != current[name]]
    code = {path.relative_to(app).as_posix() for path in macho_files(app)}
    need(all(name in code or name == "Contents/Info.plist" or "/_CodeSignature/" in name for name in changed),
         "package changed non-code/non-signature resources")
    need(all("/_CodeSignature/" in name or name.endswith("/_CodeSignature") for name in current.keys() - prior.keys()),
         "package added unexpected members")
    need(admission.inventory(source) == before, "unsigned source bundle changed")
    archive = root / "cmux-release.zip"
    commands.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", app, archive], timeout=300)
    admission.inspect_zip(archive, APP_NAME)
    readback = root / "archive-readback"
    commands.run(["/usr/bin/ditto", "-x", "-k", archive, readback], timeout=300)
    need(admission.inventory(readback / APP_NAME) == after, "complete archive readback differs")
    commands.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", readback / APP_NAME])
    save(root / "PACKAGE-MANIFEST.json", after)
    save(root / "TRANSFORM.json", {"metadata_changes": delta, "changed_members": changed,
        "new_signature_members": sorted(current.keys() - prior.keys()), "source_preserved": True,
        "source_build_result_sha256": digest(build_root / "RESULT.json")})
    main = next(row for row in receipts if row["target"] == ".")
    pin = {"schema": SCHEMA, "passed": True, "status": "SIGNED_ARCHIVE_RUNTIME_NOT_YET_ACCEPTED",
        "run": shared.run_identity(), "configuration": "Release", "official_release": False,
        "signature": "ad-hoc", "bundle_id": BUNDLE_ID, "bundle_executable": "cmux", "app_name": APP_NAME,
        "required_install_path": INSTALL_PATH, "socket_rule": SOCKET_RULE, "signed_environment": ENVIRONMENT,
        "source_tree": shared.GREEN_TREE, "source_upstream": shared.UPSTREAM,
        "original_source_app_pin_sha256": shared.SOURCE_PIN_SHA256,
        "build_result_sha256": digest(build_root / "RESULT.json"),
        "resolved_settings_sha256": built["resolved_settings_sha256"],
        "unsigned_manifest_sha256": built["unsigned_manifest_sha256"],
        "manifest_sha256": digest(root / "PACKAGE-MANIFEST.json"),
        "inventory_canonical_sha256": hashlib.sha256(shared.canonical(after)).hexdigest(),
        "signature_closure_sha256": digest(root / "SIGNATURE-CLOSURE.json"),
        "member_count": len(after), "architecture": ["arm64"], "native_code": native,
        "signing_targets": [{"target": row["target"], "executable": row["executable"]} for row in receipts],
        "native_code_canonical_sha256": hashlib.sha256(canonical(native)).hexdigest(),
        "transform_sha256": digest(root / "TRANSFORM.json"),
        "code_directory_hash": main["cdhash"], "application_sha256": digest(app / "Contents/MacOS/cmux"),
        "cli_sha256": digest(app / "Contents/Resources/bin/cmux"),
        "info_plist_sha256": digest(app / "Contents/Info.plist"), "main_entitlements": MAIN_ENTITLEMENTS,
        "archive": {"name": archive.name, "sha256": digest(archive), "bytes": archive.stat().st_size},
        "runtime_verified": False, "business_adoption_ready": False, "installed_on_user_machine": False,
        "old_tests_rerun": 0, "swift_builds_this_attempt": 1, "upstream_team_features_verified": False}
    save(root / "PACKAGE-PIN.json", pin)
    save(root / "PACKAGE-PIN-SHA256.json", {"sha256": digest(root / "PACKAGE-PIN.json")})
    return pin


def prior_cli_member(source, pin):
    """Use the exact original manifest size, never an unrelated CLI estimate."""
    raw = read_pinned(shared.PRE / "PROVENANCE.json", pin["provenance_sha256"])
    manifest = decode_json(raw).get("bundle_members")
    need(isinstance(manifest, list), "original CLI manifest missing")
    relative = "Contents/Resources/bin/cmux"
    expected = [row for row in manifest if row.get("path") == relative]
    need(len(expected) == 1 and expected[0].get("type") == "file"
         and type(expected[0].get("size")) is int and 0 < expected[0]["size"] <= 3 * 1024**3
         and expected[0].get("sha256") == pin["cli_sha256"], "original CLI manifest differs")
    rows = [row for row in source.infolist() if row.filename == pin["app_name"] + "/" + relative]
    need(len(rows) == 1 and not rows[0].is_dir() and rows[0].file_size == expected[0]["size"]
         and stat.S_IFMT(rows[0].external_attr >> 16) in (0, stat.S_IFREG), "old CLI member differs")
    return rows[0], expected[0]


def download_prior_cli(output):
    """Download the exact accepted old artifact on GitHub; extract only its CLI.

    This does not launch, install or rebuild the old app. The full old archive
    stays private on the disposable runner and is not in the evidence artifact.
    """
    shared.original_admission().runner_only()
    pin = shared.original_pin()
    root = shared.fresh_runner_output(output)
    archive = root / "original-artifact.zip"
    argv = ["gh", "api", "--method", "GET", "repos/lzhs1995/ccc/actions/artifacts/" + str(pin["artifact_id"]) + "/zip"]
    save(root / "DOWNLOAD-INTENT.json", {"argv": argv, "timeout_seconds": 600, "retries": 0})
    started = time.monotonic(); outcome = {"status": "UNKNOWN", "returncode": None}
    error = None
    with archive.open("xb") as stdout, (root / "DOWNLOAD.stderr").open("xb") as stderr:
        try:
            completed = subprocess.run(argv, stdout=stdout, stderr=stderr, timeout=600)
            outcome.update(status="COMPLETED", returncode=completed.returncode)
        except BaseException as exc:
            error = exc; outcome["error"] = repr(exc)
    save(root / "DOWNLOAD.json", outcome | {"elapsed_seconds": time.monotonic() - started,
        "sha256": digest(archive), "bytes": archive.stat().st_size, "source_app_pin_sha256": shared.SOURCE_PIN_SHA256})
    if error is not None:
        raise error
    need(outcome["returncode"] == 0 and digest(archive) == pin["artifact_download_zip_sha256"], "original download differs")
    shared.original_admission().validate_artifact_members(archive, pin)
    inner = root / "original-app.zip"
    with zipfile.ZipFile(archive) as source, source.open(pin["artifact_members"]["app_archive"]) as stream, inner.open("xb") as target:
        shutil.copyfileobj(stream, target, 1024 * 1024)
    need(digest(inner) == pin["retained_app_zip_sha256"], "original inner archive differs")
    shared.original_admission().inspect_zip(inner, pin["app_name"])
    with zipfile.ZipFile(inner) as source:
        row, expected = prior_cli_member(source, pin)
        cli = root / "cmux"
        with source.open(row) as stream, cli.open("xb") as target:
            shutil.copyfileobj(stream, target, 1024 * 1024)
    need(cli.stat().st_size == expected["size"] and digest(cli) == pin["cli_sha256"],
         "old CLI differs from accepted original")
    need(macho_architectures(cli) == ["arm64"], "old fixed CLI architecture differs")
    cli.chmod(0o700)
    shared.Commands(root / "commands").run(["/usr/bin/codesign", "--verify", "--strict", cli])
    save(root / "CLI-PIN.json", {"sha256": digest(cli), "bytes": expected["size"],
        "original_manifest_member": expected, "original_source_pin_sha256": shared.SOURCE_PIN_SHA256,
        "source_build_run": [pin["build_run_id"], pin["build_run_attempt"]],
        "source_artifact_id": pin["artifact_id"], "old_gui_launched": False,
        "not_a_claim_of_user_current_cli_identity": True})
    return cli

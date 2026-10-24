#!/usr/bin/env python3
"""CCC's small CLI/UI bundle; every action selects the activated runtime anew.

The UI bundle never carries a second copy of the continuation engine. Existing
batch workers and callbacks remain pinned to the runtime that created them.
"""

from __future__ import annotations

import hashlib
import contextlib
import fcntl
import importlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import uuid
from pathlib import Path

sys.dont_write_bytecode = True

APP = Path.home() / "Library" / "Application Support" / "cmux-codex-continue"
CLI_FILES = (
    "ccc_cli.py", "cmux_supervisor_tui.py", "ccc_claude_request_key.py",
    "bin/cmux-codex-continue", "bin/cmux-stack",
)
DEFAULT_LINKS = (Path("/opt/homebrew/bin/cmux-codex-continue"), Path("/opt/homebrew/bin/ccc"))
SHA = re.compile(r"[0-9a-f]{64}\Z")


class BundleError(RuntimeError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def _read(path: Path) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise BundleError(f"not an ordinary bundle file: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise BundleError(f"bundle file replaced: {path}")
        data = handle.read()
        after = os.fstat(handle.fileno())
    fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode)
    if fields(before) != fields(opened) or fields(opened) != fields(after):
        raise BundleError(f"bundle file changed while reading: {path}")
    return data


def _tree(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise BundleError(f"bundle root must be a real directory: {root}")
    result = {}
    for path in sorted(root.rglob("*")):
        if stat.S_ISDIR(path.lstat().st_mode):
            continue
        result[path.relative_to(root).as_posix()] = _read(path)
    return result


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_bundle(root: Path, *, cli: bool = False) -> dict:
    """Check the complete content-addressed tree before importing any of it."""
    try:
        payloads = _tree(root)
        manifest = json.loads(payloads.pop("manifest.json"), object_pairs_hook=_object)
        if not isinstance(manifest, dict) or type(manifest.get("version")) is not int:
            raise BundleError("invalid bundle manifest")
        hashes = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
        if not hashes or manifest.get("files") != hashes:
            raise BundleError("bundle file set or contents differ from manifest")
        version = manifest["version"]
        if cli:
            if (version != 1 or manifest.get("kind") != "ccc-cli"
                    or set(manifest) != {"kind", "version", "files", "modes"}
                    or set(hashes) != set(CLI_FILES)):
                raise BundleError("incomplete CLI/UI bundle")
        else:
            expected_fields = {"version", "files"}
            if version in (2, 3):
                expected_fields.add("mac")
                rows = sorted([name[len("mac/source/"):], digest]
                              for name, digest in hashes.items() if name.startswith("mac/source/"))
                mac = {"source": "mac/source", "files": len(rows),
                       "sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()}
                if not rows or manifest.get("mac") != mac:
                    raise BundleError("runtime MAC tree differs from manifest")
            if version == 3:
                expected_fields.add("modes")
            if version not in (1, 2, 3) or set(manifest) != expected_fields:
                raise BundleError("unsupported runtime manifest")
            if not {"cmux_codex_watch.py", "ccc_workspace_batch.py", "ccc_guard_scope.py"} <= set(hashes):
                raise BundleError("runtime lacks required entrypoints")
        address = hashes
        if cli or version == 3:
            modes = {name: stat.S_IMODE((root / name).lstat().st_mode) for name in hashes}
            if manifest.get("modes") != modes:
                raise BundleError("bundle executable modes changed")
            if cli and any(mode not in (0o444, 0o555) for mode in modes.values()):
                raise BundleError("CLI bundle must be read-only")
            address = {"files": hashes, "modes": modes}
        if root.name != _digest(address):
            raise BundleError("bundle content address differs")
        return manifest
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise BundleError(f"cannot verify bundle {root}: {exc}") from exc


def current_release(root: Path) -> Path:
    root = root.absolute()
    current = root / "current"
    if not current.is_symlink():
        raise BundleError(f"no activated bundle: {current}")
    release = current.resolve(strict=True)
    if release.parent != root / "releases" or not SHA.fullmatch(release.name):
        raise BundleError(f"activated bundle is outside its release directory: {current}")
    return release


def _link_value(path: Path) -> str | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode):
        raise BundleError(f"refusing to replace non-symlink: {path}")
    return os.readlink(path)


@contextlib.contextmanager
def _install_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / "install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise BundleError("CLI installation lock is not an ordinary file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BundleError("another CLI installation is in progress") from exc
        yield
    finally:
        os.close(fd)


def _pointer(path: Path, target: str, *, expected: str | None) -> None:
    if _link_value(path) != expected:
        raise BundleError(f"CLI pointer changed before replacement: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.new")
    try:
        temporary.symlink_to(target)
        if _link_value(path) != expected:
            raise BundleError(f"CLI pointer changed before replacement: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stage_cli(source: Path, root: Path = APP / "cli") -> Path:
    source, root = source.resolve(), root.absolute()
    payloads = {name: _read(source / name) for name in CLI_FILES}
    for name in CLI_FILES:
        if name.endswith(".py") or name == "bin/cmux-stack":
            compile(payloads[name], str(source / name), "exec")
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}
    modes = {name: 0o555 if name.startswith("bin/") else 0o444 for name in CLI_FILES}
    release = root / "releases" / _digest({"files": hashes, "modes": modes})
    if release.exists():
        verify_bundle(release, cli=True)
        return release
    release.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=release.parent))
    try:
        for name, data in payloads.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(modes[name])
        (temporary / "manifest.json").write_text(json.dumps(
            {"kind": "ccc-cli", "version": 1, "files": hashes, "modes": modes},
            sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (temporary / "manifest.json").chmod(0o444)
        os.replace(temporary, release)
        (release / "bin").chmod(0o555)
        release.chmod(0o555)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    verify_bundle(release, cli=True)
    return release


def install_cli(source: Path, *, root: Path = APP / "cli", links=DEFAULT_LINKS,
                allowed_previous: dict[str, str] | None = None) -> Path:
    """Install only CLI/UI files and links. Never start or stop a service."""
    root = root.resolve()
    links = tuple(dict.fromkeys(Path(link).absolute() for link in links))
    target = str(root / "current" / "bin" / "cmux-codex-continue")
    allowed_previous = allowed_previous or {}
    # Refuse ordinary files before staging or opening a transaction. In
    # particular, a failed update must never remove an existing current file.
    _link_value(root / "current")
    for link in links:
        _link_value(link)
    with _install_lock(root):
        previous = {link: _link_value(link) for link in links}
        for link, old in previous.items():
            if old is not None and old != target and allowed_previous.get(str(link)) != old:
                raise BundleError(f"CLI migration needs exact previous target: {link}")
        current = root / "current"
        old_current = _link_value(current)
        release = stage_cli(source, root)
        replacements = [(current, old_current, str(release))]
        for link in links:
            if previous[link] != target:
                replacements.append((link, previous[link], target))
        applied = []
        try:
            for path, old, new in replacements:
                _pointer(path, new, expected=old)
                applied.append((path, old, new))
        except Exception as exc:
            rollback_errors = []
            for path, old, new in reversed(applied):
                try:
                    if _link_value(path) != new:
                        raise BundleError(f"changed CLI pointer preserved during rollback: {path}")
                    if old is None:
                        path.unlink()
                    else:
                        _pointer(path, old, expected=new)
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if rollback_errors:
                raise BundleError(f"CLI installation failed: {exc}; rollback: {rollback_errors}") from exc
            raise
    return release


def uninstall_cli(*, root: Path = APP / "cli", links=DEFAULT_LINKS) -> None:
    """Remove only our exact public links, preserving all releases and services."""
    root = root.resolve()
    target = str(root / "current" / "bin" / "cmux-codex-continue")
    with _install_lock(root):
        for link in dict.fromkeys(links):
            if link.is_symlink() and os.readlink(link) == target:
                link.unlink()


def entry_command(config_path: Path, suggested_surface: str = "") -> str:
    import shlex
    command = [sys.executable, "-I", "-B", str(Path(__file__).resolve()),
               "--config", str(config_path), "tui"]
    if suggested_surface:
        command.extend(["--suggest-surface", suggested_surface])
    return shlex.join(command)


def load_runtime(cli_dir: Path, runtime_root: Path):
    verify_bundle(cli_dir, cli=True)
    release = current_release(runtime_root)
    manifest = verify_bundle(release)
    # Production starts with -I. Do not let the invoking directory/PYTHONPATH
    # supply a second engine when this function is exercised directly either.
    sys.path[:] = [str(release), str(cli_dir), *[
        p for p in sys.path if p and Path(p).resolve() not in {Path.cwd(), Path(__file__).resolve().parent}
        and p not in {str(release), str(cli_dir)}]]
    previous = sys.modules.get("cmux_codex_watch")
    if previous is not None and Path(previous.__file__).resolve() != release / "cmux_codex_watch.py":
        raise BundleError("a different runtime is already loaded")
    core = importlib.import_module("cmux_codex_watch")
    try:
        core.validate_runtime_release(release)
    except (RuntimeError, ValueError) as exc:
        raise BundleError(f"runtime rejected its bundle: {release}") from exc
    # Compatibility with activated pre-gateway runtimes; no release is edited.
    core.install_cli_link = lambda path=DEFAULT_LINKS[0]: install_cli(
        cli_dir, links=(path, DEFAULT_LINKS[1]))
    core.uninstall_cli_link = lambda path=DEFAULT_LINKS[0]: uninstall_cli(
        links=(path, DEFAULT_LINKS[1]))
    core.supervisor_command = entry_command
    os.environ["CCC_RUNTIME_ROOT"] = str(runtime_root.absolute())
    # cmux-stack supports this gateway as its watcher command. Each status
    # subprocess verifies/selects current again instead of pinning an old core.
    os.environ["CMUX_STACK_WATCHER"] = str(cli_dir / "ccc_cli.py")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    return core, release, manifest


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        cli_dir = Path(__file__).resolve().parent
        runtime_root = Path(os.environ.get("CCC_RUNTIME_ROOT", str(APP / "runtime")))
        core, release, manifest = load_runtime(cli_dir, runtime_root)
        if args == ["--entry-info"]:
            batch = importlib.import_module("ccc_workspace_batch")
            ui = importlib.import_module("cmux_supervisor_tui")
            importlib.import_module("ccc_claude_request_key")
            print(json.dumps({"cli_release": str(cli_dir), "runtime_release": str(release),
                              "runtime_version": manifest["version"],
                              "runtime_files": len(manifest["files"]),
                              "core": core.__file__, "batch": batch.__file__,
                              "ui": ui.__file__, "stack": str(ui.STACK_CTL),
                              "stack_watcher": os.environ["CMUX_STACK_WATCHER"]}, sort_keys=True))
            return 0
        return core.cli(args)
    except (BundleError, OSError) as exc:
        print(f"ccc: activated entry verification failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

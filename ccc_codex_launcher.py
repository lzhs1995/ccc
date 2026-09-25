"""Transparent native launcher except inside a verified B batch workspace."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import uuid

import ccc_batch_guard as guard


def current_workspace(config_path, surface_id):
    from ccc_guard_migration import cmux_client
    from ccc_guard_scope import records
    row = records(cmux_client(config_path).tree()).get(guard.uid(surface_id))
    if not row:
        raise RuntimeError("cannot verify this surface's current workspace; no B session launched")
    return row["workspace_id"]


def run(config_path, native, args):
    wid = os.environ.get("CMUX_WORKSPACE_ID", "")
    try:
        wid = guard.uid(wid)
    except (ValueError, TypeError):
        os.execv(native, [native, *args])
    try:
        protected = guard.provenance(config_path, wid)
        if not protected:
            # An old open Supervisor can still write a valid legacy B job.
            # A real matching job is required; workspace names confer nothing.
            from ccc_guard_migration import establish_provenance
            config = guard.core().ConfigStore(Path(config_path)).load()
            if any(r.get("workspace_id", "").upper() == wid and r.get("last_batch_id")
                   for r in config.get("workspace_rules", [])):
                establish_provenance(config_path, wid)
                protected = guard.provenance(config_path, wid)
    except (OSError, ValueError, RuntimeError):
        # A corrupt configuration must not open a previously protected pool.
        if guard.pool_dir(config_path, wid).exists():
            raise RuntimeError("B 保护配置不可读，Codex 请求入口保持关闭")
        protected = None
    if not protected:
        os.execv(native, [native, *args])
    if os.environ.get("CMUX_SURFACE_ID"):
        actual = current_workspace(config_path, os.environ["CMUX_SURFACE_ID"])
        if actual != wid:
            # A moved shell retains its inherited environment. Old placement
            # must not impose B restrictions on the destination workspace.
            os.environ["CMUX_WORKSPACE_ID"] = actual
            return run(config_path, native, args)
    # Read-only CLI operations do not create sessions or model requests.
    if any(x in args for x in ("--help", "-h", "--version", "-V")) or (args and args[0] in {
            "login", "logout", "completion", "mcp", "features", "update", "help"}):
        os.execv(native, [native, *args])
    if "--remote" in args or (args and args[0] in {"app-server", "exec", "e", "review", "fork"}):
        raise RuntimeError("此 B 工作区仅允许受实时保护的交互 Codex；请求入口未放行")
    from ccc_guard_migration import launch_options
    guard.private_directory(guard.guard_root(config_path))
    directory = guard.pool_dir(config_path, wid)
    guard.private_directory(directory)
    # Parallel old-B shells coalesce around one original-session adoption.
    # A previous success or manual pause always requires the explicit W action.
    with guard.core().FileLock(directory / "arm.lock", timeout_sec=180):
        rule = guard.provenance(config_path, wid)
        if not rule or rule.get("paused") or guard.blocked(config_path, wid):
            raise RuntimeError("B 工作区已停止；按 W 重新布防原会话")
        if guard.snapshot(config_path, wid).get("phase") != "watching":
            guard._arm(config_path, wid)
    config, _ = launch_options([native, *args])
    resume_session = None
    if "resume" in args:
        index = args.index("resume") + 1
        if index < len(args) and not args[index].startswith("-"):
            try:
                resume_session = str(uuid.UUID(args[index]))
            except ValueError:
                pass
    guard.launch(config_path, config_args=config, cli_args=args, resume_session=resume_session)


def install(config_path, source=None, *, destination=Path("/opt/homebrew/bin/codex")):
    """Atomically replace the existing symlink; retain its exact native target."""
    config_path = Path(config_path).resolve()
    source = Path(source or __file__).resolve()
    directory = config_path.parent
    metadata = directory / "codex-launcher.json"
    existing = guard.read_json(metadata)
    wrapper = directory / "codex-guard"
    if not destination.is_symlink():
        raise RuntimeError("Codex entrypoint is not the expected symlink; native executable left unchanged")
    if destination.resolve() == wrapper.resolve():
        native = existing.get("native_binary")
    else:
        native = str(destination.resolve())
    if not native or not Path(native).is_file() or Path(native) == wrapper:
        raise RuntimeError("original native Codex binary cannot be proved")
    text = "#!/bin/sh\nexec " + shlex.join([sys.executable, "-B", str(source), "--config", str(config_path),
        "--native", native, "--"]) + ' "$@"\n'
    temporary = wrapper.with_name(wrapper.name + "." + uuid.uuid4().hex)
    temporary.write_text(text)
    temporary.chmod(0o755)
    os.replace(temporary, wrapper)
    guard.write_json(metadata, {"native_binary": native, "destination": str(destination),
        "wrapper": str(wrapper), "source": str(source), "config_path": str(config_path)})
    temporary_link = destination.with_name(".codex-guard-" + uuid.uuid4().hex)
    temporary_link.symlink_to(wrapper)
    os.replace(temporary_link, destination)
    return {"native_binary": native, "destination": str(destination), "wrapper": str(wrapper)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--native", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    try:
        run(options.config, options.native, options.args[1:] if options.args[:1] == ["--"] else options.args)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

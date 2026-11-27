#!/usr/bin/env python3
"""Manage only this checkout's skill links and hook commands. Dry-run by default."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

from cmux_workflow_advisory import WORKFLOW_HOOKS

ROOT = Path(__file__).resolve().parents[1]
GUARDS = {
    "cmux_executor_closeout_guard": "PreToolUse",
    "cmux_workspace_guard": "PreToolUse",
    "cmux_agent_panel_guard": "PreToolUse",
    "cmux_handshake_receipt_guard": "PreToolUse",
    "cmux_consensus_round_guard": "PreToolUse",
    "cmux_lease_guard": "PreToolUse",
    "cmux_consensus_stop_guard": "Stop",
    "cmux_executor_idle_guard": "Stop",
    "cmux_native_delivery_guard": "PostToolUse",
    "cmux_supervisor_report_guard": "PostToolUse",
    "executor_reply_advisory": "PostToolUse",
    "callback_settlement_advisory": "PostToolUse",
    "handshake_advisory": "PostToolUse",
}
RETIRED_GUARDS = {"cmux_submit_confirmation_guard", "cmux_send_proof_stop_guard"}
OPTIONAL_GUARDS = {"cmux_executor_reask_stop_guard": "Stop"}

# --home selects the configuration being inspected, never the runtime of a
# diagnostic hook. Run each real entrypoint in its own disposable namespace.
# In particular, do not inherit a live client's CMUX/Goal/agent selectors or
# rewrite the operator's HOME to make an empty-payload probe pass.
DOCTOR_BOOTSTRAP = """
import os
from pathlib import Path
import runpy
import sys
diagnostic_root = Path(sys.argv[1]).resolve(strict=True)
Path.home = staticmethod(lambda: diagnostic_root)
os.chdir(diagnostic_root)
script, *arguments = sys.argv[2:]
sys.path.insert(0, str(Path(script).parent))
sys.argv = [script, *arguments]
runpy.run_path(script, run_name='__main__')
"""


def doctor_probe(name):
    argv = shlex.split(command(name))
    environment = {key: os.environ[key] for key in
                   ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'SYSTEMROOT')
                   if key in os.environ}
    result = dict(benignExitZero=False, probeScope='isolated-empty-payload',
                  liveInvocation=False)
    try:
        with tempfile.TemporaryDirectory(prefix='mac-doctor-') as diagnostic_root:
            run = subprocess.run(
                [sys.executable, '-I', '-B', '-c', DOCTOR_BOOTSTRAP,
                 diagnostic_root, *argv[2:]],
                input='{}', text=True, capture_output=True, timeout=15,
                cwd=diagnostic_root, env=environment)
            result.update(benignExitZero=run.returncode == 0, returncode=run.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        result['error_type'] = type(exc).__name__
    return result


def command(name):
    filename = name + '.py'
    if filename in WORKFLOW_HOOKS:
        return shlex.join([sys.executable, '-B', str(ROOT / 'scripts/cmux_workflow_advisory.py'),
                           '--hook', filename])
    return shlex.join([sys.executable, '-B', str(ROOT / 'scripts' / filename)])


def release_source(path, home):
    """Recognize only this package's exact, immutable release layout."""
    path = Path(path)
    base = Path(home) / '.local/share/multi-agent-collaboration/releases'
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        return False
    return len(parts) == 2 and parts[0] not in ('.', '..') and parts[1] == 'source'


def owned_command(value, home):
    try:
        words = shlex.split(value)
    except (ValueError, TypeError):
        return False
    while words and words[0] in ('rtk', 'proxy'):
        words.pop(0)
    if not words or not Path(words.pop(0)).name.startswith('python'):
        return False
    while words and words[0] in ('-B', '-u'):
        words.pop(0)
    if not words:
        return False
    path = Path(words.pop(0))
    if path.name == 'cmux_workflow_advisory.py':
        # Do not consume similarly named foreign wrappers, malformed options,
        # shell tails or a wrapper invoked for a hook owned by another tool.
        if len(words) != 2 or words[0] != '--hook' or words[1] not in WORKFLOW_HOOKS:
            return False
    elif words or path.stem not in set(GUARDS) | RETIRED_GUARDS | set(OPTIONAL_GUARDS):
        return False
    if (not path.is_absolute() or '..' in path.parts or path.suffix != '.py'
            or path.parent.name != 'scripts'):
        return False
    source = path.parent.parent
    aliases = [Path(home) / client / 'skills/multi-agent-collaboration'
               for client in ('.agents', '.claude', '.codex')]
    return (source == ROOT or release_source(source, home)
            or source in aliases and release_source(source.resolve(), home))


def transform(doc, uninstall=False, *, home=None, executor_reask=False):
    result = copy.deepcopy(doc)
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    home = Path(home or Path.home())
    # Remove this package's historical registrations before adding exactly one
    # current entry. A same-named hook outside our release tree is foreign.
    for event, entries in list(hooks.items()):
        if not isinstance(entries, list):
            raise ValueError("hook event must be a list")
        cleaned = []
        for entry in entries:
            old = entry.get('hooks', [])
            kept = [h for h in old if not owned_command(h.get('command'), home)]
            if len(kept) == len(old) or kept:
                entry['hooks'] = kept
                cleaned.append(entry)
        hooks[event] = cleaned
    expected = dict(GUARDS)
    if executor_reask:
        expected.update(OPTIONAL_GUARDS)
    for name, event in expected.items():
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError("hook event must be a list")
        cmd = command(name)
        if not uninstall:
            item = {"type": "command", "command": cmd}
            if name == 'cmux_executor_idle_guard':
                item['timeout'] = 75  # task-scoped empty Stop reentry waits at most60s
            entries.append({"matcher": "*", "hooks": [item]})
    return result


def read(path):
    if path.is_symlink():
        raise ValueError("refusing symlinked configuration: " + str(path))
    raw = path.read_bytes() if path.exists() else None
    doc = json.loads(raw) if raw is not None else {}
    if not isinstance(doc, dict):
        raise ValueError("configuration must be an object")
    return raw, doc


def replace(path, old, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    if (path.read_bytes() if path.exists() else None) != old:
        raise RuntimeError("configuration changed concurrently; rerun at a quiet boundary")
    if old is not None:
        backup = path.with_name(path.name + ".multi-agent-backup-" + uuid.uuid4().hex)
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(old)
            handle.flush()
            os.fsync(handle.fileno())
    raw = (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()
    fd, pending = tempfile.mkstemp(prefix=".multi-agent-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if (path.read_bytes() if path.exists() else None) != old:
            raise RuntimeError("configuration changed during write; refused replacement")
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)


def skill_snapshot(path):
    """Fingerprint the whole old directory; rename preserves every resource."""
    if path.is_symlink():
        return ('link', os.readlink(path))
    if not path.exists():
        return ('absent',)
    if not path.is_dir():
        raise RuntimeError('foreign skill installation preserved: ' + str(path))
    entries = []
    for parent, dirs, files in os.walk(path, followlinks=False):
        for name in sorted(dirs + files):
            item = Path(parent) / name
            st = item.lstat()
            value = (os.readlink(item) if item.is_symlink() else
                     hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else None)
            entries.append((str(item.relative_to(path)), st.st_ino, st.st_mode,
                            st.st_size, st.st_mtime_ns, value))
    return ('directory', sorted(entries))


def skill_plan(path, home):
    snapshot = skill_snapshot(path)
    if snapshot[0] == 'absent':
        return snapshot, False
    if snapshot[0] == 'link':
        source = path.resolve()
        if source == ROOT:
            return snapshot, True
        if release_source(source, home) and (source / 'SKILL.md').is_file():
            return snapshot, False
    elif snapshot[0] == 'directory':
        entry = path / 'SKILL.md'
        if entry.is_file() and not entry.is_symlink():
            body = entry.read_text()
            refs = [Path(p).parent for p in re.findall(r'\]\((/[^\n)]+/SKILL\.md)\)', body)
                    if release_source(Path(p).parent, home)]
            if (re.search(r'^name: multi-agent-collaboration\s*$', body, re.M)
                    and refs and len(set(refs)) == 1 and (refs[0] / 'SKILL.md').is_file()):
                return snapshot, False
    raise RuntimeError('foreign skill installation preserved: ' + str(path))


def replace_skill(path, snapshot, *, uninstall=False):
    if skill_snapshot(path) != snapshot:
        raise RuntimeError('skill changed concurrently; preserved: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if snapshot[0] != 'absent':
        backup = path.with_name(path.name + '.multi-agent-backup-' + uuid.uuid4().hex)
        path.rename(backup)
        # Catch an edit between the first comparison and rename before publishing.
        if skill_snapshot(backup) != snapshot:
            if not path.exists() and not path.is_symlink():
                backup.rename(path)
            raise RuntimeError('skill changed during migration; preserved backup')
    try:
        if not uninstall:
            path.symlink_to(ROOT, target_is_directory=True)
    except BaseException:
        if backup and not path.exists() and not path.is_symlink():
            backup.rename(path)
        raise
    return str(backup) if backup else None


def manage(home, mode, apply=False, executor_reask=False):
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10+ required; do not wire macOS Python 3.9")
    home = Path(home).expanduser().resolve()
    plans = []
    for client, filename in (("codex", "hooks.json"), ("claude", "settings.json")):
        config = home / ("." + client) / filename
        link = home / ("." + client) / "skills/multi-agent-collaboration"
        snapshot, owned = skill_plan(link, home)
        old, doc = read(config)
        new = transform(doc, mode == "uninstall", home=home,
                        executor_reask=executor_reask and client == 'claude')
        plans.append((config, link, snapshot, owned, old, doc, new))
    checks = {}
    # Check both clients before changing either one.
    for config, link, snapshot, owned, old, doc, new in plans:
        if read(config)[0] != old or skill_snapshot(link) != snapshot:
            raise RuntimeError('installation changed concurrently; rerun')
    for config, link, snapshot, owned, old, doc, new in plans:
        expected = transform(doc, home=home, executor_reask=executor_reask and config.parent.name == '.claude')
        checks[str(config)] = {"linked": owned, "configured": doc == expected,
                               "changeNeeded": doc != new}
        if mode != "doctor" and apply:
            if doc != new:
                replace(config, old, new)
            if mode == "install" and not owned:
                checks[str(config)]['skillBackup'] = replace_skill(link, snapshot)
            elif mode == "uninstall" and snapshot[0] != 'absent':
                checks[str(config)]['skillBackup'] = replace_skill(link, snapshot, uninstall=True)
    if mode == "doctor":
        for name in dict(GUARDS, **(OPTIONAL_GUARDS if executor_reask else {})):
            checks[name] = doctor_probe(name)
        ok = all(v.get("benignExitZero", v.get("linked") and v.get("configured")) for v in checks.values())
        # CURRENT-based releases have two independent launchd jobs. Checking
        # hooks alone missed a scheduler still pinned to the previous release.
        from release_jobs import check as check_jobs
        jobs = check_jobs(home)
        checks['release_jobs'] = jobs
        ok = ok and jobs['ok']
    else:
        ok = True
    return {"ok": bool(ok), "mode": mode, "applied": apply and mode != "doctor",
            "checkout": str(ROOT), "checks": checks,
            "scope": "configuration and isolated benign execution, not live client reload proof"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("install", "doctor", "uninstall"))
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--executor-reask", action="store_true", help="explicitly enable user-authorized 60-second reasks for Claude")
    args = parser.parse_args()
    try:
        result = manage(args.home, args.mode, args.apply, args.executor_reask)
        print(json.dumps(result, indent=2))
        sys.exit(0 if result["ok"] else 2)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(2)

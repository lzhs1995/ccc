#!/usr/bin/env python3
"""Retire the legacy shell/launchd Codex copier without changing TCC or sessions.

Read-only by default. Disable the two legacy launchd jobs before --apply. The
backup contains only the old helper scripts and shell startup files, never TCC.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

MARKER = "CCC_RETIRED_CODEX_COPY_LOOP"
COMMENT = "# Codex TCC keep (Documents/FDA survive brew upgrades)"
TRIGGER = '( /usr/bin/python3 "$HOME/Library/Application Support/codex-tcc-keep/grant.py" >/dev/null 2>&1 & )'
MESSAGE = "Codex automatic binary copying is retired; install a verified complete release explicitly."
STUBS = {
    "grant.py": '#!/usr/bin/env python3\n# ' + MARKER + '\nimport sys\nprint(' + repr(MESSAGE) + ', file=sys.stderr)\n',
    "update-stable.sh": '#!/bin/sh\n# ' + MARKER + '\nprintf \'%s\\n\' \'' + MESSAGE + '\' >&2\nexit 0\n',
}


def shell_without_trigger(text):
    lines = text.splitlines(keepends=True)
    remove = {i for i, line in enumerate(lines) if line.strip() == TRIGGER}
    for i in tuple(remove):
        if i > 0 and lines[i - 1].strip() == COMMENT:
            remove.add(i - 1)
    return "".join(line for i, line in enumerate(lines) if i not in remove)


def changes(user_home):
    result = []
    for name in (".zprofile", ".zshrc"):
        path = user_home / name
        if path.exists():
            old = path.read_text()
            new = shell_without_trigger(old)
            if old != new:
                result.append((path, new))
    directory = user_home / "Library/Application Support/codex-tcc-keep"
    for name, stub in STUBS.items():
        path = directory / name
        if not path.exists():
            continue
        text = path.read_text()
        if MARKER in text:
            continue
        known = ((name == "grant.py" and "def refresh_stable_copy(" in text and '".codex.new"' in text)
                 or (name == "update-stable.sh" and 'install_bin "$SRC" "$DEST"' in text
                     and 'ln -sfn "$DEST" /opt/homebrew/bin/codex' in text))
        if not known:
            raise RuntimeError(f"Unrecognized helper; preserve and inspect it: {path}")
        result.append((path, stub))
    return result


def disabled(domain, label):
    result = subprocess.run(["/bin/launchctl", "print-disabled", domain],
                            capture_output=True, text=True, timeout=10)
    return result.returncode == 0 and bool(re.search(r'"' + re.escape(label) + r'"\s*=>\s*disabled', result.stdout))


def apply_changes(planned, backup):
    """Replace whole files atomically; never open an executable for in-place writes."""
    if not planned:
        return []
    backup.mkdir(parents=True, exist_ok=False)
    backup.chmod(0o700)
    records = []
    # Back up the entire plan before the first mutation.
    for i, (path, content) in enumerate(planned):
        if path.is_symlink():
            raise RuntimeError(f"Refusing to replace a symlink: {path}")
        saved = backup / f"{i}-{path.name}"
        shutil.copy2(path, saved)
        saved.chmod(0o600)
        records.append({"path": str(path), "backup": str(saved),
                        "before_sha256": hashlib.sha256(saved.read_bytes()).hexdigest()})
    (backup / "manifest.json").write_text(json.dumps(records, indent=2) + "\n")
    for (path, content), record in zip(planned, records):
        if hashlib.sha256(path.read_bytes()).hexdigest() != record["before_sha256"]:
            raise RuntimeError(f"File changed during retirement: {path}")
        descriptor, name = tempfile.mkstemp(prefix=".ccc-retire-", dir=path.parent)
        temp = Path(name)
        try:
            with os.fdopen(descriptor, "w") as out:
                out.write(content)
                out.flush()
                os.fchmod(out.fileno(), stat.S_IMODE(path.stat().st_mode))
                os.fsync(out.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        record["after_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (backup / "manifest.json").write_text(json.dumps(records, indent=2) + "\n")
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--label", required=True, help="Exact legacy launchd label on this Mac")
    args = parser.parse_args()
    user_home = Path.home()
    planned = changes(user_home)
    domains = (f"gui/{os.getuid()}", "system")
    jobs = {domain: disabled(domain, args.label) for domain in domains}
    report = {"mode": "apply" if args.apply else "read-only", "legacy_jobs_disabled": jobs,
              "planned_files": [str(p) for p, _ in planned]}
    if args.apply:
        if not all(jobs.values()):
            raise RuntimeError("Disable and boot out the exact legacy launchd jobs before applying this migration")
        backup = args.backup or user_home / "Library/Application Support/codex-tcc-keep" / (
            "retired-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        report["changes"] = apply_changes(planned, backup)
        report["backup"] = str(backup) if planned else None
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

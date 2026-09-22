#!/usr/bin/env python3
"""Guarded quarantine expiry and a finite, explicitly requested backlog drain.

Sweeps still own candidate judgement. A drain freezes inode/mtime identities,
then intersects each bounded batch with the sweep's freshly checked candidates.
Only this module deletes sealed quarantine batches, under the sweep's mutex.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid

HOME = Path(os.environ.get("CMUX_JANITOR_HOME", Path.home()))
JD = HOME / ".config/cmux-janitor"
CM = HOME / ".cmuxterm"
Q = HOME / ".cmuxterm-janitor-quarantine"
STAGING = CM / "agent-turn-diff-baseline-snapshots-staging"
META = ".janitor-batch.json"
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
REF = re.compile(rb'"untrackedSnapshotId"\s*:\s*"([0-9a-fA-F-]{36})"')


class Refused(RuntimeError):
    pass


def utc(epoch=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if epoch is None else epoch))


def atomic_json(path, value):
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False) + "\n")
    temp.replace(path)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_json(path):
    return json.loads(path.read_text(), object_pairs_hook=unique_object)


def config():
    result = {"MODE": "dry", "USE_QUARANTINE": "1", "QUARANTINE_KEEP_HOURS": "3",
              "MAX_ITEMS_PER_RUN": "500", "SB_MIN_AGE_MIN": "10", "STAGING_MIN_AGE_MIN": "60"}
    seen = set()
    for line in (JD / "config.env").read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if not sep or key.startswith("#"):
            continue
        if key in {"QUARANTINE_RETAIN_HOURS", "METRICS_MAX_LINES"} or key in seen:
            raise Refused("ambiguous janitor configuration")
        seen.add(key)
        if key in result:
            result[key] = value.strip()
    if result["MODE"] not in {"dry", "apply"} or result["USE_QUARANTINE"] != "1":
        raise Refused("invalid mode/quarantine configuration")
    for key in ("QUARANTINE_KEEP_HOURS", "MAX_ITEMS_PER_RUN", "SB_MIN_AGE_MIN", "STAGING_MIN_AGE_MIN"):
        minimum = 0 if key.endswith("MIN_AGE_MIN") else 1
        if not result[key].isdigit() or int(result[key]) < minimum:
            raise Refused(f"invalid {key}")
        result[key] = int(result[key])
    return result


def gate():
    if any((JD / name).exists() for name in ("DISABLED", "GUARD_TRIPPED")):
        raise Refused("janitor paused or guard tripped")
    for root in (CM, STAGING, Q):
        if root.is_symlink():
            raise Refused("managed root is a symlink")


def proc_start(pid):
    return " ".join(subprocess.check_output(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                                            text=True).split())


@contextmanager
def mutex(parent_run=None):
    """Use the same lock as sweeps; never steal a live/ambiguous owner."""
    gate()
    lock = JD / ".janitor.mutex"
    token = str(uuid.uuid4())
    if parent_run:
        owner = read_json(lock / "owner")
        # The shell invokes us in a command substitution. Its process can be
        # our parent or grandparent, depending on the shell's exec optimisation.
        ancestors = {os.getppid()}
        try:
            ancestors.add(int(subprocess.check_output(
                ["/bin/ps", "-o", "ppid=", "-p", str(os.getppid())], text=True).strip()))
        except (ValueError, subprocess.CalledProcessError):
            pass
        if (owner.get("run_id") != parent_run or owner.get("pid") not in ancestors
                or owner.get("process_start") != proc_start(owner["pid"])):
            raise Refused("sweep mutex ownership changed")
        yield
        return
    try:
        lock.mkdir()
    except FileExistsError:
        raise Refused("janitor mutex busy") from None
    try:
        atomic_json(lock / "owner", {"pid": os.getpid(), "process_start": proc_start(os.getpid()), "run_id": token})
        yield
    finally:
        if read_json(lock / "owner").get("run_id") == token:
            (lock / "owner").unlink()
            lock.rmdir()


def identity(path):
    info = path.lstat()
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns]


def tree_signature(batch):
    digest = hashlib.sha256()
    blocks = 0
    for path in [batch, *sorted(batch.rglob("*"))]:
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_dev != batch.lstat().st_dev:
            raise Refused("batch contains a link, mount or special file")
        digest.update(json.dumps([str(path.relative_to(batch)), identity(path), info.st_ctime_ns]).encode())
        blocks += info.st_blocks * 512
    return digest.hexdigest(), blocks


def batch_info(batch, now):
    if batch.parent != Q or batch.name.startswith(".") or batch.is_symlink() or not batch.is_dir():
        raise Refused("unsealed batch")
    if (batch / META).is_symlink():
        raise Refused("batch metadata is a link")
    meta = read_json(batch / META)
    if not isinstance(meta, dict) or meta.get("schema_version") != 1:
        raise Refused("invalid batch schema")
    sealed = meta.get("sealed_at_epoch")
    created = meta.get("created_at_epoch")
    if (type(sealed) is not int or not 0 < sealed <= now or type(created) is not int
            or not 0 < created <= sealed or type(meta.get("item_count")) is not int):
        raise Refused("invalid batch timestamps/count")
    items = sorted(p for p in batch.iterdir() if p.name != META)
    if len(items) != meta["item_count"]:
        raise Refused("batch count changed")
    for item in items:
        if item.is_symlink() or not ((item.is_dir() and UUID.fullmatch(item.name))
                                    or (item.is_file() and ".sb-" in item.name)):
            raise Refused("unexpected batch member")
    signature, size = tree_signature(batch)
    return meta, items, signature, size


def protected_ids():
    live = CM / "agent-turn-diff-baselines.json"
    before = live.stat()
    result = set()
    tail = b""
    with live.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            data = tail + chunk
            result.update(m.group(1).decode().lower() for m in REF.finditer(data))
            tail = data[-256:]
    if before.st_size and not result:
        raise Refused("live references unverified")
    if identity(live) != [before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns]:
        raise Refused("live references changed during read")
    return result


def no_open_handles(batch):
    proc = subprocess.run(["/usr/sbin/lsof", "-nP", "+D", str(batch)],
                          capture_output=True, timeout=20)
    return proc.returncode == 1 and not proc.stdout and not proc.stderr


def scope_allows(batch, meta, items, signature, scope):
    # Existing batches are pinned before the run. New batches must belong to
    # this drain AND contain exactly the original candidate inode identities.
    if scope.get("batches", {}).get(batch.name) == signature:
        return True
    if meta.get("drain_id") != scope.get("drain_id"):
        return False
    records = {Path(r["path"]).name: r["identity"] for r in scope["candidates"]}
    return bool(items) and all(records.get(p.name) == identity(p) for p in items)


def inventory(now, keep):
    batches = 0
    sizes = 0
    oldest = None
    if Q.exists():
        for batch in Q.iterdir():
            if batch.is_symlink() or not batch.is_dir() or batch.name.startswith("."):
                continue
            batches += 1
            try:
                meta, _, _, size = batch_info(batch, now)
                sizes += size
                oldest = min(oldest or meta["sealed_at_epoch"], meta["sealed_at_epoch"])
            except (ValueError, OSError, Refused):
                return {"batch_count": batches, "bytes": {"value": None, "precision": "unknown"}, "keep_hours": keep}
    return {"batch_count": batches, "bytes": {"value": sizes, "precision": "exact"}, "keep_hours": keep,
            "oldest_sealed_at": utc(oldest) if oldest else None,
            "next_expiry_at": utc(oldest + keep * 3600) if oldest else None}


def expire(*, parent_run=None, immediate_manifest=None, preview=False):
    settings = config()
    now = time.time()
    scope = read_json(Path(immediate_manifest)) if immediate_manifest else None
    counts = dict(expired_batches=0, expired_items=0, expired_bytes=0,
                  would_expire_batches=0, would_expire_items=0, skipped_batches=0)
    def log(message):
        if not preview:
            with (JD / "janitor.log").open("a") as handle:
                handle.write(f"{utc()}  {message}\n")

    with (nullcontext() if preview else mutex(parent_run)):
        gate()
        for batch in sorted(Q.iterdir()) if Q.exists() else []:
            try:
                meta, items, signature, size = batch_info(batch, now)
                due = meta["sealed_at_epoch"] <= now - settings["QUARANTINE_KEEP_HOURS"] * 3600
                scoped = scope is not None and scope_allows(batch, meta, items, signature, scope)
                if not due and not scoped:
                    continue
                if any(p.is_dir() for p in items):
                    protected = protected_ids()
                    if any(p.name.lower() in protected for p in items):
                        raise Refused("batch is referenced by live store")
                if not no_open_handles(batch) or tree_signature(batch)[0] != signature:
                    raise Refused("batch open or changing")
                gate()
                if preview or settings["MODE"] != "apply":
                    counts["would_expire_batches"] += 1
                    counts["would_expire_items"] += len(items)
                    counts["expired_bytes"] += size
                else:
                    shutil.rmtree(batch)
                    counts["expired_batches"] += 1
                    counts["expired_items"] += len(items)
                    counts["expired_bytes"] += size
                    log(f"EXPIRED batch={batch.name} items={len(items)} bytes={size}")
            except (ValueError, OSError, Refused, subprocess.TimeoutExpired) as exc:
                counts["skipped_batches"] += 1
                marker = "SKIP-NO-BATCH-META" if not (batch / META).exists() else "SKIP-BATCH"
                log(f"{marker} {batch.name} ({type(exc).__name__}: {exc})")
        result = {"schema_version": 1, "observed_at": utc(), **counts,
                  "quarantine": inventory(time.time(), settings["QUARANTINE_KEEP_HOURS"])}
        if not preview:
            atomic_json(JD / "expiry-state.json", result)
    return result


def capture():
    settings = config()
    if settings["MODE"] != "apply":
        raise Refused("drain requires MODE=apply")
    now = time.time()
    records = []
    with mutex():
        for root, age in ((CM, settings["SB_MIN_AGE_MIN"]), (STAGING, settings["STAGING_MIN_AGE_MIN"])):
            for path in root.iterdir() if root.exists() else []:
                if path.is_symlink() or any(c in str(path) for c in "\n\r\t"):
                    continue
                eligible = (root == CM and path.is_file() and ".sb-" in path.name) or (
                    root == STAGING and path.is_dir() and UUID.fullmatch(path.name))
                if eligible and path.stat().st_mtime < now - age * 60:
                    records.append({"path": str(path), "identity": identity(path)})
        records.sort(key=lambda r: (r["identity"][-1], r["path"]))
        batches = {}
        for batch in Q.iterdir() if Q.exists() else []:
            try:
                batches[batch.name] = batch_info(batch, now)[2]
            except (ValueError, OSError, Refused):
                pass
        return {"schema_version": 1, "drain_id": str(uuid.uuid4()), "captured_at": utc(),
                "candidates": records, "batches": batches}


def select(manifest):
    scope = read_json(Path(manifest))
    records = {r["path"]: r["identity"] for r in scope["candidates"]}
    for line in sys.stdin:
        stamp, sep, name = line.rstrip("\n").partition("\t")
        try:
            if sep and records.get(name) == identity(Path(name)):
                print(line, end="")
        except FileNotFoundError:
            pass


def drain(purge_now=False):
    lock = JD / ".drain.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise Refused("drain already running or awaiting recovery") from None
    state = {"schema_version": 1, "phase": "starting", "observed_at": utc(), "pid": os.getpid(),
             "processed": 0, "disposed": 0, "expired_bytes": 0, "expired_items": 0}
    try:
        scope = capture()
        state.update(drain_id=scope["drain_id"], total=len(scope["candidates"]), phase="running")
        directory = JD / "drains" / scope["drain_id"]
        directory.mkdir(parents=True)
        manifest = directory / "manifest.json"
        atomic_json(manifest, scope)
        atomic_json(JD / "drain-state.json", state)
        if purge_now:
            result = expire(immediate_manifest=manifest)
            for key in ("expired_bytes", "expired_items"):
                state[key] += result[key]
        limit = min(config()["MAX_ITEMS_PER_RUN"], 500)
        for offset in range(0, len(scope["candidates"]), limit):
            gate()
            chunk = {**scope, "batches": {}, "candidates": scope["candidates"][offset:offset + limit],
                     "purge_now": purge_now}
            chunk_path = directory / f"batch-{offset:06d}.json"
            atomic_json(chunk_path, chunk)
            command = ["/bin/bash", str(JD / "cmux-janitor.sh"), "--manual", "--drain-manifest", str(chunk_path)]
            proc = subprocess.run(command, env={**os.environ, "HOME": str(HOME)})
            latest = read_json(JD / "janitor-state.json")
            if proc.returncode or latest.get("drain_manifest") != str(chunk_path):
                raise Refused("sweep did not complete this drain batch")
            state["processed"] += len(chunk["candidates"])
            state["disposed"] += latest["counts"]["disposed"]
            state["expired_bytes"] += latest["expired_bytes"]["value"] or 0
            state["expired_items"] += latest["counts"]["expired_items"]
            state["observed_at"] = utc()
            atomic_json(JD / "drain-state.json", state)
        state["phase"] = "complete"
    except Exception as exc:
        state.update(phase="error", error=str(exc))
        raise
    finally:
        state["observed_at"] = utc()
        atomic_json(JD / "drain-state.json", state)
        lock.rmdir()
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("expire")
    exp.add_argument("--parent-run")
    exp.add_argument("--immediate-manifest")
    exp.add_argument("--preview", action="store_true")
    exp.add_argument("--shell", action="store_true")
    sel = sub.add_parser("select")
    sel.add_argument("manifest")
    run = sub.add_parser("drain")
    run.add_argument("--purge-now", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "select":
            select(args.manifest)
            return 0
        if args.command == "drain":
            result = drain(args.purge_now)
        else:
            result = expire(parent_run=args.parent_run, immediate_manifest=args.immediate_manifest, preview=args.preview)
            if args.shell:
                print("\t".join(str(result[key]) for key in (
                    "would_expire_batches", "would_expire_items", "expired_batches", "expired_items", "expired_bytes")))
                return 0
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, Refused, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

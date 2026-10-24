#!/usr/bin/env python3
"""Durable cross-workspace resource queue; leases never expire into ownership."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid


class ResourceBusy(RuntimeError):
    pass


@contextmanager
def os_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise ResourceBusy("OS_LOCK_BUSY: " + str(path)) from e
        try:
            yield f
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def group_alive(pgid):
    if not pgid:
        return False
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Broker:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("MULTI_AGENT_RESOURCE_ROOT", Path.home() / ".local/state/multi-agent-collaboration/resources"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "queue.sqlite3", timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS resources(name TEXT PRIMARY KEY, external_locks TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            workspace_uuid TEXT NOT NULL, surface_uuid TEXT NOT NULL, resource TEXT NOT NULL,
            status TEXT NOT NULL, requested REAL NOT NULL, token TEXT, expires REAL, pgid INTEGER,
            release_proof TEXT);
          CREATE UNIQUE INDEX IF NOT EXISTS one_active_resource ON tickets(resource) WHERE status='ACTIVE';
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(resources)")}
        if "release_requirements" not in columns:
            self.db.execute("ALTER TABLE resources ADD COLUMN release_requirements TEXT NOT NULL DEFAULT '{}'")
        ticket_columns = {r[1] for r in self.db.execute("PRAGMA table_info(tickets)")}
        if "invocations" not in ticket_columns:
            self.db.execute("ALTER TABLE tickets ADD COLUMN invocations INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def configure(self, resource, external_locks=(), release_requirements=None):
        paths = [str(Path(p).expanduser().resolve()) for p in external_locks]
        if release_requirements is None:
            release_requirements = ({"documents": 0, "windows": 0, "modal": False,
                                     "zotero_current_doc": False, "zotero_current_window": False, "pending": False}
                                    if resource == "word-zotero" else {})
        with self.transaction():
            existing = self.db.execute("SELECT * FROM resources WHERE name=?", (resource,)).fetchone()
            if existing and (json.loads(existing["external_locks"]) != paths or json.loads(existing["release_requirements"]) != release_requirements):
                raise ResourceBusy("RESOURCE_LOCK_MAPPING_IS_PINNED")
            self.db.execute("INSERT OR IGNORE INTO resources(name,external_locks,release_requirements) VALUES(?,?,?)", (resource, json.dumps(paths), json.dumps(release_requirements)))

    def lock_path(self, resource):
        return self.root / (hashlib.sha256(resource.encode()).hexdigest() + ".lock")

    def _external_free(self, resource):
        row = self.db.execute("SELECT * FROM resources WHERE name=?", (resource,)).fetchone()
        if not row:
            raise ValueError("RESOURCE_NOT_CONFIGURED")
        for path in json.loads(row["external_locks"]):
            with os_lock(path):
                pass

    def request(self, resource, task_id, workspace_uuid, surface_uuid):
        if not task_id.strip():
            raise ValueError("TASK_ID_REQUIRED")
        uuid.UUID(workspace_uuid)
        uuid.UUID(surface_uuid)
        with self.transaction():
            if not self.db.execute("SELECT name FROM resources WHERE name=?", (resource,)).fetchone():
                raise ValueError("RESOURCE_NOT_CONFIGURED")
            old = self.db.execute("SELECT * FROM tickets WHERE resource=? AND task_id=? AND workspace_uuid=? AND surface_uuid=? AND status IN ('WAITING','ACTIVE')", (resource, task_id, workspace_uuid, surface_uuid)).fetchone()
            if old:
                return dict(old)
            ticket = str(uuid.uuid4())
            self.db.execute("INSERT INTO tickets(id,task_id,workspace_uuid,surface_uuid,resource,status,requested) VALUES(?,?,?,?,?,'WAITING',?)", (ticket, task_id, workspace_uuid, surface_uuid, resource, time.time()))
            return self.get(ticket)

    def get(self, ticket):
        row = self.db.execute("SELECT * FROM tickets WHERE id=?", (ticket,)).fetchone()
        if not row:
            raise ValueError("TICKET_NOT_FOUND")
        return dict(row)

    def grant(self, ticket, ttl=600):
        if not 1 <= ttl <= 86400:
            raise ValueError("INVALID_LEASE_TTL")
        with self.transaction():
            row = self.get(ticket)
            active = self.db.execute("SELECT * FROM tickets WHERE resource=? AND status='ACTIVE'", (row["resource"],)).fetchone()
            if active:
                reason = "EXPIRED_LEASE_NEEDS_RECONCILIATION" if active["expires"] <= time.time() else "RESOURCE_LEASE_BUSY"
                raise ResourceBusy(reason)
            first = self.db.execute("SELECT id FROM tickets WHERE resource=? AND status='WAITING' ORDER BY requested,id LIMIT 1", (row["resource"],)).fetchone()
            if not first or first["id"] != ticket:
                raise ResourceBusy("WAIT_YOUR_TURN")
            with os_lock(self.lock_path(row["resource"])):
                self._external_free(row["resource"])
                self.db.execute("UPDATE tickets SET status='ACTIVE', token=?, expires=? WHERE id=?", (str(uuid.uuid4()), time.time() + ttl, ticket))
            return self.get(ticket)

    def require(self, ticket, token, allow_expired=False):
        row = self.get(ticket)
        if row["status"] != "ACTIVE" or not token or row["token"] != token:
            raise ResourceBusy("LEASE_OWNER_MISMATCH")
        if not allow_expired and row["expires"] <= time.time():
            raise ResourceBusy("EXPIRED_LEASE_NEEDS_RECONCILIATION")
        return row

    def withdraw(self, ticket, task_id, workspace_uuid, surface_uuid):
        """Only the exact owner may remove an abandoned waiting ticket."""
        with self.transaction():
            row = self.get(ticket)
            if row["status"] != "WAITING" or any(row[k] != v for k, v in (
                    ("task_id", task_id), ("workspace_uuid", workspace_uuid), ("surface_uuid", surface_uuid))):
                raise ResourceBusy("WAITING_TICKET_OWNER_REQUIRED")
            self.db.execute("UPDATE tickets SET status='WITHDRAWN' WHERE id=?", (ticket,))
        return self.get(ticket)

    def run(self, ticket, token, command, **kwargs):
        row = self.require(ticket, token)
        if not command or not isinstance(command, list):
            raise ValueError("ARGV_REQUIRED")
        with os_lock(self.lock_path(row["resource"])):
            self.require(ticket, token)
            self._external_free(row["resource"])
            proc = subprocess.Popen(command, start_new_session=True, **kwargs)
            self.db.execute("UPDATE tickets SET pgid=?,invocations=invocations+1 WHERE id=?", (proc.pid, ticket))
            try:
                stdout, stderr = proc.communicate()
            except BaseException:
                # Preserve the lease and PGID; never kill another application's jobs.
                raise
            empty = not group_alive(proc.pid)
            if empty:
                self.db.execute("UPDATE tickets SET pgid=NULL WHERE id=?", (ticket,))
            return {"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr,
                    "process_group_empty": empty, "pgid": proc.pid}

    def release(self, ticket, token, proof, reconcile=False):
        with self.transaction():
            row = self.require(ticket, token, allow_expired=True)
            if row["expires"] <= time.time() and not reconcile:
                raise ResourceBusy("EXPIRED_LEASE_NEEDS_RECONCILIATION")
            if proof.get("pending") is not False or proof.get("own_processes_empty") is not True or group_alive(row["pgid"]):
                raise ResourceBusy("DRAIN_REQUIRED")
            requirements = json.loads(self.db.execute("SELECT release_requirements FROM resources WHERE name=?", (row["resource"],)).fetchone()[0])
            if requirements:
                observed = proof.get("observed_receipt", {})
                p = Path(observed.get("path", ""))
                if not p.is_absolute() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != observed.get("sha256"):
                    raise ResourceBusy("NATIVE_RELEASE_RECEIPT_REQUIRED")
                raw = json.loads(p.read_text())
                if raw.get("task_id") != row["task_id"]:
                    raise ResourceBusy("RELEASE_RECEIPT_TASK_MISMATCH")
                for key, expected in requirements.items():
                    if type(raw.get(key)) is not type(expected) or raw[key] != expected:
                        raise ResourceBusy("NATIVE_RELEASE_CONDITION_FAILED: " + key)
            if row["resource"].startswith("nlm-account:"):
                if proof.get("in_flight_requests") != []:
                    raise ResourceBusy("NLM_REMOTE_DRAIN_REQUIRED")
                no_request = proof.get("no_request_submitted") is True and row["invocations"] == 0
                if not no_request:
                    observed = proof.get("terminal_receipt") or {}
                    p = Path(observed.get("path", ""))
                    if not p.is_absolute() or not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != observed.get("sha256"):
                        raise ResourceBusy("NLM_TERMINAL_RECEIPT_REQUIRED")
                    raw = json.loads(p.read_text())
                    recorded_lease = raw.get("resource_lease") or {}
                    if (raw.get("remote_terminal") is not True or raw.get("process_group_empty") is not True
                            or raw.get("status") not in {"COMPLETE", "FAILED"} or not raw.get("request_id")
                            or recorded_lease.get("id") != ticket or recorded_lease.get("token") != token):
                        raise ResourceBusy("NLM_REMOTE_TERMINAL_NOT_PROVEN")
            with os_lock(self.lock_path(row["resource"])):
                self._external_free(row["resource"])
                checked = {**proof, "external_locks_probed_and_released": True,
                           "documents_verified": bool(requirements) and requirements.get("documents") == 0,
                           "released_at": time.time(), "reconciled": reconcile}
                self.db.execute("UPDATE tickets SET status='RELEASED', release_proof=? WHERE id=?", (json.dumps(checked), ticket))
            return {**self.get(ticket), "release_proof": checked}

    def status(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM tickets ORDER BY requested")]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["configure", "request", "grant", "status", "run", "release", "withdraw"])
    p.add_argument("--root")
    p.add_argument("--resource")
    p.add_argument("--external-lock", action="append", default=[])
    p.add_argument("--task-id")
    p.add_argument("--workspace-uuid")
    p.add_argument("--surface-uuid")
    p.add_argument("--ticket")
    p.add_argument("--token")
    p.add_argument("--ttl", type=int, default=600)
    p.add_argument("--proof")
    p.add_argument("--reconcile", action="store_true")
    args, argv = p.parse_known_args()
    if argv and args.command != "run":
        p.error("unexpected arguments: " + " ".join(argv))
    b = Broker(args.root)
    try:
        if args.command == "configure":
            b.configure(args.resource, args.external_lock)
            result = {"configured": args.resource}
        elif args.command == "request":
            result = b.request(args.resource, args.task_id, args.workspace_uuid, args.surface_uuid)
        elif args.command == "grant":
            result = b.grant(args.ticket, args.ttl)
        elif args.command == "release":
            result = b.release(args.ticket, args.token, json.loads(Path(args.proof).read_text()), args.reconcile)
        elif args.command == "withdraw":
            result = b.withdraw(args.ticket, args.task_id, args.workspace_uuid, args.surface_uuid)
        elif args.command == "run":
            result = b.run(args.ticket, args.token, argv[1:] if argv[:1] == ["--"] else argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        else:
            result = b.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result.get("exit_code", 0) if isinstance(result, dict) else 0
    except (ResourceBusy, ValueError, OSError) as e:
        print(json.dumps({"status": "BLOCKED", "reason": str(e)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

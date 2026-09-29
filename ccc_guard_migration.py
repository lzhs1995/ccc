"""One-time, original-surface/original-session adoption of genuine B pools."""
from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import sys
import time
import uuid

import ccc_batch_guard as guard
import ccc_guard_scope as scope
from ccc_codex_queue import process_writable_files


class PreflightPreservationError(RuntimeError):
    """No replacement is safe; the original process may own the only history."""


def establish_provenance(config_path, workspace_id=None):
    """Upgrade only rules with an actual matching B job, never name matches."""
    store = guard.core().ConfigStore(Path(config_path))
    def change(config):
        changed = []
        for rule in config.get("workspace_rules", []):
            if workspace_id and guard.uid(rule["workspace_id"]) != guard.uid(workspace_id):
                continue
            if guard.provenance(config_path, rule["workspace_id"], config):
                continue
            jid = rule.get("last_batch_id")
            try:
                uuid.UUID(jid)
                job = guard.read_json(Path(config_path).parent / "workspace-batches" / jid / "job.json")
                if not guard.valid_job(job, jid, rule["workspace_id"]):
                    continue
            except (OSError, ValueError, TypeError, AttributeError):
                continue
            rule["batch_guard"] = {"version": guard.VERSION, "origin_job_id": jid}
            changed.append(rule["workspace_id"])
        return changed
    return store.mutate(change)[1]


def launch_options(argv):
    """Keep native settings and UI flags, omit all original prompt operands."""
    config, frontend = [], []
    options = {"-m": "model", "--model": "model", "-s": "sandbox_mode", "--sandbox": "sandbox_mode",
               "-a": "approval_policy", "--ask-for-approval": "approval_policy",
               "-p": "profile", "--profile": "profile", "--local-provider": "model_provider"}
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in {"exec", "e", "app-server", "review", "fork"}:
            raise RuntimeError("non-interactive/native server requires explicit original-session binding")
        if arg in {"resume", "--last", "--all"}:
            i += 1
            continue
        if arg in {"-c", "--config"}:
            config += ["-c", argv[i + 1]]
            i += 2
            continue
        if arg.startswith("--config="):
            config += ["-c", arg.split("=", 1)[1]]
        elif arg in options:
            config += ["-c", options[arg] + "=" + json.dumps(argv[i + 1])]
            i += 1
        elif arg in {"--enable", "--disable"}:
            config += ["-c", "features." + argv[i + 1] + "=" + ("true" if arg == "--enable" else "false")]
            i += 1
        elif arg == "--dangerously-bypass-approvals-and-sandbox":
            config += ["-c", 'approval_policy="never"', "-c", 'sandbox_mode="danger-full-access"']
        elif arg == "--full-auto":
            config += ["-c", 'approval_policy="on-request"', "-c", 'sandbox_mode="workspace-write"']
        elif arg == "--search":
            config += ["-c", 'web_search="live"']
        elif arg in {"--no-alt-screen"}:
            frontend.append(arg)
        elif arg in {"-C", "--cd", "--remote", "-i", "--image"}:
            # cwd is read from the original live process. Images/prompts are
            # already in its original transcript and must never be submitted twice.
            i += 1
        elif arg.startswith("-"):
            raise RuntimeError("unsupported original Codex launch option: " + arg.split("=", 1)[0])
        i += 1
    return config, frontend


def original_session(record, config_path):
    files = process_writable_files(record["pid"], identities=True)
    rollouts = [p for p in files if p.name.startswith("rollout-") and p.suffix == ".jsonl"]
    if len(rollouts) == 1:
        path = rollouts[0]
        from ccc_codex_goal import linked
        if not linked(path, files[path]):
            raise RuntimeError("original rollout is unlinked or replaced; migration cannot preserve its history")
        with path.open() as handle:
            first = json.loads(handle.readline())
            opened = os.fstat(handle.fileno())
        if (opened.st_dev != files[path]["device"] or opened.st_ino != files[path]["inode"]
                or not linked(path, files[path])):
            raise RuntimeError("original rollout changed while its identity was read")
        if first.get("type") != "session_meta":
            raise RuntimeError("original rollout identity is missing")
        sid = first.get("payload", {}).get("id")
        uuid.UUID(sid)
        return sid, str(path)
    # A remote TUI has no rollout descriptor. Its exact frontend generation
    # must match the guardian's persisted native-session binding instead.
    if record.get("remote"):
        bound = guard.binding(config_path, record, verify=False)
        if (bound and bound.get("frontend_pid") == record["pid"]
                and bound.get("frontend_birth") == record["birth"] and bound.get("session_id")):
            return bound["session_id"], bound.get("transcript")
    raise RuntimeError("cannot prove a single original native session; migration did not replace it")


def composer_draft(grid):
    status, row = guard.core()._composer_status(grid)
    if status == "empty":
        return ""
    if status != "composer_busy" or row is None:
        raise RuntimeError("original draft is not fully visible; original terminal preserved")
    # Only a complete single visible line can be losslessly reconstructed
    # from cells. Multiline/attachment/hidden drafts stay in the original TUI.
    spans = sorted((s for s in grid.spans if s.row == row and s.column + s.cell_width > 2), key=lambda s: s.column)
    draft, end = "", 2
    for span in spans:
        if guard.core()._is_spinner_overlay_span(grid, span):
            continue
        text = span.text[max(0, 2 - span.column):]
        if grid.style(span.style_id).get("faint"):
            continue
        start = max(2, span.column)
        if start > end:
            draft += " " * (start - end)
        draft += text
        end = span.column + span.cell_width
    draft = draft.rstrip()
    if (not draft or "[Pasted" in draft or "[Image" in draft
            or grid.lines[row][grid.cursor.column:].strip()):
        raise RuntimeError("original draft cannot be reproduced exactly; original terminal preserved")
    return draft


def capture(config_path, process, target, client):
    record = scope.process(process["pid"], launch=True)
    if not record or record["birth"] != process["birth"]:
        raise RuntimeError("original process changed during migration planning")
    record.update(workspace_id=target["workspace_id"], window_id=target["window_id"])
    sid, transcript = original_session(record, config_path)
    viewport = client.replay(target["workspace_id"], target["surface_id"])
    grid = guard.core().Grid.from_rpc(viewport, target["surface_id"])
    draft = composer_draft(grid)
    if not scope.matches(record):
        raise RuntimeError("original process changed while its draft was captured")
    if record.get("remote"):
        prior = guard.read_json(guard.pool_dir(config_path, target["workspace_id"]) / (target["surface_id"] + ".launch.json"))
        config_args, frontend_args = prior["config_args"], prior.get("frontend_args", [])
        record["environment"] = prior["environment"]
    else:
        config_args, frontend_args = launch_options(record["argv"])
    stat = Path(transcript).stat() if transcript else None
    return {"version": 1, "workspace_id": target["workspace_id"], "surface_id": target["surface_id"],
        "window_id": target["window_id"], "original": {k: record[k] for k in
            ("pid", "birth", "surface_id", "environment_workspace_id")},
        "resume_session": sid, "transcript": transcript,
        "history_before": {"bytes": stat.st_size, "device": stat.st_dev, "inode": stat.st_ino} if stat else None,
        "cwd": record["cwd"], "environment": record["environment"], "config_args": config_args,
        "frontend_args": frontend_args, "draft": draft, "viewport": viewport,
        "captured_at": time.time(), "phase": "captured"}


def cmux_client(config_path):
    config = guard.core().ConfigStore(Path(config_path)).load()
    transport = guard.core().CmuxViewportSocket()
    client = guard.core().CmuxClient(config.get("cmux_path", guard.core().DEFAULT_CMUX), viewport_socket=transport)
    transport.configure(client.capabilities())
    return client


def adopt_workspace(config_path, workspace_id, *, client=None):
    """Serialize adoption and guarantee a closed gate on every failure path."""
    with contextlib.ExitStack() as locks:
        preflight = {"complete": False}
        authorized = False
        try:
            wid = guard.uid(workspace_id)
            if not guard.provenance(config_path, wid):
                raise PreflightPreservationError("workspace does not have a genuine B batch record")
            authorized = True
            guard.private_directory(guard.guard_root(config_path))
            guard.private_directory(guard.pool_dir(config_path, wid))
            locks.enter_context(guard.core().FileLock(
                guard.pool_dir(config_path, wid) / "migration.lock", timeout_sec=180))
            return _adopt_workspace(config_path, wid, client=client, preflight=preflight)
        except Exception as exc:
            if not preflight["complete"] or isinstance(exc, PreflightPreservationError):
                # Discovery, identity and persistence failures all precede the
                # permission to stop. Even a failed fence write must retain
                # this exception type so the caller cannot stop uncaptured
                # original processes as a generic setup-failure fallback.
                detail = str(exc)
                if not authorized:
                    # No B scope was established; even a pause marker would
                    # interfere with an ordinary workspace.
                    raise PreflightPreservationError(detail) from exc
                try:
                    guard.write_json(guard.pool_dir(config_path, wid) / "STOP.json", {
                        "reason": "session_preservation_failed", "connected": False,
                        "error": detail, "at": time.time()})
                except Exception as fence_error:
                    detail += "; stop marker failed: " + str(fence_error)
                try:
                    guard.core().ConfigStore(Path(config_path)).mutate(lambda config:
                        guard.core().workspace_rule_by_id(config, wid).update(
                            paused=True, pause_origin="guard_migration", batch_cancelled_at=time.time()))
                except Exception as fence_error:
                    detail += "; authorization fence failed: " + str(fence_error)
                raise PreflightPreservationError(detail) from exc
            guard.write_json(guard.pool_dir(config_path, wid) / "STOP.json", {
                "reason": "protection_setup_failed", "connected": False, "at": time.time()})
            with contextlib.suppress(Exception):
                guard.pause(config_path, wid, reason="protection_setup_failed")
            raise


def _adopt_workspace(config_path, workspace_id, *, client=None, preflight=None):
    """Gate, capture, stop, then resume each original session without a prompt."""
    wid = guard.uid(workspace_id)
    if not guard.provenance(config_path, wid):
        raise RuntimeError("workspace does not have a genuine B batch record")
    client = client or cmux_client(config_path)
    records = scope.records(client.tree())
    # Inspect an existing service without starting protection around legacy
    # sessions whose original history has not passed preflight yet.
    try:
        status = guard.request(config_path, "status", timeout=.3, workspace_id=wid)
    except (OSError, ValueError, RuntimeError):
        status = {}
    known = status.get("surfaces", {})
    candidates = []
    for row in scope.scan():
        if records.get(row["surface_id"], {}).get("workspace_id") != wid:
            continue
        current = known.get(row["surface_id"], {})
        bound = guard.binding(config_path, {"workspace_id": wid, "surface_id": row["surface_id"]}, verify=False)
        if ((current.get("pid") == row["pid"] and current.get("birth") == row["birth"])
                or (current and row.get("remote") and bound and bound.get("frontend_pid") == row["pid"])):
            continue
        candidates.append(row)
    if not candidates:
        if preflight is not None:
            preflight["complete"] = True
        guard.ensure_service(config_path)
        return {"workspace_id": wid, "adopted": 0}
    groups = {}
    for row in candidates:
        groups.setdefault(row["surface_id"], []).append(row)
    candidates = []
    for sid, rows in groups.items():
        if len(rows) == 1:
            candidates.extend(rows)
            continue
        bound = guard.binding(config_path, {"workspace_id": wid, "surface_id": sid}, verify=False)
        fronts = [r for r in rows if r.get("remote") and bound and r["pid"] == bound.get("frontend_pid")
                  and r["birth"] == bound.get("frontend_birth")]
        backends = [r for r in rows if r.get("backend") and bound and r["pid"] == bound.get("pid")
                    and r["birth"] == bound.get("birth")]
        if len(rows) == 2 and len(fronts) == len(backends) == 1:
            candidates.append(fronts[0])  # The stop path covers the orphan backend too.
        else:
            raise PreflightPreservationError("multiple independent Codex processes share a surface; original sessions preserved")
    token = uuid.uuid4().hex
    directory = guard.pool_dir(config_path, wid) / "migrations" / token
    guard.private_directory(directory)
    store = guard.core().ConfigStore(Path(config_path))
    before = dict(guard.core().workspace_rule_by_id(store.load(), wid))
    guard.write_json(directory / "workspace-before.json", before)
    def close_gate(config):
        rule = guard.core().workspace_rule_by_id(config, wid)
        rule.update(paused=True, pause_origin="guard_migration", guard_migration=token)
    store.mutate(close_gate)
    guard.write_json(guard.pool_dir(config_path, wid) / "STOP.json", {
        "reason": "session_migration", "connected": False, "migration": token})
    plan, failures = [], []
    with guard.core().workspace_input_lock(config_path, wid):
        def prepare(row):
            value = capture(config_path, row, records[row["surface_id"]], client)
            path = directory / (row["surface_id"] + ".json")
            guard.write_json(path, value)
            return value, path
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(prepare, row): row for row in candidates}
            for future in as_completed(futures):
                try:
                    plan.append(future.result())
                except Exception as exc:
                    failures.append({"surface_id": futures[future]["surface_id"], "error": str(exc)})
        if failures:
            # No process has been replaced; drafts and identities remain intact.
            guard.write_json(directory / "result.json", {"phase": "blocked", "failures": failures})
            raise PreflightPreservationError("B migration preflight could not preserve every original session: " + str(failures))
        if preflight is not None:
            preflight["complete"] = True
        guard.ensure_service(config_path)
        guard.request(config_path, "prepare", timeout=3, workspace_id=wid)
        for value, path in plan:
            value["phase"] = "interrupting"
            guard.write_json(path, value)
        # Migration cancellation uses the same scoped stop path as first success.
        result = guard.request(config_path, "stop", timeout=3, workspace_id=wid, reason="session_migration")
        if result.get("phase") != "stopped":
            raise RuntimeError("original native processes have not all stopped; no replacement launched")
        guard.request(config_path, "prepare", workspace_id=wid)
        def replace(item):
            value, path = item
            current = scope.records(client.tree()).get(value["surface_id"])
            if not current or current["workspace_id"] != wid:
                raise RuntimeError("surface moved before original-session resume")
            if scope.birth(value["original"]["pid"], codex=True) == value["original"]["birth"]:
                raise RuntimeError("original native process still exists")
            value.update(phase="resuming", respawn_attempt_at=time.time())
            guard.write_json(path, value)
            command = shlex.join([sys.executable, "-B", str(Path(guard.__file__).resolve()), "launch",
                "--config", str(config_path), "--record", str(path)])
            try:
                client.respawn_surface(current["window_id"], value["surface_id"], command, workspace_id=wid)
            except Exception as exc:
                value["respawn_error"] = str(exc)  # Reconcile; never replay an uncertain respawn.
                guard.write_json(path, value)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                bound = guard.binding(config_path, value)
                if bound and bound.get("session_id") == value["resume_session"]:
                    value.update(phase="resumed", backend_pid=bound["pid"], resumed_at=time.time())
                    if value["draft"]:
                        live = scope.records(client.tree()).get(value["surface_id"])
                        if not live or live["workspace_id"] != wid:
                            raise RuntimeError("surface moved before restoring its draft")
                        grid = guard.core().Grid.from_rpc(client.replay(wid, value["surface_id"]), value["surface_id"])
                        if guard.core()._composer_status(grid)[0] != "empty":
                            raise RuntimeError("new composer is not empty; draft saved without injecting input")
                        value["draft_restore_attempt_at"] = time.time()
                        guard.write_json(path, value)
                        client.send_text(wid, value["surface_id"], value["draft"])
                    guard.write_json(path, value)
                    return value["surface_id"], bound
                status = guard.snapshot(config_path, wid)
                if (status.get("phase") in {"stopping", "stopped", "failed"}
                        and (status.get("trip") or {}).get("reason") != "session_migration"):
                    detail = status.get("surfaces", {}).get(value["surface_id"], {}).get("error")
                    raise RuntimeError("original session resume stopped: " + str(detail or status.get("trip")))
                time.sleep(.1)
            raise RuntimeError("original session resume not confirmed; request gate remains closed")
        resumed = {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(replace, item): item for item in plan}
            for future in as_completed(futures):
                try:
                    sid, bound = future.result()
                    resumed[sid] = bound
                except Exception as exc:
                    failures.append({"surface_id": futures[future][0]["surface_id"], "error": str(exc)})
        guard.write_json(directory / "result.json", {"phase": "failed" if failures else "complete",
            "workspace_id": wid, "resumed": list(resumed), "failures": failures})
        if failures:
            raise RuntimeError("B original-session adoption is incomplete: " + str(failures))
        # Update only an already-submitted batch's process identity. The same
        # transcript/session/prompt receipt remain authoritative; nothing is sent.
        for job_file in (Path(config_path).parent / "workspace-batches").glob("*/job.json"):
            job = guard.read_json(job_file)
            if job.get("workspace_id", "").upper() != wid:
                continue
            changed = False
            for slot in job.get("slots", []):
                bound = resumed.get(slot.get("surface_id"))
                if bound and slot.get("session_id") == bound["session_id"]:
                    slot.update(pid=bound["pid"], process_start=bound["process_start"], guard_migration=token)
                    changed = True
            if changed:
                guard.write_json(job_file, job)
        def restore_rule(config):
            rule = guard.core().workspace_rule_by_id(config, wid)
            if rule.get("guard_migration") != token or rule.get("pause_origin") not in {"guard_migration", "batch_guard"}:
                raise RuntimeError("operator changed the pool while migration was running; kept paused")
            rule.update(paused=bool(before.get("paused")), guard_migration_completed=token)
            if before.get("active_batch_id"):
                rule["active_batch_id"] = before["active_batch_id"]
            if before.get("pause_origin"):
                rule["pause_origin"] = before["pause_origin"]
            else:
                rule.pop("pause_origin", None)
        store.mutate(restore_rule)
    return {"workspace_id": wid, "adopted": len(resumed), "record": str(directory / "result.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workspace")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    client = cmux_client(args.config)
    live = {r["workspace_id"] for r in scope.records(client.tree()).values()}
    config = guard.core().ConfigStore(args.config).load()
    targets = [r["workspace_id"] for r in config["workspace_rules"] if r.get("last_batch_id")
               and r["workspace_id"] in live and (not args.workspace or r["workspace_id"] == args.workspace)]
    if not args.apply:
        print(json.dumps({"candidate_workspaces": targets, "apply": False}))
        return
    for wid in targets:
        establish_provenance(args.config, wid)
        result = adopt_workspace(args.config, wid, client=client)
        if not guard.core().workspace_rule_by_id(guard.core().ConfigStore(args.config).load(), wid).get("paused"):
            result["protection"] = guard.request(args.config, "arm", timeout=90, workspace_id=wid, resume=True)
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

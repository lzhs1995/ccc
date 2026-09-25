"""Independent loss-of-guardian stop path, scoped by UUID and process birth."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import time

import ccc_batch_guard as guard
import ccc_guard_scope as scope

STALE_AFTER = .3


def current_membership(owned):
    path = owned.get("cmux_socket")
    if not isinstance(path, str) or not path.startswith("/"):
        raise RuntimeError("pre-established cmux transport missing")
    transport = guard.core().CmuxViewportSocket()
    transport.configure({"protocol": "cmux-socket", "version": 2, "access_mode": "automation",
                         "socket_path": path, "methods": ["system.tree"]})
    result = guard.core().CmuxClient(viewport_socket=transport)._control_rpc("system.tree", {"all": True}, timeout=.08)
    if not result:
        raise RuntimeError("fresh cmux membership unavailable")
    return {sid: row["workspace_id"] for sid, row in scope.records(result).items()}


def emergency(config_path, owned, *, last_heartbeat=None, membership=current_membership):
    started = time.monotonic()
    deadline = (last_heartbeat if last_heartbeat is not None else started - STALE_AFTER) + 1
    root = guard.guard_root(config_path)
    candidates = owned.get("workspaces", [r["workspace_id"] for r in owned.get("backends", [])])
    pools = {wid for wid in candidates if guard.provenance(config_path, wid)}
    rows = [{**record, "in_scope": True} for record in owned.get("backends", []) if record.get("workspace_id") in pools]
    coverage_error = None
    for wid in pools:
        guard.write_json(guard.pool_dir(config_path, wid) / "STOP.json", {
            "reason": "guardian_heartbeat_lost", "connected": False, "detected_at": time.time(),
            "generation": owned["generation"]})

    def signal_current(sig):
        nonlocal coverage_error
        try:
            locations = membership(owned)
            inventory = scope.scan()
            known = {(r["pid"], tuple(r["birth"])) for r in rows}
            for record in inventory:
                wid = locations.get(record["surface_id"])
                if (wid in pools and not record.get("remote")
                        and (record["pid"], tuple(record["birth"])) not in known):
                    rows.append({**record, "workspace_id": wid, "in_scope": True})
            coverage_error = None
        except Exception as exc:
            coverage_error = str(exc)
            for record in rows:
                record["error"] = str(exc)
            return
        for record in rows:
            record.pop("error", None)
            current = locations.get(record["surface_id"])
            record["in_scope"] = current in {None, record["workspace_id"]}
            if not record["in_scope"]:
                continue
            if scope.birth(record["pid"], codex=True) != record["birth"]:
                record["stop_proof"] = "original_process_exited"
                continue
            if scope.send(record, sig):
                record["signal"] = signal.Signals(sig).name
            else:
                record["error"] = "process identity changed; signal refused"

    signal_current(signal.SIGTERM)
    while time.monotonic() < min(deadline, started + .2):
        if all(not r["in_scope"] or scope.birth(r["pid"], codex=True) != r["birth"] for r in rows):
            break
        time.sleep(.005)
    signal_current(signal.SIGKILL)
    while time.monotonic() < deadline:
        pending = [r for r in rows if r["in_scope"] and scope.birth(r["pid"], codex=True) == r["birth"]]
        if not pending:
            break
        time.sleep(.005)
    for record in rows:
        if record["in_scope"] and scope.birth(record["pid"], codex=True) != record["birth"]:
            record["stop_proof"] = "original_process_exited"
            record.update(active=False, backend_exited=True)
    result = {"at": time.time(), "reason": "guardian_heartbeat_lost", "connected": False,
        "generation": owned["generation"], "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "coverage_error": coverage_error,
        "backends": rows}
    for wid in pools:
        targets = [r for r in rows if r["workspace_id"] == wid and r["in_scope"]]
        confirmed = not coverage_error and all(r.get("stop_proof") and not r.get("error") for r in targets)
        state = guard.snapshot(config_path, wid)
        elapsed = (time.monotonic() - (last_heartbeat if last_heartbeat is not None else started)) * 1000
        state.update(phase="stopped" if confirmed else "failed", watchdog=result,
                     coverage_error=coverage_error, stop_targets=targets,
                     trip={"connected": False, "reason": result["reason"], "target_count": len(targets),
                           "elapsed_ms": round(elapsed, 3), "within_deadline": confirmed and elapsed <= 1000})
        guard.write_json(guard.pool_dir(config_path, wid) / "state.json", state)
    guard.write_json(root / "watchdog-stop.json", result)
    return result


def run(config_path, parent, generation):
    root = guard.guard_root(config_path)
    owned = None
    last_good = time.monotonic()
    while True:
        try:
            newest = guard.read_json(root / "ownership.json")
            if newest.get("guard_pid") == parent and newest.get("generation") == generation:
                owned = newest
            heartbeat = guard.read_json(root / "heartbeat.json")
            healthy = (heartbeat.get("pid") == parent and heartbeat.get("generation") == generation
                and owned is not None and scope.birth(parent) == owned.get("guard_birth")
                and 0 <= time.monotonic() - heartbeat.get("monotonic", 0) <= STALE_AFTER)
            if healthy:
                last_good = heartbeat["monotonic"]
        except (OSError, ValueError, TypeError):
            healthy = False
        if not healthy:
            if owned:
                emergency(config_path, owned, last_heartbeat=last_good)
            return
        guard.write_json(root / "watchdog-heartbeat.json", {"pid": os.getpid(), "generation": generation,
            "monotonic": time.monotonic()})
        time.sleep(.05)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--generation", required=True)
    args = parser.parse_args()
    run(args.config, args.parent, args.generation)

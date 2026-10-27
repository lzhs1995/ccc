"""Offline helper-entrypoint fixture. Never talks to cmux or a real client."""
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import UUID

ROOT = Path(os.environ["CMUX_HELPER_FIXTURE_ROOT"])
Path.home = classmethod(lambda cls: ROOT / "state-home")


def config():
    return json.loads((ROOT / "config.json").read_text())


def event(value):
    with (ROOT / "events.jsonl").open("a") as out:
        out.write(json.dumps(value) + "\n")


def normalize(value):
    return str(UUID(value))


def whoami():
    return config()["caller"]


def list_surfaces():
    return config()["rows"]


def pin_workspace(surface):
    data = config()
    rows = [row for row in data["rows"] if row["ref"] == surface
            or normalize(row["surface_id"]) == normalize(surface)] if not surface.startswith("surface:") else [row for row in data["rows"] if row["ref"] == surface]
    if len(rows) != 1:
        raise RuntimeError("FIXTURE_TARGET_NOT_UNIQUE")
    row, caller = rows[0], data["caller"]
    if normalize(row["workspace_id"]) != normalize(caller["workspace_id"]):
        raise RuntimeError("WORKSPACE_SCOPE_DENIED")
    identity = dict(workspace_uuid=row["workspace_id"], caller_surface_uuid=caller["surface_id"],
                    target_surface_uuid=row["surface_id"], target_pane_uuid=row["pane_id"])
    identity.update(data.get("proof_override", {}))
    return identity


def mutate(kind):
    marker = ROOT / ("mutated-" + kind)
    if marker.exists():
        return
    marker.write_text("once")
    home = ROOT / "state-home/.local/state/multi-agent-collaboration/cmux-agent-adapter-v1"
    patterns = {"intent": "targets/*/intents/*.json", "pending": "targets/*/pending.json",
                "lock": "targets/*/delivery.lock", "group": "broadcasts/*.json",
                "group_lock": "broadcasts/*.lock"}
    if kind == "identity":
        data = config()
        data["caller"]["surface_id"] = "99999999-9999-4999-8999-999999999999"
        (ROOT / "config.json").write_text(json.dumps(data))
    else:
        path = next(home.glob(patterns[kind]))
        if kind in ("lock", "group_lock"):
            alternate = path.with_suffix(".replacement")
            alternate.write_text("replacement inode")
            os.replace(alternate, path)
        else:
            path.write_text(path.read_text() + " ")


def submit_text(surface, text, *, marker, force_compose, reconcile_only):
    if force_compose:
        raise AssertionError("fixture refuses force")
    event(dict(op="submit", surface=surface, payload=text, marker=marker,
               reconcile_only=reconcile_only))
    first = next((line.strip() for line in text.splitlines()
                  if line.strip() and not line.strip().startswith("[CMUX-AGENT]")), "")
    if re.match(r"^(?:TASK|TASK_PACK|TASK PACK)\s*[:=]", first, re.I):
        raise RuntimeError("TASK_PACK_REQUIRED")
    if not reconcile_only:
        event(dict(op="fixture_paste", marker=marker, surface=surface))
    data = config()
    received = data["state"] == "received" or (data["state"] == "receipt_on_reconcile" and reconcile_only)
    if received:
        directory = ROOT / "fixture-receipts"
        directory.mkdir(exist_ok=True)
        (directory / (marker + ".json")).write_text(json.dumps(dict(marker=marker, payload=text,
            surface=normalize(surface), identity=pin_workspace(surface))))
    if data.get("mutate_on_submit"):
        mutate(data["mutate_on_submit"])
    return dict(confirmed=received or data["state"] in ("fake_confirmed", "empty_compose"),
                delivery_state=data["state"])

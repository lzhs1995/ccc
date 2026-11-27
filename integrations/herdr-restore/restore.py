"""Rebuild a private, offline Herdr v3 session from one explicitly chosen archive."""

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import quote
import uuid


class Held(ValueError):
    """An exact binding could not be established."""


def require(condition, message):
    if not condition:
        raise Held(message)


def unique_keys(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "duplicate JSON key")
        value[key] = item
    return value


def read_json(path):
    return json.loads(path.read_bytes(), object_pairs_hook=unique_keys)


def public_number(n):
    require(type(n) is int and n > 0, "invalid public number")
    alphabet = "123456789ABCDEFGHJKMNPQRSTVWXYZ0"
    value = ""
    while n:
        n, index = divmod(n - 1, len(alphabet))
        value = alphabet[index] + value
    return value


def leaves(node):
    if isinstance(node, dict) and set(node) == {"Pane"}:
        require(type(node["Pane"]) is int, "invalid pane leaf")
        return [str(node["Pane"])]
    require(isinstance(node, dict) and set(node) == {"Split"}, "invalid layout node")
    split = node["Split"]
    require(split["direction"] in {"Vertical", "Horizontal"}, "unknown split direction")
    ratio = split["ratio"]
    require(type(ratio) in (int, float) and math.isfinite(ratio) and 0 < ratio < 1,
            "invalid split ratio")
    return leaves(split["first"]) + leaves(split["second"])


def check_index(value, items):
    require(type(value) is int and 0 <= value < len(items), "invalid selection")


def topology(saved):
    require(saved["version"] == 3, "unsupported snapshot version")
    spaces = saved["workspaces"]
    check_index(saved["active"], spaces)
    check_index(saved["selected"], spaces)
    seen = set()
    rows = []
    for space in spaces:
        wid = space["id"]
        require(re.fullmatch(r"w[0-9A-Z]+", wid) and wid not in seen, "duplicate or invalid workspace")
        seen.add(wid)
        tabs = space["tabs"]
        check_index(space["active_tab"], tabs)
        numbers = space["public_pane_numbers"]
        tabnums = space["public_tab_numbers"]
        require(len(tabs) == len(tabnums) == len(set(tabnums)), "ambiguous tab numbers")
        require(len(numbers) == len(set(numbers.values())), "ambiguous pane numbers")
        require(space["next_public_tab_number"] > max(tabnums) and
                space["next_public_pane_number"] > max(numbers.values()), "stale public allocation")
        all_ids = []
        for tab, tabnum in zip(tabs, tabnums):
            tid = wid + ":t" + public_number(tabnum)
            ids = leaves(tab["layout"])
            panes = tab["panes"]
            require(len(ids) == len(panes) and set(ids) == set(panes), "layout/pane mismatch")
            require(str(tab["focused"]) in panes and str(tab["root_pane"]) in panes, "invalid focus/root")
            all_ids.extend(ids)
            for internal in ids:
                rows.append((wid, tid, wid + ":p" + public_number(numbers[internal]), panes[internal]))
        require(len(all_ids) == len(numbers) and set(all_ids) == set(numbers), "invalid pane allocation")
    return rows


def verify_native(row, home):
    agent, sid, cwd = row["agent"], row["sid"], row["cwd"]
    require(str(uuid.UUID(sid)) == sid, "invalid native UUID")
    require(isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).is_dir(), "working directory missing")
    if agent == "agy":
        directory = home / ".gemini/antigravity-cli/conversations"
        path = directory / (sid + ".db")
        require(path.is_file() and not path.is_symlink(), "AGY database missing")
        # Immutable reads create no WAL/shm files in the source history. A SID
        # present only in a live WAL remains held until a checkpoint exists.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            ids = db.execute("SELECT DISTINCT cascade_id FROM trajectory_meta").fetchall()
            require(ids == [(sid,)] and db.execute("SELECT 1 FROM steps LIMIT 1").fetchone(),
                    "AGY cascade ID or steps mismatch")
        return path
    if agent == "grok":
        sessions = home / ".grok/sessions"
        encoded = quote(cwd, safe="")
        if len(encoded.encode()) <= 255:
            directory = sessions / encoded / sid
        else:
            groups = [g for g in sessions.iterdir() if not g.is_symlink()
                      and (g / ".cwd").is_file() and not (g / ".cwd").is_symlink()
                      and (g / ".cwd").read_text().rstrip("\n") == cwd and (g / sid).is_dir()]
            require(len(groups) == 1, "ambiguous or missing Grok long-cwd group")
            directory = groups[0] / sid
        require(not directory.is_symlink() and not directory.parent.is_symlink(), "unexpected session link")
        summary = read_json(directory / "summary.json")
        require(summary["info"].get("id") == sid and summary["info"].get("cwd") == cwd,
                "Grok native ID or cwd mismatch")
        path = directory / "updates.jsonl"
        require(not path.is_symlink(), "unexpected history link")
        with path.open("rb") as stream:
            for line in stream:
                event = json.loads(line)
                if event.get("method") != "session/update":
                    continue
                params = event["params"]
                require(params.get("sessionId") == sid, "Grok update references a different SID")
                update = params.get("update", {})
                if update.get("sessionUpdate") in ("user_message_chunk", "agent_message_chunk") and update.get("content"):
                    break
            else:
                raise Held("Grok resume log contains no conversation message")
        return path
    require(agent in ("codex", "claude"), "unsupported agent")
    proof = row["history"]
    path = Path(proof["path"])
    n = proof["bytes"]
    require(path.is_absolute() and not path.is_symlink() and type(n) is int and 0 < n <= 4194304,
            "invalid fleet history proof")
    with path.open("rb") as stream:
        prefix = stream.read(n)
    require(len(prefix) == n and hashlib.sha256(prefix).hexdigest() == proof["sha256"],
            "fleet history prefix changed")
    found = set()
    for line in prefix.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if agent == "codex" and event.get("type") == "session_meta":
            found.add(event["payload"]["id"])
        if agent == "claude" and event.get("sessionId"):
            found.add(event["sessionId"])
    require(found == {sid}, "original fleet SID missing")
    return path


def native_args(row):
    agent, sid = row["agent"], row["sid"]
    if agent == "codex":
        args = []
        if row.get("base_url_override"):
            option = row["base_url_override"]
            require(isinstance(option, str) and re.fullmatch(r'model_providers\.[A-Za-z0-9_-]+\.base_url="https?://[^"\r\n]+"', option),
                    "invalid saved provider setting")
            args = ["-c", option]
        return args + ["resume", sid]
    return ["--conversation" if agent == "agy" else "--resume", sid]


def private_json(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as writer:
        json.dump(value, writer, indent=2, ensure_ascii=False)
        writer.write("\n")
        writer.flush()
        os.fsync(writer.fileno())


def witness_rows(path, pin, digest, session, topology_rows):
    require(path is not None and isinstance(pin, str) and re.fullmatch("[0-9a-f]{64}", pin),
            "explicit witness file and SHA-256 required together")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        st = os.fstat(stream.fileno())
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.getuid()
                and stat.S_IMODE(st.st_mode) in (0o400, 0o600), "witness must be a private regular file")
        raw = stream.read(8388609)
    require(len(raw) <= 8388608 and hashlib.sha256(raw).hexdigest() == pin, "witness digest mismatch")
    data = json.loads(raw, object_pairs_hook=unique_keys)
    epoch = data["epoch"]
    require(data["schema"] == "ccc-herdr-native-epoch-v1" and str(uuid.UUID(epoch)) == epoch
            and path.name == epoch + ".json" and data["snapshot_sha256"] == digest and data["session"] == session
            and type(data["observed_at_ns"]) is int and data["observed_at_ns"] > 0
            and isinstance(data["panes"], list), "wrong witness epoch, file, session or snapshot")
    seats = {pane: (wid, tid, p) for wid, tid, pane, p in topology_rows}
    by_pane = {}
    terminals, processes, identities = set(), set(), set()
    for item in data["panes"]:
        pane, agent, sid = item["pane"], item["agent"], item["sid"]
        require(pane in seats and pane not in by_pane, "unknown or duplicate witness pane")
        wid, tid, p = seats[pane]
        require((session, wid, tid, p["cwd"]) == (item["session"], item["workspace"], item["tab"], item["cwd"]),
                "witness placement differs from snapshot")
        allowed_sources = {"codex": ("native_current_turn",),
                           "claude": ("native_resume_argv", "native_herdr_session_reference")}
        require(agent in allowed_sources and item["identity_source"] in allowed_sources[agent]
                and str(uuid.UUID(sid)) == sid, "unsupported native witness")
        ref = p.get("agent_session")
        require(ref is None or (ref.get("kind"), ref.get("agent"), ref.get("value"), ref.get("source")) ==
                ("id", agent, sid, "herdr:" + agent), "witness conflicts with saved native identity")
        terminal = item["terminal_id"]
        proc = item["observed_process"]
        birth = proc["birth"]
        require(isinstance(terminal, str) and re.fullmatch(r"term_[A-Za-z0-9_-]+", terminal)
                and type(proc["pid"]) is int and proc["pid"] > 0 and isinstance(birth, list) and len(birth) == 2
                and all(type(v) is int for v in birth) and birth[0] > 0 and 0 <= birth[1] < 1000000,
                "missing witness terminal or process generation")
        require(terminal not in terminals and proc["pid"] not in processes and (agent, sid) not in identities,
                "duplicate terminal, process or SID in witness")
        terminals.add(terminal)
        processes.add(proc["pid"])
        identities.add((agent, sid))
        by_pane[pane] = item
    return epoch, by_pane


def stage(snapshot, session, out, home, auto=False, fleet=None, launchers=None, witness=None, witness_sha256=None):
    require(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", session), "invalid session name")
    raw = snapshot.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if re.fullmatch(r"[0-9a-f]{64}", snapshot.stem):
        require(snapshot.stem == digest and snapshot.parent.name == session, "archive digest or session mismatch")
    saved = json.loads(raw, object_pairs_hook=unique_keys)
    rows = topology(saved)
    plan = {"version": 1, "session": session, "snapshot_sha256": digest, "home": str(home), "panes": []}
    observed = {}
    if witness is not None or witness_sha256 is not None:
        epoch, observed = witness_rows(witness, witness_sha256, digest, session, rows)
        plan.update(witness_epoch=epoch, witness_sha256=witness_sha256)
    refs = {}
    for _, _, pane, p in rows:
        ref = p.get("agent_session") or {}
        if not ref and pane in observed:
            item = observed[pane]
            ref = {"source": "herdr:" + item["agent"], "kind": "id", "agent": item["agent"], "value": item["sid"]}
        refs[pane] = ref
    identities = Counter((r.get("agent"), r.get("value")) for r in refs.values() if r.get("kind") == "id")
    for wid, tid, pane, p in rows:
        ref = refs[pane]
        row = {"workspace": wid, "tab": tid, "pane": pane, "cwd": p.get("cwd"),
               "agent": ref.get("agent"), "sid": ref.get("value")}
        # Never carry arbitrary saved shell commands or agent launch hooks into a new server.
        for key in set(p) - {"cwd", "label", "agent_name"}:
            del p[key]
        try:
            agent, sid = row["agent"], row["sid"]
            source = {"agy": "antigravity_cli", "grok": "grok", "claude": "claude", "codex": "codex"}
            require(agent in source and ref.get("kind") == "id" and ref.get("source") == "herdr:" + source[agent],
                    "no supported original binding")
            require(identities[(agent, sid)] == 1, "same native SID bound to multiple panes")
            if agent in ("codex", "claude"):
                if pane in observed:
                    entry = observed[pane]
                    row.update(witness_terminal_id=entry["terminal_id"], observed_process=entry["observed_process"])
                else:
                    entry = (fleet or {}).get("panes", {}).get(session + "#" + pane, {})
                    require((entry.get("session"), entry.get("pane_id"), entry.get("cwd"), entry.get("agent"), entry.get("sid"))
                            == (session, pane, row["cwd"], agent, sid), "matching fleet record required")
                row.update(history=entry["history"], base_url_override=entry.get("base_url_override"))
            verify_native(row, home)
            native_args(row)
            row["launcher"] = (launchers or {}).get(agent, agent)
            require(row["launcher"] == agent or Path(row["launcher"]).is_absolute(), "launcher must be an absolute path")
            row["status"] = "ready"
            if auto:
                row["ticket"] = secrets.token_hex(16)
                # v0.9.5 cold restore reads reported resume argv. launch_argv
                # survives live handoff but does not start commands after reboot.
                argv = ["env", sys.executable, "-B", str(Path(__file__).resolve()),
                        "enter", "--plan", str(out / "plan.json"), "--ticket", row["ticket"]]
                require(sum(len(s.encode()) for s in argv) <= 8192 and
                        all("'" not in s and s.isprintable() for s in argv),
                        "Herdr resume argv cannot represent this stage/program path")
                p["agent_resume"] = {"source": "ccc.restore", "agent": "ccc-restore", "argv": argv}
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
            row.update(status="held", reason=str(exc))
        plan["panes"].append(row)
    out.mkdir(mode=0o700, exist_ok=False)
    private_json(out / "plan.json", plan)
    private_json(out / "session.json", saved)
    return plan


def query(session, *args):
    # Keep the query process out of the pane's foreground process group.
    result = subprocess.run(["herdr", "--session", session, *args], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=3, check=True, start_new_session=True)
    return json.loads(result.stdout)["result"]


def check_seat(plan, row, rpc=query):
    expected = {"HERDR_SESSION": plan["session"], "HERDR_PANE_ID": row["pane"],
                "HERDR_TAB_ID": row["tab"], "HERDR_WORKSPACE_ID": row["workspace"]}
    require(all(os.environ.get(k) == v for k, v in expected.items()), "wrong session/workspace/tab/pane")
    require(os.isatty(0) and os.tcgetpgrp(0) == os.getpgrp(), "not the foreground terminal")
    session, pane = plan["session"], row["pane"]
    state = rpc(session, "pane", "get", pane)["pane"]
    require((state["pane_id"], state["workspace_id"], state["tab_id"]) == (pane, row["workspace"], row["tab"])
            and state.get("terminal_id") and not state.get("agent"), "occupied or changed pane")
    require(not state.get("agent_session"), "competing native binding")
    live = rpc(session, "pane", "process-info", "--pane", pane)["process_info"]
    require(live.get("pane_id") == pane and live.get("foreground_process_group_id") == os.getpgrp()
            and [p.get("pid") for p in live.get("foreground_processes", [])] == [os.getpid()],
            "original terminal does not own this foreground process")
    for other in rpc(session, "pane", "list")["panes"]:
        claim = other.get("agent_session") or {}
        require((claim.get("agent"), claim.get("value")) != (row["agent"], row["sid"]),
                "native session already claimed")
    return state["terminal_id"]


def check_holders(row, home, history):
    paths = [history]
    if row["agent"] == "agy":
        paths.append(home / ".gemini/antigravity-cli/presence" / (row["sid"] + ".lock"))
    if row["agent"] == "grok":
        paths.extend(history.with_name(name) for name in ("updates.jsonl.lock", "chat_history.jsonl", "chat_history.jsonl.lock"))
    scan = subprocess.run(["lsof", "-nP", "-t", *[str(p) for p in paths if p.exists()]],
                          stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    require(scan.returncode == 1 and not scan.stdout.strip() and not scan.stderr.strip(),
            "native history in use or owner check unavailable")


def enter(path, ticket):
    plan = read_json(path)
    require(plan["version"] == 1 and re.fullmatch("[0-9a-f]{32}", ticket), "invalid restore ticket")
    matches = [r for r in plan["panes"] if r.get("status") == "ready" and r.get("ticket") == ticket]
    require(len(matches) == 1, "unknown or ambiguous restore ticket")
    row = matches[0]
    home = Path(plan["home"])
    require(home == Path.home().resolve(), "native home changed")
    # Herdr can start pane commands before opening its private RPC socket.
    deadline = time.monotonic() + 10
    while True:
        try:
            terminal = check_seat(plan, row)
            break
        except (subprocess.SubprocessError, ConnectionError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)
    history = verify_native(row, home)
    check_holders(row, home, history)
    exe = shutil.which(row["launcher"])
    require(exe, "native launcher unavailable")
    args = [exe, *native_args(row)]
    require(check_seat(plan, row) == terminal, "terminal changed")
    os.chdir(row["cwd"])
    # Exclusive and durable before exec. A lost result or startup crash never
    # implicitly consumes another dispatch. Inspect the original private receipt.
    private_json(path.parent / ("attempt-" + ticket + ".json"),
                 {"status": "exec_intent", "pane": row["pane"], "terminal": terminal, "sid": row["sid"]})
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    os.execv(exe, args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("stage")
    build.add_argument("--snapshot", type=Path, required=True)
    build.add_argument("--session", required=True)
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--home", type=Path, default=Path.home())
    build.add_argument("--auto-enter", action="store_true")
    build.add_argument("--fleet-manifest", type=Path)
    build.add_argument("--witness", type=Path, help="one explicitly selected private epoch JSON file")
    build.add_argument("--witness-sha256", help="independently recorded digest of the selected epoch")
    build.add_argument("--launchers", type=Path, help="private agent to absolute executable JSON map")
    run = sub.add_parser("enter")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--ticket", required=True)
    args = parser.parse_args()
    if args.cmd == "stage":
        plan = stage(args.snapshot, args.session, args.out.resolve(), args.home.resolve(), args.auto_enter,
                     read_json(args.fleet_manifest) if args.fleet_manifest else None,
                     read_json(args.launchers) if args.launchers else None, args.witness, args.witness_sha256)
        print(json.dumps(dict(Counter(r["status"] for r in plan["panes"]))))
    else:
        enter(args.plan, args.ticket)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
        sys.exit("restore held: " + str(error))

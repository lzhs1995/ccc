"""Runner-only automatic native hook observer, plus an explicit exec-origin negative.

Only native hooks.json invokes --event. No fixture runner calls that branch.
No installed inbox helper, host emulator, registry injection, or control request.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import fixture_common as f
from native_query import identity, need, observe
from reader_acceptance import validate_reply

LIMIT = 65536
EVENTS = ("PreToolUse", "PostToolUse")


def header(root, sid):
    paths = list((root / "n/sessions").rglob("*" + sid + ".jsonl"))
    need(len(paths) == 1, "unique actual native rollout required")
    path = f.contained(root, paths[0])
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
             and not before.st_mode & 0o022, "unsafe actual native rollout")
        raw = stream.readline(LIMIT + 1)
        after = os.fstat(stream.fileno())
    current = path.lstat()
    need(raw.endswith(b"\n") and len(raw) <= LIMIT
         and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
         == (current.st_dev, current.st_ino), "native header read changed")
    row = json.loads(raw)
    need(row.get("type") == "session_meta" and row["payload"].get("id") == sid,
         "actual rollout header differs from native thread")
    return {"path": str(path), "device": before.st_dev, "inode": before.st_ino,
            "header": row["payload"], "header_sha256": f.sha(raw)}


def root_facts(root, root_sid, shared_session):
    meta = header(root, root_sid)
    need(meta["header"].get("parent_thread_id") is None
         and isinstance(meta["header"].get("source"), str) and bool(meta["header"]["source"])
         and meta["header"].get("thread_source") in (None, "user", "root"),
         "expected actual root metadata")
    return {"actual_thread_id": root_sid, "inherited_session_id": shared_session,
            "thread_scope": "ROOT_METADATA", "parent_thread_id": None,
            "transcript_path": meta["path"], "transcript_device": meta["device"],
            "transcript_inode": meta["inode"], "metadata_sha256": meta["header_sha256"]}, meta


def reader_result(record, payload, facts, handler_id):
    try:
        result = validate_reply(record["reply"], record["request"]["challenge"],
                                record["client"], record["peer"], [record["client"]],
                                record["request"]["expectedClientEpoch"],
                                payload, facts, handler_id)
        return {"accepted": True, "value": result,
                "scope": "pure_original_validator_only_no_host_or_inbox"}
    except ValueError as error:
        return {"accepted": False, "error": str(error),
                "scope": "pure_original_validator_only_no_host_or_inbox"}


def validate_native(record, ticket, event, payload, registration):
    value = record["reply"]
    need(value.get("schema") == 1 and value.get("purpose") == "native_caller_thread"
         and value.get("scope") == "caller-thread-readonly"
         and value.get("source") == "same-process-native-spawn"
         and value.get("threadId") == ticket["sid"]
         and value.get("challenge") == record["request"]["challenge"]
         and value.get("clientEpoch") == record["request"]["expectedClientEpoch"]
         and value.get("clientPid") == record["client"]["pid"]
         and value.get("clientBirth") == record["client"]["birth"]
         and value.get("peerPid") == os.getpid()
         and value.get("peerBirth") == record["peer"]["birth"]
         and value.get("spawnPid") == os.getpid()
         and value.get("spawnBirth") == record["peer"]["birth"]
         and record["peer"]["ppid"] == record["client"]["pid"],
         "actual automatic hook process/caller differs")
    need(value.get("foreground", False) is None and all(value.get(k) is False for k in
         ("sendAuthority", "controllerAuthority", "inboxAuthority")), "unexpected authority")
    expected = {"kind": "native-command-hook", "event": event,
                "turnId": payload.get("turn_id"), "toolUseId": payload.get("tool_use_id"),
                "handlerId": registration["handlers"][event]["handler_id"]}
    need(value.get("origin") == expected and value.get("callId") == expected["toolUseId"]
         and isinstance(expected["turnId"], str) and 0 < len(expected["turnId"]) <= 256
         and isinstance(expected["toolUseId"], str) and 0 < len(expected["toolUseId"]) <= 256,
         "native event/trigger/handler differs from real hook")
    need(payload.get("hook_event_name") == event and payload.get("tool_name") == "Bash"
         and payload.get("tool_input", {}).get("command") == ticket["command"],
         "hook does not describe this one actual fixture exec")


def automatic(args):
    root = f.require_runner(args.root)
    registration = json.loads(f.strict_file(root / "HOOK-REGISTRATION.json"))
    client = json.loads(f.strict_file(root / "CLIENT.json"))
    if not args.after_exec:
        raw = sys.stdin.buffer.read(LIMIT + 1)
        need(0 < len(raw) <= LIMIT, "hook input bound")
        # The directory is exclusively created; even a duplicate actual event is retained
        # separately and makes final cardinality fail. No overwrite/replay.
        invocation = root / ("hook-" + str(os.getpid()) + "-" + args.event)
        invocation.mkdir(mode=0o700)
        f.write_new(invocation / "stdin.json", raw, 0o600)
        payload = json.loads(raw)
        candidates = []
        for role in ("root", "child"):
            path = root / (role + "-ticket.json")
            if path.is_file():
                ticket = json.loads(f.strict_file(path))
                if payload.get("tool_input", {}).get("command") == ticket["command"]:
                    candidates.append(ticket)
        need(len(candidates) == 1, "hook input does not match one issued private exec")
        ticket = candidates[0]
        f.save_once(invocation / "BEFORE-EXEC.json",
                    {"process": identity(os.getpid()), "original_thread_environment": os.environ.get("CODEX_THREAD_ID"),
                     "override": ticket["root_sid"], "ticket": ticket,
                     "original_stdin_sha256": f.sha(raw)})
        env = dict(os.environ)
        env["CODEX_THREAD_ID"] = ticket["root_sid"]
        os.execve(sys.executable, [sys.executable, "-BE", str(Path(__file__).resolve()),
                   "--root", str(root), "--event", args.event,
                   "--after-exec", str(invocation)], env)
    invocation = f.contained(root, args.after_exec)
    before = json.loads(f.strict_file(invocation / "BEFORE-EXEC.json"))
    raw = f.strict_file(invocation / "stdin.json")
    payload = json.loads(raw)
    ticket = before["ticket"]
    need(identity(os.getpid()) == before["process"]
         and os.environ.get("CODEX_THREAD_ID") == ticket["root_sid"]
         and f.sha(raw) == before["original_stdin_sha256"], "exec override lost original hook")
    need(client == ticket["client"], "issued ticket differs from owned native client")
    record = observe(ticket)
    f.save_once(invocation / "NATIVE-OBSERVATION.json", record)
    validate_native(record, ticket, args.event, payload, registration)
    own = header(root, ticket["sid"])
    need(payload.get("session_id") == ticket["root_sid"]
         and payload.get("transcript_path") == own["path"], "actual hook session/transcript mismatch")
    if ticket["role"] == "child":
        need(ticket["sid"] != ticket["root_sid"] and payload.get("agent_id") == ticket["sid"]
             and own["header"].get("parent_thread_id") == ticket["root_sid"],
             "child must retain actual thread despite common session")
    else:
        need(payload.get("agent_id") is None and own["header"].get("parent_thread_id") is None,
             "root became a child")
    facts, root_meta = root_facts(root, ticket["root_sid"], payload["session_id"])
    original_reader = None
    if args.event == "PostToolUse":
        # Child supplies root metadata and removes its declared child tag. Actual native
        # thread still decides rejection. This is not an invocation of the inbox helper.
        claimed_payload = copy.deepcopy(payload)
        claimed_payload.pop("agent_id", None)
        claimed_payload.pop("agent_type", None)
        claimed_payload["transcript_path"] = root_meta["path"]
        original_reader = reader_result(record, claimed_payload, facts,
                            registration["handlers"]["PostToolUse"]["handler_id"])
        need(original_reader["accepted"] is (ticket["role"] == "root"),
             "original pure reader accepted wrong caller scope")
    marker = "AUTOHOOK_CONTEXT:" + ticket["nonce"] + ":" + ticket["role"] + ":" + args.event
    output = {"hookSpecificOutput": {"hookEventName": args.event, "additionalContext": marker}}
    result = {"passed": True, "role": ticket["role"], "event": args.event,
              "before_exec": before, "payload": payload, "positive": record,
              "native_metadata": own, "root_metadata": root_meta,
              "pure_reader": original_reader, "additional_context": marker, "output": output,
              "fixture_only": True, "registry_injected": False, "inbox_consumed": False}
    f.save_once(invocation / "RESULT.json", result)
    f.save_once(root / (ticket["role"] + "-" + args.event + "-hook.json"), result)
    print(json.dumps(output, separators=(",", ":")), flush=True)


def tool_negative(args):
    """This is an actual model-issued exec, explicitly NOT an automatic hook."""
    ticket = json.loads(f.strict_file(args.ticket))
    root = f.require_runner(ticket["root"])
    need(ticket["role"] in ("root", "child"), "foreign role")
    record = observe(ticket)
    f.save_once(root / (ticket["role"] + "-exec-observation.json"), record)
    need(record["reply"].get("origin") == {"kind": "unified-exec"}
         and record["reply"].get("threadId") == ticket["sid"], "not actual native exec origin")
    prior = json.loads(f.strict_file(root / (ticket["role"] + "-PreToolUse-hook.json")))
    need(prior["positive"]["reply"]["callId"] == record["reply"]["callId"],
         "automatic Pre hook and actual exec trigger differ")
    payload = copy.deepcopy(prior["payload"])
    payload["hook_event_name"] = "PostToolUse"
    payload.pop("agent_id", None)
    payload.pop("agent_type", None)
    facts, root_meta = root_facts(root, ticket["root_sid"], ticket["root_sid"])
    payload["transcript_path"] = root_meta["path"]
    registration = json.loads(f.strict_file(root / "HOOK-REGISTRATION.json"))
    refusal = reader_result(record, payload, facts,
                            registration["handlers"]["PostToolUse"]["handler_id"])
    need(refusal["accepted"] is False and refusal["error"] == "not the original automatic PostToolUse hook",
         "ordinary exec acquired automatic hook provenance")
    f.save_once(root / (ticket["role"] + "-tool.json"), {"passed": True, "positive": record,
                "fabricated_post_payload": payload, "refusal": refusal,
                "classification": "actual_exec_negative_not_automatic_hook",
                "original_inbox_helper_invoked": False})
    print("AUTOHOOK_TOOL_OK:" + ticket["role"] + ":" + ticket["nonce"], flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    parser.add_argument("--event", choices=EVENTS)
    parser.add_argument("--after-exec", type=Path)
    parser.add_argument("--ticket", type=Path)
    args = parser.parse_args()
    if args.ticket is not None:
        need(args.root is None and args.event is None and args.after_exec is None, "mixed execution paths")
        tool_negative(args)
    else:
        need(args.root is not None and args.event is not None, "automatic hook arguments absent")
        automatic(args)


if __name__ == "__main__":
    main()

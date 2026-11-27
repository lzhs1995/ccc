"""Preserve the exact original Codex launch when recovering a proven client panic.

This is separate from IEC repair: no provider selection, sanitizer override or
history rewrite. The existing one-use store, connected respawn and launcher are
shared; an unknown host result or claimed exec is never a retry authorization.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import time
import uuid

import ccc_request_recovery as request


RECOVERY_KIND = "native_client_panic"
VALUE_FLAGS = frozenset({"-m", "--model", "-c", "--config", "-p", "--profile",
    "-C", "--cd", "-s", "--sandbox", "-a", "--ask-for-approval", "--disable",
    "--enable", "-i", "--image", "--add-dir"})
BARE_FLAGS = frozenset({"--full-auto", "--dangerously-bypass-approvals-and-sandbox",
    "--search", "--oss", "--no-alt-screen"})


class Refused(ValueError):
    pass


def original_resume_argv(argv, session_id):
    """Preserve flags byte for byte, removing only one exact prior resume SID.

    Unknown flags, positional prompts and chooser/fork options refuse capture.
    Routing overrides are retained, including flags after the resume command.
    """
    session_id = str(uuid.UUID(session_id))
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise Refused("original argv unavailable")
    result, index, resumed = [argv[0]], 1, False
    while index < len(argv):
        arg = argv[index]
        if arg == "resume":
            if resumed or index + 1 >= len(argv) or argv[index + 1] != session_id:
                raise Refused("original resume must name exactly the owned session")
            resumed, index = True, index + 2
            continue
        name, separator, value = arg.partition("=")
        if arg in VALUE_FLAGS:
            if index + 1 >= len(argv) or not argv[index + 1]:
                raise Refused("original flag value missing")
            result.extend(argv[index:index + 2])
            index += 2
        elif separator and name in VALUE_FLAGS and value:
            result.append(arg)
            index += 1
        elif arg.startswith(("-c", "-p")) and len(arg) > 2 and not arg.startswith("--"):
            result.append(arg)
            index += 1
        elif arg in BARE_FLAGS:
            result.append(arg)
            index += 1
        else:
            raise Refused("unsupported original launch argument")
    return result + ["resume", session_id]


def original_config_pins(capture):
    """Pin all original supported config inputs without interpreting routing."""
    env = capture["environment"]
    directory = Path(capture["cwd"]).resolve(strict=True)
    home = env.get("CODEX_HOME") or str(Path(env.get("HOME", "")) / ".codex")
    if not Path(home).is_absolute():
        raise Refused("original config home is not absolute")
    # Alternative config loaders cannot be recreated by guessing their paths.
    if env.get("CODEX_CONFIG") or env.get("CODEX_CONFIG_OVERRIDES"):
        raise Refused("unsupported original config loader")
    paths = [Path(home) / "config.toml", directory / ".codex/config.toml",
        *(parent / ".codex/config.toml" for parent in directory.parents),
        Path("/etc/codex/config.toml"), Path("/Library/Application Support/Codex/config.toml")]
    pins, absent, seen = [], [], set()
    for path in paths:
        path = path.absolute()
        if str(path) in seen:
            continue
        seen.add(str(path))
        if path.exists() or path.is_symlink():
            if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                raise Refused("original configuration is not a bounded regular file")
            pins.append(request.file_pin(path))
        else:
            absent.append(str(path))
    return pins, absent


def original_route_matches(route):
    """A launch retains its original endpoint; no provider health is inferred."""
    try:
        return (route.get("kind") == "original_client_launch_configuration"
            and route.get("configuration_sha256") == request.digest({
                "config_pins": route["config_pins"], "absent_config_paths": route["absent_config_paths"],
                "original_argv_sha256": route["original_argv_sha256"],
                "original_environment_sha256": route["original_environment_sha256"]})
            and all(request.file_matches(pin, content=True) for pin in route["config_pins"])
            and all(not Path(p).exists() and not Path(p).is_symlink()
                    for p in route["absent_config_paths"]))
    except (KeyError, TypeError, ValueError, OSError):
        return False


def capture_panic_plan(target, turn, anchor, message):
    """Capture an authentic current reader and independently proven fatal EOF."""
    import ccc_guard_scope as scope
    import ccc_client_panic as panic

    capture = scope.process(turn.get("pid"), launch=True)
    if (not capture or capture["birth"] != list(turn.get("birth") or [])
            or capture["surface_id"] != target["surface_id"]
            or capture["environment_workspace_id"] != target["workspace_id"]
            or turn.get("kind") not in {"native_activity", "task_started"}
            or turn.get("session_id") != anchor["rollout"]["session_id"]
            or turn.get("transcript_path") != anchor["rollout"]["path"]):
        raise Refused("current panic does not own the original launch")
    verified = panic.native_anchor(turn["transcript_path"], turn["session_id"], capture["birth"],
                                   anchor["panic"], expected_signature=turn.get("signature"))
    if verified != anchor:
        raise Refused("fatal evidence changed before capture")
    executable = request.executable_path(capture["pid"])
    native_pin, argv0_pin = request.file_pin(executable), request.file_pin(capture["argv"][0])
    if argv0_pin["resolved"] != executable:
        raise Refused("original executable cannot be authenticated")
    config_pins, absent = original_config_pins(capture)
    argv = original_resume_argv(list(capture["argv"]), turn["session_id"])
    identity = {"surface_id": target["surface_id"], "workspace_id": target["workspace_id"],
        "pane_id": target["pane_id"], "session_id": turn["session_id"], "pid": capture["pid"],
        "birth": capture["birth"], "process_start": capture["process_start"],
        "turn_id": turn.get("turn_id"), "recovery_kind": RECOVERY_KIND,
        "fatal_output_sha256": anchor["fatal_output"]["sha256"],
        "rollout_file_id": anchor["rollout"]["file_id"], "rollout_fence": anchor["rollout"]["fence"]}
    prompt = str(message) + " CCC_CLIENT_PANIC_" + request.digest(identity)[:16]
    route_inputs = {"config_pins": config_pins, "absent_config_paths": absent,
        "original_argv_sha256": request.digest(capture["argv"]),
        "original_environment_sha256": request.digest(capture["environment"])}
    route = dict(route_inputs, kind="original_client_launch_configuration",
                 configuration_sha256=request.digest(route_inputs))
    plan = {"recovery_kind": RECOVERY_KIND, "identity": identity, "prompt": prompt,
        "executable": executable, "argv": argv + [prompt], "env": dict(capture["environment"]),
        "cwd": capture["cwd"], "rollout": anchor["rollout"], "fatal_evidence": anchor,
        "expected_turn": dict(turn), "original_capture_sha256": request.digest(capture),
        "original_process": {k: capture[k] for k in ("pid", "birth", "process_start")},
        "executable_pin": native_pin, "argv0_pin": argv0_pin, "config_pins": config_pins,
        "absent_config_paths": absent, "route": route,
        "launcher_pin": request.file_pin(request.__file__), "python_pin": request.file_pin(os.sys.executable),
        "dependency_pins": request.dependency_pins(recovery_kind=RECOVERY_KIND),
        "new_session_id": False, "routing_changed": False, "history_changed": False}
    if (scope.process(capture["pid"], launch=True) != capture
            or not request.rollout_matches(anchor["rollout"]) or not original_route_matches(route)):
        raise Refused("original launch or native EOF changed during capture")
    return plan

#!/usr/bin/env python3
"""
cmux_bridge.py — thin wrapper around the verified cmux CLI primitives.

All functions raise RuntimeError on cmux CLI failure.
Surface refs are strings like "surface:17" or UUIDs.
"""
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import unicodedata
from cmux_workspace_guard import require_same_workspace, WorkspaceScopeError, uuid_value, CMUX as VERIFIED_CMUX
from collections import Counter
from pathlib import Path

CMUX = VERIFIED_CMUX

COLLABORATION_SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"


class TaskPackContractError(RuntimeError):
    """Raised before delivery when a task pack can bypass callback discipline."""


def _first_payload_line(text):
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("[CMUX-AGENT]"):
            continue
        return line
    return ""


def _looks_like_task_dispatch(text):
    first = _first_payload_line(text)
    return bool(re.match(r"^(?:(?:TASK|TASK_PACK|TASK PACK)\s*[:=]|TASK_PACK_V2(?:\s|$))", first, re.I))


_TASK_PACK_NOTICE_SUFFIX = (
    " READ_AND_OBEY_REQUIRED_SKILL_FIRST; verify pack SHA; follow only its scope; "
    "write report then call cmux_bridge.submit_completion_callback(pack)."
)


def _task_pack_notice(path, pack, raw):
    """Exact single-line wire contract; all scope and callback bytes stay in pack."""
    from cmux_prompt_reference import require_inline
    notice = (
        "TASK_PACK_V2 TASK=" + json.dumps(pack["task_id"], ensure_ascii=False)
        + " PACK=" + json.dumps(str(path), ensure_ascii=False)
        + " SHA256=" + hashlib.sha256(raw).hexdigest()
        + _TASK_PACK_NOTICE_SUFFIX
    )
    require_inline(notice)
    return notice


def task_pack_notice(task_pack_path):
    """Read-only rendering; does not dispatch, receive, or accept a task."""
    path = Path(task_pack_path)
    pack = validate_task_pack_contract(path)
    raw = path.read_bytes()
    if json.loads(raw.decode("utf-8")) != pack:
        raise TaskPackContractError("TASK_PACK_CHANGED_DURING_NOTICE")
    return _task_pack_notice(path, pack, raw)


def validate_task_pack_contract(task_pack_path, prompt_text=None):
    """Validate the callback contract before a task reaches an executor."""
    path = Path(str(task_pack_path or ""))
    problems = []
    if not path.is_absolute() or not path.is_file():
        raise TaskPackContractError(
            "TASK_PACK_NOT_DISPATCHABLE: task_pack_path must be an existing absolute file"
        )
    try:
        raw = path.read_bytes()
        pack = json.loads(raw.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskPackContractError(
            f"TASK_PACK_NOT_DISPATCHABLE: unreadable task pack: {exc}"
        ) from exc
    if not isinstance(pack, dict):
        raise TaskPackContractError("TASK_PACK_NOT_DISPATCHABLE: root must be an object")

    task_id = pack.get("task_id")
    nonce = pack.get("completion_nonce")
    report = pack.get("report")
    completion = pack.get("completion_callback")
    target = pack.get("callback_target")
    skill_value = pack.get("required_skill")
    skill = Path(skill_value) if isinstance(skill_value, str) else None

    if pack.get("draft") is not False:
        problems.append("draft must be false")
    if not isinstance(task_id, str) or not task_id:
        problems.append("task_id must be nonempty")
    if not isinstance(nonce, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,}", nonce or ""
    ):
        problems.append("completion_nonce must be concrete")
    if not isinstance(report, str) or not Path(report).is_absolute():
        problems.append("report must be absolute")
    if skill is None or not skill.is_absolute() or not skill.is_file():
        problems.append("required_skill must be an existing absolute file")
    elif skill.resolve() != COLLABORATION_SKILL_PATH.resolve():
        problems.append("required_skill must be the canonical collaboration skill")
    if not isinstance(target, str) or not re.fullmatch(r"surface:[0-9]+", target):
        problems.append("callback_target must be surface:<N>")
    expected_callbacks = {
        f"DONE|{task_id}|{nonce}|REPORT={report}",
        f"BLOCKED|{task_id}|{nonce}|REPORT={report}",
    }
    if completion not in expected_callbacks:
        problems.append("completion_callback does not exactly bind task, nonce, and report")
    if pack.get("completion_delivery") != {
        "transport": "cmux_bridge.submit_completion_callback",
        "require_confirmed": True,
    }:
        problems.append("completion_delivery must require confirmed bridge delivery")
    receipt = pack.get("completion_receipt")
    if not isinstance(receipt, str) or not Path(receipt).is_absolute():
        problems.append("completion_receipt must be absolute")
    elif Path(receipt).parent.resolve() != path.parent.resolve():
        problems.append("completion_receipt must be a sibling of the task pack")

    if prompt_text is not None and str(prompt_text).startswith("TASK_PACK_V2"):
        try:
            if str(prompt_text) != _task_pack_notice(path, pack, raw):
                problems.append("task notice must exactly bind the whole pack; extra instructions forbidden")
        except (ValueError, KeyError) as exc:
            problems.append("invalid single-line task notice: " + str(exc))
    elif prompt_text is not None:
        prompt = str(prompt_text)
        required_fragments = (
            f"TASK_PACK={path}",
            f"REQUIRED_SKILL={COLLABORATION_SKILL_PATH}",
            "READ_AND_OBEY_REQUIRED_SKILL_FIRST",
            str(completion),
            f"CALLBACK_TARGET={target}",
        )
        for fragment in required_fragments:
            if fragment not in prompt:
                problems.append(f"prompt missing required binding: {fragment}")

    if problems:
        raise TaskPackContractError(
            "TASK_PACK_NOT_DISPATCHABLE: " + "; ".join(problems)
        )
    return pack


_workspace_pins = {}


def pin_workspace(surface, *, workspace_uuid=None, target_uuid=None, caller_uuid=None):
    expected = dict(_workspace_pins.get(surface, {}))
    for key, value in (("workspace_uuid", workspace_uuid),
                       ("target_surface_uuid", target_uuid),
                       ("caller_surface_uuid", caller_uuid)):
        if value is not None:
            if key in expected and uuid_value(expected[key]) != uuid_value(value):
                raise WorkspaceScopeError("WORKSPACE_SCOPE_DENIED: cannot overwrite a bound identity: " + key)
            expected[key] = value
    proof = require_same_workspace(surface, expected=expected)
    _workspace_pins[surface] = {key: proof[key] for key in
        ("workspace_uuid", "caller_surface_uuid", "target_surface_uuid", "target_pane_uuid")}
    return proof


def pin_successor_pair(artifact, *, expected_caller=None, expected_target=None):
    """Bind an explicitly authorized successor caller/receiver pair.

    Ordinary bridge calls never use this route and therefore keep the strict
    same-workspace contract.  The returned proof carries both workspace UUIDs
    so a caller cannot accidentally reuse the old single-workspace pin.
    """
    from cmux_workspace_guard import require_successor_pair
    return require_successor_pair(
        artifact, expected_caller=expected_caller, expected_target=expected_target)


def _run_successor(*args, artifact, check=True, capture=True, receipt_contract=None, json_output=False):
    """Run only explicit target RPCs under a successor pair proof."""
    from successor_rebind import CODEX_SAME_PANE
    if (artifact.get("transport") == CODEX_SAME_PANE
            and receipt_contract != "native-user-marker-v1"):
        raise WorkspaceScopeError("Historical native-only authorization forbids terminal input")
    args = list(args)
    proof = pin_successor_pair(artifact)
    if args[:2] == ['rpc', 'surface.input_state']:
        if len(args) != 3:
            raise WorkspaceScopeError('SUCCESSOR_REBIND_DENIED: explicit state target required')
        params = json.loads(args[2])
        if (not isinstance(params, dict) or set(params) != {'workspace_id', 'surface_id'}
                or uuid_value(params['workspace_id']) != uuid_value(proof['target_workspace_uuid'])
                or uuid_value(params['surface_id']) != uuid_value(proof['target_surface_uuid'])):
            raise WorkspaceScopeError('SUCCESSOR_REBIND_DENIED: state target override')
    elif args[:2] == ["rpc", "terminal.paste"]:
        if len(args) != 3:
            raise WorkspaceScopeError("SUCCESSOR_REBIND_DENIED: explicit paste parameters required")
        try:
            params = json.loads(args[2])
        except (TypeError, ValueError) as exc:
            raise WorkspaceScopeError("SUCCESSOR_REBIND_DENIED: invalid paste parameters") from exc
        if (not isinstance(params, dict) or params.get("submit_key") != "none"
                or set(params) != {"text", "submit_key", "workspace_id", "surface_id"}
                or not isinstance(params.get("text"), str) or not params["text"]):
            raise WorkspaceScopeError("SUCCESSOR_REBIND_DENIED: literal paste must not submit")
        if uuid_value(params["workspace_id"]) != uuid_value(proof["target_workspace_uuid"]):
            raise WorkspaceScopeError("SUCCESSOR_REBIND_DENIED: target workspace override")
        if uuid_value(params["surface_id"]) != uuid_value(proof["target_surface_uuid"]):
            raise WorkspaceScopeError("SUCCESSOR_REBIND_DENIED: target surface override")
        args[2] = json.dumps(dict(params, workspace_id=proof["target_workspace_uuid"],
                                   surface_id=proof["target_surface_uuid"]), ensure_ascii=False)
    elif args and args[0] in {"send", "send-key", "paste"}:
        if "--surface" not in args:
            raise WorkspaceScopeError("SUCCESSOR_REBIND_DENIED: explicit surface required")
        i = args.index("--surface") + 1
        if uuid_value(args[i]) != uuid_value(proof["target_surface_uuid"]):
            raise WorkspaceScopeError("SUCCESSOR_REBIND_DENIED: target surface override")
        if "--workspace" in args:
            wi = args.index("--workspace") + 1
            if uuid_value(args[wi]) != uuid_value(proof["target_workspace_uuid"]):
                raise WorkspaceScopeError("SUCCESSOR_REBIND_DENIED: target workspace override")
        else:
            args[1:1] = ["--workspace", proof["target_workspace_uuid"]]
    else:
        raise WorkspaceScopeError("SUCCESSOR_REBIND_DENIED: unsupported route")
    result = subprocess.run([CMUX] + (["--json"] if json_output else []) + args,
                            capture_output=capture, text=True)
    if check and result.returncode != 0:
        if args and args[0] == 'paste':
            from cmux_direct_input import protected_submit_refusal
            refused = protected_submit_refusal(result, proof['target_surface_uuid'])
            if refused is not None:
                raise refused
        raise RuntimeError(f"cmux successor call failed (rc={result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip() if capture else ""


def _run(*args, check=True, capture=True, json_output=False):
    """Run cmux with given args, return stdout string."""
    args = list(args)
    if args[:2] == ["rpc", "terminal.paste"]:
        if len(args) != 3:
            raise WorkspaceScopeError("WORKSPACE_SCOPE_DENIED: explicit paste parameters required")
        try:
            params = json.loads(args[2])
        except (TypeError, ValueError) as exc:
            raise WorkspaceScopeError("WORKSPACE_SCOPE_DENIED: invalid paste parameters") from exc
        if (not isinstance(params, dict) or params.get("submit_key") != "none"
                or set(params) != {"text", "submit_key", "workspace_id", "surface_id"}
                or not isinstance(params.get("text"), str) or not params["text"]):
            raise WorkspaceScopeError("WORKSPACE_SCOPE_DENIED: literal paste must not implicitly submit")
        proof = pin_workspace(params["surface_id"])
        if uuid_value(params["workspace_id"]) != uuid_value(proof["workspace_uuid"]):
            raise WorkspaceScopeError("WORKSPACE_SCOPE_DENIED: paste workspace changed")
        params.update(workspace_id=proof["workspace_uuid"], surface_id=proof["target_surface_uuid"])
        args[2] = json.dumps(params, ensure_ascii=False)
    elif args and args[0] in {"send", "send-key", "paste"}:
        if "--surface" not in args:
            raise WorkspaceScopeError("WORKSPACE_SCOPE_DENIED: explicit surface required")
        index = args.index("--surface") + 1
        proof = pin_workspace(args[index])
        args[index] = proof["target_surface_uuid"]
        if "--workspace" in args:
            wi = args.index("--workspace") + 1
            if args[wi].upper() != proof["workspace_uuid"]:
                raise WorkspaceScopeError("WORKSPACE_SCOPE_DENIED: workspace override")
        else:
            args[1:1] = ["--workspace", proof["workspace_uuid"]]
    cmd = [CMUX] + (["--json"] if json_output else []) + args
    result = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
    )
    if check and result.returncode != 0:
        if args and args[0] == 'paste':
            from cmux_direct_input import protected_submit_refusal
            refused = protected_submit_refusal(result, proof['target_surface_uuid'])
            if refused is not None:
                raise refused
        raise RuntimeError(
            f"cmux {' '.join(args)} failed (rc={result.returncode}):\n"
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip() if capture else ""


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def ping():
    """Return True if cmux socket is reachable."""
    try:
        out = _run("ping", check=False)
        return "PONG" in out
    except Exception:
        return False


def whoami():
    """
    Return caller identity dict from `cmux identify --json`.

    Keys: workspace_ref, surface_ref, surface_type, pane_ref, window_ref,
          workspace_id (from env).
    """
    from cmux_workspace_guard import caller_snapshot
    data, _tree, caller_env, _proof = caller_snapshot()
    caller = data.get("caller", {})
    return {
        "workspace_ref":  caller.get("workspace_ref", ""),
        "surface_ref":    caller.get("surface_ref", ""),
        "surface_type":   caller.get("surface_type", ""),
        "pane_ref":       caller.get("pane_ref", ""),
        "window_ref":     caller.get("window_ref", ""),
        "workspace_id":   caller_env.get("CMUX_WORKSPACE_ID", ""),
        "surface_id":     caller_env.get("CMUX_SURFACE_ID", ""),
        "provider":       "codex" if _proof is not None else "unknown",
    }


def identify_surface(surface_ref, workspace=None, window=None):
    """
    Return identity details for an explicit surface.

    The harness records the executor's actual terminal placement. Existing
    terminals may share a pane while retaining distinct surface identities.
    """
    args = ["identify", "--surface", surface_ref, "--json"]
    if workspace:
        args += ["--workspace", workspace]
    if window:
        args += ["--window", window]
    raw = _run(*args)
    data = json.loads(raw)
    caller = data.get("caller", {})
    return {
        "workspace_ref": caller.get("workspace_ref", ""),
        "surface_ref": caller.get("surface_ref", ""),
        "surface_type": caller.get("surface_type", ""),
        "pane_ref": caller.get("pane_ref", ""),
        "window_ref": caller.get("window_ref", ""),
    }


def surface_uuid_map():
    """Map surface_ref -> identity dict from `cmux tree --all --json --id-format both`.

    Refs (surface:N) are renumbered whenever surfaces open or close, so any
    artifact that outlives one poll must carry the stable UUID next to the ref.
    This is the same lesson the CCC watcher learned on 2026-08-20: joining
    tree/top snapshots on refs bound one surface's data to another's identity.

    Each value: {"surface_id", "workspace_id", "workspace_ref", "pane_ref",
    "title", "surface_type"}.  Returns {} when the tree is unreadable — callers
    must treat a missing UUID as "unknown", never guess from the ref.
    """
    try:
        raw = _run("tree", "--all", "--json", "--id-format", "both")
        data = json.loads(raw)
    except Exception:
        return {}
    out = {}
    for window in data.get("windows") or []:
        for workspace in window.get("workspaces") or []:
            ws_id = workspace.get("id", "")
            ws_ref = workspace.get("ref", "")
            for pane in workspace.get("panes") or []:
                for surface in pane.get("surfaces") or []:
                    ref = surface.get("ref", "")
                    sid = surface.get("id", "")
                    if not ref or not sid:
                        continue
                    out[ref] = {
                        "surface_id": sid,
                        "workspace_id": ws_id,
                        "workspace_ref": ws_ref,
                        "pane_ref": surface.get("pane_ref", ""),
                        "title": surface.get("title") or "",
                        "surface_type": surface.get("type") or "",
                    }
    return out


# ---------------------------------------------------------------------------
# Surface discovery
# ---------------------------------------------------------------------------

def list_surfaces(workspace=None):
    """
    Return live tree members of the resolved caller's workspace by default.

    Every row carries workspace/surface UUIDs. ``selected`` denotes the
    verified caller, not the focused tab or a globally docked surface.
    """
    # A managed daemon's default workspace is its origin, not this caller.
    # A global dock can appear under a workspace in the tree without being a
    # member of that workspace. Exclude it explicitly from peer discovery.
    from cmux_workspace_guard import caller_snapshot
    identity, tree, env, _proof = caller_snapshot()
    caller = identity["caller"]
    selector = workspace or env.get("CMUX_WORKSPACE_ID") or caller["workspace_ref"]
    matches = [ws for win in tree.get("windows", []) for ws in win.get("workspaces", [])
               if selector in (ws.get("id"), ws.get("ref"))]
    if len(matches) != 1:
        raise WorkspaceScopeError("WORKSPACE_SCOPE_DENIED: inventory workspace missing or ambiguous")
    ws = matches[0]
    return [{"ref": s["ref"], "surface_id": s["id"],
             "workspace_id": ws["id"], "workspace_ref": ws["ref"],
             "pane_ref": pane.get("ref", ""), "title": s.get("title", ""),
             "selected": s["ref"] == caller["surface_ref"],
             "surface_type": s.get("type", "unknown"),
             "is_terminal": s.get("type") == "terminal"}
            for pane in ws.get("panes", []) for s in pane.get("surfaces", [])
            if s.get("dock_scope") != "global"]


def _legacy_list_surfaces(workspace):
    """Legacy text parser adapter; not used for identity or task discovery."""
    # Try list-panels first (workspace-wide, shows type)
    args = ["list-panels"]
    if workspace:
        args += ["--workspace", workspace]
    raw = _run(*args, check=False)
    if raw and "surface:" in raw:
        return _parse_list_panels(raw)

    # Fallback: list-pane-surfaces (pane-scoped, text format)
    args2 = ["list-pane-surfaces"]
    if workspace:
        args2 += ["--workspace", workspace]
    raw2 = _run(*args2)
    return _parse_list_pane_surfaces(raw2)


def _parse_list_panels(raw):
    """Parse cmux list-panels output."""
    import re
    surfaces = []
    for line in raw.splitlines():
        line_stripped = line.strip()
        if not line_stripped or "surface:" not in line_stripped:
            continue
        focused = "[focused]" in line_stripped
        selected = focused  # focused == selected for list-panels
        # Extract surface ref
        m = re.search(r"(surface:\S+)", line_stripped)
        ref = m.group(1) if m else ""
        # Extract type (terminal, agentSession, browser, ...)
        type_m = re.search(r"surface:\S+\s+(\S+)", line_stripped)
        surface_type = type_m.group(1) if type_m else "unknown"
        surface_type = surface_type.replace("[focused]", "").strip()
        # Extract quoted title
        title_m = re.search(r'"([^"]*)"', line_stripped)
        title = title_m.group(1) if title_m else line_stripped
        surfaces.append({
            "ref": ref,
            "title": title,
            "selected": selected,
            "surface_type": surface_type,
            "is_terminal": surface_type == "terminal",
        })
    return surfaces


def _parse_list_pane_surfaces(raw):
    """Parse cmux list-pane-surfaces text output."""
    surfaces = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        selected = "[selected]" in line
        line = line.replace("[selected]", "").replace("*", "").strip()
        parts = line.split(None, 1)
        if not parts:
            continue
        ref = parts[0]
        title = parts[1].lstrip("⠂ ").strip() if len(parts) > 1 else ""
        surfaces.append({
            "ref": ref,
            "title": title,
            "selected": selected,
            "surface_type": "terminal",  # assumed
            "is_terminal": True,
        })
    return surfaces


def detect_provider(surface_dict):
    """
    Heuristic: infer agent provider from surface title and type.
    Returns 'claude', 'codex', 'opencode', or 'unknown'.
    """
    title = surface_dict.get("title", "").lower()
    stype = surface_dict.get("surface_type", "").lower()
    # agentSession surfaces have known providers
    if "claude" in title:
        return "claude"
    if "codex" in title:
        return "codex"
    if "opencode" in title:
        return "opencode"
    # Terminal surfaces running agent CLIs need screen-read to detect,
    # but we trust explicit --executor arg over heuristics
    return "unknown"


# ---------------------------------------------------------------------------
# Surface lifecycle
# ---------------------------------------------------------------------------

def spawn_executor(provider="codex", pane=None, cwd=None, focus=False, direction=None, supervisor_surface=None):
    """
    Spawn a terminal side split panel and launch the agent CLI inside it.

    agent-session surfaces (Codex native UI) do NOT support cmux send/read-screen.
    We use a plain terminal side panel and launch 'codex' (or other agent CLI)
    inside it so that send_text + read_screen work for harness I/O.

    Returns {ref, pane_ref, workspace_ref, provider}.
    """
    # New executor agents must be visible side panels, not tabs.
    # Split from the supervisor/caller surface unless a pane-only fallback is
    # explicitly needed by older cmux versions.
    direction = (direction or os.environ.get("MULTI_AGENT_PANEL_DIRECTION", "right")).strip().lower()
    if direction not in {"left", "right", "up", "down"}:
        direction = "right"

    supervisor_surface = supervisor_surface or os.environ.get("CMUX_SURFACE_ID", "")
    args = ["new-split", direction]
    if supervisor_surface:
        args += ["--surface", supervisor_surface]
    args += ["--focus", "true" if focus else "false"]
    out = _run(*args)
    # expected: "OK surface:24 workspace:7" for new-split
    parts = out.split()
    if len(parts) < 3 or parts[0] != "OK":
        raise RuntimeError(f"Unexpected new-split output: {out!r}")
    result = {
        "ref":           parts[1],
        "pane_ref":      "",
        "workspace_ref": parts[2],
        "provider":      provider,
        "mode":          "terminal+cli",
        "panel_mode":    f"side-split:{direction}",
    }
    return result


def launch_agent_in_terminal(surface_ref, provider="codex", prompt=None, cwd=None):
    """
    Launch an agent CLI inside a terminal surface.
    After this, the terminal is ready for send_text / read_screen.
    """
    import shutil
    if shutil.which(provider) is None:
        raise RuntimeError(f"Agent CLI '{provider}' not found on PATH")
    # Give the shell a moment to initialise
    import time as _time
    _time.sleep(1)
    cmd = provider
    if prompt:
        # non-interactive exec with initial prompt
        cmd = f"{provider} {prompt}"
    if cwd:
        import shlex
        cmd = f"cd {shlex.quote(str(cwd))} && {cmd}"
    # Start interactive session; handshake will confirm readiness
    send_text(surface_ref, cmd + "\n")


# ---------------------------------------------------------------------------
# Text I/O
# ---------------------------------------------------------------------------

class DispatchUnconfirmed(RuntimeError):
    """The TUI did not prove that a submitted prompt was consumed.

    Carries a `state` drawn from SUBMISSION_STATES. The message text stays
    human-readable and backward compatible (callers that only str() the
    exception keep working), but `state` is what a caller must branch on:
    only DELIVERY_QUEUED_AT_RECEIVER means "wait", and no state means
    "resend blindly".
    """

    def __init__(self, message, state=None, details=None):
        super().__init__(message)
        self.state = state
        self.details = dict(details or {})


# ---------------------------------------------------------------------------
# Submission state taxonomy (R3 consensus, task multi-agent-skill-hardening-20260830)
#
# One reason string cannot carry five delivery states with different corrective
# actions. `SUBMISSION_ABORTED_BUSY` and `COMPOSE_OCCUPIED` remain distinct
# constants for receipt readability even though they share the same busy-buffer
# remedy; collapsing the states is what made a delivered message indistinguishable
# from one that was never pasted.
# ---------------------------------------------------------------------------

SUPERVISOR_DID_NOT_SUBMIT = "SUPERVISOR_DID_NOT_SUBMIT"
SUBMISSION_ABORTED_BUSY = "SUBMISSION_ABORTED_BUSY"
COMPOSE_OCCUPIED = "COMPOSE_OCCUPIED"
COMPOSE_UNPARSED = "COMPOSE_UNPARSED"
# Enter was sent but our exact payload is still the receiver's draft.
STRANDED_IN_COMPOSE = "STRANDED_IN_COMPOSE"
DELIVERY_UNVERIFIED_BY_DETECTOR = "DELIVERY_UNVERIFIED_BY_DETECTOR"
DELIVERY_QUEUED_AT_RECEIVER = "DELIVERY_QUEUED_AT_RECEIVER"

SUBMISSION_STATES = (
    SUPERVISOR_DID_NOT_SUBMIT,
    SUBMISSION_ABORTED_BUSY,
    COMPOSE_OCCUPIED,
    COMPOSE_UNPARSED,
    DELIVERY_UNVERIFIED_BY_DETECTOR,
    DELIVERY_QUEUED_AT_RECEIVER,
)

# Only this state means "wait"; every other state means "stop and fix". No
# state means "resend blindly".
SUBMISSION_STATES_MEANING_WAIT = (DELIVERY_QUEUED_AT_RECEIVER,)


def native_submission_state(result):
    """Classify an original input observation without claiming native receipt."""
    return (DELIVERY_QUEUED_AT_RECEIVER if result.get("state") == "NATIVE_QUEUED"
            else DELIVERY_UNVERIFIED_BY_DETECTOR)

# A receiver that queues an inbound message while inside a tool call renders a
# labelled pending region. The marker sitting there is proof of delivery, not
# proof of failure.
_PENDING_QUEUE_RE = re.compile(
    r"Messages? to be submitted after|"
    r"Press up to edit queued messages|"
    r"queued message",
    re.IGNORECASE,
)
# Codex 排队区的标题行（实测 2026-10-09 01:15，EXECUTOR_READY_73c025c0/ff5ade80）：
# 顶格「• Queued follow-up inputs」，其下是缩进的 ↳ 排队正文，最后才是
# 「shift+← edit last queued message」。排队正文一律缩进，顶格才是 TUI 自己画的标题；
# 正文里引用这个标题名不能冒充排队区。
_QUEUED_FOLLOWUP_HEADER_RE = re.compile(r"^•\s+Queued follow-up inputs\b", re.I)


def compose_contains(screen, marker):
    """Public: is `marker` still sitting in the active compose block?

    Exposed because both the harness and the guards must ask this question, and
    a private helper forced each caller to re-implement compose parsing. The
    bridge-test clear postcondition is the primary consumer: `ctrl+u` is only
    proven to have worked when this returns False on a fresh read.
    """
    return _prompt_block_pending(screen, marker)


# Empty-compose placeholders, per receiver UI. An empty box is not a blank line:
# each TUI renders prompt text inside it, so a substring search for "any text"
# would classify every idle box as occupied.
COMPOSE_PLACEHOLDERS = (
    "Ask Codex to do anything",
    "Ask Claude to do anything",
    "esc to interrupt",
    "? for shortcuts",
    "for shortcuts",
    "Try \"",
)

# Claude Code can render product-owned virtual suggestions inside an otherwise
# empty compose box. They look exactly like submitted Chinese prompts in a
# screen read, but are not user input and disappear when real text is entered.
# Keep this list exact and normalized: broad substring matching could erase a
# genuine prompt. These strings were confirmed by the user after a false
# COMPOSE_OCCUPIED diagnosis on the context-bearing Claude session.
CLAUDE_VIRTUAL_COMPOSE_PROMPTS = (
    "/multi-agent-collaboration",
    "continue",
    "read the executor report",
    "read the report",
    "review the consensus documents",
    "read the review and apply the changes",
    "check the integration validation artifact",
    "git diff scripts/thesis_format_adapter.py scripts/test_thesis_adapter_hardening.py",
    "/context",
    "任务中断了么？如果是就请继续，如果任务完成了务必在最后一句向我报告 ‘ 完成，建议检查 usage: /context’。如果任务没有中断就请继续，不要影响你的进度",
    "看一下 codex任务进展到哪了？下一步该干啥。详细计划给我。",
    "看一下 codex 那边进展到哪了？下一步该干啥。详细计划给我。",
    "看一下 codex 那边进展",
    "看一下 codex 那边收到了吗",
    "看一下 codex 那边收到没有",
    "看一下 codex 那边接下来要做什么",
    "继续，等 supervisor 的 nonce ACK",
    "继续握手，发送 ACK",
    "继续，等 supervisor 的下一个 dispatch",
    "继续等 codex 下一个派发",
    "继续，等 codex 的下一个 dispatch",
    "请澄清 203 Python 测试的位置和接口",
)

# A provider hint row rendered BELOW the model/provider row. Measured
# 2026-10-04T12:22Z on a GPT-6-Astra TUI (fixture
# claude-output/fixture-codex-footer-20261004.txt) and on 2026-10-02 with the
# `← for agents` prefix. Because it sits below the model row, the old
# tail[-1]-only footer check never reached the model row and an idle, empty
# composer classified as UNKNOWN, refusing every send with zero input.
# Keep this anchored and token-exact: it is the one trailing shape allowed to
# be skipped, so any NEW unmeasured row must still override a stale footer.
def _clipped_prefix_pattern(literal):
    """Regex source matching any prefix of ``literal`` (including empty).

    Pane width clips a footer row at an arbitrary column, so the tail of a
    known row can arrive as any prefix of itself. Expanding the literal into
    nested optional characters keeps the match token-exact: `warnings · f2`
    matches, `warnings · f3` does not. A looser `.*` here would let any
    trailing text pose as known chrome and defeat the fail-closed gate.
    """
    pattern = ""
    for char in reversed(literal):
        pattern = "(?:" + re.escape(char) + pattern + ")?"
    return pattern


# The provider hint row, tolerant of width truncation. Measured
# 2026-10-04T12:33Z on surface:27 (verification/fixtures/) where the SAME idle
# composer classified AGENT_TUI at full width and UNKNOWN once clipped,
# refusing an executor completion callback with zero input. The `⚠` warning
# segment, its count, and the `f2 to view` hint may each be cut mid-token.
# Two measured shapes share this slot. Codex renders the shortcuts row when
# the composer is empty and `tab to queue message` when it holds un-submitted
# text (measured 2026-10-04, fixture codex-footer-tab-to-queue-20261004.txt).
# Both sit BELOW the model row, so both defeat a tail[-1]-only footer check.
# Treating them as chrome only answers "is the receiver an agent"; the
# separate compose check still sees the pasted text and still refuses, so a
# peer's un-submitted callback is not at risk from this row alone.
_PROVIDER_HINT_ROW_RE = re.compile(
    r"\s*(?:"
    r"(?:←\s*for agents\s*·\s*)?\?\s*for shortcuts\s*"
    r"(?:⚠\s*(?:\d+\s*" + _clipped_prefix_pattern("warnings · f2 to view") +
    r")?)?"
    r"|"
    r"tab to " + _clipped_prefix_pattern("queue message") +
    r"|⚠\s*\d+\s*" + _clipped_prefix_pattern("warnings · f2 to view") +
    r")\s*",
    re.IGNORECASE,
)

# Status/footer lines some TUIs render inside or directly under the compose box.
# They are chrome, not user content, but they are not fixed strings either, so
# they need patterns rather than substring removal.
_COMPOSE_CHROME_RE = re.compile(
    r"^" + _PROVIDER_HINT_ROW_RE.pattern + r"$|"
    r"^\s*(?:gpt-[\w.\-]+|claude-[\w.\-]+|opus-[\w.\-]+|sonnet-[\w.\-]+)\b.*$|"
    # Width-clipped forms of the model banner and cwd segment. A narrow
    # pane cuts the banner before its closing "]" and renders the cwd as
    # "claude/<branch>", so the strict forms above miss and
    # compose_block_is_empty counted both as typed user text -> false
    # COMPOSE_OCCUPIED. Measured 2026-10-04 on three saved captures.
    # Deliberately narrow: a provider token followed by "/" or "-", or an
    # unclosed provider bracket -- never arbitrary trailing text.
    r"^\s*\[(?:Opus|Claude|Sonnet|GPT)[^\]]*$|"
    r"^\s*(?:claude|codex|opus|sonnet|gpt)[/\-][\w.\-/]*…?\s*$|"
    r"^\s*\[(?:Opus|Claude|Sonnet|GPT)[^\]]*\].*$|"
    r"^\s*[^\n]*\bgit:\([^)]*\).*$|"
    r"^\s*\d+%\s+context.*$|"
    r"^\s*上下文\b.*$|"
    r"^\s*⏱️?\s+\d+[hm]\b.*$|"
    r"^\s*\d+\s+.*(?:CLAUDE|MCPs|hooks|钩子).*$|"
    r"^\s*▸\s+.+\(\d+/\d+\)\s*$|"
    r"^\s*[✓⏵⏩].*$|"
    r"^\s*[─│╭╮╰╯>\s?]*$",
    re.IGNORECASE,
)


_CLAUDE_BORDER_RE = re.compile(r"─{8,}")
_CLAUDE_DURATION = r"(?:\d+h(?:\s+\d+m)?|\d+m(?:\s+\d+s)?|\d+s)"
# Goal's measured long-running display uses whole days. Keep this separate
# from elapsed/tool-summary durations, whose renderer has not shown that form.
_CLAUDE_GOAL_DURATION = rf"(?:\d+d|{_CLAUDE_DURATION})"
_CLAUDE_TOOL = r"[A-Za-z][\w.:-]*"
_CLAUDE_COUNTS = (rf"✓\s+{_CLAUDE_TOOL}\s+×\d+(?:\s*\|\s*✓\s+{_CLAUDE_TOOL}\s+×\d+)*"
                  r"(?:\s*\|\s*\+[1-9]\d* more)?")
_CLAUDE_ACTIVE_TOOL_RE = re.compile(
    rf"[◐◑◒◓]\s+{_CLAUDE_TOOL}:\s+[^|\n]+(?:\s*\|\s*{_CLAUDE_COUNTS})?")
_CLAUDE_SUMMARY_RE = re.compile(
    rf"(?:{_CLAUDE_COUNTS}|✓\s+{_CLAUDE_TOOL}(?:\s+\[[\w.-]+\])?:\s+"
    rf"[^|\n]+\((?:<)?{_CLAUDE_DURATION}\)|▸\s+[^\n│]+(?:\(\d+/\d+\)|…|\.{{3}}))")
# Measured 2026-10-13: the HUD appends input/cache counts at high context.
# Its formatTokens renders <1000 as integers, rounded thousands as Nk (up
# to 1000k), and millions with one decimal and M. Accept only that grammar
# below the complete borders; a matching draft is still verbatim user input.
_CLAUDE_HUD_TOKENS = (
    r"(?:0|[1-9][0-9]{0,2}|(?:[1-9][0-9]{0,2}|1000)k|[1-9][0-9]*\.[0-9]M)")
_CLAUDE_CONTEXT_ROW_RE = re.compile(
    r"上下文\s+[█░▒▓▏▎▍▌▋▊▉▐]+\s+(?:100|\d{1,2})%"
    rf"(?: \(输入: {_CLAUDE_HUD_TOKENS}, 缓存: {_CLAUDE_HUD_TOKENS}\))?")
_CLAUDE_GOAL_ROW_RE = re.compile(rf"\s*◎ /goal active \({_CLAUDE_GOAL_DURATION}\)\s*")
_CLAUDE_BYPASS_ROW_RE = re.compile(
    r"⏵⏵\s+bypass permissions on(?:"
    r" \(shift\+tab to cycle\)(?:\s*·\s*← for agents)?"
    # Measured 2026-10-13/14: Monitor or one shell replaces the shift+tab
    # hint. Recognize only the complete footer below matching borders.
    r"| · (?:1 (?:monitor|shell)|[2-9][0-9]* monitors|1[0-9]+ monitors) · ← for agents)")


def _claude_framed_prompt(screen):
    """A Claude frame is present, even when its footer cannot be parsed."""
    rows = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", screen).splitlines()
    start = next((i for i in range(len(rows) - 1, -1, -1)
                  if _PROMPT_GLYPH_RE.match(rows[i])), None)
    return bool(start is not None and rows[start].lstrip().startswith("❯")
                and any(_CLAUDE_BORDER_RE.fullmatch(row.strip())
                        for row in rows[max(0, start - 1):]))


def _claude_bordered_compose(screen):
    """仅解析完整 Claude 边框；正文绝不参与 footer 正则过滤。"""
    rows = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", screen).splitlines()
    start = next((i for i in range(len(rows) - 1, -1, -1)
                  if _PROMPT_GLYPH_RE.match(rows[i])), None)
    if (start is None or start == 0 or not rows[start].lstrip().startswith("❯")
            or not _CLAUDE_BORDER_RE.fullmatch(rows[start - 1].strip())):
        return None
    end = next((i for i in range(start + 1, len(rows))
                if rows[i].strip() == rows[start - 1].strip()), None)
    if end is None:
        return None
    before = rows[:start - 1]
    current = next((row for row in reversed(before) if row.strip()), "")
    if (re.match(r"\s*◎\s+/goal\b", current)
            and not _CLAUDE_GOAL_ROW_RE.fullmatch(current)):
        # Unknown Goal chrome must not hide an active spinner from the
        # clearing/submission guards. Preserve the whole frame as unparsed.
        return None
    footer_rows = [row for row in rows[end + 1:] if row.strip()]
    footer = [row.strip() for row in footer_rows]
    if not footer:
        return None
    # 当前实屏的 model/cwd/time/goal 同行；cwd 可以没有 git 信息。
    model_row = re.sub(rf"\s+◎\s+/goal active\s+\({_CLAUDE_GOAL_DURATION}\)$", "", footer[0])
    fields = [part.strip() for part in model_row.split("│")]
    if (not 1 <= len(fields) <= 3 or not re.fullmatch(
            # Measured 2026-10-18: provider/model banners retain the same
            # complete field. Accept one bounded ASCII provider segment only.
            r"(?:\[(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/)?claude-[\w.-]+(?:\[\d+[mM]\])?\]|"
            r"\[(?:Opus|Sonnet|Claude)\s+[^\]\n]+\])", fields[0], re.I)):
        return None
    elapsed = rf"⏱\ufe0f?\s+{_CLAUDE_DURATION}"
    # Measured 2026-10-09/19: the inline elapsed field may carry the clear
    # hint or the auto-compact percentage with its exact model suggestion.
    # Require one complete known suffix and ASCII padding outside the borders;
    # never strip matching draft text or accept arbitrary trailing chrome.
    inline_elapsed = (
        elapsed + r"(?: +(?:new task\? /clear to save "
        r"(?:[0-9]+(?:\.[0-9])?k|[0-9]+) tokens|"
        r"(?:100|[1-9]?[0-9])% until auto-compact · /model opus\[1m\]))?")
    cwd_field = r"[\w./~+\-]+(?:\s+git:\([^()\n]+\))?"
    # Measured on Claude48 at 55 columns: the git branch ends in an ellipsis
    # before its closing parenthesis. Accept only at the physical row end,
    # below matching complete composer borders and above the remaining footer.
    # Claude also uses ASCII "..." for clipping (2026-10-12). The fullmatch
    # applies to a physical footer row, never to text inside the composer.
    cwd_at_line_end = rf"(?:{cwd_field}|[\w./~+\-]+\s+git:\([^()\n│…]+(?:…|\.{{3}}))"
    cwd_ends_row = len(fields) == 2
    footer_start = 1
    # Measured 2026-10-10 (surface:4546): model-only row, then either one
    # complete cwd/time row or two separate cwd and elapsed rows. Consume only
    # these exact footer shapes, never composer text or an arbitrary
    # continuation row. The remaining chrome is still required.
    if len(fields) == 1 and len(footer) > 1:
        wrapped = [part.strip() for part in footer[1].split("│")]
        if (len(wrapped) == 2 and re.fullmatch(cwd_field, wrapped[0])
                and re.fullmatch(inline_elapsed, wrapped[1])):
            fields.extend(wrapped)
            footer_start = 2
        elif (len(footer) > 2
              and footer_rows[1].startswith("  ")
              and footer_rows[2].startswith("  ")
              and re.fullmatch(cwd_at_line_end, footer[1])
              and re.fullmatch(inline_elapsed, footer[2])):
            fields.extend((footer[1], footer[2]))
            footer_start = 3
            cwd_ends_row = True
    if len(fields) >= 2 and not re.fullmatch(
            cwd_at_line_end if cwd_ends_row else cwd_field, fields[1]):
        return None
    if len(fields) == 3 and not re.fullmatch(inline_elapsed, fields[2]):
        return None
    if len(footer) > 1:
        rest = footer[footer_start:]
        # The measured Goal HUD may occupy one final physical row after
        # bypass. Only complete footer rows outside the editor are consumed;
        # duplicates, unknown Goal forms and draft bytes remain untouched.
        if rest and _CLAUDE_GOAL_ROW_RE.fullmatch(rest[-1]):
            rest = rest[:-1]
        # The same measured hint can occupy its own row AFTER the complete
        # footer. Never remove an occurrence inside the composer, or permit
        # arbitrary text after the normal bypass row.
        if rest and re.fullmatch(
                r"new task\? /clear to save (?:[0-9]+(?:\.[0-9])?k|[0-9]+) tokens"
                rf"(?: · ◎ /goal active \({_CLAUDE_GOAL_DURATION}\))?",
                rest[-1]):
            rest = rest[:-1]
        if rest and re.fullmatch(elapsed, rest[0]):
            rest = rest[1:]
        if (len(rest) < 3
                or not _CLAUDE_CONTEXT_ROW_RE.fullmatch(rest[0])
                or not re.fullmatch(
                    r"\d+\s+CLAUDE\.md\s*\|\s*(?:\d+\s+规则\s*\|\s*)?"
                    r"\d+\s+MCPs\s*\|\s*\d+\s+(?:钩子|hooks)", rest[1])
                or not _CLAUDE_BYPASS_ROW_RE.fullmatch(rest[-1])
                or not all(_CLAUDE_SUMMARY_RE.fullmatch(row)
                           or _CLAUDE_ACTIVE_TOOL_RE.fullmatch(row) for row in rest[2:-1])):
            return None
    elif len(fields) != 1 or model_row != footer[0]:
        # 历史的单 model 行有独立回归；完整多字段 footer 不能截断成它。
        return None
    first = re.sub(r"^\s*❯[ \u00a0]?", "", rows[start], count=1)
    return {"body": "\n".join([first, *rows[start + 1:end]]),
            "footer": footer, "before": before, "start": start, "end": end}


def compose_block_text(screen):
    """Body of the CURRENT compose block, or None when no block is open.

    Uses the same last-block parse as ``_prompt_block_pending``: the final
    glyph block is live compose and an earlier one is transcript echo. Reusing
    that function's rule rather than re-deriving it is deliberate -- treating an
    earlier block as live was a real bug fixed once already, and a second parse
    would be free to regress independently.
    """
    claude = _claude_bordered_compose(screen)
    if claude is not None:
        return claude["body"]
    block = []
    in_prompt = False
    for line in screen.splitlines():
        if _PROMPT_GLYPH_RE.match(line):
            in_prompt = True
            block = [_PROMPT_GLYPH_RE.sub("", line, count=1)]
            continue
        if _ACTIVITY_RE.match(line):
            in_prompt = False
            block = []
            continue
        if in_prompt:
            block.append(line)
    if not in_prompt:
        return None
    return "\n".join(block)


def delivery_compose_text(screen):
    """Conservative last glyph block; payload bullets never close an editor."""
    lines = screen.splitlines()
    positions = [i for i,line in enumerate(lines) if _PROMPT_GLYPH_RE.match(line)]
    if not positions:
        return None
    i = positions[-1]
    return "\n".join([_PROMPT_GLYPH_RE.sub("", lines[i], count=1), *lines[i+1:]])


def compose_rendered_text(screen):
    """Conservative editor text, stripping footer chrome; None is unobserved."""
    claude = _claude_bordered_compose(screen)
    if claude is not None:
        return claude["body"].strip(" \t\r\n")
    if _claude_framed_prompt(screen):
        return None  # Unknown footer is unobserved, not a fabricated draft.
    body = delivery_compose_text(screen)
    if body is None:
        return None
    lines = body.splitlines()
    prompt = next((row for row in reversed(screen.splitlines())
                   if _PROMPT_GLYPH_RE.match(row)), "")
    if prompt.lstrip().startswith("❯"):
        # 无完整边框时只保留历史单 model 行布局；多行草稿和未知 footer 不裁切。
        if len(lines) == 2 and re.fullmatch(
                r"\s*\[(?:Opus|Claude|Sonnet|GPT)[^\]]*\](?: context \d+%)?\s*",
                lines[-1], re.I):
            return lines[0].strip(" \t\r\n")
        return body.strip(" \t\r\n")
    # Never discard a typed first line, even when it resembles footer chrome.
    while len(lines) > 1 and (not lines[-1].strip() or _COMPOSE_CHROME_RE.fullmatch(lines[-1].strip())):
        lines.pop()
    return "\n".join(lines).strip(" \t\r\n│─╭╮╰╯")


def compose_block_is_empty(screen):
    rendered = compose_rendered_text(screen)
    # Plain screen text cannot distinguish a dim suggestion from a user who
    # typed continue, /context, /compact, or a prior virtual-prompt allowlist.
    return rendered in ("", "Ask Codex to do anything", "Ask Claude to do anything")


def pending_queue_holds(screen, marker):
    """True when the receiver has queued the marker behind an active tool call.

    Positively detectable, so this state never needs a timeout to diagnose. The
    marker must appear at or after the pending-queue banner; a marker elsewhere
    on screen is a different state entirely.
    """
    if not isinstance(marker, str) or not marker:
        return False
    lines = screen.splitlines()
    # 队列只存在于 live composer 上方；不能用别人的 banner 认领下面的草稿。
    starts = [i for i, line in enumerate(lines) if _PROMPT_GLYPH_RE.match(line)]
    if not starts:
        return False
    limit = starts[-1]
    # Codex 的排队正文在标题之下、「edit last queued message」提示之上；
    # 只认提示行会漏掉真排队的原次（0.4.0 实测 ff5ade80 判为未排队）。
    banners = [i for i in range(limit) if _PENDING_QUEUE_RE.search(lines[i])
               or _QUEUED_FOLLOWUP_HEADER_RE.match(lines[i])]
    for pos, index in enumerate(banners):
        end = banners[pos + 1] if pos + 1 < len(banners) else limit
        region = lines[index + 1:end]
        # 新活动或新的已提交消息结束该 queue 区域。
        for j, row in enumerate(region):
            if _ACTIVITY_LINE_RE.match(row) or _PROMPT_GLYPH_RE.match(row):
                region = region[:j]
                break
        if "".join(marker.split()) in "".join("\n".join(region).split()):
            return True
    return False


def classify_submission_failure(screen, marker, submitted):
    """Map observable evidence onto exactly one submission state.

    `submitted` is whether the paste+enter pair actually ran. It is passed in
    rather than inferred because "never sent" and "sent but unverified" are
    indistinguishable from the screen alone, and that ambiguity is precisely
    what caused a valid ACK to be recorded as executor silence.
    """
    if not submitted:
        return SUPERVISOR_DID_NOT_SUBMIT
    if pending_queue_holds(screen, marker):
        return DELIVERY_QUEUED_AT_RECEIVER
    if _queued_or_active_input(screen):
        return COMPOSE_OCCUPIED
    if compose_contains(screen, marker):
        return SUBMISSION_ABORTED_BUSY
    return DELIVERY_UNVERIFIED_BY_DETECTOR


# Compose-prompt glyphs, by receiver UI. Claude renders ``❯`` (U+276F); Codex
# renders ``›`` (U+203A). The busy/compose detector is provider-agnostic by
# design -- it runs against whatever surface we are sending to -- so knowing only
# one glyph made it blind in one direction. Measured: a callback left unsubmitted
# in a Codex supervisor's compose box classified as
# DELIVERY_UNVERIFIED_BY_DETECTOR ("cannot tell") when the true state was
# definitively SUBMISSION_ABORTED_BUSY ("still in compose, never sent"). The same
# blindness would have made the bridge-test clear postcondition report
# clear_confirmed=true against a Codex receiver without ever verifying anything --
# a false green in the mechanism built to prevent false greens.
_PROMPT_GLYPH_RE = re.compile(r"^\s*(?:❯|›)(?:\s|$)")

# A structural activity line closes a compose block. Named once and shared, so
# the block parse cannot drift between callers: `compose_block_text` and
# `_prompt_block_pending` must agree on where the current block starts and ends,
# and two copies of this pattern would be free to diverge.
_ACTIVITY_RE = re.compile(
    r"^\s*(?:⏺|✻|✢|✳|✶|✽|◐|◑|◒|◓)(?:\s|$)|"
    r"^\s*•\s+(?:Edited|Ran|Read|Updated|Created|Deleted|Applied|Searched|Checked|Viewed|Wrote)(?:\s|$)"
)


def _prompt_block_pending(screen, marker):
    """Return True only when marker remains in the CURRENT compose block.

    Handles both receiver UIs; see ``_PROMPT_GLYPH_RE``. Only a glyph in leading
    position opens a block, so the same character quoted inside prose is not a
    boundary.

    "Current" means the last block on screen, which is the live compose box.
    An earlier glyph block is transcript: both UIs echo a submitted message with
    the same glyph they use for the input box, so a marker found there is proof
    of delivery, not of a stuck paste. An earlier version returned True as soon
    as a second block opened, which inverted exactly that case -- measured on a
    real callback that Codex had already received and was actively working on.
    """
    claude = _claude_bordered_compose(screen)
    if claude is not None:
        return "".join(marker.split()) in "".join(claude["body"].split())
    block = []
    in_prompt = False
    for line in screen.splitlines():
        if _PROMPT_GLYPH_RE.match(line):
            in_prompt = True
            block = [line]
            continue
        if _ACTIVITY_RE.match(line):
            in_prompt = False
            block = []
            continue
        if in_prompt:
            block.append(line)
    return in_prompt and "".join(marker.split()) in "".join("\n".join(block).split())


def _submission_confirmed(screen, marker):
    """Require marker followed by activity; terminal wrapping may split marker."""
    preceding = ""
    token = "".join(marker.split())
    for line in screen.splitlines():
        if token in preceding and _ACTIVITY_LINE_RE.match(line):
            return True
        preceding += "".join(line.split())
    return False


_ACTIVITY_LINE_RE = re.compile(
    r"^\s*(?:⏺|✻|✢|✳|✶|✽|◐|◑|◒|◓)(?:\s|$)|"
    r"^\s*•\s+(?:Edited|Ran|Read|Updated|Created|Deleted|Applied|Searched|Checked|Viewed|Wrote)(?:\s|$)"
)


def require_exact_composer(screen, surface, text):
    """Match the complete payload under measured terminal rendering only.

    Claude can render one extra ASCII cell after the last character. This
    cannot be distinguished from a typed trailing space using read-screen;
    it is display equivalence, not byte-exact access to the native editor.
    No body whitespace, unknown footer, or extra content is normalized.
    """
    try:
        return _require_exact_composer_without_cursor_cell(screen, surface, text)
    except DispatchUnconfirmed:
        rows = screen.splitlines()
        start = next((i for i in range(len(rows)-1, -1, -1)
                      if _PROMPT_GLYPH_RE.match(rows[i])), None)
        if (not text or text[-1].isspace() or start is None or start == 0
                or not rows[start].startswith("❯\u00a0")
                or not re.fullmatch(r"─{8,}", rows[start-1])):
            raise
        end = next((i for i in range(start+1, len(rows))
                    if rows[i] == rows[start-1]), None)
        if (end is None or end+1 >= len(rows)
                or _claude_bordered_compose(screen) is None
                or not rows[end-1].endswith(" ")
                or len(rows[end-1]) < 2 or rows[end-1][-2].isspace()):
            raise
        # Exactly one terminal cell on the final content row, never strip().
        rows[end-1] = rows[end-1][:-1]
        return _require_exact_composer_without_cursor_cell("\n".join(rows), surface, text)


def _display_cells(value):
    return sum(0 if unicodedata.combining(c) else
               (2 if unicodedata.east_asian_width(c) in "WF" else 1)
               for c in value)


def _claude_task_pack_padding_candidate(content, text, border_cells):
    """Measured formal-notice padding, without reading or altering a pack.

    B0015/B0016 wrap the PACK token at 114 display cells, then add exactly
    44/52 ASCII cells after its closing quote (at display cell 96). B0018
    has a 45-cell suffix and 27 ASCII cells, ending at cell 72. B0019
    has a 47-cell suffix and 49 ASCII cells, ending at cell 96. Restrict
    equivalence to those observed shapes. B0021/B0022 use 105-cell borders,
    wrap PACK at 100 cells with a 56/60-cell suffix, and add 11 ASCII cells
    after the standalone SHA256 token. The caller still checks the entire
    notice and wrap geometry; draft stability retains every original cell.
    """
    if (border_cells, len(content)) not in ((118, 5), (105, 6)):
        return None
    prefix = "TASK_PACK_V2 TASK="
    header, separator, rest = text.partition(" PACK=")
    if not separator or not header.startswith(prefix) or content[0] != header:
        return None
    try:
        task_id = json.loads(header[len(prefix):])
        path, end = json.JSONDecoder().raw_decode(rest)
    except (ValueError, TypeError):
        return None
    if (not isinstance(task_id, str) or not task_id
            or header != prefix + json.dumps(task_id, ensure_ascii=False)
            or not isinstance(path, str) or not path.startswith("/")
            or rest[:end] != json.dumps(path, ensure_ascii=False)
            or not re.fullmatch(r" SHA256=[0-9a-f]{64}" +
                                re.escape(_TASK_PACK_NOTICE_SUFFIX), rest[end:])):
        return None
    token = "PACK=" + rest[:end]
    if (not content[1].startswith("PACK=") or not token.startswith(content[1])
            or _display_cells(content[1]) != (100 if border_cells == 105 else 114)
            or not any(unicodedata.east_asian_width(c) in "WF" for c in token)):
        return None
    suffix = token[len(content[1]):]
    suffix_cells = _display_cells(suffix)
    sha_token = rest[end + 1:].split(" ", 1)[0]
    if border_cells == 105:
        if (suffix_cells not in (56, 60) or content[2] != suffix
                or content[3] != sha_token + " " * 11
                or content[4:] != _TASK_PACK_NOTICE_SUFFIX[1:].rsplit(" ", 1)):
            return None
        return [content[0], content[1], suffix, sha_token, *content[4:]]
    padding = {44: 52, 52: 44, 45: 27, 47: 49}.get(suffix_cells)
    if (padding is None or content[2] != suffix + " " * padding
            or not content[3].startswith(sha_token + " ")):
        return None
    return [content[0], content[1], suffix, *content[3:]]


def _claude_reference_padding_candidate(content, text, border_cells):
    """One measured V2 BODY-row rendering; never trim arbitrary draft text.

    A/B's 183-cell bordered editor adds 16/17 ASCII cells after the complete
    BODY token when the next SHA256 word wraps. This is display equivalence,
    not native editor-byte proof. The caller still checks every other character
    and both wrap boundaries. _draft_structure retains the original raw rows.
    """
    if border_cells in (105, 118):
        return _claude_task_pack_padding_candidate(content, text, border_cells)
    if border_cells == 180:
        # Claude 2.1.303 renders four complete ASCII reference rows, with
        # twelve/thirteen BODY-row cells. Observed 4/3-digit BYTES forms and
        # long maintenance markers retain the complete original wire text.
        from cmux_prompt_reference import SINGLE_LINE_FORMAT, wire_reference
        try:
            ref = wire_reference(text)
        except ValueError:
            return None
        if (not ref or ref['format'] != SINGLE_LINE_FORMAT
                or not text.isascii() or len(content) != 4):
            return None
        token = 'BODY=' + json.dumps(ref['path'], ensure_ascii=False)
        header, separator, tail = text.partition(' ' + token + ' ')
        notice = ('Native receipt proves this notice only; body reading and '
                  'acceptance are separate.')
        third, boundary, remainder = tail.partition(' ' + notice)
        if (not separator or not boundary or remainder
                or len(token) != 144 or len(third) not in (172, 173)
                or not third.startswith(f"SHA256={ref['sha256']} BYTES={ref['bytes']} ")
                or content != [header, token + ' ' * {172: 13, 173: 12}[len(third)], third, notice]):
            return None
        return [header, token, third, notice]
    if border_cells != 183 or len(content) < 3:
        return None
    from cmux_prompt_reference import SINGLE_LINE_FORMAT, wire_reference
    try:
        ref = wire_reference(text)
    except ValueError:
        return None
    if not ref or ref['format'] != SINGLE_LINE_FORMAT:
        return None
    token = 'BODY=' + json.dumps(ref['path'], ensure_ascii=False)
    header, separator, _ = text.partition(' ' + token + ' ')
    if (not separator or content[0] != header
            or content[1] not in (token + ' ' * 16, token + ' ' * 17)
            or not content[2].startswith(f"SHA256={ref['sha256']} BYTES={ref['bytes']} ")):
        return None
    return [content[0], token, *content[2:]]


def _claude_status_padding_candidate(content, text, border_cells):
    """Measured Claude 2.1.303 STATUS geometry, with full outer text matching.

    The 134-cell editor hard-wraps at 130, then pads a 93-cell continuation
    by 18 cells before a 128-cell row. Preserve every other character and
    all raw rows for stability. This is display equivalence only.
    """
    if (border_cells != 134 or len(content) != 4 or not text.isascii()
            or not re.match(r'^STATUS: [A-Za-z0-9_.:-]+ ', text)
            or len(content[0]) != 130 or len(content[1]) != 111
            or len(content[2]) != 128 or not content[1].endswith(' ' * 18)
            or content[1][-19].isspace()):
        return None
    return [content[0], content[1][:-18], *content[2:]]


def _require_exact_composer_without_cursor_cell(screen, surface, text):
    """Require the expected visible draft, allowing known bordered hard wrapping.

    Only the renderer's two-column continuation gutter and measured wrap width
    may be added. Do not collapse whitespace or accept truncated/paste summaries.
    Screen evidence cannot distinguish buffers with identical visual rendering.
    """
    require_agent_input(screen, surface)
    rows = screen.splitlines()
    start = next((i for i in range(len(rows)-1, -1, -1)
                  if _PROMPT_GLYPH_RE.match(rows[i])), None)
    if start is None:
        raise DispatchUnconfirmed("COMPOSE_UNVERIFIED: no Enter", state=COMPOSE_OCCUPIED)
    # Remove the renderer's single prompt separator, never user whitespace.
    first = re.sub(r"^\s*[›❯][ \u00a0]?", "", rows[start], count=1)
    bordered = start > 0 and re.fullmatch(r"─{8,}", rows[start-1])
    if bordered:
        end = next((i for i in range(start+1, len(rows))
                    if rows[i] == rows[start-1]), None)
        if end is None:
            raise DispatchUnconfirmed("COMPOSE_TRUNCATED: no Enter", state=COMPOSE_OCCUPIED)
        body = "\n".join([first] + rows[start+1:end])
    else:
        lines = [first] + rows[start+1:]
        # Remove only terminal chrome, never similar lines inside the draft.
        if len(lines) > 2 and _PROVIDER_HINT_ROW_RE.fullmatch(lines[-1]):
            lines.pop()
        has_model_footer = len(lines) > 1 and re.fullmatch(
                r"\s*(?:\[(?:Opus|Claude|Sonnet|GPT)[^\]]*\](?: context \d+%)?|"
                r"(?:GPT|claude)-[\w.-]+(?: (?:low|medium|high|xhigh|max|ultra))?"
                r"(?: · [^\n]+)?)\s*",
                lines[-1], re.I)
        if has_model_footer:
            lines.pop()
        body = "\n".join(lines)
        bodies = [body]
        # Measured Codex footer gaps: one or two blank UI rows immediately
        # before verified model chrome. Keep all payload whitespace; each
        # candidate must still match the entire expected message. Never trim
        # an arbitrary blank suffix or infer chrome from unknown trailing text.
        if has_model_footer:
            for gap in (1, 2):
                if len(lines) > gap and all(
                        row == "" or (row == " " and rows[start].startswith("› "))
                        for row in lines[-gap:]):
                    bodies.append("\n".join(lines[:-gap]))
        for candidate in bodies:
            if candidate == text:
                return
            parts = candidate.split("\n")
            if not all(row.startswith("  ") for row in parts[1:]):
                continue
            # Preserve every content character. Only the two-column gutter
            # and hard/soft visual row boundaries are renderer-dependent.
            # Measured 2026-10-08 (C2596 r22 status): Codex word-wraps at a
            # space and drops that space from both rows, so "only because"
            # renders as "...only" / "  because...". A single " " separator is
            # accepted only where the previous row is wide enough that the
            # next word could not have fit on it (a real word wrap).
            widest = max((len(row) for row in rows), default=0)
            positions = {0}
            previous = ""
            for number, part in enumerate(parts):
                part = part if number == 0 else part[2:]
                next_positions = set()
                word = re.match(r"\S+", part) if part else None
                wrapped = bool(
                    number and word and previous and not previous[-1].isspace()
                    and 2 + _display_cells(previous) + 1 + _display_cells(word.group())
                    > max(widest - 2, 40))
                for pos in positions:
                    # Empty continuation rows are explicit blank content,
                    # never a soft wrap that can silently disappear.
                    separators = (("",) if number == 0 else
                                  (("", "\n", " ") if wrapped else ("", "\n"))
                                  if part else ("\n",))
                    for sep in separators:
                        chunk = sep + part
                        if text.startswith(chunk, pos):
                            next_positions.add(pos + len(chunk))
                positions = next_positions
                previous = part
            if len(text) in positions:
                return
    if body == text:
        return
    if bordered:
        # Claude's editor reserves two additional cursor cells in some builds.
        # Word wrapping can consume exactly one separator at a visual boundary.
        # Match complete content against measured geometry; never strip user
        # whitespace or treat a paste summary as the original draft.
        parts = body.split("\n")
        if all(row.startswith("  ") for row in parts[1:]):
            content = [parts[0]] + [row[2:] for row in parts[1:]]
            def cells(value):
                return sum(0 if unicodedata.combining(c) else
                           (2 if unicodedata.east_asian_width(c) in "WF" else 1)
                           for c in value)
            if not any(unicodedata.category(c).startswith("C")
                       for row in content for c in row):
                layouts = [content]
                candidate = _claude_reference_padding_candidate(content, text, len(rows[start-1]))
                if candidate is not None:
                    layouts.append(candidate)
                candidate = _claude_status_padding_candidate(content, text, len(rows[start-1]))
                if candidate is not None:
                    layouts.append(candidate)
                for layout, width in ((layout, width) for layout in layouts
                                      for width in (len(rows[start-1]) - 4, len(rows[start-1]) - 2)):
                    positions = {0}
                    previous = ""
                    for number, part in enumerate(layout):
                        if cells(part) > width:
                            positions = set()
                            break
                        separators = ("",) if number == 0 else ("\n",)
                        if number and previous and part:
                            if cells(previous) + cells(part[0]) > width:
                                separators += ("",)
                            word = re.match(r"[^\s]+", part)
                            # Measured Claude editor: a word that would exactly
                            # fill the available cells starts the next row too.
                            # Consume only this one boundary separator; never
                            # normalize the payload or any other whitespace.
                            if (word and not previous[-1].isspace()
                                    and cells(previous) + 1 + cells(word.group()) >= width):
                                separators += (" ",)
                        positions = {pos + len(sep) + len(part)
                                     for pos in positions for sep in separators
                                     if text.startswith(sep + part, pos)}
                        previous = part
                    if len(text) in positions:
                        return
    # Claude's bordered editor uses a two-column prompt/continuation gutter.
    # Preserve explicit newlines, spaces and Unicode; generate expected rows
    # rather than deleting arbitrary whitespace from the observed draft.
    if bordered:
        width = len(rows[start-1]) - 2
        expected = []
        for line in text.split("\n"):
            chunk, cells = "", 0
            for char in line:
                if unicodedata.category(char).startswith("C"):
                    raise DispatchUnconfirmed("COMPOSE_CONTROL_CHARACTER: no Enter", state=COMPOSE_OCCUPIED)
                size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
                if cells + size > width:
                    expected.append(chunk); chunk, cells = "", 0
                chunk += char; cells += size
            expected.append(chunk)
        rendered = expected[0] + "".join("\n  " + row for row in expected[1:])
        if body == rendered:
            return
    if body != text:
        raise DispatchUnconfirmed("COMPOSE_CHANGED_OR_UNVERIFIED: preserve draft; no Enter",
                                  state=COMPOSE_OCCUPIED)



def _exact_pending_text(screen, text):
    """Require full visible payload; only a final explicit queue hint is chrome."""
    rows = screen.splitlines()
    if rows and re.fullmatch(r"\s*tab to queue message\s*", rows[-1]):
        rows.pop()
    try:
        require_exact_composer("\n".join(rows), "receiver", text)
    except DispatchUnconfirmed:
        return False
    return True


def _draft_structure(screen, text):
    """保留全部草稿行、空格和空行；footer 文字可更新，结构不可猜测。"""
    if not _exact_pending_text(screen, text):
        raise DispatchUnconfirmed("COMPOSE_CHANGED: no recovery key", state=COMPOSE_OCCUPIED)
    rows = screen.splitlines()
    start = next(i for i in range(len(rows) - 1, -1, -1)
                 if _PROMPT_GLYPH_RE.match(rows[i]))
    border = rows[start - 1] if start and re.fullmatch(r"─{8,}", rows[start - 1]) else None
    if border is not None:
        end = next(i for i in range(start + 1, len(rows)) if rows[i] == border)
        return (border, tuple(rows[start:end]))
    body = rows[start:]
    if body and re.fullmatch(r"\s*tab to queue message\s*", body[-1]):
        body.pop()
    if len(body) > 2 and _PROVIDER_HINT_ROW_RE.fullmatch(body[-1]):
        body.pop()
    if len(body) > 1 and re.fullmatch(
            r"\s*(?:\[(?:Opus|Claude|Sonnet|GPT)[^\]]*\](?: context \d+%)?|"
            r"(?:GPT|claude)-[\w.-]+(?: (?:low|medium|high|xhigh|max|ultra))?"
            r"(?: · [^\n]+)?)\s*", body[-1], re.I):
        body.pop()
    return (None, tuple(body))


def require_original_draft(attempt, screen, text):
    """原 ENTER 后任何不确定或结构变化都撤销补键资格；恢复原样不复权。"""
    events = attempt.get("events", [])
    if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
        raise TaskPackContractError("ORIGINAL_ENTER_EVIDENCE_REQUIRED")
    original = [e for e in events if e.get("phase") == "ENTER_INTENT"]
    if len(original) != 1 or original[0].get("screen_sha256") != screen_hash(original[0].get("screen", "")):
        raise TaskPackContractError("ORIGINAL_ENTER_EVIDENCE_REQUIRED")
    expected = _draft_structure(original[0]["screen"], text)
    samples = [e for e in events[events.index(original[0])+1:] if "screen" in e]
    for event in samples:
        if (not isinstance(event["screen"], str)
                or event.get("screen_sha256") != screen_hash(event["screen"])):
            raise TaskPackContractError("ORIGINAL_ENTER_EVIDENCE_CHANGED")
    for observed in [original[0]["screen"], *(e["screen"] for e in samples), screen]:
        if pending_queue_holds(observed, text):
            raise DispatchUnconfirmed("ALREADY_QUEUED: no recovery key",
                                      state=DELIVERY_QUEUED_AT_RECEIVER)
        if receiver_cannot_submit_now(observed) or _draft_structure(observed, text) != expected:
            raise DispatchUnconfirmed("COMPOSE_STRUCTURE_CHANGED: preserve draft; no recovery key",
                                      state=COMPOSE_OCCUPIED)
    return expected


_RECEIVER_UNSUBMITTABLE_RE = re.compile(r"Compacting (?:context|conversation)|Reconnecting", re.I)


def _current_status_region(screen):
    """Text from the receiver's latest status bullet down to its composer.

    Measured 2026-10-08: a finished "• Compacting context" block stays in
    scrollback above later "• Working" turns. Judging the whole screen made Tab
    permanently unavailable and stranded a pasted payload in the composer.
    Without a status bullet the whole pre-composer screen is used (fail closed).

    Measured 2026-10-09 01:15 +0800 (EXECUTOR_READY_73c025c0): Codex draws its
    "• Queued follow-up inputs" block between the status bullet and the
    composer. Taking that header as the status hid "• Compacting context", so
    every compaction gate passed and the payload was pasted and Entered into a
    compacting receiver. The live queue block is the last top-level bullet
    above the composer; it is cut off and the status is searched above it.
    Queued payload rows are indented, so their text never becomes the status.
    """
    # Claude reports can quote Codex status bullets and then render a completed
    # turn summary followed by a multiline recap. Only its latest top-level
    # activity row is current chrome; neither the report nor recap is a Codex
    # status region. The full bordered editor/footer authenticates this branch.
    claude = _claude_bordered_compose(screen)
    if claude is not None:
        row = next((row for row in reversed(claude["before"])
                    if re.match(r"^(?:⏺|✻|✢|✳|✶|✽|◐|◑|◒|◓)(?:\s|$)", row)), "")
        return row if not row.startswith("⏺") else ""
    rows = screen.splitlines()
    start = next((i for i in range(len(rows) - 1, -1, -1)
                  if _PROMPT_GLYPH_RE.match(rows[i])), len(rows))
    top = next((i for i in range(start - 1, -1, -1)
                if re.match(r"^•\s", rows[i])), None)
    end = top if top is not None and (_QUEUED_FOLLOWUP_HEADER_RE.match(rows[top])
                                      or _PENDING_QUEUE_RE.search(rows[top])) else start
    bullet = next((i for i in range(end - 1, -1, -1)
                   if re.match(r"^\s*•\s", rows[i])), 0)
    return "\n".join(rows[bullet:end])


def receiver_cannot_submit_now(screen):
    """Codex is compacting/reconnecting: neither Enter nor Tab submits a draft."""
    return bool(_RECEIVER_UNSUBMITTABLE_RE.search(_current_status_region(screen)))


def _compose_is_partial_payload(screen, text):
    """The composer shows a strict prefix of our payload: still rendering.

    Only decides whether to re-read (never authorizes a key), so the draft is
    taken as the rows from the last prompt glyph up to the first blank row.
    """
    rows = screen.splitlines()
    start = next((i for i in range(len(rows) - 1, -1, -1)
                  if _PROMPT_GLYPH_RE.match(rows[i])), None)
    if start is None:
        return False
    draft = [re.sub(r"^\s*[›❯][  ]?", "", rows[start], count=1)]
    for row in rows[start + 1:]:
        if not row.strip():
            break
        draft.append(row)
    shown, full = "".join("".join(draft).split()), "".join(text.split())
    return bool(shown) and shown != full and full.startswith(shown)


def _codex_tab_queue_allowed(screen, text):
    """Only the measured Codex busy composer with its explicit queue key."""
    return bool(
        receiver_input_kind(screen) == "AGENT_TUI"
        and re.search(r"(?m)^\s*tab to queue message\s*$", screen)
        and re.search(r"(?m)^\s*›(?:\s|$)", screen)
        and _exact_pending_text(screen, text)
        and not receiver_cannot_submit_now(screen)
        and not pending_queue_holds(screen, text)
    )


def _screen_consumption_candidate(before, after, marker, text=None):
    # A reused/stale marker followed by unrelated new activity is not proof
    # of this submission. Nonces must be fresh relative to the pre-paste view.
    from cmux_delivery_evidence import confirmed
    return bool(
        marker and text and not compose_contains(before, marker)
        and "".join(marker.split()) not in "".join(before.split())
        and receiver_input_kind(after) == "AGENT_TUI"
        and compose_block_is_empty(after)
        and not compose_contains(after, marker)
        and not pending_queue_holds(after, marker)
        and _submission_confirmed(after, marker)
        and _new_activity_after_submit(before, after)
        and confirmed(sys.modules[__name__], before, after, marker, text)
    )


def _delivery_confirmed(before, after, marker, text=None, *, native_binding=None, native_proof=None):
    # 兼容读取接口也必须带原生回执；屏幕只能用于判断输入所有权。
    from cmux_native_delivery import validate_proof
    return bool(marker and text and native_binding and native_proof
                and validate_proof(native_binding, text, native_proof))


def _new_activity_after_submit(before, after):
    """Prove progress when the delivery marker scrolled out of view."""
    if before == after:
        return False
    before_lines = Counter(
        line for line in before.splitlines() if _ACTIVITY_LINE_RE.match(line)
    )
    after_lines = Counter(
        line for line in after.splitlines() if _ACTIVITY_LINE_RE.match(line)
    )
    return any(after_lines[line] > before_lines[line] for line in after_lines)


def _queued_or_active_input(screen):
    """Do not issue a blind second Enter while a queued/active task owns the TUI."""
    claude = _claude_bordered_compose(screen)
    if claude is None and _claude_framed_prompt(screen):
        # require_agent_input reports COMPOSE_UNPARSED. Do not attribute an
        # unknown current UI to a historical completed summary containing
        # words such as "1 monitor still running".
        return False
    queue_region = "\n".join(claude["before"]) if claude is not None else screen
    if re.search(r"^[ \t]*Messages? to be submitted after|Press up to edit queued messages", queue_region, re.I | re.M):
        return True
    if claude is not None:
        if any(_CLAUDE_ACTIVE_TOOL_RE.fullmatch(row) for row in claude["footer"]):
            return True
        # 活动只读当前边框前的状态，防止历史 spinner 或输入正文冒充活动。
        # Claude may render a Tip below the current spinner. It is a hint,
        # not a replacement status; composer text is never filtered here.
        before = list(claude["before"])
        while before and (not before[-1].strip()
                          or _CLAUDE_GOAL_ROW_RE.fullmatch(before[-1])
                          or re.fullmatch(r"\s*⎿\s+Tip:[ \t]+[^\r\n]+", before[-1])):
            before.pop()
        screen = before[-1] if before else ""
        # Live spinners can omit elapsed time. Match an activity label, not
        # prose such as '· Note: see README...'. Tool headers have no space
        # between their identifier and '(', unlike '⏺ Done (12 tests pass).'.
        if re.fullmatch(r"\s*[·✻✢✳✶✽◐◑◒◓]\s+(?:[A-Za-z][A-Za-z-]*|Compacting (?:conversation|context))(?:…|\.\.\.)(?:\s*\([^\n]+\))?\s*", screen):
            return True
        if re.match(r"\s*⏺\s+[A-Za-z_][A-Za-z0-9_.:/-]*\(", screen):
            return True
        if re.fullmatch(r"\s*⎿\s+Running(?:(?:…|\.\.\.)(?:\s*\([^\n]+\))?)?\s*", screen):
            return True
        return bool(re.match(
            r"\s*[✻✢✳✶✽◐◑◒◓]\s+(?:thinking|running|waiting|retrying|"
            r"recording|checking|working|processing|executing|loading|tool|bash:|api error)\b",
            screen, re.I))
    if re.search(r"^[ \t]*•\s+(?:Working|Thinking|Running|Compacting context)\s+\([^\n)]*\besc to interrupt\b", screen, re.I | re.M):
        return True
    # Claude uses the same glyph for active and completed summaries. Restrict
    # this guard to words that identify live work; "Churned/Brewed/Cooked"
    # summaries must not strand the next prompt in compose.
    active_words = (
        r"thinking|running|waiting|retrying|recording|checking|working|"
        r"processing|executing|loading|tool|bash:|api error"
    )
    return bool(
        re.search(
            rf"^[ \t]*(?:✻|✢|✳|✶|✽|◐|◑|◒|◓)[ \t]+.*(?:{active_words})\b",
            screen,
            re.I | re.M,
        )
    )


def send_text(surface, text):
    """原样粘贴 Unicode、字面转义和真实换行，显式禁止隐式提交。

    terminal.paste 的 submit_key=none 只粘贴；失败不得回退到会解码
    字面反斜杠 n/r/t 的 CLI send。RPC 成功只证明传输接受，不是入站回执。
    """
    if not isinstance(text, str) or not text:
        raise ValueError("nonempty paste text required")
    proof = pin_workspace(surface)
    _run("rpc", "terminal.paste", json.dumps(dict(
        text=text, submit_key="none", workspace_id=proof["workspace_uuid"],
        surface_id=proof["target_surface_uuid"]), ensure_ascii=False))


def send_key(surface, key):
    """
    Send a key event (e.g. 'enter', 'ctrl+c', 'ctrl+u') to a surface.
    """
    _run("send-key", "--surface", surface, "--", key)


def protected_submit_result(raw):
    from cmux_direct_input import protected_submit_result as parse
    return parse(raw)


def require_prompt_free(surface, *, provider=None):
    from cmux_direct_input import require_prompt_state
    proof = pin_workspace(surface)
    target = proof['target_surface_uuid']
    workspace = proof['workspace_uuid']
    return require_prompt_state(_run('rpc', 'surface.input_state', json.dumps(dict(
        surface_id=target, workspace_id=workspace))), target, workspace,
        read_claude_screen=(lambda: read_screen(surface)) if provider == 'claude' else None)


def submit_text_protected(surface, text):
    """One draft-protected paste/submit command; never follow failure with a key.

    --force is deliberately absent. The CLI checks the original target prompt;
    its host handles the literal paste and agent-aware return in one request.
    Busy work/Goal are not our admission gates. Native observation still decides
    reception. Historical controllers keep their original send_text path.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("nonempty paste text required")
    if any(ord(char) < 32 and char not in "\t\r\n" or ord(char) == 127 for char in text):
        raise ValueError("PASTE_CONTROL_CHARACTER_WOULD_CHANGE: zero input")
    return protected_submit_result(_run("paste", "--submit", "--surface", surface,
                                       "--", text, json_output=True))


def focus_surface(surface):
    """Focus the pane owning ``surface`` before sending terminal editing keys."""
    pin_workspace(surface)
    identity = identify_surface(surface)
    pane = identity.get("pane_ref")
    if pane:
        _run("focus-pane", "--pane", pane)


def clear_known_compose_by_delete(surface, compose):
    """Backspace one fingerprinted idle compose buffer, then let caller verify.

    Claude's multiline editor has ignored Escape, ctrl+u, and ctrl+c in real
    sessions. Its terminal also ignores forward Delete even after Home, while
    End+Backspace edits the actual buffer. The force-compose path already owns
    the exact buffer, so erase a bounded number of trailing cells. The
    subsequent screen read remains the acceptance authority.
    """
    focus_surface(surface)
    send_key(surface, "end")
    count = min(max(len(compose) + 64, 256), 4096)
    for _ in range(count):
        send_key(surface, "backspace")
    return count


def receiver_input_kind(screen):
    """Classify the current input area; transcript/model history is not liveness.

    A bare prompt after a CLI exits must win over any old agent banner. Fancy
    shell prompts using the same glyph as Claude remain UNKNOWN without current
    UI chrome. This observation does not establish task identity or acceptance.
    """
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", screen)
    lines = [line.rstrip() for line in clean.splitlines() if line.strip()]
    if not lines:
        return "UNKNOWN"
    last = lines[-1].strip()
    shell = (r"^(?:\([^\n)]*\)\s*)?[\w.-]+@[\w.-]+[^\n]*?[%$#](?:\s|$)",
             r"^(?:bash|zsh|sh)(?:-[\d.]+)?[$#](?:\s|$)",
             r"^PS\s+(?:[A-Za-z]:[\\/]|/)[^\n]*>(?:\s|$)",
             r"^(?:[A-Za-z]:\\[^\n]*>|[%$#])\s*$")
    if any(re.match(pattern, last) for pattern in shell):
        return "SHELL"
    prompts = [i for i, line in enumerate(lines) if _PROMPT_GLYPH_RE.match(line)]
    chrome = re.compile(r"^\s*(?:GPT-[\w.-]+|claude-[\w.-]+|(?:Opus|Sonnet)\s+[\d.]+|\[(?:Opus|Claude|Sonnet|GPT)[^\]]*\])(?:\s|$)", re.I)
    if prompts:
        tail = lines[prompts[-1]:]
        # Claude's current idle UI has a bordered editor followed by several
        # status rows. Bind this case to both borders and the complete known
        # footer; an old model banner or unknown trailing row is insufficient.
        if _claude_bordered_compose(clean) is not None:
            return "AGENT_TUI"
        if (tail[0].lstrip().startswith("❯") and
                any(_CLAUDE_BORDER_RE.fullmatch(row.strip())
                    for row in lines[max(prompts[-1] - 1, 0):])):
            return "UNKNOWN"
        # A historical footer cannot override unknown current input below it.
        # Exactly one measured exception: the provider hint row renders BELOW
        # the model row, so skip that single known row (and nothing else)
        # before looking for model chrome. Any other trailing content still
        # wins, keeping the fail-closed direction intact.
        footer_tail = tail
        if (len(footer_tail) > 2
                and _PROVIDER_HINT_ROW_RE.fullmatch(footer_tail[-1])):
            footer_tail = footer_tail[:-1]
        if len(footer_tail) > 1 and chrome.search(footer_tail[-1]):
            return "AGENT_TUI"
        if len(tail) == 1 and re.fullmatch(r"\s*[›❯]\s*Ask (?:Codex|Claude) to do anything\s*", tail[0]):
            return "AGENT_TUI"
    elif chrome.search(lines[-1]) and any(_ACTIVITY_LINE_RE.match(line) for line in lines[-8:]):
        return "AGENT_TUI"
    return "UNKNOWN"


def require_agent_input(screen, surface):
    kind = receiver_input_kind(screen)
    if kind != "AGENT_TUI":
        if kind == "UNKNOWN" and _claude_framed_prompt(screen):
            raise DispatchUnconfirmed(
                f"COMPOSE_UNPARSED surface={surface}; unknown Claude frame/footer; "
                "preserve original screen and draft; no input sent", state=COMPOSE_UNPARSED)
        raise DispatchUnconfirmed(
            f"AGENT_INPUT_REQUIRED surface={surface} receiver_kind={kind}; "
            "message not submitted; stop further input and inspect the original session",
            state=SUPERVISOR_DID_NOT_SUBMIT,
        )


def require_clearable_agent_input(screen, surface):
    """Recheck a fresh observation before any further compose-clearing keys."""
    require_agent_input(screen, surface)
    if _queued_or_active_input(screen):
        raise DispatchUnconfirmed(
            f"COMPOSE_OCCUPIED surface={surface}; active/queued work cannot be cleared",
            state=COMPOSE_OCCUPIED,
        )


def submit_text(surface, text, marker=None, confirm_lines=200, task_pack_path=None,
                force_compose=False, delivery_observer=None, *, reconcile_only=False,
                recover_stranded=False, native_binding=None, native_attempt=None,
                wait_seconds=None):
    """公开发送统一落原 journal；屏幕变化和按键返回不构成送达。"""
    from cmux_confirmation import wait_budget
    seconds = wait_budget(wait_seconds, reconcile_only=reconcile_only)
    if recover_stranded and (task_pack_path is not None or delivery_observer is not None):
        raise TaskPackContractError('RECOVER_STRANDED_MESSAGE_ONLY')
    if delivery_observer is not None:
        if reconcile_only:
            raise TaskPackContractError('RECONCILE_WITH_ORIGINAL_CONTROLLER')
        return _submit_text_once(surface, text, marker, confirm_lines, task_pack_path,
                                 force_compose, delivery_observer,
                                 native_binding=native_binding, native_attempt=native_attempt,
                                 wait_seconds=seconds)
    if task_pack_path is not None:
        return submit_task_pack(surface, text, task_pack_path, marker, confirm_lines,
                                force_compose, reconcile_only=reconcile_only, wait_seconds=seconds)
    if force_compose:
        raise TaskPackContractError('MESSAGE_PRESERVE_COMPOSE: no forced replacement')
    from cmux_message_journal import deliver
    return deliver(sys.modules[__name__], surface, text, marker, confirm_lines,
                   reconcile_only=reconcile_only, recover_stranded=recover_stranded,
                   wait_seconds=seconds)


def _delivery_delay(name, default, maximum=5.0):
    try:
        value = float(os.environ.get(name, str(default)))
        return max(0.0, min(maximum, value)) if math.isfinite(value) else default
    except ValueError:
        return default


def _observe_native(surface, text, native_binding, native_attempt):
    from cmux_native_delivery import scan_original
    if native_attempt is None:
        raise TaskPackContractError('ORIGINAL_NATIVE_JOURNAL_REQUIRED')
    return scan_original(sys.modules[__name__], surface, text, native_attempt, native_binding)


def _stable_owned_draft(surface, text, confirm_lines, observer, *, expected_structure=None):
    """只读等待两次完整草稿；截断、粘贴摘要、压缩和外来草稿都不授权按键。"""
    count = 0
    previous = None
    screen = ""
    reads = 20
    while reads:
        reads -= 1
        time.sleep(_delivery_delay("CMUX_AGENT_SUBMIT_DELAY", 0.25, 1.0))
        screen = read_screen(surface, lines=confirm_lines)
        if observer:
            observer("DRAFT_OBSERVATION", screen)
        if expected_structure is not None and pending_queue_holds(screen, text):
            raise DispatchUnconfirmed("ALREADY_QUEUED: no recovery key",
                                      state=DELIVERY_QUEUED_AT_RECEIVER)
        if not receiver_cannot_submit_now(screen) and _exact_pending_text(screen, text):
            current = _draft_structure(screen, text)
            if expected_structure is not None and current != expected_structure:
                raise DispatchUnconfirmed("COMPOSE_STRUCTURE_CHANGED: no recovery key",
                                          state=COMPOSE_OCCUPIED)
            count = count + 1 if current == previous else 1
            previous = current
            if count == 2:
                return screen
        else:
            if expected_structure is not None:
                raise DispatchUnconfirmed("COMPOSE_OWNERSHIP_REVOKED: no recovery key",
                                          state=COMPOSE_OCCUPIED)
            count = 0
            previous = None
    raise DispatchUnconfirmed(
        "DRAFT_NOT_READY: original paste preserved; no submission key sent",
        state=STRANDED_IN_COMPOSE)


def _wait_native_after_key(surface, text, marker, confirm_lines, observer,
                           native_binding, native_attempt, observed, wait_seconds=None):
    """有限等待同一次输入的原生记录；不重贴、不发键、不按静屏推定失败。"""
    from cmux_confirmation import observe_receipt
    screen = ""
    def observe():
        nonlocal screen
        screen = read_screen(surface, lines=confirm_lines)
        if observer:
            observer(observed, screen)
        result = _observe_native(surface, text, native_binding, native_attempt)
        if pending_queue_holds(screen, marker):
            result = dict(result, queued_at_receiver=True)
        return result
    result = observe_receipt(observe, wait_seconds, stop_when_queued=True)
    return screen, result


def _unconfirmed_native(screen, marker, result):
    if result.get("state") == "NATIVE_QUEUED" or pending_queue_holds(screen, marker):
        state = DELIVERY_QUEUED_AT_RECEIVER
    elif _prompt_block_pending(screen, marker):
        state = STRANDED_IN_COMPOSE
    else:
        state = DELIVERY_UNVERIFIED_BY_DETECTOR
    raise DispatchUnconfirmed(
        "NATIVE_DELIVERY_UNCONFIRMED: original attempt preserved; key/queue is not reception; "
        "reconcile original without repasting", state=state, details=result)


def _submit_text_once(surface, text, marker=None, confirm_lines=200, task_pack_path=None,
                      force_compose=False, delivery_observer=None, *,
                      native_binding=None, native_attempt=None, wait_seconds=None):
    """Codex 原生直投；Claude 保留一次粘贴。唯有原生 user 记录证明接收。"""
    from cmux_native_delivery import require_bound
    from cmux_confirmation import wait_budget
    seconds = wait_budget(wait_seconds)
    _validate_submission(surface, text, task_pack_path, force_compose,
                         delivery_observer, native_binding, native_attempt)
    def terminal():
        from cmux_direct_input import send_once as direct_send
        return direct_send(text, marker,
            preflight=lambda: require_prompt_free(surface, provider=native_binding.get('provider')),
            recheck=lambda: require_bound(sys.modules[__name__], surface, native_binding, text),
            emit=delivery_observer, paste=lambda value: send_text(surface, value),
            submit=lambda value: submit_text_protected(surface, value),
            key=lambda value: send_key(surface, value),
            observe=lambda: _observe_native(surface, text, native_binding, native_attempt),
            read_screen=lambda: read_screen(surface, lines=confirm_lines), wait_seconds=seconds)
    current = native_binding.get("receipt_contract") == "native-user-marker-v1"
    if native_binding["provider"] == "codex":
        from cmux_codex_input import send_once
        return send_once(native_binding, text,
            recheck=lambda: require_bound(sys.modules[__name__], surface, native_binding, text),
            emit=delivery_observer,
            observe=lambda: _observe_native(surface, text, native_binding, native_attempt),
            wait_seconds=seconds, **({"fallback": terminal} if current else {}))
    if current:
        return terminal()
    return _submit_terminal_once(surface, text, marker, confirm_lines, task_pack_path,
        force_compose, delivery_observer, native_binding=native_binding,
        native_attempt=native_attempt, wait_seconds=seconds)


def _validate_submission(surface, text, task_pack_path, force_compose,
                         delivery_observer, native_binding, native_attempt):
    """Shared contract for current input and offline historical fixtures."""
    from cmux_native_delivery import require_bound
    if native_attempt is None or delivery_observer is None:
        raise TaskPackContractError("ORIGINAL_NATIVE_JOURNAL_REQUIRED")
    if force_compose:
        raise TaskPackContractError("NATIVE_DELIVERY_PRESERVE_COMPOSER")
    if task_pack_path is not None:
        pack = validate_task_pack_contract(task_pack_path, prompt_text=text)
        from availability_contract import require_action
        require_action(pack["task_id"], "dispatch", pack)
    elif _looks_like_task_dispatch(text):
        raise TaskPackContractError("TASK_PACK_REQUIRED")
    # One gate covers ordinary messages, formal tasks and exact callbacks.
    # Historical read-only reconciliation never enters this new-paste path.
    from cmux_prompt_reference import require_inline
    require_inline(text)
    require_bound(sys.modules[__name__], surface, native_binding, text)


def _submit_terminal_once(surface, text, marker=None, confirm_lines=200, task_pack_path=None,
                          force_compose=False, delivery_observer=None, *,
                          native_binding=None, native_attempt=None, wait_seconds=None):
    """Claude terminal path; old Codex attempts are exercised only by fixtures."""
    from cmux_native_delivery import require_bound
    from cmux_confirmation import wait_budget
    seconds = wait_budget(wait_seconds)
    before = read_screen(surface, lines=confirm_lines)
    require_agent_input(before, surface)
    if not compose_block_is_empty(before):
        raise DispatchUnconfirmed("COMPOSE_OCCUPIED: preserve existing text; no input sent",
                                  state=COMPOSE_OCCUPIED)
    if receiver_cannot_submit_now(before):
        raise DispatchUnconfirmed("RECEIVER_COMPACTING: no input sent",
                                  state=SUBMISSION_ABORTED_BUSY)
    delivery_observer("PASTE_INTENT", before)
    require_bound(sys.modules[__name__], surface, native_binding, text)
    send_text(surface, text)
    delivery_observer("PASTED")
    screen = _stable_owned_draft(surface, text, confirm_lines, delivery_observer)

    # 忙碌 Codex 显示 Tab 排队时直接使用该动作；不先用 Enter 向草稿插入换行。
    queue = _codex_tab_queue_allowed(screen, text)
    key = "tab" if queue else "enter"
    intent = "QUEUE_TAB_INTENT" if queue else "ENTER_INTENT"
    observed = "POST_QUEUE_TAB_OBSERVATION" if queue else "POST_ENTER_OBSERVATION"
    if _queued_or_active_input(screen) and not queue and native_binding['provider'] == 'codex':
        raise DispatchUnconfirmed("STRANDED_IN_COMPOSE: no supported submit action",
                                  state=STRANDED_IN_COMPOSE)
    delivery_observer(intent, screen)
    require_bound(sys.modules[__name__], surface, native_binding, text)
    send_key(surface, key)
    if not queue:
        delivery_observer("ENTER_SENT")
    screen, result = _wait_native_after_key(surface, text, marker, confirm_lines,
        delivery_observer, native_binding, native_attempt, observed, seconds)
    if result.get("confirmed") is True:
        return dict(result, retries=0, submission_key=key)
    if queue or pending_queue_holds(screen, marker) or result.get("state") == "NATIVE_QUEUED":
        _unconfirmed_native(screen, marker, result)

    # 首次提交只有一个键。Enter 可能插入换行，未见原生记录不能自动补键。
    _unconfirmed_native(screen, marker, result)


def recover_stranded_once(surface, text, marker, before, confirm_lines=200,
                          delivery_observer=None, wait_seconds=None, *,
                          native_binding=None, native_attempt=None):
    """原 attempt 最多补一个键；原生绑定缺失不允许补造，也不重新粘贴。"""
    from cmux_native_delivery import require_bound
    from cmux_confirmation import wait_budget
    seconds = wait_budget(wait_seconds)
    if native_attempt is None or delivery_observer is None:
        raise TaskPackContractError("ORIGINAL_NATIVE_JOURNAL_REQUIRED")
    original = json.loads(Path(native_attempt).read_text())
    if (native_binding or {}).get("receipt_contract") == "native-user-marker-v1":
        from cmux_direct_input import recover_once
        result = recover_once(text, marker, original=original,
            recheck=lambda: require_bound(sys.modules[__name__], surface, native_binding, text),
            emit=delivery_observer, key=lambda value: send_key(surface, value),
            observe=lambda: _observe_native(surface, text, native_binding, native_attempt),
            read_screen=lambda: read_screen(surface, lines=confirm_lines), wait_seconds=seconds)
        if result.get("confirmed") is not True:
            raise DispatchUnconfirmed("DIRECT_RECOVERY_PENDING: observe original; never repaste",
                                      details=result)
        return result
    result = _observe_native(surface, text, native_binding, native_attempt)
    if result.get("confirmed") is True:
        return dict(result, retries=0, recovered="already_consumed")
    if result.get('state') != 'NATIVE_PENDING':
        raise DispatchUnconfirmed('NATIVE_RECOVERY_UNPROVEN: reconcile original without input')
    screen = read_screen(surface, lines=confirm_lines)
    delivery_observer("RECOVERY_OBSERVATION", screen)
    if result.get("state") == "NATIVE_QUEUED" or pending_queue_holds(screen, marker):
        _unconfirmed_native(screen, marker, result)
    structure = require_original_draft(original, screen, text)
    screen = _stable_owned_draft(surface, text, confirm_lines, delivery_observer,
                                 expected_structure=structure)
    if _codex_tab_queue_allowed(screen, text):
        key, intent, observed = "tab", "QUEUE_TAB_INTENT", "POST_QUEUE_TAB_OBSERVATION"
    elif not _queued_or_active_input(screen):
        key, intent, observed = "enter", "EXTRA_ENTER_INTENT", "POST_ENTER_OBSERVATION"
    else:
        _unconfirmed_native(screen, marker, result)
    delivery_observer(intent, screen)
    require_bound(sys.modules[__name__], surface, native_binding, text)
    send_key(surface, key)
    screen, result = _wait_native_after_key(surface, text, marker, confirm_lines,
        delivery_observer, native_binding, native_attempt, observed, seconds)
    if result.get("confirmed") is True:
        return dict(result, retries=0, recovered=key)
    _unconfirmed_native(screen, marker, result)


def submit_task_pack(surface, text, task_pack_path, marker=None, confirm_lines=200,
                     force_compose=False, *, reconcile_only=False, wait_seconds=None, recover_stranded=False):
    """Durable task dispatch; ambiguous attempts are observed, never repasted."""
    from cmux_task_journal import deliver
    return deliver(sys.modules[__name__], surface, text, task_pack_path, marker,
                   confirm_lines, force_compose, reconcile_only=reconcile_only,
                   wait_seconds=wait_seconds, **({"recover_stranded": True} if recover_stranded else {}))


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def submit_completion_callback(task_pack_path, confirm_lines=200, *, reconcile_only=False,
                               resume_queue_only=False, wait_seconds=None, recover_stranded=False):
    """Report-bound delivery with durable attempts and zero-input reconciliation."""
    from cmux_callback_journal import deliver
    return deliver(sys.modules[__name__], task_pack_path, confirm_lines,
                   reconcile_only=reconcile_only, resume_queue_only=resume_queue_only,
                   wait_seconds=wait_seconds, **({"recover_stranded": True} if recover_stranded else {}))


def read_screen(surface, lines=200):
    """
    Read last N lines of a surface via `cmux read-screen`.
    Returns plain text string.
    """
    if surface in _workspace_pins:
        proof = pin_workspace(surface)
        target, workspace = proof["target_surface_uuid"], proof["workspace_uuid"]
    else:
        rows = [row for ref, row in surface_uuid_map().items()
                if surface in (ref, row["surface_id"])]
        if len(rows) != 1:
            raise WorkspaceScopeError("WORKSPACE_SCOPE_DENIED: read target missing or ambiguous")
        target, workspace = rows[0]["surface_id"], rows[0]["workspace_id"]
    return _run("read-screen", "--workspace", workspace, "--surface", target,
                "--lines", str(lines))


def read_screen_successor(artifact, lines=200):
    """Read the authorized successor receiver by its target workspace UUID."""
    proof = pin_successor_pair(artifact)
    result = subprocess.run(
        [CMUX, "read-screen", "--workspace", proof["target_workspace_uuid"],
         "--surface", proof["target_surface_uuid"], "--lines", str(lines)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise WorkspaceScopeError(
            f"SUCCESSOR_REBIND_DENIED: read-screen failed (rc={result.returncode})")
    # Recheck the caller and receiver after the read; a stale screen is never
    # used to authorize paste or a key.
    pin_successor_pair(artifact, expected_caller=proof["caller_surface_uuid"],
                       expected_target=proof["target_surface_uuid"])
    return result.stdout


def wait_for_ack(surface, ack_prefix="PREFLIGHT_ACK", timeout=120, poll=3, lines=200, task_id=None, provider=None, nonce=None):
    """
    Poll read_screen until a real executor ACK appears, or timeout.

    When nonce is provided, the ACK must be generated by the executor response.
    The handshake prompt must not contain the literal ack_prefix; this prevents
    matching the echoed prompt text instead of Claude/Codex's reply.

    Returns the matching line, or raises TimeoutError.
    """
    def compact(text):
        return re.sub(r"\s+", "", text)

    def in_claude_response_block(screen_lines, line_index):
        # Claude's alternate-screen UI may wrap a cautious acknowledgement
        # across enough prose that the leading assistant marker is more than
        # ten rows above the nonce. Scan to the nearest *structural* block
        # marker instead of using a fixed-distance heuristic. Symbols quoted
        # inside prose are not block boundaries.
        for j in range(line_index, -1, -1):
            line = screen_lines[j]
            if re.match(r"^\s*⏺(?:\s|$)", line):
                return True
            if re.match(r"^\s*❯(?:\s|$)", line):
                return False
        return False

    def find_nonce_ack(screen):
        if not (task_id and provider and nonce):
            return None

        expected = f"{ack_prefix}|{task_id}|{provider}:identity|READY|INLINE|{nonce}"
        screen_lines = screen.splitlines()
        for i in range(len(screen_lines)):
            window_lines = screen_lines[i : i + 4]
            window = " ".join(window_lines).strip()
            window_compact = compact(window)
            if expected not in window_compact:
                continue
            if provider == "claude":
                # A window that crosses a Claude user-prompt marker is prompt
                # evidence even if an older assistant marker is nearby.
                if any("❯" in line for line in window_lines):
                    continue
                if not in_claude_response_block(screen_lines, i):
                    continue
            return expected

        if provider != "claude" and expected in compact(screen):
            return expected
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        screen = read_screen(surface, lines=lines)
        ack = find_nonce_ack(screen)
        if ack:
            return ack
        for line in reversed(screen.splitlines()):
            if nonce is None and ack_prefix in line:
                return line.strip()
        time.sleep(poll)
    raise TimeoutError(
        f"Timed out waiting for '{ack_prefix}' on {surface} after {timeout}s"
    )


# ---------------------------------------------------------------------------
# Consensus round evidence (anti-forgery)
# ---------------------------------------------------------------------------

def screen_hash(text):
    """Return a short stable hash of captured screen text (tamper-evidence)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def capture_round_evidence(surface, nonce, provider=None, lines=200, expected_ack=None):
    """
    Capture proof that the executor actually produced a per-round nonce reply.

    Reuses the handshake anti-echo rule: for a Claude executor the nonce must
    appear inside a genuine response block (after ``⏺``), not echoed after the
    ``❯`` prompt. Returns a dict with the capture hash and whether the nonce was
    found in a real executor response. Never raises — callers decide on failure.
    """
    screen = read_screen(surface, lines=lines)
    screen_lines = screen.splitlines()

    def in_claude_response_block(line_index):
        for j in range(line_index, -1, -1):
            line = screen_lines[j]
            if re.match(r"^\s*⏺(?:\s|$)", line):
                return True
            if re.match(r"^\s*❯(?:\s|$)", line):
                return False
        return False

    found = False
    for i in range(len(screen_lines)):
        window_lines = screen_lines[i : i + 4]
        compact_window = re.sub(r"\s+", "", " ".join(window_lines))
        evidence_present = (
            expected_ack in compact_window if expected_ack else nonce in compact_window
        )
        if not evidence_present:
            continue
        if provider == "claude":
            if any("❯" in line for line in window_lines):
                continue
            if not in_claude_response_block(i):
                continue
        found = True
        break

    return {
        # `executor_nonce_found` is overloaded by design: when expected_ack is
        # supplied it means the COMPLETE line matched in a genuine response
        # block, not merely that the nonce appeared. These two extra keys make
        # that explicit so a caller cannot mistake nonce-possession for a full
        # verdict line -- a distinction that matters now that the executor picks
        # its own verdict rather than echoing the requested one.
        "expected_ack": expected_ack,
        "expected_ack_found": (found if expected_ack else None),
        "nonce": nonce,
        "executor_nonce_found": found,
        "screen_hash": screen_hash(screen),
        "screen_lines_read": len(screen_lines),
    }


# ---------------------------------------------------------------------------
# Labelling
# ---------------------------------------------------------------------------

def rename_tab(surface, title, *, workspace_uuid, surface_uuid, caller_uuid):
    """Set the visible tab title of a surface."""
    if surface_uuid == caller_uuid:
        me = whoami()
        if (me["surface_ref"] != surface or
                uuid_value(me["surface_id"]) != uuid_value(caller_uuid) or
                uuid_value(me["workspace_id"]) != uuid_value(workspace_uuid)):
            raise WorkspaceScopeError("WORKSPACE_SCOPE_DENIED: rename caller changed")
    else:
        pin_workspace(surface, workspace_uuid=workspace_uuid,
                      target_uuid=surface_uuid, caller_uuid=caller_uuid)
    _run("rename-tab", "--workspace", workspace_uuid, "--surface", surface_uuid, "--", title)


# ---------------------------------------------------------------------------
# Notifications / feed
# ---------------------------------------------------------------------------

def notify(surface, title, body=""):
    """
    Send a notification to a surface via `cmux notify`.
    Falls back silently if cmux notify is not available in this version.
    """
    try:
        _run("notify", "--surface", surface, "--title", title, "--body", body)
    except RuntimeError:
        pass  # advisory; non-fatal


def _cli_main(argv=None):
    """Command-line facade for the same bridge functions used by the harness.

    The old module entry point printed a diagnostic ping regardless of the
    arguments supplied by an executor.  That made ``python cmux_bridge.py
    submit_text ...`` look successful while never sending anything.  Keep the
    import API authoritative, but make script invocation explicit and
    fail-closed: every mutating command returns a confirmation JSON object or
    a non-zero error with its delivery state.
    """
    import argparse

    parser = argparse.ArgumentParser(description="cmux collaboration bridge")
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("ping")
    subs.add_parser("whoami")
    subs.add_parser("surfaces")
    notice = subs.add_parser("task-notice")
    notice.add_argument("--task-pack", required=True)

    read = subs.add_parser("read-screen", aliases=["read_screen"])
    read.add_argument("--surface", required=True)
    read.add_argument("--lines", type=int, default=200)

    probe = subs.add_parser("probe", help="authenticate a Codex receiver and read metadata; zero input")
    probe.add_argument("--surface", required=True)

    submit = subs.add_parser("submit-text", aliases=["submit_text"])
    submit.add_argument("--surface", required=True)
    submit.add_argument("--text", required=True)
    submit.add_argument("--marker")
    submit.add_argument("--reconcile-only", action="store_true",
                        help="observe the original ordinary message without terminal input")
    submit.add_argument("--recover-stranded", action="store_true",
                        help="Enter was sent but the payload is still in the receiver "
                             "composer: send one queue/submit key, never repaste")
    submit.add_argument("--confirm-lines", type=int, default=200)
    submit.add_argument(
        "--force-compose",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="replace idle Claude compose before submit (default: disabled; explicit authorization required)",
    )

    pack = subs.add_parser("submit-task-pack", aliases=["submit_task_pack"])
    pack.add_argument("--surface", required=True)
    pack.add_argument("--text", required=True)
    pack.add_argument("--task-pack", required=True)
    pack.add_argument("--marker")
    pack.add_argument("--confirm-lines", type=int, default=200)
    pack.add_argument("--reconcile-only", action="store_true",
                      help="observe the original task dispatch without terminal input")
    pack.add_argument(
        "--force-compose",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="replace idle Claude compose before task dispatch (default: disabled; explicit authorization required)",
    )

    completion = subs.add_parser(
        "submit-completion-callback", aliases=["submit_completion_callback"]
    )
    completion.add_argument("--task-pack", required=True)
    completion.add_argument("--confirm-lines", type=int, default=200)
    completion.add_argument("--reconcile-only", action="store_true",
                            help="verify an existing callback attempt without sending input")

    for sender in (pack, completion):
        sender.add_argument("--recover-stranded", action="store_true",
            help="current marker attempt only: spend its unused extra Enter; never repaste")
    for sender in (submit, pack, completion):
        sender.add_argument("--wait-seconds", type=float, default=None,
            help="native observation budget 0–60 seconds; default 30 for new input, "
                 "0 for --reconcile-only; never retries input")

    args = parser.parse_args(argv)
    try:
        if args.command == "ping":
            result = {"command": "ping", "ok": bool(ping())}
        elif args.command == "whoami":
            result = {"command": "whoami", "value": whoami()}
        elif args.command == "surfaces":
            result = {"command": "surfaces", "value": list_surfaces()}
        elif args.command == "task-notice":
            print(task_pack_notice(args.task_pack))
            return 0
        elif args.command == "probe":
            from cmux_codex_input import probe_target
            result = {"command": "probe", **probe_target(sys.modules[__name__], args.surface)}
        elif args.command in {"read-screen", "read_screen"}:
            # Screen text is intentionally not JSON-escaped into a nested
            # field: callers use it as the raw input to the classifier.
            print(read_screen(args.surface, lines=args.lines), end="")
            return 0
        elif args.command in {"submit-text", "submit_text"}:
            result = submit_text(
                args.surface,
                args.text,
                marker=args.marker,
                confirm_lines=args.confirm_lines,
                force_compose=args.force_compose,
                reconcile_only=args.reconcile_only,
                wait_seconds=args.wait_seconds,
                **({"recover_stranded": True} if args.recover_stranded else {}),
            )
            result = {"command": "submit_text", "surface": args.surface, **result}
        elif args.command in {"submit-task-pack", "submit_task_pack"}:
            result = submit_task_pack(
                args.surface,
                args.text,
                args.task_pack,
                marker=args.marker,
                confirm_lines=args.confirm_lines,
                force_compose=args.force_compose,
                reconcile_only=args.reconcile_only,
                wait_seconds=args.wait_seconds,
                **({"recover_stranded": True} if args.recover_stranded else {}),
            )
            result = {
                "command": "submit_task_pack",
                "surface": args.surface,
                "task_pack": args.task_pack,
                **result,
            }
        elif args.command in {
            "submit-completion-callback",
            "submit_completion_callback",
        }:
            result = submit_completion_callback(
                args.task_pack, confirm_lines=args.confirm_lines,
                reconcile_only=args.reconcile_only, wait_seconds=args.wait_seconds,
                **({"recover_stranded": True} if args.recover_stranded else {})
            )
            result = {"command": "submit_completion_callback", **result}
        else:  # pragma: no cover - argparse makes this unreachable
            parser.error(f"unknown command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except DispatchUnconfirmed as exc:
        payload = {
            "confirmed": False,
            "error": str(exc),
            "delivery_state": getattr(exc, "state", None),
            "details": getattr(exc, "details", {}),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 75
    except (TaskPackContractError, OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                {"confirmed": False, "error": str(exc), "details": getattr(exc, "details", {})},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())

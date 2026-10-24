#!/usr/bin/env python3
"""
mac_harness.py — cmux-native multi-agent coordination harness (replaces multi-agent-harness.ps1).

Usage:
  python3 mac_harness.py <subcommand> [options]

Subcommands:
  doctor            Dump environment, socket, surfaces
  setup-check       Verify cmux reachable, python3, socket PONG
  identity          Show caller identity (workspace/surface)
  surface-inventory List surfaces in workspace; classify same/other
  identity-gate     Detect supervisor + executor; write identity-gate.json
  name-surfaces     Rename executor tab; write naming-proof.json
  bridge-test       Non-submitting send→read proof; write bridge-test evidence
  handshake         Send PREFLIGHT_ACK prompt; poll for executor ACK; write handshake-receipt.json
  validate          Aggregate all artifacts → validation.json PASS/FAIL
  task-pack         Emit a task-pack.json scaffold (fill fields manually)
  map               Print role-map.json for current task
  receipt           Print handshake-receipt.json evidence summary
  guard-check       Check whether validation.json PASS exists for task
  record-round      Append one consensus/audit round to rounds.json
  consensus-check   Require strict validation plus at least 3 completed rounds

Options:
  --task-id <id>           Task identifier (default: multi-agent-task)
  --supervisor <provider>  claude | codex | opencode (default: auto from env)
  --executor <provider>    codex | claude | opencode (default: codex)
  --executor-surface <ref> Explicit surface ref for executor (e.g. surface:24).
                           Repeatable: first is EXECUTOR_1, second EXECUTOR_2, …
                           Optional per-ref provider: surface:24=claude
  --artifact-root <path>   Override artifact directory
  --spawn                  Request a side-split executor panel if none exists
  --spawn-authorized       Confirm the user explicitly authorized a new executor session
  --direction <dir>        Split direction for --spawn: right|left|up|down (default: right)
  --cwd <path>             Working directory for spawned executor
  --json                   Output machine-readable JSON where available
  --timeout <sec>          Explicitly override the phase-specific timeout
  --handshake-timeout <sec> Cold handshake poll budget (default: 600)
  --round-timeout <sec>    Per-round ACK poll budget (default: 180)
  --lines <n>              Lines to read for bridge-test/handshake (default: 200)
  --force-compose          Explicitly discard the current Claude compose buffer
                           before bridge-test/dispatch (records an override)
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid as _uuid
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Import bridge from same directory
_dir = Path(__file__).parent
sys.path.insert(0, str(_dir))
from cmux_coordination_paths import ACTIVE_DIR as _ACTIVE_DIR
import cmux_bridge as cmux
import cmux_claude_interruption as interrupted_probe
import render_cmux_agent

# ---------------------------------------------------------------------------
# Phase budgets and bridge-test clear postcondition
# (R3 consensus, task multi-agent-skill-hardening-20260830)
# ---------------------------------------------------------------------------

# A cold handshake must first verify a receipt on disk before it can answer, so
# its floor is far above a round ACK from an already-bound executor. These are
# the *minimums* used to decide whether a timeout may be blamed on the executor
# at all: an effective budget below the minimum makes the timeout the
# supervisor's, not the executor's.
PHASE_MINIMUM_BUDGET_SECONDS = {"handshake": 600, "round": 180}

# Communication is admitted by the guarded sender, including working turns.
# A destructive idle-only probe is an explicit diagnostic, never a preflight step.
HANDSHAKE_PROBE_POLICY = "optional-probe-v1"

# A receiver can consume a prompt after the bridge's detector gives up (for
# example while Claude is recovering from a 429).  Give the same nonce a
# bounded, read-only grace period before declaring the round failed.  This is
# deliberately a wait, never a resend.
ROUND_LATE_ACK_GRACE_SECONDS = 120

COLLABORATION_SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"

# Verdicts an executor may choose for a review round. The supervisor may state
# which one it *expects*, but the parser records what the executor actually sent,
# and a value outside this set is rejected rather than coerced.
ROUND_ALLOWED_VERDICTS = (
    "PASS",
    "PASS_WITH_CHANGES",
    "PASS_WITH_P2",
    "CONDITIONAL_PASS",
    "FAIL",
)

# Verdicts that let consensus close. A verdict outside this set must either set
# blocks_consensus or be named by a later resolves_rounds entry.
ROUND_APPROVING_VERDICTS = ("PASS", "PASS_WITH_P2", "APPROVE", "APPROVED", "APPROVE_WITH_P2")

# ctrl+u is a request, not a receipt. Verify the compose block is actually clear
# before handing the surface to handshake.
BRIDGE_TEST_CLEAR_MAX_ATTEMPTS = 3
BRIDGE_TEST_CLEAR_DELAY_SECONDS = 1.0
BRIDGE_TEST_CLEAR_DELETE_COUNT = 256
BRIDGE_TEST_CLEAR_KEY_DELAY_SECONDS = 0.01

# The consensus floor lives HERE, in code, not in the file being audited.
#
# `consensus-check` previously read `minimum_required_rounds` out of rounds.json
# and then checked rounds.json against it, so lowering that number in the audited
# file also lowered the bar it was judged by. A gate whose threshold is an input
# it does not control is not a gate. The declared value may still RAISE the floor
# (a task wanting five rounds is welcome to say so); it can never lower it.
CONSENSUS_MINIMUM_ROUNDS = 3

# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------

ARTIFACT_REGISTRY_DIR = Path("/tmp/multi-agent-collaboration/_registry")


def _registered_artifact_root(task_id: str) -> Path | None:
    """Artifact root recorded for `task_id`, or None when it is unregistered."""
    if not task_id:
        return None
    safe_id = task_id.replace(os.sep, "_")
    path = ARTIFACT_REGISTRY_DIR / f"{safe_id}.json"
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    root = entry.get("artifact_root")
    if not isinstance(root, str) or not root:
        return None
    candidate = Path(root)
    return candidate if candidate.is_absolute() else None


def _artifact_root(args):
    """Resolve the artifact root without consulting the caller's cwd.

    D6 is deliberately strict for every harness command: an explicit absolute
    root wins, otherwise the task registry is authoritative, and an unknown
    task is a hard ``ROOT_NOT_FOUND``. A cwd fallback would let a typo or a
    shell launched from an unrelated directory create/read evidence in a
    different tree, which is the exact observation-path failure this resolver
    exists to prevent. Creation commands must therefore receive an explicit
    ``--artifact-root`` and register it through ``_ensure_root``.
    """
    explicit = getattr(args, "artifact_root", "")
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_absolute():
            _fail(
                f"ARTIFACT_ROOT_NOT_ABSOLUTE for task {getattr(args, 'task_id', '')!r}: "
                f"{explicit!r} must be an absolute path; refusing cwd resolution."
            )
            raise SystemExit(1)
        return candidate

    registered = _registered_artifact_root(getattr(args, "task_id", ""))
    if registered is not None:
        return registered

    task_id = getattr(args, "task_id", "")
    safe_id = task_id.replace(os.sep, "_")
    _fail(
        f"ROOT_NOT_FOUND for task {task_id!r}: pass --artifact-root, or register "
        f"the task at {ARTIFACT_REGISTRY_DIR / (safe_id + '.json')} with an "
        "artifact_root field. The caller's cwd is deliberately not used."
    )
    raise SystemExit(1)


def _ensure_root(root: Path, task_id: str = ""):
    if task_id:
        existing = _registered_artifact_root(task_id)
        # A registry entry whose directory has already been removed is a stale
        # index left by a finished temporary task; it may be replaced. A live
        # different root, however, means two evidence trees would share one
        # TASK_ID and must fail closed.
        if (existing is not None and existing.exists()
                and existing.resolve() != root.resolve()):
            _fail(
                f"ARTIFACT_ROOT_MISMATCH for task {task_id!r}: registry binds "
                f"{existing}, not the supplied {root}. Refusing to split evidence."
            )
            raise SystemExit(1)
    root.mkdir(parents=True, exist_ok=True)
    # Registry identity is the task id, not the directory basename. Those are
    # usually equal, which hid this bug until an explicit root such as
    # `/tmp/review-artifacts` was reused for a differently named task.
    reg = ARTIFACT_REGISTRY_DIR
    reg.mkdir(parents=True, exist_ok=True)
    safe_id = (task_id or root.name).replace(os.sep, "_")
    _write(reg / f"{safe_id}.json", {"artifact_root": str(root)})


def _sha256_file(path: Path) -> str | None:
    """Hash a file, or return None when it does not exist.

    None is a meaningful answer, not an error: `record-round` uses it to tell
    "the artifact is missing" apart from "the artifact is present and hashes to
    X". Returning None rather than raising keeps the caller in charge of whether
    a missing artifact is fatal.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


# A read that cannot establish a fact is neither PASS nor FAIL. Naming that state
# stops an absent tool or a truncated artifact from being scored as success --
# the same fail-closed shape the helper-parity gate already uses.
STATUS_UNVERIFIABLE = "UNVERIFIABLE"


def _write(path: Path, data: dict):
    """Atomically replace `path` with `data`.

    Plain write_text() leaves a window where the file exists but is truncated. A
    later gate reading it would either fail to parse or, worse, parse a partial
    object and treat it as evidence. Receipts are exactly the artifacts a gate
    consults after a crash, so the write must be all-or-nothing:

      O_CREAT|O_EXCL  unique temp in the destination directory (never /tmp, so
                      the rename stays within one filesystem and is atomic)
      write + fsync   data durable before it is visible under the real name
      os.replace      atomic swap; readers see either the old or the new bytes
      dir fsync       the rename itself made durable, best-effort
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2)
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # Some filesystems reject directory fsync; the replace is still atomic.
        pass
    print(f"  wrote → {path}")
    _touch_active_marker(path)


def _read(path: Path):
    """Read a JSON artifact. Absent -> None. Unparseable -> UNVERIFIABLE marker.

    A truncated or corrupt artifact must not raise past a gate (which would look
    like a crash) nor return a partial object (which would look like evidence).
    Callers already treat a falsy result as "no evidence", and the explicit
    status field lets a caller that cares distinguish "never written" from
    "written but unreadable".
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return {
            "status": STATUS_UNVERIFIABLE,
            "pass": False,
            "artifact": str(path),
            "reason": f"{type(exc).__name__}: {exc}",
            "note": (
                "Artifact exists but could not be parsed. This is neither PASS "
                "nor FAIL; it is an absence of evidence and must fail closed."
            ),
        }


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ok(msg):
    print(f"[OK] {msg}")


def _fail(msg):
    print(f"[FAIL] {msg}", file=sys.stderr)


def _info(msg):
    print(f"  {msg}")


# ---------------------------------------------------------------------------
# Armed-task markers (consumed by the Stop guard)
# ---------------------------------------------------------------------------
# A task becomes "armed" when identity-gate passes. While armed, the Stop guard
# scans a turn's final message for collaboration/consensus assertions and blocks
# turn-end unless PASS evidence exists. Disarm clears it (zero-friction default).

_ARM_TTL_SECONDS = 6 * 60 * 60  # 6h safety expiry so a stale marker can't wedge sessions
# Contract version for external read-only consumers (CCC Supervisor TUI reads
# these markers to render its 协作 column).  Bump on breaking shape changes;
# see schemas/active-marker.schema.json for the published shape.
#
# v2 (2026-09-01): one file per collaboration under
# _active/<workspace_uuid>/<collaboration_id>.json so concurrent
# collaborations in one workspace no longer overwrite each other, and
# participants carry role+ordinal instead of numbered role enums.  The v1
# single file _active/<workspace_key>.json remains read-only compatible for
# existing markers; nothing writes that shape anymore.
_ACTIVE_MARKER_VERSION = 2

# The --task-id default. argparse cannot distinguish "user typed the default"
# from "user typed nothing", so disarm compares against this constant to tell an
# explicitly named target apart from an unnamed one. Shared with the argparse
# declaration below so the two can never drift.
DEFAULT_TASK_ID = "multi-agent-task"


def _workspace_key():
    from cmux_workspace_guard import daemon_identity, caller_snapshot
    if daemon_identity.collect(os.environ) is not None:
        return caller_snapshot()[2]["CMUX_WORKSPACE_ID"]
    return os.environ.get("CMUX_WORKSPACE_ID", "") or "default"


def _legacy_arm_path():
    """v1 single-marker path — read/cleanup compatibility only, never written."""
    return _ACTIVE_DIR / f"{_workspace_key()}.json"


def _collab_dir(workspace_uuid=""):
    return _ACTIVE_DIR / (workspace_uuid or _workspace_key())


def _workspace_marker_paths():
    """Every marker file that belongs to the current workspace, v2 then v1.

    v2 markers live in the directory named by the workspace key; the v1
    single file sits directly in _ACTIVE_DIR.  Anything else in _ACTIVE_DIR
    belongs to other workspaces and is never touched from here.
    """
    paths = []
    d = _collab_dir()
    if d.is_dir():
        paths.extend(sorted(p for p in d.glob("*.json") if not p.name.startswith(".")))
    legacy = _legacy_arm_path()
    if legacy.is_file():
        paths.append(legacy)
    return paths


@contextmanager
def _marker_update_lock(marker_path, marker, *, create=False):
    """Caller locks first, then a nonblocking marker-directory lock.

    The directory also serializes legacy markers without executor UUIDs and
    survives atomic file replacement. No lock sidecars accumulate on disarm.
    """
    from executor_reask_lifecycle import marker_locks
    with marker_locks(marker):
        if create:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(marker_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)


def _read_active_marker(path):
    try:
        marker = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(marker, dict):
        raise ValueError('active marker must be an object')
    return marker


def _same_active_marker(left, right):
    """Only telemetry may change while acquiring the original participants."""
    return (isinstance(left, dict) and isinstance(right, dict)
            and {k: v for k, v in left.items() if k != 'last_activity_at'}
            == {k: v for k, v in right.items() if k != 'last_activity_at'})


def _touch_active_marker(written_path: Path):
    """Best-effort heartbeat: bump last_activity_at when a task artifact lands.

    The active marker's armed_at + 6h TTL only says "a task started"; a
    consumer that wants to hide *stalled* collaborations needs to know when the
    task last produced evidence.  Every receipt/round/validation write under the
    armed artifact_root counts as activity.  The owning collaboration is found
    by artifact_root prefix across all of this workspace's markers (v2 files
    and the v1 legacy single file).  Failure here must never break an evidence
    write, and the marker files themselves (or anything else in _ACTIVE_DIR)
    must not re-trigger the touch, or arm_task would recurse.
    """
    try:
        if _ACTIVE_DIR == written_path or _ACTIVE_DIR in written_path.parents:
            return
        for marker_path in _workspace_marker_paths():
            try:
                observed = _read_active_marker(marker_path)
                root = str((observed or {}).get("artifact_root") or "")
                if not root or not str(written_path).startswith(root.rstrip("/") + "/"):
                    continue
                with _marker_update_lock(marker_path, observed):
                    marker = _read_active_marker(marker_path)
                    if not _same_active_marker(observed, marker):
                        # Disarm/replacement won before the lock. Never recreate
                        # its marker or overwrite a different task/participant.
                        continue
                    marker["last_activity_at"] = _now()
                    tmp = marker_path.parent / (
                        f".{marker_path.name}.hb.{os.getpid()}.{secrets.token_hex(4)}")
                    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    try:
                        with os.fdopen(fd, 'w') as handle:
                            json.dump(marker, handle, indent=2)
                        os.replace(tmp, marker_path)
                    finally:
                        tmp.unlink(missing_ok=True)
            except Exception:
                # Both locks are nonblocking. A busy task must not hold up an
                # evidence write or suppress unrelated markers' heartbeats.
                continue
    except Exception:
        # A heartbeat is telemetry for read-only viewers, never a gate input;
        # losing one beat is strictly better than failing the artifact write.
        pass


def _gate_executors(gate):
    """Every executor in an identity-gate, newest shape first.

    Readers must not pick between the singular ``executor`` field and the
    ``executors`` list on their own: doing that is how a gate that named three
    executors ended up with evidence for only the first. This is the one place
    that resolves the shape, and it falls back to the singular field so gates
    written before ``executors[]`` existed still read as a one-element list.

    Returns a list of dicts with ``surface_ref``/``provider``/``surface_uuid``/
    ``workspace_ref``/``pane_ref`` and an added 1-based ``ordinal``.
    """
    raw = gate.get("executors") or []
    if not raw:
        raw = [{
            "surface_ref": gate.get("executor", ""),
            "provider": gate.get("executor_provider", ""),
            "surface_uuid": gate.get("executor_surface_uuid", ""),
            "workspace_ref": gate.get("executor_workspace_ref", ""),
            "pane_ref": gate.get("executor_pane_ref", ""),
        }]
    out = []
    for i, entry in enumerate(raw, start=1):
        item = dict(entry)
        item["ordinal"] = i
        item["surface_ref"] = str(item.get("surface_ref") or "")
        item["provider"] = str(item.get("provider") or gate.get("executor_provider", "") or "")
        out.append(item)
    return out


def _matching_reservation(directory, candidate):
    """Reuse one native-bound reservation while all participant/marker locks hold.

    Surface labels are display data. Missing native pins cannot establish the
    same conversation, and historical duplicates are preserved for their owner
    to release explicitly rather than silently merged or replaced.
    """
    from executor_reask_lifecycle import MAX_FORMAL_MARKERS, marker_epoch, valid_epoch

    def well_formed(marker):
        # Pre-native v2 markers have complete participant rows but no native
        # session keys at all. They remain preserved and cannot establish a
        # conversation match. Missing rows or partial native pins are damage,
        # not permission to silently create another reservation.
        if (marker.get('marker_version') != _ACTIVE_MARKER_VERSION
                or not all(isinstance(marker.get(key), str) and marker[key] for key in
                           ('collaboration_id', 'task_id', 'artifact_root',
                            'workspace_id', 'workspace_uuid'))):
            return False
        participants = marker.get('participants')
        if not isinstance(participants, list) or len(participants) < 2:
            return False
        for index, participant in enumerate(participants):
            if (not isinstance(participant, dict)
                    or participant.get('role') != ('supervisor' if index == 0 else 'executor')
                    or type(participant.get('ordinal')) is not int
                    or participant['ordinal'] != index
                    or not all(isinstance(participant.get(key), str) for key in
                               ('surface_ref', 'surface_uuid', 'provider'))):
                return False
        if any('native_session_id' in row for row in participants):
            for row in participants:
                if not all(isinstance(row.get(key), str) and row[key] for key in
                           ('surface_uuid', 'provider', 'native_session_id')):
                    return False
                try:
                    _uuid.UUID(row['native_session_id'])
                    _uuid.UUID(row['surface_uuid'])
                except ValueError:
                    return False
        return True

    def identity(marker):
        participants = marker.get('participants')
        if not isinstance(participants, list) or not participants:
            return None
        keys = ('role', 'ordinal', 'surface_uuid', 'provider', 'native_session_id')
        pins = []
        for participant in participants:
            if (not isinstance(participant, dict)
                    or not all(participant.get(k) for k in
                               ('role', 'surface_uuid', 'provider', 'native_session_id'))):
                return None
            pins.append({key: participant.get(key) for key in keys})
        return ({key: marker.get(key) for key in
                 ('marker_version', 'task_id', 'artifact_root', 'workspace_id', 'workspace_uuid')},
                pins)

    expected = identity(candidate)
    if expected is None:
        return None
    matches = []
    with os.scandir(directory) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_FORMAL_MARKERS:
                raise RuntimeError('active reservation lookup capacity exceeded')
            if (entry.name.startswith('.') or not entry.name.endswith('.json')
                    or not entry.is_file(follow_symlinks=False)):
                continue
            try:
                _uuid.UUID(entry.name[:-5])
                uuid_filename = True
            except ValueError:
                uuid_filename = False
            try:
                marker = _read_active_marker(Path(entry.path))
            except ValueError as exc:
                # v2 reservations have UUID filenames. Non-marker debris may
                # be diagnosed and ignored, but a corrupted UUID file could
                # still reserve these participants: never recreate it.
                if not uuid_filename:
                    print('Ignoring malformed non-reservation file: ' + entry.path,
                          file=sys.stderr)
                    continue
                raise RuntimeError(
                    'unreadable active reservation; preserve original file and '
                    'resolve its ownership before arming: ' + entry.path) from exc
            if marker is not None and uuid_filename and (
                    not well_formed(marker)
                    or entry.name != marker['collaboration_id'] + '.json'):
                raise RuntimeError(
                    'malformed active reservation; preserve original file and '
                    'resolve its ownership before arming: ' + entry.path)
            if marker is not None and identity(marker) == expected:
                if entry.name != str(marker.get('collaboration_id')) + '.json':
                    raise RuntimeError('matching reservation filename is not bound to its id')
                matches.append(marker)
    if len(matches) > 1:
        raise RuntimeError('multiple matching active reservations; preserve and release originals explicitly')
    if not matches:
        return None
    marker = matches[0]
    armed = marker_epoch(marker)
    ttl = marker.get('ttl_seconds')
    now = time.time()
    # Identity alone is insufficient: returning an expired marker would say
    # arm succeeded while TTL-aware consumers consider the task inactive.
    # Preserve both the marker and its reservation; only the original owner
    # can RELEASE it. Heartbeats never extend the original armed_at or TTL.
    if (not valid_epoch(armed) or not valid_epoch(ttl) or ttl <= 0
            or not 0 <= now - armed <= ttl):
        raise RuntimeError(
            'matching reservation expired or has invalid timing; preserve the original '
            'marker and require its owner to RELEASE it explicitly before rearming')
    return marker


def arm_task(task_id, root, supervisor="", executor="", *,
             supervisor_provider="", executor_provider="",
             supervisor_surface_uuid="", executor_surface_uuid="",
             workspace_uuid="", executors=None, native_participants=None):
    """Write a v2 collaboration marker and return it.

    ``executors`` is the multi-executor API: a list of dicts with
    ``surface_ref``/``provider``/``surface_uuid`` keys.  The legacy single
    ``executor=`` keywords stay supported and are converted to a one-element
    list, so every existing caller keeps working unchanged.

    Invariants enforced here (writers fail loudly, readers fail closed):
      * exactly one supervisor at ordinal 0;
      * at least one executor, ordinals 1..n;
      * no duplicate non-empty surface_uuid within the marker.
    """
    if executors is None:
        executors = [{
            "surface_ref": executor,
            "provider": executor_provider,
            "surface_uuid": executor_surface_uuid,
        }]
    if not executors:
        raise ValueError("arm_task requires at least one executor")

    participants = [{
        "role": "supervisor",
        "ordinal": 0,
        "surface_ref": supervisor,
        "surface_uuid": supervisor_surface_uuid,
        "provider": supervisor_provider,
    }]
    for i, entry in enumerate(executors, start=1):
        participants.append({
            "role": "executor",
            "ordinal": i,
            "surface_ref": str(entry.get("surface_ref") or ""),
            "surface_uuid": str(entry.get("surface_uuid") or ""),
            "provider": str(entry.get("provider") or ""),
        })

    uuids = [p["surface_uuid"] for p in participants if p["surface_uuid"]]
    if len(uuids) != len(set(uuids)):
        raise ValueError("duplicate surface_uuid within one collaboration marker")

    if native_participants is not None:
        # Enrollment belongs to the native conversation, not to a reusable pane.
        # cmd_identity_gate obtains these pins from the designated live clients.
        pins = {p["surface_uuid"]: p for p in native_participants}
        if len(pins) != len(participants) or set(pins) != set(uuids):
            raise ValueError("native enrollment must cover exactly the task participants")
        for participant in participants:
            pin = pins[participant["surface_uuid"]]
            if (pin.get("provider") != participant["provider"]
                    or pin.get("role") != participant["role"]
                    or not pin.get("native_session_id")):
                raise ValueError("native enrollment participant mismatch")
            participant["native_session_id"] = str(_uuid.UUID(pin["native_session_id"]))

    collaboration_id = str(_uuid.uuid4())
    workspace_id = _workspace_key()
    workspace_uuid = workspace_uuid or workspace_id
    now = _now()
    marker = {
        "marker_version": _ACTIVE_MARKER_VERSION,
        "collaboration_id": collaboration_id,
        "task_id": task_id,
        "artifact_root": str(root),
        "workspace_id": workspace_id,
        "workspace_uuid": workspace_uuid,
        "participants": participants,
        "armed_at": now,
        "last_activity_at": now,
        "ttl_seconds": _ARM_TTL_SECONDS,
    }
    from executor_reask_lifecycle import task_boundary_locked
    try:
        with _marker_update_lock(
                _collab_dir(workspace_uuid) / f"{collaboration_id}.json", marker, create=True):
            existing = _matching_reservation(_collab_dir(workspace_uuid), marker)
            if existing is not None:
                # Repair an interrupted reservation publication if necessary;
                # keep the original id, armed time and marker bytes unchanged.
                task_boundary_locked(existing, 'RESERVED')
                return existing
            # Publish the formal evidence first, under all participant locks.
            # If reservation persistence fails, retain it for observation and
            # propagate the error; no caller can mistake this for a good arm.
            _write(_collab_dir(workspace_uuid) / f"{collaboration_id}.json", marker)
            task_boundary_locked(marker, 'RESERVED')
    except BlockingIOError as exc:
        raise RuntimeError('inquiry state or active marker busy; task was not armed') from exc
    return marker


def disarm_task(task_id=None):
    """Remove this workspace's marker(s); never another workspace's.

    With one active collaboration the call stays zero-friction.  With several,
    a bare disarm would have to guess which one finished, so it refuses and
    asks for the task id instead of deleting a still-running collaboration.
    Returns the number of markers removed.
    """
    paths = _workspace_marker_paths()
    if not task_id and len(paths) > 1:
        raise ValueError(
            f"{len(paths)} active markers in this workspace — pass --task-id "
            "to disarm a specific collaboration")
    from executor_reask_lifecycle import task_boundary_locked
    removed = 0
    for p in paths:
        try:
            observed = _read_active_marker(p)
        except (ValueError, OSError):
            if task_id:
                continue
            raise
        if observed is None or task_id and observed.get('task_id') != task_id:
            continue
        try:
            with _marker_update_lock(p, observed):
                marker = _read_active_marker(p)
                if marker is None:
                    continue
                if not _same_active_marker(observed, marker):
                    raise RuntimeError('active marker changed; nothing released or removed')
                task_boundary_locked(marker, 'RELEASED')
                if _read_active_marker(p) != marker:
                    raise RuntimeError('active marker changed during release; marker preserved')
                p.unlink()
                removed += 1
        except BlockingIOError as exc:
            raise RuntimeError('inquiry state or active marker busy; marker preserved') from exc
    return removed


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def cmd_doctor(args):
    print("=== cmux multi-agent doctor ===")
    print(f"  CMUX_WORKSPACE_ID : {os.environ.get('CMUX_WORKSPACE_ID','(not set)')}")
    print(f"  CMUX_SURFACE_ID   : {os.environ.get('CMUX_SURFACE_ID','(not set)')}")
    print(f"  CMUX_PANE_ID      : {os.environ.get('CMUX_PANE_ID','(not set)')}")
    print(f"  CMUX_BIN          : {cmux.CMUX}")
    print(f"  python3           : {sys.executable}")
    ping = cmux.ping()
    print(f"  socket ping       : {'PONG ✓' if ping else 'FAIL ✗'}")
    try:
        me = cmux.whoami()
        print(f"  caller workspace  : {me['workspace_ref']}")
        print(f"  caller surface    : {me['surface_ref']}")
    except Exception as e:
        print(f"  identify error    : {e}", file=sys.stderr)
    try:
        surfs = cmux.list_surfaces()
        print(f"  surfaces in pane  : {len(surfs)}")
        for s in surfs:
            sel = " [selected]" if s["selected"] else ""
            print(f"    {s['ref']}  {s['title']}{sel}")
    except Exception as e:
        print(f"  surface list error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# setup-check
# ---------------------------------------------------------------------------

def cmd_setup_check(args):
    print("=== setup-check ===")
    ok = True

    # cmux binary
    if Path(cmux.CMUX).exists():
        _ok(f"cmux CLI found: {cmux.CMUX}")
    else:
        _fail(f"cmux CLI not found at: {cmux.CMUX}")
        ok = False

    # socket
    if cmux.ping():
        _ok("cmux socket PONG")
    else:
        _fail("cmux socket not reachable — is cmux running?")
        ok = False

    # python3
    _ok(f"python3: {sys.executable} ({sys.version.split()[0]})")

    # workspace env
    ws = os.environ.get("CMUX_WORKSPACE_ID", "")
    if ws:
        _ok(f"CMUX_WORKSPACE_ID: {ws}")
    else:
        _fail("CMUX_WORKSPACE_ID not set — run inside a cmux workspace")
        ok = False

    # surface env
    sf = os.environ.get("CMUX_SURFACE_ID", "")
    if sf:
        _ok(f"CMUX_SURFACE_ID: {sf}")
    else:
        _fail("CMUX_SURFACE_ID not set")
        ok = False

    if ok:
        print("\n✓ setup-check PASS")
    else:
        print("\n✗ setup-check FAIL — fix warnings above", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------

def cmd_identity(args):
    me = cmux.whoami()
    if args.json:
        print(json.dumps(me, indent=2))
    else:
        print("=== caller identity ===")
        for k, v in me.items():
            print(f"  {k}: {v}")


# ---------------------------------------------------------------------------
# surface-inventory
# ---------------------------------------------------------------------------

def cmd_surface_inventory(args):
    root = _artifact_root(args)
    _ensure_root(root, args.task_id)
    me = cmux.whoami()
    my_ws = me["workspace_ref"]
    surfs = cmux.list_surfaces(workspace=me.get("workspace_id") or my_ws)
    same, other = [], []
    for s in surfs:
        provider = cmux.detect_provider(s)
        entry = {**s, "provider": provider, "workspace_ref": my_ws}
        if s["ref"] == me["surface_ref"]:
            entry["role"] = "supervisor_candidate"
        same.append(entry)

    inventory = {
        "task_id": args.task_id,
        "workspace_ref": my_ws,
        "surfaces_same_workspace": same,
        "cross_workspace_rejected": other,
        "updated_at": _now(),
    }
    _write(root / "surface-inventory.json", inventory)
    if args.json:
        print(json.dumps(inventory, indent=2))
    else:
        print(f"Workspace {my_ws}: {len(same)} surface(s)")
        for s in same:
            print(f"  {s['ref']}  {s['title']}  provider={s['provider']}")


# ---------------------------------------------------------------------------
# identity-gate
# ---------------------------------------------------------------------------

def cmd_identity_gate(args):
    root = _artifact_root(args)
    _ensure_root(root, args.task_id)
    me = cmux.whoami()

    # Supervisor = caller
    supervisor_provider = args.supervisor or "auto"
    if supervisor_provider == "auto":
        # A focused executor or an old title is not the managed native caller.
        surfs = cmux.list_surfaces(workspace=me.get("workspace_id") or me["workspace_ref"])
        sel = next((s for s in surfs if s["ref"] == me["surface_ref"]), None)
        supervisor_provider = me.get("provider", "unknown")
        if supervisor_provider == "unknown":
            supervisor_provider = cmux.detect_provider(sel) if sel else "unknown"

    supervisor_ref = me["surface_ref"]

    # Executor discovery. --executor-surface is repeatable: the first ref is
    # EXECUTOR_1, the second EXECUTOR_2, and so on. Auto-detection and spawning
    # still yield exactly one executor, so they feed the same list.
    #
    # A ref may carry its own provider as `surface:24=claude`. Without that,
    # --executor is one provider for the whole panel, so a Codex+Claude+Grok
    # panel could not be described at all and would be armed as three of
    # whatever --executor said. A ref with no `=` keeps the --executor default,
    # so the single-executor command line is unchanged.
    executor_refs = []
    provider_overrides = {}
    for raw_ref in (args.executor_surface or []):
        ref, _, provider = str(raw_ref).partition("=")
        ref = ref.strip()
        executor_refs.append(ref)
        if provider.strip():
            provider_overrides[ref] = provider.strip()
    dupes = [r for r in set(executor_refs) if executor_refs.count(r) > 1]
    if dupes:
        _fail(f"DUPLICATE_EXECUTOR_SURFACE — {', '.join(sorted(dupes))} passed more than once")
        _write(root / "identity-gate.json", {
            "task_id": args.task_id,
            "status": "FAIL",
            "reason": "DUPLICATE_EXECUTOR_SURFACE",
            "duplicates": sorted(dupes),
            "workspace_ref": me["workspace_ref"],
            "supervisor": supervisor_ref,
            "updated_at": _now(),
        })
        sys.exit(1)
    if supervisor_ref in executor_refs:
        _fail(f"EXECUTOR_IS_SUPERVISOR — {supervisor_ref} cannot be both roles")
        _write(root / "identity-gate.json", {
            "task_id": args.task_id,
            "status": "FAIL",
            "reason": "EXECUTOR_IS_SUPERVISOR",
            "workspace_ref": me["workspace_ref"],
            "supervisor": supervisor_ref,
            "updated_at": _now(),
        })
        sys.exit(1)
    executor_ref = executor_refs[0] if executor_refs else ""
    executor_provider = args.executor or "codex"
    spawned = None

    if args.spawn_authorized and not args.spawn:
        _fail(
            "SPAWN_REQUIRES_BOTH_FLAGS — --spawn-authorized is invalid without --spawn; "
            "reuse the existing context-bearing executor"
        )
        gate = {
            "task_id": args.task_id,
            "status": "FAIL",
            "reason": "SPAWN_REQUIRES_BOTH_FLAGS",
            "workspace_ref": me["workspace_ref"],
            "supervisor": supervisor_ref,
            "updated_at": _now(),
        }
        _write(root / "identity-gate.json", gate)
        sys.exit(1)

    if not executor_ref:
        # Look in current surfaces for a non-selected surface matching provider
        surfs = cmux.list_surfaces(workspace=me.get("workspace_id") or me["workspace_ref"])
        candidates = [
            s for s in surfs
            if s["ref"] != supervisor_ref and cmux.detect_provider(s) == executor_provider
        ]
        # Also accept terminal surfaces (executor running agent CLI inside a shell)
        terminal_candidates = [
            s for s in surfs
            if s["ref"] != supervisor_ref and s.get("is_terminal") and cmux.detect_provider(s) == "unknown"
        ]
        if len(candidates) == 1:
            executor_ref = candidates[0]["ref"]
            _info(f"Auto-detected executor: {executor_ref} ({candidates[0]['title']})")
        elif len(candidates) > 1:
            _fail("MULTIPLE_EXECUTOR_CANDIDATES — use --executor-surface to bind explicitly")
            refs = [c["ref"] for c in candidates]
            gate = {
                "task_id": args.task_id,
                "status": "FAIL",
                "reason": "MULTIPLE_EXECUTOR_CANDIDATES",
                "candidates": refs,
                "workspace_ref": me["workspace_ref"],
                "supervisor": supervisor_ref,
                "updated_at": _now(),
            }
            _write(root / "identity-gate.json", gate)
            sys.exit(1)
        elif args.spawn and not args.spawn_authorized:
            _fail(
                "SPAWN_REQUIRES_EXPLICIT_AUTHORIZATION — reuse the existing context-bearing "
                "executor, or obtain explicit user authorization and pass both "
                "--spawn --spawn-authorized"
            )
            gate = {
                "task_id": args.task_id,
                "status": "FAIL",
                "reason": "SPAWN_REQUIRES_EXPLICIT_AUTHORIZATION",
                "workspace_ref": me["workspace_ref"],
                "supervisor": supervisor_ref,
                "updated_at": _now(),
            }
            _write(root / "identity-gate.json", gate)
            sys.exit(1)
        elif args.spawn and args.spawn_authorized:
            _info(f"Spawning new {args.direction} side-split executor for {executor_provider}...")
            cwd = args.cwd or str(Path.cwd())
            spawned = cmux.spawn_executor(
                provider=executor_provider,
                cwd=cwd,
                focus=True,
                direction=args.direction,
                supervisor_surface=supervisor_ref,
            )
            executor_ref = spawned["ref"]
            _ok(f"Spawned side-split terminal executor: {executor_ref}")
            # Launch the agent CLI inside the terminal
            cmux.launch_agent_in_terminal(executor_ref, provider=executor_provider, cwd=cwd)
            _ok(f"Launched '{executor_provider}' CLI in {executor_ref}")
            time.sleep(3)  # let the agent initialise
        else:
            _fail(
                "NO_EXECUTOR_FOUND — resolve and explicitly bind the user's existing "
                "context-bearing executor; do not auto-spawn a replacement"
            )
            gate = {
                "task_id": args.task_id,
                "status": "FAIL",
                "reason": "NO_EXECUTOR_FOUND",
                "workspace_ref": me["workspace_ref"],
                "supervisor": supervisor_ref,
                "updated_at": _now(),
            }
            _write(root / "identity-gate.json", gate)
            sys.exit(1)

    # Discovery/spawn resolves a single ref; fold it back so the validation
    # below has exactly one code path for one executor and for many.
    executor_refs = executor_refs or [executor_ref]

    # Every executor must clear identity and terminal-channel validation before any
    # marker is written. Failing here writes a FAIL gate and no marker at all,
    # so a partially-validated set can never be armed.
    identities = {}
    workspace_proofs = {}
    canonical_refs = []
    canonical_providers = {}
    target_uuids = set()
    for index, ref in enumerate(executor_refs):
        try:
            expected_targets = getattr(args, "expected_executor_uuid", []) or []
            if expected_targets and len(expected_targets) != len(executor_refs):
                raise RuntimeError("WORKSPACE_SCOPE_DENIED: one expected UUID per executor required")
            proof = cmux.pin_workspace(
                ref, workspace_uuid=getattr(args, "expected_workspace_uuid", None),
                target_uuid=(expected_targets[index] if expected_targets else None),
            )
            identity = cmux.identify_surface(
                ref,
                workspace=me.get("workspace_ref") or None,
                window=me.get("window_ref") or None,
            )
            canonical = proof["target_surface_ref"]
            if (not re.fullmatch(r"surface:[0-9]+", canonical)
                    or identity.get("surface_ref") != canonical
                    or canonical in canonical_refs
                    or proof["target_surface_uuid"] in target_uuids):
                raise RuntimeError("EXECUTOR_SELECTOR_ALIAS_OR_IDENTITY_CHANGED")
            canonical_refs.append(canonical)
            target_uuids.add(proof["target_surface_uuid"])
            canonical_providers[canonical] = provider_overrides.get(ref, executor_provider)
            workspace_proofs[canonical] = proof
            identities[canonical] = identity
        except Exception as e:
            gate = {
                "task_id": args.task_id,
                "status": "FAIL",
                "reason": "EXECUTOR_IDENTITY_UNAVAILABLE",
                "error": str(e),
                "workspace_ref": me["workspace_ref"],
                "supervisor": supervisor_ref,
                "executor": ref,
                "executors": executor_refs,
                "failed_executor": ref,
                "updated_at": _now(),
            }
            _write(root / "identity-gate.json", gate)
            _fail(f"EXECUTOR_IDENTITY_UNAVAILABLE — cannot authenticate terminal {ref}")
            sys.exit(1)

    executor_refs = canonical_refs
    executor_ref = executor_refs[0]
    provider_overrides = canonical_providers
    executor_identity = identities[executor_ref]
    uuid_map = cmux.surface_uuid_map()
    supervisor_uuid = workspace_proofs[executor_ref]["caller_surface_uuid"]
    executor_uuid = workspace_proofs[executor_ref]["target_surface_uuid"]
    workspace_uuid = workspace_proofs[executor_ref]["workspace_uuid"]

    # Build the executors list for arm_task. The first executor's data also
    # fills the five singular fields for old readers.
    executors_list = []
    for ref in executor_refs:
        uuid = workspace_proofs[ref]["target_surface_uuid"]
        ws_ref = identities[ref].get("workspace_ref", "")
        pane_ref = identities[ref].get("pane_ref", "")
        executors_list.append({
            "surface_ref": ref,
            # Per-ref override when given, else the panel-wide --executor.
            "provider": provider_overrides.get(ref, executor_provider),
            "surface_uuid": uuid,
            "workspace_ref": ws_ref,
            "pane_ref": pane_ref,
            "workspace_uuid": workspace_proofs[ref]["workspace_uuid"],
            "pane_uuid": workspace_proofs[ref]["target_pane_uuid"],
            "surface_type": identities[ref].get("surface_type"),
        })

    panes = [p["pane_ref"] for p in executors_list]
    side_panel = bool(me.get("pane_ref") and all(panes)
                      and me["pane_ref"] not in panes and len(set(panes)) == len(panes))

    gate = {
        "task_id": args.task_id,
        "status": "PASS",
        "workspace_ref": me["workspace_ref"],
        "workspace_uuid": workspace_uuid,
        "supervisor": supervisor_ref,
        "supervisor_provider": supervisor_provider,
        "supervisor_pane_ref": me["pane_ref"],
        "supervisor_surface_uuid": supervisor_uuid,
        "supervisor_pane_uuid": workspace_proofs[executor_ref]["caller_pane_uuid"],
        "supervisor_surface_type": me.get("surface_type"),
        # Singular fields for compatibility: fill from the first executor.
        "executor": executor_ref,
        "executor_provider": provider_overrides.get(executor_ref, executor_provider),
        "executor_workspace_ref": executor_identity.get("workspace_ref", ""),
        "executor_pane_ref": executor_identity.get("pane_ref", ""),
        "executor_surface_uuid": executor_uuid,
        # New field holding all executors.
        "executors": executors_list,
        "panel_mode": spawned.get("panel_mode") if spawned else (
            "existing-side-panel" if side_panel else "existing-terminal-tabs"),
        "panel_direction": args.direction if spawned else "",
        "side_panel": side_panel,
        "terminal_channels_version": 1,
        "identity_source": "live-caller+live-tree+same-workspace-uuid",
        "workspace_proofs": workspace_proofs,
        "updated_at": _now(),
    }
    from cmux_terminal_channels import check_gate
    gate["terminal_channels"] = check_gate(gate)
    if not gate["terminal_channels"]["pass"]:
        gate.update(status="FAIL", reason="EXECUTOR_CHANNEL_IDENTITY_INVALID")
        _write(root / "identity-gate.json", gate)
        _fail("EXECUTOR_CHANNEL_IDENTITY_INVALID — " + gate["terminal_channels"].get("reason", ""))
        sys.exit(1)
    import cmux_hook_scope as hook_scope
    native_participants = hook_scope.bind_participants(cmux, workspace_uuid, [
        dict(role="supervisor", surface_uuid=supervisor_uuid, provider=supervisor_provider),
        *[dict(role="executor", surface_uuid=p["surface_uuid"], provider=p["provider"])
          for p in executors_list],
    ])
    gate["native_participants"] = native_participants
    _write(root / "identity-gate.json", gate)
    arm_task(
        args.task_id, root, supervisor=supervisor_ref,
        supervisor_provider=supervisor_provider,
        supervisor_surface_uuid=supervisor_uuid,
        workspace_uuid=workspace_uuid,
        executors=executors_list,
        native_participants=native_participants,
    )
    _ok(f"identity-gate PASS  supervisor={supervisor_ref}  executor(s)={','.join(executor_refs)}")
    _info("task ARMED — advisory hooks observe progress; acceptance requires explicit evidence review")
    if args.json:
        print(json.dumps(gate, indent=2))


# ---------------------------------------------------------------------------
# name-surfaces
# ---------------------------------------------------------------------------

def _recheck_workspace_gate(gate):
    for item in _gate_executors(gate):
        # Missing legacy UUIDs fail closed, rather than silently rebinding refs.
        if not all((gate.get("workspace_uuid"), gate.get("supervisor_surface_uuid"),
                    item.get("surface_uuid"))):
            raise RuntimeError("WORKSPACE_SCOPE_DENIED: gate lacks UUID pins; fresh identity required")
        cmux.pin_workspace(item["surface_ref"], workspace_uuid=gate["workspace_uuid"],
                           target_uuid=item["surface_uuid"], caller_uuid=gate["supervisor_surface_uuid"])


def cmd_name_surfaces(args):
    root = _artifact_root(args)
    gate = _read(root / "identity-gate.json")
    if not gate or gate.get("status") != "PASS":
        _fail("identity-gate.json not PASS — run identity-gate first")
        sys.exit(1)

    _recheck_workspace_gate(gate)

    supervisor_ref = gate["supervisor"]
    task_id = args.task_id
    sup_label = f"SUPERVISOR | {gate.get('supervisor_provider','?')} | {supervisor_ref}"

    # One row per participant: EXECUTOR_1, EXECUTOR_2, … derived from ordinal.
    # Machine roles are executor, executor2, executor3 to match role-map.
    targets = [(supervisor_ref, sup_label, "supervisor")]
    for item in _gate_executors(gate):
        ordinal = item["ordinal"]
        targets.append((
            item["surface_ref"],
            f"EXECUTOR_{ordinal} | {item.get('provider') or '?'} | {item['surface_ref']}",
            "executor" if ordinal == 1 else f"executor{ordinal}",
        ))

    entries = []
    target_uuids = {supervisor_ref: gate["supervisor_surface_uuid"],
                    **{item["surface_ref"]: item["surface_uuid"] for item in _gate_executors(gate)}}
    for ref, label, role in targets:
        try:
            _recheck_workspace_gate(gate)
            cmux.rename_tab(ref, label, workspace_uuid=gate["workspace_uuid"],
                            surface_uuid=target_uuids[ref], caller_uuid=gate["supervisor_surface_uuid"])
            entries.append({"surface_ref": ref, "role": role, "label": label, "status": "PASS"})
            _ok(f"renamed {ref} → '{label}'")
        except Exception as e:
            entries.append({"surface_ref": ref, "role": role, "label": label, "status": "FAIL", "error": str(e)})
            _fail(f"rename {ref} failed: {e}")

    all_pass = all(e["status"] == "PASS" for e in entries)
    proof = {
        "task_id": task_id,
        "status": "PASS" if all_pass else "FAIL",
        "workspace_ref": gate["workspace_ref"],
        "entries": entries,
        "blocking_reasons": [] if all_pass else ["rename failed"],
        "updated_at": _now(),
    }
    _write(root / "naming-proof.json", proof)
    if not all_pass:
        sys.exit(1)


# ---------------------------------------------------------------------------
# bridge-test (non-submitting: type token, bounded owned-token cleanup)
# ---------------------------------------------------------------------------

def _bridge_test_token(task_id, ordinal):
    # Identity lives in the evidence, not in dozens of characters requiring
    # guarded deletion. Keep each executor's probe short and independently fresh.
    return f"B{ordinal}_{secrets.token_hex(4)}"


def cmd_bridge_test(args):
    root = _artifact_root(args)
    gate = _read(root / "identity-gate.json")
    if not gate or gate.get("status") != "PASS":
        _fail("identity-gate.json not PASS — run identity-gate first")
        sys.exit(1)

    _recheck_workspace_gate(gate)

    executors = _gate_executors(gate)
    # Every executor gets its own token. A shared token would let one
    # executor's echo satisfy another executor's evidence — exactly the
    # "declared three, proved one" state these gates exist to prevent.
    per_executor = []
    for item in executors:
        token = _bridge_test_token(args.task_id, item["ordinal"])
        evidence, ok = _bridge_test_one(args, item["surface_ref"], token, item["ordinal"])
        per_executor.append(evidence)
        if not ok:
            _write_bridge_evidence(root, per_executor)
            sys.exit(1)

    _write_bridge_evidence(root, per_executor)
    _ok(
        f"bridge-test done for {len(per_executor)} executor(s)  "
        + "  ".join(
            f"{e['executor']}: observed={e['observed_in_screen']} (advisory) "
            f"clear_confirmed=True (attempts={e['clear_attempts']})"
            for e in per_executor
        )
    )


def _write_bridge_evidence(root, per_executor):
    """Publish bridge-test evidence.

    The first executor's fields stay at the top level so readers written
    against the single-executor shape keep working; ``executors`` carries every
    executor. Called even on failure so a mid-list abort still records what the
    earlier executors did.
    """
    evidence = dict(per_executor[0])
    evidence["executors"] = per_executor
    _write(root / "bridge-test-evidence.json", evidence)


def _bridge_test_one(args, executor_ref, token, ordinal):
    # A probe types and deletes too. Share the formal sender's exact pane and
    # target locks so an automatic continuation cannot race the cleanup.
    import cmux_input_mutex as mutex
    identity = cmux.pin_workspace(executor_ref)
    with mutex.hold(identity) as check:
        return _bridge_test_one_locked(args, executor_ref, token, ordinal, check)


def _bridge_test_one_locked(args, executor_ref, token, ordinal, check):
    """One full type-observe-clear cycle against one executor.

    Returns ``(evidence, ok)`` and writes nothing — the caller publishes, so a
    failure partway down the executor list still records the earlier results.
    """
    # Ownership precondition, checked BEFORE anything is typed.
    #
    # The clear step below deletes the compose line from column zero. That is
    # only safe if bridge-test owns the line, and ownership is a fact to be
    # observed, not assumed: the previous version pasted first and read second,
    # so the pre-paste state was never seen, while a comment asserted the line
    # was "otherwise empty". If a human had an unsubmitted prompt in the box, a
    # bounded forward-delete would have silently eaten up to
    # BRIDGE_TEST_CLEAR_DELETE_COUNT characters of it and still recorded
    # clear_confirmed=true, because the postcondition only asks whether the
    # token is gone.
    pre_screen = cmux.read_screen(executor_ref, lines=args.lines)
    forced_compose = bool(getattr(args, "force_compose", False))
    preexisting_compose = cmux.compose_block_text(pre_screen) or ""
    compose_was_occupied = not cmux.compose_block_is_empty(pre_screen)
    override = {
        "force_compose_override": forced_compose and compose_was_occupied,
        "preexisting_compose_sha256": hashlib.sha256(
            preexisting_compose.encode("utf-8")
        ).hexdigest() if compose_was_occupied else None,
        "preexisting_compose_preview": preexisting_compose[:240] if compose_was_occupied else None,
        "preexisting_compose_lines": len(preexisting_compose.splitlines()) if compose_was_occupied else 0,
    }
    boundary = None
    boundary_error = None

    def input_busy(screen):
        # 只用于本次不提交的 probe。正式投递和全局 busy 判定不变。
        # 中断例外每次都重核原生尾部及进程；失效后不降回普通屏幕判断。
        return (not boundary.check(screen) if boundary is not None
                else cmux._queued_or_active_input(screen))

    def boundary_evidence():
        return {"interrupted_bash_boundary": boundary.evidence if boundary else None,
                "interrupted_bash_refusal": boundary_error}

    def refuse_input(reason):
        _fail(
            "COMPOSE_OCCUPIED — " + reason + ". Refusing to type or clear; "
            "preserve the receiver's input and wait for an idle empty editor."
        )
        return {
            "task_id": args.task_id,
            "executor": executor_ref,
            "ordinal": ordinal,
            "token": token,
            "status": "COMPOSE_OCCUPIED",
            "submission_state": cmux.COMPOSE_OCCUPIED,
            "observed_in_screen": False,
            "clear_confirmed": False,
            "clear_attempts": 0,
            "clear_key_count": 0,
            "clear_verified_at": None,
            "pre_read_performed": True,
            "compose_was_empty_before_send": cmux.compose_block_is_empty(pre_screen),
            "active_or_queued_before_send": cmux._queued_or_active_input(pre_screen),
            "token_sent": False,
            "override": override,
            **boundary_evidence(),
            "note": (
                "Pre-paste observation refused the token. No bridge token was "
                "typed or submitted; any prior authorized clear actions are "
                "recorded separately in override."
            ),
            "updated_at": _now(),
        }, False

    # Empty compose and idle receiver are distinct facts. In particular, Claude
    # can expose an empty bordered editor while a Bash tool is still running.
    # Even an explicit compose override does not authorize input to that task.
    if cmux._queued_or_active_input(pre_screen):
        if not compose_was_occupied and not forced_compose:
            try:
                boundary = interrupted_probe.InterruptedBashBoundary(
                    cmux, executor_ref, pre_screen, token)
            except interrupted_probe.PROBE_ERRORS as exc:
                boundary_error = (type(exc).__name__ + ": " + str(exc))[:240]
        if boundary is None:
            return refuse_input("active/queued work owns the executor")
    if compose_was_occupied and not forced_compose:
        return refuse_input("the executor's live compose block is not empty")

    if compose_was_occupied and forced_compose:
        _info("Force-compose override: discarding the current compose with Esc")
        override["clear_actions"] = ["escape"]
        override["clear_confirmed"] = False
        override["clear_verified_at"] = None
        override["delete_count"] = 0
        check()
        cmux.send_key(executor_ref, "escape")
        time.sleep(BRIDGE_TEST_CLEAR_DELAY_SECONDS)
        pre_screen = cmux.read_screen(executor_ref, lines=args.lines)
        if cmux._queued_or_active_input(pre_screen):
            return refuse_input("active/queued work appeared during authorized compose clearing")
        if not cmux.compose_block_is_empty(pre_screen):
            # Claude may leave the compose unchanged after Escape.  Since the
            # operator explicitly authorized discarding this fingerprinted
            # buffer, focus its pane and try the terminal line-clear key once.
            cmux.focus_surface(executor_ref)
            override["clear_actions"].append("focused_ctrl+u")
            check()
            cmux.send_key(executor_ref, "ctrl+u")
            time.sleep(BRIDGE_TEST_CLEAR_DELAY_SECONDS)
            pre_screen = cmux.read_screen(executor_ref, lines=args.lines)
            if not cmux.compose_block_is_empty(pre_screen):
                # Ctrl+C is allowed only while the same visible compose block
                # is idle. A queued message or active command is not compose
                # ownership, even under an explicit force override.
                if not cmux._queued_or_active_input(pre_screen):
                    override["clear_actions"].append("ctrl+c")
                    check()
                    cmux.send_key(executor_ref, "ctrl+c")
                    time.sleep(BRIDGE_TEST_CLEAR_DELAY_SECONDS)
                    pre_screen = cmux.read_screen(executor_ref, lines=args.lines)

                if (
                    not cmux.compose_block_is_empty(pre_screen)
                    and not cmux._queued_or_active_input(pre_screen)
                ):
                    # Claude's multiline editor can ignore Escape, Ctrl+U and
                    # Ctrl+C. The buffer was fingerprinted before mutation and
                    # explicitly authorized for replacement, so use the same
                    # length-derived, capped deletion fallback as submit_text.
                    override["clear_actions"].append("end+bounded_backspace")
                    check()
                    override["delete_count"] = cmux.clear_known_compose_by_delete(
                        executor_ref, preexisting_compose
                    )
                    time.sleep(BRIDGE_TEST_CLEAR_DELAY_SECONDS)
                    pre_screen = cmux.read_screen(executor_ref, lines=args.lines)

                if (
                    not cmux.compose_block_is_empty(pre_screen)
                    and cmux._queued_or_active_input(pre_screen)
                ):
                    active = cmux._queued_or_active_input(pre_screen)
                    reason = (
                        "active/queued input owns the receiver"
                        if active
                        else "the verified buffer remained occupied"
                    )
                    _fail(
                        "FORCE_COMPOSE_CLEAR_FAILED — explicit override did not "
                        f"reach an empty compose block ({reason}); refusing to "
                        "stack or submit a prompt"
                    )
                    return {
                        "task_id": args.task_id,
                        "executor": executor_ref,
                        "ordinal": ordinal,
                        "token": token,
                        "status": "FORCE_COMPOSE_CLEAR_FAILED",
                        "submission_state": cmux.COMPOSE_OCCUPIED,
                        "observed_in_screen": False,
                        "clear_confirmed": False,
                        "clear_attempts": 0,
                        "clear_verified_at": None,
                        "pre_read_performed": True,
                        "compose_was_empty_before_send": False,
                        "token_sent": False,
                        "override": override,
                        "updated_at": _now(),
                    }, False

                if not cmux.compose_block_is_empty(pre_screen):
                    # Claude Code can render an uneditable virtual suggestion
                    # in an otherwise empty compose buffer. Once the explicit
                    # override has exhausted the bounded real-buffer clear, the
                    # bridge token itself atomically replaces that suggestion.
                    override["direct_replace_after_clear_attempts"] = True

        override["clear_confirmed"] = cmux.compose_block_is_empty(pre_screen)
        override["clear_verified_at"] = _now() if override["clear_confirmed"] else None

    if boundary is not None:
        pre_screen = cmux.read_screen(executor_ref, lines=args.lines)
        if not cmux.compose_block_is_empty(pre_screen):
            return refuse_input("compose changed while verifying the native interruption")
    if input_busy(pre_screen):
        return refuse_input("active/queued work appeared before the bridge token")

    # Only the actual pre-paste screen can prove restoration of an explicitly
    # authorized virtual suggestion. The earlier occupied buffer is not enough:
    # force cleanup may already have changed it.
    restore_text = (
        cmux.compose_rendered_text(pre_screen)
        if override.get("direct_replace_after_clear_attempts") else None
    )
    _info(f"Sending non-submitting bridge token to {executor_ref}: {token!r}")
    check()
    cmux.send_text(executor_ref, token)  # no newline
    time.sleep(1)
    screen = cmux.read_screen(executor_ref, lines=args.lines)
    observed = token in screen

    def cleared(screen):
        if input_busy(screen):
            return None
        if cmux.compose_block_is_empty(screen):
            return "EMPTY_COMPOSE"
        if (
            restore_text is not None
            and cmux.compose_rendered_text(screen) == restore_text
            and not input_busy(screen)
        ):
            return "AUTHORIZED_PRE_PASTE_RESTORED"
        return None

    # A missing full token does not prove an empty editor. Delete only a
    # positively observed prefix of our own token (backspace leaves prefixes).
    # Missing glyphs, queued work, and foreign text get bounded re-reads only.
    # Every key still goes through the live workspace/UUID guard.
    clear_confirmed = False
    clear_attempts = 0
    clear_key_count = 0
    clear_confirmed_by = None
    clear_observations = []
    post_clear_screen = screen
    for attempt in range(1, BRIDGE_TEST_CLEAR_MAX_ATTEMPTS + 1):
        clear_attempts = attempt
        body = cmux.compose_rendered_text(post_clear_screen)
        owned = (
            bool(body) and token.startswith(body)
            and not input_busy(post_clear_screen)
        )
        delete_count = 0
        if not cleared(post_clear_screen) and owned:
            limit = min(BRIDGE_TEST_CLEAR_DELETE_COUNT, len(body))
            if boundary is None:
                check()
                cmux.send_key(executor_ref, "end")
                for _ in range(limit):
                    check()
                    cmux.send_key(executor_ref, "backspace")
                    time.sleep(BRIDGE_TEST_CLEAR_KEY_DELAY_SECONDS)
                delete_count = limit
            else:
                # 用户可随时输入。中断 HUD 的例外逐键重读；仅删仍完整可见的
                # 自有 token 前缀，不用刚才的屏幕为后续按键继续授权。
                fresh = cmux.read_screen(executor_ref, lines=args.lines)
                if not input_busy(fresh) and cmux.compose_rendered_text(fresh) == body:
                    check()
                    cmux.send_key(executor_ref, "end")
                    for _ in range(limit):
                        fresh = cmux.read_screen(executor_ref, lines=args.lines)
                        current_body = cmux.compose_rendered_text(fresh)
                        if (input_busy(fresh) or not current_body
                                or not token.startswith(current_body)):
                            break
                        check()
                        cmux.send_key(executor_ref, "backspace")
                        delete_count += 1
                        time.sleep(BRIDGE_TEST_CLEAR_KEY_DELAY_SECONDS)
            clear_key_count += delete_count
        clear_observations.append({
            "attempt": attempt,
            "compose_observed": body is not None,
            "owned_token_prefix": bool(owned),
            "delete_count": delete_count,
            "screen_sha256": hashlib.sha256(post_clear_screen.encode("utf-8")).hexdigest(),
        })
        time.sleep(BRIDGE_TEST_CLEAR_DELAY_SECONDS)
        post_clear_screen = cmux.read_screen(executor_ref, lines=args.lines)
        clear_confirmed_by = cleared(post_clear_screen)
        if clear_confirmed_by:
            clear_confirmed = True
            break

    evidence = {
        "task_id": args.task_id,
        "executor": executor_ref,
        "ordinal": ordinal,
        "token": token,
        "observed_in_screen": observed,
        "clear_confirmed": clear_confirmed,
        "clear_attempts": clear_attempts,
        "clear_key_count": clear_key_count,
        "clear_confirmed_by": clear_confirmed_by,
        "clear_observations": clear_observations,
        "clear_verified_at": _now() if clear_confirmed else None,
        "pre_read_performed": True,
        "compose_was_empty_before_send": not compose_was_occupied,
        "token_sent": True,
        "override": override,
        **boundary_evidence(),
        "note": (
            "observed_in_screen is advisory — some TUIs hide unsubmitted input. "
            "clear_confirmed is the load-bearing field: handshake requires it. "
            "compose_was_empty_before_send records the normal ownership proof. "
            "When force-compose is explicitly enabled, override records the "
            "pre-clear fingerprint and Esc discard instead."
        ),
        "updated_at": _now(),
    }

    if not clear_confirmed:
        _fail(
            f"BRIDGE_TEST_UNCONFIRMED — compose cleanup was not verified after "
            f"{clear_attempts} bounded observations on {executor_ref}. "
            "Preserve the evidence; do not submit a handshake behind unknown input."
        )
        return evidence, False

    return evidence, True


# ---------------------------------------------------------------------------
# handshake
# ---------------------------------------------------------------------------

def _bridge_clear_binding(args, root, gate):
    """Narrow recovery of one already-sent bridge token; never a new dispatch."""
    if (not isinstance(gate, dict) or gate.get("task_id") != args.task_id or
            getattr(args, "force_compose", False)):
        raise ValueError("recovery requires the original task gate and no force-compose")
    items = _gate_executors(gate)
    original = root / "bridge-test-evidence.json"
    if original.is_symlink() or (root / "identity-gate.json").is_symlink():
        raise ValueError("recovery may not follow substituted evidence symlinks")
    original_bytes = original.read_bytes()
    gate_bytes = (root / "identity-gate.json").read_bytes()
    if json.loads(gate_bytes) != gate:
        raise ValueError("identity gate changed before recovery binding")
    ev = json.loads(original_bytes)
    if (not isinstance(ev, dict) or gate.get("status") != "PASS" or len(items) != 1 or
            ev.get("task_id") != args.task_id or
            ev.get("executor") != items[0]["surface_ref"] or
            ev.get("clear_confirmed") is not False or
            ev.get("pre_read_performed") is not True or
            ev.get("compose_was_empty_before_send") is not True or
            ev.get("token_sent") is not True):
        raise ValueError("recovery requires one bound, previously owned, sent bridge token")
    leaves = ev.get("executors", [ev])
    if (not isinstance(leaves, list) or len(leaves) != 1 or
            not isinstance(leaves[0], dict) or
            any(leaves[0].get(key) != ev.get(key) for key in
                ("task_id", "executor", "token", "clear_confirmed",
                 "pre_read_performed", "compose_was_empty_before_send", "token_sent"))):
        raise ValueError("single executor bridge evidence is inconsistent")
    return items[0], {
        "task_id": args.task_id,
        "executor": items[0]["surface_ref"],
        "original_bridge_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "identity_gate_sha256": hashlib.sha256(gate_bytes).hexdigest(),
    }


def _clear_recovery_path(args, root):
    value = getattr(args, "bridge_clear_recovery", None)
    if not value:
        raise ValueError("--bridge-clear-recovery is required")
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or
            path.parent.resolve() != root.resolve() or
            path.name in {"identity-gate.json", "bridge-test-evidence.json", "handshake-receipt.json", "task-pack.json"}):
        raise ValueError("recovery receipt must be a distinct nonsymlink sibling of original evidence")
    return path


def _empty_idle_agent_screen(screen):
    return (cmux.receiver_input_kind(screen) == "AGENT_TUI" and
            cmux.compose_block_is_empty(screen) and
            not cmux._queued_or_active_input(screen))


def cmd_bridge_clear_observe(args):
    """Zero-input observation; preserve the failed bridge evidence verbatim."""
    root = _artifact_root(args)
    gate = _read(root / "identity-gate.json") or {}
    try:
        path = _clear_recovery_path(args, root)
        if path.exists():
            raise ValueError("recovery receipt already exists; preserve it")
        if any((root / name).exists() for name in ("handshake-receipt.json", "task-pack.json")):
            raise ValueError("handshake/task already started; do not replay its precondition")
        item, binding = _bridge_clear_binding(args, root, gate)
        _recheck_workspace_gate(gate)
        screen = cmux.read_screen(item["surface_ref"], lines=args.lines)
        _recheck_workspace_gate(gate)
        if _bridge_clear_binding(args, root, gate)[1] != binding:
            raise ValueError("original evidence changed during observation")
        ok = _empty_idle_agent_screen(screen)
        receipt = dict(binding, status="PASS" if ok else "REFUSED",
                       terminal_input_sent=False, clear_confirmed_by="EMPTY_COMPOSE_READ_ONLY" if ok else None,
                       screen=screen, screen_sha256=hashlib.sha256(screen.encode("utf-8")).hexdigest(),
                       checked_at=_now())
        with path.open("x", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not ok:
            raise ValueError("current receiver is not a verified empty idle agent; zero input sent")
        _ok(f"bridge clear observed without input; original failure preserved; receipt={path}")
    except (OSError, ValueError, RuntimeError) as exc:
        _fail(str(exc))
        sys.exit(1)


def _recovered_bridge_clear(args, root, gate):
    """Bind the original failure, saved observation, and a fresh live recheck."""
    path = _clear_recovery_path(args, root)
    item, binding = _bridge_clear_binding(args, root, gate)
    receipt_bytes = path.read_bytes()
    receipt = json.loads(receipt_bytes)
    if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in binding.items()):
        raise ValueError("recovery receipt binding mismatch")
    screen = receipt.get("screen")
    if (receipt.get("status") != "PASS" or receipt.get("terminal_input_sent") is not False or
            receipt.get("clear_confirmed_by") != "EMPTY_COMPOSE_READ_ONLY" or
            not isinstance(screen, str) or
            hashlib.sha256(screen.encode("utf-8")).hexdigest() != receipt.get("screen_sha256") or
            not _empty_idle_agent_screen(screen)):
        raise ValueError("recovery receipt lacks a valid saved empty idle observation")
    _recheck_workspace_gate(gate)
    current = cmux.read_screen(item["surface_ref"], lines=args.lines)
    _recheck_workspace_gate(gate)
    if not _empty_idle_agent_screen(current):
        raise ValueError("receiver changed after observation; refuse handshake")
    if _bridge_clear_binding(args, root, gate)[1] != binding:
        raise ValueError("original binding changed before handshake")
    if path.read_bytes() != receipt_bytes:
        raise ValueError("recovery receipt changed before handshake")
    return {"path": str(path), "sha256": hashlib.sha256(receipt_bytes).hexdigest()}


def cmd_handshake(args):
    root = _artifact_root(args)
    if any(os.path.lexists(root / name) for name in ("handshake-receipt.json", "task-pack.json")):
        _fail("original handshake already started; reconcile its original ACK or use continue_handshake.py; no replay")
        sys.exit(1)
    gate = _read(root / "identity-gate.json")
    if not gate or gate.get("status") != "PASS":
        _fail("identity-gate.json not PASS — run identity-gate first")
        sys.exit(1)

    _recheck_workspace_gate(gate)

    executors = _gate_executors(gate)
    # Each executor gets its own nonce and must ACK it. Reusing one nonce across
    # executors would let the first executor's reply satisfy the whole set — the
    # "declared three, proved one" state the marker must never reach. Abort on
    # the first failure so no later executor is credited without evidence.
    per_executor = []
    for item in executors:
        if not _handshake_one(args, root, gate, item, per_executor):
            _write_handshake_receipt(root, per_executor)
            sys.exit(1)

    _write_handshake_receipt(root, per_executor)
    if len(per_executor) > 1:
        _ok(f"handshake PASS for all {len(per_executor)} executors")


def _write_handshake_receipt(root, per_executor):
    """Publish the handshake receipt.

    The first executor's identity fields stay at the top level so every existing
    reader — validate's handshake_strict, the receipt command, the receipt guard
    hook — keeps working unchanged; ``executors`` carries the full set. Written
    even on failure so an abort partway down the list still records what
    happened.

    The verdict fields, however, are the panel's and not executor 1's. Copying
    executor 1 wholesale published status=PASS / executor_ack=true for a panel
    where executor 2 timed out, and handshake_strict reads exactly those fields —
    a marker naming three reviewers would clear the gate on one reviewer's ACK.
    So a single unproven executor downgrades the top-level verdict.
    """
    from handshake_panel import aggregate
    gate = _read(root / "identity-gate.json")
    members = _gate_executors(gate) if gate else None
    _write(root / "handshake-receipt.json", aggregate(per_executor, members))


def handshake_without_probe(receipt, *, bridge_evidence_present):
    """Recognize this controller's honest no-probe receipt, never a legacy bypass."""
    return (not bridge_evidence_present
            and receipt.get("bridge_probe_policy") == HANDSHAKE_PROBE_POLICY
            and receipt.get("bridge_probe_status") == "NOT_RUN"
            and "bridge_clear_confirmed" in receipt
            and receipt["bridge_clear_confirmed"] is None
            and "bridge_clear_recovery" in receipt
            and receipt["bridge_clear_recovery"] is None)


def _handshake_bridge_context(args, root, gate, executor_ref):
    """Keep any explicit probe's postcondition; absence grants no clear proof."""
    path = root / "bridge-test-evidence.json"
    context = {"bridge_probe_policy": HANDSHAKE_PROBE_POLICY,
               "bridge_probe_status": "NOT_RUN", "bridge_clear_confirmed": None,
               "bridge_clear_recovery": None}
    if not path.exists() and not path.is_symlink():
        return context
    bridge_all = _read(path)
    if not isinstance(bridge_all, dict) or bridge_all.get("task_id") != args.task_id:
        raise ValueError("BRIDGE_TEST_ORIGINAL_EVIDENCE_INVALID_OR_DIFFERENT_TASK")
    rows = bridge_all["executors"] if "executors" in bridge_all else [bridge_all]
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("BRIDGE_TEST_ORIGINAL_EXECUTORS_INVALID")
    matched = [row for row in rows if row.get("executor") == executor_ref]
    if len(matched) != 1:
        raise ValueError("BRIDGE_TEST_ORIGINAL_EXECUTOR_MISSING_OR_DUPLICATED")
    if matched[0].get("clear_confirmed") is True:
        context.update(bridge_probe_status="CLEARED", bridge_clear_confirmed=True)
    else:
        recovery = _recovered_bridge_clear(args, root, gate)
        context.update(bridge_probe_status="CLEAR_RECOVERED", bridge_clear_confirmed=True,
                       bridge_clear_recovery=recovery)
    return context


def _handshake_one(args, root, gate, item, per_executor):
    """One full challenge-ACK cycle against one executor.

    Appends its receipt to ``per_executor`` and returns True on ACK. Every
    intermediate state is published through ``_write_handshake_receipt`` so a
    cautious executor can verify its own pending nonce on disk before replying —
    which is why the accumulating list is threaded through rather than returned.
    """
    executor_ref = item["surface_ref"]
    ordinal = item["ordinal"]
    supervisor_ref = gate["supervisor"]
    executor_provider = item.get("provider") or "agent"
    task_id = args.task_id

    # Fresh handshakes send directly through the guarded bridge. Existing
    # diagnostic evidence is never ignored, rewritten or credited to a peer.
    try:
        bridge_context = _handshake_bridge_context(args, root, gate, executor_ref)
    except (OSError, ValueError, RuntimeError) as exc:
        _fail("BRIDGE_TEST_CLEAR_UNCONFIRMED — " + str(exc))
        sys.exit(1)

    # One fresh nonce per executor. secrets.token_hex is called once per
    # executor, so two executors cannot be issued the same challenge.
    ack_nonce = secrets.token_hex(4)
    ack_line_expected = f"PREFLIGHT_ACK|{task_id}|{executor_provider}:identity|READY|INLINE|{ack_nonce}"

    # Which budget is actually in force, and did a generic override undercut it?
    #
    # The 182 s false FAIL in this task was NOT a wrong default: cmd_handshake
    # already resolves 600 s. A generic `--timeout` wins unconditionally in
    # _effective_timeout, so an explicit 180 s silently downgraded the cold
    # handshake to the round budget. Recording the provenance means a future
    # timeout can be attributed correctly instead of blamed on the executor.
    handshake_timeout = _effective_timeout(args, "handshake")
    budget_source = (
        "generic_timeout_override"
        if getattr(args, "timeout", None) is not None
        else "phase_handshake_default"
    )
    phase_minimum = PHASE_MINIMUM_BUDGET_SECONDS["handshake"]
    below_minimum = handshake_timeout < phase_minimum

    receipt = {
        "task_id": task_id,
        "status": "HELLO_SENT",
        "lifecycle": "PENDING",
        "supervisor": supervisor_ref,
        "executor": executor_ref,
        "ordinal": ordinal,
        "executor_provider": executor_provider,
        "workspace_ref": gate.get("workspace_ref"),
        "executor_ack": False,
        "ack_line": None,
        "ack_line_expected": ack_line_expected,
        "ack_nonce": ack_nonce,
        "ack_format": "compact-nonce-v1",
        "ack_source": None,
        "ack_response_marker": False,
        "prompt_contains_literal_ack": False,
        **bridge_context,
        "budget_source": budget_source,
        "budget_seconds": handshake_timeout,
        "phase_minimum_seconds": phase_minimum,
        "budget_below_phase_minimum": below_minimum,
        "submission_state": None,
        "detector_side": None,
        "created_at": _now(),
        "dispatch_started_at": None,
        "dispatch_submitted_at": None,
        "ack_observed_at": None,
        "parser_confirmed_at": None,
        "terminal_error_at": None,
        "updated_at": _now(),
    }
    if below_minimum:
        _info(
            f"WARNING: effective handshake budget {handshake_timeout}s is below the "
            f"{phase_minimum}s phase minimum (source={budget_source}). A timeout will "
            f"be recorded as SUPERVISOR_BUDGET_TOO_SHORT, not executor silence."
        )
    # Persist the nonce before sending it. This lets a cautious executor verify
    # that the challenge came from the harness instead of trusting prompt text.
    per_executor.append(receipt)
    _write_handshake_receipt(root, per_executor)

    import handshake_evidence
    hello_msg = handshake_evidence.challenge(receipt, root, COLLABORATION_SKILL_PATH)

    receipt["dispatch_started_at"] = _now()
    receipt["updated_at"] = _now()
    _write_handshake_receipt(root, per_executor)
    _info(f"Sending handshake to {executor_ref} (EXECUTOR_{ordinal})...")
    try:
        # Only this newly created challenge is prepared as a bounded notice.
        # Old nonces/attempts retain their original bytes and late-ACK path.
        import cmux_prompt_reference as prompt_reference
        prepared = prompt_reference.plan(hello_msg, ack_nonce)
        prompt_reference.persist(prepared['reference'], hello_msg)
        receipt['dispatch_payload_sha256'] = prompt_reference.digest(prepared['text'])
        receipt['dispatch_payload'] = prepared['text']
        receipt['dispatch_body_reference'] = prepared['reference']
        receipt['dispatch_confirmation_scope'] = 'reference_notice' if prepared['reference'] else 'inline_payload'
        import cmux_message_journal
        handshake_evidence.record_controller(receipt, cmux_message_journal.__file__,
                                             COLLABORATION_SKILL_PATH, __file__, cmux.__file__)
        _write_handshake_receipt(root, per_executor)
        submit_kwargs = {"marker": ack_nonce}
        if getattr(args, "force_compose", False):
            submit_kwargs["force_compose"] = True
        result = cmux.submit_text(executor_ref, prepared['text'], **submit_kwargs)
        handshake_evidence.record_dispatch(receipt, result)
        receipt["dispatch_submitted_at"] = _now()
        receipt["updated_at"] = _now()
        _write_handshake_receipt(root, per_executor)
    except cmux.DispatchUnconfirmed as e:
        handshake_evidence.record_dispatch(receipt, getattr(e, 'details', None), failed=True)
        state = getattr(e, "state", None) or cmux.SUPERVISOR_DID_NOT_SUBMIT
        receipt["submission_state"] = state
        receipt["detector_side"] = "supervisor"
        receipt["dispatch_error"] = str(e)
        receipt["dispatch_error_at"] = _now()
        receipt["updated_at"] = _now()
        if state not in (
            cmux.DELIVERY_UNVERIFIED_BY_DETECTOR,
            cmux.DELIVERY_QUEUED_AT_RECEIVER,
        ):
            receipt["status"] = "FAIL"
            receipt["lifecycle"] = "REJECTED"
            receipt["error"] = str(e)
            receipt["terminal_error_at"] = _now()
            _write_handshake_receipt(root, per_executor)
            _fail(f"{state} — {e}")
            return False
        # Input may already have reached the executor. Preserve the original
        # transport failure and poll the exact challenge through the ordinary
        # strict ACK parser. Never retry paste/Enter or invent a submitted time.
        receipt["late_ack_recovery_attempted"] = True
        receipt["late_ack_recovered"] = False
        _write_handshake_receipt(root, per_executor)
        _info(f"{state} — polling the original nonce; no resend.")
    except cmux.TaskPackContractError as e:
        # A contract failure may precede the journal, or follow actual input.
        # Settle this supervisor workflow without inventing NO_INPUT evidence
        # or allowing a retry. Only preserve details returned by the sender.
        handshake_evidence.record_dispatch(receipt, getattr(e, "details", None), failed=True)
        failed_at = _now()
        receipt.update(
            status="FAIL", lifecycle="REJECTED", detector_side="supervisor",
            attributable_to_executor=False, dispatch_error_type=type(e).__name__,
            dispatch_error=str(e), error=str(e), dispatch_error_at=failed_at,
            terminal_error_at=failed_at, updated_at=failed_at,
        )
        _write_handshake_receipt(root, per_executor)
        _fail(f"SUPERVISOR_DISPATCH_CONTRACT_ERROR — {e}")
        return False

    _info(f"Polling for PREFLIGHT_ACK (timeout={handshake_timeout}s, poll=3s)...")
    try:
        ack_line = cmux.wait_for_ack(
            executor_ref,
            ack_prefix="PREFLIGHT_ACK",
            timeout=handshake_timeout,
            poll=3,
            lines=args.lines,
            task_id=task_id,
            provider=executor_provider,
            nonce=ack_nonce,
        )
        receipt["executor_ack"] = True
        receipt["ack_line"] = ack_line
        receipt["ack_source"] = "executor_response_nonce"
        receipt["ack_response_marker"] = True
        receipt["ack_observed_at"] = _now()
        receipt["parser_confirmed_at"] = receipt["ack_observed_at"]
        receipt["status"] = "PASS"
        if receipt.get("late_ack_recovery_attempted"):
            receipt["late_ack_recovered"] = True
        _ok(f"ACK received: {ack_line}")
    except TimeoutError as e:
        # Two different facts wear one word. A timeout under a budget that was
        # never large enough is a SUPERVISOR problem; the executor may have been
        # working correctly the whole time. Attributing it to executor silence is
        # what produced this task's 182 s false FAIL against a byte-exact ACK.
        ack_state = (
            "SUPERVISOR_BUDGET_TOO_SHORT" if below_minimum else "HANDSHAKE_TIMEOUT"
        )
        receipt["status"] = "FAIL"
        receipt["lifecycle"] = "EXPIRED"
        receipt["ack_state"] = ack_state
        receipt["detector_side"] = (
            "supervisor" if (below_minimum or receipt.get("late_ack_recovery_attempted")) else "executor"
        )
        receipt["attributable_to_executor"] = (
            not below_minimum and not receipt.get("late_ack_recovery_attempted", False)
        )
        receipt["error"] = str(e)
        receipt["terminal_error_at"] = _now()
        if below_minimum:
            _fail(
                f"SUPERVISOR_BUDGET_TOO_SHORT — polled {handshake_timeout}s but the "
                f"handshake phase minimum is {phase_minimum}s (source={budget_source}). "
                f"This is NOT evidence of executor silence. Rerun without --timeout, "
                f"or with --handshake-timeout >= {phase_minimum}."
            )
        else:
            _fail(f"HANDSHAKE_TIMEOUT — {e}")

    receipt["updated_at"] = _now()
    if receipt["status"] != "PASS":
        return False
    receipt["lifecycle"] = "ACKED"
    receipt["attributable_to_executor"] = False
    return True


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

def cmd_validate(args):
    root = _artifact_root(args)
    task_id = args.task_id
    checks = {}

    def _check(name, path, required_status="PASS"):
        data = _read(root / path)
        if data is None:
            checks[name] = {"status": "MISSING", "path": str(root / path)}
            return
        st = data.get("status", "UNKNOWN")
        checks[name] = {"status": st, "pass": st == required_status}

    _check("identity_gate",   "identity-gate.json")
    _check("naming_proof",    "naming-proof.json")
    _check("handshake",       "handshake-receipt.json")

    handshake_data = _read(root / "handshake-receipt.json") or {}
    ack_nonce = handshake_data.get("ack_nonce", "")
    ack_line = handshake_data.get("ack_line") or ""
    handshake_strict_ok = (
        handshake_data.get("status") == "PASS"
        and handshake_data.get("executor_ack") is True
        and handshake_data.get("ack_source") == "executor_response_nonce"
        and handshake_data.get("ack_response_marker") is True
        and bool(ack_nonce)
        and ack_nonce in ack_line
    )
    checks["handshake_strict"] = {
        "status": "PASS" if handshake_strict_ok else "FAIL",
        "pass": handshake_strict_ok,
        "ack_source": handshake_data.get("ack_source", ""),
        "ack_format": handshake_data.get("ack_format", ""),
        "ack_nonce_present": bool(ack_nonce),
        "ack_line_contains_nonce": bool(ack_nonce and ack_nonce in ack_line),
        "ack_response_marker": handshake_data.get("ack_response_marker"),
        "prompt_contains_literal_ack": handshake_data.get("prompt_contains_literal_ack"),
    }

    # Optional diagnostic: absence is not a failed probe or proof of clearing.
    bridge_data = _read(root / "bridge-test-evidence.json")
    bridge_ok = bridge_data is not None
    checks["bridge_test"] = {
        "status": "present" if bridge_ok else "NOT_RUN",
        "advisory": True,
        "observed": bridge_data.get("observed_in_screen", False) if bridge_data else False,
    }

    gate_data = _read(root / "identity-gate.json") or {}
    from handshake_panel import matches
    gate_members = _gate_executors(gate_data)
    panel_rows = handshake_data.get("executors", [handshake_data])
    panel_complete = (isinstance(panel_rows, list) and len(panel_rows) == len(gate_members)
                      and all(isinstance(row, dict) and matches(row, member)
                              and row.get("executor_ack") is True
                              for row, member in zip(panel_rows, gate_members)))
    checks["handshake_panel"] = {"pass": panel_complete,
        "status": "PASS" if panel_complete else "FAIL", "expected_executor_count": len(gate_members)}
    # Retain the actual pane layout as a diagnostic. New gates independently
    # prove distinct terminal identities; sharing a pane does not deny messages.
    sup_pane = gate_data.get("supervisor_pane_ref", "")
    exec_panes = []
    for item in _gate_executors(gate_data):
        exec_panes.append({
            "executor": item["surface_ref"],
            "ordinal": item["ordinal"],
            "pane_ref": item.get("pane_ref", "") or gate_data.get("executor_pane_ref", ""),
        })
    panes = [e["pane_ref"] for e in exec_panes]
    side_panel_ok = (
        bool(gate_data.get("side_panel"))
        and bool(sup_pane)
        and bool(panes)
        and all(panes)
        and sup_pane not in panes
        and len(set(panes)) == len(panes)
    )
    checks["side_panel"] = {
        "status": "PASS" if side_panel_ok else "FAIL",
        "pass": side_panel_ok,
        "supervisor_pane_ref": sup_pane,
        # Singular field kept for existing readers.
        "executor_pane_ref": gate_data.get("executor_pane_ref", ""),
        "executor_panes": exec_panes,
        "executor_count": len(exec_panes),
        "all_panes_distinct": bool(panes) and len(set(panes)) == len(panes),
        "panel_mode": gate_data.get("panel_mode", ""),
    }
    channel_ok = side_panel_ok  # Historical gates keep their original contract.
    if "terminal_channels_version" in gate_data:
        from cmux_terminal_channels import check_gate
        checks["terminal_channels"] = check_gate(gate_data)
        channel_ok = checks["terminal_channels"]["pass"]
        checks["side_panel"]["advisory"] = True
        if channel_ok and not side_panel_ok:
            checks["side_panel"]["status"] = "SHARED_PANE"

    gate_pass      = checks.get("identity_gate", {}).get("pass", False)
    naming_pass    = checks.get("naming_proof",  {}).get("pass", False)
    handshake_pass = checks.get("handshake",     {}).get("pass", False) and handshake_strict_ok and panel_complete

    overall = "PASS" if (gate_pass and naming_pass and handshake_pass and channel_ok) else "FAIL"

    validation = {
        "task_id": task_id,
        "status": overall,
        "checks": checks,
        "bridge_test_advisory": bridge_ok,
        "artifact_root": str(root),
        "updated_at": _now(),
    }
    _write(root / "validation.json", validation)

    if overall == "PASS":
        _ok("validation PASS — ready for task dispatch")
    else:
        failing = [k for k, v in checks.items() if not v.get("pass", False)
                   and not v.get("advisory", False)]
        _fail(f"validation FAIL — failing checks: {failing}")
        sys.exit(1)

    if args.json:
        print(json.dumps(validation, indent=2))


# ---------------------------------------------------------------------------
# task-pack (scaffold)
# ---------------------------------------------------------------------------

def cmd_task_pack(args):
    root = _artifact_root(args)
    _ensure_root(root, args.task_id)
    gate = _read(root / "identity-gate.json") or {}
    val  = _read(root / "validation.json") or {}

    if val.get("status") != "PASS":
        _fail("validation.json not PASS — run validate first")
        sys.exit(1)

    pack = {
        "task_id":           args.task_id,
        "availability_required": True,
        "availability_state": str(root / "executor-availability.json"),
        "fallback_policy": "manual",
        "authorization_record": None,
        "executor_uuid": gate.get("executor_surface_uuid"),
        # Scaffold output is a DRAFT and says so in machine-readable form.
        #
        # Previously the scaffold emitted `<FILL: ...>` placeholders and nothing
        # else, so a half-written pack was structurally indistinguishable from a
        # dispatchable one: the schema only required strings, and a placeholder is
        # a string. This task's own first pack pointed at an already-merged PR,
        # which the executor had to notice by hand. `draft: true` plus an explicit
        # finalize step makes "not ready" a fact a guard can read. (R3 item 1.)
        "draft":             True,
        "role":              "executor",
        "supervisor":        gate.get("supervisor", ""),
        # Singular field for old readers: the first executor.
        "executor":          gate.get("executor", ""),
        # Full set, so a pack can never silently address one of three executors.
        "executors":         [
            {"surface_ref": e["surface_ref"], "provider": e.get("provider", ""),
             "ordinal": e["ordinal"], "display": f"EXECUTOR_{e['ordinal']}"}
            for e in _gate_executors(gate)
        ],
        "role_map":          str(root / "role-map.json"),
        "context":           "<FILL: user objective and relevant context>",
        "source":            "<FILL: source files or URLs>",
        "scope":             "<FILL: what executor must do>",
        "forbidden":         "<FILL: what executor must NOT do>",
        "local_first":       "Run all local tests / dry-runs before any production action.",
        "executor_model":    os.environ.get("MULTI_AGENT_EXECUTOR_MODEL") or "preserve-current-model",
        "model_command":     None,
        "required_skill":    str(COLLABORATION_SKILL_PATH),
        "needs_auth":        "<FILL: explicit user auth required for VPS/production>",
        "verify":            "<FILL: how executor verifies success locally>",
        # Where the authority for a production action came from. A peer agent
        # asserting "the user approved this" is not user approval; only a first-
        # hand user message is. Default to `none` so silence never reads as
        # consent. (R3 item 7.)
        "authorization_source": "none",
        "report":            str(root / "executor-report.md"),
        "callback":          f"PREFLIGHT_ACK|{args.task_id}|<provider>:identity|READY|INLINE|<nonce>",
        # A task completion callback is a separate contract from the cold
        # handshake ACK.  Keeping both fields prevents a report from being
        # mistaken for a terminal callback, and gives sentinels a task-bound
        # token to wait for after implementation work finishes.
        "completion_nonce":  "<completion-nonce>",
        "completion_callback": (
            f"DONE|{args.task_id}|<completion-nonce>|"
            f"REPORT={root / 'executor-report.md'}"
        ),
        "callback_target":   gate.get("supervisor", ""),
        "completion_delivery": {
            "transport": "cmux_bridge.submit_completion_callback",
            "require_confirmed": True,
        },
        "completion_receipt": str(root / "completion-callback-receipt.json"),
        "bridge_test_evidence": str(root / "bridge-test-evidence.json"),
        "identity_gate":     str(root / "identity-gate.json"),
        "pane_inventory":    str(root / "surface-inventory.json"),
        "naming_proof":      str(root / "naming-proof.json"),
        "validation_evidence": str(root / "validation.json"),
    }
    _write(root / "task-pack.json", pack)
    _ok(
        "task-pack DRAFT written (draft=true) — fill context/scope/source/verify, "
        "then run `finalize-pack` to clear the draft flag"
    )


# Placeholder shapes the scaffold emits. Finalization must reject every one of
# them: a pack that still says "<FILL: what executor must do>" has no scope.
_PLACEHOLDER_MARKERS = ("<FILL", "<TODO", "TBD", "<fill")


def _extract_source_paths(source) -> list[str]:
    """Pull candidate filesystem paths out of either source shape.

    `source` is deliberately string-or-array: every historical pack stores a
    string, and making it array-only would invalidate all of them for no gain.
    The real defect was never the type — it was that nothing checked whether the
    cited paths exist. This task's own pack contained a trailing garbage entry
    that resolved to a directory named `。` and no one noticed. (R3 item 1.)
    """
    if isinstance(source, list):
        candidates = [str(item) for item in source]
    else:
        text = str(source or "")
        # Absolute paths, separated by whitespace, commas, or CJK punctuation.
        candidates = re.findall(r"/[^\s,;，；、]+", text)
    cleaned = []
    for candidate in candidates:
        # Strip trailing punctuation that reads as prose, not as a path.
        candidate = candidate.rstrip("。，、；;,.)）]】")
        if candidate.startswith("/"):
            cleaned.append(candidate)
    return cleaned


def cmd_finalize_pack(args):
    """Promote a draft task pack to dispatchable, or refuse with a reason.

    This is the gate that did not exist. Every check below corresponds to a
    defect actually observed in this task or the preceding one, not to a
    hypothetical: unfilled placeholders, empty scope/verify, a source path that
    does not exist on disk, a missing callback token, and an authorization claim
    that is not first-hand.
    """
    root = _artifact_root(args)
    pack_path = root / "task-pack.json"
    pack = _read(pack_path)
    if pack is None:
        _fail(f"PACK_MISSING — {pack_path} does not exist. Run task-pack first.")
        sys.exit(1)
    if pack.get("task_id") != args.task_id:
        _fail(
            f"PACK_TASK_MISMATCH — pack is bound to {pack.get('task_id')!r}, "
            f"not {args.task_id!r}."
        )
        sys.exit(1)

    problems: list[str] = []

    # 1. No placeholder may survive into a dispatchable pack.
    for field, value in pack.items():
        text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        for marker in _PLACEHOLDER_MARKERS:
            if marker in text:
                problems.append(f"{field} still contains a placeholder ({marker})")
                break

    # 2. The fields that define the work cannot be empty.
    for field in (
        "context", "scope", "forbidden", "verify", "report", "callback",
        "completion_nonce", "completion_callback", "callback_target",
        "completion_receipt",
        "required_skill",
    ):
        value = pack.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{field} is empty or not a string")

    # A completion callback is deliberately not interchangeable with the
    # handshake callback.  Catch the common copy/paste regression at pack
    # finalization, before an executor can be told that readiness equals done.
    completion = pack.get("completion_callback")
    completion_nonce = pack.get("completion_nonce")
    if isinstance(completion, str) and completion.strip():
        if completion == pack.get("callback") or "PREFLIGHT_ACK" in completion:
            problems.append(
                "completion_callback must be separate from PREFLIGHT_ACK"
            )
        if not completion.startswith(("DONE|", "BLOCKED|")):
            problems.append(
                "completion_callback must start with DONE| or BLOCKED|"
            )
        if "REPORT=" not in completion:
            problems.append("completion_callback must include REPORT=<absolute-path>")
        task_id = str(pack.get("task_id") or "")
        report_path = str(pack.get("report") or "")
        expected_done = f"DONE|{task_id}|{completion_nonce}|REPORT={report_path}"
        expected_blocked = f"BLOCKED|{task_id}|{completion_nonce}|REPORT={report_path}"
        if completion not in {expected_done, expected_blocked}:
            problems.append(
                "completion_callback must exactly bind task_id, completion_nonce, "
                "and the absolute report path"
            )

    if not isinstance(completion_nonce, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,}", completion_nonce or ""
    ):
        problems.append("completion_nonce must be a concrete, non-placeholder nonce")

    callback_target = pack.get("callback_target")
    expected_target = (_read(root / "identity-gate.json") or {}).get("supervisor")
    if not isinstance(callback_target, str) or not re.fullmatch(
        r"surface:[0-9]+", callback_target or ""
    ):
        problems.append("callback_target must be an explicit surface:<N>")
    elif expected_target and callback_target != expected_target:
        problems.append(
            f"callback_target {callback_target!r} does not match bound supervisor "
            f"{expected_target!r}"
        )

    delivery = pack.get("completion_delivery")
    if delivery != {
        "transport": "cmux_bridge.submit_completion_callback",
        "require_confirmed": True,
    }:
        problems.append(
            "completion_delivery must require confirmed completion callback delivery"
        )

    completion_receipt = pack.get("completion_receipt")
    if not isinstance(completion_receipt, str) or not Path(completion_receipt).is_absolute():
        problems.append("completion_receipt must be an absolute path")
    elif Path(completion_receipt).parent.resolve() != root.resolve():
        problems.append("completion_receipt must live directly under artifact_root")

    skill_path = pack.get("required_skill")
    if isinstance(skill_path, str) and skill_path.strip():
        skill = Path(skill_path)
        if not skill.is_absolute() or not skill.is_file():
            problems.append("required_skill must be an existing absolute skill path")
        elif skill.resolve() != COLLABORATION_SKILL_PATH.resolve():
            problems.append(
                "required_skill must name the canonical multi-agent-collaboration skill"
            )

    # 3. Every cited source path must exist. A pack that points at a file nobody
    #    wrote sends the executor to read nothing.
    source_paths = _extract_source_paths(pack.get("source"))
    if not source_paths:
        problems.append("source names no absolute path")
    source_entries = []
    for path_str in source_paths:
        exists = Path(path_str).exists()
        source_entries.append({"path": path_str, "exists": exists})
        if not exists:
            problems.append(f"source path does not exist: {path_str}")

    # 4. Absolute report path, so evidence does not depend on the caller's cwd.
    report = pack.get("report")
    if isinstance(report, str) and report and not Path(report).is_absolute():
        problems.append(f"report path is not absolute: {report!r}")

    # 5. Authorization provenance must be explicit and first-hand for production.
    auth = pack.get("authorization_source", "none")
    if auth not in {"user_message", "peer_assertion", "none"}:
        problems.append(f"authorization_source has an unknown value: {auth!r}")
    needs_auth = str(pack.get("needs_auth") or "")
    mentions_production = any(
        word in needs_auth.lower()
        for word in ("production", "vps", "deploy", "ghcr", "生产")
    )
    if mentions_production and auth != "user_message":
        problems.append(
            "needs_auth describes a production action but authorization_source is "
            f"{auth!r}; only a first-hand user message authorizes production"
        )

    if problems:
        _fail(
            "PACK_NOT_DISPATCHABLE — finalization refused:\n    "
            + "\n    ".join(problems)
        )
        sys.exit(1)

    if pack.get("availability_required"):
        from availability_contract import initial, load as load_availability, save as save_availability
        from resource_broker import os_lock
        gate = _read(root / "identity-gate.json") or {}
        state_file = Path(pack["availability_state"])
        with os_lock(str(state_file) + ".lock"):
            prior = load_availability(state_file)
            if not prior:
                availability = initial(
                    args.task_id, gate["workspace_uuid"], gate["executor_surface_uuid"], gate["executor"],
                    authorization_source=pack.get("authorization_source", "none"),
                    authorization_record=pack.get("authorization_record"),
                    fallback_policy=pack.get("fallback_policy", "manual"),
                    protected_paths=pack.get("protected_paths", []))
                save_availability(state_file, availability)
    pack["draft"] = False
    pack["source_entries"] = source_entries
    pack["finalized_at"] = _now()
    _write(pack_path, pack)
    _ok(
        f"task-pack finalized — draft=false, {len(source_entries)} source paths "
        f"verified present, authorization_source={auth}"
    )


# ---------------------------------------------------------------------------
# map
# ---------------------------------------------------------------------------

def cmd_map(args):
    root = _artifact_root(args)
    gate = _read(root / "identity-gate.json")
    if not gate:
        _fail("identity-gate.json not found")
        sys.exit(1)
    executors = _gate_executors(gate)
    role_map = {
        "task_id": args.task_id,
        "supervisor": {"surface_ref": gate.get("supervisor"), "provider": gate.get("supervisor_provider"), "display": "SUPERVISOR"},
        "workspace_ref": gate.get("workspace_ref"),
        "updated_at": _now(),
    }
    # Machine roles are executor, executor2, executor3 …; display names are
    # EXECUTOR_1, EXECUTOR_2, … The label is derived from ordinal rather than
    # hardcoded, so a three-executor gate cannot render as one EXECUTOR_1. The
    # `executor` key stays first for readers that only know that one role.
    for item in executors:
        ordinal = item["ordinal"]
        role = "executor" if ordinal == 1 else f"executor{ordinal}"
        role_map[role] = {
            "surface_ref": item["surface_ref"],
            "provider": item.get("provider", ""),
            "display": f"EXECUTOR_{ordinal}",
        }
    _write(root / "role-map.json", role_map)
    if args.json:
        print(json.dumps(role_map, indent=2))
    else:
        print(f"  supervisor: {gate.get('supervisor')} ({gate.get('supervisor_provider')})")
        for item in executors:
            print(f"  EXECUTOR_{item['ordinal']}: {item['surface_ref']}   ({item.get('provider','')})")


# ---------------------------------------------------------------------------
# receipt
# ---------------------------------------------------------------------------

def cmd_receipt(args):
    root = _artifact_root(args)
    receipt = _read(root / "handshake-receipt.json")
    if not receipt:
        _fail("handshake-receipt.json not found")
        sys.exit(1)
    if args.json:
        print(json.dumps(receipt, indent=2))
    else:
        print(f"  task_id:      {receipt.get('task_id')}")
        print(f"  status:       {receipt.get('status')}")
        print(f"  executor_ack: {receipt.get('executor_ack')}")
        print(f"  ack_line:     {receipt.get('ack_line')}")
        print(f"  ack_source:   {receipt.get('ack_source')}")
        print(f"  ack_format:   {receipt.get('ack_format')}")
        print(f"  ack_nonce:    {receipt.get('ack_nonce')}")
        if not receipt.get("executor_ack"):
            _fail("No ACK — do not claim executor participated")


# ---------------------------------------------------------------------------
# guard-check
# ---------------------------------------------------------------------------

HOOK_CONFIGS = {
    "codex": Path.home() / ".codex" / "hooks.json",
    "claude": Path.home() / ".claude" / "settings.json",
}

# Guards that must be active on BOTH sides, with the event each one needs.
# A PreToolUse guard cannot see a turn ending, and a Stop guard cannot see a
# command being run, so the event is part of the requirement, not a detail.
from manage_install import GUARDS as REQUIRED_GUARD_WIRING


def _wired_guards(config_path: Path) -> dict[str, set[str]]:
    """Map guard name -> set of events it is actually wired to.

    Reads the hook config the agent really loads. A guard file sitting in the
    skill directory is inert until some config invokes it, so presence on disk
    is not evidence of enforcement.
    """
    found: dict[str, set[str]] = {}
    if not config_path.exists():
        return found
    try:
        doc = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return found
    hooks = doc.get("hooks") or {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for hook in (entry or {}).get("hooks") or []:
                command = str((hook or {}).get("command") or "")
                for guard in REQUIRED_GUARD_WIRING:
                    if guard in command:
                        found.setdefault(guard, set()).add(event)
    return found


# ---------------------------------------------------------------------------
# External helper parity
# ---------------------------------------------------------------------------
#
# `SKILL.md` states that `cmux-agent ask` and `cmux_bridge.submit_text` implement
# the same delivery-proof contract in both directions. They are two separate
# implementations of one detector, in two languages, and only one of them is
# inside this skill. Fixing the Python copy therefore does not fix the callback
# path an executor actually uses -- measured: `~/.local/bin/cmux-agent`
# is a bash script whose `prompt_block_pending` still matches only Claude's
# ``❯`` and still treats an earlier transcript block as live compose.
#
# This gate cannot repair that file (it is outside the task pack), so it does the
# next best thing: it runs the helper's own function, byte-for-byte, against the
# same fixtures as the in-scope module and reports divergence as a finding. A
# second implementation nobody compares is how one copy silently rots.

HELPER_FUNCTION_NAME = "prompt_block_pending"
HELPER_STATE_FUNCTION_NAME = "delivery_state"

HELPER_CANDIDATE_PATHS = (
    Path.home() / ".local" / "bin" / "cmux-agent",
    Path("/opt/homebrew/bin/cmux-agent"),
    Path("/usr/local/bin/cmux-agent"),
)

# Fixtures chosen to discriminate, not to decorate: each one is a screen shape
# that was actually observed on surface:147 during this task's callbacks.
_CLAUDE_GLYPH = "❯"
_CODEX_GLYPH = "›"

HELPER_PARITY_FIXTURES = (
    (
        "codex_stuck_in_live_compose",
        f"{_CODEX_GLYPH} [CMUX-AGENT][delivery:MARK] TASK: x\n"
        "  still sitting in the compose box\n",
    ),
    (
        "codex_delivered_transcript_then_empty_box",
        f"{_CODEX_GLYPH} [CMUX-AGENT][delivery:MARK] TASK: x\n"
        "  TASK: do the thing\n"
        "\n"
        f"{_CODEX_GLYPH} Ask Codex to do anything\n",
    ),
    (
        "claude_stuck_in_live_compose",
        f"{_CLAUDE_GLYPH} [CMUX-AGENT][delivery:MARK] TASK: x\n"
        "  still sitting in the compose box\n",
    ),
    (
        "claude_delivered_transcript_then_empty_box",
        f"{_CLAUDE_GLYPH} [CMUX-AGENT][delivery:MARK] TASK: x\n"
        "  TASK: do the thing\n"
        "\n"
        f"{_CLAUDE_GLYPH} \n",
    ),
    (
        "claude_delivered_with_activity_line",
        f"{_CLAUDE_GLYPH} [CMUX-AGENT][delivery:MARK] TASK: x\n"
        "⏺ working on it\n"
        f"{_CLAUDE_GLYPH} \n",
    ),
    (
        "glyph_inside_prose_is_not_a_boundary",
        "⏺ I will paste MARK into the box\n"
        f"  the prompt looks like {_CLAUDE_GLYPH} when idle\n",
    ),
)

HELPER_PARITY_MARKER = "MARK"


def find_external_helper() -> Path | None:
    """Locate the bash callback helper without importing or executing it."""
    which = shutil.which("cmux-agent")
    if which:
        return Path(which)
    for candidate in HELPER_CANDIDATE_PATHS:
        if candidate.is_file():
            return candidate
    return None


def extract_bash_function(text: str, name: str) -> str | None:
    """Slice one shell function out of a script, so nothing else can run.

    Sourcing the helper would execute its trailing `case` dispatch. Extracting
    just the function body keeps this check read-only while still testing the
    shipped bytes rather than a paraphrase of them.
    """
    lines = text.splitlines()
    start = None
    opener = re.compile(rf"^{re.escape(name)}\(\)\s*\{{\s*$")
    for index, line in enumerate(lines):
        if opener.match(line):
            start = index
            break
    if start is None:
        return None
    for end in range(start + 1, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[start : end + 1])
    return None


def helper_prompt_block_pending(function_src: str, screen: str, marker: str):
    """Return the helper's own verdict, or None if it could not be evaluated."""
    driver = (
        function_src
        + "\n"
        + f'if {HELPER_FUNCTION_NAME} "$(cat "$1")" "$2"; then echo TRUE; else echo FALSE; fi\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        script = tmp_path / "driver.sh"
        screen_file = tmp_path / "screen.txt"
        script.write_text(driver, encoding="utf-8")
        screen_file.write_text(screen, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["bash", str(script), str(screen_file), marker],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
    out = (proc.stdout or "").strip()
    if out == "TRUE":
        return True
    if out == "FALSE":
        return False
    return None


def helper_delivery_state(function_src: str, screen: str, marker: str):
    """Evaluate only the extracted state detector, never the helper's CLI.

    A state detector is not a boolean detector: UNCONFIRMED must remain unknown.
    """
    driver = function_src + '\n' + (
        f'{HELPER_STATE_FUNCTION_NAME} "$(cat "$1")" "$2"\n'
    )
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "driver.sh"
        screen_file = Path(tmp) / "screen.txt"
        script.write_text(driver, encoding="utf-8")
        screen_file.write_text(screen, encoding="utf-8")
        try:
            proc = subprocess.run(
                ["bash", str(script), str(screen_file), marker],
                capture_output=True, text=True, timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return None
    state = (proc.stdout or "").strip()
    if proc.returncode != 0 or state not in {
        "COMPOSE_PENDING", "QUEUED", "SUBMITTED", "UNCONFIRMED"
    }:
        return None
    return state


def check_helper_parity() -> dict:
    """Compare the external helper's detector against the in-scope one.

    Never edits, sources, or invokes the helper's CLI. Returns a structured
    result so the caller decides whether divergence is fatal.
    """
    result = {
        "helper_path": None,
        "helper_sha256": None,
        "function_found": False,
        "function_name": None,
        "state_observations": [],
        "evaluated": 0,
        "divergences": [],
        "status": "HELPER_ABSENT",
        "route_verified": False,
    }
    helper = find_external_helper()
    if helper is None:
        return result

    result["helper_path"] = str(helper)
    result["helper_sha256"] = _sha256_file(helper)
    try:
        text = helper.read_text(encoding="utf-8", errors="replace")
    except OSError:
        result["status"] = "HELPER_UNREADABLE"
        return result

    # 新 helper 使用同版 bridge adapter；核验完整渲染结果而非单个 exec 字符串。
    if "cmux_agent_adapter.py" in text or "ask|send|broadcast|reconcile)" in text:
        try:
            result.update(render_cmux_agent.verify_route(
                helper.read_bytes(), Path(__file__).resolve().parents[1]))
            result["status"] = "PARITY_OK"
            result["function_name"] = "fixed_bridge_adapter"
        except (OSError, ValueError, UnicodeError) as exc:
            result["status"] = "HELPER_ROUTE_INVALID"
            result["route_error"] = str(exc)
        return result

    function_src = extract_bash_function(text, HELPER_FUNCTION_NAME)
    function_name = HELPER_FUNCTION_NAME
    if function_src is None:
        function_name = HELPER_STATE_FUNCTION_NAME
        function_src = extract_bash_function(text, function_name)
    if function_src is None:
        result["status"] = "HELPER_FUNCTION_NOT_FOUND"
        return result
    result["function_found"] = True
    result["function_name"] = function_name

    for name, screen in HELPER_PARITY_FIXTURES:
        if function_name == HELPER_STATE_FUNCTION_NAME:
            state = helper_delivery_state(function_src, screen, HELPER_PARITY_MARKER)
            result["state_observations"].append({"fixture": name, "state": state})
            theirs = {"COMPOSE_PENDING": True, "QUEUED": False,
                      "SUBMITTED": False}.get(state)
        else:
            theirs = helper_prompt_block_pending(
                function_src, screen, HELPER_PARITY_MARKER
            )
        if theirs is None:
            result["divergences"].append(
                {"fixture": name, "helper": "UNEVALUATED", "in_scope": None}
            )
            continue
        ours = cmux.compose_contains(screen, HELPER_PARITY_MARKER)
        result["evaluated"] += 1
        if theirs != ours:
            result["divergences"].append(
                {"fixture": name, "helper": theirs, "in_scope": ours}
            )

    result["status"] = "DIVERGENT" if result["divergences"] else "PARITY_OK"
    return result


def cmd_helper_parity(args):
    """Standalone gate: does the real callback helper match the shipped contract?"""
    result = check_helper_parity()
    root = _artifact_root(args)
    _write(root / "helper-parity.json", {**result, "checked_at": _now()})

    print(f"  helper        : {result['helper_path']}")
    print(f"  sha256        : {result['helper_sha256']}")
    print(f"  fixtures ok   : {result['evaluated']}/{len(HELPER_PARITY_FIXTURES)}")

    if result["status"] == "HELPER_ABSENT":
        _ok("helper-parity SKIPPED — no external cmux-agent helper on this machine")
        return
    if result["status"] in {"HELPER_UNREADABLE", "HELPER_FUNCTION_NOT_FOUND", "HELPER_ROUTE_INVALID"}:
        _fail(f"HELPER_PARITY_INDETERMINATE — {result['status']}")
        sys.exit(1)
    if result["divergences"]:
        callback_transport = getattr(args, "callback_transport", "auto")
        if callback_transport == "bridge":
            _ok(
                "helper-parity DIVERGENT but callback transport is pinned to "
                "cmux_bridge; external helper is prohibited for this task"
            )
            return
        lines = [
            f"{d['fixture']}: helper={d['helper']} in_scope={d['in_scope']}"
            for d in result["divergences"]
        ]
        _fail(
            "HELPER_PARITY_DIVERGENT — the external callback helper does not "
            "implement the shipped delivery contract:\n    " + "\n    ".join(lines)
            + "\n  Consequence: helper=False where in_scope=True means a stuck "
            "paste is reported as 'delivered, unverified' and never retried; "
            "helper=True where in_scope=False means an already-delivered message "
            "can receive a second enter and arrive twice."
            + "\n  Remediation: install the helper rendered for the same fixed "
            "release, or explicitly pin this task to cmux_bridge. Preserve any "
            "original uncertain attempt; only native reception confirms delivery."
        )
        sys.exit(1)
    if not result["route_verified"]:
        if getattr(args, "callback_transport", "auto") == "bridge":
            _ok("legacy screen parity is diagnostic only; this task uses cmux_bridge")
            return
        _fail("HELPER_NATIVE_ROUTE_REQUIRED — screen parity does not prove native delivery; "
              "install the fixed adapter helper or explicitly use cmux_bridge")
        sys.exit(1)
    _ok("helper-parity PASS — complete helper bytes delegate to the same fixed bridge adapter")


def cmd_guard_check(args):
    root = _artifact_root(args)
    val = _read(root / "validation.json")
    validation_ok = bool(
        val and val.get("status") == "PASS" and val.get("task_id") == args.task_id
    )
    if validation_ok:
        _ok(f"guard-check PASS — validation.json exists and PASS for {args.task_id}")
    else:
        _fail(f"guard-check FAIL — no valid validation.json for task {args.task_id}")

    # Wiring symmetry is a separate failure from missing validation.
    #
    # Measured on this task before the fix: 4 guards existed, Codex had 3 wired,
    # Claude had 1, and cmux_consensus_stop_guard was inert on both sides. So the
    # guard whose whole purpose is rejecting prompt-echo handshake evidence had
    # never been able to fire in the executor's session. Both roles run the same
    # protocol, so a guard active on only one side leaves the other free to make
    # exactly the claim the guard exists to stop. (R3 item 6.)
    wiring = {side: _wired_guards(path) for side, path in HOOK_CONFIGS.items()}
    print("  guard wiring (guard -> event per side):")
    problems: list[str] = []
    for guard, required_event in sorted(REQUIRED_GUARD_WIRING.items()):
        cells = []
        for side in HOOK_CONFIGS:
            events = wiring[side].get(guard, set())
            if required_event in events:
                cells.append(f"{side}={required_event}")
            elif events:
                cells.append(f"{side}=WRONG_EVENT({','.join(sorted(events))})")
                problems.append(
                    f"{guard} on {side} is wired to {sorted(events)}, needs {required_event}"
                )
            else:
                cells.append(f"{side}=NOT_WIRED")
                problems.append(f"{guard} is not wired on {side} ({required_event})")
        print(f"    {guard:34s} {'  '.join(cells)}")

    if problems:
        _fail(
            "GUARD_WIRING_ASYMMETRIC — a guard that is not wired is not a guard:\n    "
            + "\n    ".join(problems)
        )
    else:
        _ok(f"guard wiring symmetric — {len(REQUIRED_GUARD_WIRING)} guards active on both sides")

    if not validation_ok or problems:
        sys.exit(1)


# ---------------------------------------------------------------------------
# record-round / consensus-check
# ---------------------------------------------------------------------------

def _rounds_path(root: Path) -> Path:
    return root / "rounds.json"


def _effective_timeout(args, phase: str) -> int:
    """Return a phase-specific budget while preserving --timeout compatibility."""
    override = getattr(args, "timeout", None)
    if override is not None:
        return max(1, int(override))
    default = 600 if phase == "handshake" else 180
    value = getattr(args, "handshake_timeout" if phase == "handshake" else "round_timeout", default)
    return max(1, int(value))


def _budget_source(args, phase: str) -> str:
    """Where the effective budget came from, for blame attribution.

    Shared by the handshake and round paths so the two cannot disagree about
    provenance. A generic --timeout wins unconditionally in _effective_timeout,
    which is exactly how a cold handshake was once silently downgraded to the
    round budget and then blamed on executor silence.
    """
    if getattr(args, "timeout", None) is not None:
        return "generic_timeout_override"
    return f"phase_{phase}_default"


def _load_rounds(root: Path) -> dict:
    path = _rounds_path(root)
    if not path.exists():
        return {
            "task_id": "",
            "status": "IN_PROGRESS",
            "minimum_required_rounds": 3,
            "rounds": [],
        }
    return json.loads(path.read_text())


def _strict_validation_ok(root: Path, task_id: str) -> tuple[bool, str]:
    val = _read(root / "validation.json") or {}
    if val.get("task_id") != task_id:
        return False, "validation.json task_id mismatch or missing"
    if val.get("status") != "PASS":
        return False, "validation.json is not PASS"
    strict = val.get("checks", {}).get("handshake_strict", {})
    if strict.get("pass") is not True:
        return False, "handshake_strict.pass is not true"
    return True, "strict validation PASS"


def _record_round_one(args, root, entry, item, artifact_path, artifact_sha256):
    """Request and prove ONE executor's round ACK.

    Returns the per-executor evidence dict. Exits non-zero on any failure, so
    rounds.json is never appended for a round whose proof is incomplete.
    """
    executor_ref = item["surface_ref"]
    executor_provider = item.get("provider") or "agent"
    round_nonce = secrets.token_hex(4)
    safe_round_id = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in args.round_id
    )
    round_receipt_path = root / "round-receipts" / f"{safe_round_id}-{round_nonce}.json"
    round_receipt = {
        "task_id": args.task_id,
        "round_id": args.round_id,
        "speaker": args.speaker,
        # `verdict` is retained for compatibility with existing readers, but
        # it is the SUPERVISOR'S REQUEST, not a reviewer's judgment. The
        # split below is the load-bearing part: a nonce proves the executor
        # possessed the channel, never that it formed an opinion. Previously
        # the expected ACK line was built from args.verdict, so only an echo
        # could match and a pre-selected outcome was indistinguishable from a
        # real review.
        "verdict": args.verdict,
        "requested_review": {
            "artifact": str(artifact_path) if artifact_path else args.artifact,
            "artifact_sha256": artifact_sha256,
            "requested_verdict": args.verdict,
        },
        "allowed_verdicts": list(ROUND_ALLOWED_VERDICTS),
        # MUST stay null until the executor answers. A non-null value here is
        # SUPERVISOR_PRESELECTED_VERDICT and voids the round.
        "executor_verdict": None,
        "executor": executor_ref,
        "executor_provider": executor_provider,
        "round_nonce": round_nonce,
        "status": "AWAITING_EXECUTOR_ACK",
        "executor_ack": False,
        # Budget provenance. Without these a round timeout cannot be
        # attributed: five round receipts in the preceding task carried None
        # for all of them, and three of those five had never been submitted
        # at all, yet every one recorded an executor-shaped error string. A
        # reader could not tell a 1.2s sender-side abort from a genuine
        # 184s executor timeout.
        #
        # These are an axis ORTHOGONAL to submission_state, not an
        # alternative to it: submission_state says where the message stopped,
        # budget says whose deadline was too short. Sharing one field is what
        # made the misattribution possible.
        "budget_source": _budget_source(args, "round"),
        "budget_seconds": _effective_timeout(args, "round"),
        "phase_minimum_seconds": PHASE_MINIMUM_BUDGET_SECONDS["round"],
        "budget_below_phase_minimum": (
            _effective_timeout(args, "round")
            < PHASE_MINIMUM_BUDGET_SECONDS["round"]
        ),
        "attributable_to_executor": None,   # decided at terminal state
        "submission_state": None,
        "detector_side": None,
        "detector_failure_state": None,
        "late_ack_recovery_attempted": False,
        "late_ack_recovered": False,
        "late_ack_grace_seconds": 0,
        "created_at": _now(),
        "dispatch_started_at": None,
        "dispatch_submitted_at": None,
        "evidence_observed_at": None,
        "parser_confirmed_at": None,
        "terminal_error_at": None,
        "updated_at": _now(),
    }
    # Fail closed if a caller ever pre-populates the executor's judgment.
    # This is the excision point that makes the split non-vacuous: remove it
    # and a pre-filled verdict silently becomes "the executor's review".
    if round_receipt.get("executor_verdict") is not None:
        _fail(
            "SUPERVISOR_PRESELECTED_VERDICT — executor_verdict must be null in a "
            "pending receipt. The supervisor may request a verdict; it may not "
            "supply the executor's. Round NOT recorded."
        )
        sys.exit(1)
    if args.verdict not in ROUND_ALLOWED_VERDICTS:
        _fail(
            f"ROUND_VERDICT_NOT_ALLOWED — {args.verdict!r} is not one of "
            f"{list(ROUND_ALLOWED_VERDICTS)}."
        )
        sys.exit(1)

    # Write before prompting so the executor can independently authenticate
    # the per-round nonce. rounds.json remains untouched until proof passes.
    _write(round_receipt_path, round_receipt)
    prompt = (
        f"DELIVERY_NONCE={round_nonce}. "
        f"cmux multi-agent consensus round {args.round_id} for task {args.task_id}. "
        f"Before judging, read and obey the multi-agent-collaboration skill at "
        f"{COLLABORATION_SKILL_PATH}. This is an executor callback round: the "
        "report artifact and a visible DONE sentence are not a callback and "
        "must never replace the nonce-bound ACK. "
        f"Verify the pending receipt at {round_receipt_path}, then construct a response from "
        f"ROUND_TASK_ID={args.task_id} ROUND_ID={args.round_id} "
        f"ROUND_AGENT={executor_provider}:identity ROUND_VERDICT={args.verdict} "
        f"ROUND_NONCE={round_nonce}. After verification, your entire assistant response "
        "must be exactly one compact ACK line with no prose before or after. Reply in compact format "
        "ROUND_ACK|<task-id>|<round-id>|<agent:identity>|<verdict>|<nonce>."
    )
    round_receipt["dispatch_started_at"] = _now()
    round_receipt["updated_at"] = _now()
    _write(round_receipt_path, round_receipt)
    _info(f"Requesting executor round-ack from {executor_ref} (nonce={round_nonce})...")
    try:
        # This newly created review challenge uses the same bounded notice as
        # the handshake. Preserve the original nonce and complete body on disk.
        import cmux_prompt_reference as prompt_reference
        prepared = prompt_reference.plan(prompt, round_nonce)
        prompt_reference.persist(prepared['reference'], prompt)
        round_receipt['dispatch_payload_sha256'] = prompt_reference.digest(prepared['text'])
        round_receipt['dispatch_body_reference'] = prepared['reference']
        round_receipt['dispatch_confirmation_scope'] = 'reference_notice' if prepared['reference'] else 'inline_payload'
        _write(round_receipt_path, round_receipt)
        submit_kwargs = {"marker": round_nonce}
        if getattr(args, "force_compose", False):
            submit_kwargs["force_compose"] = True
        cmux.submit_text(executor_ref, prepared['text'], **submit_kwargs)
        round_receipt["dispatch_submitted_at"] = _now()
        round_receipt["updated_at"] = _now()
        _write(round_receipt_path, round_receipt)
    except cmux.DispatchUnconfirmed as e:
        # The bridge may have pasted and submitted successfully even when its
        # detector cannot prove that fact.  Do not resend: keep the nonce and
        # listen for a late, genuine executor ACK so a real callback cannot be
        # lost merely because Claude hit a transient API/queue boundary.
        round_receipt["error"] = str(e)
        # Preserve the delivery state instead of flattening every cause into
        # one executor-shaped string. Only DELIVERY_QUEUED_AT_RECEIVER means
        # wait; none of the five permits a blind resend.
        state = getattr(e, "state", None)
        if state not in (
            cmux.DELIVERY_UNVERIFIED_BY_DETECTOR,
            cmux.DELIVERY_QUEUED_AT_RECEIVER,
        ):
            # A busy or never-submitted compose is not a late-delivery case.
            # Stop immediately, preserve the transport classification, and
            # require the operator to clear/fix the sender before another try.
            round_receipt["status"] = "FAIL"
            round_receipt["submission_state"] = (
                state or cmux.SUPERVISOR_DID_NOT_SUBMIT
            )
            round_receipt["detector_side"] = "supervisor"
            round_receipt["attributable_to_executor"] = False
            round_receipt["terminal_error_at"] = _now()
            round_receipt["updated_at"] = _now()
            _write(round_receipt_path, round_receipt)
            _fail(
                f"{round_receipt['submission_state']} — the round prompt was not "
                "delivered; refusing to wait or resend behind the current compose."
            )
            sys.exit(1)
        round_receipt["submission_state"] = state
        round_receipt["detector_failure_state"] = state
        round_receipt["detector_side"] = "supervisor"
        # A delivery-state result means the bridge did execute the paste/submit
        # transaction; only the detector failed to prove consumption. Preserve
        # that distinction so a later same-nonce ACK cannot be misreported as
        # "the supervisor never submitted".
        if state in (
            cmux.DELIVERY_UNVERIFIED_BY_DETECTOR,
            cmux.DELIVERY_QUEUED_AT_RECEIVER,
        ):
            round_receipt["dispatch_submitted_at"] = _now()
        round_receipt["late_ack_recovery_attempted"] = True
        round_receipt["late_ack_grace_seconds"] = ROUND_LATE_ACK_GRACE_SECONDS
        round_receipt["updated_at"] = _now()
        _write(round_receipt_path, round_receipt)
        _info(
            f"{e} — waiting up to {ROUND_LATE_ACK_GRACE_SECONDS}s for the "
            "same nonce callback; no resend will be attempted."
        )
        late_ack_deadline = time.time() + ROUND_LATE_ACK_GRACE_SECONDS
    else:
        late_ack_deadline = None
    # Poll briefly for the nonce to appear in a genuine response block.
    evidence = None
    round_timeout = _effective_timeout(args, "round")
    deadline = (
        late_ack_deadline if late_ack_deadline is not None
        else time.time() + max(15, round_timeout)
    )
    # Accept ANY allowed verdict, not just the requested one. Binding the
    # parser to args.verdict is what made the ACK an echo: a reviewer who
    # disagreed had no representable answer.
    chosen_verdict = None
    while time.time() < deadline:
        for candidate in ROUND_ALLOWED_VERDICTS:
            expected_round_ack = (
                f"ROUND_ACK|{args.task_id}|{args.round_id}|"
                f"{executor_provider}:identity|{candidate}|{round_nonce}"
            )
            ev = cmux.capture_round_evidence(
                executor_ref,
                round_nonce,
                provider=executor_provider,
                lines=args.lines,
                expected_ack=expected_round_ack,
            )
            # When expected_ack is supplied, executor_nonce_found already
            # means the COMPLETE line matched inside a genuine response
            # block, so it is the authoritative signal. expected_ack_found is
            # the same fact stated explicitly; treat it as advisory so a
            # caller or fixture that predates it is not silently starved into
            # a full-budget timeout.
            explicit = ev.get("expected_ack_found")
            if ev["executor_nonce_found"] and explicit is not False:
                evidence = ev
                chosen_verdict = candidate
                break
        if evidence:
            break
        # Nonce present but no allowed verdict line yet: keep waiting rather
        # than accepting nonce-possession as a review.
        time.sleep(3)
    if not evidence:
        below = round_receipt["budget_below_phase_minimum"]
        round_receipt["status"] = "FAIL"
        # A silent executor and a too-short deadline are different failures.
        # Only the former is the executor's.
        round_receipt["error"] = (
            "SUPERVISOR_BUDGET_TOO_SHORT" if below
            else "NO_EXECUTOR_ROUND_EVIDENCE"
        )
        round_receipt["submission_state"] = cmux.DELIVERY_UNVERIFIED_BY_DETECTOR
        round_receipt["detector_side"] = "supervisor"
        round_receipt["attributable_to_executor"] = not below
        round_receipt["terminal_error_at"] = _now()
        round_receipt["updated_at"] = _now()
        _write(round_receipt_path, round_receipt)
        if below:
            _fail(
                f"SUPERVISOR_BUDGET_TOO_SHORT — polled {round_timeout}s but the "
                f"round phase minimum is {PHASE_MINIMUM_BUDGET_SECONDS['round']}s "
                f"(source={round_receipt['budget_source']}). "
                "attributable_to_executor=false; raise the budget and retry."
            )
        else:
            _fail(
                f"NO_EXECUTOR_ROUND_EVIDENCE — executor {executor_ref} did not produce "
                f"nonce {round_nonce} in a genuine response within {round_timeout}s "
                f"(>= {PHASE_MINIMUM_BUDGET_SECONDS['round']}s minimum). Round NOT recorded."
            )
        sys.exit(1)
    round_receipt["status"] = "PASS"
    round_receipt["executor_ack"] = True
    round_receipt["attributable_to_executor"] = False
    if round_receipt.get("late_ack_recovery_attempted"):
        round_receipt["late_ack_recovered"] = True
    round_receipt["submission_state"] = None
    # The executor's own selection, which may differ from what was requested.
    round_receipt["executor_verdict"] = chosen_verdict
    round_receipt["verdict_matched_request"] = (chosen_verdict == args.verdict)
    if chosen_verdict != args.verdict:
        _info(
            f"Executor selected {chosen_verdict!r}, which differs from the requested "
            f"{args.verdict!r}. Recording the EXECUTOR's verdict — that is the point "
            "of the split."
        )
    # rounds.json records what the reviewer decided, not what was asked for.
    entry["verdict"] = chosen_verdict
    entry["requested_verdict"] = args.verdict
    entry["executor_selected_verdict"] = True
    round_receipt["screen_hash"] = evidence["screen_hash"]
    round_receipt["evidence_observed_at"] = _now()
    round_receipt["parser_confirmed_at"] = round_receipt["evidence_observed_at"]
    round_receipt["updated_at"] = _now()
    _write(round_receipt_path, round_receipt)
    entry["executor_evidence"] = {
        "executor": executor_ref,
        "executor_provider": executor_provider,
        "ordinal": item["ordinal"],
        "round_nonce": round_nonce,
        "screen_hash": evidence["screen_hash"],
        "nonce_proven": True,
        # This reviewer's own verdict, so the caller can aggregate across
        # executors instead of keeping whichever was polled last.
        "executor_verdict": chosen_verdict,
        "receipt": str(round_receipt_path),
        "captured_at": _now(),
    }
    _ok(f"executor round evidence captured (nonce={round_nonce}, hash={evidence['screen_hash']})")
    return entry["executor_evidence"]

def cmd_record_round(args):
    root = _artifact_root(args)
    _ensure_root(root, args.task_id)
    from availability_contract import require_action
    require_action(args.task_id, "round_record", _read(root / "task-pack.json") or {})

    if not args.round_id:
        _fail("--round-id is required")
        sys.exit(1)
    if not args.speaker:
        _fail("--speaker is required")
        sys.exit(1)
    if not args.verdict:
        _fail("--verdict is required")
        sys.exit(1)

    rounds_doc = _load_rounds(root)
    if not rounds_doc.get("task_id"):
        rounds_doc["task_id"] = args.task_id
    elif rounds_doc.get("task_id") != args.task_id:
        _fail("rounds.json task_id mismatch")
        sys.exit(1)

    # A round is only as real as the artifact it cites.
    #
    # Before this check, `record-round` stored `args.artifact` as an unverified
    # string: a round could name a file that never existed, or name a real file
    # whose contents later changed, and `consensus-check` would still count it.
    # That made "3 rounds of review" satisfiable without three reviews.
    #
    # Absolute paths only, because a relative path resolves against whatever cwd
    # the supervisor happened to be in -- the same wrong-cwd class that voided
    # readings elsewhere in this project. Hash recorded at admission time so a
    # later edit to the artifact is detectable rather than silent.
    artifact_path = None
    artifact_sha256 = None
    if args.artifact:
        artifact_path = Path(args.artifact)
        if not artifact_path.is_absolute():
            _fail(
                f"ROUND_ARTIFACT_NOT_ABSOLUTE — {args.artifact!r} is relative. A "
                "relative artifact path resolves against the caller's cwd, so the "
                "recorded evidence would depend on where the command ran."
            )
            sys.exit(1)
        if not artifact_path.is_file():
            _fail(
                f"ROUND_ARTIFACT_MISSING — {artifact_path} does not exist. A round "
                "cannot cite an artifact that was never written."
            )
            sys.exit(1)
        artifact_sha256 = _sha256_file(artifact_path)
        if not artifact_sha256:
            _fail(f"ROUND_ARTIFACT_UNREADABLE — cannot hash {artifact_path}")
            sys.exit(1)

    entry = {
        "round_id": args.round_id,
        "speaker": args.speaker,
        "verdict": args.verdict,
        "artifact": str(artifact_path) if artifact_path else args.artifact,
        "artifact_exists": artifact_path is not None,
        "artifact_sha256": artifact_sha256,
        "summary": args.summary,
        "blocks_consensus": bool(args.blocks_consensus),
        "resolves_rounds": list(args.resolves_round or []),
        "recorded_at": _now(),
    }

    # Anti-forgery: an executor round must be bound to REAL executor screen
    # evidence (a supervisor-issued per-round nonce that the executor echoed back
    # in a genuine response block). This stops a supervisor from fabricating
    # "the executor participated in N rounds" by passing --speaker executor alone.
    speaker = args.speaker.lower()
    is_executor_round = speaker not in {"supervisor", "self", ""} or args.executor_evidence
    if is_executor_round and speaker != "supervisor":
        gate = _read(root / "identity-gate.json")
        if not gate or gate.get("status") != "PASS" or not gate.get("executor"):
            _fail("executor round requires a PASS identity-gate.json with a bound executor")
            sys.exit(1)
        # One round request per executor, each with its own nonce. A shared
        # nonce would let one executor's echo prove the whole panel reviewed
        # the artifact. Any executor failing exits before rounds.json is
        # appended, so a partially-proven round is never recorded.
        executors = _gate_executors(gate)
        per_executor = []
        for item in executors:
            per_executor.append(_record_round_one(args, root, entry, item, artifact_path, artifact_sha256))
        # Singular field keeps the first executor for existing readers,
        # including consensus-check's nonce_proven assertion.
        entry["executor_evidence"] = dict(per_executor[0])
        entry["executor_evidence"]["executors"] = per_executor
        entry["executor_evidence"]["executor_count"] = len(per_executor)
        entry["executor_evidence"]["all_executors_proven"] = all(
            e.get("nonce_proven") is True for e in per_executor
        )
        # Reviewers may disagree. Each helper call writes entry["verdict"], so
        # without this the LAST executor polled would silently become the round's
        # verdict — list order deciding a review outcome. The round takes the
        # least favourable verdict any reviewer gave: ROUND_ALLOWED_VERDICTS is
        # ordered most-to-least favourable, so that is the highest index. One
        # FAIL among three reviewers must not be averaged away.
        verdicts = [e.get("executor_verdict") for e in per_executor]
        entry["executor_verdicts"] = verdicts
        known = [v for v in verdicts if v in ROUND_ALLOWED_VERDICTS]
        if known:
            entry["verdict"] = max(known, key=ROUND_ALLOWED_VERDICTS.index)
        entry["executor_verdicts_unanimous"] = len(set(verdicts)) == 1
        if not entry["executor_verdicts_unanimous"]:
            _info(
                f"Executors disagreed: {verdicts}. Recording the least favourable "
                f"({entry['verdict']!r}) as the round verdict."
            )

    rounds_doc.setdefault("rounds", []).append(entry)
    rounds_doc["status"] = "IN_PROGRESS"
    rounds_doc["updated_at"] = _now()
    _write(_rounds_path(root), rounds_doc)
    _ok(f"recorded consensus round {args.round_id} from {args.speaker}")


# ---------------------------------------------------------------------------
# Pack-enumerated advisory leases
# ---------------------------------------------------------------------------

LEASE_DEFAULT_TTL_SECONDS = 3600
LEASE_CONFLICT = "LEASE_CONFLICT"
LEASE_STALE = "LEASE_STALE"
LEASE_SCOPE_VIOLATION = "LEASE_SCOPE_VIOLATION"


def _leases_dir(root: Path) -> Path:
    return root / "leases"


def _pack_enumerated_paths(root: Path) -> set:
    """Absolute paths the FINALIZED pack declares. A draft pack enumerates nothing.

    Scope is intentionally this narrow. The motivating incident was a concurrent
    write to a file the pack had declared as a source; covering arbitrary globs or
    the whole home directory would buy a stuck global lock instead.
    """
    pack = _read(root / "task-pack.json") or {}
    if pack.get("draft") is not False:
        return set()
    out = set()
    for entry in pack.get("source") or []:
        if isinstance(entry, str) and entry.startswith("/"):
            out.add(os.path.realpath(entry))
    for entry in pack.get("source_entries") or []:
        if isinstance(entry, dict):
            p = entry.get("path")
            if isinstance(p, str) and p.startswith("/"):
                out.add(os.path.realpath(p))
    return out


def _lease_source_sha(paths) -> str:
    joined = "\n".join(sorted(paths))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _live_leases(root: Path, now=None) -> tuple[list, list]:
    """Return (live, stale) leases. Expiry is reported, never auto-reclaimed."""
    now = now or datetime.now(timezone.utc)
    live, stale = [], []
    d = _leases_dir(root)
    if not d.exists():
        return live, stale
    for path in sorted(d.glob("*.json")):
        rec = _read(path)
        if not isinstance(rec, dict) or rec.get("status") == STATUS_UNVERIFIABLE:
            continue
        if rec.get("released_at"):
            continue
        try:
            expires = datetime.fromisoformat(rec["expires_at"])
        except (KeyError, TypeError, ValueError):
            stale.append(rec)
            continue
        (stale if expires <= now else live).append(rec)
    return live, stale


def lease_conflicts(root: Path, owner_role: str, paths, mode: str, now=None):
    """Verdicts blocking acquisition of `paths` for `owner_role`.

    Enforcement lives with the caller (harness command or hook); this function is
    the shared decision so both sides cannot disagree about what a conflict is.
    """
    wanted = {os.path.realpath(p) for p in paths}
    verdicts = []

    allowed = _pack_enumerated_paths(root)
    outside = sorted(wanted - allowed)
    if outside:
        verdicts.append({
            "verdict": LEASE_SCOPE_VIOLATION,
            "paths": outside,
            "message": (
                "path(s) not enumerated by the finalized task pack; leases cover "
                "only pack-declared absolute paths"
            ),
        })

    live, stale = _live_leases(root, now=now)
    for rec in live:
        if rec.get("owner_role") == owner_role:
            continue
        held = {os.path.realpath(p) for p in rec.get("paths") or []}
        overlap = sorted(wanted & held)
        if not overlap:
            continue
        if mode == "exclusive" or rec.get("mode") == "exclusive":
            verdicts.append({
                "verdict": LEASE_CONFLICT,
                "paths": overlap,
                "held_by": rec.get("owner_role"),
                "held_by_surface": rec.get("owner_surface"),
                "expires_at": rec.get("expires_at"),
                "message": "overlapping path held under an exclusive lease",
            })
    for rec in stale:
        held = {os.path.realpath(p) for p in rec.get("paths") or []}
        overlap = sorted(wanted & held)
        if overlap:
            verdicts.append({
                "verdict": LEASE_STALE,
                "paths": overlap,
                "held_by": rec.get("owner_role"),
                "expires_at": rec.get("expires_at"),
                "message": (
                    "an expired lease covers this path; release it explicitly. "
                    "Expired records are never silently reclaimed."
                ),
            })
    return verdicts


def _resolved_round_ids(rounds: list[dict]) -> set:
    return {
        round_id
        for entry in rounds
        for round_id in (entry.get("resolves_rounds") or [])
        if round_id
    }


def _unresolved_blockers(rounds: list[dict]) -> list[dict]:
    resolved = _resolved_round_ids(rounds)
    return [
        entry
        for entry in rounds
        if entry.get("blocks_consensus") is True
        and entry.get("round_id") not in resolved
    ]


def _unaccounted_nonapproving(rounds: list[dict]) -> list[dict]:
    """Non-approving verdicts that neither block nor are explicitly resolved.

    Checking only the LAST round's verdict was insufficient: a later approving
    verdict superseded an earlier non-approving one without ever addressing what
    it raised, and nothing forced the earlier round to set blocks_consensus. That
    left the safety property depending on a human remembering a flag while the
    ordering behaviour was automatic. Now every non-approving verdict must be
    accounted for explicitly, in code.
    """
    resolved = _resolved_round_ids(rounds)
    out = []
    for entry in rounds:
        verdict = (entry.get("verdict") or "").upper()
        if verdict in ROUND_APPROVING_VERDICTS:
            continue
        if entry.get("blocks_consensus") is True:
            continue          # accounted: it blocks, handled by _unresolved_blockers
        if entry.get("round_id") in resolved:
            continue          # accounted: a later round explicitly resolved it
        out.append(entry)
    return out


def cmd_lease_acquire(args):
    root = _artifact_root(args)
    paths = [os.path.realpath(p) for p in (args.path or [])]
    if not paths:
        _fail("--path is required (absolute) at least once")
        sys.exit(1)
    relative = [p for p in (args.path or []) if not p.startswith("/")]
    if relative:
        _fail(f"lease paths must be absolute; got {relative}")
        sys.exit(1)

    mode = "shared" if args.shared else "exclusive"
    verdicts = lease_conflicts(root, args.owner_role, paths, mode)
    if verdicts:
        _write(root / "lease-check.json", {
            "task_id": args.task_id, "status": "FAIL",
            "requested": {"owner_role": args.owner_role, "paths": paths, "mode": mode},
            "verdicts": verdicts, "updated_at": _now(),
        })
        for v in verdicts:
            _fail(f"{v['verdict']} — {v['message']} :: {v['paths']}")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    ttl = max(1, int(args.ttl_seconds))
    rec = {
        "task_id": args.task_id,
        "owner_role": args.owner_role,
        "owner_surface": args.owner_surface,
        "paths": paths,
        "mode": mode,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl)).isoformat(),
        "source_list_sha256": _lease_source_sha(paths),
        "reason": args.reason or "",
        "released_at": None,
    }
    out = _leases_dir(root) / f"{args.owner_role}-{_lease_source_sha(paths)[:12]}.json"
    _write(out, rec)
    _ok(f"lease acquired: {mode} on {len(paths)} path(s), ttl={ttl}s → {out}")


def cmd_lease_release(args):
    root = _artifact_root(args)
    released = 0
    for path in sorted(_leases_dir(root).glob("*.json")):
        rec = _read(path)
        if not isinstance(rec, dict) or rec.get("status") == STATUS_UNVERIFIABLE:
            continue
        if rec.get("owner_role") != args.owner_role or rec.get("released_at"):
            continue
        rec["released_at"] = _now()
        _write(path, rec)
        released += 1
    _ok(f"released {released} lease(s) for {args.owner_role}")


def cmd_lease_check(args):
    root = _artifact_root(args)
    live, stale = _live_leases(root)
    allowed = _pack_enumerated_paths(root)
    result = {
        "task_id": args.task_id,
        "live_leases": live,
        "stale_leases": stale,
        "pack_enumerated_path_count": len(allowed),
        "pack_is_finalized": len(allowed) > 0,
        "updated_at": _now(),
    }
    if args.path:
        result["requested_check"] = lease_conflicts(
            root, args.owner_role, [os.path.realpath(p) for p in args.path],
            "shared" if args.shared else "exclusive")
    _write(root / "lease-check.json", result)
    _info(f"live={len(live)} stale={len(stale)} pack_paths={len(allowed)}")
    if result.get("requested_check"):
        for v in result["requested_check"]:
            _fail(f"{v['verdict']} — {v['message']} :: {v['paths']}")
        sys.exit(1)
    _ok("lease-check complete")


def cmd_consensus_check(args):
    root = _artifact_root(args)
    ok_validation, validation_msg = _strict_validation_ok(root, args.task_id)
    rounds_doc = _load_rounds(root)
    rounds = rounds_doc.get("rounds", [])
    completed = [
        r for r in rounds
        if r.get("round_id") and r.get("speaker") and r.get("verdict")
    ]
    blockers = _unresolved_blockers(completed)

    # The floor is EXTERNAL to the file being audited.
    #
    # Previously: `minimum = int(rounds_doc.get("minimum_required_rounds") or 3)`,
    # which read the bar out of rounds.json — the very artifact under audit. Writing
    # `"minimum_required_rounds": 1` into that file lowered the standard it was
    # checked against, so the gate could authorise itself. Same defect class as a
    # manifest gate whose markers can be emptied.
    #
    # The in-file value may only RAISE the bar (a task that wants five rounds is
    # welcome to say so); it can never lower it below the module constant.
    declared = rounds_doc.get("minimum_required_rounds")
    try:
        declared_int = int(declared) if declared is not None else CONSENSUS_MINIMUM_ROUNDS
    except (TypeError, ValueError):
        declared_int = CONSENSUS_MINIMUM_ROUNDS
    minimum = max(CONSENSUS_MINIMUM_ROUNDS, declared_int)
    floor_source = (
        "external_constant"
        if minimum == CONSENSUS_MINIMUM_ROUNDS
        else "task_raised_above_external_floor"
    )

    final_verdict = completed[-1].get("verdict", "") if completed else ""
    final_ok = final_verdict.upper() in ROUND_APPROVING_VERDICTS

    # Every non-approving verdict must be explicitly accounted for, not merely
    # outvoted by position. See _unaccounted_nonapproving.
    unaccounted = _unaccounted_nonapproving(completed)

    # Anti-forgery: every executor-spoken round must carry proven nonce evidence.
    executor_rounds = [
        r for r in completed
        if r.get("speaker", "").lower() not in {"supervisor", "self", ""}
    ]
    unproven = [
        r for r in executor_rounds
        if not (r.get("executor_evidence", {}) or {}).get("nonce_proven") is True
    ]
    # Require at least one proven executor round so consensus is genuinely 2-party.
    proven_executor_rounds = len(executor_rounds) - len(unproven)

    checks = {
        "strict_validation": {
            "pass": ok_validation,
            "message": validation_msg,
        },
        "minimum_rounds": {
            "pass": len(completed) >= minimum,
            "completed": len(completed),
            "required": minimum,
        },
        "no_consensus_blockers": {
            "pass": len(blockers) == 0,
            "blocker_count": len(blockers),
            "unresolved_round_ids": [r.get("round_id") for r in blockers],
        },
        "final_verdict": {
            "pass": final_ok,
            "verdict": final_verdict,
        },
        "executor_rounds_proven": {
            "pass": len(unproven) == 0 and proven_executor_rounds >= 1,
            "proven_executor_rounds": proven_executor_rounds,
            "unproven_executor_rounds": len(unproven),
        },
        "nonapproving_verdicts_accounted": {
            "pass": len(unaccounted) == 0,
            "unaccounted_count": len(unaccounted),
            "unaccounted_round_ids": [r.get("round_id") for r in unaccounted],
            "rule": (
                "every non-approving verdict must set blocks_consensus=true or be "
                "named in a later round's resolves_rounds; a later approving verdict "
                "does not supersede it by position"
            ),
        },
    }
    overall = all(item.get("pass") is True for item in checks.values())
    result = {
        "task_id": args.task_id,
        "status": "PASS" if overall else "FAIL",
        "checks": checks,
        "rounds_json": str(_rounds_path(root)),
        "updated_at": _now(),
    }
    _write(root / "consensus-validation.json", result)

    if overall:
        rounds_doc["status"] = "PASS"
        rounds_doc["updated_at"] = _now()
        _write(_rounds_path(root), rounds_doc)
        _ok(f"consensus-check PASS — {len(completed)} rounds complete")
    else:
        _fail(f"consensus-check FAIL — {checks}")
        if args.json:
            print(json.dumps(result, indent=2))
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# disarm — clear the armed-task marker (zero-friction once work is done/aborted)
# ---------------------------------------------------------------------------

def cmd_disarm(args):
    # --task-id scopes the disarm to that collaboration. An explicitly named id
    # that matches nothing means "that target is absent", never "pick one for
    # me" — falling back to the unscoped path there deletes a bystander marker
    # whenever exactly one exists. Only the untouched default id may take the
    # zero-friction unscoped path, which still refuses to guess between several
    # concurrent collaborations.
    #
    # Note for anyone cleaning up legacy markers: a workspace holding a stale
    # v1 marker alongside a v2 one is protected by the ">1 marker refuses to
    # guess" rule. Removing that v1 file drops the workspace back to a single
    # marker, so this explicit-id check becomes the only remaining protection.
    # argparse supplies DEFAULT_TASK_ID even when the option was omitted.  Do
    # not use that value as a proxy for user intent: an explicit
    # ``--task-id multi-agent-task`` must remain scoped and must never fall
    # through to deleting a lone bystander marker.
    explicit_task_id = getattr(args, "task_id_explicit", None)
    if explicit_task_id is None:
        # Programmatic callers have no argv to inspect; a non-default value is
        # necessarily scoped, while the default preserves the old zero-friction
        # no-argument behavior.
        explicit_task_id = bool(args.task_id and args.task_id != DEFAULT_TASK_ID)
    try:
        if explicit_task_id:
            removed = disarm_task(args.task_id)
            if not removed:
                _info(f"no marker for task {args.task_id} — nothing disarmed "
                      f"({len(_workspace_marker_paths())} other marker(s) untouched)")
                return
        else:
            removed = disarm_task(None)
    except ValueError as e:
        _fail(str(e))
        sys.exit(1)
    if removed:
        _ok(f"disarmed {removed} task marker(s) for workspace {_workspace_key()}")
        _info(
            "next_action=SUPERVISOR_STATUS_SYNC_IF_STALE — if a settled report's "
            "executor repeats its old closeout status, send one guarded notice "
            "linking the existing callback receipt, supervisor adjudication, "
            "and disarm evidence. A notice is not proof of tool recovery or "
            "deliverable acceptance."
        )
    else:
        _info(f"no active marker for workspace {_workspace_key()} (already disarmed)")


# ---------------------------------------------------------------------------
# Full preflight (setup-check → identity-gate → name-surfaces → handshake → validate)
# ---------------------------------------------------------------------------

def cmd_preflight(args):
    root = _artifact_root(args)
    if any(os.path.lexists(root / name) for name in ("handshake-receipt.json", "task-pack.json")):
        _fail("original handshake already started; preserve its identity gate and use original recovery; no replay")
        sys.exit(1)
    print("=== PREFLIGHT (full acceptance sequence) ===")
    cmd_setup_check(args)
    # Runs first because it is cheap, read-only, and gates the thing every later
    # step depends on. Dispatch readiness includes the callback path: a task pack
    # delivered over a helper that cannot classify its own delivery leaves the
    # supervisor unable to tell "not sent" from "sent and working". Failing here
    # rather than after handshake keeps the diagnosis close to the cause.
    cmd_helper_parity(args)
    cmd_identity_gate(args)
    cmd_name_surfaces(args)
    # Optional `bridge-test` is deliberately not run: /goal and ordinary work
    # may continue while the guarded sender delivers the handshake prompt.
    cmd_handshake(args)
    cmd_validate(args)
    print("\n✓ PREFLIGHT COMPLETE — all gates passed, ready for task dispatch")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _supervisor_provider(value):
    provider = value.lower()
    if provider not in {"auto", "codex", "claude", "opencode"}:
        raise argparse.ArgumentTypeError(
            "--supervisor/--supervisor-provider expects a provider "
            "(auto, codex, claude, opencode), not a surface or UUID; "
            "for a Codex supervisor use --supervisor codex. "
            "The caller surface is resolved from the current authenticated client."
        )
    return provider


def main():
    p = argparse.ArgumentParser(
        description="cmux multi-agent harness (Mac/cmux native)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("command", choices=[
        "doctor", "setup-check", "identity", "surface-inventory",
        "identity-gate", "name-surfaces", "bridge-test", "bridge-clear-observe", "handshake",
        "validate", "task-pack", "finalize-pack", "map", "receipt", "guard-check",
        "preflight", "helper-parity",
        "record-round", "consensus-check", "disarm",
        "lease-acquire", "lease-release", "lease-check",
    ])
    p.add_argument(
        "--callback-transport",
        choices=["auto", "bridge", "helper"],
        default="auto",
        help=(
            "Delivery implementation for task/callback traffic. 'bridge' permits "
            "preflight to continue past a measured external-helper divergence; "
            "the finalized pack must still pin cmux_bridge.submit_text."
        ),
    )
    p.add_argument("--owner-role",    default="executor",
                   choices=["supervisor", "executor", "executor2", "executor3"])
    p.add_argument("--owner-surface", default="surface:0")
    p.add_argument("--path",          action="append", default=[],
                   help="absolute path to lease; repeatable")
    p.add_argument("--shared",        action="store_true",
                   help="acquire a shared lease instead of exclusive")
    p.add_argument("--ttl-seconds",   type=int, default=LEASE_DEFAULT_TTL_SECONDS)
    p.add_argument("--reason",        default="")
    p.add_argument("--task-id",          default=DEFAULT_TASK_ID)
    p.add_argument("--supervisor", "--supervisor-provider", dest="supervisor",
                   type=_supervisor_provider, default="auto",
                   help="provider: auto, codex, claude or opencode; not a surface selector")
    p.add_argument("--executor",         default="codex")
    p.add_argument("--executor-surface", action="append", default=[],
                   help="repeatable; first is EXECUTOR_1, second EXECUTOR_2, …; "
                        "optional per-ref provider as surface:24=claude")
    p.add_argument("--expected-workspace-uuid", default=None,
                   help="user-designated workspace UUID; mismatch rejects without fallback")
    p.add_argument("--expected-executor-uuid", action="append", default=[],
                   help="user-designated executor UUID, one per executor-surface in the same order")
    p.add_argument("--artifact-root",    default="")
    p.add_argument("--spawn",            action="store_true")
    p.add_argument("--spawn-authorized", action="store_true")
    p.add_argument("--direction",        choices=["right", "left", "up", "down"], default="right")
    p.add_argument("--cwd",              default="")
    p.add_argument("--json",             action="store_true")
    # Keep --timeout as an explicit compatibility override. Cold handshakes
    # and already-ready round ACKs need different failure budgets.
    p.add_argument("--timeout",          type=int, default=None,
                   help="override the phase-specific timeout")
    p.add_argument("--handshake-timeout", type=int, default=600,
                   help="handshake poll budget (default: 600s)")
    p.add_argument("--round-timeout",     type=int, default=180,
                   help="per-round ACK poll budget (default: 180s)")
    p.add_argument("--lines",            type=int, default=200)
    p.add_argument("--bridge-clear-recovery", default=None,
                   help="independent zero-input clear observation bound to the original failed bridge test")
    p.add_argument(
        "--force-compose",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "authoritatively replace current idle Claude compose before dispatch "
            "(default: disabled; requires explicit operator authorization)"
        ),
    )
    p.add_argument("--round-id",         default="")
    p.add_argument("--speaker",          default="")
    p.add_argument("--verdict",          default="")
    p.add_argument("--artifact",         default="")
    p.add_argument("--summary",          default="")
    p.add_argument("--blocks-consensus", action="store_true")
    p.add_argument(
        "--resolves-round",
        action="append",
        default=[],
        help="Round id whose blocker is explicitly resolved by this round; repeatable",
    )
    p.add_argument("--executor-evidence", action="store_true",
                   help="Force capture of executor nonce evidence for this round")
    raw_argv = sys.argv[1:]
    args = p.parse_args()
    args.task_id_explicit = any(
        token == "--task-id" or token.startswith("--task-id=")
        for token in raw_argv
    )

    dispatch = {
        "doctor":           cmd_doctor,
        "setup-check":      cmd_setup_check,
        "identity":         cmd_identity,
        "surface-inventory": cmd_surface_inventory,
        "identity-gate":    cmd_identity_gate,
        "name-surfaces":    cmd_name_surfaces,
        "bridge-test":      cmd_bridge_test,
        "bridge-clear-observe": cmd_bridge_clear_observe,
        "handshake":        cmd_handshake,
        "validate":         cmd_validate,
        "task-pack":        cmd_task_pack,
        "finalize-pack":    cmd_finalize_pack,
        "map":              cmd_map,
        "receipt":          cmd_receipt,
        "guard-check":      cmd_guard_check,
        "helper-parity":    cmd_helper_parity,
        "preflight":        cmd_preflight,
        "record-round":     cmd_record_round,
        "consensus-check":   cmd_consensus_check,
        "disarm":           cmd_disarm,
        "lease-acquire":    cmd_lease_acquire,
        "lease-release":    cmd_lease_release,
        "lease-check":      cmd_lease_check,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()

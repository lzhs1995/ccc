"""Original handshake bytes and dispatch provenance; no discovery or transport.

The live journal path must come from the actual sender result. Missing legacy
evidence stays unavailable; neither a nonce nor a directory scan can recreate it.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys

from cmux_evidence_io import snapshot as read_snapshot
from delivery_receipts import snapshot
import executor_reask_protocol as protocol

CONTROLLER_SCHEMA = 'handshake-controller-v1'


def verify_controller(pin, provenance, *, checked=None):
    """Verify the originally pinned complete release, without executing it.

    The manifest has no Python field. Its Python binding is captured separately
    from the running interpreter before NEW input, never inferred from CURRENT.
    """
    if (not isinstance(pin, dict) or pin.get('schema') != CONTROLLER_SCHEMA
            or pin.get('provenance') != provenance or not isinstance(provenance, dict)):
        raise ValueError('original complete controller pin required')
    source = Path(provenance['source'])
    base = Path.home() / '.local/share/multi-agent-collaboration/releases'
    if (not source.is_absolute() or source.resolve(strict=True) != source
            or source.name != 'source' or source.parent.parent != base):
        raise ValueError('controller is outside the canonical immutable layout')
    entrypoint = source / 'scripts/cmux_message_journal.py'
    expected = {'skill': source / 'SKILL.md', 'harness': source / 'scripts/mac_harness.py',
                'bridge': source / 'scripts/cmux_bridge.py'}
    if (provenance.get('entrypoint') != str(entrypoint)
            or any(pin.get(k) != str(p) for k, p in expected.items())):
        raise ValueError('harness, skill and actual sender must belong to one release')
    manifest = pin['manifest']
    python = pin['python']
    python_path = Path(provenance['python'])
    if (manifest.get('path') != str(source.parent / 'MANIFEST.json')
            or python.get('path') != str(python_path)
            or not python_path.is_absolute() or python_path.resolve(strict=True) != python_path
            or snapshot(manifest['path']) != manifest or snapshot(python_path) != python):
        raise ValueError('original manifest or Python pin changed')
    # Cache only inside this bounded hook call. Never carry approval across calls.
    key = (str(source), manifest['sha256'])
    files = checked.get(key) if checked is not None else None
    if files is None:
        files = protocol._verify_release(source, manifest['sha256'])
        if checked is not None:
            checked[key] = files
    names = ['scripts/cmux_message_journal.py', 'SKILL.md',
             'scripts/mac_harness.py', 'scripts/cmux_bridge.py']
    if (not all(name in files for name in names)
            or files[names[0]]['sha256'] != provenance.get('entrypoint_sha256')):
        raise ValueError('complete manifest does not pin the actual handshake sender')
    return source


def record_controller(row, entrypoint, skill, harness, bridge):
    """Capture the actual new dispatch runtime before input; no legacy repair."""
    from cmux_confirmation import controller_provenance
    try:
        provenance = controller_provenance(entrypoint)
        source = Path(provenance['source'])
        pin = dict(schema=CONTROLLER_SCHEMA, provenance=provenance,
                   manifest=snapshot(source.parent / 'MANIFEST.json'),
                   python=snapshot(Path(sys.executable).resolve(strict=True)),
                   skill=str(Path(skill).resolve(strict=True)),
                   harness=str(Path(harness).resolve(strict=True)),
                   bridge=str(Path(bridge).resolve(strict=True)))
        verify_controller(pin, provenance)
        row['dispatch_controller_pin'] = pin
        row['dispatch_controller_status'] = 'PINNED_BEFORE_INPUT'
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        # Writable development checkouts and old receipts get no advisory
        # authority. This observer must not become a new handshake gate.
        row['dispatch_controller_status'] = 'UNAVAILABLE'
        row['dispatch_controller_unavailable_reason'] = str(exc)


def challenge(row, root, skill_path):
    task_id, ack_nonce = row['task_id'], row['ack_nonce']
    supervisor_ref, executor_ref = row['supervisor'], row['executor']
    executor_provider, ordinal = row['executor_provider'], row['ordinal']
    return (
        f"DELIVERY_NONCE={ack_nonce}. "
        f"This is a legitimate cmux multi-agent harness handshake from supervisor {supervisor_ref} "
        f"for task {task_id}. Use the /multi-agent-collaboration skill for this session. "
        f"Read the canonical skill file at {skill_path} (do not substitute an "
        f"archived or ~/.agents copy). Verify the pending receipt at the absolute path "
        f"{Path(root).resolve() / 'handshake-receipt.json'}; do not search the disk. "
        f"Select exactly one executors[] record with executor == {executor_ref} "
        f"and ordinal == {ordinal}. In that record confirm task_id == {task_id}, "
        f"executor_provider == {executor_provider}, and ack_nonce == {ack_nonce}. "
        "Top-level fields describe executor 1 only; never use them for another executor. "
        "If the file is missing, the match is not unique, or a field differs, reply "
        "BLOCKED|RECEIPT_NOT_FOUND; never guess or reconstruct the nonce. Then construct the "
        f"ACK from these fields: ACK_TASK_ID={task_id} ACK_AGENT={executor_provider}:identity "
        f"ACK_STATUS=READY "
        f"ACK_REPORT=INLINE ACK_NONCE={ack_nonce}. After verification, your entire assistant "
        "response must be exactly one compact ACK line with no prose before or after. "
        f"The agent field is the literal string {executor_provider}:identity; do not replace it "
        "with a model name such as claude:opus-5. Do not inspect the task, discuss the bridge, "
        "or send any other message before this exact ACK. Reply in compact format "
        "PREFLIGHT_ACK|<task-id>|<agent:identity>|READY|INLINE|<nonce>."
    )


def record_dispatch(row, details, *, failed=False):
    """Retain the complete result before ACK waiting, without changing its outcome."""
    row['dispatch_error_details' if failed else 'dispatch_result'] = copy.deepcopy(details)
    row['dispatch_evidence_status'] = 'UNAVAILABLE'
    try:
        if not isinstance(details, dict) or not isinstance(details.get('attempt'), str):
            raise ValueError('sender did not return an original attempt path')
        path = Path(details['attempt'])
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ValueError('sender attempt must be an absolute original path')
        raw, identity = read_snapshot(path)
        attempt = json.loads(raw)
        if not isinstance(attempt, dict):
            raise ValueError('original sender attempt must be an object')
        pin = dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                   bytes=len(raw), identity=list(identity))
        if snapshot(path) != pin:
            raise ValueError('original attempt changed while saving its pin')
        # An actual sender result is evidence even when its controller cannot
        # be authenticated. Preserve that independent fact before checking the
        # release; it must not be reconstructed from a nonce later.
        row.update(dispatch_attempt=pin,
                   dispatch_evidence_status='ORIGINAL_ATTEMPT_PINNED')
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        row['dispatch_evidence_unavailable_reason'] = str(exc)
        return
    row['dispatch_controller_evidence_status'] = 'UNVERIFIED'
    try:
        controller = attempt.get('controller_provenance')
        row['dispatch_controller_provenance'] = copy.deepcopy(controller)
        if (not isinstance(controller, dict) or not controller.get('source')
                or details.get('controller_provenance') != controller):
            raise ValueError('original controller provenance was not returned')
        verify_controller(row.get('dispatch_controller_pin'), controller)
        row['dispatch_controller_evidence_status'] = 'VERIFIED'
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        row['dispatch_controller_evidence_unavailable_reason'] = str(exc)

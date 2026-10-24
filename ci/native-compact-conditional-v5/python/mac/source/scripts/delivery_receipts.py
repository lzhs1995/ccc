"""Read-only native callback acknowledgement and atomic receipts; no send budget."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util

from datetime import datetime

import hashlib

import json

import os

from pathlib import Path

import stat

import sys

import tempfile

import time

from cmux_evidence_io import snapshot as evidence_snapshot, read_bytes, open_regular_lock

class ReceiptError(RuntimeError):
    pass

def snapshot(path):
    path = Path(path)
    try:
        data, identity = evidence_snapshot(path)
    except (OSError, ValueError) as exc:
        raise ReceiptError("bounded regular evidence required: " + str(path)) from exc
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "identity": list(identity)}

def publish(path, value):
    """No-overwrite publication; a crash cannot expose a partially written JSON."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.link(tmp, path)  # atomic, fails if another sender/receiver already won
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        os.unlink(tmp)

@contextlib.contextmanager
def delivery_attempt_lock(path):
    """Share the sender's existing lock; acknowledgement never spends a retry."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open_regular_lock(path.with_suffix('.lock')) as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReceiptError('original delivery in progress; reconcile without sending later') from exc
        yield

def bind(pack, bridge, *, receiver=False):
    gate = json.loads(read_bytes(Path(pack["identity_gate"])))
    if gate.get("task_id") != pack["task_id"] or gate.get("status") != "PASS":
        raise ReceiptError("task identity gate mismatch")
    peers = gate.get("executors") or [{"surface_ref": gate.get("executor"),
                                        "surface_uuid": gate.get("executor_surface_uuid")}]
    peers = [r for r in peers if r.get("surface_ref") == pack.get("executor")]
    if len(peers) != 1 or pack.get("callback_target") != gate.get("supervisor"):
        raise ReceiptError("callback participant mismatch")
    executor = peers[0]["surface_uuid"]
    if pack.get("executor_uuid", executor) != executor:
        raise ReceiptError("executor UUID mismatch")
    supervisor = gate["supervisor_surface_uuid"]
    return bridge.pin_workspace(
        pack["executor"] if receiver else pack["callback_target"],
        workspace_uuid=gate["workspace_uuid"],
        caller_uuid=supervisor if receiver else executor,
        target_uuid=executor if receiver else supervisor,
    )

def callback_receipt(pack, report, *, producer, evidence):
    return {"version": 2, "task_id": pack["task_id"],
            "completion_nonce": pack["completion_nonce"],
            "completion_callback": pack["completion_callback"],
            "callback_target": pack["callback_target"], "report": pack["report"],
            "report_sha256": report["sha256"], "report_bytes": report["bytes"],
            "confirmed": True, "confirmation_source": producer,
            "evidence": evidence, "recorded_at_epoch": time.time()}

def native_user_record(path, line_number, *, session_id, exact_text, after):
    """按指定行有界读取；大日志使用原 journal 的增量核收入口。"""
    import cmux_native_delivery as native
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink()
            or type(line_number) is not int or line_number < 2):
        raise ReceiptError("invalid native transcript location")
    with native._open(path) as f:
        before = os.fstat(f.fileno())
        meta = json.loads(f.readline(native.MAX_RECORD + 1))
        if meta.get("type") != "session_meta" or meta.get("payload", {}).get("id") != session_id:
            raise ReceiptError("native session mismatch")
        number = 1
        while f.tell() < min(before.st_size, native.MAX_SCAN):
            offset = f.tell()
            raw = f.readline(native.MAX_RECORD + 1)
            if len(raw) > native.MAX_RECORD or not raw.endswith(b'\n'):
                raise ReceiptError('native record incomplete or oversized')
            number += 1
            if number == line_number:
                break
        else:
            raise ReceiptError('native line unavailable within budget; use original --reconcile-only')
        current = path.stat()
        if ((current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or current.st_size < offset + len(raw)):
            raise ReceiptError('native transcript replaced or truncated')
    event = json.loads(raw)
    payload = event.get("payload", {})
    if (event.get("type") != "response_item" or payload.get("type") != "message"
            or payload.get("role") != "user"
            or payload.get('content') != [{'type': 'input_text', 'text': exact_text}]):
        raise ReceiptError("record is not this exact received user message")
    if datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")) < datetime.fromisoformat(after.replace("Z", "+00:00")):
        raise ReceiptError("record predates task finalization")
    return {"path": str(path), "line": line_number, "session_id": session_id,
            "record_sha256": hashlib.sha256(raw).hexdigest(), "record": event,
            'offset': offset, 'length': len(raw), 'device': before.st_dev, 'inode': before.st_ino}

def acknowledge_received_callback(pack_path, bridge, *, transcript, line,
                                  received_callback):
    """旧无 journal 核收入口退役；保留签名，绝不追补发送意图或回执。"""
    raise ReceiptError(
        'legacy native acknowledgement retired: original native binding and paste fence required; '
        'use the original controller --reconcile-only; preserve all existing evidence')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task-pack', type=Path, required=True)
    parser.add_argument('--transcript', type=Path, required=True)
    parser.add_argument('--line', type=int, required=True)
    parser.add_argument('--received-callback', required=True)
    parser.add_argument('--bound-bridge', type=Path,
                        help='For an old task: its fixed release scripts/cmux_bridge.py')
    args = parser.parse_args(argv)
    if args.bound_bridge:
        pack = json.loads(read_bytes(args.task_pack))
        source = args.bound_bridge
        if (not source.is_absolute() or source.name != 'cmux_bridge.py'
                or source.parent.name != 'scripts'
                or source.parent.parent / 'SKILL.md' != Path(pack['required_skill'])):
            raise ReceiptError('bound bridge must belong to the original required_skill release')
        pin = snapshot(source)
        spec = importlib.util.spec_from_file_location('bound_callback_bridge', source)
        bridge = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = bridge
        spec.loader.exec_module(bridge)
        if snapshot(source) != pin:
            raise ReceiptError('bound bridge changed during import')
    else:
        import cmux_bridge as bridge
    receipt = acknowledge_received_callback(args.task_pack, bridge, transcript=args.transcript,
                                            line=args.line, received_callback=args.received_callback)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Observe an existing handshake; never send, renew its nonce, or edit receipts.

Run in a fresh process with the original immutable controller and SHA-pinned
receipt/attempt. stdout is a separate observation, not a replacement receipt.
An ACK, native receipt of the notice, executor body reading and task acceptance
are deliberately separate facts. A timeout means ACK_NOT_VISIBLE here.
"""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys
import time

from repair_task_notice import controller_modules
import handshake_panel as panel
from native_user_record import generated_user_record


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def original_modules(controller):
    bridge, native, evidence, _, _ = controller_modules(controller)
    journal = importlib.import_module('cmux_message_journal')
    if Path(journal.__file__).resolve().parent != Path(controller) / 'scripts':
        raise ValueError('ACK_OBSERVATION_MIXED_CONTROLLER')
    return bridge, native, evidence, journal


def exclude_generated_receipt(native, binding, text, received, intent_at, fence):
    """Narrow negative check over old proof bytes; never change its controller.

    Historical controllers remain responsible for identity, intent and proof
    validation. This current observer can reject a known summary false positive,
    but cannot manufacture a receipt or rewrite the original attempt.
    """
    if received.get('confirmed') is not True:
        return received
    proof = received['native_proof']
    fenced = native._fenced_binding(binding, text, fence, intent_at)
    pin = fenced['transcript']
    with native._open(pin['path']) as stream:
        native._check_file(stream, binding)
        native._check_file(stream, fenced)
        stream.seek(proof['offset'])
        raw = stream.read(proof['length'])
        if digest(raw) != proof['sha256'] or not raw.endswith(b'\n'):
            raise ValueError('ACK_NATIVE_PROOF_CHANGED')
        record = json.loads(raw)
        native._check_file(stream, fenced)
    if generated_user_record(record):
        return dict(confirmed=False, state='NATIVE_GENERATED_SUMMARY_EXCLUDED',
                    excluded_proof=proof, original_observation=received,
                    next_offset=received.get('next_offset'), input_operations=0)
    return received


def observe(args, modules=None):
    bridge, native, evidence, journal = modules or original_modules(args.controller)
    paths = [Path(args.receipt), Path(args.attempt), Path(args.text_file)]
    pins = [evidence.snapshot(path) for path in paths]
    receipt_raw, attempt_raw, text_raw = [pin[0] for pin in pins]
    if digest(receipt_raw) != args.receipt_sha256:
        raise ValueError('ACK_ORIGINAL_RECEIPT_CHANGED')
    if digest(attempt_raw) != args.attempt_sha256:
        raise ValueError('ACK_ORIGINAL_ATTEMPT_CHANGED')
    receipt, attempt = json.loads(receipt_raw), json.loads(attempt_raw)
    text = text_raw.decode('utf-8')
    binding = attempt['native_binding']
    identity = binding['identity']
    target = identity['target_surface_uuid']
    rows = receipt.get('executors', [receipt])
    if not isinstance(rows, list):
        raise ValueError('ACK_RECEIPT_EXECUTORS_INVALID')
    matches = [row for row in rows if isinstance(row, dict) and row.get('executor') == target]
    gate_pin = None
    gate_path = paths[0].parent / 'identity-gate.json'
    if not matches:
        try:
            gate_pin = evidence.snapshot(gate_path)
        except OSError as exc:
            raise ValueError('ACK_RECEIPT_EXECUTOR_NOT_UNIQUE') from exc
        gate = json.loads(gate_pin[0])
        peers = [p for p in gate.get('executors', []) if p.get('surface_uuid') == target]
        if (gate.get('status') != 'PASS' or len(peers) != 1
                or gate.get('supervisor_surface_uuid') != identity['caller_surface_uuid']
                or gate.get('workspace_uuid') != identity['workspace_uuid']):
            raise ValueError('ACK_RECEIPT_EXECUTOR_GATE_CHANGED')
        matches = [row for row in rows if isinstance(row, dict) and panel.matches(row, peers[0])
                   and row.get('task_id') == gate.get('task_id')]
    if len(matches) != 1:
        raise ValueError('ACK_RECEIPT_EXECUTOR_NOT_UNIQUE')
    row = matches[0]
    nonce, task, provider = row['ack_nonce'], row['task_id'], row['executor_provider']
    if (not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{8}', nonce)
            or not isinstance(task, str) or not task or '|' in task
            or provider != 'claude' or binding.get('provider') != provider):
        raise ValueError('ACK_ORIGINAL_CHALLENGE_INVALID')
    expected = f'PREFLIGHT_ACK|{task}|{provider}:identity|READY|INLINE|{nonce}'
    payload_sha = digest(text_raw)
    if (row.get('ack_line_expected') != expected
            or row.get('dispatch_payload_sha256') != payload_sha
            or attempt.get('binding') != dict(identity=identity, marker=nonce, payload_sha256=payload_sha)
            or binding.get('payload_sha256') != payload_sha or nonce not in text):
        raise ValueError('ACK_ORIGINAL_CHALLENGE_BINDING_CHANGED')
    key = digest(json.dumps([identity['caller_surface_uuid'], nonce], sort_keys=True).encode())
    directory = Path.home() / '.local/state/multi-agent-collaboration/message-dispatch-v1' / key
    if paths[1].parent != directory or not re.fullmatch(r'attempt-[0-9]{4}\.json', paths[1].name):
        raise ValueError('ACK_ORIGINAL_MESSAGE_ATTEMPT_REQUIRED')
    body_pin = journal.original_body_pin(text, attempt)
    reference = journal.prompt_reference.wire_reference(text)
    if row.get('dispatch_body_reference') != reference:
        raise ValueError('ACK_ORIGINAL_BODY_REFERENCE_CHANGED')
    # Validate the original input fence; queued/pasted text cannot manufacture
    # native receipt. probe() is read-only; scan_original() writes a sidecar even
    # with read_only=True, so it is intentionally NOT used by this entry point.
    intent_at, fence = native._original_intent(attempt, binding, text)
    native.require_bound(bridge, target, binding, text, read_only=True)
    received = native.probe(binding, text, not_before=intent_at, paste_fence=fence,
                            max_bytes=min(native.MAX_SCAN, 4 * 1024 * 1024))
    ack = None
    try:
        ack = bridge.wait_for_ack(target, task_id=task, provider=provider, nonce=nonce,
                                  timeout=args.timeout, poll=1, lines=args.lines)
    except TimeoutError:
        pass
    if ack is not None and ack != expected:
        raise ValueError('ACK_ORIGINAL_PARSER_RESULT_CHANGED')
    native.require_bound(bridge, target, binding, text, read_only=True)
    if (any(evidence.snapshot(path) != pin for path, pin in zip(paths, pins))
            or (gate_pin is not None and evidence.snapshot(gate_path) != gate_pin)
            or journal.original_body_pin(text, attempt) != body_pin):
        raise ValueError('ACK_ORIGINAL_EVIDENCE_CHANGED_DURING_OBSERVATION')
    if received.get('confirmed') is True and not native.validate_proof(
            binding, text, received.get('native_proof'), not_before=intent_at, paste_fence=fence):
        raise ValueError('ACK_NATIVE_PROOF_CHANGED')
    received = exclude_generated_receipt(native, binding, text, received, intent_at, fence)
    return dict(
        schema='original-handshake-observation-v1', status='OBSERVED',
        observed_at_epoch=time.time(), controller=str(args.controller),
        original_receipt=str(paths[0]), original_receipt_sha256=digest(receipt_raw),
        original_attempt=str(paths[1]), original_attempt_sha256=digest(attempt_raw),
        task_id=task, executor_uuid=target, original_nonce=nonce,
        payload_sha256=payload_sha, original_receipt_status=row.get('status'),
        original_receipt_lifecycle=row.get('lifecycle'), original_evidence_unchanged=True,
        input_operations=0, journal_writes=0,
        ack=dict(observed=ack is not None, state='ACK_OBSERVED' if ack else 'ACK_NOT_VISIBLE',
                 line=ack, source='original_controller_response_nonce_parser'),
        native_message=received,
        body=dict(reference=reference, verified_locally=body_pin is not None,
                  executor_read='NOT_EVALUATED'),
        business_acceptance='NOT_EVALUATED',
    )


def bounded_integer(minimum, maximum):
    def parse(value):
        number = int(value)
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f'expected {minimum}..{maximum}')
        return number
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('controller', 'receipt', 'receipt-sha256', 'attempt', 'attempt-sha256', 'text-file'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--timeout', type=bounded_integer(1, 45), default=1,
                        help='read-only ACK observation seconds; default 1, maximum 45')
    parser.add_argument('--lines', type=bounded_integer(1, 2000), default=200)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(observe(args), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps(dict(status='OBSERVATION_UNAVAILABLE', error=str(exc),
                              input_operations=0, journal_writes=0), ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())

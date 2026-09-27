#!/usr/bin/env python3
"""One-shot repair of a sustained N listener that never dispatched a request.

Preparation is read-only except for the requested private evidence file.
Apply preserves the original fifty native processes, sessions and ledger. It
stops only the exact idle gateway and submits one fixed prompt per original
409 turn. It never retries an uncertain signal, process launch, paste or Enter.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ccc_access_service as service
import ccc_codex_queue as native
import ccc_guard_migration as migration
import ccc_guard_scope as scope
import ccc_workspace_batch as batch
import cmux_codex_watch as core

FAILURE = 'first-wave setup failed; no partial substitute for fifty checks'
OPERATOR_FILES = ('tools/recover_access_setup.py', 'ccc_workspace_batch.py',
                  'ccc_guard_scope.py', 'ccc_guard_migration.py',
                  'ccc_codex_queue.py', 'cmux_codex_watch.py')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def operator_source():
    root = Path(__file__).resolve().parents[1]
    return {name: sha(root / name) for name in OPERATOR_FILES}


def old_root(config, owner):
    generation = hashlib.sha256(json.dumps(owner['source'], sort_keys=True).encode()).hexdigest()
    return Path(config).parent / 'access-gateway' / ('runtime-' + generation)


def membership(client, wid):
    return {row['surface_id']: row for row in core.workspace_surface_records(client.tree(), wid).values()}


def terminal_failure(turn, descriptor, slot):
    index = slot['index']
    endpoint = (f"http://127.0.0.1:{descriptor['port']}/{descriptor['policy']['job_id']}/"
                f"{index}/{descriptor['tokens'][index]}/v1/responses")
    message = f'unexpected status 409 Conflict: {FAILURE}, url: {endpoint}'
    return bool(turn and turn.get('kind') == 'task_complete' and turn.get('turn_id')
                and isinstance(turn.get('error'), dict) and turn['error'].get('message') == message)


def turn_identity(turn):
    return {k: turn[k] for k in ('kind', 'at', 'turn_id', 'error')}


def inspect_native(client, descriptor, slot, *, expected=None, draft=False, members=None):
    """Bind visible composer to the original process's currently open rollout."""
    wid, sid = descriptor['policy']['workspace_id'], slot['surface_id']
    members = membership(client, wid) if members is None else members
    if sid not in members:
        raise ValueError('an original native surface moved or closed')
    identity = scope.process(slot['pid'])
    if (not identity or identity['surface_id'] != sid or identity['environment_workspace_id'] != wid
            or identity['process_start'] != slot['process_start']
            or expected and identity != expected['identity']):
        raise ValueError('original native process identity changed')
    transcript = Path(slot['transcript']).resolve()
    result = subprocess.run(['/usr/sbin/lsof', '-a', '-p', str(slot['pid']), '-F', 'fan'],
                            capture_output=True, text=True, timeout=8)
    writable = {p for p in native.writable_open_files(result.stdout)
                if p.name.startswith('rollout-') and p.suffix == '.jsonl'}
    if result.returncode or writable != {transcript}:
        raise ValueError('original native session is no longer the writable rollout')
    turn = native.task_snapshot(transcript, slot['session_id'])
    if not terminal_failure(turn, descriptor, slot):
        raise ValueError('native session no longer ends in its exact local setup 409')
    if expected and turn_identity(turn) != expected['turn']:
        raise ValueError('original failed turn changed')
    grid = core.Grid.from_rpc(client.replay(wid, sid), sid)
    if draft:
        valid = batch.BatchWorker._own_prompt_draft(grid)
    else:
        valid = (core._composer_status(grid)[0] == 'empty'
                 and core.classify_grid(grid).kind == 'idle')
    if not valid or not scope.matches(identity):
        raise ValueError('original native composer is not the expected empty or owned draft')
    # Lifecycle can change while reading the viewport; an old grid is not input
    # authorization for a new turn.
    after = native.task_snapshot(transcript, slot['session_id'])
    if not after or turn_identity(after) != turn_identity(turn):
        raise ValueError('native lifecycle changed during viewport inspection')
    return {'index': slot['index'], 'surface_id': sid, 'session_id': slot['session_id'],
            'identity': identity, 'transcript': str(transcript), 'turn': turn_identity(turn)}


def idle_listener(owner):
    """No TCP peer is allowed, including a local client still in a handler."""
    result = subprocess.run(['/usr/sbin/lsof', '-nP', '-a', '-p', str(owner['pid']),
                             '-iTCP', '-F', 'pftnT'],
                            capture_output=True, text=True, timeout=8)
    rows, current = [], None
    process = None
    for line in result.stdout.splitlines():
        if line.startswith('p'):
            process = int(line[1:])
        elif line.startswith('f'):
            current = {'fd': line[1:]}
            rows.append(current)
        elif current is not None and line.startswith('n'):
            current['name'] = line[1:]
        elif current is not None and line.startswith('TST='):
            current['state'] = line[4:]
    if (result.returncode or process != owner['pid'] or len(rows) != 1
            or rows[0].get('name') != f"127.0.0.1:{owner['port']}"
            or rows[0].get('state') != 'LISTEN'):
        raise ValueError('original gateway has a TCP peer or its sole listener was not proven')
    return rows


def inspect_status(config, jid, owner, expected):
    value = service.status(config, jid)
    if (value.get('instance') != owner['instance'] or value.get('job_id') != jid
            or not 0 <= time.time() - value.get('updated_at', 0) < 2
            or value.get('fault') != FAILURE or value.get('attempts') != expected['journal']['attempts']
            or any(value.get(k) != 0 for k in ('forwarded', 'in_flight', 'complete'))
            or value.get('first_complete') is not None or value.get('blocked_slots')
            or value.get('closed')):
        raise ValueError('old setup fault is not fresh, idle and never dispatched')
    return value


def authorized(config, job, sid=None):
    if not batch.allowed(config, job):
        raise ValueError('original N is paused, blocked or no longer authorized')
    rule = core.workspace_rule_by_id(config, job['workspace_id'])
    if sid is not None and (sid in rule.get('excluded_surface_ids', [])
            or core.batch_start_hold(rule, sid)
            or any(t.get('surface_id') == sid and (t.get('paused') or not t.get('enabled', True))
                   for t in config.get('targets', []))):
        raise ValueError('original surface input is no longer authorized')


def prepare(config, job_id, output, *, client=None):
    config, output = Path(config).resolve(), Path(output).resolve()
    client = client or migration.cmux_client(config)
    descriptor = service.read_private(service.job_root(config, job_id) / 'access.json')
    owners = []
    for path in (config.parent / 'access-gateway').glob('runtime-*/owner.json'):
        candidate = service.read_private(path)
        if (candidate.get('instance') == descriptor.get('gateway_instance')
                and candidate.get('port') == descriptor.get('port')):
            owners.append(candidate)
    if len(owners) != 1 or not service.owner_alive(owners[0], config, check_runtime=False):
        raise ValueError('no unique live original gateway')
    owner = owners[0]
    jobs, inputs = {}, []
    current = core.ConfigStore(config).load()
    for directory in (config.parent / 'workspace-batches').iterdir():
        if not directory.is_dir():
            continue
        job = core.load_json(directory / 'job.json', {})
        try:
            desc = service.read_private(directory / 'access.json')
        except FileNotFoundError:
            if 'access_mode' in job or 'access_policy' in job:
                raise ValueError('N descriptor missing during generation inventory')
            continue
        if desc.get('gateway_instance') != owner['instance']:
            continue
        policy = service.verify_descriptor(desc, config, job)
        if policy.attempt_mode != 'sustained':
            raise ValueError('a finite job cannot be repaired or replayed')
        expected = {'descriptor_sha256': service.descriptor_sha(desc),
            'job_sha256': sha(directory / 'job.json'),
            'journal': service.cancelled_setup_history(directory / 'access-journal.jsonl', policy),
            'bindings': {str(i): sha(directory / f'access-session-{i}.json') for i in range(50)}}
        jobs[job['id']] = expected
        inspect_status(config, job['id'], owner, expected)
        if job['id'] == job_id:
            authorized(current, job)
            if (len(job.get('slots', [])) != 50
                    or {s.get('index') for s in job['slots']} != set(range(50))
                    or len({s.get('surface_id') for s in job['slots']}) != 50
                    or len({s.get('session_id') for s in job['slots']}) != 50):
                raise ValueError('repair requires the original fifty distinct slots')
            members = membership(client, job['workspace_id'])
            for slot in sorted(job['slots'], key=lambda s: s['index']):
                authorized(current, job, slot['surface_id'])
                binding = service.read_private(directory / f"access-session-{slot['index']}.json")
                if any(binding.get(k) != slot.get(k) for k in
                       ('index', 'surface_id', 'session_id', 'pid', 'process_start')):
                    raise ValueError('job slot differs from its frozen native binding')
                inputs.append(inspect_native(client, desc, slot, members=members))
        else:
            # An omitted native request might still be pending locally. Only
            # completely retired jobs are excluded from this repair's inputs.
            for i in range(50):
                binding = service.read_private(directory / f'access-session-{i}.json')
                if scope.birth(binding.get('pid'), codex=True):
                    raise ValueError('a non-restored historical job still has native processes')
    plan = {'version': 1, 'purpose': 'recover-sustained-before-first-dispatch',
            'config_path': str(config), 'config_sha256': sha(config), 'source': service.fingerprint(),
            'operator_source': operator_source(), 'old_owner': owner,
            'jobs': jobs, 'restore_jobs': [job_id], 'inputs': inputs,
            'native_before': scope.scan(), 'prepared_at': time.time()}
    if len(inputs) != 50:
        raise ValueError('the requested original job was not fully inventoried')
    idle_listener(owner)
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    service.create_private(output, plan)
    service.validate_recovery_plan(config, output, require_old_dead=False)
    return plan


def preflight(path, client):
    plan = service.read_private(path)
    config = Path(plan['config_path'])
    plan, previous = service.validate_recovery_plan(config, path, require_old_dead=False)
    if plan.get('operator_source') != operator_source() or len(plan['restore_jobs']) != 1:
        raise ValueError('one-shot operator source or explicit recovery scope changed')
    owner = plan['old_owner']
    if not service.owner_alive(owner, config, check_runtime=False):
        raise ValueError('original gateway changed before any recovery action')
    repair = service.recovery_root(config, plan)
    if ((previous / 'setup-recovery-claim.json').exists() or (repair / 'owner.json').exists()):
        raise ValueError('a recovery was already attempted; inspect its receipt, never replay it')
    for jid, expected in plan['jobs'].items():
        inspect_status(config, jid, owner, expected)
        if jid not in plan['restore_jobs']:
            for i in range(50):
                binding = service.read_private(service.job_root(config, jid) / f'access-session-{i}.json')
                if scope.birth(binding.get('pid'), codex=True):
                    raise ValueError('retired native job became live before recovery')
    jid = plan['restore_jobs'][0]
    directory = service.job_root(config, jid)
    job = core.load_json(directory / 'job.json', {})
    desc = service.read_private(directory / 'access.json')
    config_value = core.ConfigStore(config).load()
    members = membership(client, job['workspace_id'])
    inputs = plan.get('inputs')
    if (not isinstance(inputs, list) or len(inputs) != 50
            or [r.get('index') for r in inputs] != list(range(50))):
        raise ValueError('all original inputs must be explicitly prepared exactly once')
    slots = {s['index']: s for s in job['slots']}
    for expected in inputs:
        slot = slots[expected['index']]
        authorized(config_value, job, slot['surface_id'])
        observed = inspect_native(client, desc, slot, expected=expected, members=members)
        if observed != expected:
            raise ValueError('prepared native input binding changed')
    if any(not scope.matches(row) for row in plan['native_before']):
        raise ValueError('a baseline native identity changed; prepare a new actual baseline')
    idle_listener(owner)
    return plan, previous


class Receipt:
    def __init__(self, path, plan_path):
        self.path = Path(path)
        self.token = uuid.uuid4().hex
        self.value = {'version': 1, 'id': self.token, 'pid': os.getpid(), 'birth': scope.birth(os.getpid()),
            'plan_path': str(Path(plan_path).resolve()), 'plan_sha256': sha(plan_path),
            'phase': 'claimed', 'steps': [], 'inputs': {}, 'created_at': time.time()}
        service.create_private(self.path, self.value)

    def save(self, phase, **fields):
        existing = service.read_private(self.path)
        if (existing.get('id') != self.token or existing.get('pid') != os.getpid()
                or existing.get('birth') != scope.birth(os.getpid())):
            raise ValueError('recovery receipt ownership changed')
        self.value.update(phase=phase, **fields)
        self.value['steps'].append({'phase': phase, 'at': time.time()})
        core.atomic_write_json(self.path, self.value)


def submit_originals(plan, owner, receipt, client):
    config = Path(plan['config_path'])
    jid = plan['restore_jobs'][0]
    directory = service.job_root(config, jid)
    job = core.load_json(directory / 'job.json', {})
    desc = service.read_private(directory / 'access.json')
    slots = {s['index']: s for s in job['slots']}
    wid = job['workspace_id']
    # Whole-cohort validation precedes all input. In particular, a moved last
    # slot must result in zero pasted prompts, not 49 partial submissions.
    members = membership(client, wid)
    for expected in plan['inputs']:
        authorized(core.ConfigStore(config).load(), job, expected['surface_id'])
        inspect_native(client, desc, slots[expected['index']], expected=expected, members=members)
    for expected in plan['inputs']:
        slot, sid = slots[expected['index']], expected['surface_id']
        if str(slot['index']) in receipt.value['inputs']:
            raise ValueError('an input already has an intent; never replay an uncertain paste')
        if (not service.owner_alive(owner, config)
                or service.status(config, jid).get('first_complete')):
            raise ValueError('repaired gateway changed or already succeeded')
        authorized(core.ConfigStore(config).load(), job, sid)
        inspect_native(client, desc, slot, expected=expected)
        item = {'index': slot['index'], 'surface_id': sid, 'session_id': slot['session_id'],
                'old_turn_id': expected['turn']['turn_id'], 'paste_intent_at': time.time()}
        receipt.value['inputs'][str(slot['index'])] = item
        receipt.save('input_paste_intent')
        inspect_native(client, desc, slot, expected=expected)
        authorized(core.ConfigStore(config).load(), job, sid)
        client.send_text(wid, sid, batch.PROMPT)
        item['paste_ack_at'] = time.time()
        receipt.save('input_pasted')
        deadline = time.monotonic() + 2
        while True:
            try:
                inspect_native(client, desc, slot, expected=expected, draft=True)
                break
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.025)
        authorized(core.ConfigStore(config).load(), job, sid)
        item['enter_intent_at'] = time.time()
        receipt.save('input_enter_intent')
        # Re-read after the durable intent too; no cached viewport permits Enter.
        inspect_native(client, desc, slot, expected=expected, draft=True)
        authorized(core.ConfigStore(config).load(), job, sid)
        client.send_key(wid, sid, 'enter')
        item['enter_ack_at'] = time.time()
        receipt.save('input_submitted')
    pending = {row['index']: row for row in plan['inputs']}
    deadline = time.monotonic() + 15
    while pending and time.monotonic() < deadline:
        for index, expected in list(pending.items()):
            if not scope.matches(expected['identity']):
                raise ValueError('original native process changed after input')
            turn = native.task_snapshot(expected['transcript'], expected['session_id'])
            if (turn and turn.get('kind') in {'task_started', 'task_complete'}
                    and turn.get('turn_id') and turn['turn_id'] != expected['turn']['turn_id']):
                receipt.value['inputs'][str(index)]['new_turn_id'] = turn['turn_id']
                receipt.value['inputs'][str(index)]['new_turn_observed_at'] = time.time()
                pending.pop(index)
        if pending:
            time.sleep(.05)
    if pending:
        raise ValueError('some new turns remain unconfirmed; input will not be replayed')
    receipt.save('original_fifty_resumed')


def apply(path, receipt_path, *, client=None):
    path = Path(path).resolve()
    plan = service.read_private(path)
    config = Path(plan['config_path'])
    client = client or migration.cmux_client(config)
    plan, previous = preflight(path, client)
    repair = service.recovery_root(config, plan)
    jid = plan['restore_jobs'][0]
    wid = service.read_private(service.job_root(config, jid) / 'access.json')['policy']['workspace_id']
    receipt = None
    # All other workspaces continue to read configuration and run normally.
    # config.lock is held only across inventory and the listener handoff, then
    # released before any native input; pause can still revoke authorization.
    with core.workspace_input_lock(config, wid):
        try:
            with contextlib.ExitStack() as locks:
                locks.enter_context(core.FileLock(config.parent / 'config.lock', timeout_sec=10))
                locks.enter_context(core.FileLock(previous / 'start.lock', timeout_sec=10))
                service.root(config).mkdir(parents=True, mode=0o700, exist_ok=True)
                repair.mkdir(parents=True, mode=0o700, exist_ok=True)
                locks.enter_context(core.FileLock(repair / 'start.lock', timeout_sec=10))
                plan, previous = preflight(path, client)
                receipt = Receipt(receipt_path, path)
                service.create_private(previous / 'setup-recovery-claim.json', {
                    'receipt': str(Path(receipt_path).resolve()), 'receipt_id': receipt.token,
                    'plan_path': str(path), 'plan_sha256': sha(path)})
                receipt.save('gateway_stop_intent', old_owner=plan['old_owner'])
                idle_listener(plan['old_owner'])
                if not service.owner_alive(plan['old_owner'], config, check_runtime=False):
                    raise ValueError('old gateway identity changed at the signal boundary')
                os.kill(plan['old_owner']['pid'], signal.SIGTERM)
                deadline = time.monotonic() + 10
                while scope.birth(plan['old_owner']['pid']) == plan['old_owner']['birth']:
                    child = service._started_processes.get(plan['old_owner']['pid'])
                    if child is not None:
                        child.poll()  # Reap only our own child in a local fixture.
                    if time.monotonic() >= deadline:
                        raise RuntimeError('old idle gateway exit unconfirmed; no second signal or spawn')
                    time.sleep(.025)
                receipt.save('gateway_stopped')
                command = [sys.executable, '-B', str(Path(service.__file__).resolve()), 'serve',
                           '--config', str(config), '--recovery', str(path)]
                receipt.save('gateway_start_intent', command=command)
                with (repair / 'service.log').open('ab') as log:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                        stdout=log, stderr=log, start_new_session=True, close_fds=True)
                service._started_processes[process.pid] = process
                receipt.save('gateway_started', replacement_pid=process.pid)
                deadline, owner = time.monotonic() + 10, None
                while time.monotonic() < deadline:
                    try:
                        observed = service.read_private(repair / 'owner.json')
                        if (observed.get('pid') == process.pid and service.owner_alive(observed, config)
                                and observed.get('port') == plan['old_owner']['port']
                                and observed.get('instance') == plan['old_owner']['instance']
                                and service.ping(observed)):
                            owner = observed
                            break
                    except (OSError, ValueError):
                        pass
                    if process.poll() is not None:
                        raise RuntimeError('repair receiver exited; no duplicate was started')
                    time.sleep(.025)
                if owner is None:
                    raise RuntimeError('repair receiver startup unconfirmed; no duplicate was started')
                # Original descriptor, binding and full journal bytes remain
                # unchanged until input; a recovery never refunds old attempts.
                service.validate_recovery_plan(config, path)
                service.create_private(previous / 'repair-owner.json', owner)
                receipt.save('gateway_recovered', new_owner=owner)
            submit_originals(plan, owner, receipt, client)
            changed = [row for row in plan['native_before'] if not scope.matches(row)]
            receipt.save('resumed_requires_observation', native_identity_changes=changed)
            if changed:
                raise RuntimeError('baseline native identity changed during recovery; receipt retained')
            return receipt.value
        except BaseException as exc:
            if receipt is not None:
                receipt.save('failed_requires_inspection', error_type=type(exc).__name__, error=str(exc))
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    preparing = sub.add_parser('prepare')
    preparing.add_argument('--config', type=Path, required=True)
    preparing.add_argument('--job', required=True)
    preparing.add_argument('--output', type=Path, required=True)
    checking = sub.add_parser('check')
    checking.add_argument('--plan', type=Path, required=True)
    applying = sub.add_parser('apply')
    applying.add_argument('--plan', type=Path, required=True)
    applying.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        value = prepare(args.config, args.job, args.output)
        print(json.dumps({'phase': 'prepared', 'path': str(args.output.resolve()),
                          'restore_jobs': value['restore_jobs'], 'original_inputs': len(value['inputs'])}))
    elif args.action == 'check':
        config = Path(service.read_private(args.plan)['config_path'])
        preflight(args.plan, migration.cmux_client(config))
        print(json.dumps({'phase': 'preflight_passed', 'signals': 0, 'inputs': 0}))
    else:
        value = apply(args.plan, args.receipt)
        print(json.dumps({'phase': value['phase'], 'receipt': str(args.receipt.resolve())}))


if __name__ == '__main__':
    main()

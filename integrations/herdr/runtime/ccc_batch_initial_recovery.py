"""Explicit single-use recovery of a proven failed pre-exec batch bootstrap.

Original launch claims/events stay immutable. A separately consumed recovery
attempt pins the same job/index/surface and uses the ordinary initial hook and
native first-task proof in a separate directory. No creation or terminal input
is performed by this module.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import signal
import time
import uuid

import cmux_codex_watch as core
import ccc_workspace_batch as batch
from ccc_guard_scope import birth, arguments
from ccc_codex_queue import batch_shell_identity
import ccc_codex_queue as native

SCHEMA = 'ccc-original-initial-recovery-v1'


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read_exact(path, digest):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('recovery evidence symlink refused')
    raw = path.read_bytes()
    if _sha(raw) != digest:
        raise RuntimeError('recovery original evidence changed: ' + str(path))
    return raw


def _original_gone(claim):
    pid = claim['bootstrap_pid']
    if type(pid) is not int or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    current = birth(pid)
    return current is not None and current != claim['bootstrap_birth']


def _children(pid):
    if native._proc_listpids is None:
        raise RuntimeError('native child inspection unavailable')
    values = (ctypes.c_int * 64)()
    size = ctypes.sizeof(values)
    count = native._proc_listpids(6, pid, values, size)
    if count < 0 or count >= size or count % ctypes.sizeof(ctypes.c_int):
        raise RuntimeError('incomplete original shell child inventory')
    return set(values[:count // ctypes.sizeof(ctypes.c_int)])


def _shell_tty_device(pid, expected_birth):
    if native._proc_pidinfo is None or batch_shell_identity(pid) != expected_birth:
        raise RuntimeError('original shell process identity unavailable')
    info = native._BsdInfo()
    size = ctypes.sizeof(info)
    if (native._proc_pidinfo(pid, 3, 0, ctypes.byref(info), size) != size or info.pid != pid
            or [info.start_sec, info.start_usec] != expected_birth
            or batch_shell_identity(pid) != expected_birth):
        raise RuntimeError('original shell process changed during tty observation')
    # proc_bsdinfo: misc[3] is e_tdev, not the foreground process group.
    return int(info.misc[3])


def _surface_tty(tree, surface_id):
    for window in tree.get('windows', []):
        for workspace in window.get('workspaces', []):
            for pane in workspace.get('panes', []):
                if pane.get('dock_scope') is not None:
                    continue
                for surface in pane.get('surfaces', []):
                    if surface.get('id') == surface_id:
                        tty = surface.get('tty')
                        if tty is None:
                            return None
                        if not isinstance(tty, str) or not tty.startswith('ttys') or '/' in tty:
                            raise RuntimeError('original surface tty unavailable')
                        return tty
    raise RuntimeError('original surface tty not found')


def _tty_for_device(device):
    matches = []
    for entry in os.scandir('/dev'):
        if entry.name.startswith('ttys') and not entry.is_symlink():
            try:
                if entry.stat(follow_symlinks=False).st_rdev == device:
                    matches.append(entry.name)
            except FileNotFoundError:
                continue
    if len(matches) != 1:
        raise RuntimeError('original shell kernel tty is not uniquely addressable')
    return matches[0]


def maintenance_active(config_path, job_id):
    marker = batch.job_path(config_path, job_id).parent / 'recovery-maintenance-owner.json'
    try:
        if marker.is_symlink():
            return True
        record = json.loads(marker.read_bytes())
        if record.get('job_id') != job_id or type(record.get('pid')) is not int:
            return True
        try:
            os.kill(record['pid'], 0)
        except ProcessLookupError:
            return False
        current = birth(record['pid'])
        return current is None or current == record.get('birth')
    except FileNotFoundError:
        return False
    except (OSError, ValueError, TypeError):
        return True


@contextlib.contextmanager
def maintenance(config_path, job_id, attempt_id):
    """Stop only the proven original helper; hold the same job's single-writer lock."""
    source = batch.job_path(config_path, job_id).parent
    job = core.load_json(source / 'job.json', {})
    if (not batch.argv_initial(job, config_path) or any(s.get('phase') not in
            {'confirmed', 'created', 'create_unknown'} for s in job.get('slots', []))):
        raise RuntimeError('batch still has mutable startup operations; maintenance not admitted')
    owner_birth = birth(os.getpid())
    if owner_birth is None:
        raise RuntimeError('maintenance owner birth unavailable')
    marker = source / 'recovery-maintenance-owner.json'
    record = {'schema': SCHEMA, 'job_id': job_id, 'attempt_id': attempt_id,
              'pid': os.getpid(), 'birth': owner_birth, 'at': time.time()}
    _, marker_bytes = batch._write_once_json(marker, record)
    helper = {'pid': job.get('worker_pid'), 'birth': job.get('worker_birth'), 'signal_sent': False}
    try:
        pid = helper['pid']
        expected_birth = helper['birth']
        if (type(pid) is not int or pid <= 1 or not isinstance(expected_birth, list)
                or len(expected_birth) != 2 or any(type(value) is not int for value in expected_birth)):
            raise RuntimeError('original batch helper complete birth unavailable')
        current_birth = birth(pid)
        if current_birth is None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError('original batch helper live identity unavailable')
        if current_birth == expected_birth:
            argv, _ = arguments(pid)
            if (len(argv) != 8 or argv[1] != '-B' or Path(argv[2]).name != 'ccc_workspace_batch.py'
                    or not Path(argv[2]).is_absolute()
                    or argv[3:] != ['run', '--config', str(config_path), '--job', job_id]):
                raise RuntimeError('original batch helper argv changed')
            batch._write_once_json(source / f'recovery-maintenance-{attempt_id}-intent.json', {
                **record, 'worker': helper, 'worker_argv': argv, 'operation': 'SIGTERM original batch helper only'})
            if arguments(pid)[0] != argv or birth(pid) != helper['birth']:
                raise RuntimeError('original batch helper changed before normal termination')
            os.kill(pid, signal.SIGTERM)
            helper['signal_sent'] = True
        with core.FileLock(source / 'worker.lock', timeout_sec=15, purpose='original batch recovery handoff'):
            if marker.read_bytes() != marker_bytes or birth(os.getpid()) != owner_birth:
                raise RuntimeError('maintenance owner changed')
            yield
    finally:
        if marker.exists() and marker.read_bytes() == marker_bytes:
            marker.unlink()
        batch._write_once_json(source / f'recovery-maintenance-{attempt_id}-result.json', {
            **record, 'worker': helper, 'at': time.time(), 'business_cli_signalled': False,
            'original_job_id_retained': True})
        # The new reconciler may also schedule this exact job; worker.lock
        # remains the only writer grant, so simultaneous launch cannot overlap.
        batch._launch(config_path, core.load_json(source / 'job.json', {}))


def validate_plan(config_path, plan):
    """Validate immutable pre-exec failure evidence; absence alone never grants recovery."""
    if plan.get('schema') != SCHEMA:
        raise RuntimeError('unsupported recovery plan')
    target = plan['target']
    job_id, index = target['job_id'], target['index']
    uuid.UUID(plan['attempt_id'])
    if type(index) is not int or not 0 <= index < batch.COUNT:
        raise RuntimeError('invalid recovery slot')
    source = batch.job_path(config_path, job_id).parent
    if Path(plan['config_path']).resolve() != Path(config_path).resolve():
        raise RuntimeError('recovery configuration changed')
    claim_raw = _read_exact(source / f'initial-argv-{index}.json', plan['original_claim_sha256'])
    receipt_raw = _read_exact(source / f'surface-{index}.json', plan['original_receipt_sha256'])
    claim, receipt = json.loads(claim_raw), json.loads(receipt_raw)
    for key in ('job_id', 'index', 'launch_id', 'workspace_id', 'surface_id'):
        if claim.get(key) != target.get(key) or receipt.get(key) != target.get(key):
            raise RuntimeError('original claim/receipt target mismatch')
    if (claim.get('state') != 'exec_intent' or claim.get('receipt_sha256') != _sha(receipt_raw)
            or claim.get('policy') != batch.ARGV_INITIAL_POLICY
            or claim.get('argv', [])[-1:] != [batch.PROMPT]):
        raise RuntimeError('original claim/receipt evidence invalid')
    event = source / f'initial-native-events-{index}.jsonl'
    event_info = event.stat()
    if (event.is_symlink() or event_info.st_size != 0
            or [event_info.st_dev, event_info.st_ino] != claim.get('tui_log_identity')
            or claim.get('tui_log') != str(event)):
        raise RuntimeError('original native execution remains uncertain')
    if any(os.path.lexists(source / f'{prefix}-{index}.json') for prefix in
           ('initial-hook-attempt', 'initial-session')):
        raise RuntimeError('original native startup may have executed')
    trace = _read_exact(plan['trace_path'], plan['trace_sha256']).decode('utf-8', 'strict')
    if not all(fragment in trace for fragment in [
        f'--job {job_id} --index {index} --launch-id {target["launch_id"]}',
        'if not final_guard():', 'tree = client.workspace_tree(wid)',
        'cmux_codex_watch.CmuxError: system.tree control request failed: timed out']):
        raise RuntimeError('exact original pre-exec traceback missing')
    job = core.load_json(source / 'job.json', {})
    slot = job['slots'][index]
    if (job.get('id') != job_id or job.get('workspace_id') != target['workspace_id']
            or slot.get('launch_id') != target['launch_id']
            or slot.get('surface_id') != target['surface_id']):
        raise RuntimeError('original batch slot changed')
    return source, claim, receipt, job


def record_directory(config_path, job_id, index):
    source = batch.job_path(config_path, job_id).parent
    active_path = source / f'recovery-active-{index}.json'
    if active_path.is_symlink():
        raise RuntimeError('recovery activation symlink refused')
    active = json.loads(active_path.read_bytes())
    attempt = str(uuid.UUID(active['attempt_id']))
    directory = source.resolve() / 'initial-recovery' / attempt
    if directory.is_symlink() or directory.resolve() != directory:
        raise RuntimeError('recovery evidence directory changed')
    plan = json.loads(_read_exact(directory / 'PLAN.json', active['plan_sha256']))
    if (active.get('schema') != SCHEMA or plan['attempt_id'] != attempt
            or plan['target']['job_id'] != job_id or plan['target']['index'] != index):
        raise RuntimeError('recovery activation identity changed')
    validate_plan(config_path, plan)
    return directory


def bootstrap_current(config_path, job_id, index):
    directory = record_directory(config_path, job_id, index)
    plan = json.loads((directory / 'PLAN.json').read_bytes())
    _, claim, receipt, _ = validate_plan(config_path, plan)
    pid = receipt['shell_pid']
    if (type(pid) is not int or pid <= 1 or not receipt.get('shell_start')
            or not _original_gone(claim) or os.getppid() != pid
            or batch_shell_identity(pid) != receipt['shell_start']
            or _children(pid) != {os.getpid()}):
        return False
    target = plan['target']
    # Shell startup metadata omits CMUX variables exported after zsh starts.
    # Bind the original kernel tty instead; the bootstrap's live environment
    # must still carry the exact original workspace and surface UUIDs.
    tty = plan.get('original_shell_tty')
    if not tty or os.ttyname(0) != '/dev/' + tty:
        return False
    return (_shell_tty_device(pid, receipt['shell_start']) == os.stat('/dev/' + tty).st_rdev
            and os.environ.get('CMUX_SURFACE_ID') == target['surface_id']
            and os.environ.get('CMUX_WORKSPACE_ID') == target['workspace_id']
            and batch_shell_identity(pid) == receipt['shell_start'])


def launch(config_path, job_id, index, launch_id):
    directory = record_directory(config_path, job_id, index)
    plan = json.loads((directory / 'PLAN.json').read_bytes())
    _, original_claim, _, job = validate_plan(config_path, plan)
    if launch_id != plan['target']['launch_id'] or not bootstrap_current(config_path, job_id, index):
        raise RuntimeError('recovery original shell or launch identity changed')
    if not batch.allowed(core.ConfigStore(Path(config_path)).load(), job):
        raise RuntimeError('recovery batch authorization revoked')
    executable = Path(original_claim['executable'])
    if (Path(original_claim['argv'][0]).resolve(strict=True) != executable
            or tuple(original_claim['executable_generation']) != batch._file_generation(executable)):
        raise RuntimeError('original native executable changed')
    argv = batch.native_launch_argv(config_path, job, index, native_argv=[original_claim['argv'][0]])
    # This path deliberately skips register(): original surface receipt, hold,
    # cwd and SQLite directory already exist and must not be replaced.
    return batch._launch_initial_registered(config_path, job, index, launch_id, argv)


def prepare(config_path, observation_path, attempt_id, *, worker_locked=False):
    """Append one recovery activation while the original job worker is quiesced."""
    observation_path = Path(observation_path).resolve(strict=True)
    observation = json.loads(observation_path.read_bytes())
    if (not observation.get('pre_exec_trace') or not observation.get('kill_zero_esrch')
            or not all(observation.get('absent_files', {}).values())):
        raise RuntimeError('observation does not prove original pre-exec failure')
    target = {key: observation[key] for key in ('job_id', 'index', 'launch_id', 'workspace_id', 'surface_id')}
    job_id, index = target['job_id'], target['index']
    source = batch.job_path(config_path, job_id).parent
    attempt_id = str(uuid.UUID(attempt_id))
    directory = source / 'initial-recovery' / attempt_id
    trace = observation_path.parent / 'original-surface.txt'
    plan = {'schema': SCHEMA, 'attempt_id': attempt_id, 'target': target,
            'config_path': str(Path(config_path).resolve()),
            'original_claim_sha256': observation['originals'][f'initial-argv-{index}.json']['sha256'],
            'original_receipt_sha256': observation['originals'][f'surface-{index}.json']['sha256'],
            'trace_path': str(trace), 'trace_sha256': observation['terminal_read']['sha256'],
            'at': time.time(), 'authorization': 'explicit original-slot maintenance after proven pre-exec failure',
            'budgets': {'native_launches': 1, 'terminal_submit': 1, 'surface_create': 0}}
    with (contextlib.nullcontext() if worker_locked else
          core.FileLock(source / 'worker.lock', timeout_sec=0, purpose='original batch recovery handoff')):
        _, claim, receipt, job = validate_plan(config_path, plan)
        if job['slots'][index].get('phase') != 'created' or job['slots'][index].get('argv_claim_sha256'):
            raise RuntimeError('original slot is no longer a failed pre-exec bootstrap')
        if not _original_gone(claim):
            raise RuntimeError('original bootstrap still present or identity unavailable')
        if (type(receipt.get('shell_pid')) is not int or receipt['shell_pid'] <= 1
                or not receipt.get('shell_start')
                or batch_shell_identity(receipt['shell_pid']) != receipt['shell_start']
                or _children(receipt['shell_pid'])):
            raise RuntimeError('original shell is not idle and unchanged')
        config = core.ConfigStore(Path(config_path)).load()
        if not batch.allowed(config, job):
            raise RuntimeError('original batch authorization revoked')
        client = batch._bootstrap_client(config, job)
        tree = client.workspace_tree(target['workspace_id'])
        actual = core.find_main_surface(tree, target['surface_id'])
        if actual['workspace_id'] != target['workspace_id']:
            raise RuntimeError('original surface moved')
        tree_tty = _surface_tty(tree, target['surface_id'])
        device = _shell_tty_device(receipt['shell_pid'], receipt['shell_start'])
        tty = tree_tty or _tty_for_device(device)
        if device != os.stat('/dev/' + tty).st_rdev:
            raise RuntimeError('original shell tty no longer matches exact surface')
        plan['original_shell_tty'] = tty
        plan['surface_tree_tty_present'] = tree_tty is not None
        if os.path.lexists(source / f'recovery-active-{index}.json'):
            raise RuntimeError('a recovery attempt is already consumed; inspect it without replay')
        directory.mkdir(parents=True, exist_ok=False)
        # Keep the frozen traceback with the attempt, so later verification
        # cleanup cannot remove this recovery controller's original evidence.
        raw_trace = _read_exact(trace, plan['trace_sha256'])
        trace_copy = directory / 'ORIGINAL_TRACE.txt'
        with trace_copy.open('xb') as handle:
            handle.write(raw_trace)
            handle.flush()
            os.fsync(handle.fileno())
        plan['trace_path'] = str(trace_copy)
        _, raw = batch._write_once_json(directory / 'PLAN.json', plan)
        batch._write_once_json(source / f'recovery-active-{index}.json', {
            'schema': SCHEMA, 'attempt_id': attempt_id, 'plan_sha256': _sha(raw)})
    return {'attempt_directory': str(directory), 'command_argv': [sys.executable, '-B',
        str(Path(batch.__file__).resolve()), 'recover-initial', '--config', str(config_path),
        '--job', job_id, '--index', str(index), '--launch-id', target['launch_id']],
        'input_sent': False, 'restored': False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--observation', required=True, type=Path)
    parser.add_argument('--attempt-id', required=True)
    parser.add_argument('--handoff', action='store_true')
    args = parser.parse_args()
    if args.handoff:
        observed = json.loads(args.observation.read_bytes())
        with maintenance(args.config, observed['job_id'], args.attempt_id):
            result = prepare(args.config, args.observation, args.attempt_id, worker_locked=True)
    else:
        result = prepare(args.config, args.observation, args.attempt_id)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()

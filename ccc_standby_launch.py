"""No-prompt native launch policy, distinct from historical argv-first-task.

The preparation manager must provide a current effective-generation reader.
This module alone does not publish readiness or create a workspace cohort.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import time

import cmux_codex_watch as core
import ccc_workspace_batch as batch
import ccc_standby_environment as native_environment
from ccc_native_standby import POLICY, COUNT, controller_identifier, generation, identifier, write_once, digest


def policy(job, config_path):
    from ccc_private_check import POLICY as CHECK_POLICY
    if (job.get('standby_policy') != POLICY or job.get('standby_mode') not in ('b', 'N')
            or job.get('initial_prompt_policy') is not None
            or job.get('initial_prompt') != batch.PROMPT
            or job.get('cwd_policy') != batch.EMPTY_CWD_POLICY
            or job.get('check_retry_policy') != CHECK_POLICY
            or job.get('native_runtime_policy') != batch.NATIVE_RUNTIME_POLICY
            or job.get('name_policy') is not None or job.get('guard_version') is not None
            or job.get('native_trace_policy') is not None
            or batch.startup_mode(job, config_path) != 'private_check'
            or len(job.get('slots', [])) != COUNT
            or [s.get('index') for s in job['slots']] != list(range(COUNT))
            or (job['standby_mode'] == 'N') != (job.get('native_access_policy') == batch.NATIVE_ACCESS_POLICY)):
        raise ValueError('invalid explicit native standby job policy')
    return {'policy': POLICY, 'job_id': identifier(job['id']),
            'cohort_id': identifier(job['standby_cohort_id']),
            'workspace_id': controller_identifier(job['workspace_id']), 'mode': job['standby_mode'],
            'boot_id': identifier(job['standby_boot_id']),
            'generation': generation(job['standby_generation'])}


def launch_argv(config_path, job, index):
    policy(job, config_path)
    if type(index) is not int or not 0 <= index < COUNT:
        raise ValueError('invalid standby launch slot')
    identifier(job['slots'][index]['launch_id'])
    # This base has no initial_prompt_policy, so it cannot append a prompt or
    # install the argv-first-task Hook. Keep the native cwd/trust/SQLite policy.
    if 'standby_target' in job:
        from ccc_standby_target import slot_argv
        argv = batch.native_launch_argv(config_path, job, index,
                                      native_argv=slot_argv(job['standby_target'], index))
    else:
        argv = batch.native_launch_argv(config_path, job, index)
    command = shlex.join([sys.executable, '-B', str(Path(__file__).resolve()), 'bind',
        '--config', str(config_path), '--job', job['id'], '--index', str(index),
        '--launch-id', job['slots'][index]['launch_id']])
    # Preserve the selected native skill features, including explicit CLI
    # enable/disable choices. Readiness must observe the actual configuration.
    argv.extend(batch.initial_hook_arguments(command))
    return argv  # No positional prompt, resume/fork, naming or probe request.


def claim_path(config_path, job_id, index):
    if type(index) is not int or not 0 <= index < COUNT:
        raise ValueError('invalid standby claim slot')
    return batch.job_path(config_path, job_id).parent / f'standby-native-{index}.json'


def exec_claimed(config_path, job_id, index, record, argv, final_guard, final_authorized, *, environment):
    if (record.get('policy') != POLICY or record.get('job_id') != job_id
            or record.get('index') != index or record.get('argv') != argv
            or not argv or not Path(argv[0]).is_absolute()
            or any(not isinstance(v, str) or '\0' in v for v in argv)):
        raise ValueError('standby claim and argv disagree')
    environment = dict(environment)
    if native_environment.signature(environment) != record.get('environment_sha256'):
        raise ValueError('standby exec environment differs from claim')
    argv = list(argv)
    path = claim_path(config_path, job_id, index)
    raw = write_once(path, record)
    original_generation = batch._file_generation(path)
    if not final_guard():
        raise ValueError('standby launch authorization changed after claim')
    if (path.is_symlink() or batch._file_generation(path) != original_generation
            or path.read_bytes() != raw or batch._file_generation(path) != original_generation):
        raise ValueError('standby launch claim changed')
    if not final_authorized():
        raise ValueError('standby launch authorization changed at exec')
    # --cd selects native's logical project, but does not bind the OS cwd.
    # Enter the declared original directory before exec; retain both final
    # guards after the change. This runs only in the one-shot bootstrap.
    previous = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
    target = None
    try:
        target = os.open(record['cwd'], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(target)
        if [info.st_dev, info.st_ino] != record['cwd_generation'][:2]:
            raise ValueError('standby original working directory changed')
        os.fchdir(target)
        if not final_guard() or not final_authorized():
            raise ValueError('standby launch authorization changed after entering cwd')
        here = os.stat('.')
        if ([here.st_dev, here.st_ino] != record['cwd_generation'][:2]
                or Path.cwd() != Path(record['cwd']).resolve(strict=True)):
            raise ValueError('standby OS working directory changed before exec')
        os.execve(argv[0], argv, environment)
    finally:
        # Real exec never returns. Failed or mocked exec restores its caller.
        os.fchdir(previous)
        os.close(previous)
        if target is not None:
            os.close(target)


def launch_registered(config_path, job_id, index, launch_id, *, generation_current, environment_current):
    """Retain register()'s durable initial hold, then exec one original native.

    No default generation provider: the manager must cover effective profile,
    skills/rules, environment, binary and runtime and reject a stale generation.
    """
    from ccc_guard_scope import birth
    from ccc_batch_timing import boot_id
    path = batch.job_path(config_path, job_id)
    job = core.load_json(path, {})
    selected = policy(job, config_path)
    if not callable(environment_current):
        raise ValueError('explicit target environment reader required')
    target_environment = native_environment.template(environment_current())
    target_sha256 = native_environment.signature(target_environment)
    if job.get('standby_environment_sha256') != target_sha256:
        raise ValueError('standby target environment differs from admitted job')
    if not callable(generation_current) or generation_current() != selected['generation']:
        raise ValueError('standby effective configuration is not current')
    if boot_id() != selected['boot_id']:
        raise ValueError('standby boot changed')
    batch.register(config_path, job_id, index, launch_id)
    job = core.load_json(path, {})
    if policy(job, config_path) != selected:
        raise ValueError('standby policy changed during registration')
    argv = launch_argv(config_path, job, index)
    receipt_path = path.parent / f'surface-{index}.json'
    raw = receipt_path.read_bytes()
    receipt = json.loads(raw)
    sid, wid = os.environ.get('CMUX_SURFACE_ID'), os.environ.get('CMUX_WORKSPACE_ID')
    if (receipt.get('surface_id') != sid or receipt.get('workspace_id') != wid
            or wid != job['workspace_id'] or receipt.get('launch_id') != launch_id):
        raise ValueError('standby registration identity changed')
    pid = os.getpid()
    born = birth(pid)
    if born is None:
        raise ValueError('standby bootstrap birth unavailable')
    cwd = batch.working_directory(config_path, job_id, index)
    executable = Path(argv[0]).resolve(strict=True)
    files = {p: batch._file_generation(p) for p in (receipt_path, cwd, executable)}
    client = batch._bootstrap_client(core.ConfigStore(Path(config_path)).load(), job)
    tui = path.parent / f'standby-native-events-{index}.jsonl'
    with os.fdopen(os.open(tui, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as handle:
        os.fsync(handle.fileno())
    info = tui.stat()
    surface_environment = {k: os.environ[k] for k in native_environment.SURFACE_KEYS if k in os.environ}
    environment = native_environment.compose(target_environment, surface_environment,
        workspace_id=wid, surface_id=sid, cwd=cwd, tui_log=tui)
    record = {**selected, 'job_id': job_id, 'workspace_id': wid, 'surface_id': sid,
              'index': index, 'launch_id': launch_id, 'bootstrap_pid': pid, 'bootstrap_birth': born,
              'receipt_sha256': hashlib.sha256(raw).hexdigest(), 'cwd': str(cwd),
              'cwd_generation': list(files[cwd]), 'argv': argv, 'executable': str(executable),
              'executable_generation': list(files[executable]), 'at': time.time(), 'state': 'exec_intent',
              'tui_log': str(tui), 'tui_log_identity': [info.st_dev, info.st_ino],
              'requested_environment': {k: environment[k] for k in native_environment.FIXED_KEYS},
              'target_environment_sha256': target_sha256,
              'environment_sha256': native_environment.signature(environment)}

    def permission_current():
        current = core.load_json(path, {})
        if (policy(current, config_path) != selected or launch_argv(config_path, current, index) != argv
                or current['slots'][index].get('launch_id') != launch_id
                or current['slots'][index].get('surface_id') not in (None, sid)
                or any(p.is_symlink() or batch._file_generation(p) != value for p, value in files.items())
                or receipt_path.read_bytes() != raw or any(cwd.iterdir())
                or Path(argv[0]).resolve(strict=True) != executable or birth(pid) != born
                or {k: os.environ[k] for k in native_environment.SURFACE_KEYS if k in os.environ} != surface_environment
                or native_environment.template(environment_current()) != target_environment
                or current.get('standby_environment_sha256') != target_sha256
                or boot_id() != selected['boot_id'] or generation_current() != selected['generation']):
            return False
        config = core.ConfigStore(Path(config_path)).load()
        if job.get('bootstrap_endpoint') is not None and not batch._bootstrap_endpoint_current(job['bootstrap_endpoint'], config):
            return False
        rule = core.workspace_rule_by_id(config, wid)
        hold = core.batch_start_hold(rule, sid)
        return (batch.allowed(config, current) and hold.get('job_id') == job_id and hold.get('index') == index
                and not (sid in rule.get('excluded_surface_ids', [])
                         and rule.get('excluded_surface_reasons', {}).get(sid) != f'batch:{job_id}:initial')
                and not any(t.get('surface_id') == sid and (t.get('paused') or not t.get('enabled', True))
                            for t in config['targets']))

    membership_seen = False
    membership_deadline = None

    def authorized():
        nonlocal membership_seen, membership_deadline
        # A create ACK can precede visibility in the controller's tree. Wait
        # only for this original surface's first observation, never recreate
        # it or treat absence as authorization. After visibility, disappearance
        # is a revocation and the final exec guard must reject immediately.
        if membership_deadline is None:
            membership_deadline = time.monotonic() + 5.0
        while True:
            if not permission_current():
                return False
            tree = client.workspace_tree(wid)  # RPC errors are not retried.
            records = core.main_surface_records(tree)
            matches = [row for row in records if row['surface_id'] == sid]
            if matches:
                if len(matches) != 1 or matches[0]['workspace_id'] != wid:
                    return False
                if not membership_seen and time.monotonic() >= membership_deadline:
                    raise ValueError('standby initial surface visibility deadline exceeded')
                membership_seen = True
                return True
            if membership_seen:
                raise core.CmuxError(f'main-area surface not found: {sid}')
            remaining = membership_deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('standby initial surface visibility deadline exceeded')
            time.sleep(min(0.05, remaining))

    with core.workspace_input_lock(config_path, wid, shared=True):
        exec_claimed(config_path, job_id, index, record, argv, authorized, permission_current,
                     environment=environment)


def bind_initial(config_path, job_id, index, launch_id, payload):
    """Join a postactivation Hook to the session pinned before activation."""
    from ccc_guard_scope import process, birth
    from ccc_codex_queue import process_writable_files
    from ccc_batch_timing import boot_id
    jobfile = batch.job_path(config_path, job_id)
    selected = policy(core.load_json(jobfile, {}), config_path)
    tracked = {}

    def read(path):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('standby activation evidence must be private regular files')
        before = batch._file_generation(path)
        raw = path.read_bytes()
        if batch._file_generation(path) != before:
            raise ValueError('standby activation evidence changed during read')
        tracked[path] = (before, raw)
        return json.loads(raw), raw

    native_claim, claim_bytes = read(claim_path(config_path, job_id, index))
    if (native_claim.get('policy') != POLICY or native_claim.get('job_id') != job_id
            or native_claim.get('index') != index or native_claim.get('launch_id') != launch_id
            or payload.get('hook_event_name') != 'SessionStart' or payload.get('source') != 'startup'):
        raise ValueError('not the original standby startup Hook')
    # Even an invalid first session or activation consumes this callback.
    write_once(jobfile.parent / f'standby-hook-attempt-{index}.json', {
        'claim_sha256': hashlib.sha256(claim_bytes).hexdigest(), 'at': time.time()})
    directory = jobfile.parent / 'standby'
    directory_info = directory.lstat()
    if (not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != os.geteuid()
            or stat.S_IMODE(directory_info.st_mode) & 0o077):
        raise ValueError('standby activation directory must be private and owned')
    directory_identity = [directory_info.st_dev, directory_info.st_ino]
    manifest, _ = read(directory / 'cohort.json')
    activation, activation_bytes = read(directory / 'activation.json')
    attempt, _ = read(directory / 'activation-attempt.json')
    originals, _ = read(directory / 'originals.json')
    delivery, _ = read(directory / f'input-{index}.json')
    if (directory.is_symlink() or os.path.lexists(directory / 'invalidated.json')
            or any(activation.get(k) != selected[k] for k in
                   ('policy', 'cohort_id', 'boot_id', 'mode', 'generation'))
            or controller_identifier(activation['workspace_id']) != selected['workspace_id']
            or activation.get('count') != COUNT or manifest.get('count') != COUNT
            or any(activation.get(k) != v for k, v in manifest.items())
            or activation['originals'] != originals or len(originals) != COUNT
            or [r['index'] for r in originals] != list(range(COUNT))
            or attempt.get('activation_sha256') != digest(activation)
            or attempt.get('action_id') != activation['action_id']
            or delivery.get('action_id') != activation['action_id']
            or delivery.get('activation_sha256') != hashlib.sha256(activation_bytes).hexdigest()
            or delivery.get('prompt') != batch.PROMPT or activation.get('prompt') != batch.PROMPT
            or boot_id() != selected['boot_id']):
        raise ValueError('standby activation not bound to this cohort')
    row = originals[index]
    identifier(delivery['input_id'])
    if (delivery.get('original') != row or row['index'] != index or row['launch_id'] != launch_id
            or row['claim_sha256'] != hashlib.sha256(claim_bytes).hexdigest()
            or row['argv_sha256'] != hashlib.sha256(json.dumps(native_claim['argv'], separators=(',', ':')).encode()).hexdigest()
            or native_claim.get('policy') != POLICY or native_claim.get('state') != 'exec_intent'
            or native_claim.get('job_id') != job_id or native_claim.get('index') != index
            or native_claim.get('launch_id') != launch_id
            or native_claim.get('bootstrap_pid') != row['pid'] or native_claim.get('bootstrap_birth') != row['birth']
            or any(native_claim.get(k) != selected[k] for k in ('cohort_id', 'boot_id', 'mode', 'generation'))
            or controller_identifier(native_claim['surface_id']) != row['surface_id']
            or controller_identifier(native_claim['workspace_id']) != row['workspace_id']
            or identifier(payload['session_id']) != row['session_id']
            or payload.get('hook_event_name') != 'SessionStart' or payload.get('source') != 'startup'
            or Path(payload['cwd']).resolve() != Path(native_claim['cwd']).resolve()):
        raise ValueError('standby Hook differs from original activation')
    current = process(row['pid'], launch=True)
    if (not current or current.get('birth') != row['birth'] or current.get('remote')
            or ('environment_sha256' in native_claim and
                native_environment.signature(current.get('environment')) != native_claim['environment_sha256'])
            or current.get('argv') != native_claim['argv']
            or controller_identifier(current['surface_id']) != row['surface_id']
            or controller_identifier(current['environment_workspace_id']) != row['workspace_id']):
        raise ValueError('standby Hook process identity changed')
    root = (Path(current['environment'].get('CODEX_HOME') or Path.home() / '.codex') / 'sessions').resolve()
    lock = Path(row['writer_lock'])
    tui = Path(native_claim['tui_log'])
    files = process_writable_files(row['pid'], identities=True)
    for path, expected in ((lock, row['writer_identity']), (tui, native_claim['tui_log_identity'])):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or [info.st_dev, info.st_ino] != expected
                or files.get(path) != dict(zip(('device', 'inode'), expected))):
            raise ValueError('standby Hook original writer changed')
    if lock != root.parent / 'thread-writer-locks' / (row['session_id'] + '.lock'):
        raise ValueError('standby Hook writer outside original native home')
    data, submitted = batch._initial_event_prefix(native_claim)
    if not submitted:
        raise ValueError('standby Hook has no activated first task')
    transcript = payload.get('transcript_path')
    if transcript:
        transcript = str(Path(transcript).resolve())
        if (not Path(transcript).is_relative_to(root)
                or not Path(transcript).name.endswith(row['session_id'] + '.jsonl')):
            raise ValueError('standby Hook transcript differs from original session')
    current_files = process_writable_files(row['pid'], identities=True)
    after_directory = directory.lstat()
    if (process(row['pid'], launch=True) != current or birth(row['pid'], codex=True) != row['birth']
            or current_files.get(lock) != files[lock] or current_files.get(tui) != files[tui]
            or not stat.S_ISDIR(after_directory.st_mode) or after_directory.st_uid != os.geteuid()
            or stat.S_IMODE(after_directory.st_mode) & 0o077
            or [after_directory.st_dev, after_directory.st_ino] != directory_identity
            or any(path.is_symlink() or batch._file_generation(path) != stamp or path.read_bytes() != raw
                   for path, (stamp, raw) in tracked.items())
            or os.path.lexists(directory / 'invalidated.json')):
        raise ValueError('standby activation changed before Hook persistence')
    # scope/process reads above can block. Recheck the original writer paths
    # after they return, not only the FD snapshot taken before those reads.
    for path, expected in ((lock, row['writer_identity']), (tui, native_claim['tui_log_identity'])):
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or [info.st_dev, info.st_ino] != expected
                or current_files.get(path) != dict(zip(('device', 'inode'), expected))):
            raise ValueError('standby writer path changed before Hook persistence')
    write_once(jobfile.parent / f'standby-session-{index}.json', {
        **row, 'job_id': job_id, 'cohort_id': selected['cohort_id'], 'action_id': activation['action_id'],
        'input_id': delivery['input_id'], 'source': 'startup', 'at': time.time(),
        'transcript': transcript, 'sessions_root': str(root),
        'tui_prefix_bytes': len(data), 'tui_prefix_sha256': hashlib.sha256(data).hexdigest()})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['bind'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--index', type=int, required=True)
    parser.add_argument('--launch-id', required=True)
    args = parser.parse_args()
    bind_initial(args.config, args.job, args.index, args.launch_id, json.loads(sys.stdin.read()))


if __name__ == '__main__':
    main()

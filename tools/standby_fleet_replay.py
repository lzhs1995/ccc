"""Replay one completed fleet's original evidence without sending any input.

This joins existing original terminal/performance verifiers. It does not grant
launch authority or replace the genuine UI50 prerequisite at the entrypoint.
"""
import hashlib
import math
from pathlib import Path

from ccc_standby_timing import _elapsed, _read
from ccc_workspace_batch import PROMPT
from tools.standby_fleet_identity import verify as verify_identity
from tools.standby_performance_replay import verify as verify_performance
from tools.standby_process_overlap import validate_topology
from tools.standby_run_manifest import RunManifest
from tools.standby_run_terminal import verify as verify_terminal


def validate_overlap(value, *, plan, plan_sha256, bindings):
    if (value.get('kind') != 'standby_original_process_overlap'
            or value.get('native_live_checks') is not True
            or value.get('process_overlap_proven') is not True
            or value.get('run_id') != plan['run_id']
            or value.get('boot_id') != plan['boot_id']
            or value.get('plan_sha256') != plan_sha256 or value.get('bindings') != bindings):
        raise ValueError('overlap not bound to original run')
    boot = plan['boot_id']
    lower, upper = value['overlap_started'], value['overlap_finished']
    duration = _elapsed(lower, upper, boot)
    if duration <= 0 or duration != value['overlap_seconds']:
        raise ValueError('positive original common process interval required')
    for phase in ('before', 'after'):
        rows = value[phase]
        if (len(rows) != 500 or any(type(r.get('index')) is not int for r in rows)
                or [r['index'] for r in rows] != list(range(500))):
            raise ValueError('complete original live sweep required')
        for row in rows:
            _elapsed(row['started'], row['finished'], boot)
            if phase == 'before':
                _elapsed(row['finished'], lower, boot)
            else:
                _elapsed(upper, row['started'], boot)


def validate_observer(row, baseline, *, action_id, before=False):
    if (not row.get('generation_sha256') or not row.get('route_identity')
            or row['generation_sha256'] != baseline['generation_sha256']
            or row['route_identity'] != baseline['route_identity']):
        raise ValueError('original configuration or route identity changed')
    source = row['source_resources']
    watcher = source['watcher']
    if (source.get('closed') is not False or watcher.get('close_started') is not False
            or watcher.get('queue_closed') is not False or watcher.get('close_errors') != []
            or type(watcher.get('remaining_owned_fds')) is not int
            or watcher['remaining_owned_fds'] <= 0):
        raise ValueError('original source watcher was not live')
    route = row['routes']
    if (route.get('closed') is not False or route.get('failed')
            or type(route.get('unattributed_requests')) is not int
            or route['unattributed_requests'] != 0
            or route.get('action_id') != (None if before else action_id)
            or len(route['slots']) != 50):
        raise ValueError('original route observation invalid')
    for slot in route['slots']:
        if (type(slot.get('before_activation')) is not int or slot['before_activation'] != 0
                or type(slot.get('requests')) is not int or slot['requests'] < 0
                or (before and slot['requests'] != 0)):
            raise ValueError('requests preceded original activation')
    if before and (type(route.get('pending_connections')) is not int or route['pending_connections'] != 0):
        raise ValueError('pending request before original activation')


def validate_resources(value):
    disk = value['disk']
    if (not isinstance(disk.get('root'), str) or not Path(disk['root']).is_absolute()
            or not isinstance(disk.get('root_identity'), list) or len(disk['root_identity']) != 2
            or any(type(n) is not int or n < 0 for n in disk['root_identity'])
            or any(type(disk.get(k)) is not int or disk[k] < 0 for k in
                   ('regular_files', 'logical_bytes', 'allocated_metadata_bytes', 'filesystem_free_bytes'))
            or disk.get('allocation_atomic') is not False
            or disk.get('unique_physical_bytes_proven') is not False):
        raise ValueError('original bounded disk observation required')
    for key, count in (('process_count', 521), ('native_process_count', 500),
                       ('auxiliary_process_count', 21), ('retained_ui_count', 10)):
        if type(value.get(key)) is not int or value[key] != count:
            raise ValueError('complete original resource accounting required')
    rows = value['processes']
    for row in rows:
        generation = row.get('birth')
        if (type(row.get('pid')) is not int or row['pid'] <= 1
                or not isinstance(generation, list) or len(generation) != 2
                or any(type(n) is not int for n in generation)
                or generation[0] <= 0 or not 0 <= generation[1] < 1000000):
            raise ValueError('original resource process generation required')
    if (len(rows) != 521 or len({r['pid'] for r in rows}) != 521
            or sum(r['role'] == 'native' for r in rows) != 500
            or sum(r['role'] == 'auxiliary' for r in rows) != 21):
        raise ValueError('resource roles or unique processes incomplete')
    for key, total in (('rss_bytes', 'rss_sum_bytes'), ('fd_count', 'fd_sum')):
        if (any(type(r.get(key)) is not int or r[key] < 0 for r in rows)
                or type(value.get(total)) is not int or value[total] != sum(r[key] for r in rows)):
            raise ValueError('resource totals differ from original measurements')
    if (any(type(value.get(k)) not in (int, float) or not math.isfinite(value[k])
            or value[k] < 0 for k in ('started_monotonic', 'finished_monotonic'))
            or value['finished_monotonic'] < value['started_monotonic']):
        raise ValueError('resource observation clock reversed')


def recheck_dependencies(proofs, *, check=lambda: None):
    """Recheck every cohort's consumed bytes after the last cohort replay."""
    hashes = {}
    for proof in proofs:
        for path, sha in proof['evidence_sha256'].items():
            if path in hashes and hashes[path] != sha:
                raise ValueError('conflicting cohort evidence hashes')
            hashes[path] = sha
    for name, expected in hashes.items():
        check()
        path = Path(name)
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise ValueError('canonical replay dependency required')
        with path.open('rb') as stream:
            raw = stream.read(32*1024*1024+1)
        if len(raw) > 32*1024*1024 or hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('cohort evidence changed after replay')
        check()
    return hashes


def verify(output, run_directory, *, check=lambda: None):
    output = Path(output)
    pinned = {}

    def read(path):
        check()
        path = Path(path)
        value, raw = _read(path)
        if path in pinned and pinned[path] != raw:
            raise ValueError('fleet evidence changed during replay')
        pinned[path] = raw
        check()
        return value

    state = read(output/'execution-state.json')
    if (state.get('lifecycle_completed') is not True or state.get('scoped_cleanup_complete') is not True
            or state.get('cleanup_errors') != [] or state.get('source_unchanged') is not True
            or any(k in state for k in ('error', 'terminal_error'))):
        raise ValueError('original execution or cleanup incomplete')
    baseline = read(output/'module-origin-baseline.json')
    source = Path(baseline['source'])
    hashes = baseline['python_sources']
    required = set(source.glob('*.py')) | set((source/'tools').glob('*.py'))
    if not required or not {str(p) for p in required}.issubset(hashes):
        raise ValueError('complete original source freeze required')

    def sources_current():
        check()
        if set(source.glob('*.py')) | set((source/'tools').glob('*.py')) != required:
            raise ValueError('source set changed during fleet replay')
        for path, sha in hashes.items():
            p = Path(path)
            if (not p.is_absolute() or p.resolve(strict=True) != p
                    or hashlib.sha256(p.read_bytes()).hexdigest() != sha):
                raise ValueError('tested source changed')
        check()

    sources_current()
    manifest = RunManifest(run_directory)
    validate_topology(manifest.plan)
    # Only replay an already resolved run; do not create new bindings here.
    read(Path(run_directory)/'run-bindings.json')
    bindings = manifest.resolve()
    terminal_proof = verify_terminal(run_directory)
    if (terminal_proof['succeeded'] is not True or terminal_proof['run_terminal'] is not True
            or terminal_proof != state.get('run_verification')):
        raise ValueError('original terminal not successful or changed')
    terminal = read(terminal_proof['terminal_path'])
    overlap = read(output/'overlap/process-overlap.json')
    validate_overlap(overlap, plan=manifest.plan, plan_sha256=manifest.sha256, bindings=bindings)
    settled = read(output/'observer-bindings-settled.json')
    completed = read(output/'observer-bindings-completed.json')
    resources = read(output/'original-process-resources.json')
    validate_resources(resources)
    if resources['disk']['root'] != read(output/'root.json')['path']:
        raise ValueError('disk observation differs from original private root')
    metrics, witnesses, performance = {}, {}, {}
    terminal_rows = {r['batch_id']: r for r in terminal['batches']}
    for item in bindings['batches']:
        check()
        binding = item['binding']
        key = binding['batch_id']
        ui = read(binding['activation_ui_path'])
        witnesses[key] = {r['index']: r for r in ui['originals']}
        job_terminal = read(terminal_rows[key]['terminal_path'])
        completion = read(job_terminal['completion']['observation_path'])
        if (completion['action_id'] != binding['action_id'] or completion['failed_rounds'] != 1
                or [r['original'] for r in completion['slots']] != ui['originals']):
            raise ValueError('terminal completion differs from original performance cohort')
        result = metrics[key] = read(output/key/'performance.json')
        expected_paths = {
            binding['activation_ui_path'],
            str(Path(binding['activation_ui_path']).with_name('activation-terminal.json')),
            str(output/key/'rpc-responses.ndjson')}
        if set(result['evidence_sha256']) != expected_paths:
            raise ValueError('performance timing not bound to original run')
        indices = [b['index'] for b in result['transcript_bindings']]
        if (any(type(i) is not int for i in indices) or sorted(indices) != list(range(50))):
            raise ValueError('exact original performance indices required')
        if (len(result['transcript_bindings']) != 50 or any(
                b['original'] != completion['slots'][b['index']]['transcript']
                or b['sha256'] != completion['slots'][b['index']]['transcript_sha256']
                for b in result['transcript_bindings'])):
            raise ValueError('performance transcript differs from terminal completion')
        replay = verify_performance(result, witnesses=witnesses[key],
            deliveries_path=output/key/'original-deliveries.json', prompt=PROMPT, check=check)
        if (replay['startup_passed'] is not True or replay['recovery_passed'] is not True
                or result.get('passed') is not True or result.get('zero_requests_before_activation') is not True):
            raise ValueError('original startup or recovery performance failed')
        performance[key] = replay
        observer = read(output/('observer-baseline-'+key+'.json'))
        validate_observer(observer, observer, action_id=binding['action_id'], before=True)
        for phase in (settled, completed):
            validate_observer(phase['cohorts'][key], observer, action_id=binding['action_id'])
        route = result['route']
        validate_observer(dict(completed['cohorts'][key], routes=route), observer,
                          action_id=binding['action_id'])
        if type(route.get('pending_connections')) is not int or route['pending_connections'] != 0:
            raise ValueError('performance route has pending connections')
    joined = verify_identity(witnesses=witnesses, metrics=metrics, settled=settled,
        completed=completed, overlap=overlap, resources=resources, check=check)
    if verify_terminal(run_directory) != terminal_proof or manifest.resolve() != bindings:
        raise ValueError('terminal or run changed during aggregate replay')
    manifest.current()
    for path, raw in list(pinned.items()):
        if read(path) is None or pinned[path] != raw:
            raise ValueError('fleet evidence changed')
    sources_current()
    dependencies = recheck_dependencies(performance.values(), check=check)
    return dict(evidence_replayed=True, run_id=manifest.plan['run_id'],
        terminal=terminal_proof, identity_join=joined, performance=performance,
        performance_dependency_sha256=dependencies,
        evidence_sha256={str(p): hashlib.sha256(raw).hexdigest() for p, raw in pinned.items()},
        full_500_acceptance=False,
        scope='Original fleet replay; launch prerequisite and final entry acceptance remain separate. '
              'Sequential observations, not OS-atomic continuity or resource peaks.')

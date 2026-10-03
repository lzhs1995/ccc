"""Read partial preparation originals without inferring process absence.

This is an inventory, never a cleanup certificate or authority to send input.
Use after owner closure; missing intent alone does not establish no side effects.
"""
import os
from pathlib import Path

import ccc_standby_launch as launch
import ccc_workspace_batch as batch
from ccc_native_standby import identifier
from ccc_standby_timing import _read, _sha
from tools.standby_run_manifest import directory_identity


def inventory(config_path, job_id, directory):
    directory = Path(directory)
    identity = directory_identity(directory)
    job_path = batch.job_path(Path(config_path), job_id)
    job, raw = _read(job_path)
    selected = launch.policy(job, config_path)
    if selected['job_id'] != identifier(job_id):
        raise ValueError('preparation inventory job mismatch')
    pinned = [(job_path, raw)]
    absent, rows, surfaces, processes = [], [], set(), set()
    expected_names = {f'create-{kind}-{index}.json'
                      for index in range(len(job['slots'])) for kind in ('intent', 'ack')}
    def names():
        return {p.name for p in directory.glob('create-*.json')}
    original_names = names()
    if not original_names <= expected_names:
        raise ValueError('unexpected preparation creation record')
    for index, slot in enumerate(job['slots']):
        records = {}
        paths = {'intent': directory / f'create-intent-{index}.json',
                 'ack': directory / f'create-ack-{index}.json',
                 'claim': launch.claim_path(config_path, job_id, index),
                 'failure': directory / f'bootstrap-failure-{index}.json'}
        for kind, path in paths.items():
            if not os.path.lexists(path):
                absent.append(path)
                continue
            value, data = _read(path)
            if (type(value.get('index')) is not int or value['index'] != index
                    or value.get('launch_id') != slot['launch_id']):
                raise ValueError('preparation slot identity mismatch')
            records[kind] = value
            pinned.append((path, data))
        intent, ack, claim = (records.get(k) for k in ('intent', 'ack', 'claim'))
        failure = records.get('failure')
        if (ack or claim or failure) and not intent:
            raise ValueError('preparation original intent missing')
        if intent and any(intent.get(k) != v for k, v in selected.items()):
            raise ValueError('preparation intent job binding mismatch')
        surface = None
        if ack:
            surface = identifier(ack['surface_id'])
            if ack.get('workspace_id') != selected['workspace_id'] or surface in surfaces:
                raise ValueError('duplicate or foreign preparation surface')
        process = None
        process_source = None
        if claim:
            if (any(claim.get(k) != selected[k] for k in
                    ('job_id', 'workspace_id', 'cohort_id', 'generation', 'boot_id', 'mode'))
                    or claim.get('argv') != launch.launch_argv(config_path, job, index)
                    or (ack and claim.get('surface_id') != ack['surface_id'])):
                raise ValueError('preparation claim binding mismatch')
            surface = identifier(claim['surface_id'])
            pid, birth = claim.get('bootstrap_pid'), claim.get('bootstrap_birth')
            if (type(pid) is not int or pid <= 0 or not isinstance(birth, list)
                    or len(birth) != 2 or any(type(n) is not int or n < 0 for n in birth)):
                raise ValueError('preparation claim process identity invalid')
            process = (pid, *birth)
            process_source = 'claim'
        if failure and failure.get('bootstrap_identity') is not None:
            spec_path = directory / 'bootstrap.json'
            spec, spec_raw = _read(spec_path)
            pinned.append((spec_path, spec_raw))
            observed = failure['bootstrap_identity']
            if (failure.get('kind') != 'bootstrap_launch_failure'
                    or any(failure.get(k) != v or spec.get(k) != v for k, v in selected.items())
                    or spec.get('kind') != 'standby_live_bootstrap' or spec.get('schema') != 1
                    or spec.get('config_path') != str(Path(config_path).resolve())
                    or spec.get('launch_ids') != [s['launch_id'] for s in job['slots']]
                    or failure.get('spec_sha256') != _sha(spec_raw)
                    or not isinstance(observed, dict)):
                raise ValueError('preparation failure original binding mismatch')
            pid, born = observed.get('pid'), observed.get('birth_before')
            valid = (type(pid) is int and pid > 0 and failure.get('pid') == pid
                     and isinstance(born, list) and len(born) == 2
                     and all(type(n) is int and n >= 0 for n in born)
                     and born[0] > 0 and born[1] < 1000000
                     and observed.get('birth_after') == born
                     and ack is not None
                     and observed.get('surface_id') == ack['surface_id']
                     and observed.get('surface_id_after') == ack['surface_id']
                     and observed.get('workspace_id') == selected['workspace_id']
                     and observed.get('workspace_id_after') == selected['workspace_id'])
            if valid:
                failed_process = (pid, *born)
                if process is not None and process != failed_process:
                    raise ValueError('preparation claim and failure process differ')
                if process is None:
                    process = failed_process
                    process_source = 'bootstrap_failure'
        if process is not None:
            if process in processes:
                raise ValueError('duplicate preparation process identity')
            processes.add(process)
        if surface is not None:
            if surface in surfaces:
                raise ValueError('duplicate preparation surface identity')
            surfaces.add(surface)
        state = ('unrecorded' if not intent else 'unknown_ack' if not ack
                 else 'unknown_process' if process is None else 'identified')
        rows.append({'index': index, 'launch_id': slot['launch_id'], 'state': state,
                     'surface_id': ack['surface_id'] if ack else None,
                     'claim_surface_id': claim['surface_id'] if claim else None,
                     'process_identity': list(process) if process else None})
        if process_source == 'bootstrap_failure':
            rows[-1]['process_identity_source'] = process_source
    if (directory_identity(directory) != identity or names() != original_names
            or any(os.path.lexists(p) for p in absent)
            or any(_read(p)[1] != b for p, b in pinned)):
        raise ValueError('preparation originals changed during inventory')
    return {'job_id': selected['job_id'], 'rows': rows,
            'originals': {str(p): _sha(b) for p, b in pinned},
            'unresolved_slots': [r['index'] for r in rows if r['state'] != 'identified'],
            'cleanup_proven': False}

"""Settle initial submission without closing the original continuation owner.

This receipt permits subsequent admission, not a claim of run completion or
latency. It requires the original fifty task receipts and released input holds.
"""
from __future__ import annotations

import copy
from pathlib import Path

import cmux_codex_watch as core
import ccc_workspace_batch as batch
from ccc_native_standby import write_once
from ccc_standby_timing import ActivationTiming, _read, _sha
from ccc_batch_timing import boot_id


def _record(timing):
    # finish revalidates all original task/transcript receipts, even when an
    # existing terminal is returned. Never mint a terminal on this path.
    terminal, raw = _read(timing.terminal)
    if terminal.get('outcome') != 'complete':
        raise ValueError('only complete original submission may settle')
    if timing.finish(outcome='complete', persist=False) != terminal:
        raise ValueError('original activation terminal changed')
    if _read(timing.terminal)[1] != raw:
        raise ValueError('activation terminal changed during settlement')
    return {'version': 1, 'kind': 'standby_submission_settled',
        **timing.selected, 'action_id': timing.origin['action_id'],
        'job_sha256': _sha(timing._job_raw),
        'activation_ui_sha256': _sha(timing._receipt_raw),
        'activation_terminal_sha256': _sha(raw),
        'job_terminal': False, 'run_terminal': False}


def settle(timing, *, authorized):
    """Called after all send workers and first-task releases have returned."""
    if not callable(authorized):
        raise ValueError('live settlement authorization required')
    with core.workspace_input_lock(timing.config_path, timing.selected['workspace_id'], shared=True):
        record = _record(timing)
        config = core.ConfigStore(timing.config_path).load()
        if authorized() is not True or not batch.allowed(config, timing.job):
            raise ValueError('submission settlement no longer authorized')
        rule = core.workspace_rule_by_id(config, timing.selected['workspace_id'])
        for row in _read(timing.receipt)[0]['originals']:
            sid = row['surface_id']
            if (sid in rule.get('batch_start_holds', {})
                    or sid in rule.get('excluded_surface_ids', [])
                    or any(t.get('surface_id') == sid and
                        (t.get('paused') or not t.get('enabled', True))
                        for t in config.get('targets', []))):
                raise ValueError('original initial input hold remains or was revoked')
        if authorized() is not True or _record(timing) != record:
            raise ValueError('submission changed before settlement')
        path = timing.directory / 'submission-settled.json'
        if path.exists():
            if _read(path)[0] != record:
                raise ValueError('original settlement differs')
        else:
            write_once(path, record)
        return copy.deepcopy(record)


def read_settlement(config_path, job_id):
    """Validate persisted settlement and all of its original evidence anew."""
    config_path = Path(config_path).resolve(strict=True)
    path = batch.job_path(config_path, job_id).parent / 'standby'
    saved, saved_raw = _read(path / 'submission-settled.json')
    receipt, raw = _read(path / 'activation-ui.json')
    timing = ActivationTiming(config_path, job_id, receipt['origin'],
        current_hashes=lambda: receipt['origin']['source_hashes'])
    timing._receipt_raw = raw
    # Use the original terminal clock for verification, without replacing it.
    terminal, _ = _read(timing.terminal)
    timing.clock = lambda: copy.deepcopy(terminal['event'])
    if _record(timing) != saved or _read(path / 'submission-settled.json')[1] != saved_raw:
        raise ValueError('original submission settlement changed')
    return saved, _sha(saved_raw)


def live_settlement(config_path, job_id):
    """Require the original owner to remain alive and retain its routes."""
    from ccc_standby_service import request
    config_path = Path(config_path).resolve(strict=True)
    saved, sha = read_settlement(config_path, job_id)
    if saved.get('boot_id') != boot_id():
        raise ValueError('settled owner belongs to another boot')
    root = batch.job_path(config_path, job_id).parent / 'standby'
    binding, binding_raw = _read(root / 'owner.json')
    spec_path = Path(binding['spec_path'])
    spec, spec_raw = _read(spec_path)
    keys = ('job_id', 'cohort_id', 'workspace_id', 'mode', 'boot_id', 'generation', 'policy')
    if (binding.get('kind') != 'standby_owner_binding'
            or spec.get('kind') != 'standby_live_owner'
            or binding.get('spec_sha256') != _sha(spec_raw)
            or any(binding.get(k) != saved[k] or spec.get(k) != saved[k] for k in keys)):
        raise ValueError('settled owner no longer belongs to original job')
    state = request(spec_path, _sha(spec_raw), 'status')
    if (state.get('state') != 'first_tasks_observed'
            or saved.get('boot_id') != boot_id()
            or state.get('submission_settlement') != saved
            or state.get('job_terminal') is not False or state.get('run_terminal') is not False
            or any(state.get(k) != saved[k] for k in keys)
            or _read(root / 'owner.json')[1] != binding_raw
            or _read(spec_path)[1] != spec_raw
            or read_settlement(config_path, job_id) != (saved, sha)):
        raise ValueError('original settled owner unavailable or changed')
    return saved, sha

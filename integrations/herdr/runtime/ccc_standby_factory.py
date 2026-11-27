"""Admit and assemble one live standby owner under the workspace locks.

Source capture and native readiness observation are required dependencies.
This factory neither invents ready proofs nor restores a consumed owner. The
returned owner creates natives only when its explicit prepare() is called.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import uuid

import cmux_codex_watch as core
import ccc_workspace_batch as batch
from ccc_batch_timing import boot_id
from ccc_native_standby import COUNT, POLICY, StandbyLedger, controller_identifier, generation, identifier, write_once
from ccc_private_check import POLICY as CHECK_POLICY
from ccc_standby_activation import ActivationOwner
from ccc_standby_launch import policy
from ccc_standby_prepare import PreparationOwner
from ccc_standby_service import CohortService, ServiceEndpoint


def _private(path, *, container=False):
    info = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & (0o022 if container else 0o077)):
        raise ValueError('standby admission requires a canonical private directory')
    return info.st_dev, info.st_ino


def _permission(config, wid):
    rule = core.workspace_rule_by_id(config, wid)
    if (config.get('mode') != 'armed' or config.get('global_paused')
            or not rule.get('enabled', True) or rule.get('paused')):
        raise ValueError('workspace is not authorized for standby preparation')
    return rule


class AdmittedOwner:
    """Keep the live endpoint and service together; closing never deletes natives."""
    def __init__(self, service, endpoint):
        self.service, self.endpoint = service, endpoint

    def prepare(self):
        self.endpoint._check()
        return self.service.prepare()

    def status(self):
        self.endpoint._check()
        return self.service.status()

    def close(self):
        try:
            self.endpoint.close()
        finally:
            self.service.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def admit(config_path, workspace_id, *, mode, client, capture_sources, sessions_root,
          target_environment, native_target=None, activation_committed=None):
    """Persist a fresh job, authorize it, and publish its single live owner.

    capture_sources(draft_job, environment=...) runs after empty cwd creation and before the
    immutable job write. It returns (live_source_pin, live_readiness_reader).
    The draft's zero generation is only for resolving launch argv. The real
    pin value replaces it before persistence; no native is started here.
    The reader must implement ActivationOwner's actual observation contract.
    A missing reader is an error, never permission to turn /pwd into ready.

    A preceding standby permits admission only after verified submission
    settlement while its original continuation owner remains alive. Its job
    and worker lock remain intact. Ordinary completed jobs require acquiring
    their worker lock before admitting a new cohort.
    """
    if mode not in ('b', 'N') or not callable(capture_sources):
        raise ValueError('explicit b/N mode and a native source capture required')
    from ccc_standby_environment import template, signature
    environment = template(target_environment)
    if native_target is not None:
        from ccc_standby_target import normalize
        native_target = normalize(native_target)
    config_path = Path(config_path).resolve(strict=True)
    wid = controller_identifier(workspace_id)
    sessions_root = Path(sessions_root).resolve(strict=True)
    if not sessions_root.is_dir():
        raise ValueError('native sessions directory unavailable')
    store = core.ConfigStore(config_path)
    root = None
    pin = preparation = activation = service = endpoint = None
    with core.FileLock(config_path.parent / f'batch-start-{wid}.lock', timeout_sec=5), \
            core.workspace_input_lock(config_path, wid, shared=False), contextlib.ExitStack() as locks:
        config = store.load()
        rule = _permission(config, wid)
        baseline = {key: copy.deepcopy(rule.get(key)) for key in
                    ('active_batch_id', 'last_batch_id', 'batch_cancelled_at')}
        from ccc_batch_guard import blocked
        if blocked(config_path, wid):
            raise ValueError('workspace guard refuses standby preparation')
        previous_raw = None
        previous_path = None
        previous_id = baseline['last_batch_id']
        previous_settlement = None
        if baseline['active_batch_id'] not in (None, previous_id):
            raise ValueError('workspace has another active batch')
        if previous_id:
            previous_path = batch.job_path(config_path, identifier(previous_id))
            previous_raw = previous_path.read_bytes()
            previous = json.loads(previous_raw)
            if previous.get('id') != previous_id or previous.get('workspace_id') != wid:
                raise ValueError('original batch must be preserved; no standby replacement')
            if 'standby_policy' in previous:
                from ccc_standby_settlement import live_settlement
                try:
                    previous_settlement = live_settlement(config_path, previous_id)
                except OSError as exc:
                    raise ValueError('original standby submission is not settled') from exc
            else:
                locks.enter_context(core.FileLock(previous_path.parent / 'worker.lock', timeout_sec=0))
                if (previous_path.read_bytes() != previous_raw
                        or previous.get('status') not in {'complete', 'stopped_success'}):
                    raise ValueError('original batch must be preserved; no standby replacement')

        def authorized(latest):
            current = _permission(latest, wid)
            if (any(current.get(k) != v for k, v in baseline.items())
                    or blocked(config_path, wid)
                    or (previous_path is not None and
                        (previous_path.is_symlink() or previous_path.read_bytes() != previous_raw))):
                raise ValueError('workspace admission changed during preparation')
            if previous_settlement is not None:
                if live_settlement(config_path, previous_id) != previous_settlement:
                    raise ValueError('original settled continuation owner changed')
            return current

        # Topology and source capture may block. Recheck authorization after
        # each, including inside the final configuration mutation.
        tree = client.workspace_tree(wid)
        workspace = core.find_workspace(tree, wid)
        if workspace.get('workspace_id') != wid:
            raise ValueError('standby target workspace is absent')
        authorized(store.load())
        job = {'id': str(uuid.uuid4()), 'workspace_id': wid,
            'config_path': str(config_path), 'created_at': time.time(), 'status': 'pending',
            'standby_policy': POLICY, 'standby_mode': mode,
            'standby_cohort_id': str(uuid.uuid4()), 'standby_boot_id': boot_id(),
            'standby_generation': '0' * 64, 'initial_prompt': batch.PROMPT,
            'standby_environment_sha256': signature(environment),
            'cwd_policy': batch.EMPTY_CWD_POLICY, 'check_retry_policy': CHECK_POLICY,
            'native_runtime_policy': batch.NATIVE_RUNTIME_POLICY, 'launch_mode': 'native',
            'slots': [{'index': i, 'phase': 'pending', 'launch_id': str(uuid.uuid4())}
                      for i in range(COUNT)]}
        if mode == 'N':
            job['native_access_policy'] = batch.NATIVE_ACCESS_POLICY
        if native_target is not None:
            job['standby_target'] = copy.deepcopy(native_target)
        bootstrap = batch._bootstrap_endpoint(client, config)
        if bootstrap is not None:
            job['bootstrap_endpoint'] = bootstrap
        jobfile = batch.job_path(config_path, job['id'])
        batches = jobfile.parent.parent
        batches.mkdir(mode=0o700, exist_ok=True)
        # Ordinary batches historically create this shared parent as 0755.
        # Preserve it; only our new per-job directory must be private. Neither
        # location may be a link or writable by another user.
        batches_identity = _private(batches, container=True)
        jobfile.parent.mkdir(mode=0o700)
        root = jobfile.parent
        root_identity = _private(root)
        try:
            for index in range(COUNT):
                batch.prepare_working_directory(config_path, job, index)
            pin, reader = capture_sources(copy.deepcopy(job), environment=dict(environment))
            if not callable(reader) or not callable(getattr(pin, 'current', None)):
                raise ValueError('live source pin and native readiness reader required')
            job['standby_generation'] = generation(pin.current())
            selected = policy(job, config_path)
            if boot_id() != selected['boot_id']:
                raise ValueError('boot changed during standby source capture')
            authorized(store.load())
            if (_private(batches, container=True) != batches_identity
                    or _private(root) != root_identity):
                raise ValueError('original standby admission directory changed')
            job_raw = write_once(jobfile, job)
            ledger = StandbyLedger.create(root / 'standby',
                cohort_id=selected['cohort_id'], workspace_id=wid,
                boot_id=selected['boot_id'], mode=mode, prompt=job['initial_prompt'],
                config_generation=selected['generation'])

            def commit(latest):
                if (pin.current() != selected['generation']
                        or boot_id() != selected['boot_id']
                        or _private(batches, container=True) != batches_identity
                        or _private(root) != root_identity or jobfile.is_symlink()
                        or jobfile.read_bytes() != job_raw):
                    raise ValueError('standby sources or original job changed before admission')
                current = authorized(latest)
                current.update(active_batch_id=job['id'], last_batch_id=job['id'])
            store.mutate(commit)
            # Short private sockets; durable binding remains below the job.
            # Failed/closed owner records are retained, never reconstructed.
            directory = Path(tempfile.mkdtemp(prefix='ccc-owner-', dir='/tmp')).resolve(strict=True)
            preparation = PreparationOwner(config_path, job['id'], directory=directory,
                client=client, source_pin=pin, sessions_root=sessions_root,
                target_environment=environment)
            activation = ActivationOwner(preparation, ledger, readiness_proof=reader)
            service = (CohortService(activation) if activation_committed is None else
                       CohortService(activation, activation_committed=activation_committed))
            endpoint = ServiceEndpoint(service, directory, binding_path=root / 'standby' / 'owner.json')
            preparation._current()
            if not batch.allowed(store.load(), job):
                raise ValueError('standby permission revoked before publishing owner')
            return AdmittedOwner(service, endpoint)
        except BaseException as exc:
            # Preserve the failed attempt and authorization if already
            # committed. Never roll back into an automatic second cohort.
            try:
                if endpoint is not None:
                    endpoint.close()
            finally:
                try:
                    if service is not None:
                        service.close()
                    elif activation is not None:
                        activation.close()
                    elif preparation is not None:
                        preparation.close()
                    elif pin is not None:
                        pin.close()
                finally:
                    with contextlib.suppress(OSError, ValueError):
                        if _private(root) == root_identity:
                            write_once(root / 'admission-failed.json',
                                {'job_id': job['id'], 'error_type': type(exc).__name__,
                                 'created_at': time.time(), 'native_creation_started': False})
            raise

"""Retain ten declared Runner lifetimes through a shared observation window.

The native harness owns preparation, real UI activation, per-batch performance,
and native cleanup. This coordinator never synthesizes UI input or restarts a
Runner. It closes only the owned Runner handles when explicitly requested.
"""
from __future__ import annotations

import copy
import threading
import time
from pathlib import Path

from ccc_native_standby import identifier
from ccc_standby_runner import Runner
from ccc_standby_settlement import live_settlement
from tools.standby_process_overlap import capture, validate_topology
from tools.standby_run_manifest import RunManifest


class RetainedOwners:
    """One-shot coordinator; failed or uncertain starts are never retried.

    All invocation tuples (batch_id, Invocation, runner_directory) are registered
    before the first start. Call methods from one controlling thread. Keep this
    object alive until close reports no pending handles, even after an error.
    """
    def __init__(self, run_directory, invocations, *, runner_factory=Runner):
        self.manifest = RunManifest(run_directory)
        validate_topology(self.manifest.plan)
        self.run_directory = Path(run_directory)
        self.runner_factory = runner_factory
        self.handles = {}
        self.closed = False
        self.specs = {}
        expected = {identifier(row['batch_id']): row for row in self.manifest.plan['batches']}
        # Validate the entire set before registering any attempt or starting work.
        directories, invocation_ids = set(), set()
        for batch_id, invocation, directory in invocations:
            key = identifier(batch_id)
            if key not in expected or key in self.specs:
                raise ValueError('exact unique declared batch set required')
            invocation.current()
            value = invocation.value
            if any(value[k] != expected[key][k] for k in ('workspace_id', 'mode')):
                raise ValueError('invocation differs from original plan')
            path = Path(directory).resolve(strict=True)
            iid = identifier(value['invocation_id'])
            if path in directories or iid in invocation_ids:
                raise ValueError('runner directory and invocation must be unique')
            directories.add(path)
            invocation_ids.add(iid)
            self.specs[key] = (invocation, path)
        if set(self.specs) != set(expected):
            raise ValueError('all ten original invocations required')
        self.order = list(expected)
        self.expected = copy.deepcopy(expected)
        for key in self.order:
            invocation, path = self.specs[key]
            self.manifest.register_attempt(key, invocation.path, invocation.sha256, path)

    def _open(self):
        if self.closed:
            raise ValueError('coordinator is closing; new work forbidden')
        self.manifest.current()

    def _health(self):
        """A live thread is necessary; retained settled owners also need RPC proof."""
        self._open()
        for key, handle in self.handles.items():
            if not handle['thread'].is_alive() or handle['error'] is not None:
                raise ValueError('original Runner exited: ' + key)
            if handle['opening'] is not None:
                runner = handle['runner']
                if (runner.owner is not handle['original_owner']
                        or runner.caller is not handle['original_caller']):
                    raise ValueError('original Runner owner or caller replaced: ' + key)
            if handle['settlement'] is not None:
                config, job, saved, digest = handle['settlement']
                if live_settlement(config, job) != (saved, digest):
                    raise ValueError('retained owner or route changed: ' + key)

    def start(self, batch_id):
        self._health()
        key = identifier(batch_id)
        if key not in self.specs or key in self.handles:
            raise ValueError('unknown or already consumed Runner start')
        workspace = self.expected[key]['workspace_id']
        previous = [bid for bid in self.order[:self.order.index(key)]
                    if self.expected[bid]['workspace_id'] == workspace]
        if any(bid not in self.handles or self.handles[bid]['settlement'] is None
               for bid in previous):
            raise ValueError('prior cohort must settle before successor admission')
        invocation, directory = self.specs[key]
        stop = threading.Event()
        runner = self.runner_factory(invocation, directory, stop=stop)
        handle = dict(runner=runner, stop=stop, opened=threading.Event(),
                      opening=None, settlement=None, error=None, returncode=None)

        def emit(row):
            if row.get('kind') == 'standby_runner_open':
                if handle['opening'] is not None:
                    raise ValueError('original Runner opened more than once')
                if runner.owner is None or runner.caller is None:
                    raise ValueError('original Runner opened without owner or caller')
                handle['original_owner'] = runner.owner
                handle['original_caller'] = runner.caller
                handle['opening'] = copy.deepcopy(row)
                handle['opened'].set()

        def execute():
            try:
                handle['returncode'] = runner.run(prepare=False, emit=emit)
            except BaseException as exc:
                handle['error'] = type(exc).__name__
            finally:
                handle['opened'].set()

        handle['thread'] = threading.Thread(target=execute, name='standby-'+key,
                                             daemon=False)
        # Persisted Runner intent and this handle both remain consumed on errors.
        self.handles[key] = handle
        handle['thread'].start()

    def await_open(self, batch_id, *, timeout=1):
        """Poll the same retained handle; timeout returns None, never restarts."""
        import math
        if (isinstance(timeout, bool) or not isinstance(timeout, (float, int))
                or not math.isfinite(timeout) or not 0 <= timeout <= 60):
            raise ValueError('poll timeout must be between zero and sixty seconds')
        self._open()
        handle = self.handles[identifier(batch_id)]
        handle['opened'].wait(timeout)
        self._health()
        return copy.deepcopy(handle['opening'])

    def settle(self, batch_id):
        self._health()
        key = identifier(batch_id)
        handle = self.handles[key]
        opened = handle['opening']
        if opened is None:
            raise ValueError('Runner has not opened')
        invocation, _ = self.specs[key]
        config, job = opened['config_path'], opened['job_id']
        if (config != invocation.value['config_path']
                or opened['workspace_id'] != self.expected[key]['workspace_id']):
            raise ValueError('opened Runner differs from original invocation')
        saved, digest = live_settlement(config, job)
        binding = self.manifest.bind(key, config, job)
        if (binding['settlement_sha256'] != digest
                or binding['action_id'] != saved['action_id']):
            raise ValueError('bound settlement differs from original owner')
        observed = (config, job, copy.deepcopy(saved), digest)
        if handle['settlement'] not in (None, observed):
            raise ValueError('original settlement changed')
        handle['settlement'] = observed
        self._health()
        return binding

    def prepare(self, batch_id):
        """Start original preparation once; poll the retained owner afterwards.

        An exception may follow native creation, so it consumes this call too.
        No UI activation, restart or model request is implied by this return.
        """
        self._health()
        key = identifier(batch_id)
        handle = self.handles[key]
        if (handle['opening'] is None or handle['settlement'] is not None
                or handle.get('preparation_consumed')):
            raise ValueError('preparation requires an unconsumed open original')
        self.specs[key][0].current()
        owner = handle['runner'].owner
        if owner is None:
            raise ValueError('original owner unavailable')
        handle['preparation_consumed'] = True
        try:
            return owner.prepare()
        except BaseException as exc:
            handle['preparation_error'] = type(exc).__name__
            raise

    def begin_activation(self, batch_id, directory):
        """Retain the real UI driver; caller continues polling this same handle."""
        from tools.standby_ui_activation import BoundActivation
        return BoundActivation(self, batch_id, directory)

    def poll_activation(self, batch_id, *, timeout=0):
        """Write at most the next original UI phase; never manufacture settlement."""
        self._health()
        handle = self.handles[identifier(batch_id)]
        driver = handle.get('ui_activation')
        if driver is None:
            raise ValueError('original activation has not begun')
        return driver.poll(timeout=timeout)

    def observe_overlap(self, output_directory, *, seconds=120):
        self._health()
        if (set(self.handles) != set(self.specs)
                or any(h['settlement'] is None for h in self.handles.values())):
            raise ValueError('all ten settled owners must remain alive')
        value = capture(self.run_directory, output_directory, seconds=seconds)
        self._health()
        return value

    def close(self, *, timeout=1):
        """Signal only owned Runners; retain and report any unfinished handles.

        This is not native process cleanup or run acceptance. Repeat this method
        on pending handles; do not construct replacement owners or race close().
        """
        import math
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 <= timeout <= 60):
            raise ValueError('close observation timeout must be zero to sixty seconds')
        self.closed = True
        ui_pending = []
        for key, handle in self.handles.items():
            driver = handle.get('ui_activation')
            if driver is not None and not driver.close(timeout=0):
                ui_pending.append(key)
        if ui_pending:
            # The private UI must not outlive the resources it may still read.
            # Keep all original owners until every retained UI has exited.
            return {key: dict(alive=h['thread'].is_alive(), error=h['error'],
                             returncode=h['returncode'], ui_pending=key in ui_pending)
                    for key, h in self.handles.items()}
        for handle in self.handles.values():
            handle['stop'].set()
        deadline = time.monotonic() + timeout
        for handle in self.handles.values():
            if handle['thread'].ident is not None:
                handle['thread'].join(max(0, deadline-time.monotonic()))
        return {key: dict(alive=h['thread'].is_alive(), error=h['error'],
                          returncode=h['returncode']) for key, h in self.handles.items()}

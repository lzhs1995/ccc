"""Join the original preparation owner to guarded, one-shot activation.

The readiness proof reader is deliberately required. A /pwd return witness
does not establish complete configuration coverage or zero model requests.
This module has no CLI and never launches or repairs a missing original.
"""
from __future__ import annotations

import copy
import contextlib
import threading

import cmux_codex_watch as core
from ccc_batch_timing import boot_id
from ccc_native_standby import COUNT, fresh, original
from ccc_standby_manager import StandbyManager
from ccc_standby_transport import send_initial


class ActivationOwner:
    def __init__(self, preparation, ledger, *, readiness_proof):
        if not callable(readiness_proof):
            raise ValueError('live readiness proof reader required')
        self.preparation, self.ledger = preparation, ledger
        self.client = preparation.client
        self.proof_reader = readiness_proof
        self._invalid = threading.Event()
        self._originals = {}
        self._lock = threading.RLock()
        self._operation_guard = None
        self._operation_active = False
        self._selected = copy.deepcopy(preparation.selected)
        expected = {key: self._selected[key] for key in
                    ('policy', 'cohort_id', 'workspace_id', 'boot_id', 'mode', 'generation')}
        if (ledger.directory != preparation.jobfile.parent / 'standby'
                or any(ledger.manifest.get(k) != v for k, v in expected.items())
                or ledger.manifest.get('count') != COUNT
                or ledger.manifest.get('prompt') != preparation.job['initial_prompt']):
            raise ValueError('activation ledger differs from original preparation job')
        self._current()
        self.manager = StandbyManager(ledger, generation_current=self._current,
            boot_current=boot_id, authorized=self.authorized,
            observe=self.observe, send=self.send, operation_context=self._operation_context)

    @contextlib.contextmanager
    def _operation_context(self):
        # The manager serializes operations and waits for their worker futures.
        # Capture the callback, never its boolean result, in the caller thread.
        # CmuxClient's thread-local storage alone cannot reach executor workers.
        with self._lock:
            if self._operation_active:
                raise ValueError('activation operation already active')
            self._operation_guard = getattr(self.client._input_guard_local, 'guards', {}).get(id(self.client))
            self._operation_active = True
        try:
            yield
        finally:
            with self._lock:
                self._operation_active = False
                self._operation_guard = None

    def _caller_authorized(self):
        with self._lock:
            if not self._operation_active or self._invalid.is_set():
                return False
            check = self._operation_guard
        return check is None or check() is True

    def _current(self):
        if self._invalid.is_set():
            raise ValueError('activation owner permanently invalidated')
        value = self.preparation._current()
        if (value != self._selected['generation']
                or self.preparation.selected != self._selected
                or self._invalid.is_set()):
            self._invalid.set()
            raise ValueError('activation preparation generation changed')
        return value

    def _index(self, index):
        if type(index) is not int or not 0 <= index < COUNT:
            raise ValueError('invalid activation slot')
        return self.preparation.job['slots'][index]

    def authorized(self, index, *, topology=True):
        """Reapply the caller's live input guard inside the ledger write lock.

        CmuxClient also invokes this thread's input guard on connection
        admission. Repeating it here covers time spent waiting for that lock;
        the original preparation permission and generation follow the read.
        """
        try:
            self._index(index)
            self._current()
            sid = self.preparation._surfaces.get(index)
            if not sid:
                return False
            if not self._caller_authorized():
                self._invalid.set()
                return False
            check = getattr(self.client._input_guard_local, 'guards', {}).get(id(self.client))
            if check is not None and check != self._caller_authorized and check() is not True:
                self._invalid.set()
                return False
            allowed = self.preparation._authorized(index, surface_id=sid,
                connected=self.client if topology else None)
            self._current()
            if allowed is not True:
                self._invalid.set()
                return False
            return True
        except BaseException:
            self._invalid.set()
            raise

    def observe(self, index):
        """Bind a separately established ready proof to the fresh original.

        Proofs must describe the same original and current observation window;
        the reader must stay live and invalidate on any source/request change.
        No identity, permission or queue field is copied from the proof.
        """
        try:
            slot = self._index(index)
            self._current()
            checked = []
            selected = self._selected

            def connected(row):
                if checked:
                    raise ValueError('activation proof reader repeated')
                baseline = original(row, selected['workspace_id'])
                if (row.get('job_id') != selected['job_id']
                        or row.get('index') != index
                        or row.get('launch_id') != slot['launch_id']
                        or row.get('surface_id') != self.preparation._surfaces.get(index)
                        or row.get('generation') != selected['generation']
                        or row.get('boot_id') != selected['boot_id']):
                    raise ValueError('activation observer is not the original job slot')
                with self._lock:
                    if index in self._originals and self._originals[index] != baseline:
                        raise ValueError('activation original writer/session changed')
                    self._originals.setdefault(index, copy.deepcopy(baseline))
                proof = self.proof_reader(index, copy.deepcopy(row))
                if proof is not None and (not isinstance(proof, dict)
                        or proof.get('readiness_proven') is not True
                        or proof.get('sources_complete') is not True
                        or proof.get('job_id') != selected['job_id']
                        or proof.get('generation') != selected['generation']
                        or proof.get('boot_id') != selected['boot_id']
                        or proof.get('original') != baseline
                        or proof.get('return_receipt_sha256') != row.get('return_receipt_sha256')
                        or type(proof.get('model_request_count')) is not int
                        or proof['model_request_count'] != 0):
                    raise ValueError('readiness proof does not cover the original activation')
                # Both callbacks precede the barrier's actual screen read and
                # final original PID/writer/rollout/prefix inspection.
                # The preparation barrier immediately follows this callback
                # with its own connected topology/permission check, then
                # repeats it after replay and after native identity reads.
                # Retain every live caller/source/permission check here, but
                # do not issue the same topology RPC twice at this boundary.
                if not self.authorized(index, topology=False):
                    raise ValueError('activation observation no longer authorized')
                checked.append((baseline, proof is not None, row['return_receipt_sha256']))

            final_checks = []
            def final():
                if final_checks or not self.authorized(index, topology=False):
                    raise ValueError('activation final action authorization refused')
                final_checks.append(True)

            row = self.preparation.observe_for_activation(index,
                connected_check=connected, final_check=final)
            if row is None:
                return None
            baseline = original(row, selected['workspace_id'])
            if (len(checked) != 1 or len(final_checks) != 1 or checked[0][0] != baseline
                    or checked[0][2] != row.get('return_receipt_sha256')
                    or row.get('index') != index or row.get('job_id') != selected['job_id']):
                raise ValueError('activation inspection skipped or changed connected proof')
            if self._invalid.is_set():
                raise ValueError('activation invalidated during observation')
            if not checked[0][1]:
                return None
            ready = {**row, 'readiness_proven': True, 'model_request_count': 0}
            fresh(ready, selected['boot_id'], self.ledger.clock())
            return ready
        except BaseException:
            self._invalid.set()
            raise

    def send(self, row, prompt, input_id, *, write_guard):
        """Use the existing workspace lock and the admitted socket guard.

        The ledger owns consumption and calls observe/authorized after taking
        its final write lock. This sender supplies neither retries nor a
        separate readiness path. ACK waiting remains outside that write lock.
        """
        index = row['index']
        self._index(index)
        self._current()
        with self._lock:
            if self._originals.get(index) != original(row, self._selected['workspace_id']):
                raise ValueError('sender is not bound to the observed original')
        if prompt != self.ledger.manifest['prompt']:
            raise ValueError('sender prompt differs from durable activation')
        with core.workspace_input_lock(self.preparation.config_path,
                self._selected['workspace_id'], shared=True):
            # Install the captured live action guard in this worker too, so
            # the actual admitted connection runs it before its guarded write.
            previous = getattr(self.client._input_guard_local, 'guards', {}).get(id(self.client))
            def combined():
                return (self._caller_authorized()
                        and (previous is None or previous() is True))
            with self.client.input_guard(combined):
                return send_initial(self.client, row, prompt, input_id, write_guard=write_guard)

    def close(self):
        self._invalid.set()
        try:
            self.manager.close()
        finally:
            self.preparation.close()

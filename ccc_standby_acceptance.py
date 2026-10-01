"""Observe an activated original's first task, then release only its hold.

No launch, input or readiness capability is created here. The immutable job,
activation, per-slot input and original startup Hook must already exist. The
incremental native transcript parser is shared with the legacy worker, without
constructing that worker or converting a standby job into a legacy job.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import threading
import time

import cmux_codex_watch as core
import ccc_workspace_batch as batch
import ccc_standby_launch as launch
from ccc_batch_timing import boot_id, stamp
from ccc_codex_queue import epoch, process_writable_files, IncompleteVnodeRead, VnodeInventoryChanged
from ccc_guard_scope import process, birth
from ccc_native_standby import COUNT, digest, identifier, write_once


class FirstTaskObserver:
    _confirm = batch.BatchWorker._confirm
    _recheck_context_proof = batch.BatchWorker._recheck_context_proof

    def __init__(self, config_path, job_id, action_id, *, client, clock=time.time,
                 observation_clock=None):
        self.config_path = Path(config_path).resolve(strict=True)
        self.path = batch.job_path(self.config_path, identifier(job_id))
        self.client, self.clock = client, clock
        self.observation_clock = observation_clock or stamp
        self.store = core.ConfigStore(self.config_path)
        self.action_id = identifier(action_id)
        self._files, self._slots, self._failed = {}, {}, set()
        self._locks = [threading.Lock() for _ in range(COUNT)]
        self._evidence_lock = threading.RLock()
        self.job = self._read(self.path)
        self.selected = launch.policy(self.job, self.config_path)
        self.directory = self.path.parent / 'standby'
        self._directory = self._dir_identity(self.directory)
        manifest = self._read(self.directory / 'cohort.json')
        self.activation = self._read(self.directory / 'activation.json')
        attempt = self._read(self.directory / 'activation-attempt.json')
        self.originals = self._read(self.directory / 'originals.json')
        if (any(manifest.get(k) != self.selected[k] for k in
                ('policy', 'cohort_id', 'workspace_id', 'boot_id', 'mode', 'generation'))
                or manifest.get('count') != COUNT or manifest.get('prompt') != batch.PROMPT
                or any(self.activation.get(k) != v for k, v in manifest.items())
                or self.activation.get('action_id') != self.action_id
                or self.activation.get('originals') != self.originals
                or len(self.originals) != COUNT
                or [r['index'] for r in self.originals] != list(range(COUNT))
                or attempt.get('action_id') != self.action_id
                or attempt.get('activation_sha256') != digest(self.activation)):
            raise ValueError('first-task observer is not bound to the original activation')
        self._current()

    @staticmethod
    def _dir_identity(path):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('first-task evidence directory is not private')
        return info.st_dev, info.st_ino

    def _read(self, path):
        with self._evidence_lock:
            info = path.lstat()
            before = batch._file_generation(path)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > 2 * 1024 * 1024):
                raise ValueError('first-task evidence is not a bounded private file')
            raw = path.read_bytes()
            if batch._file_generation(path) != before:
                raise ValueError('first-task evidence changed during read')
            old = self._files.setdefault(path, (before, raw))
            if old != (before, raw):
                raise ValueError('first-task original evidence changed')
            return json.loads(raw)

    def _current(self):
        if (boot_id() != self.selected['boot_id']
                or self._dir_identity(self.directory) != self._directory
                or os.path.lexists(self.directory / 'invalidated.json')):
            raise ValueError('first-task activation invalidated')
        with self._evidence_lock:
            for path in tuple(self._files):
                self._read(path)

    def _bind(self, index):
        row = self.originals[index]
        slot = self.job['slots'][index]
        claim_path = launch.claim_path(self.config_path, self.job['id'], index)
        claim = self._read(claim_path)
        hook = self._read(self.path.parent / f'standby-session-{index}.json')
        receipt = self._read(self.path.parent / f'surface-{index}.json')
        delivery = self._read(self.directory / f'input-{index}.json')
        claim_sha = hashlib.sha256(self._files[claim_path][1]).hexdigest()
        activation_sha = hashlib.sha256(self._files[self.directory / 'activation.json'][1]).hexdigest()
        if (row['launch_id'] != slot['launch_id'] or row['index'] != index
                or row['workspace_id'] != self.selected['workspace_id']
                or any(hook.get(k) != v for k, v in row.items())
                or any(hook.get(k) != v for k, v in {
                    'job_id': self.job['id'], 'cohort_id': self.selected['cohort_id'],
                    'action_id': self.action_id, 'source': 'startup'}.items())
                or delivery.get('original') != row or delivery.get('action_id') != self.action_id
                or delivery.get('activation_sha256') != activation_sha
                or delivery.get('prompt') != batch.PROMPT or hook.get('input_id') != delivery.get('input_id')
                or row.get('claim_sha256') != claim_sha
                or any(claim.get(k) != v for k, v in self.selected.items())
                or claim.get('state') != 'exec_intent' or claim.get('index') != index
                or claim.get('launch_id') != slot['launch_id']
                or claim.get('bootstrap_pid') != row['pid'] or claim.get('bootstrap_birth') != row['birth']
                or claim.get('surface_id') != row['surface_id']
                or row.get('argv_sha256') != hashlib.sha256(json.dumps(claim['argv'], separators=(',', ':')).encode()).hexdigest()
                or claim.get('receipt_sha256') != hashlib.sha256(self._files[self.path.parent / f'surface-{index}.json'][1]).hexdigest()
                or any(receipt.get(k) != row[k] for k in ('surface_id', 'workspace_id', 'launch_id'))):
            raise ValueError('first-task Hook/input/launch identity mismatch')
        identifier(delivery['input_id'])
        self._current()
        return row, claim, hook

    def _live(self, row, claim, hook):
        current = process(row['pid'], launch=True)
        if (not current or current.get('remote') or current.get('birth') != row['birth']
                or current.get('argv') != claim['argv'] or current.get('surface_id') != row['surface_id']
                or current.get('environment_workspace_id') != row['workspace_id']):
            raise ValueError('first-task original process changed')
        root = (Path(current['environment'].get('CODEX_HOME') or Path.home() / '.codex') / 'sessions').resolve(strict=True)
        if (str(root) != hook['sessions_root'] or Path(row['writer_lock']) !=
                root.parent / 'thread-writer-locks' / (row['session_id'] + '.lock')):
            raise ValueError('first-task original native home changed')
        files = process_writable_files(row['pid'], identities=True)
        for name, expected in ((row['writer_lock'], row['writer_identity']),
                               (claim['tui_log'], claim['tui_log_identity'])):
            path = Path(name)
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or [info.st_dev, info.st_ino] != expected
                    or files.get(path) != dict(zip(('device', 'inode'), expected))):
                raise ValueError('first-task original writer changed')
        if (process(row['pid'], launch=True) != current
                or birth(row['pid'], codex=True) != row['birth']):
            raise ValueError('first-task original process changed during observation')
        final_files = process_writable_files(row['pid'], identities=True)
        for name, expected in ((row['writer_lock'], row['writer_identity']),
                               (claim['tui_log'], claim['tui_log_identity'])):
            path = Path(name)
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or [info.st_dev, info.st_ino] != expected
                    or final_files.get(path) != dict(zip(('device', 'inode'), expected))):
                raise ValueError('first-task writer changed during final process read')
        return root, files

    def _submission(self, claim, hook):
        data, submitted = batch._initial_event_prefix(claim, require_turn=True)
        length = hook['tui_prefix_bytes']
        if (not submitted or type(length) is not int or not 0 < length <= len(data)
                or hashlib.sha256(data[:length]).hexdigest() != hook['tui_prefix_sha256']):
            raise ValueError('first-task original Hook prefix changed')
        for line in data.splitlines():
            event = json.loads(line)
            if (event.get('dir') == 'from_tui' and event.get('kind') == 'op'
                    and 'UserTurn' in (event.get('payload') or {})):
                at = epoch(event['ts'])
                if not math.isfinite(at) or at < claim['at'] - .001:
                    raise ValueError('first-task submission timestamp missing or invalid')
                return at
        raise ValueError('first-task original submission missing')

    def _transcript_path(self, row, hook, root, files):
        # The Hook may predate rollout persistence. Use its exact path or the
        # original process's writable rollout, never derive a date from UUID.
        paths = [Path(hook['transcript'])] if hook.get('transcript') else [
            p for p in files if p.is_relative_to(root) and p.name.endswith(row['session_id'] + '.jsonl')]
        if not paths:
            return None
        if len(paths) != 1:
            raise ValueError('ambiguous original first-task transcript')
        path = paths[0]
        if not os.path.lexists(path):
            return None  # The native Hook can provide a not-yet-persisted path.
        if (not path.is_absolute() or not path.is_relative_to(root)
                or not path.name.endswith(row['session_id'] + '.jsonl')
                or path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode)):
            raise ValueError('first-task transcript outside original native sessions')
        return path

    @staticmethod
    def _prefix(path, length):
        if type(length) is not int or length < 0:
            raise ValueError('invalid first-task prefix length')
        remaining, hashed = length, hashlib.sha256()
        with path.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size < length:
                raise ValueError('original transcript prefix truncated')
            while remaining:
                data = stream.read(min(remaining, batch.CONFIRM_READ_BYTES))
                if not data:
                    raise ValueError('original transcript prefix truncated during read')
                hashed.update(data)
                remaining -= len(data)
            after = os.fstat(stream.fileno())
        now = path.lstat()
        if (not stat.S_ISREG(now.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or (after.st_dev, after.st_ino) != (now.st_dev, now.st_ino) or now.st_size < length):
            raise ValueError('original transcript replaced during prefix read')
        return {'identity': [before.st_dev, before.st_ino], 'bytes': length, 'sha256': hashed.hexdigest()}

    def _confirm_original(self, slot):
        path = Path(slot['transcript'])
        previous = slot.get('read_prefix')
        if previous and self._prefix(path, previous['bytes']) != previous:
            raise ValueError('original transcript previously read prefix changed')
        offset = slot.get('confirmation', {}).get('offset', slot['transcript_offset'])
        length = min(path.stat().st_size, offset + batch.CONFIRM_READ_BYTES)
        before = self._prefix(path, length)
        old_proof = copy.deepcopy(slot.get('confirmation'))
        confirmed = self._confirm(slot)
        if self._prefix(path, length) != before:
            raise ValueError('original transcript changed while parsing first task')
        end = slot.get('confirmation', {}).get('offset', 0)
        if end > length:
            # Appended bytes arrived after the read boundary. Parse those in
            # the next bounded observation, without certifying unseen bytes.
            if old_proof is None:
                slot.pop('confirmation', None)
            else:
                slot['confirmation'] = old_proof
            return False
        slot['read_prefix'] = before
        return confirmed

    def _first_observation(self, index, row, slot):
        """Keep the first stable task observation, including across restart.

        Native task_started can precede its user_message on disk. Persist the
        clock here, but only a later fully confirmed receipt can certify it.
        """
        proof = slot.get('confirmation', {})
        if not proof.get('started') or not proof.get('task_id') or proof.get('blocked'):
            return None
        path = self.path.parent / f'standby-first-observation-{index}.json'
        binding = {**self.selected, 'action_id': self.action_id, 'original': row,
            'task_id': proof['task_id'], 'task_at': proof['task_at'],
            'transcript': slot['transcript']}
        if path.exists():
            saved = self._read(path)
            if any(saved.get(k) != v for k, v in binding.items()):
                raise ValueError('persisted first observation belongs to another task')
            prefix = saved['transcript_prefix']
            if self._prefix(Path(slot['transcript']), prefix['bytes']) != prefix:
                raise ValueError('persisted first observation prefix changed')
        else:
            saved = {**binding, 'transcript_prefix': copy.deepcopy(slot['read_prefix']),
                     'observed': self.observation_clock()}
            self._validate_observation(saved['observed'])
            write_once(path, saved)
            saved = self._read(path)
        self._validate_observation(saved['observed'])
        return copy.deepcopy(saved['observed'])

    def _validate_observation(self, observed):
        if (observed.get('boot_id') != self.selected['boot_id']
                or any(type(observed.get(k)) not in (int, float)
                    or not math.isfinite(observed[k]) or observed[k] < 0
                    for k in ('wall', 'monotonic'))
                or observed['monotonic'] < self.activation['committed_monotonic']):
            raise ValueError('first observation clock does not cover original activation')

    def poll(self, index, *, release=True):
        if type(index) is not int or not 0 <= index < COUNT:
            raise ValueError('invalid first-task slot')
        with self._locks[index]:
            if index in self._failed:
                raise ValueError('first-task slot permanently rejected')
            try:
                self._current()
                if not (self.path.parent / f'standby-session-{index}.json').exists():
                    return None
                row, claim, hook = self._bind(index)
                root, files = self._live(row, claim, hook)
                submitted = self._submission(claim, hook)
                transcript = self._transcript_path(row, hook, root, files)
                if transcript is None:
                    if index in self._slots:
                        raise ValueError('original first-task transcript disappeared')
                    return None
                slot = self._slots.setdefault(index, {**copy.deepcopy(row),
                    'native_birth': row['birth'], 'process_start': row['birth'][0],
                    'transcript': str(transcript), 'transcript_offset': 0, 'submit_at': submitted})
                if slot['transcript'] != str(transcript) or slot['submit_at'] != submitted:
                    raise ValueError('first-task transcript or submission changed')
                confirmed = self._confirm_original(slot)
                if slot.get('confirmation', {}).get('started'):
                    self._bind(index)
                    self._live(row, claim, hook)
                    self._submission(claim, hook)
                observed = self._first_observation(index, row, slot)
                if not confirmed:
                    if slot.get('confirmation', {}).get('blocked'):
                        raise ValueError(slot['confirmation']['blocked'])
                    return None
                proof = slot['confirmation']
                if not proof.get('task_id'):
                    raise ValueError('first-task native turn identity missing')
                self._bind(index)
                self._live(row, claim, hook)
                self._submission(claim, hook)
                record_path = self.path.parent / f'standby-first-task-{index}.json'
                record = {**self.selected, 'action_id': self.action_id, 'input_id': hook['input_id'],
                    'original': row, 'submit_at': submitted, 'transcript': str(transcript),
                    'transcript_prefix': copy.deepcopy(slot['read_prefix']),
                    'confirmation': copy.deepcopy(proof), 'observed_at': self.clock(),
                    'first_task_observed': observed}
                if record_path.exists():
                    saved = self._read(record_path)
                    if (any(saved.get(k) != record[k] for k in record if k not in ('confirmation', 'observed_at', 'transcript_prefix'))
                            or any(saved.get('confirmation', {}).get(k) != proof.get(k) for k in
                                ('identity', 'session_id', 'task_id', 'task_at', 'prompt', 'started', 'confirmed'))):
                        raise ValueError('first-task persisted confirmation differs')
                    if self._prefix(transcript, saved['transcript_prefix']['bytes']) != saved['transcript_prefix']:
                        raise ValueError('persisted first-task transcript prefix changed')
                    record = saved
                else:
                    write_once(record_path, record)
                    self._read(record_path)
                if release:
                    self._release(index, row, claim, hook, slot)
                return copy.deepcopy(record)
            except (IncompleteVnodeRead, VnodeInventoryChanged):
                # A changing FD inventory is no proof either way. The caller's
                # original observation deadline bounds subsequent polls. Never
                # release a hold or return a cached receipt on this poll.
                return None
            except BaseException:
                self._failed.add(index)
                raise

    def _release(self, index, row, claim, hook, slot):
        with core.workspace_input_lock(self.config_path, row['workspace_id'], shared=True):
            target = core.find_main_surface(self.client.workspace_tree(row['workspace_id']), row['surface_id'])
            if target.get('workspace_id') != row['workspace_id']:
                raise ValueError('first-task surface left its original workspace')
            self._bind(index)
            self._live(row, claim, hook)
            self._submission(claim, hook)
            if self._prefix(Path(slot['transcript']), slot['read_prefix']['bytes']) != slot['read_prefix']:
                raise ValueError('first-task transcript changed before hold release')
            from ccc_private_check import record_origin
            record_origin(self.config_path, self.job, {**slot, 'phase': 'confirmed'})

            def change(config):
                self._current()  # Includes the durable first-task receipt.
                if not batch.allowed(config, self.job):
                    raise ValueError('first-task hold release no longer authorized')
                rule = core.workspace_rule_by_id(config, row['workspace_id'])
                sid = row['surface_id']
                reasons = rule.get('excluded_surface_reasons', {})
                own = f"batch:{self.job['id']}:initial"
                hold = rule.get('batch_start_holds', {}).get(sid)
                if (any(t.get('surface_id') == sid and (t.get('paused') or not t.get('enabled', True))
                        for t in config['targets'])
                        or (sid in rule.get('excluded_surface_ids', []) and reasons.get(sid) != own)
                        or (hold is not None and (hold.get('job_id') != self.job['id'] or hold.get('index') != index))):
                    raise ValueError('first-task hold belongs to another operation or is excluded')
                if hold is not None:
                    rule['batch_start_holds'].pop(sid)
                if reasons.get(sid) == own:
                    reasons.pop(sid)
                    rule['excluded_surface_ids'] = [s for s in rule.get('excluded_surface_ids', []) if s != sid]
            self.store.mutate(change)

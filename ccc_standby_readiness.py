"""One local preparation control and an observed native refresh-return barrier.

This is not a model-request certificate. The native single-case fixture must
separately establish zero requests and the full adapter must bind configuration
sources before this observation can contribute to manager readiness.
"""
from __future__ import annotations

import contextlib
import copy
from functools import partial
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
import uuid

import cmux_codex_watch as core
from ccc_native_standby import generation, identifier, original, write_once
from ccc_standby_identity import inspect_original, _idle_prefix, StartupPending, ObservationPending
from ccc_standby_transport import send_initial


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class _Preparing(Exception):
    """A later complete observation may progress without another input."""


def pwd_visible(grid, cwd):
    """Accept exact rendered output, never a prefix of another directory.

    Wrapped paths are deliberately pending until a supported, unambiguous
    representation is available. We do not strip whitespace inside paths.
    """
    expected = 'Current working directory: ' + cwd
    lines = [line.strip().removeprefix('• ').removeprefix('● ') for line in grid.lines]
    return sum(line == expected for line in lines) == 1


class StandbyRefreshBarrier:
    """Bind one /pwd to one original, then inspect fresh actual rendering.

    authorized(client, row) must perform the original live workspace/input
    authorization, including stop/pause/protection. The callback is invoked
    inside the actual socket-write guard using the admitted connection.
    No uncertain control is retried, even by a newly constructed instance.
    """
    def __init__(self, directory, claim_path, *, claim_sha256, expected,
                 expected_argv, sessions_root, client, generation_current,
                 boot_current, authorized, inspect=None, clock=time.monotonic):
        if not all(callable(c) for c in (generation_current, boot_current, authorized, clock)):
            raise ValueError('complete preparation callbacks required')
        self.directory = Path(directory)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError('preparation ledger must be private and owned')
        self._directory_identity = [info.st_dev, info.st_ino]
        self.claim_path = Path(claim_path)
        self._claim_raw = self.claim_path.read_bytes()
        if _sha(self._claim_raw) != claim_sha256:
            raise ValueError('preparation claim changed')
        self.claim = json.loads(self._claim_raw)
        self.client, self.clock = client, clock
        self.generation_current, self.boot_current = generation_current, boot_current
        self.authorized = authorized
        self.inspect = inspect or partial(inspect_original, self.claim_path,
            claim_sha256=claim_sha256, expected=expected, expected_argv=expected_argv,
            sessions_root=sessions_root)
        self._generation = generation(generation_current())
        self._boot = identifier(boot_current())
        self._invalid = threading.Event()
        self._write_lock = threading.RLock()
        self._operation = threading.RLock()
        self._original = None
        self._prefix = b''
        self._reloads = None
        self._ack = False
        self._write_entered = False
        self._receipt = None
        self._intent_raw = None
        self._last_clock = -1.0
        self._observation_deadline = None
        self._attempt_deadline = None
        self._return_deadline = None
        self._index = expected['index']
        self._expected = copy.deepcopy(expected)
        self.intent = self.directory / f'prepare-control-{self._index}.json'
        self.return_receipt = self.directory / f'prepare-return-{self._index}.json'
        self.invalid_receipt = self.directory / f'prepare-invalid-{self._index}.json'
        # A persisted intent is an irrevocable consumption, never permission
        # to reconstruct readiness or send a replacement local command.
        self._consumed = any(os.path.lexists(p) for p in
                             (self.intent, self.return_receipt, self.invalid_receipt))

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < self._last_clock or now < 0:
            raise ValueError('preparation monotonic clock invalid')
        self._last_clock = now
        return now

    def _live(self):
        if self._invalid.is_set() or os.path.lexists(self.invalid_receipt):
            raise ValueError('preparation permanently invalidated')
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or [info.st_dev, info.st_ino] != self._directory_identity
                or self.claim_path.is_symlink() or self.claim_path.read_bytes() != self._claim_raw
                or generation(self.generation_current()) != self._generation
                or identifier(self.boot_current()) != self._boot):
            raise ValueError('preparation ownership/configuration/boot changed')
        if self._invalid.is_set():
            raise ValueError('preparation invalidated during callback')
        if self._intent_raw is not None and (self.intent.is_symlink()
                or self.intent.read_bytes() != self._intent_raw):
            raise ValueError('preparation control receipt changed')

    def invalidate(self, reason):
        self._invalid.set()  # Remains invalid even if recording the reason fails.
        with self._write_lock:
            if not os.path.lexists(self.invalid_receipt):
                write_once(self.invalid_receipt, {'reason': type(reason).__name__,
                    'index': self._index, 'generation': self._generation, 'boot_id': self._boot})

    def _control_draft_pending(self, grid):
        # terminal.paste's ACK confirms the terminal operation, not that the
        # native event loop has consumed Enter. Only observe this exact local
        # command's intermediate rendering; never clear it or send again.
        row = grid.cursor.row
        return (self._ack and self._receipt is None and self._write_entered
                and grid.raw.get('full') is True and grid.cursor.visible
                and grid.cursor.column == 6 and row >= 2
                and core.classify_grid(grid).kind == 'composer_busy'
                and core._composer_status(grid) == ('composer_busy', row)
                and grid.lines[row].rstrip() == '› /pwd'
                and grid.lines[row - 2].rstrip() == '› /pwd  show the current working directory'
                and not grid.lines[row - 1].strip()
                and not core._queued_followup_present(grid.lines, row))

    def _screen(self, row, *, allow_control_pending=False):
        payload = self.client.replay(row['workspace_id'], row['surface_id'], live=True)
        if any(payload.get(k) != row[k] for k in ('workspace_id', 'surface_id')):
            raise ValueError('preparation replay target changed')
        grid = core.Grid.from_rpc(payload, row['surface_id'])
        if (core.classify_grid(grid).kind != 'idle' or core._composer_status(grid)[0] != 'empty'
                or core._queued_followup_present(grid.lines, grid.cursor.row)):
            if not (allow_control_pending and self._control_draft_pending(grid)):
                raise ValueError('preparation composer/queue is not empty and idle')
        return grid

    def _check_observation_deadline(self):
        now = self._now()
        # Keep the exhausted budget and original slot in the propagated error:
        # the service records this error before tearing down its endpoint.
        for kind, deadline, message in (
                ('return', self._return_deadline,
                 'acknowledged preparation control did not return within 30 seconds'),
                ('observation', self._observation_deadline,
                 'original vnode observation unavailable for 30 seconds'),
                ('attempt', self._attempt_deadline,
                 'preparation operation exceeded 30 seconds')):
            if deadline is not None and now >= deadline:
                raise TimeoutError(f'{message}; index={self._index} deadline_kind={kind} '
                                   f'deadline={deadline:.6f} observed={now:.6f}')

    def _inspect(self, *, before_write=False, pending_for_activation=False, **callbacks):
        if not self._consumed or self._ack or before_write:
            self._check_observation_deadline()
        try:
            observation = self.inspect(**callbacks)
        except StartupPending as pending:
            observation = pending.observation
        except ObservationPending as pending:
            if self._consumed and not self._ack and not before_write:
                raise
            self._live()
            if any(pending.observation.get(k) != v for k, v in self._expected.items()):
                raise ValueError('pending original launch changed')
            if self.authorized(self.client, pending.observation) is not True:
                raise ValueError('pending observation authorization refused')
            pending.recheck()
            self._live()
            now = self._now()
            if self._observation_deadline is None:
                self._observation_deadline = now + 30.0
            if now >= self._observation_deadline:
                self._check_observation_deadline()
            if pending_for_activation:
                # The outer activation owner must rebuild its proof/final
                # callbacks too. A second FD read may fail after the first
                # callback was already consumed; do not retry it in place.
                raise
            raise _Preparing('original vnode observation pending') from pending
        # A complete identity inspection already brackets two stable native
        # inventories and PID/writer/prefix checks. It ends this unavailable
        # episode, even if the next independent inspection is pending again.
        # Keep a separate operation deadline so repeated recoveries cannot
        # extend a connected write attempt indefinitely.
        self._check_observation_deadline()
        self._observation_deadline = None
        return observation

    def _events(self, row):
        data, sent = _idle_prefix(self.claim)
        size = row['tui_prefix_bytes']
        if (sent or not data.startswith(self._prefix) or size > len(data)
                or _sha(data[:size]) != row['tui_prefix_sha256']):
            raise ValueError('preparation TUI prefix changed during observation')
        reloads = []
        offset = 0
        for line in data.splitlines(keepends=True):
            offset += len(line)
            event = json.loads(line)
            if event.get('dir') == 'from_tui' and event.get('kind') == 'op':
                skills = event.get('payload', {}).get('ListSkills', {})
                if skills.get('force_reload') is True and skills.get('cwds') == [self.claim['cwd']]:
                    reloads.append({'end_offset': offset, 'sha256': _sha(line)})
        if self._reloads is not None and reloads != self._reloads:
            raise ValueError('another skills refresh invalidated preparation control')
        return data, reloads

    def _check(self, *, before_control=False, before_write=False):
        self._live()
        row = self._inspect(before_write=before_write)
        if any(row.get(k) != value for k, value in self._expected.items()):
            raise ValueError('preparation original launch changed')
        identity = original(row, row['workspace_id'])
        if not identity.get('writer_identity') or not identity.get('writer_lock'):
            raise ValueError('preparation original writer absent')
        if self._original is not None and identity != self._original:
            raise ValueError('preparation original identity changed')
        data, reloads = self._events(row)
        if self.authorized(self.client, row) is not True:
            raise ValueError('original live input authorization refused')
        if row.get('startup_observed') is not True:
            self._live()
            self._prefix, self._original = data, identity
            if self._consumed:
                raise ValueError('native startup evidence lost after control consumption')
            # A validated StartupPending observation includes complete vnode
            # identity evidence. Native startup waiting is not continued
            # file-inventory unavailability; a later outage gets its own bound.
            self._observation_deadline = None
            raise _Preparing('original native startup not yet observed')
        # Retain the first complete original before another blocking read.
        # Pending observations may not rebase its writer, prefix or reload.
        self._prefix, self._original = data, identity
        if reloads:
            self._reloads = reloads
        grid = self._screen(row, allow_control_pending=not before_control and not before_write)
        if before_control and any('Current working directory:' in line for line in grid.lines):
            raise ValueError('preexisting local output cannot identify a fresh control')
        if self.authorized(self.client, row) is not True:
            raise ValueError('original live input authorization changed during connected reads')
        # These checks also run after potentially blocking connected reads.
        # Do not let a callback change the generation or native identity and
        # then use its previous positive result to authorize the write.
        after = self._inspect(before_write=before_write)
        if original(after, row['workspace_id']) != identity:
            raise ValueError('preparation identity changed after connected reads')
        after_data, after_reloads = self._events(after)
        if not after_data.startswith(data):
            raise ValueError('preparation events changed during connected reads')
        if after_reloads != reloads:
            if (not self._consumed and self._reloads is None
                    and not reloads and len(after_reloads) == 1):
                # Startup may dispatch its first refresh while replay blocks.
                # Bind the observed event, then repeat all reads on a later
                # prepare call. This screen cannot authorize a control; any
                # subsequent refresh must still invalidate the bound event.
                self._live()
                self._prefix, self._original = after_data, identity
                self._reloads = after_reloads
                raise _Preparing('first skills refresh arrived during connected reads')
            raise ValueError('preparation events changed during connected reads')
        self._live()
        self._prefix, self._original = after_data, identity
        if not reloads:
            if self._consumed:
                raise ValueError('original skills refresh evidence disappeared')
            raise _Preparing('original force-reload dispatch not yet observed')
        self._reloads = reloads
        self._check_observation_deadline()
        if self._control_draft_pending(grid):
            # All authorization, identity and event checks above still apply
            # to this intermediate frame. It supplies no readiness witness.
            raise _Preparing('acknowledged local control is still being rendered')
        return after, grid

    @contextlib.contextmanager
    def _guard(self):
        with self._write_lock:
            if self._write_entered:
                raise ValueError('preparation input already attempted')
            while True:
                try:
                    self._check(before_control=True, before_write=True)
                    break
                except _Preparing:
                    # No input bytes have been written: wait inside this
                    # single transport attempt, never reconnect or resend.
                    self._check_observation_deadline()
                    time.sleep(0.02)
            self._check_observation_deadline()
            self._write_entered = True
            self._sent_at = self._now()
            self._live()
            yield

    def prepare(self):
        """Consume one local control. An error or lost ACK is never retried."""
        with self._operation:
            if self._consumed:
                return False
            try:
                self._attempt_deadline = self._now() + 30.0
                row, grid = self._check(before_control=True)
                self._check_observation_deadline()
                control_id = str(uuid.uuid4())
                value = {'schema': 1, 'kind': 'standby_preparation_control', 'command': '/pwd',
                    'control_id': control_id, 'original': self._original,
                    'generation': self._generation, 'boot_id': self._boot,
                    'reloads': self._reloads, 'tui_prefix_bytes': len(self._prefix),
                    'tui_prefix_sha256': _sha(self._prefix), 'screen_signature': grid.signature(),
                    'intent_monotonic': self._now()}
                self._consumed = True
                self._intent_raw = write_once(self.intent, value)
                send_initial(self.client, row, '/pwd', control_id, write_guard=self._guard)
                if not self._write_entered:
                    raise ValueError('preparation transport skipped actual-write guard')
                self._live()
                self._ack_at = self._now()
                self._ack = True
                self._return_deadline = self._ack_at + 30.0
                # A known ACK starts an observation-only phase. An unknown
                # ACK never reaches here and must never permit a retry.
                self._observation_deadline = None
                return True
            except _Preparing:
                return False
            except Exception as exc:
                self.invalidate(exc)
                raise
            finally:
                self._attempt_deadline = None

    def observe(self):
        """Return a current return-witness, not overall native readiness."""
        with self._operation:
            if not self._ack:
                return None
            try:
                self._attempt_deadline = self._now() + 30.0
                row, grid = self._check()
                self._check_observation_deadline()
                now = self._now()
                if self._receipt is None:
                    if not pwd_visible(grid, self.claim['cwd']):
                        return None
                    value = {'schema': 1, 'kind': 'standby_refresh_return',
                        'control_sha256': _sha(self._intent_raw), 'original': self._original,
                        'generation': self._generation, 'boot_id': self._boot,
                        'sent_monotonic': self._sent_at, 'ack_monotonic': self._ack_at,
                        'observed_monotonic': now, 'screen_signature': grid.signature(),
                        'tui_prefix_bytes': len(self._prefix), 'tui_prefix_sha256': _sha(self._prefix),
                        'refresh_return_observed': True, 'refresh_success_verified': False,
                        'readiness_proven': False}
                    raw = write_once(self.return_receipt, value)
                    self._receipt = raw
                    self._check()  # Include changes while persisting the witness.
                self._check_observation_deadline()
                self._return_deadline = None
                if self.return_receipt.is_symlink() or self.return_receipt.read_bytes() != self._receipt:
                    raise ValueError('preparation return receipt changed')
                self._check_observation_deadline()
                self._observation_deadline = None
                return {**row, 'boot_id': self._boot, 'generation': self._generation,
                    'observed_monotonic': self._now(), 'refresh_return_observed': True,
                    'refresh_success_verified': False, 'readiness_proven': False,
                    'preparation_control_count': 1, 'control_receipt_sha256': _sha(self._intent_raw),
                    'return_receipt_sha256': _sha(self._receipt)}
            except _Preparing:
                # Only observation can wait after ACK; prepare remains
                # consumed and activation still requires complete evidence.
                return None
            except Exception as exc:
                self.invalidate(exc)
                raise
            finally:
                self._attempt_deadline = None

    def observe_for_activation(self, *, connected_check=None, final_check=None):
        """Recheck a witnessed original using one bounded identity inspection.

        This never sends the preparation control or manufactures readiness.
        The preparation return must already be durable in this live owner.
        The identity helper brackets one screen/authorization read with live
        PID/FD/prefix checks; its rollout callback must use the shared inventory
        in the real preparation owner. Provider/source completeness remains a
        separate adapter obligation, so readiness_proven stays False.
        """
        with self._operation:
            if connected_check is not None and not callable(connected_check):
                raise ValueError('activation connected check must be callable')
            if final_check is not None and not callable(final_check):
                raise ValueError('activation final check must be callable')
            if self._receipt is None or not self._ack:
                return None
            try:
                self._live()
                self._check_observation_deadline()
                if self._return_deadline is not None:
                    # Persistence precedes the final observation, which can
                    # still be pending. Only observe() may finish that phase;
                    # a durable receipt alone cannot authorize activation.
                    return None
                if (self.return_receipt.is_symlink()
                        or self.return_receipt.read_bytes() != self._receipt):
                    raise ValueError('preparation return receipt changed')
                checked = []
                inspected = []

                def connected(row):
                    if checked:
                        raise ValueError('identity helper repeated connected inspection')
                    if (any(row.get(k) != value for k, value in self._expected.items())
                            or original(row, row['workspace_id']) != self._original):
                        raise ValueError('activation original differs from preparation')
                    data, reloads = self._events(row)
                    if connected_check is not None:
                        # Proof/permission readers can block. Run them before
                        # the actual screen read and final PID/FD/prefix check.
                        connected_check({**copy.deepcopy(row),
                            'boot_id': self._boot, 'generation': self._generation,
                            'refresh_return_observed': True,
                            'return_receipt_sha256': _sha(self._receipt)})
                    if self.authorized(self.client, row) is not True:
                        raise ValueError('activation live input authorization refused')
                    grid = self._screen(row)
                    if self.authorized(self.client, row) is not True:
                        raise ValueError('activation authorization changed during replay')
                    self._live()
                    checked.append((data, reloads, grid.signature()))
                    inspected.append(copy.deepcopy(row))

                def final():
                    if len(inspected) != 1:
                        raise ValueError('final authorization lacks original inspection')
                    if final_check is not None:
                        final_check()
                    if self.authorized(self.client, inspected[0]) is not True:
                        raise ValueError('activation authorization changed during identity read')
                    self._live()

                row = self._inspect(pending_for_activation=True,
                    connected_check=connected, final_check=final)
                if (len(checked) != 1 or row.get('startup_observed') is not True
                        or any(row.get(k) != value for k, value in self._expected.items())
                        or original(row, row['workspace_id']) != self._original):
                    raise ValueError('activation final original identity changed')
                data, reloads = self._events(row)
                if not data.startswith(checked[0][0]) or reloads != checked[0][1]:
                    raise ValueError('activation events changed during connected inspection')
                self._live()
                if (self.return_receipt.is_symlink()
                        or self.return_receipt.read_bytes() != self._receipt):
                    raise ValueError('preparation return receipt changed during inspection')
                self._prefix = data
                return {**row, 'boot_id': self._boot, 'generation': self._generation,
                    'observed_monotonic': self._now(), 'refresh_return_observed': True,
                    'refresh_success_verified': False, 'readiness_proven': False,
                    'initialized': True, 'idle': True, 'composer_empty': True,
                    'pending_approval': False, 'task_count': 0, 'user_input_count': 0,
                    'screen_signature': checked[0][2], 'preparation_control_count': 1,
                    'control_receipt_sha256': _sha(self._intent_raw),
                    'return_receipt_sha256': _sha(self._receipt)}
            except ObservationPending:
                # Validated by _inspect; provides no readiness or input
                # authority. Retain the original fixed outage deadline.
                raise
            except Exception as exc:
                self.invalidate(exc)
                raise

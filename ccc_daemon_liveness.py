"""Separate-process progress watchdog for the launchd CCC daemon only.

KeepAlive cannot detect a live, wedged interpreter. Independent progress tokens
track the scheduling loop, completed observations, and health publication.
No terminal, client process, task, delivery record or provider state is changed.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import threading
import time

STALL_SECONDS = 90.0
TERM_GRACE_SECONDS = 15.0
IDENTITY_RETRY_SECONDS = 0.25
IDENTITY_LOG_SECONDS = 30.0
MAX_STALL_SECONDS = 900.0


def retry_timeout(journal, *, now=None):
    """Back off repeated daemon stalls, independently of provider retry state."""
    now = time.time() if now is None else now
    try:
        with Path(journal).open('rb') as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 65536))
            raw = stream.read(65536)
        rows = []
        for line in raw.splitlines():
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if (isinstance(value, dict) and value.get('event') == 'stalled'
                    and type(value.get('at')) in (float, int)
                    and 0 <= now - value['at'] < 3600):
                rows.append(value)
        if not rows or now - rows[-1]['at'] >= 1800:
            return STALL_SECONDS
        return min(MAX_STALL_SECONDS, STALL_SECONDS * (2 ** min(len(rows), 4)))
    except OSError:
        return STALL_SECONDS


class ObservationProgress:
    """Consume new completion records, independently of per-target health.

    The parent and native workers already publish completion timestamps only
    after an observation returns. A health heartbeat may read those records
    repeatedly, but each timestamp can advance liveness at most once. Retain
    watermarks across removal, pause and re-enrollment of the same surface.
    """

    def __init__(self):
        self._completed = {}
        self._seen = {}
        self._now = None
        self._lock = threading.Lock()

    def advanced(self, targets, state, *, now, cadence=1, paused=False):
        with self._lock:
            # A wall-clock rollback needs a new baseline, not an indefinitely
            # unreachable old watermark or a synthetic progress heartbeat.
            rebasing = self._now is not None and now < self._now
            self._now = now
            if rebasing:
                self._completed.clear()
            active, progressed = False, False
            for target in targets:
                sid = str(target['surface_id'])
                enabled = (not paused and target.get('enabled', True)
                           and not target.get('paused', False))
                active |= enabled
                timing = state.get(sid, {}).get('observation_timings') or {}
                stamp = timing.get('completed_at', 0)
                if type(stamp) not in (int, float) or not math.isfinite(stamp):
                    continue
                changed = stamp != self._seen.get(sid)
                self._seen[sid] = stamp
                if not 0 < stamp <= now:
                    continue
                previous = self._completed.get(sid, 0)
                self._completed[sid] = max(previous, stamp)
                if (enabled and not rebasing and changed and stamp > previous
                        and now - stamp <= max(30, 3 * cadence)):
                    progressed = True
            # No observation is due while every target is paused/disabled or
            # absent. Otherwise one progressing worker keeps the daemon alive;
            # stale peers remain degraded in the unchanged health report.
            return not active or progressed


def process_birth(pid):
    from ccc_guard_scope import birth
    return birth(pid)


def record(path, **value):
    value.update(at=time.time(), watchdog_pid=os.getpid())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(value, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        print('CCC liveness evidence unavailable: ' + str(error), file=sys.stderr, flush=True)


def monitor(fd, parent, expected_birth, journal, *, timeout=STALL_SECONDS,
            grace=TERM_GRACE_SECONDS, birth=process_birth, channels=(b'M', b'H'),
            python_stacks=False):
    """Only our still-live original parent can be signalled, never a PID reuse."""
    last_identity_log = float('-inf')
    def same_parent():
        nonlocal last_identity_log
        if os.getppid() != parent or not expected_birth:
            return False
        try:
            actual = birth(parent)
        except (OSError, RuntimeError, ValueError):
            actual = None
        if actual is None:
            # An unavailable identity probe is never permission to signal.
            now = time.monotonic()
            if now - last_identity_log >= IDENTITY_LOG_SECONDS:
                record(journal, event='identity_unavailable', parent_pid=parent)
                last_identity_log = now
            time.sleep(IDENTITY_RETRY_SECONDS)
            return None
        return actual == expected_birth

    started = time.monotonic()
    progress = dict.fromkeys(channels, started)
    def read_progress(wait):
        ready, _, _ = select.select([fd], [], [], wait)
        if ready:
            data = os.read(fd, 4096)
            if not data or b'S' in data:
                return False
            now = time.monotonic()
            for token in progress:
                if token in data:
                    progress[token] = now
        return True

    state = same_parent()
    while state is None:
        if not read_progress(0):
            return 0
        state = same_parent()
    if not state:
        return 2
    progress = dict.fromkeys(progress, time.monotonic())
    degraded_at = float('-inf')
    record(journal, event='armed', parent_pid=parent, parent_birth=expected_birth,
           timeout_seconds=timeout, grace_seconds=grace)
    while True:
        state = same_parent()
        if state is False:
            return 0
        if state is None:
            if not read_progress(0):
                return 0
            continue
        if not read_progress(min(1.0, timeout / 4)):
            return 0
        now = time.monotonic()
        stalled = [token.decode() for token, stamp in progress.items() if now - stamp >= timeout]
        if not stalled:
            continue
        if stalled == ['H'] and b'O' in progress:
            # Observers and main loop are making progress, but health-file
            # publication fails (for example ENOSPC). Restart cannot fix that.
            if now - degraded_at >= timeout:
                record(journal, event='health_publication_degraded', parent_pid=parent,
                       parent_birth=expected_birth)
                degraded_at = now
            continue
        record(journal, event='stalled', parent_pid=parent, parent_birth=expected_birth,
               stalled_channels=stalled,
               ages_seconds={key.decode(): now - stamp for key, stamp in progress.items()})
        state = same_parent()
        if state is False:
            return 0
        if state is None:
            if not read_progress(0):
                return 0
            continue
        if python_stacks:
            # Only this parent's startup can enable the signal handler. Never
            # send USR1 to a legacy daemon where its default action is fatal.
            try:
                os.kill(parent, signal.SIGUSR1)
            except ProcessLookupError:
                return 0
            python_stacks = False  # At most one diagnostic per parent instance.
            record(journal, event='python_stack_requested', parent_pid=parent,
                   parent_birth=expected_birth)
            # The C handler does not need the GIL. Keep the existing shutdown
            # budget; a diagnostic is never a new liveness heartbeat.
            if not read_progress(0.2):
                return 0
            state = same_parent()
            if state is False:
                return 0
            if state is None:
                continue
        try:
            os.kill(parent, signal.SIGTERM)
        except ProcessLookupError:
            return 0
        record(journal, event='sigterm', parent_pid=parent, parent_birth=expected_birth)
        deadline = time.monotonic() + grace
        while True:
            state = same_parent()
            if state is False:
                return 0
            if state is None:
                if not read_progress(0):
                    return 0
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Shutdown closes the pipe only after the daemon finished cleanup.
            if not read_progress(min(0.2, grace / 4, remaining)):
                return 0
        try:
            os.kill(parent, signal.SIGKILL)
        except ProcessLookupError:
            record(journal, event='parent_gone_after_sigterm', parent_pid=parent,
                   parent_birth=expected_birth)
        else:
            record(journal, event='sigkill', parent_pid=parent, parent_birth=expected_birth)
        return 0


class LivenessGuard:
    def __init__(self, journal):
        self.journal = Path(journal)
        self.process = None
        self.fd = None
        self.last = {}
        self.stack_file = None

    def _register_python_stacks(self, generation):
        if (not hasattr(signal, 'SIGUSR1')
                or signal.getsignal(signal.SIGUSR1) != signal.SIG_DFL):
            return False
        path = self.journal.with_name('daemon-stack-{}-{}-{}.log'.format(
            os.getpid(), *generation))
        stream = None
        try:
            # ObservationProgress also runs in isolated native interpreters.
            # faulthandler is process-wide and must be loaded only by the guard.
            import faulthandler
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            stream = os.fdopen(fd, 'wb')
            faulthandler.register(signal.SIGUSR1, file=stream, all_threads=True)
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            if stream is not None:
                stream.close()
            record(self.journal, event='python_stack_registration_unavailable',
                   parent_pid=os.getpid(), parent_birth=generation,
                   error=str(error))
            return False
        self.stack_file = stream
        identity = os.fstat(stream.fileno())
        record(self.journal, event='python_stack_handler_registered',
               parent_pid=os.getpid(), parent_birth=generation,
               signal='SIGUSR1', stack_path=str(path),
               device=identity.st_dev, inode=identity.st_ino)
        return True

    def _close_python_stacks(self):
        if self.stack_file is not None:
            import faulthandler
            faulthandler.unregister(signal.SIGUSR1)
            self.stack_file.close()
            self.stack_file = None

    def start(self):
        generation = process_birth(os.getpid())
        if not generation:
            raise RuntimeError('cannot authenticate liveness watchdog parent')
        python_stacks = self._register_python_stacks(generation)
        read_fd, write_fd = os.pipe()
        try:
            child = subprocess.Popen([
                sys.executable, '-B', str(Path(__file__).resolve()),
                '--fd', str(read_fd), '--parent', str(os.getpid()),
                '--birth', json.dumps(generation), '--journal', str(self.journal),
                *(['--python-stacks'] if python_stacks else [])],
                pass_fds=(read_fd,), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, close_fds=True)
        except BaseException:
            os.close(write_fd)
            self._close_python_stacks()
            raise
        finally:
            os.close(read_fd)
        os.set_blocking(write_fd, False)
        self.fd, self.process = write_fd, child

    def progress(self, token):
        if token not in (b'M', b'H', b'O'):
            raise ValueError('unknown progress channel')
        if self.process is None or self.process.poll() is not None:
            # Cleanup can be blocked by the same wedged worker that killed the
            # watchdog. Exit immediately so launchd's KeepAlive starts a fresh
            # immutable runtime; never continue with an unmonitored daemon.
            os._exit(75)
        now = time.monotonic()
        if now - self.last.get(token, -100.0) < 1:
            return
        try:
            os.write(self.fd, token)
            self.last[token] = now
        except BlockingIOError:
            pass  # Never stall the work being watched.
        except BrokenPipeError:
            # The child may die after poll() but before this write. Treat this
            # race exactly like an observed watchdog exit, without cleanup.
            os._exit(75)

    def close(self):
        if self.fd is not None:
            try:
                os.write(self.fd, b'S')
            except OSError:
                pass
            os.close(self.fd)
            self.fd = None
        if self.process is not None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=2)
        self._close_python_stacks()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fd', type=int, required=True)
    parser.add_argument('--parent', type=int, required=True)
    parser.add_argument('--birth', required=True)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--python-stacks', action='store_true')
    args = parser.parse_args()
    return monitor(args.fd, args.parent, json.loads(args.birth), args.journal,
                   timeout=retry_timeout(args.journal), channels=(b'M', b'H', b'O'),
                   python_stacks=args.python_stacks)


if __name__ == '__main__':
    raise SystemExit(main())

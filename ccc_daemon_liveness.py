"""Separate-process progress watchdog for the launchd CCC daemon only.

KeepAlive cannot detect a live, wedged interpreter. Independent progress tokens
track the scheduling loop, completed observations, and health publication.
No terminal, client process, task, delivery record or provider state is changed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
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


def observations_current(targets, state, *, now, cadence=1):
    """A fresh health file alone cannot prove that observation work completed."""
    for target in targets:
        if not target.get('enabled', True) or target.get('paused', False):
            continue
        row = state.get(str(target['surface_id']), {})
        timing = row.get('observation_timings') or {}
        stamp = timing.get('completed_at', 0)
        if (type(stamp) not in (int, float) or not 0 < stamp <= now
                or now - stamp > max(30, 3 * cadence)):
            return False
    return True


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
            grace=TERM_GRACE_SECONDS, birth=process_birth, channels=(b'M', b'H')):
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

    def start(self):
        generation = process_birth(os.getpid())
        if not generation:
            raise RuntimeError('cannot authenticate liveness watchdog parent')
        read_fd, write_fd = os.pipe()
        try:
            child = subprocess.Popen([
                sys.executable, '-B', str(Path(__file__).resolve()),
                '--fd', str(read_fd), '--parent', str(os.getpid()),
                '--birth', json.dumps(generation), '--journal', str(self.journal)],
                pass_fds=(read_fd,), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, close_fds=True)
        except BaseException:
            os.close(write_fd)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fd', type=int, required=True)
    parser.add_argument('--parent', type=int, required=True)
    parser.add_argument('--birth', required=True)
    parser.add_argument('--journal', type=Path, required=True)
    args = parser.parse_args()
    return monitor(args.fd, args.parent, json.loads(args.birth), args.journal,
                   timeout=retry_timeout(args.journal), channels=(b'M', b'H', b'O'))


if __name__ == '__main__':
    raise SystemExit(main())

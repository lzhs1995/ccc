"""Fresh complete FD inventories on a private, single-reader Python process.

The reader uses its own GIL; the owning service retains admission, original
PID/session checks and final authorization. Each RPC performs both native
passes anew. A timeout or protocol failure permanently closes this instance.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time

PROTOCOL = 1
MAX_REPLY = 128 * 1024 * 1024
MAX_REQUEST = 4096


def _same_fields(value, expected):
    # JSON bool/float must never satisfy an integer protocol/PID/sequence.
    return (type(value) is dict and value.keys() == expected.keys()
            and all(type(value[key]) is type(item) and value[key] == item
                    for key, item in expected.items()))


class ProcessInventoryReader:
    """One owned child, no cache, fallback, replacement or inherited sockets."""

    def __init__(self, *, timeout=2.0):
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('invalid inventory RPC timeout')
        self.timeout = timeout
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._child = None
        self._sequence = 0
        self._buffer = bytearray()
        deadline = time.monotonic() + timeout
        try:
            self._child = subprocess.Popen(
                [sys.executable, '-I', '-S', '-B', str(Path(__file__).resolve()), '--worker'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, close_fds=True, env={}, cwd=str(Path(__file__).resolve().parent))
            os.set_blocking(self._child.stdin.fileno(), False)
            os.set_blocking(self._child.stdout.fileno(), False)
            ready = self._receive(deadline)
            if not _same_fields(ready, {'protocol': PROTOCOL, 'ready': self._child.pid}):
                raise OSError('invalid inventory worker handshake')
        except BaseException:
            self.close()
            raise

    def _check(self, deadline):
        if self._closed.is_set():
            raise OSError('inventory worker closed')
        if time.monotonic() >= deadline:
            raise TimeoutError('inventory RPC deadline elapsed')
        if self._child is None or self._child.poll() is not None:
            raise OSError('inventory worker exited')

    def _wait(self, stream, deadline, *, writing=False):
        while True:
            self._check(deadline)
            wait = min(.05, max(0.0, deadline - time.monotonic()))
            readable, writable, _ = select.select(
                [] if writing else [stream], [stream] if writing else [], [], wait)
            self._check(deadline)
            if readable or writable:
                return

    def _send(self, value, deadline):
        data = json.dumps(value, separators=(',', ':')).encode('ascii') + b'\n'
        if len(data) > MAX_REQUEST:
            raise OSError('inventory request too large')
        offset = 0
        while offset < len(data):
            self._wait(self._child.stdin, deadline, writing=True)
            try:
                count = os.write(self._child.stdin.fileno(), data[offset:])
            except BlockingIOError:
                continue
            if count <= 0:
                raise OSError('inventory request pipe closed')
            offset += count

    def _receive(self, deadline):
        while b'\n' not in self._buffer:
            self._wait(self._child.stdout, deadline)
            try:
                chunk = os.read(self._child.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            if not chunk:
                raise OSError('inventory response pipe closed')
            self._buffer.extend(chunk)
            if len(self._buffer) > MAX_REPLY:
                raise OSError('inventory response too large')
        line, _, remainder = self._buffer.partition(b'\n')
        # One outstanding RPC: extra frames cannot belong to a future call.
        if remainder:
            raise OSError('unsolicited inventory response')
        self._buffer.clear()
        result = json.loads(line)
        self._check(deadline)
        return result

    def __call__(self, pid, *, identities=False, writer_identity_only=False):
        if type(pid) is not int or not 0 < pid < 2**31:
            raise OSError('invalid process identity')
        if type(identities) is not bool or type(writer_identity_only) is not bool:
            raise ValueError('invalid inventory options')
        deadline = time.monotonic() + self.timeout
        acquired = False
        try:
            while not acquired:
                self._check(deadline)
                acquired = self._lock.acquire(timeout=min(.05, max(0.0, deadline - time.monotonic())))
            self._check(deadline)
            self._sequence += 1
            request = {'protocol': PROTOCOL, 'sequence': self._sequence,
                       'pid': pid, 'writer_identity_only': writer_identity_only}
            self._send(request, deadline)
            response = self._receive(deadline)
            if not isinstance(response, dict) or not _same_fields(response.get('request'), request):
                raise OSError('inventory response identity mismatch')
            if set(response) == {'request', 'error'}:
                error = response['error']
                if not isinstance(error, dict) or set(error) != {'type', 'errno', 'message', 'inventory_change'}:
                    raise OSError('invalid inventory error')
                from ccc_codex_queue import IncompleteVnodeRead, VnodeInventoryChanged
                kinds = {'OSError': OSError, 'IncompleteVnodeRead': IncompleteVnodeRead,
                         'VnodeInventoryChanged': VnodeInventoryChanged}
                if error['type'] not in kinds or not isinstance(error['message'], str) or (
                        error['errno'] is not None and type(error['errno']) is not int):
                    raise OSError('invalid inventory error type')
                result = kinds[error['type']](error['message']) if error['errno'] is None else kinds[error['type']](error['errno'], error['message'])
                if error['inventory_change'] is not None:
                    if not isinstance(error['inventory_change'], dict):
                        raise OSError('invalid inventory drift evidence')
                    result.inventory_change = error['inventory_change']
            else:
                if set(response) != {'request', 'files'} or not isinstance(response['files'], list):
                    raise OSError('invalid inventory result')
                result = {}
                for row in response['files']:
                    if (not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str)
                            or not Path(row[0]).is_absolute() or '\x00' in row[0]
                            or any(type(v) is not int or v < 0 for v in row[1:])):
                        raise OSError('invalid inventory writer')
                    path = Path(row[0])
                    if path in result:
                        raise OSError('duplicate inventory writer')
                    result[path] = {'device': row[1], 'inode': row[2]}
                if not identities:
                    result = set(result)
            self._check(deadline)
        except BaseException:
            # Cancellation/timeouts/unknown outcomes never reuse the channel.
            self._closed.set()
            raise
        finally:
            if acquired:
                self._lock.release()
            if self._closed.is_set():
                self.close()
        # A complete negative observation can be retried by the original
        # caller's existing policy. Protocol/transport failures above cannot.
        if isinstance(result, OSError):
            raise result
        return result

    def close(self):
        self._closed.set()
        with self._lock:
            child = self._child
            if child is None:
                return
            try:
                if child.poll() is None:
                    try:
                        child.terminate()
                    except ProcessLookupError:
                        pass  # Reap a child that exited during the signal.
                    try:
                        child.wait(timeout=.25)
                    except subprocess.TimeoutExpired:
                        try:
                            child.kill()
                        except ProcessLookupError:
                            pass
                        child.wait(timeout=.25)
            finally:
                try:
                    child.stdin.close()
                finally:
                    child.stdout.close()
                    self._buffer.clear()
                    # Retain an unreaped child for a later explicit close.
                    # No caller can submit another RPC after cancellation.
                    if child.poll() is not None:
                        self._child = None


def _worker(read, input_stream, output_stream):
    def write(value):
        data = json.dumps(value, separators=(',', ':')).encode('ascii') + b'\n'
        if len(data) > MAX_REPLY:
            raise OSError('inventory response too large')
        output_stream.write(data)
        output_stream.flush()
    write({'protocol': PROTOCOL, 'ready': os.getpid()})
    sequence = 0
    while True:
        line = input_stream.readline(MAX_REQUEST + 1)
        if not line:
            return
        if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
            raise OSError('invalid inventory request framing')
        request = json.loads(line)
        sequence += 1
        if (not isinstance(request, dict) or set(request) != {'protocol', 'sequence', 'pid', 'writer_identity_only'}
                or type(request['protocol']) is not int or request['protocol'] != PROTOCOL
                or type(request['sequence']) is not int or request['sequence'] != sequence
                or type(request['pid']) is not int or not 0 < request['pid'] < 2**31
                or type(request['writer_identity_only']) is not bool):
            raise OSError('invalid inventory request identity')
        try:
            value = read(request['pid'], identities=True,
                         writer_identity_only=request['writer_identity_only'])
            response = {'request': request, 'files': [[str(path), identity['device'], identity['inode']]
                        for path, identity in value.items()]}
        except OSError as error:
            kind = type(error).__name__
            if kind not in {'IncompleteVnodeRead', 'VnodeInventoryChanged'}:
                kind = 'OSError'
            response = {'request': request, 'error': {'type': kind, 'errno': error.errno,
                'message': error.strerror or str(error),
                'inventory_change': getattr(error, 'inventory_change', None)}}
        write(response)


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('private inventory worker requires --worker')
    # -I -S avoids inherited PYTHONPATH/site hooks; only this exact sibling
    # module supplies the same native reader as the owning candidate.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ccc_codex_queue import process_writable_files
    _worker(process_writable_files, sys.stdin.buffer, sys.stdout.buffer)

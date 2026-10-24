"""Same-release, bounded client of the existing Codex control socket.

Never starts a daemon, changes a session, reads credentials or sends terminal keys.
The caller journals its one input operation before calling request(). A timeout is
ambiguous and MUST NOT trigger a retry. Native user evidence is checked separately.
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time

DEFAULT_SOCKET = Path.home() / '.codex/app-server-control/app-server-control.sock'
MAX_MESSAGE = 16 * 1024 * 1024


class RPCError(RuntimeError):
    def __init__(self, method, error):
        self.method = method
        self.error = error
        super().__init__(method + ': ' + json.dumps(error, ensure_ascii=False))


class DaemonRPC:
    def __init__(self, socket_path=DEFAULT_SOCKET, timeout=20.0):
        self.path = Path(socket_path)
        self.timeout = float(timeout)
        if not 0 < self.timeout <= 120:
            raise ValueError('timeout must be >0 and <=120 seconds')
        resolved = self.path.resolve(strict=True)
        st = resolved.stat()
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise PermissionError('control socket must be current-user-owned mode 0600')
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._deadline = time.monotonic() + self.timeout
        self._buffer = bytearray()
        self._id = 0
        try:
            self._set_timeout()
            self.sock.connect(str(resolved))
            # Authenticate the actual Unix peer, not merely a socket filename.
            if hasattr(socket, 'SO_PEERCRED'):
                _, peer_uid, _ = struct.unpack('3i', self.sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
            else:
                uid, gid = ctypes.c_uint(), ctypes.c_uint()
                fn = ctypes.CDLL(None, use_errno=True).getpeereid
                fn.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
                fn.restype = ctypes.c_int
                if fn(self.sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
                    raise OSError(ctypes.get_errno(), 'getpeereid failed')
                peer_uid = uid.value
            if peer_uid != os.geteuid():
                raise PermissionError('daemon peer uid differs from current user')
            current = resolved.stat()
            if (st.st_dev, st.st_ino, st.st_uid, st.st_mode) != (
                    current.st_dev, current.st_ino, current.st_uid, current.st_mode):
                raise PermissionError('control socket changed during connection')
            self.peer_uid = peer_uid
            self.resolved_socket = str(resolved)
            self.socket_identity = dict(path=str(resolved), device=st.st_dev,
                inode=st.st_ino, uid=st.st_uid, mode=stat.S_IMODE(st.st_mode),
                peer_uid=peer_uid)
            self._upgrade()
            self.initialize = self.request('initialize', {
                'clientInfo': {'name': 'cmux_native_input', 'title': 'Verified Codex Input', 'version': '1.0'},
                'capabilities': {'experimentalApi': True},
            })
            self.notify('initialized')
        except BaseException:
            self.sock.close()
            raise

    def _set_timeout(self):
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('Codex control API request deadline exceeded')
        self.sock.settimeout(left)

    def _read(self, length):
        while len(self._buffer) < length:
            self._set_timeout()
            chunk = self.sock.recv(min(65536, length - len(self._buffer)))
            if not chunk:
                raise ConnectionError('Codex control socket closed')
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:length])
        del self._buffer[:length]
        return result

    def _upgrade(self):
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = ('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                   'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                   f'Sec-WebSocket-Key: {key}\r\n\r\n').encode('ascii')
        self._set_timeout()
        self.sock.sendall(request)
        header = bytearray()
        while not header.endswith(b'\r\n\r\n'):
            header.extend(self._read(1))
            if len(header) > 16384:
                raise ValueError('oversized WebSocket upgrade response')
        lines = header.decode('ascii').split('\r\n')
        if len(lines[0].split()) < 2 or lines[0].split()[1] != '101':
            raise ConnectionError(f'WebSocket upgrade rejected: {lines[0]}')
        headers = {name.lower(): value.strip() for line in lines[1:] if ':' in line for name, value in [line.split(':', 1)]}
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
        if headers.get('sec-websocket-accept') != expected or headers.get('upgrade', '').lower() != 'websocket':
            raise ConnectionError('invalid WebSocket upgrade proof')

    def _send_frame(self, payload, opcode=1):
        length = len(payload)
        if length > MAX_MESSAGE:
            raise ValueError('outgoing message too large')
        mask = os.urandom(4)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0xFE]) + struct.pack('!H', length)
        else:
            header = bytes([0x80 | opcode, 0xFF]) + struct.pack('!Q', length)
        masked = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        self._set_timeout()
        self.sock.sendall(header + mask + masked)

    def _receive_json(self):
        message = bytearray()
        started = False
        while True:
            first, second = self._read(2)
            final, opcode = bool(first & 0x80), first & 0x0F
            if first & 0x70 or second & 0x80:
                raise ValueError('unexpected reserved flags or masked server frame')
            length = second & 0x7F
            if length == 126:
                length = struct.unpack('!H', self._read(2))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._read(8))[0]
            if length > MAX_MESSAGE or len(message) + length > MAX_MESSAGE:
                raise ValueError('incoming message too large')
            payload = self._read(length)
            if opcode == 8:
                raise ConnectionError('Codex sent WebSocket close')
            if opcode in (9, 10):
                if not final or length > 125:
                    raise ValueError('invalid WebSocket control frame')
                if opcode == 9:
                    self._send_frame(payload, 10)
                continue
            if opcode not in (0, 1) or (opcode == 0 and not started) or (opcode == 1 and started):
                raise ValueError('unexpected WebSocket message sequence')
            started = True
            message.extend(payload)
            if final:
                return json.loads(message.decode('utf-8'))

    def notify(self, method, params=None):
        value = {'method': method}
        if params is not None:
            value['params'] = params
        self._send_frame(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))

    def request(self, method, params=None):
        self._deadline = time.monotonic() + self.timeout
        self._id += 1
        value = {'id': self._id, 'method': method}
        if params is not None:
            value['params'] = params
        self._send_frame(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        while True:
            message = self._receive_json()
            if not isinstance(message, dict):
                raise ValueError('RPC envelope must be an object')
            # Server requests have their own id namespace. Never accept one
            # as our reply, and never grant an approval on this input channel.
            if 'method' in message:
                continue
            if message.get('id') != self._id:
                continue
            if 'error' in message:
                raise RPCError(method, message['error'])
            if 'result' not in message:
                raise ValueError('RPC reply has neither result nor error')
            return message['result']

    def close(self):
        try:
            self._deadline = time.monotonic() + 1
            self._send_frame(struct.pack('!H', 1000), 8)
        except (OSError, TimeoutError):
            pass
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def request(method, params=None, *, timeout=20.0, socket_path=DEFAULT_SOCKET):
    with DaemonRPC(socket_path=socket_path, timeout=timeout) as client:
        return client.request(method, params)


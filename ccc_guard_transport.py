"""Small RFC 6455 transport for Codex's private Unix socket protocol.

No network listener, compression, authentication material, or HTTP proxying.
The caller owns a mode-0700 directory and a mode-0600 Unix socket. Fragmented
text messages and control frames are supported; all allocation is bounded.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct

MAX_MESSAGE = 16 * 1024 * 1024
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocket:
    def __init__(self, reader, writer, *, client=False):
        self.reader, self.writer, self.client = reader, writer, client
        self.closed = False

    @classmethod
    async def accept(cls, reader, writer):
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        if len(header) > 16384:
            raise ValueError("oversized websocket handshake")
        lines = header.decode("ascii").split("\r\n")
        if lines[0] not in {"GET / HTTP/1.1", "GET /rpc HTTP/1.1"}:
            raise ValueError("only the native websocket endpoint is supported")
        fields = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
        fields = {k.lower(): v.strip() for k, v in fields.items()}
        key = fields.get("sec-websocket-key", "")
        if (fields.get("upgrade", "").lower() != "websocket"
                or fields.get("sec-websocket-version") != "13"
                or "upgrade" not in fields.get("connection", "").lower()
                or len(base64.b64decode(key, validate=True)) != 16):
            raise ValueError("invalid websocket handshake")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
        await writer.drain()
        return cls(reader, writer)

    @classmethod
    async def connect(cls, path):
        reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_MESSAGE)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write(("GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                      "Sec-WebSocket-Key: " + key + "\r\n\r\n").encode())
        await writer.drain()
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest())
        if not header.startswith(b"HTTP/1.1 101 ") or accept not in header:
            writer.close()
            raise ValueError("websocket upgrade rejected")
        return cls(reader, writer, client=True)

    def write(self, data, opcode=1):
        if self.closed:
            return
        data = data.encode() if isinstance(data, str) else data
        if len(data) > MAX_MESSAGE:
            raise ValueError("oversized websocket message")
        n, mask_bit = len(data), 128 if self.client else 0
        header = bytes([128 | opcode])
        header += (bytes([mask_bit | n]) if n < 126 else
                   bytes([mask_bit | 126]) + struct.pack("!H", n) if n < 65536 else
                   bytes([mask_bit | 127]) + struct.pack("!Q", n))
        if self.client:
            mask = os.urandom(4)
            header += mask
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.writer.write(header + data)

    async def recv(self):
        parts, total, fragmented = [], 0, False
        while not self.closed:
            first, second = await self.reader.readexactly(2)
            final, opcode, masked = bool(first & 128), first & 15, bool(second & 128)
            if first & 112 or masked == self.client:
                raise ValueError("invalid websocket flags")
            n = second & 127
            if n == 126:
                n = struct.unpack("!H", await self.reader.readexactly(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
            if n > MAX_MESSAGE or (opcode >= 8 and (not final or n > 125)):
                raise ValueError("invalid websocket frame length")
            mask = await self.reader.readexactly(4) if masked else b""
            data = await self.reader.readexactly(n)
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 8:
                self.close()
                return None
            if opcode == 9:
                self.write(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in {0, 1} or (opcode == 0) != fragmented:
                raise ValueError("invalid websocket fragmentation")
            total += n
            if total > MAX_MESSAGE:
                raise ValueError("oversized fragmented websocket message")
            parts.append(data)
            if final:
                return b"".join(parts).decode("utf-8")
            fragmented = True
        return None

    def close(self):
        if not self.closed:
            self.write(b"", 8)
            self.closed = True
            self.writer.close()

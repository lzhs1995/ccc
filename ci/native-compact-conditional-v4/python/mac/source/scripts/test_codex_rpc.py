"""Offline tests for cmux_codex_rpc; run this file alone, not suite discovery.

Put the repository's scripts directory on PYTHONPATH, or move this file into
scripts. Use Python -B to avoid writing bytecode into the shared repository.
Only the standard library is required. Every constructor receives an explicit
fake path; real socket creation is blocked. No daemon, credentials, temporary
socket, sleep, or subprocess is used.

The method-bearing ID collision test is an ordinary regression test, not an
expected failure: it should fail until the corresponding RPC fix is present.
"""

from __future__ import annotations

import base64
from collections import deque
from contextlib import ExitStack, contextmanager
import ctypes
import errno
import hashlib
import json
import socket
import stat
import struct
from types import SimpleNamespace
import unittest
from unittest import mock

import cmux_codex_rpc as rpc


FAKE_PATH = "/offline-codex-rpc-tests/control.sock"
UID = 4242
PEERCRED_OPTION = 12345
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
TURN_ID = "019d0000-0000-7000-8000-000000000002"
INPUT_PARAMS = {
    "threadId": "019d0000-0000-7000-8000-000000000001",
    "expectedTurnId": TURN_ID,
    "input": [{"type": "text", "text": "离线回执  two spaces\n下一行\t\\literal"}],
}


def server_frame(payload, *, opcode=1, final=True):
    """Build an unmasked server frame using the RFC 6455 wire format."""
    first = opcode | (0x80 if final else 0)
    size = len(payload)
    if size < 126:
        header = bytes((first, size))
    elif size < 65536:
        header = bytes((first, 126)) + size.to_bytes(2, "big")
    else:
        header = bytes((first, 127)) + size.to_bytes(8, "big")
    return header + payload


def json_frame(value):
    return server_frame(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def client_frame(raw):
    """Decode and independently check one complete masked client frame."""
    if len(raw) < 6 or raw[0] & 0x70 or not raw[0] & 0x80 or not raw[1] & 0x80:
        raise AssertionError("expected a final, masked, unextended client frame")
    size, offset = raw[1] & 0x7F, 2
    if size == 126:
        size, offset = int.from_bytes(raw[2:4], "big"), 4
    elif size == 127:
        size, offset = int.from_bytes(raw[2:10], "big"), 10
    if len(raw) != offset + 4 + size:
        raise AssertionError("client frame length does not match its header")
    mask = raw[offset:offset + 4]
    payload = bytes(byte ^ mask[index % 4]
                    for index, byte in enumerate(raw[offset + 4:]))
    return raw[0] & 0x0F, payload


def sent_messages(fake):
    messages = []
    for raw in fake.writes:
        if raw.startswith(b"GET "):
            continue
        opcode, payload = client_frame(raw)
        if opcode == 1:
            messages.append(json.loads(payload))
    return messages


def socket_stat(**overrides):
    values = dict(st_dev=7, st_ino=123, st_uid=UID,
                  st_mode=stat.S_IFSOCK | 0o600)
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeSocket:
    def __init__(self, incoming=b"", *, chunk_size=None, clock=None,
                 recv_step=0.0, peer_uid=UID):
        self.incoming = bytearray(incoming)
        self.chunk_size = chunk_size
        self.clock, self.recv_step = clock, recv_step
        self.peer_uid = peer_uid
        self.writes, self.connections, self.timeouts, self.peer_calls = [], [], [], []
        self.closed, self.close_count, self.recv_count = False, 0, 0
        self.on_send = None
        self.connect_error = self.peer_error = self.exhausted_error = None

    def connect(self, path):
        if path != FAKE_PATH:
            raise AssertionError("only the explicit fake path may be connected")
        self.connections.append(path)
        if self.connect_error is not None:
            raise self.connect_error

    def settimeout(self, seconds):
        self.timeouts.append(seconds)

    def getsockopt(self, *args):
        self.peer_calls.append(args)
        if self.peer_error is not None:
            raise self.peer_error
        return struct.pack("3i", 999, self.peer_uid, UID)

    def fileno(self):
        return 42424  # Never passed to a real OS call.

    def sendall(self, raw):
        if self.closed:
            raise AssertionError("send after close")
        self.writes.append(bytes(raw))
        if self.on_send is not None:
            self.on_send(bytes(raw), self)

    def recv(self, size):
        self.recv_count += 1
        if self.clock is not None:
            self.clock.advance(self.recv_step)
        if not self.incoming:
            if self.exhausted_error is not None:
                raise self.exhausted_error
            return b""
        count = min(size, len(self.incoming), self.chunk_size or size)
        result = bytes(self.incoming[:count])
        del self.incoming[:count]
        return result

    def close(self):
        self.closed = True
        self.close_count += 1


class FakePath:
    def __init__(self, snapshots):
        self.snapshots = deque(snapshots)
        self.resolve_calls = []

    def resolve(self, *, strict):
        self.resolve_calls.append(strict)
        return self

    def stat(self):
        if not self.snapshots:
            raise AssertionError("unexpected additional socket stat")
        result = self.snapshots.popleft()
        if isinstance(result, BaseException):
            raise result
        return result

    def __str__(self):
        return FAKE_PATH


def upgrade_reply(request, *, status="101 Switching Protocols",
                  accept_override=None, upgrade="websocket"):
    headers = dict(line.split(":", 1) for line in request.decode("ascii").split("\r\n")
                   if ":" in line)
    key = headers["Sec-WebSocket-Key"].strip()
    accept = base64.b64encode(hashlib.sha1(
        (key + WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
    if accept_override is not None:
        accept = accept_override
    return (f"HTTP/1.1 {status}\r\nUpgrade: {upgrade}\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii")


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(rpc.time, "monotonic", self.clock.monotonic))
        self.stack.enter_context(mock.patch.object(
            socket, "socket", side_effect=AssertionError("real socket creation forbidden")))

    def bare_client(self, fake=None, *, timeout=2.0):
        """Exercise wire methods without calling any connection constructor."""
        client = object.__new__(rpc.DaemonRPC)
        client.sock = fake if fake is not None else FakeSocket()
        client.timeout = timeout
        client._deadline = self.clock.now + timeout
        client._buffer = bytearray()
        client._id = 0
        return client

    @contextmanager
    def constructor(self, *, platform="linux", fake=None, snapshots=None,
                    peer_uid=UID, peer_errno=0, response_handler=None,
                    upgrade_options=None, raw_upgrade=None, initialize_error=None):
        """Fake filesystem metadata, socket factory, and both peer-ID APIs."""
        fake = fake if fake is not None else FakeSocket(peer_uid=peer_uid)
        path = FakePath(snapshots if snapshots is not None
                        else [socket_stat(), socket_stat()])
        factory = mock.Mock(return_value=fake)
        socket_api = SimpleNamespace(AF_UNIX=socket.AF_UNIX, SOCK_STREAM=socket.SOCK_STREAM,
                                     SOL_SOCKET=socket.SOL_SOCKET, socket=factory)
        bsd_calls = []

        def getpeereid(fd, uid_pointer, gid_pointer):
            bsd_calls.append(fd)
            ctypes.cast(uid_pointer, ctypes.POINTER(ctypes.c_uint))[0] = peer_uid
            ctypes.cast(gid_pointer, ctypes.POINTER(ctypes.c_uint))[0] = UID
            return -1 if peer_errno else 0

        def on_send(raw, target):
            if raw.startswith(b"GET "):
                reply = raw_upgrade if raw_upgrade is not None else upgrade_reply(
                    raw, **(upgrade_options or {}))
                target.incoming.extend(reply)
                return
            opcode, payload = client_frame(raw)
            if opcode != 1:
                return
            message = json.loads(payload)
            if message.get("method") == "initialize":
                reply = {"id": message["id"]}
                if initialize_error is None:
                    reply["result"] = {"userAgent": "offline-test"}
                else:
                    reply["error"] = initialize_error
                target.incoming.extend(json_frame(reply))
            elif "id" in message and response_handler is not None:
                response_handler(message, target)

        fake.on_send = on_send
        with ExitStack() as stack:
            path_factory = stack.enter_context(mock.patch.object(rpc, "Path", return_value=path))
            stack.enter_context(mock.patch.object(rpc, "socket", socket_api))
            stack.enter_context(mock.patch.object(rpc.os, "geteuid", return_value=UID))
            libc = stack.enter_context(mock.patch.object(
                rpc.ctypes, "CDLL", return_value=SimpleNamespace(getpeereid=getpeereid)))
            stack.enter_context(mock.patch.object(rpc.ctypes, "get_errno", return_value=peer_errno))
            if platform == "linux":
                socket_api.SO_PEERCRED = PEERCRED_OPTION
            elif platform != "bsd":
                raise AssertionError("unknown fake peer API")
            yield SimpleNamespace(fake=fake, factory=factory, path=path,
                                  path_factory=path_factory, libc=libc, bsd_calls=bsd_calls)


class PeerAndConstructorTests(OfflineCase):
    def test_linux_peer_authentication_and_socket_identity(self):
        with self.constructor() as env:
            with rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2) as client:
                self.assertEqual(client.peer_uid, UID)
                self.assertEqual(client.socket_identity,
                                 dict(path=FAKE_PATH, device=7, inode=123,
                                      uid=UID, mode=0o600, peer_uid=UID))
                self.assertEqual(client.initialize, {"userAgent": "offline-test"})
            env.path_factory.assert_called_once_with(FAKE_PATH)
            self.assertEqual(env.path.resolve_calls, [True])
            self.assertEqual(env.fake.peer_calls,
                             [(socket.SOL_SOCKET, PEERCRED_OPTION, struct.calcsize("3i"))])
            env.libc.assert_not_called()
            self.assertEqual(env.fake.connections, [FAKE_PATH])
            self.assertEqual([item["method"] for item in sent_messages(env.fake)],
                             ["initialize", "initialized"])
            self.assertEqual(env.fake.close_count, 1)

    def test_bsd_getpeereid_authentication(self):
        with self.constructor(platform="bsd") as env:
            with rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2) as client:
                self.assertEqual(client.socket_identity["peer_uid"], UID)
            self.assertEqual(env.bsd_calls, [env.fake.fileno()])
            self.assertEqual(env.fake.peer_calls, [])
            env.libc.assert_called_once_with(None, use_errno=True)
            self.assertEqual(env.fake.close_count, 1)

    def test_rejects_non_socket_wrong_owner_and_group_or_other_permissions(self):
        cases = {
            "regular file": dict(st_mode=stat.S_IFREG | 0o600),
            "wrong owner": dict(st_uid=UID + 1),
            "group read": dict(st_mode=stat.S_IFSOCK | 0o640),
            "group write": dict(st_mode=stat.S_IFSOCK | 0o620),
            "other read": dict(st_mode=stat.S_IFSOCK | 0o604),
            "other write": dict(st_mode=stat.S_IFSOCK | 0o602),
        }
        for name, changes in cases.items():
            with self.subTest(name=name), self.constructor(
                    snapshots=[socket_stat(**changes)]) as env:
                with self.assertRaises(PermissionError):
                    rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2)
                env.factory.assert_not_called()
                self.assertEqual(env.fake.writes, [])

    def test_rejects_authenticated_peer_uid_mismatch_before_upgrade(self):
        for platform in ("linux", "bsd"):
            with self.subTest(platform=platform), self.constructor(
                    platform=platform, peer_uid=UID + 1) as env:
                with self.assertRaisesRegex(PermissionError, "peer uid"):
                    rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2)
                self.assertEqual(env.fake.writes, [])
                self.assertEqual(env.fake.close_count, 1)

    def test_peer_credential_api_errors_close_without_upgrade(self):
        for platform in ("linux", "bsd"):
            fake = FakeSocket()
            if platform == "linux":
                fake.peer_error = OSError(errno.EACCES, "fake peer lookup failed")
            with self.subTest(platform=platform), self.constructor(
                    platform=platform, fake=fake, peer_errno=errno.EACCES) as env:
                with self.assertRaises(OSError) as caught:
                    rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2)
                self.assertEqual(caught.exception.errno, errno.EACCES)
                self.assertEqual(env.fake.writes, [])
                self.assertEqual(env.fake.close_count, 1)

    def test_socket_identity_drift_aborts_before_upgrade(self):
        for field, replacement in (("st_dev", 8), ("st_ino", 124),
                                   ("st_uid", UID + 1), ("st_mode", stat.S_IFSOCK | 0o640)):
            with self.subTest(field=field), self.constructor(
                    snapshots=[socket_stat(), socket_stat(**{field: replacement})]) as env:
                with self.assertRaisesRegex(PermissionError, "changed during connection"):
                    rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2)
                self.assertEqual(env.fake.writes, [])
                self.assertEqual(env.fake.close_count, 1)

    def test_socket_disappearance_after_connect_closes_without_upgrade(self):
        with self.constructor(snapshots=[socket_stat(), FileNotFoundError("removed")]) as env:
            with self.assertRaises(FileNotFoundError):
                rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2)
            self.assertEqual(env.fake.writes, [])
            self.assertEqual(env.fake.close_count, 1)

    def test_connect_failure_closes_and_does_not_retry(self):
        fake = FakeSocket()
        fake.connect_error = ConnectionRefusedError("fake refused")
        with self.constructor(fake=fake) as env:
            with self.assertRaises(ConnectionRefusedError):
                rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2)
            env.factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
            self.assertEqual(fake.connections, [FAKE_PATH])
            self.assertEqual(fake.writes, [])
            self.assertEqual(fake.close_count, 1)

    def test_invalid_timeouts_rejected_before_filesystem_or_socket_access(self):
        for timeout in (0, -1, 121, float("inf"), float("nan"), "invalid"):
            with self.subTest(timeout=timeout), self.constructor() as env:
                with self.assertRaises(ValueError):
                    rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=timeout)
                self.assertEqual(env.path.resolve_calls, [])
                env.factory.assert_not_called()

    def test_invalid_upgrade_status_accept_or_protocol_closes(self):
        for options in ({"status": "200 OK"}, {"accept_override": "wrong-proof"},
                        {"upgrade": "not-websocket"}):
            with self.subTest(options=options), self.constructor(upgrade_options=options) as env:
                with self.assertRaises(ConnectionError):
                    rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2)
                self.assertEqual(sent_messages(env.fake), [])
                self.assertEqual(env.fake.close_count, 1)

    def test_oversized_upgrade_header_rejected(self):
        raw = b"HTTP/1.1 101 Switching Protocols\r\nX-Fill: " + b"x" * 16400 + b"\r\n\r\n"
        with self.constructor(raw_upgrade=raw) as env:
            with self.assertRaisesRegex(ValueError, "oversized WebSocket upgrade"):
                rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2)
            self.assertEqual(sent_messages(env.fake), [])
            self.assertEqual(env.fake.close_count, 1)

    def test_initialize_error_closes_before_initialized_notification(self):
        error = {"code": -32602, "message": "fake initialization rejection"}
        with self.constructor(initialize_error=error) as env:
            with self.assertRaises(rpc.RPCError) as caught:
                rpc.DaemonRPC(socket_path=FAKE_PATH, timeout=2)
            self.assertEqual(caught.exception.method, "initialize")
            self.assertEqual(caught.exception.error, error)
            self.assertEqual([item["method"] for item in sent_messages(env.fake)], ["initialize"])
            self.assertEqual(env.fake.close_count, 1)


class FramingTests(OfflineCase):
    def test_masked_client_frame_matches_rfc_hello_vector(self):
        client = self.bare_client()
        with mock.patch.object(rpc.os, "urandom", return_value=bytes.fromhex("37fa213d")):
            client._send_frame(b"Hello")
        self.assertEqual(client.sock.writes, [bytes.fromhex("818537fa213d7f9f4d5158")])

    def test_client_length_boundaries_and_payload_roundtrip(self):
        for size, marker in ((0, 0), (125, 125), (126, 126), (65535, 126), (65536, 127)):
            with self.subTest(size=size):
                client = self.bare_client()
                payload = bytes(index % 251 for index in range(size))
                client._send_frame(payload)
                self.assertEqual(len(client.sock.writes), 1)
                raw = client.sock.writes[0]
                self.assertEqual(raw[1] & 0x7F, marker)
                self.assertEqual(client_frame(raw), (1, payload))

    def test_incoming_length_boundaries_with_short_tcp_reads(self):
        for size in (125, 126, 65535, 65536):
            with self.subTest(size=size):
                payload = b'"' + b"x" * (size - 2) + b'"'
                fake = FakeSocket(server_frame(payload), chunk_size=97)
                self.assertEqual(self.bare_client(fake)._receive_json(), "x" * (size - 2))
                self.assertEqual(fake.incoming, b"")

    def test_fragmented_utf8_with_interleaved_ping_and_pong(self):
        expected = {"result": "原文  空格\n换行\t\\literal"}
        payload = json.dumps(expected, ensure_ascii=False).encode("utf-8")
        split = payload.index("原".encode("utf-8")) + 1
        wire = (server_frame(payload[:split], final=False)
                + server_frame(b"ping-bytes", opcode=9)
                + server_frame(b"unrelated-pong", opcode=10)
                + server_frame(payload[split:], opcode=0))
        fake = FakeSocket(wire, chunk_size=1)
        self.assertEqual(self.bare_client(fake)._receive_json(), expected)
        self.assertEqual([client_frame(raw) for raw in fake.writes], [(10, b"ping-bytes")])

    def test_back_to_back_messages_remain_separate(self):
        fake = FakeSocket(json_frame({"first": 1}) + json_frame({"second": 2}), chunk_size=3)
        client = self.bare_client(fake)
        self.assertEqual(client._receive_json(), {"first": 1})
        self.assertEqual(client._receive_json(), {"second": 2})
        self.assertEqual(fake.incoming, b"")

    def test_rejects_reserved_masked_and_invalid_fragment_or_control_frames(self):
        cases = {
            "reserved bits": bytes.fromhex("c100"),
            "masked server": bytes.fromhex("8180"),
            "orphan continuation": server_frame(b"{}", opcode=0),
            "second text fragment": server_frame(b"{", final=False) + server_frame(b"}"),
            "binary": server_frame(b"{}", opcode=2),
            "reserved opcode": server_frame(b"{}", opcode=3),
            "fragmented ping": server_frame(b"p", opcode=9, final=False),
            "oversized ping": server_frame(b"p" * 126, opcode=9),
            "oversized pong": server_frame(b"p" * 126, opcode=10),
        }
        for name, wire in cases.items():
            with self.subTest(name=name):
                fake = FakeSocket(wire)
                with self.assertRaises(ValueError):
                    self.bare_client(fake)._receive_json()
                self.assertEqual(fake.writes, [])

    def test_incoming_limit_rejects_declared_size_before_reading_payload(self):
        for header in (b"\x81\x09", b"\x81\x7e\x00\x09",
                       b"\x81\x7f" + (9).to_bytes(8, "big")):
            with self.subTest(header=header), mock.patch.object(rpc, "MAX_MESSAGE", 8):
                with self.assertRaisesRegex(ValueError, "incoming message too large"):
                    self.bare_client(FakeSocket(header))._receive_json()

    def test_incoming_limit_applies_to_complete_fragmented_message(self):
        wire = server_frame(b'"abcd', final=False) + server_frame(b'efg"', opcode=0)
        with mock.patch.object(rpc, "MAX_MESSAGE", 8):
            with self.assertRaisesRegex(ValueError, "incoming message too large"):
                self.bare_client(FakeSocket(wire))._receive_json()

    def test_outgoing_limit_prevents_any_write(self):
        client = self.bare_client()
        with mock.patch.object(rpc, "MAX_MESSAGE", 8):
            with self.assertRaisesRegex(ValueError, "outgoing message too large"):
                client._send_frame(b"x" * 9)
        self.assertEqual(client.sock.writes, [])

    def test_invalid_json_or_utf8_fails_without_response_or_retry(self):
        for payload in (b"not json", b'"\xff"'):
            with self.subTest(payload=payload):
                fake = FakeSocket(server_frame(payload))
                with self.assertRaises(ValueError):
                    self.bare_client(fake)._receive_json()
                self.assertEqual(fake.writes, [])

    def test_close_eof_and_truncated_length_raise_connection_error(self):
        for wire in (b"", b"\x81", b"\x81\x7e\x00",
                     server_frame(struct.pack("!H", 1000), opcode=8)):
            with self.subTest(wire=wire):
                fake = FakeSocket(wire, chunk_size=1)
                with self.assertRaises(ConnectionError):
                    self.bare_client(fake)._receive_json()
                self.assertEqual(fake.writes, [])


class RequestAndDeadlineTests(OfflineCase):
    def test_request_preserves_payload_and_skips_notifications_and_other_ids(self):
        wire = (json_frame({"method": "thread/status/changed", "params": {}})
                + json_frame({"id": 99, "result": {"wrong": True}})
                + json_frame({"id": 1, "result": {"turnId": TURN_ID}}))
        fake = FakeSocket(wire, chunk_size=5)
        result = self.bare_client(fake).request("turn/steer", INPUT_PARAMS)
        self.assertEqual(result, {"turnId": TURN_ID})
        self.assertEqual(sent_messages(fake),
                         [{"id": 1, "method": "turn/steer", "params": INPUT_PARAMS}])

    def test_server_request_id_collision_does_not_consume_the_real_reply(self):
        wire = (json_frame({"method": "thread/status/changed", "params": {}})
                + json_frame({"id": 1, "method": "item/commandExecution/requestApproval",
                              "params": {"threadId": INPUT_PARAMS["threadId"]}})
                + json_frame({"id": 1, "result": {"turnId": TURN_ID}}))
        fake = FakeSocket(wire, chunk_size=7)
        result = self.bare_client(fake).request("turn/steer", INPUT_PARAMS)
        self.assertEqual(result, {"turnId": TURN_ID})
        self.assertEqual(sent_messages(fake),
                         [{"id": 1, "method": "turn/steer", "params": INPUT_PARAMS}])
        self.assertEqual(fake.incoming, b"")

    def test_rpc_error_retains_method_and_error_without_retry(self):
        error = {"code": -32000, "message": "turn 已结束", "data": {"retry": False}}
        fake = FakeSocket(json_frame({"id": 1, "error": error}))
        with self.assertRaises(rpc.RPCError) as caught:
            self.bare_client(fake).request("turn/steer", INPUT_PARAMS)
        self.assertEqual(caught.exception.method, "turn/steer")
        self.assertEqual(caught.exception.error, error)
        self.assertIn("turn 已结束", str(caught.exception))
        self.assertEqual(len(sent_messages(fake)), 1)

    def test_socket_timeout_after_send_never_resends(self):
        fake = FakeSocket()
        fake.exhausted_error = TimeoutError("fake lost reply")
        with self.assertRaises(TimeoutError):
            self.bare_client(fake).request("turn/steer", INPUT_PARAMS)
        self.assertEqual(sent_messages(fake),
                         [{"id": 1, "method": "turn/steer", "params": INPUT_PARAMS}])

    def test_continuous_notifications_do_not_extend_absolute_deadline(self):
        notification = json_frame({"method": "thread/status/changed", "params": {}})
        fake = FakeSocket(notification * 100, clock=self.clock, recv_step=0.125)
        started = self.clock.now
        with self.assertRaisesRegex(TimeoutError, "deadline exceeded"):
            self.bare_client(fake, timeout=0.5).request("turn/steer", INPUT_PARAMS)
        self.assertEqual(self.clock.now - started, 0.5)
        self.assertEqual(len(sent_messages(fake)), 1)
        self.assertTrue(fake.incoming)  # Deadline, not exhausted test data, stopped the loop.
        self.assertTrue(all(0 < value <= 0.5 for value in fake.timeouts))
        self.assertEqual(fake.timeouts, sorted(fake.timeouts, reverse=True))

    def test_expired_deadline_prevents_frame_send(self):
        client = self.bare_client(timeout=0.5)
        self.clock.advance(0.5)
        with self.assertRaisesRegex(TimeoutError, "deadline exceeded"):
            client._send_frame(b"{}")
        self.assertEqual(client.sock.writes, [])

    def test_expired_deadline_prevents_socket_read(self):
        fake = FakeSocket(json_frame({"result": True}))
        client = self.bare_client(fake, timeout=0.5)
        self.clock.advance(0.5)
        with self.assertRaisesRegex(TimeoutError, "deadline exceeded"):
            client._receive_json()
        self.assertEqual(fake.recv_count, 0)

    def test_explicit_new_request_gets_a_fresh_bounded_deadline(self):
        fake = FakeSocket(json_frame({"id": 1, "result": {}}))
        client = self.bare_client(fake, timeout=0.5)
        self.clock.advance(2)
        self.assertEqual(client.request("thread/read", {"threadId": "offline"}), {})
        self.assertEqual(client._deadline, self.clock.now + 0.5)
        self.assertTrue(all(0 < value <= 0.5 for value in fake.timeouts))


class OneRequestFacadeTests(OfflineCase):
    def test_one_connection_and_one_input_on_success_error_timeout_eof_or_send_failure(self):
        for outcome in ("accepted", "rpc_error", "timeout", "eof", "send_failure"):
            def respond(message, fake):
                if outcome == "accepted":
                    fake.incoming.extend(json_frame({"id": message["id"],
                                                     "result": {"turnId": TURN_ID}}))
                elif outcome == "rpc_error":
                    fake.incoming.extend(json_frame({"id": message["id"], "error": {
                        "code": -32000, "message": "fake turn changed"}}))
                elif outcome == "timeout":
                    fake.exhausted_error = TimeoutError("accepted bytes; reply unknown")
                elif outcome == "send_failure":
                    raise BrokenPipeError("fake partial send; delivery unknown")
                # EOF supplies no reply after recording the sole request.

            with self.subTest(outcome=outcome), self.constructor(response_handler=respond) as env:
                def invoke():
                    return rpc.request("turn/steer", INPUT_PARAMS,
                                       socket_path=FAKE_PATH, timeout=2)

                if outcome == "accepted":
                    self.assertEqual(invoke(), {"turnId": TURN_ID})
                else:
                    expected_error = {"rpc_error": rpc.RPCError, "timeout": TimeoutError,
                                      "eof": ConnectionError, "send_failure": BrokenPipeError}[outcome]
                    with self.assertRaises(expected_error):
                        invoke()
                env.factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
                self.assertEqual(env.fake.connections, [FAKE_PATH])
                messages = sent_messages(env.fake)
                self.assertEqual([message["method"] for message in messages],
                                 ["initialize", "initialized", "turn/steer"])
                self.assertEqual(messages[-1],
                                 {"id": 2, "method": "turn/steer", "params": INPUT_PARAMS})
                self.assertEqual(env.fake.close_count, 1)


if __name__ == "__main__":
    unittest.main()

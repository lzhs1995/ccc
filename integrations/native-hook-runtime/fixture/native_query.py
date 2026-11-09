"""Exact read-only socket helpers from original r3 package; no injected registry."""
import ctypes, hashlib, json, os, socket, stat, sys, uuid
from pathlib import Path
import fixture_common as h
import fixture_common as f
def need(value, message):
    if not value:
        raise ValueError(message)


def identity(pid):
    # Exact proc_bsdinfo layout used by r2 utils/pty/local_identity.rs.
    class BsdInfo(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint32) for n in
                    ("flags", "status", "xstatus", "pid", "ppid")]
        _fields_ += [("ids", ctypes.c_uint32 * 7), ("comm", ctypes.c_uint8 * 16),
                     ("name", ctypes.c_uint8 * 32), ("misc", ctypes.c_uint32 * 6),
                     ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]
    need(sys.platform == "darwin" and 1 < pid < 2**31, "unsupported process")
    info = BsdInfo()
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                ctypes.c_void_p, ctypes.c_int]
    count = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    need(count == ctypes.sizeof(info) == 136 and info.pid == pid
         and info.status != 5 and info.start_sec > 0 and info.start_usec < 1000000
         and info.ids[0] == os.getuid(), "process identity unavailable")
    return {"pid": pid, "ppid": info.ppid, "uid": info.ids[0],
            "birth": [info.start_sec, info.start_usec]}


def capability(ticket):
    root = h.require_runner(ticket["root"])
    path = root / "n/credential-observations/compaction-recovery" / (
        "client-%s-control.json" % ticket["client"]["pid"])
    raw = f.strict_file(path)
    cap = json.loads(raw)
    need(cap["pid"] == ticket["client"]["pid"] and cap["birth"] == ticket["client"]["birth"],
         "capability is not the owned client generation")
    need(cap["methods"] == ["compaction/recovery/query", "compaction/recovery/control"]
         and cap.get("callerObservation") == {"schema": 1, "method": "caller/thread/observe",
             "scope": "caller-thread-readonly", "requiresSameProcessSpawn": True},
         "r2 capability not present")
    h.contained(root, cap["socket"])
    return path, raw, cap


def observe(ticket, changes=None):
    path, raw, cap = capability(ticket)
    peer_before = identity(os.getpid())
    client_before = identity(cap["pid"])
    need(client_before == ticket["client"], "owned client changed")
    body = {"method": "caller/thread/observe", "challenge": str(uuid.uuid4()),
            "expectedClientPid": cap["pid"], "expectedClientBirth": cap["birth"],
            "expectedClientEpoch": cap["client_epoch"]}
    body.update(changes or {})
    sockpath = Path(cap["socket"])
    before = sockpath.lstat()
    need(stat.S_ISSOCK(before.st_mode) and before.st_uid == os.getuid()
         and not before.st_mode & 0o077
         and [before.st_dev, before.st_ino] == [cap["socket_device"], cap["socket_inode"]],
         "socket identity differs")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(3)
        stream.connect(str(sockpath))
        server_pid = int.from_bytes(stream.getsockopt(0, 2, 4), sys.byteorder, signed=True)
        uid = ctypes.c_uint32()
        gid = ctypes.c_uint32()
        lib = ctypes.CDLL(None)
        need(lib.getpeereid(stream.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0
             and uid.value == os.getuid() and server_pid == cap["pid"], "wrong kernel server peer")
        stream.sendall(json.dumps(body).encode() + b"\n")
        response = bytearray()
        while not response.endswith(b"\n"):
            part = stream.recv(4096)
            need(part and len(response) + len(part) <= 65536, "incomplete/oversize reply")
            response.extend(part)
    after = sockpath.lstat()
    need((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
         and f.strict_file(path) == raw and identity(cap["pid"]) == client_before
         and identity(os.getpid()) == peer_before, "identity changed during query")
    reply = json.loads(response)
    return {"request": body, "reply": reply, "peer": peer_before, "client": client_before,
            "socket": {"path": str(sockpath), "device": before.st_dev, "inode": before.st_ino},
            "capability_sha256": hashlib.sha256(raw).hexdigest()}



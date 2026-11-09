"""Optional root-inbox caller proof; no foreground, sender, hook runner, or controller.

The immutable pin is supplied by the original trusted hook registration, never
by hook stdin or inherited environment. It is not permission to install a binary.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import sys
import time
import uuid

import cmux_daemon_identity as native
import cmux_darwin_fds as native_fds
import cmux_identity_budget as budget
import cmux_workspace_guard as workspace

METHOD = 'caller/thread/observe'
METHODS = ['compaction/recovery/query', 'compaction/recovery/control']
LIMIT = 16384

def need(condition, reason):
    if not condition:
        raise ValueError(reason)

def canonical(value):
    need(isinstance(value, str), 'UUID string required')
    result = str(uuid.UUID(value))
    need(result == value.lower(), 'canonical UUID required')
    return result

def strict_json(raw):
    def pairs(items):
        result = {}
        for k, v in items:
            need(k not in result, 'duplicate native evidence key')
            result[k] = v
        return result
    def invalid(_):
        raise ValueError('invalid JSON constant')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)

def signature(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_nlink)

def private_read(path, limit=LIMIT):
    path = Path(path)
    need(path.is_absolute() and path.resolve(strict=True) == path, 'canonical private evidence required')
    initial = path.lstat()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
             and before.st_nlink == 1 and not before.st_mode & 0o077
             and before.st_size <= limit, 'private bounded evidence required')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    need(len(raw) <= limit and signature(initial) == signature(before)
         == signature(after) == signature(path.lstat()), 'private evidence changed')
    return raw, signature(before)

def kernel_peer(connection):
    need(sys.platform == 'darwin', 'native socket peer identity requires Darwin')
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    library = ctypes.CDLL(None, use_errno=True)
    getpeereid = library.getpeereid
    getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
    getpeereid.restype = ctypes.c_int
    need(getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) == 0, 'native socket UID unavailable')
    raw = connection.getsockopt(0, 2, 4)  # SOL_LOCAL / LOCAL_PEERPID
    need(len(raw) == 4, 'native socket PID unavailable')
    pid = struct.unpack('i', raw)[0]
    need(pid > 1 and uid.value == os.getuid(), 'native socket peer invalid')
    return pid, uid.value

def request_caller(path, client, epoch, timeout=2.0):
    """Exactly one read-only method; expected caller SID is never sent."""
    challenge = str(uuid.uuid4())
    body = dict(method=METHOD, challenge=challenge, expectedClientPid=client['pid'],
                expectedClientBirth=client['birth'], expectedClientEpoch=epoch)
    deadline = time.monotonic() + min(timeout, budget.timeout())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.connect(str(path))
        need(kernel_peer(connection) == (client['pid'], os.getuid()), 'wrong native socket owner')
        connection.sendall(json.dumps(body, separators=(',', ':')).encode() + b'\n')
        raw = bytearray()
        while True:
            need(time.monotonic() < deadline, 'native caller deadline expired')
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            chunk = connection.recv(min(4096, LIMIT + 1 - len(raw)))
            need(bool(chunk), 'native caller response incomplete')
            raw.extend(chunk)
            need(len(raw) <= LIMIT, 'native caller response too large')
            if b'\n' in raw:
                need(raw.count(b'\n') == 1 and raw.endswith(b'\n'), 'ambiguous native response')
                break
        # Native server closes after its reply. The connected FD never reconnects;
        # owner PID/birth and endpoint inode are rechecked by NativeHookCaller.
    return strict_json(raw), challenge

def validate_reply(reply, challenge, client, current, chain, epoch, payload, facts, handler_id):
    """Pure acceptance. No reply can supply foreground or sending authority."""
    need(isinstance(reply, dict) and set(reply) == {'schema','purpose','scope','source','challenge',
         'clientPid','clientBirth','clientEpoch','peerPid','peerBirth','spawnPid','spawnBirth',
         'threadId','callId','origin','foreground','sendAuthority','controllerAuthority','inboxAuthority'}
         and type(reply.get('schema')) is int and reply['schema'] == 1
         and reply.get('purpose') == 'native_caller_thread'
         and reply.get('scope') == 'caller-thread-readonly'
         and reply.get('source') == 'same-process-native-spawn', 'wrong caller evidence scope')
    need(reply.get('challenge') == challenge and reply.get('clientPid') == client['pid']
         and reply.get('clientBirth') == client['birth'] and reply.get('clientEpoch') == epoch
         and reply.get('peerPid') == current['pid'] and reply.get('peerBirth') == current['birth'],
         'native caller generation or nonce mismatch')
    need(reply.get('foreground') is None and all(reply.get(k) is False for k in
         ('sendAuthority', 'controllerAuthority', 'inboxAuthority')), 'caller evidence grants forbidden authority')
    origin = reply.get('origin')
    need(isinstance(origin, dict) and set(origin) == {'kind','event','turnId','toolUseId','handlerId'}
         and origin['kind'] == 'native-command-hook' and origin['event'] == 'PostToolUse'
         and origin['handlerId'] == handler_id, 'not the original automatic PostToolUse hook')
    need(all(isinstance(origin[k],str) and 0 < len(origin[k]) <= 256 for k in ('turnId','toolUseId'))
         and payload.get('hook_event_name') == origin['event']
         and payload.get('turn_id') == origin['turnId'] and bool(origin['turnId'])
         and payload.get('tool_use_id') == origin['toolUseId'] and bool(origin['toolUseId'])
         and reply.get('callId') == origin['toolUseId'], 'native hook trigger differs from stdin')
    thread = canonical(reply.get('threadId'))
    need(facts.get('thread_scope') == 'ROOT_METADATA' and not facts.get('parent_thread_id')
         and canonical(facts.get('actual_thread_id')) == thread
         and canonical(facts.get('inherited_session_id')) == thread
         and canonical(payload.get('session_id')) == thread, 'child or mismatched root metadata')
    matches = [p for p in [current] + chain if p.get('pid') == reply.get('spawnPid')
               and p.get('birth') == reply.get('spawnBirth')]
    need(len(matches) == 1 and matches[0]['ppid'] == client['pid'], 'native hook spawn is not this reader ancestor')
    return dict(thread_id=thread, origin=dict(origin), spawn_pid=reply['spawnPid'], spawn_birth=reply['spawnBirth'])

class NativeHookCaller:
    def __init__(self, pin_path, pin_sha256):
        need(isinstance(pin_sha256, str) and re.fullmatch('[0-9a-f]{64}', pin_sha256), 'immutable native pin SHA required')
        self.pin_path = Path(pin_path)
        self.pin_raw, self.pin_signature = private_read(self.pin_path)
        need(hashlib.sha256(self.pin_raw).hexdigest() == pin_sha256, 'native pin changed')
        self.pin = strict_json(self.pin_raw)
        need(isinstance(self.pin, dict) and set(self.pin) == {'schema','binary_path','binary_sha256','handler_id'}
             and self.pin['schema'] == 'native-hook-caller-pin-v1', 'invalid native caller pin')
        need(isinstance(self.pin['binary_sha256'], str) and re.fullmatch('[0-9a-f]{64}', self.pin['binary_sha256']), 'native binary SHA required')
        need(isinstance(self.pin['handler_id'], str) and 0 < len(self.pin['handler_id']) <= 4096, 'original native handler identity required')
        self.binary = Path(self.pin['binary_path'])
        need(self.binary.is_absolute() and self.binary.resolve(strict=True) == self.binary, 'canonical pinned native binary required')
        self.binary_signature = None

    def _binary(self, client):
        need(client.get('executable') == str(self.binary), 'client is not pinned native binary')
        info = self.binary.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
             and not info.st_mode & 0o022 and info.st_mode & 0o111, 'unsafe native binary')
        need(client.get('file_identity') == [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns], 'kernel native executable changed')
        if self.binary_signature is None:
            digest = hashlib.sha256()
            with self.binary.open('rb') as stream:
                need(signature(os.fstat(stream.fileno())) == signature(info), 'native binary replaced')
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    budget.check(); digest.update(chunk)
                need(signature(os.fstat(stream.fileno())) == signature(info), 'native binary mutated')
            need(digest.hexdigest() == self.pin['binary_sha256'], 'native binary content differs')
            self.binary_signature = signature(info)
        need(signature(info) == self.binary_signature == signature(self.binary.lstat()), 'native binary generation changed')

    def _client(self):
        chain, daemon = native._ancestry()
        need(daemon is None, 'remote or shared daemon caller observation unsupported')
        matches = [(i,p) for i,p in enumerate(chain) if p.get('executable') == str(self.binary)]
        need(len(matches) == 1, 'unique pinned native ancestor required')
        index, client = matches[0]
        need(client.get('argv', [])[1:2] != ['app-server'], 'external app-server is not embedded TUI')
        native._assert_proxy_ancestry(chain[:index])
        current = native.process(os.getpid())
        need(chain and current['ppid'] == chain[0]['pid'], 'reader ancestry changed')
        self._binary(client)
        return client, current, chain

    @staticmethod
    def _writer(client, facts):
        path = Path(facts['transcript_path'])
        current = path.lstat()
        need(path.is_absolute() and path.resolve(strict=True) == path
             and stat.S_ISREG(current.st_mode) and current.st_uid == os.getuid()
             and not current.st_mode & 0o022
             and [current.st_dev,current.st_ino] == [facts['transcript_device'],facts['transcript_inode']], 'root transcript identity changed')
        matches = [f for f in native_fds.frames(client['pid']) if f.get('a') in ('u','w')
                   and f.get('n') == str(path) and f.get('i') == str(current.st_ino)]
        need(matches, 'native root transcript writer not held')
        return sorted((f['f'],f['a'],f['i'],f['n']) for f in matches)

    def __call__(self, payload, facts):
        with budget.limit(3.0):
            need(private_read(self.pin_path) == (self.pin_raw,self.pin_signature), 'native pin replaced')
            client, current, chain = self._client()
            env = client['env']
            workspace_id, surface_id = canonical(env.get('CMUX_WORKSPACE_ID')), canonical(env.get('CMUX_SURFACE_ID'))
            home = Path(env.get('CODEX_HOME') or str(Path(env['HOME']) / '.codex'))
            root = home / 'credential-observations/compaction-recovery'
            directories = []
            for directory in [home, home/'credential-observations', root]:
                s = directory.lstat()
                need(directory.is_absolute() and directory.resolve(strict=True) == directory
                     and stat.S_ISDIR(s.st_mode) and s.st_uid == os.getuid()
                     and not s.st_mode & (0o022 if directory == home else 0o077), 'untrusted native control directory')
                directories.append((directory, signature(s)[:4]))
            marker = root/'enabled-v1'; marker_value = private_read(marker, 64)
            need(marker_value[0] == b'ccc-native-compaction-recovery-v1\n', 'native control disabled')
            cap_path = root/f"client-{client['pid']}-control.json"
            cap_raw, cap_sig = private_read(cap_path)
            cap = strict_json(cap_raw)
            need(type(cap.get('schema')) is int and cap['schema'] == 1 and cap.get('purpose') == 'native_compaction_recovery_control'
                 and cap.get('pid') == client['pid'] and cap.get('birth') == client['birth']
                 and cap.get('methods') == METHODS and cap.get('controlRequiresNativeBackend') is True,
                 'native capability mismatch')
            need(cap.get('callerObservation') == dict(schema=1,method=METHOD,scope='caller-thread-readonly',requiresSameProcessSpawn=True),
                 'native caller capability unavailable')
            epoch = canonical(cap.get('client_epoch'))
            sock_path = root/f"c-{client['pid']}-{epoch[:8]}.sock"
            need(cap.get('socket') == str(sock_path), 'native socket path differs')
            sock = sock_path.lstat()
            need(stat.S_ISSOCK(sock.st_mode) and sock.st_uid == os.getuid() and not sock.st_mode & 0o077
                 and [sock.st_dev,sock.st_ino] == [cap.get('socket_device'),cap.get('socket_inode')], 'native socket identity differs')
            need(Path(facts['transcript_path']).is_relative_to(home/'sessions')
                 or Path(facts['transcript_path']).is_relative_to(home/'archived_sessions'), 'root transcript outside native home')
            writer = self._writer(client,facts)
            reply, challenge = request_caller(sock_path,client,epoch)
            accepted = validate_reply(reply,challenge,client,current,chain,epoch,payload,facts,self.pin['handler_id'])
            tty = subprocess.run(['/bin/ps','-p',str(client['pid']),'-o','tty='],capture_output=True,text=True,
                                 check=True,timeout=budget.timeout()).stdout.strip()
            need(tty and tty not in ('?','??','-'), 'native TUI terminal unavailable')
            # Reuse only the original pure UUID/TTY host binding validator. This
            # private structural argument carries no foreground or caller rights.
            located = dict(client,tty=tty)
            identity = workspace._read_json_command('identify','--json')
            tree = workspace._read_json_command('tree','--workspace',workspace_id.upper(),'--json','--id-format','both')
            workspace.daemon_identity.resolve(identity,tree,os.environ,
                dict(source='embedded-native-client-v1',daemon=None,client=located))
            need(self._client() == (client,current,chain) and self._writer(client,facts) == writer, 'native caller changed during host read')
            need(private_read(cap_path) == (cap_raw,cap_sig) and private_read(marker,64) == marker_value
                 and signature(sock_path.lstat()) == signature(sock)
                 and all(signature(d.lstat())[:4] == sig for d,sig in directories), 'native endpoint changed')
            return dict(workspace=workspace_id,surface=surface_id,client_pid=client['pid'],client_birth=client['birth'],
                        client_epoch=epoch,capability_sha256=hashlib.sha256(cap_raw).hexdigest(),
                        metadata_sha256=facts['metadata_sha256'],transcript_device=facts['transcript_device'],
                        transcript_inode=facts['transcript_inode'],**accepted)

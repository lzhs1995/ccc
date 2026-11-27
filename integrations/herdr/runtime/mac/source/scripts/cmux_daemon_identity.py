"""Resolve a native Codex caller from live process evidence, never focus.

No environment mutation or terminal operations. The thread ID is only a selector:
a unique same-user native foreground (or legacy resume) client, executable,
birth, TTY and cmux UUID must agree. Embedded clients require their actual tool
ancestry and a current native foreground record; selectors alone are not proof.
"""
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import cmux_identity_budget as budget
import cmux_foreground_thread as foreground


class IdentityError(RuntimeError):
    pass


class ProcessExited(IdentityError):
    """The initial kernel read proved absence, before any identity was read."""


class LoginInfoDenied(IdentityError):
    """Only an initial full-BSD permission refusal, not an absent process."""


class ShortBsdInfo(ctypes.Structure):
    # Public Darwin sys/proc_info.h, PROC_PIDT_SHORTBSDINFO (13).
    _fields_ = [('pid', ctypes.c_uint32), ('ppid', ctypes.c_uint32),
                ('pgid', ctypes.c_uint32), ('status', ctypes.c_uint32),
                ('comm', ctypes.c_char * 16), ('flags', ctypes.c_uint32),
                ('uid', ctypes.c_uint32), ('gid', ctypes.c_uint32),
                ('ruid', ctypes.c_uint32), ('rgid', ctypes.c_uint32),
                ('svuid', ctypes.c_uint32), ('svgid', ctypes.c_uint32),
                ('reserved', ctypes.c_uint32)]


class BsdInfo(ctypes.Structure):
    _fields_ = [('flags', ctypes.c_uint32), ('status', ctypes.c_uint32),
                ('xstatus', ctypes.c_uint32), ('pid', ctypes.c_uint32),
                ('ppid', ctypes.c_uint32), ('uid', ctypes.c_uint32),
                ('gid', ctypes.c_uint32), ('ruid', ctypes.c_uint32),
                ('rgid', ctypes.c_uint32), ('svuid', ctypes.c_uint32),
                ('svgid', ctypes.c_uint32), ('reserved', ctypes.c_uint32),
                ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32),
                ('misc', ctypes.c_uint32 * 6), ('start_sec', ctypes.c_uint64),
                ('start_usec', ctypes.c_uint64)]


def _args(data):
    argc = ctypes.c_int.from_buffer_copy(data[:4]).value
    if not 1 <= argc <= 65536:
        raise IdentityError('invalid process arguments')
    offset = data.index(b'\0', 4) + 1
    while offset < len(data) and data[offset] == 0:
        offset += 1
    argv = []
    for _ in range(argc):
        end = data.index(b'\0', offset)
        argv.append(data[offset:end].decode())
        offset = end + 1
    env = {}
    for item in data[offset:].split(b'\0'):
        if not item:
            break
        key, sep, value = item.partition(b'=')
        if not sep or key in env:
            raise IdentityError('invalid process environment')
        env[key] = value
    # Credentials never leave this reader.
    return argv, {k: env[k.encode()].decode() for k in
                  ('CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID', 'CODEX_THREAD_ID',
                   'HOME', 'CODEX_HOME', 'CODEX_CLIENT_THREAD_OBSERVER') if k.encode() in env}


def process(pid, *, arguments=True, validate_argv=True, allow_system_login=False):
    """Read a same-user process; raw inventory reads are not caller proof.

    Only candidate discovery may defer the argv-path rule. The selected caller
    is reread with the strict default before using its terminal identity.
    """
    budget.check()
    if sys.platform != 'darwin' or type(pid) is not int or pid <= 1:
        raise IdentityError('live Darwin process required')
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                ctypes.c_void_p, ctypes.c_int]
    def info(*, initial=False):
        b = BsdInfo()
        ctypes.set_errno(0)
        count = lib.proc_pidinfo(pid, 3, 0, ctypes.byref(b), ctypes.sizeof(b))
        if count != ctypes.sizeof(b):
            if initial and count <= 0 and ctypes.get_errno() == errno.ESRCH:
                raise ProcessExited('process absent at initial kernel read')
            if initial and count <= 0 and allow_system_login and ctypes.get_errno() in (errno.EPERM, errno.EACCES):
                raise LoginInfoDenied('full process identity permission denied')
            raise IdentityError('process identity inaccessible or changed')
        system_login = allow_system_login and b.uid == 0 and os.getuid() != 0
        if b.pid != pid or not b.start_sec or (b.uid != os.getuid() and not system_login):
            raise IdentityError('process identity changed, foreign, or invalid')
        if b.status == 5:
            if initial:
                raise ProcessExited('process zombie at initial kernel read')
            raise IdentityError('process exited during read')
        result = {'pid': pid, 'ppid': b.ppid, 'birth': [b.start_sec, b.start_usec]}
        if system_login:
            result['system_login'] = True
        return result
    result = info(initial=True)
    initial = dict(result)
    if result.get('system_login'):
        # A root-owned macOS login is an ancestry boundary, never a caller.
        # Do not try to read its private argv/env or treat arbitrary foreign
        # processes as evidence that this is an ordinary terminal.
        executable = ctypes.create_string_buffer(4096)
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        def login_path():
            if (lib.proc_pidpath(pid, executable, len(executable)) <= 0
                    or executable.value != b'/usr/bin/login'):
                raise IdentityError('foreign ancestor is not system login')
        login_path()
        st = Path('/usr/bin/login').stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise IdentityError('system login executable is not protected')
        if info() != initial:
            raise IdentityError('system login changed during read')
        login_path()
        budget.check()
        return result
    if arguments:
        size = os.sysconf('SC_ARG_MAX')
        if not 0 < size <= 2 * 1024 * 1024:
            raise IdentityError('unsupported argument buffer')
        buf, length = ctypes.create_string_buffer(size), ctypes.c_size_t(size)
        sysctl = ctypes.CDLL(None).sysctl
        sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                          ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        if sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buf, ctypes.byref(length), None, 0) != 0:
            raise IdentityError('process arguments inaccessible')
        argv, env = _args(buf.raw[:length.value])
        executable = ctypes.create_string_buffer(4096)
        lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        if lib.proc_pidpath(pid, executable, len(executable)) <= 0:
            raise IdentityError('executable identity inaccessible')
        path = Path(executable.value.decode()).resolve(strict=True)
        if not argv:
            raise IdentityError('process has no argv')
        if validate_argv and path.name == 'codex' and (not Path(argv[0]).is_absolute()
                                                      or Path(argv[0]).resolve(strict=True) != path):
            raise IdentityError('argv executable differs from kernel executable')
        st = path.stat()
        result.update(argv=argv, env=env, executable=str(path),
                      file_identity=[st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns])
    if info() != initial:
        raise IdentityError('process changed during read')
    budget.check()
    return result


def client_candidates():
    """Enumerate kernel executable paths; pgrep -x can omit live Darwin clients.

    ps is discovery only: process() still verifies executable, owner, birth and
    argv before any candidate can become an authenticated caller.
    """
    listing = subprocess.run(['/bin/ps', '-U', str(os.getuid()), '-o', 'pid=,comm='],
                             capture_output=True, text=True, timeout=budget.timeout())
    budget.check()
    if listing.returncode != 0:
        raise IdentityError('client inventory unavailable')
    candidates = []
    for line in listing.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            raise IdentityError('malformed client inventory')
        if Path(fields[1]).name == 'codex':
            candidates.append(fields[0])
    if len(set(candidates)) != len(candidates):
        raise IdentityError('duplicate client inventory')
    return candidates



def _login_boundary(pid, child):
    """Permission-limited root login, pinned by its still-live original child.

    Short BSD info has no birth time, so it is NOT generic process identity.
    Require the already kernel-read child's complete identity (including birth
    and ppid) before and after. If login exits/reuses its PID, the original child
    is reparented and fails this check. No argv/env from root is read or trusted.
    """
    if (not isinstance(child, dict) or child.get('ppid') != pid
            or not child.get('birth') or not child.get('argv')
            or child.get('system_login')):
        raise IdentityError('system login requires a verified live child')
    if process(child['pid']) != child:
        raise IdentityError('system login child changed before read')
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                ctypes.c_void_p, ctypes.c_int]
    lib.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    def short_info():
        budget.check()
        b = ShortBsdInfo()
        if lib.proc_pidinfo(pid, 13, 0, ctypes.byref(b), ctypes.sizeof(b)) != ctypes.sizeof(b):
            raise IdentityError('system login short identity inaccessible')
        if (b.pid != pid or b.uid != 0 or b.ruid != os.getuid()
                or os.getuid() == 0 or b.status == 5 or b.ppid <= 0):
            raise IdentityError('system login short identity foreign or invalid')
        return dict(pid=b.pid, ppid=b.ppid, pgid=b.pgid, uid=b.uid,
                    ruid=b.ruid, svuid=b.svuid)
    def login_path():
        executable = ctypes.create_string_buffer(4096)
        if (lib.proc_pidpath(pid, executable, len(executable)) <= 0
                or executable.value != b'/usr/bin/login'):
            raise IdentityError('foreign ancestor is not system login')
        st = Path('/usr/bin/login').stat()
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise IdentityError('system login executable is not protected')
    first = short_info()
    login_path()
    if short_info() != first:
        raise IdentityError('system login short identity drift')
    login_path()
    if process(child['pid']) != child:
        raise IdentityError('system login child changed or reparented')
    budget.check()
    return dict(pid=pid, ppid=first['ppid'], system_login=True,
                identity_source='short_bsd_stable_child', short_identity=first,
                child_pid=child['pid'], child_birth=list(child['birth']))


def _ancestor_process(pid, child):
    try:
        return process(pid, allow_system_login=True)
    except LoginInfoDenied:
        # No fallback for missing/unknown identity, argv failure, or later drift.
        return _login_boundary(pid, child)


def _ancestry():
    chain, pid, daemon = [], os.getppid(), None
    for _ in range(64):
        budget.check()
        if pid <= 1:
            break
        if any(p['pid'] == pid for p in chain):
            raise IdentityError('cyclic process ancestry')
        item = _ancestor_process(pid, chain[-1] if chain else None)
        chain.append(item)
        if item.get('system_login'):
            break
        argv = item['argv']
        if (Path(item['executable']).name == 'codex' and len(argv) > 1
                and argv[1] == 'app-server' and '--managed-daemon' in argv[2:]):
            daemon = item
            break
        pid = item['ppid']
    else:
        raise IdentityError('process ancestry exceeded limit')
    if daemon is None:
        for index, item in enumerate(chain):
            child = chain[index - 1] if index else None
            if _ancestor_process(item['pid'], child) != item:
                raise IdentityError('ordinary process ancestry drift')
    return chain, daemon


def _assert_proxy_ancestry(chain):
    """Only shells and the supported proxy may relay a client's identity.

    Inspect kernel executable names, not argv labels or inherited selectors.
    The leaf interpreter running this resolver is not an intermediate process;
    an interpreter hosting another CLI is. Unknown intermediates fail closed,
    including version-named native clients and node/python model launchers.
    """
    proxies = {'sh', 'bash', 'zsh', 'dash', 'ksh', 'fish', 'csh', 'tcsh', 'rtk'}
    for item in chain:
        if Path(item.get('executable', '')).name not in proxies:
            raise IdentityError('intermediate process is not a supported tool proxy')


def _collect_embedded(env, chain, *, payload=None):
    """Authenticate the ancestor TUI without inventing a managed daemon.

    No global process scan or legacy resume fallback is needed: this tool must
    descend from the very client whose native foreground it selects. A child
    thread cannot borrow its parent's foreground, UUIDs or inbox authority.
    """
    candidates = [(index, item) for index, item in enumerate(chain)
                  if Path(item.get('executable', '')).name == 'codex']
    if not candidates:
        return None
    index, client = candidates[0]
    if client.get('argv', [])[1:2] == ['app-server']:
        raise IdentityError('unmanaged app-server is not an embedded native client')
    try:
        session = str(uuid.UUID(payload['session_id'] if payload is not None
                                else env['CODEX_THREAD_ID']))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise IdentityError('embedded caller lacks a valid thread selector') from exc
    if payload is not None and payload.get('hook_event_name') not in (
            'PreToolUse', 'PostToolUse', 'Stop', 'SubagentStop',
            'SessionStart', 'UserPromptSubmit'):
        raise IdentityError('embedded hook event is missing or unsupported')
    current = process(os.getpid())
    if current['pid'] != os.getpid() or current['ppid'] != chain[0]['pid']:
        raise IdentityError('embedded tool ancestry changed')
    tool_chain = [current] + chain[:index]
    _assert_proxy_ancestry(tool_chain[1:])
    if (payload is None and current['env'].get('CODEX_THREAD_ID') != session
            or env.get('CODEX_THREAD_ID') not in (None, session)
            or any(item['env'].get('CODEX_THREAD_ID') not in (None, session)
                   for item in tool_chain)):
        raise IdentityError('embedded thread selector differs from tool ancestry')
    for key in ('CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID'):
        wanted = client['env'].get(key)
        if (not wanted or env.get(key) != wanted or current['env'].get(key) != wanted
                or any(item['env'].get(key) not in (None, wanted) for item in tool_chain)):
            raise IdentityError('embedded UUID environment differs from client ancestry')
    try:
        selected = foreground.read(client)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise IdentityError('embedded native foreground unavailable: ' + str(exc)) from exc
    if selected is None or selected.get('thread_id') != session:
        raise IdentityError('embedded caller requires its matching native foreground thread')
    tty = subprocess.run(['/bin/ps', '-p', str(client['pid']), '-o', 'tty='],
                         capture_output=True, text=True,
                         timeout=budget.timeout(), check=True).stdout.strip()
    budget.check()
    if not tty or tty in ('?', '??', '-'):
        raise IdentityError('embedded native client has no terminal')
    if (process(os.getpid()) != current or _ancestry() != (chain, None)
            or foreground.read(client) != selected):
        raise IdentityError('embedded caller changed during authentication')
    return dict(source='embedded-native-client-v1', session=session, daemon=None,
                client=dict(client, tty=tty), chain=[current] + chain,
                foreground=selected)


def collect(env):
    """Bind a tool's kernel-observed selector to its live native client.

    A shell may exec its last command. The resulting Python process is then a
    direct managed-daemon child, with no extra shell/rtk ancestor. Its own
    immutable process identity supplies the tool selector in that exact case.
    """
    if sys.platform != 'darwin' or not env.get('CODEX_THREAD_ID'):
        return None
    chain, daemon = _ancestry()
    if daemon is None:
        return _collect_embedded(env, chain)
    session = str(uuid.UUID(env['CODEX_THREAD_ID']))
    tool_threads = [p['env']['CODEX_THREAD_ID'] for p in chain[:-1]
                    if p['env'].get('CODEX_THREAD_ID')]
    if not tool_threads:
        # Do not require an incidental wrapper to keep a selectable ancestor
        # alive. Read this actual process; a caller-provided env dictionary or
        # the daemon's inherited workspace cannot establish the thread.
        if len(chain) != 1 or chain[0] != daemon or os.getppid() != daemon['pid']:
            raise IdentityError('thread selector missing from tool ancestry')
        current = process(os.getpid())
        if (current['pid'] != os.getpid() or current['ppid'] != daemon['pid']
                or current['env'].get('CODEX_THREAD_ID') != session):
            raise IdentityError('thread selector differs from current tool ancestry')
        if any(current['env'].get(k) != env.get(k) for k in
               ('CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID')):
            raise IdentityError('current tool environment differs from tool selector')
        # _collect_client rereads every member of this chain after inventory,
        # including PID/birth/executable/parent/selector, before returning proof.
        chain = [current] + chain
        tool_threads = [current['env']['CODEX_THREAD_ID']]
    if any(t != session for t in tool_threads):
        raise IdentityError('thread selector differs from tool ancestry')
    if any(env.get(k) != daemon['env'].get(k) for k in
           ('CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID')):
        raise IdentityError('tool environment differs from managed daemon')
    return _collect_client(session, chain, daemon)


def collect_hook(env, payload):
    """Native hooks carry session_id in stdin, including direct daemon children.

    The payload selects a session only after the actual managed ancestor has
    been read. It never supplies workspace/surface identity or selects by focus.
    A tool-shell thread selector, when present, must agree with the payload.
    """
    if sys.platform != 'darwin':
        return None
    chain, daemon = _ancestry()
    if daemon is None:
        return _collect_embedded(env, chain, payload=payload)
    try:
        session = str(uuid.UUID(payload['session_id']))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise IdentityError('managed hook lacks a valid session_id') from exc
    if payload.get('hook_event_name') not in ('PreToolUse', 'PostToolUse', 'Stop', 'SubagentStop',
                                             'SessionStart', 'UserPromptSubmit'):
        raise IdentityError('managed hook event is missing or unsupported')
    selectors = [p['env']['CODEX_THREAD_ID'] for p in chain[:-1]
                 if p['env'].get('CODEX_THREAD_ID')]
    if env.get('CODEX_THREAD_ID'):
        selectors.append(env['CODEX_THREAD_ID'])
    if any(t != session for t in selectors):
        raise IdentityError('hook session differs from thread ancestry')
    if any(env.get(k) != daemon['env'].get(k) for k in
           ('CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID')):
        raise IdentityError('hook environment differs from managed daemon')
    return _collect_client(session, chain, daemon)


def _collect_client(session, chain, daemon):
    # A shell may exec the resolver directly; that leaf is already verified by
    # collect(). Other model clients must never relay the daemon's selectors,
    # for either ordinary tools or hooks carrying a copied session_id.
    _assert_proxy_ancestry([p for p in chain
                            if p['pid'] not in (daemon['pid'], os.getpid())])
    candidates = client_candidates()
    clients = []
    selections = []
    unavailable = object()
    selection_errors = []

    def matches(p, selected):
        if selected is unavailable:
            return False
        argv = p['argv']
        return (selected['thread_id'] == session if selected is not None
                else len(argv) >= 3 and argv[1:3] == ['resume', session])

    for candidate in candidates:
        try:
            # A discovery row is not yet this session's caller. Read its kernel
            # identity before enforcing the selected caller's argv-path rule.
            p = process(int(candidate), validate_argv=False)
        except ProcessExited:
            # Only an initial ESRCH/zombie is harmless. Unknown identity and
            # a disappearance after a partial read must not hide a second match.
            continue
        try:
            selected = foreground.read(p)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # 候选清单不是当前 caller。坏记录只能使该候选不具备认证
            # 资格，不能否决另一位证据完整的主管，也不能降级为 argv。
            selected = unavailable
            selection_errors.append((p['pid'], str(exc)))
        selections.append((p, selected))
        if selected is unavailable:
            continue
        if matches(p, selected):
            if process(p['pid']) != p:
                raise IdentityError('selected client identity drift')
            if Path(p['executable']).name != 'codex':
                raise IdentityError('client executable mismatch')
            tty = subprocess.run(['/bin/ps', '-p', candidate, '-o', 'tty='],
                                 capture_output=True, text=True,
                                 timeout=budget.timeout(), check=True).stdout.strip()
            budget.check()
            if not tty or tty in ('?', '??', '-'):
                raise IdentityError('client has no terminal')
            clients.append(dict(p, tty=tty))
        elif process(p['pid'], validate_argv=False) != p:
            # A stable, readable nonmatch can be excluded, including a relative
            # argv[0]. Never exclude an unreadable or changing session selector.
            raise IdentityError('unrelated candidate identity drift')
    if len(clients) != 1:
        detail = ''
        if not clients and selection_errors:
            # Keep useful diagnostics when the actual caller cannot be found.
            # Unrelated failures never enter the selected proof: fluctuating
            # foreign records must not make caller_snapshot report caller drift.
            detail = '; native foreground selection unavailable for ' + str(len(selection_errors)) + ' candidate(s): '
            detail += '; '.join(str(pid) + ': ' + error for pid, error in selection_errors[:4])
        raise IdentityError('native caller discovery did not resolve a unique active client' + detail)
    # Verify every ancestor as well as the selected client after the inventory.
    for item in chain + clients:
        if process(item['pid']) != {k: v for k, v in item.items() if k != 'tty'}:
            raise IdentityError('process identity drift')
    for original, selected in selections:
        is_caller = original['pid'] == clients[0]['pid']
        try:
            current = foreground.read(original)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if is_caller:
                raise IdentityError('native foreground selection recheck failed: ' + str(exc)) from exc
            continue
        if is_caller and current != selected:
            raise IdentityError('native foreground selection changed during inventory')
        if not is_caller and matches(original, current):
            # A second authenticated match, including one recovered during the
            # scan, is real ambiguity. Unrelated thread changes are not.
            raise IdentityError('native foreground selection changed during inventory: second active client')
    proof = {'session': session, 'daemon': daemon, 'client': clients[0], 'chain': chain}
    selected = next(s for p, s in selections if p['pid'] == clients[0]['pid'])
    if selected is not None:
        proof['foreground'] = selected
    return proof


def resolve(identity, tree, env, proof):
    if proof is None:
        return identity, env
    client, daemon = proof['client'], proof['daemon']
    rows = []
    for window in tree.get('windows', []):
        for ws in window.get('workspaces', []):
            for pane in ws.get('panes', []):
                for surface in pane.get('surfaces', []):
                    rows.append(dict(surface_ref=surface.get('ref'), surface_id=surface.get('id'),
                                     workspace_ref=ws.get('ref'), workspace_id=ws.get('id'),
                                     pane_ref=pane.get('ref'), pane_id=pane.get('id'),
                                     window_ref=window.get('ref'), surface_type=surface.get('type'),
                                     dock_scope=surface.get('dock_scope'),
                                     tty=surface.get('tty')))
    def by_env(process_env):
        matches = [r for r in rows if str(r['surface_id']).upper() ==
                   str(process_env.get('CMUX_SURFACE_ID', '')).upper()]
        if len(matches) != 1 or str(matches[0]['workspace_id']).upper() != str(
                process_env.get('CMUX_WORKSPACE_ID', '')).upper():
            raise IdentityError('process UUIDs disagree with live tree')
        if matches[0]['dock_scope'] == 'global':
            raise IdentityError('global dock is not a workspace member')
        return matches[0]
    embedded = proof.get('source') == 'embedded-native-client-v1' and daemon is None
    if daemon is None and not embedded:
        raise IdentityError('native caller proof has no authenticated origin')
    source, caller = by_env((client if embedded else daemon)['env']), by_env(client['env'])
    raw = identity.get('caller') or {}
    if any(raw.get(k) != source[k] for k in ('surface_ref', 'workspace_ref', 'pane_ref')):
        raise IdentityError('identify differs from native client origin' if embedded
                            else 'identify differs from daemon origin')
    # cmux can retain the name of a recycled PTY on an unrelated surface.
    # The unique live native process and its kernel-read UUID environment
    # select the caller; a global TTY-name search must not select or veto it.
    # Still require that exact UUID row to agree with the live client's TTY.
    if (not client.get('tty') or caller['tty'] != client['tty']
            or caller['surface_type'] != 'terminal'):
        raise IdentityError('native client TTY missing or differs from UUID-bound surface')
    resolved_env = dict(env, **{k: client['env'][k] for k in
                               ('CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID')})
    return dict(identity, caller=caller), resolved_env


def public_proof(proof):
    if proof is None:
        return None
    p = proof['client']
    public = dict(session=proof['session'], pid=p['pid'], birth=p['birth'], tty=p['tty'],
                  executable=p['executable'], surface_uuid=p['env']['CMUX_SURFACE_ID'],
                  daemon_pid=proof['daemon']['pid'] if proof['daemon'] is not None else None,
                  argv_sha256=hashlib.sha256(json.dumps(p['argv']).encode()).hexdigest())
    if proof.get('source') == 'embedded-native-client-v1':
        public['source'] = proof['source']
    # caller_snapshot has rechecked this selection against the live process.
    # Root-only hook discovery needs it after projection; do not expose the
    # full control-socket record, process environment or ancestor chain.
    selected = proof.get('foreground')
    if isinstance(selected, dict) and isinstance(selected.get('thread_id'), str):
        public['foreground'] = {'thread_id': selected['thread_id']}
    return public

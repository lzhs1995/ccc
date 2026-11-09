"""官方 Codex 签名缓存（V48 私有候选）。只有显式 refresh 才跑一次完整 codesign；热路径只读受保护缓存并核身份与内容摘要。

信任边界：缓存目录 0700、文件 0600、属主为本 UID，只防别的用户和误写；同 UID 的进程能伪造缓存，也能直接改可执行文件。
ctime/mtime 只是文件系统元数据，不是密码学不可变。缓存缺失或不符一律 fail closed，不自动回退到慢速 codesign。
"""
import argparse, hashlib, json, math, os, stat, subprocess, time
from pathlib import Path
import cmux_identity_budget as budget
from contextlib import contextmanager
from contextvars import ContextVar

_validation = ContextVar('official_signature_validation', default=None)

@contextmanager
def validation_scope():
    """Full digest at entry and exit, fresh pin checks at every intervening call.

    Evidence is local to this one resolution and never survives it. A changed
    pin rejects the entire resolution, even if a replacement has a valid cache.
    Exceptions produce no successful identity and need no closing validation.
    """
    if _validation.get() is not None:
        yield
        return
    scope = {'pins': {}, 'sealing': False}
    token = _validation.set(scope)
    try:
        yield
        scope['sealing'] = True
        for executable, launcher in tuple(scope['pins']):
            ok, reason = check(executable, launcher)
            if not ok:
                raise ValueError('signature scope changed before return: ' + str(reason))
        budget.check()
    finally:
        _validation.reset(token)

SCHEMA = 'official-codex-signature-cache-v1'
REQUIREMENT = 'anchor apple generic and certificate leaf[subject.OU] = "2DC432GLL2"'
CODESIGN = '/usr/bin/codesign'
CACHE_DIR = str(Path.home() / '.local/state/multi-agent-collaboration/signatures')
MAX_BYTES = 65536

def _identity(st):
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]

def _digest(path):
    h = hashlib.sha256()
    # Hash every byte on every check, using a reused bounded buffer. Avoid the
    # repeated 8 MiB allocations/copies in a hot authentication path.
    block = bytearray(256 << 10)
    view = memoryview(block)
    with open(path, 'rb', buffering=0) as f:
        while length := f.readinto(block):
            budget.check(); h.update(view[:length])
    return h.hexdigest()

def _safe_dir(directory):
    st = os.lstat(directory)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError('cache directory not a private 0700 directory of this user')
    parent = os.stat(os.path.dirname(directory))
    if parent.st_mode & 0o022 or os.path.realpath(directory) != directory:
        raise ValueError('cache directory parent is writable by others or path has a symlink')

def cache_file(directory, executable):
    return os.path.join(directory, hashlib.sha256(str(executable).encode()).hexdigest()[:32] + '.json')
def check(executable, launcher):
    """热路径：只读缓存，不跑 codesign。返回 (ok, reason)；reason 以 SIGNATURE_REFRESH_REQUIRED 开头。"""
    budget.check(); exe = str(executable)
    try:
        if CACHE_DIR is None: raise ValueError('cache directory not configured')
        _safe_dir(CACHE_DIR)
        fd = os.open(cache_file(CACHE_DIR, exe), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
                    or st.st_mode & 0o177 or st.st_size > MAX_BYTES):
                raise ValueError('cache file not a private 0600 regular file of this user')
            def pairs(items):
                value = {}
                for key, item in items:
                    if key in value: raise ValueError('duplicate cache field')
                    value[key] = item
                return value
            rec = json.loads(os.read(fd, MAX_BYTES).decode('utf-8'), object_pairs_hook=pairs)
            fresh = os.fstat(fd)
            if _identity(st) != _identity(fresh): raise ValueError('cache changed during read')
        finally: os.close(fd)
        if (not isinstance(rec, dict) or rec.get('schema') != SCHEMA or rec.get('requirement') != REQUIREMENT
                or rec.get('executable') != exe or type(rec.get('codesign_rc')) is not int
                or rec.get('codesign_rc') != 0 or rec.get('uid') != os.getuid()):
            raise ValueError('cache schema/requirement/path/result mismatch')
        if rec.get('launcher_realpath') != os.path.realpath(launcher):
            raise ValueError('launcher target changed since refresh')
        if rec.get('identity') != _identity(os.stat(exe)):
            raise ValueError('executable identity drift since refresh')
        scope = _validation.get()
        key = (exe, str(launcher))
        pin = (tuple(rec['identity']), rec.get('sha256'), rec.get('launcher_realpath'))
        previous = scope['pins'].get(key) if scope is not None else None
        if previous is not None and previous != pin:
            raise ValueError('verified executable pin changed within resolution')
        reuse = previous is not None and not scope['sealing']
        if (not reuse and rec.get('sha256') != _digest(exe) or rec.get('identity') != _identity(os.stat(exe))
                or rec.get('launcher_realpath') != os.path.realpath(launcher)):
            raise ValueError('executable content digest or launcher target drift since refresh')
        if scope is not None and previous is None:
            if len(scope['pins']) >= 16:
                raise ValueError('signature resolution capacity exceeded')
            scope['pins'][key] = pin
    except (OSError, ValueError, UnicodeDecodeError) as e:
        budget.check(); return False, 'SIGNATURE_REFRESH_REQUIRED: %s: %s' % (type(e).__name__, e)
    budget.check(); return True, None

def _local_cap(timeout):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError('refresh timeout must be finite and within (0, 60] seconds')
    return float(timeout)

def refresh(executable, launcher, directory, timeout=60):
    """显式刷新：至多一次真实只读 codesign；超时取 min(本地上限, 共享剩余预算)，入口/返回/超时处都查共享截止，过期不发布。"""
    cap = _local_cap(timeout); budget.check(); exe = str(executable)
    id0, d0, l0 = _identity(os.stat(exe)), _digest(exe), os.path.realpath(launcher); t0 = time.monotonic()
    try:
        r = subprocess.run([CODESIGN, '--verify', '--strict', '-R=' + REQUIREMENT, exe], capture_output=True, text=True, timeout=budget.timeout(cap))
    except subprocess.TimeoutExpired:
        budget.check()  # 共享截止已过则上抛；只是 codesign 自身超时则不发布
        return dict(published=False, codesign_timeout=True, codesign_seconds=round(time.monotonic() - t0, 4), executable=exe)
    budget.check()
    rec = dict(schema=SCHEMA, requirement=REQUIREMENT, executable=exe, launcher_realpath=l0, uid=os.getuid(),
               identity=id0, sha256=d0, codesign_rc=r.returncode, codesign_stdout=r.stdout, codesign_stderr=r.stderr,
               codesign_seconds=round(time.monotonic() - t0, 4), refreshed_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    if r.returncode != 0 or _identity(os.stat(exe)) != id0 or _digest(exe) != d0 or os.path.realpath(launcher) != l0:
        return dict(rec, published=False)
    budget.check(); os.makedirs(directory, mode=0o700, exist_ok=True); _safe_dir(directory)
    final = cache_file(directory, exe); tmp = final + '.%d.tmp' % os.getpid()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f: json.dump(rec, f, ensure_ascii=False); f.flush(); os.fsync(f.fileno())
    budget.check()  # V50：写入、flush、fsync 并关闭临时缓存之后、发布之前再查共享截止；已过期则不发布最终缓存
    os.replace(tmp, final)
    return dict(rec, published=True, cache=final)

def main(argv=None):
    """私有 JSON 入口：status 只读、绝不跑 codesign；refresh 只在显式调用时跑，服从 --budget-seconds 的有限预算。"""
    ap = argparse.ArgumentParser(); ap.add_argument('mode', choices=('status', 'refresh')); ap.add_argument('--cache-dir', required=True)
    ap.add_argument('--executable', required=True); ap.add_argument('--launcher', required=True); ap.add_argument('--budget-seconds', type=float, default=60)
    a = ap.parse_args(argv)
    if not os.path.isabs(a.cache_dir) or os.path.realpath(a.cache_dir) != a.cache_dir:
        print(json.dumps(dict(status='INVALID_CACHE_DIR', cache_dir=a.cache_dir))); return 2
    global CACHE_DIR
    CACHE_DIR = a.cache_dir
    if a.mode == 'status':
        try:
            with budget.limit(_local_cap(a.budget_seconds)):
                ok, why = check(a.executable, a.launcher)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            ok, why = False, 'SIGNATURE_REFRESH_REQUIRED: %s: %s' % (type(error).__name__, error)
        print(json.dumps(dict(status='VERIFIED_CACHE' if ok else 'SIGNATURE_REFRESH_REQUIRED', reason=why, cache_dir=CACHE_DIR), ensure_ascii=False)); return 0 if ok else 3
    try:
        cap = _local_cap(a.budget_seconds)
        with budget.limit(cap): rec = refresh(a.executable, a.launcher, CACHE_DIR, timeout=cap)
    except (ValueError, OSError, subprocess.TimeoutExpired) as e: rec = dict(published=False, error='%s: %s' % (type(e).__name__, e))
    print(json.dumps(dict(status='VERIFIED_CACHE' if rec.get('published') else 'SIGNATURE_REFRESH_REQUIRED',
                          **{k: rec.get(k) for k in ('codesign_rc', 'codesign_seconds', 'codesign_timeout', 'cache', 'error')}), ensure_ascii=False))
    return 0 if rec.get('published') else 3

if __name__ == '__main__':
    raise SystemExit(main())

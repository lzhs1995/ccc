# CCC 托管净化代理服务（V24 候选，基于 A23 29e1e1e9）：在私有状态目录里启动/核验一个只属于本服务的 loopback 代理子进程。
# 子进程身份 = pid + 内核出生时刻（CCC ccc_guard_scope.birth）+ 脚本 SHA256 + 端口 + 上游，
# 四项都由子进程 /health 自报并与状态记录比对。端口被别的进程占用时只拒绝，不杀、不接管。
# V24：ensure/status/stop 由本服务私有 flock 串行（不是全机锁）；记录用 O_EXCL 唯一临时文件发布，
# no-follow 有界读取并逐字段校验，坏记录/外来配置记录一律拒绝且原样保留；启动后任何失败只清理
# 本次 Popen、仍与捕获出生时刻一致的子进程；stop 只删除自己读到的那一份记录。
# 状态文件不含任何密钥；代理本身不持有密钥（Authorization 由各 Codex 客户端自带）。
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HEALTH_TIMEOUT = 2.0      # 单次 /health 上限（秒）
START_DEADLINE = 10.0     # 新子进程必须在这么久内自报健康
STOP_GRACE = 5.0          # SIGTERM 后等待退出的上限，超时才 SIGKILL
LOCK_WAIT = 30.0          # 等本服务锁的上限，大于一次完整 ensure 的最长耗时
RECORD_MAX_BYTES = 65536
SCHEMA = "ccc-proxy-service-v1"
RECORD_KEYS = ("script", "script_sha256", "port", "upstream")


class ServiceError(RuntimeError):
    pass


def file_sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def health(port, timeout=HEALTH_TIMEOUT):
    """GET http://127.0.0.1:port/health；显式空 ProxyHandler，不经任何 HTTP(S)_PROXY。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d/health" % port, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            value = json.loads(resp.read(65536))
            return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def port_in_use(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def _default_birth():
    # 安装进 CCC release 时与 ccc_guard_scope.py 同目录；测试可用 CCC_RELEASE_DIR 指定只读 release
    release = os.environ.get("CCC_RELEASE_DIR") or str(Path(__file__).resolve().parent)
    if release not in sys.path:
        sys.path.insert(0, release)
    from ccc_guard_scope import birth
    return birth


def _owned_regular(fd, what):
    """fd 必须是本 uid 的普通文件、仅一个硬链接、无组/他人权限。"""
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1
            or st.st_mode & 0o077):
        raise ServiceError("%s_INVALID not an owned private regular file" % what)
    return st


class ProxyService:
    def __init__(self, state_dir, script, script_sha256, port, upstream, python=None, birth=None):
        self.state_dir = Path(os.path.abspath(state_dir))
        self.script = str(Path(script).resolve())
        self.script_sha256 = script_sha256
        self.port = int(port)
        self.upstream = upstream
        self.python = python or sys.executable
        self.birth = birth or _default_birth()
        self.state_path = self.state_dir / "proxy-service.json"
        self.lock_path = self.state_dir / "proxy-service.lock"

    def _config(self):
        return {"script": self.script, "script_sha256": self.script_sha256, "port": self.port, "upstream": self.upstream}

    def _private_dir(self, create):
        if create:
            try:
                os.mkdir(self.state_dir, 0o700)
            except FileExistsError:
                pass
        try:
            st = os.lstat(self.state_dir)  # lstat：符号链接目录在这里就被拒，不跟随
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ServiceError("STATE_DIR_INVALID %s" % self.state_dir)
        return True

    @contextlib.contextmanager
    def _locked(self, create):
        """本服务（本状态目录）唯一一把锁；no-follow 打开并校验后 flock，等待有上限。"""
        if not self._private_dir(create):
            yield False
            return
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_CREAT if create else 0)
        try:
            fd = os.open(self.lock_path, flags, 0o600)
        except FileNotFoundError:
            yield False
            return
        except OSError as exc:
            raise ServiceError("LOCK_INVALID %s: %s" % (self.lock_path, exc.strerror))
        try:
            _owned_regular(fd, "LOCK")
            deadline = time.monotonic() + LOCK_WAIT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ServiceError("LOCK_TIMEOUT %s" % self.lock_path)
                    time.sleep(0.05)
            yield True
        finally:
            os.close(fd)  # 关闭即释放 flock

    def _read_record(self):
        """返回 (记录, 指纹) 或 (None, None)。坏记录、外来配置记录抛错且不改动文件。"""
        try:
            fd = os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            raise ServiceError("RECORD_INVALID %s: %s" % (self.state_path, exc.strerror))
        try:
            st = _owned_regular(fd, "RECORD")
            if st.st_size > RECORD_MAX_BYTES:
                raise ServiceError("RECORD_INVALID too large")
            parts, total = [], 0
            while True:
                part = os.read(fd, RECORD_MAX_BYTES + 1 - total)
                if not part:
                    break
                parts.append(part)
                total += len(part)
                if total > RECORD_MAX_BYTES:
                    raise ServiceError("RECORD_INVALID too large")
        finally:
            os.close(fd)
        raw = b"".join(parts)
        try:
            record = json.loads(raw)
        except ValueError:
            raise ServiceError("RECORD_INVALID not json")
        if not self._well_formed(record):
            raise ServiceError("RECORD_INVALID schema/types")
        if {k: record[k] for k in RECORD_KEYS} != self._config():
            raise ServiceError("RECORD_CONFIG_MISMATCH recorded=%s" % json.dumps({k: record[k] for k in RECORD_KEYS}))
        return record, (st.st_dev, st.st_ino, hashlib.sha256(raw).hexdigest())

    @staticmethod
    def _well_formed(record):
        def is_int(v):
            return type(v) is int
        return (isinstance(record, dict) and record.get("schema") == SCHEMA
                and is_int(record.get("pid")) and 1 < record["pid"] < 2 ** 31
                and isinstance(record.get("birth"), list) and len(record["birth"]) == 2
                and all(is_int(v) and v >= 0 for v in record["birth"]) and record["birth"][0] > 0
                and isinstance(record.get("script"), str) and isinstance(record.get("upstream"), str)
                and isinstance(record.get("script_sha256"), str) and len(record["script_sha256"]) == 64
                and is_int(record.get("port")) and 0 < record["port"] < 65536)

    def _publish(self, tmp):
        os.replace(tmp, self.state_path)  # 测试可在此注入失败

    def _save(self, record):
        tmp = self.state_dir / (".proxy-service.%s.tmp" % secrets.token_hex(8))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(record, fh, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            self._publish(tmp)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)  # 只删本次 O_EXCL 建的临时文件
            raise

    def _same_child(self, record):
        """记录里的 pid 仍是同一个内核进程（出生时刻相同，非僵尸）。"""
        actual = self.birth(record["pid"]) if record else None
        return actual is not None and list(actual) == list(record["birth"])

    def _healthy_as(self, record):
        h = health(self.port)
        return bool(h) and h.get("pid") == record["pid"] and h.get("script_sha256") == self.script_sha256 \
            and h.get("port") == self.port and h.get("upstream") == self.upstream

    def _status(self):
        record, _ = self._read_record()
        if not record:
            return "none", None
        if not self._same_child(record):
            return "dead", record
        return ("live" if self._healthy_as(record) else "unhealthy"), record

    def status(self):
        with self._locked(create=False) as present:
            return self._status() if present else ("none", None)

    def ensure(self):
        """返回 (动作, 记录)。动作 = reused | started；其余情况抛 ServiceError，不杀任何外来进程。"""
        with self._locked(create=True):
            if file_sha256(self.script) != self.script_sha256:  # 复用路径也要求脚本 pin
                raise ServiceError("SCRIPT_PIN_MISMATCH %s" % self.script)
            state, record = self._status()
            if state == "live":
                return "reused", record
            if state == "unhealthy":
                # 自己的子进程还活着但不健康：不自动杀，交给显式 stop
                raise ServiceError("OWN_CHILD_UNHEALTHY pid=%s" % record["pid"])
            if port_in_use(self.port):
                h = health(self.port) or {}
                raise ServiceError("PORT_OCCUPIED_FOREIGN port=%d health_pid=%s" % (self.port, h.get("pid")))
            return "started", self._start()

    def _start(self):
        env = dict(os.environ)
        env.update(SANITIZER_PORT=str(self.port), SANITIZER_UPSTREAM=self.upstream,
                   SANITIZER_LOG=str(self.state_dir / "proxy.log"))
        # loopback 自身不得经 Clash；上游 HTTPS_PROXY 原样保留
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
        out = os.open(self.state_dir / "proxy.stdout.txt",
                      os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            child = subprocess.Popen([self.python, "-I", "-B", self.script], stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=subprocess.STDOUT, env=env,
                                     start_new_session=True, close_fds=True)
        finally:
            os.close(out)
        born = self.birth(child.pid)
        try:
            if born is None:
                raise ServiceError("CHILD_BIRTH_UNREADABLE pid=%d" % child.pid)
            deadline = time.monotonic() + START_DEADLINE
            while True:
                if child.poll() is not None:
                    raise ServiceError("CHILD_EXITED rc=%s" % child.returncode)
                h = health(self.port, timeout=0.5)
                if h and h.get("pid") == child.pid:
                    break
                if time.monotonic() >= deadline:
                    raise ServiceError("CHILD_NOT_HEALTHY_BY_DEADLINE pid=%d" % child.pid)
                time.sleep(0.1)
            if self.birth(child.pid) != born:
                raise ServiceError("CHILD_BIRTH_CHANGED pid=%d" % child.pid)
            record = {"schema": SCHEMA, "pid": child.pid, "birth": list(born),
                      "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **self._config()}
            if not self._healthy_as(record):
                raise ServiceError("CHILD_HEALTH_MISMATCH pid=%d" % child.pid)
            try:
                self._save(record)
            except OSError as exc:
                raise ServiceError("STATE_PUBLISH_FAILED %s" % exc)
            return record
        except BaseException:
            self._reap_failed(child, born)
            raise

    def _reap_failed(self, child, born):
        """只清理本次 Popen 的子进程：尚未回收（pid 不可能被复用）且出生时刻仍是捕获值。"""
        if child.poll() is None and (born is None or self.birth(child.pid) == born):
            child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(STOP_GRACE)

    def stop(self):
        """只停本服务记录里、出生时刻仍一致的子进程；返回 stopped | not_running。"""
        with self._locked(create=False) as present:
            if not present:
                return "not_running"
            record, fingerprint = self._read_record()
            if not record or not self._same_child(record):
                return "not_running"
            os.kill(record["pid"], signal.SIGTERM)
            deadline = time.monotonic() + STOP_GRACE
            while time.monotonic() < deadline and self._same_child(record):
                time.sleep(0.05)
            if self._same_child(record):
                os.kill(record["pid"], signal.SIGKILL)
            self._unlink_if_same(fingerprint)
            return "stopped"

    def _unlink_if_same(self, fingerprint):
        """只删除仍是 stop 读到的那一份记录（同 dev/ino、同内容）；已被替换就保留。"""
        try:
            fd = os.open(self.state_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return
        try:
            st = os.fstat(fd)
            raw = os.read(fd, RECORD_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if (st.st_dev, st.st_ino, hashlib.sha256(raw).hexdigest()) == fingerprint:
            os.unlink(self.state_path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="CCC managed sanitizer proxy service")
    parser.add_argument("action", choices=("ensure", "status", "stop"))
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--script-sha256", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--upstream", required=True)
    args = parser.parse_args(argv)
    service = ProxyService(args.state_dir, args.script, args.script_sha256, args.port, args.upstream)
    try:
        if args.action == "ensure":
            action, record = service.ensure()
            print(json.dumps({"action": action, "record": record}))
        elif args.action == "status":
            state, record = service.status()
            print(json.dumps({"state": state, "record": record}))
        else:
            print(json.dumps({"action": service.stop()}))
    except ServiceError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

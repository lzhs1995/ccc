#!/usr/bin/env python3
"""cmux-stack 测试矩阵 —— 全部对着 fake 跑，绝不碰真实服务。

这套测试的核心约束，每一条都有具体的事故来源：

  1. **fake launchctl 必须记账**。控制器的 up/down 会 bootstrap/bootout 真实
     label。测试用一个把 argv 写进沙箱日志的假 launchctl，然后断言日志里
     【从未】出现真实 label 的 bootout。只断言「测试通过」不够 —— 必须证明
     沙箱操作没有溢出到真实域。
  2. **不扫真实 ~/.cmuxterm**。janitor 那侧的清扫面在真实隔离区上，控制器只读
     janitorctl 的 JSON，永远不自己走目录。用例拦截被测进程的目录访问并用
     负向探针验证拦截有效；真实 cmux 并发写日志不能使隔离检查误报。
  3. **凭据不得出现在控制器输出里**。fake ccp 的 profile 里放一个哨兵 token，
     断言 cmux-stack 的任何输出（stdout/stderr/JSON 全文）都不含它。
     这是白名单投影的回归闸门：上游多一个字段也不能漏过来。
  4. **组件失败必须隔离**。一个探针崩了，另外两个仍要如实上报；不能因为一个
     组件坏掉就整体 unknown。
  5. **up 对健康 watcher 必须是 no-op**。重启会丢在飞的 episode 状态，
     「已健康」只能是不动，不能是「稳妥起见重启一下」。
"""

from __future__ import annotations

import json
import os
import pathlib
import pty
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STACK = Path(__file__).resolve().parent.parent / "bin" / "cmux-stack"

EXIT_OK, EXIT_DEGRADED, EXIT_USAGE, EXIT_FAIL_CLOSED, EXIT_ABSENT = 0, 1, 2, 3, 4

# 真实 label / 路径：只用于「证明没被碰」的反向断言，测试从不对它们动手。
# 【label 前缀必须和被测代码同源派生，不能写死账号名】写死 `com.<某个账号>` 有两个
# 问题：一是发布源码会泄露账号名；二是这些常量的**唯一作用**是反向断言「沙箱操作没有
# 溢出到真实域」，而「真实域」就是 cmux-stack 自己算出来的那三个 label。写死的字面量
# 在换账号的机器上会指向一组根本不存在的 label，于是「从未 bootout 真实 label」这条
# 断言变成空转——它检查的是一个不可能被碰到的名字。所以这里按被测代码同一规则派生。
LABEL_PREFIX = f"com.{os.environ.get('USER') or Path.home().name or 'user'}"
REAL_LABELS = (f"{LABEL_PREFIX}.cmux-codex-continue", f"{LABEL_PREFIX}.cmux-janitor",
               f"{LABEL_PREFIX}.cmux-janitor-guard")
REAL_CMUXTERM = Path.home() / ".cmuxterm"

SENTINEL_TOKEN = "sk-ant-SENTINEL-must-never-be-printed-9f3a1c"

results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


# ------------------------------------------------------------------ fake 装置

FAKE_LAUNCHCTL = """#!/bin/sh
# 假 launchctl：把每次调用追加到日志，返回码按【每个 label】决定。
#
# 【必须支持按 label 区分】P1-1/P1-2 的核心场景是「清扫器已加载、守卫没加载」这类
# 单边失效——106.14.4 的事故就是只有一个 label 被踢掉。全局 FAKE_LAUNCHCTL_RC
# 只能同时改两个 label，测不出单边，也就测不出「守卫不被调度时永不跳闸」。
#
# FAKE_LAUNCHCTL_UNLOADED：逗号分隔的 label 列表，命中则 rc=1（未加载）
# FAKE_LAUNCHCTL_RC      ：全局兜底（用来模拟 launchctl 整体不可用）
#
# 【匹配必须是精确尾串 */$lbl，不能是 *$lbl*】实测：`<prefix>.cmux-janitor` 是
# `<prefix>.cmux-janitor-guard` 的前缀，用包含匹配「只卸清扫器」会把守卫一起卸掉，
# 于是「单边失效」用例根本测不到单边——两个 label 同时 rc=1，断言却仍会绿。
# 这正是本轮要防的那类装置自造假象，所以判据钉在 `print gui/<uid>/<label>` 的行尾。
# bootstrap 的参数以 .plist 结尾，天然不匹配，写操作路径不受影响。
printf '%s\\n' "$*" >> "$FAKE_LAUNCHCTL_LOG"
old_ifs=$IFS
IFS=,
for lbl in ${FAKE_LAUNCHCTL_UNLOADED:-}; do
  IFS=$old_ifs
  [ -n "$lbl" ] || continue
  case "$*" in
    *"/$lbl") exit 1 ;;
  esac
  IFS=,
done
IFS=$old_ifs
exit "${FAKE_LAUNCHCTL_RC:-0}"
"""

FAKE_PROBE = """#!/usr/bin/env python3
# 假被控组件：把预置的 JSON 原样吐出，按 FAKE_RC 决定返回码。
# FAKE_PAYLOAD 指向一个文件；内容不是 JSON 时故意吐坏数据，用来测 fail-closed。
import os, sys
payload = os.environ.get("FAKE_PAYLOAD_FILE", "")
if payload and os.path.isfile(payload):
    sys.stdout.write(open(payload, encoding="utf-8").read())
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""


def watcher_status(*, pid_alive=True, paused=False, pid=4242,
                   source_matches_disk=True, targets=3) -> dict:
    return {
        "mode": "auto",
        "global_paused": paused,
        "daemon": {"pid": pid, "pid_alive": pid_alive,
                   "source_matches_disk": source_matches_disk,
                   "runtime_metadata_present": True,
                   "source_sha256": "deadbeef" * 8},
        "explicit_targets": [{"surface_ref": f"surface:{i}"} for i in range(targets)],
        "workspace_rules": [],
        "observation_coverage": {"status": "ok", "scope": "authorized_targets",
                                 "counts": {"readable": targets, "live_unreadable": 0,
                                            "dormant": 0, "paused": 0, "missing": 0, "unknown": 0}},
        "claude_hook_coverage": {"status": "ok"},
        "continuation_health": {"status": "ok", "counts": {"ok": targets}},
        # 控制器【绝不能】把这一坨投影出去：真实环境下这里是 ~300KB 会话状态。
        "runtime": {"sessions": {"secret-session": {"token": SENTINEL_TOKEN}}},
    }


def janitor_status(*, paused=False, tripped=False, health="healthy",
                   violations=None, j_stale=False, g_stale=False, batches=7,
                   j_present=True, j_invalid=False,
                   g_present=True, g_invalid=False) -> dict:
    """janitorctl 公开投影的桩。

    present/invalid 必须出现在桩里：真实 janitorctl 一直发布它们，而控制器起初
    没读——桩若也不发，测试就永远测不到这条「生产者写了、消费者不读」的漂移
    （同 106.14.4）。桩的字段集必须覆盖真实生产者的字段集，否则本地全绿而生产
    有洞（同 [[newapi-fixture-fidelity-false-green]]）。
    """
    return {
        "schema_version": 1,
        "control": {"paused": paused, "guard_tripped": tripped},
        "launchd": {"janitor_loaded": True, "guard_loaded": True},
        "janitor": {"stale": j_stale, "run_id": "r-1", "phase": "idle",
                    "present": j_present, "invalid": j_invalid},
        "quarantine": {"batch_count": batches, "keep_hours": 48},
        "guard": {"health": health, "stale": g_stale, "violations": violations or [],
                  "present": g_present, "invalid": g_invalid},
    }


def ccp_status(*, unhealthy=0, count=5, template=True, manifest=True) -> dict:
    return {
        "schema_version": 1,
        "component": "ccp-new",
        "read_only": True,
        "profile_dir_present": True,
        "source_sha256": "cafe" * 16,
        "template": {"present": True, "healthy": template, "key_count": 37},
        "manifest": {"present": True, "healthy": manifest, "union_key_count": 37},
        "profile_count": count,
        "unhealthy_count": unhealthy,
        # 白名单回归：上游即便真的吐了凭据，控制器也不该带出去。
        "profiles": [{"name": "alpha", "has_credential": True,
                      "leaked_token_should_not_pass": SENTINEL_TOKEN}],
    }


class Sandbox:
    """一次性沙箱：假 HOME、假 launchctl、三个假组件。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="cmux-stack-test-"))
        self.home = self.root / "home"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        (self.home / ".config" / "cmux-janitor").mkdir(parents=True)
        (self.home / ".claude-profiles").mkdir(parents=True)

        self.launchctl_log = self.root / "launchctl.log"
        self.launchctl_log.touch()
        self.launchctl = self._script("launchctl", FAKE_LAUNCHCTL)

        self.watcher = self._script("cmux_codex_watch.py", FAKE_PROBE)
        self.janitorctl = self._script("cmux-janitorctl", FAKE_PROBE)
        self.ccp = self._script("_ccp_new.py", FAKE_PROBE)

        self.payloads = self.root / "payloads"
        self.payloads.mkdir()

    def _script(self, name: str, body: str) -> Path:
        p = self.root / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
        return p

    def payload(self, name: str, data) -> Path:
        p = self.payloads / f"{name}.json"
        p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return p

    def env(self, *, watcher=None, janitor=None, ccp=None,
            watcher_rc=0, janitor_rc=0, ccp_rc=0,
            launchctl_rc=0, unloaded=(), missing=()) -> dict:
        e = dict(os.environ)
        e["HOME"] = str(self.home)
        e["FAKE_LAUNCHCTL_LOG"] = str(self.launchctl_log)
        e["FAKE_LAUNCHCTL_RC"] = str(launchctl_rc)
        # 逐 label 控制「是否已加载」。P1-1 的核心状态是「一个 label 掉了、另一个
        # 还在」，用全局 rc 表达不了：全局 rc=1 会让三个 label 一起变 False，
        # 于是测不出「守卫掉了但清扫器还在」这类真实事故形态（§106.14.1 就是
        # 只有 janitor label 被踢掉）。
        e["FAKE_LAUNCHCTL_UNLOADED"] = ",".join(unloaded)
        e["CMUX_STACK_LAUNCHCTL"] = str(self.launchctl)
        e["CMUX_STACK_TIMEOUT"] = "20"
        e["CMUX_STACK_PYTHON"] = sys.executable
        e["CMUX_STACK_WATCHER"] = str(self.root / "missing-watcher"
                                      if "watcher" in missing else self.watcher)
        e["CMUX_STACK_JANITORCTL"] = str(self.root / "missing-janitorctl"
                                         if "janitor" in missing else self.janitorctl)
        e["CMUX_STACK_CCP"] = str(self.root / "missing-ccp"
                                  if "profiles" in missing else self.ccp)
        # 三个假组件共用一个 FAKE_PAYLOAD_FILE 不行 —— 后写的会盖掉先写的，
        # 三个探针就会读到同一份 payload。所以每个组件生成自己的入口脚本，
        # payload 与 rc 直接钉死在脚本里，不经环境变量。
        e["FAKE_RC"] = "0"
        # `missing` 必须胜过 payload：_wrap 会覆写同一个环境变量，若无条件调用，
        # 那么 env(ccp=..., missing=("profiles",)) 会先指向缺失路径、再被包装器
        # 覆盖回存在的路径 —— 「组件缺失」用例于是测不到缺失分支，还报全绿。
        if "watcher" not in missing:
            self._wrap(e, "watcher", watcher, watcher_rc)
        if "janitor" not in missing:
            self._wrap(e, "janitor", janitor, janitor_rc)
        if "profiles" not in missing:
            self._wrap(e, "profiles", ccp, ccp_rc)
        return e

    def _wrap(self, e: dict, which: str, data, rc: int) -> None:
        """给每个假组件生成独立入口，钉住它自己的 payload 与 rc。

        【必须是 Python 而不是 sh 包装】控制器用 [PYTHON, path, ...] 调探针，
        路径是被当作 Python 源码交给解释器的，不走 shebang。早前这里写成
        `#!/bin/sh` + exec，于是 python3 去解析 `exec "..." "$@"`，抛
        SyntaxError、stdout 为空，控制器如实报「not JSON」——测试于是测的是
        「假装置坏了」而不是产品行为。这类自造伪缺陷必须钉在装置侧。
        """
        if data is None:
            return
        pf = self.payload(which, data)
        w = self.root / f"wrap-{which}.py"
        # json.dumps 兼作 Python 字符串字面量转义，路径含空格/反斜杠也安全。
        w.write_text(
            "import sys\n"
            f"sys.stdout.write(open({json.dumps(str(pf))}, encoding='utf-8').read())\n"
            f"sys.exit({int(rc)})\n",
            encoding="utf-8")
        w.chmod(0o755)
        key = {"watcher": "CMUX_STACK_WATCHER", "janitor": "CMUX_STACK_JANITORCTL",
               "profiles": "CMUX_STACK_CCP"}[which]
        e[key] = str(w)

    def launchctl_calls(self) -> list[str]:
        return [l for l in self.launchctl_log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def launchctl_calls_write(self) -> list[str]:
        """只返回【写】类调用（bootstrap / bootout / kickstart / enable / disable）。

        `print` 是只读探测，status/doctor 每次都会发一堆；用总调用数当「没写过」的
        判据会把只读探测算成副作用，于是断言永远失败。区分读写才是正确判据
        （同「日志字段先查语义再当判据」）。
        """
        writes = ("bootstrap", "bootout", "kickstart", "enable", "disable", "unload", "load")
        return [c for c in self.launchctl_calls()
                if c.split()[0:1] and c.split()[0] in writes]

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def run_stack(sb: Sandbox, args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(STACK), *args],
                          capture_output=True, text=True,
                          env=env if env is not None else sb.env(), timeout=120)


# ---------------------------------------------------------------------- 用例

def case_freshness_is_separate_from_health():
    sb = Sandbox()
    try:
        for value in (None, "unknown", False, True):
            env = sb.env(watcher=watcher_status(source_matches_disk=value))
            p = run_stack(sb, ["status", "--json", "--component", "watcher"], env)
            data = json.loads(p.stdout)
            row = data["components"]["watcher"]
            expected = value if isinstance(value, bool) else None
            codes = (["source_drift"] if value is False else
                     ["source_provenance_unknown"] if expected is None else [])
            record(f"source provenance {value!r} retains tri-state and operational health",
                   p.returncode == 0 and data["overall"] == "ok"
                   and row["source_matches_disk"] is expected and row["warnings"] == codes
                   and data["warning_count"] == len(codes), str(row))
        payload = watcher_status()
        del payload["daemon"]["source_matches_disk"]
        p = run_stack(sb, ["status", "--json", "--component", "watcher"], sb.env(watcher=payload))
        row = json.loads(p.stdout)["components"]["watcher"]
        record("missing provenance is unknown, not proven drift",
               row["source_matches_disk"] is None and "source_drift" not in row["warnings"], str(row))
    finally:
        sb.cleanup()


def case_janitor_source_comparison_is_read_only_and_bounded():
    sb = Sandbox()
    try:
        payload = janitor_status(paused=True, j_stale=True)
        payload["janitor"]["observed_at"] = "2026-09-05T01:02:03Z"
        payload["guard"]["observed_at"] = "not-a-timestamp"
        env = sb.env(janitor=payload)
        source = sb.root / "source"
        source.mkdir()
        env["CMUX_STACK_JANITOR_SOURCE"] = str(source)
        names = ("cmux-janitor.sh", "guard.sh", "cmux-janitorctl", "config.env")
        for name in names:
            (source / name).write_text("fixture bytes\n")
            (sb.root / name).write_text("fixture bytes\n")
        (source / "guard.sh").write_text("reviewed replacement\n")
        before = {str(p): p.read_bytes() for directory in (source, sb.root)
                  for p in directory.iterdir() if p.is_file() and p != sb.launchctl_log}
        p = run_stack(sb, ["status", "--json", "--component", "janitor"], env)
        data = json.loads(p.stdout)
        row = data["components"]["janitor"]
        comparisons = row["artifact_sources"]
        record("janitor compares four artifacts without redefining paused health",
               p.returncode == 0 and row["healthy"] is True and set(comparisons) == set(names)
               and comparisons["guard.sh"]["matches"] is False
               and comparisons["config.env"]["matches"] is True
               and row["warnings"] == ["installed_source_drift", "janitor_observation_stale"], str(row))
        record("observation timestamp is bounded and validated",
               row["janitor_observed_at"] == "2026-09-05T01:02:03Z" and row["guard_observed_at"] is None, str(row))
        record("source comparison has no file or launchd writes",
               all(Path(path).read_bytes() == raw for path, raw in before.items())
               and sb.launchctl_calls_write() == [], "read-only fingerprints")
        env["CMUX_STACK_JANITOR_SOURCE"] = str(sb.root / "absent-source")
        p = run_stack(sb, ["status", "--json", "--component", "janitor"], env)
        row = json.loads(p.stdout)["components"]["janitor"]
        record("absent source reference yields unknown comparison, not drift",
               all(item["matches"] is None for item in row["artifact_sources"].values())
               and "source_reference_unavailable" in row["warnings"]
               and "installed_source_drift" not in row["warnings"], str(row))
    finally:
        sb.cleanup()


def case_all_healthy_schema():
    """全健康 → exit 0，且 JSON 携带完整 schema 字段。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        ok = (p.returncode == EXIT_OK
              and data["schema_version"] == 1
              and data["controller"] == "cmux-stack"
              and data["read_only"] is True
              and data["overall"] == "ok"
              and data["probed_count"] == 3
              and set(data["components"]) == {"watcher", "janitor", "profiles"})
        record("全健康 → exit 0 且 status JSON schema 完整", ok,
               f"rc={p.returncode} overall={data.get('overall')} probed={data.get('probed_count')}")

        # 每个组件都必须 probe_ok 且 healthy
        rows = data["components"]
        ok2 = all(rows[c]["probe_ok"] and rows[c]["healthy"] is True for c in rows)
        record("三组件各自 probe_ok + healthy", ok2,
               " ".join(f"{c}={rows[c]['healthy']}" for c in rows))
    finally:
        sb.cleanup()


def case_no_credentials_leak():
    """哨兵 token 在上游 payload 里，控制器任何输出都不得出现。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        blobs = []
        for args in (["status", "--json"], ["status"], ["doctor", "--json"], ["doctor"],
                     ["up", "--json"], ["update", "--plan", "--json"]):
            p = run_stack(sb, args, env)
            blobs.append(p.stdout + p.stderr)
        joined = "\n".join(blobs)
        leaked = SENTINEL_TOKEN in joined
        record("凭据/会话哨兵不出现在任何控制器输出中", not leaked,
               f"检查了 {len(blobs)} 个输出面，共 {len(joined)} 字节")

        # 另外证明 runtime 巨块没有被整体投影
        data = json.loads(run_stack(sb, ["status", "--json"], env).stdout)
        w = data["components"]["watcher"]
        ok = "runtime" not in w and "sessions" not in json.dumps(w)
        record("watcher runtime 大块未被整体投影（白名单而非过滤）", ok,
               f"watcher 字段={sorted(w)[:6]}…")
    finally:
        sb.cleanup()


def case_degraded_watcher_dead():
    """watcher pid 不存活 → degraded，exit 1。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(pid_alive=False),
                     janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        ok = (p.returncode == EXIT_DEGRADED and data["overall"] == "degraded"
              and data["unhealthy"] == ["watcher"]
              and data["components"]["watcher"]["reason"] == "daemon pid not alive")
        record("watcher 死 → exit 1 degraded 且点名 watcher", ok,
               f"rc={p.returncode} unhealthy={data.get('unhealthy')}")
    finally:
        sb.cleanup()


def case_paused_janitor_is_healthy():
    """暂停的 janitor 是运维状态不是故障：healthy=True，但 reason 说明已暂停。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(paused=True),
                     ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        j = data["components"]["janitor"]
        ok = (p.returncode == EXIT_OK and j["healthy"] is True
              and j["paused"] is True and "paused" in (j["reason"] or ""))
        record("暂停的 janitor 判为健康（不诱导 rearm）", ok,
               f"rc={p.returncode} healthy={j['healthy']} reason={j['reason']}")
    finally:
        sb.cleanup()


def case_guard_tripped_degraded():
    """守卫跳闸 → 不健康，且文案明说需人工，不自动 rearm。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(),
                     janitor=janitor_status(tripped=True, health="tripped"),
                     ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        j = data["components"]["janitor"]
        ok = (p.returncode == EXIT_DEGRADED and j["healthy"] is False
              and j["guard_tripped"] is True and "no auto-rearm" in (j["reason"] or ""))
        record("守卫跳闸 → degraded 且拒绝自动 rearm", ok,
               f"rc={p.returncode} reason={j['reason']}")

        # 跳闸时也不许有任何 launchctl 写操作
        calls = sb.launchctl_calls()
        bad = [c for c in calls if "bootout" in c or "bootstrap" in c]
        record("跳闸状态下 status 未发起任何 launchctl 写操作", not bad, f"写操作={bad}")
    finally:
        sb.cleanup()


def case_guard_violations_degraded():
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(),
                     janitor=janitor_status(violations=["baseline drift", "mode mismatch"]),
                     ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        j = json.loads(p.stdout)["components"]["janitor"]
        ok = (p.returncode == EXIT_DEGRADED and j["healthy"] is False
              and j["violation_count"] == 2)
        record("守卫有 violation → degraded 且只报计数不报正文", ok,
               f"rc={p.returncode} count={j['violation_count']} reason={j['reason']}")
    finally:
        sb.cleanup()


def case_stale_states_surfaced():
    """陈旧状态必须如实上报，不能当成 fresh。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(),
                     janitor=janitor_status(j_stale=True, g_stale=True),
                     ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        j = json.loads(p.stdout)["components"]["janitor"]
        ok = j["janitor_stale"] is True and j["guard_stale"] is True
        record("janitor/guard 陈旧标记被如实投影", ok,
               f"janitor_stale={j['janitor_stale']} guard_stale={j['guard_stale']}")
    finally:
        sb.cleanup()


def case_missing_component_absent():
    """组件缺失 → partial + exit 4，且不假装它是 false/健康。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(),
                     ccp=ccp_status(), missing=("profiles",))
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        row = data["components"]["profiles"]
        ok = (p.returncode == EXIT_ABSENT and data["overall"] == "partial"
              and row["installed"] is False and row["probe_ok"] is False
              and row["healthy"] is None)
        record("组件缺失 → exit 4 partial，healthy 为 null 而非 false", ok,
               f"rc={p.returncode} overall={data['overall']} healthy={row['healthy']}")
    finally:
        sb.cleanup()


def case_all_missing_fail_closed():
    """三个都不可用 → unknown + exit 3，绝不报 ok。"""
    sb = Sandbox()
    try:
        env = sb.env(missing=("watcher", "janitor", "profiles"))
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        ok = (p.returncode == EXIT_FAIL_CLOSED and data["overall"] == "unknown"
              and data["probed_count"] == 0)
        record("全部不可探测 → exit 3 unknown（fail closed）", ok,
               f"rc={p.returncode} overall={data['overall']}")
    finally:
        sb.cleanup()


def case_bad_json_fail_closed():
    """上游吐非 JSON → 该组件 probe_ok False，理由具体，不崩。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher="this is not json at all",
                     janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        w = data["components"]["watcher"]
        ok = (w["probe_ok"] is False and "not JSON" in (w["reason"] or "")
              and "Traceback" not in p.stderr)
        record("上游非 JSON → 该组件未探测且无 traceback", ok,
               f"reason={w['reason']} rc={p.returncode}")
    finally:
        sb.cleanup()


def case_component_failure_isolation():
    """一个组件坏掉，另外两个仍如实上报（失败隔离）。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher="{{{ broken", janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        rows = json.loads(p.stdout)["components"]
        ok = (rows["watcher"]["probe_ok"] is False
              and rows["janitor"]["probe_ok"] is True and rows["janitor"]["healthy"] is True
              and rows["profiles"]["probe_ok"] is True and rows["profiles"]["healthy"] is True)
        record("单组件失败不污染其余组件（失败隔离）", ok,
               f"watcher={rows['watcher']['probe_ok']} "
               f"janitor={rows['janitor']['healthy']} profiles={rows['profiles']['healthy']}")
    finally:
        sb.cleanup()


def case_up_idempotent_healthy_watcher():
    """健康 watcher → up 计划为 none，且【绝不】调用 launchctl。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["up", "--json"], env)
        data = json.loads(p.stdout)
        plan = {i["component"]: i for i in data["plan"]}
        ok = (data["applied"] is False
              and plan["watcher"]["action"] == "none"
              and plan["janitor"]["action"] == "none"
              and plan["profiles"]["action"] == "none")
        record("up 对健康组件是 no-op（不重启在飞 watcher）", ok,
               " ".join(f"{k}={v['action']}" for k, v in plan.items()))

        writes = [c for c in sb.launchctl_calls() if "bootstrap" in c or "bootout" in c]
        record("up 干跑期间零 launchctl 写操作", not writes, f"写操作={writes}")

        ok3 = "interactive CLI" in plan["profiles"]["why"]
        record("profiles 明确无服务可启（设计要求）", ok3, plan["profiles"]["why"])
    finally:
        sb.cleanup()


def case_up_apply_only_bootstraps_sandbox_label():
    """--apply 真调 launchctl，但只允许经过 fake，且不得碰真实 label 的 bootout。"""
    sb = Sandbox()
    try:
        # watcher 死 + launchd 未加载 → 才会产生 bootstrap 动作
        env = sb.env(watcher=watcher_status(pid_alive=False),
                     janitor=janitor_status(), ccp=ccp_status(), launchctl_rc=1)
        # launchctl_rc=1 让 _launchd_loaded 返回 False（未加载）
        p = run_stack(sb, ["up", "--apply", "--json", "--component", "watcher"], env)
        data = json.loads(p.stdout)
        calls = sb.launchctl_calls()
        bootstraps = [c for c in calls if c.startswith("bootstrap")]
        bootouts = [c for c in calls if "bootout" in c]
        ok = (data["applied"] is True and bootstraps and not bootouts)
        record("up --apply 只 bootstrap 不 bootout", ok,
               f"bootstrap={len(bootstraps)} bootout={len(bootouts)}")

        # 关键反向断言：真实 label 从未被 bootout
        bad = [c for c in calls for lbl in REAL_LABELS if "bootout" in c and lbl in c]
        record("沙箱 up 从未 bootout 任何真实 label", not bad, f"越界={bad}")

        # 且所有调用都经过 fake（日志存在即证明），没有走 /bin/launchctl
        record("所有 launchctl 调用都经过 fake（未触真实二进制）",
               all(c.strip() for c in calls) and len(calls) > 0,
               f"共 {len(calls)} 次调用")
    finally:
        sb.cleanup()


def case_down_requires_explicit_component():
    """down 不提供全量停机；未指定组件 → exit 2。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["down"], env)
        ok = p.returncode == EXIT_USAGE and "显式指定" in p.stderr
        record("down 未指定组件 → exit 2（无全量停机）", ok, f"rc={p.returncode}")

        writes = [c for c in sb.launchctl_calls() if "bootout" in c]
        record("被拒的 down 未产生任何 bootout", not writes, f"写操作={writes}")

        p2 = run_stack(sb, ["down", "--component", "profiles"], env)
        ok2 = p2.returncode == EXIT_USAGE and "没有常驻服务" in p2.stderr
        record("down profiles → exit 2（ccp-new 无服务，设计要求）", ok2, f"rc={p2.returncode}")
    finally:
        sb.cleanup()


def case_down_dry_run_then_apply():
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["down", "--component", "watcher", "--json"], env)
        data = json.loads(p.stdout)
        ok = data["applied"] is False and not sb.launchctl_calls()
        record("down 干跑不执行且零 launchctl 调用", ok,
               f"applied={data['applied']} calls={len(sb.launchctl_calls())}")

        p2 = run_stack(sb, ["down", "--component", "watcher", "--apply", "--json"], env)
        data2 = json.loads(p2.stdout)
        calls = sb.launchctl_calls()
        # fake launchctl 收到的是沙箱 uid/label 字符串；断言它没有溢出到真实域之外
        ok2 = data2["applied"] is True and any("bootout" in c for c in calls)
        record("down --apply 执行 bootout（仅经 fake）", ok2, f"calls={calls[-1:]}")
    finally:
        sb.cleanup()


def case_update_plan_not_atomic():
    """update 必须明说非原子；不得伪装成跨信任域事务。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["update", "--plan", "--json"], env)
        data = json.loads(p.stdout)
        ok = (p.returncode == EXIT_OK and data["atomic"] is False
              and "no shared rollback" in data["atomicity_note"]
              and data["applied"] is False and len(data["steps"]) == 3
              and all(s["independent"] is True for s in data["steps"]))
        record("update --plan 明示非原子 + 三步独立可回滚", ok,
               f"atomic={data['atomic']} steps={len(data['steps'])}")

        p2 = run_stack(sb, ["update", "--component", "janitor", "--json"], env)
        d2 = json.loads(p2.stdout)
        ok2 = len(d2["steps"]) == 1 and d2["steps"][0]["component"] == "janitor"
        record("update --component 收窄到单组件", ok2, f"steps={len(d2['steps'])}")
    finally:
        sb.cleanup()


def case_usage_errors():
    """用法错误一律 exit 2，且不是 traceback、不是「全绿」。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, [], env)
        record("无参数 → exit 2 并打印帮助", p.returncode == EXIT_USAGE, f"rc={p.returncode}")

        p2 = run_stack(sb, ["status", "--component", "watchr", "--json"], env)
        ok2 = (p2.returncode == EXIT_USAGE and "未知组件" in p2.stderr
               and "Traceback" not in p2.stderr and not p2.stdout.strip())
        record("--component 拼错 → exit 2，不静默扩大为全量", ok2,
               f"rc={p2.returncode} stdout空={not p2.stdout.strip()}")

        p3 = run_stack(sb, ["update", "--component", "nope", "--json"], env)
        record("update 未知组件 → exit 2", p3.returncode == EXIT_USAGE, f"rc={p3.returncode}")
    finally:
        sb.cleanup()


def case_component_narrowing_works():
    """--component 收窄后只探一个，requested_count 随之变化。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["status", "--component", "janitor", "--json"], env)
        data = json.loads(p.stdout)
        ok = (data["requested_count"] == 1 and set(data["components"]) == {"janitor"}
              and p.returncode == EXIT_OK)
        record("--component 正确收窄探测集", ok,
               f"requested={data['requested_count']} keys={sorted(data['components'])}")
    finally:
        sb.cleanup()


def case_doctor_checks():
    """doctor 报安装级事实；ccp-new 有 LaunchAgent 时必须判失败。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["doctor", "--json"], env)
        data = json.loads(p.stdout)
        checks = {f["check"]: f["ok"] for f in data["findings"]}
        ok = (p.returncode == EXIT_OK
              and checks.get("ccp-new has no LaunchAgent (required)") is True)
        record("doctor：ccp-new 无 LaunchAgent 判为通过", ok, f"rc={p.returncode}")

        # 造一个 ccp LaunchAgent → 必须判失败（设计违规检测）
        plist = sb.home / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}.ccp-new.plist"
        plist.write_text("<plist/>", encoding="utf-8")
        p2 = run_stack(sb, ["doctor", "--json"], env)
        d2 = json.loads(p2.stdout)
        c2 = {f["check"]: f["ok"] for f in d2["findings"]}
        ok2 = (c2.get("ccp-new has no LaunchAgent (required)") is False
               and p2.returncode == EXIT_DEGRADED)
        record("doctor：给 ccp-new 加 LaunchAgent 被判违规", ok2, f"rc={p2.returncode}")
    finally:
        sb.cleanup()


def case_doctor_flags_source_drift():
    """运行源 != 磁盘源：surfaced 但不判 unhealthy（重启是人的决定）。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(source_matches_disk=False),
                     janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        w = json.loads(p.stdout)["components"]["watcher"]
        ok = w["source_matches_disk"] is False and w["healthy"] is True
        record("磁盘源漂移只上报不判故障（重启属人工决策）", ok,
               f"drift={not w['source_matches_disk']} healthy={w['healthy']}")

        p2 = run_stack(sb, ["doctor", "--json"], env)
        d2 = json.loads(p2.stdout)
        has = any(f["check"] == "watcher running source == disk source" and not f["ok"]
                  for f in d2["findings"])
        ok2 = has and p2.returncode == EXIT_DEGRADED
        record("doctor 把源漂移列为待办 finding", ok2, f"rc={p2.returncode} finding={has}")
    finally:
        sb.cleanup()


def case_profile_tty_passthrough():
    """profile 必须 exec 出去、保留真实 TTY，且退出码原样透传。"""
    sb = Sandbox()
    try:
        marker = sb.root / "tty-marker.json"
        probe = sb.root / "tty-probe.py"
        # 不要用 f-string 拼这段源码：花括号需要双写转义，早前那版把 json.dump 的
        # 括号配对写坏了，探针启动即 SyntaxError、marker 从不生成，报出来却像是
        # 「profile 没能 exec」。装置的语法错误必须与产品行为区分开。
        probe.write_text(
            "import json, os, sys\n"
            "payload = {'stdin_tty': os.isatty(0), 'stdout_tty': os.isatty(1),\n"
            "           'argv': sys.argv[1:], 'pid': os.getpid()}\n"
            "with open(" + json.dumps(str(marker)) + ", 'w', encoding='utf-8') as fh:\n"
            "    json.dump(payload, fh)\n"
            "sys.exit(47)\n", encoding="utf-8")

        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        env["CMUX_STACK_CCP"] = str(probe)

        # 真 pty：证明子进程看到的是终端，而不是管道
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, str(STACK), "profile", "--status"], env)
            os._exit(99)
        _, wait_status = os.waitpid(pid, 0)
        try:
            os.close(fd)
        except OSError:
            pass
        rc = os.waitstatus_to_exitcode(wait_status)

        data = json.loads(marker.read_text(encoding="utf-8"))
        ok = data["stdin_tty"] is True and data["stdout_tty"] is True
        record("profile 保留真实 TTY（stdin/stdout 都是终端）", ok,
               f"stdin_tty={data['stdin_tty']} stdout_tty={data['stdout_tty']}")

        record("profile 原样透传退出码（证明是 exec 而非包装）", rc == 47, f"rc={rc}")

        ok3 = data["argv"] == ["--status"]
        record("profile 参数按原样透传给 ccp-new", ok3, f"argv={data['argv']}")

        # `profile -- --status` 与 `profile --status` 必须等价：显式分隔符是给
        # 用户消歧用的，不能当成 ccp-new 的第一个参数递下去。早前版本把 `--`
        # 原样传给了 ccp-new，argv 变成 ['--','--status']，于是 ccp-new 走
        # 「参数个数不等于 3」分支报用法错误 —— 一个只在带分隔符时才出现的坑。
        marker.unlink()
        pid2, fd2 = pty.fork()
        if pid2 == 0:
            os.execve(sys.executable,
                      [sys.executable, str(STACK), "profile", "--", "--status"], env)
            os._exit(99)
        _, ws2 = os.waitpid(pid2, 0)
        try:
            os.close(fd2)
        except OSError:
            pass
        rc2 = os.waitstatus_to_exitcode(ws2)
        d2 = json.loads(marker.read_text(encoding="utf-8"))
        ok4 = d2["argv"] == ["--status"] and rc2 == 47
        record("profile -- <args> 等价于 profile <args>（分隔符被吃掉）", ok4,
               f"argv={d2['argv']} rc={rc2}")
    finally:
        sb.cleanup()


def case_profile_missing_absent():
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(),
                     ccp=ccp_status(), missing=("profiles",))
        p = run_stack(sb, ["profile"], env)
        ok = p.returncode == EXIT_ABSENT and "不存在" in p.stderr
        record("profile 目标缺失 → exit 4", ok, f"rc={p.returncode}")
    finally:
        sb.cleanup()


def case_ccp_unhealthy_degraded():
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(),
                     ccp=ccp_status(unhealthy=2), ccp_rc=1)
        p = run_stack(sb, ["status", "--json"], env)
        row = json.loads(p.stdout)["components"]["profiles"]
        ok = p.returncode == EXIT_DEGRADED and row["healthy"] is False and row["unhealthy_count"] == 2
        record("profile 不健康 → degraded 且只报计数", ok,
               f"rc={p.returncode} unhealthy={row['unhealthy_count']}")

        env2 = sb.env(watcher=watcher_status(), janitor=janitor_status(),
                      ccp=ccp_status(manifest=False), ccp_rc=1)
        p2 = run_stack(sb, ["status", "--json"], env2)
        r2 = json.loads(p2.stdout)["components"]["profiles"]
        ok2 = r2["manifest_healthy"] is False and r2["healthy"] is False
        record("manifest 坏 → profiles 判不健康", ok2, f"manifest={r2['manifest_healthy']}")
    finally:
        sb.cleanup()


def case_never_touches_real_cmuxterm():
    """控制器只读 janitorctl 的 JSON，绝不扫真实隔离区。"""
    sb = Sandbox()
    try:
        # Directory mtime cannot attribute an access to this process: the real
        # cmux logger and hooks legitimately update it while tests run. Guard
        # this subprocess's actual I/O, including stat (which has no audit event).
        guarded = sb.root / "guarded_stack.py"
        protected = [str(REAL_CMUXTERM), str(sb.home / ".cmuxterm")]
        guarded.write_text(
            "import os, runpy, sys\n"
            f"protected = {protected!r}\n"
            "def guard(value):\n"
            "    if not isinstance(value, (str, bytes, os.PathLike)):\n"
            "        return\n"
            "    path = os.path.abspath(os.fsdecode(value))\n"
            "    if any(path == root or path.startswith(root + os.sep) for root in protected):\n"
            "        raise RuntimeError('forbidden terminal storage access')\n"
            "def wrap(fn):\n"
            "    def checked(path, *args, **kwargs):\n"
            "        guard(path)\n"
            "        return fn(path, *args, **kwargs)\n"
            "    return checked\n"
            "os.stat, os.lstat = wrap(os.stat), wrap(os.lstat)\n"
            "def audit(event, args):\n"
            "    if event in {'open', 'os.listdir', 'os.scandir', 'os.mkdir', 'os.chmod',\n"
            "                 'os.remove', 'os.rmdir', 'os.rename', 'os.link', 'os.symlink'}:\n"
            "        for value in args:\n"
            "            guard(value)\n"
            "sys.addaudithook(audit)\n"
            "if sys.argv[1:] == ['--prove-guard']:\n"
            "    os.stat(protected[0])\n"
            f"sys.argv[0] = {str(STACK)!r}\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n",
            encoding="utf-8",
        )
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        def checked(args):
            return subprocess.run([sys.executable, "-B", str(guarded), *args], env=env,
                                  capture_output=True, text=True, timeout=30)
        control = checked(["--prove-guard"])
        record("隔离检测能在真实目录访问前阻断负向探针",
               control.returncode != 0 and "forbidden terminal storage access" in control.stderr)
        responses = []
        for args in (["status", "--json"], ["doctor", "--json"], ["up", "--json"],
                     ["update", "--plan", "--json"]):
            responses.append(checked(args))
        record("控制器未访问真实或隔离的 .cmuxterm 目录",
               all("forbidden terminal storage access" not in p.stderr
                   and isinstance(json.loads(p.stdout), dict) for p in responses))

        # 隔离区数据只可能来自 fake janitorctl
        data = json.loads(run_stack(sb, ["status", "--json"], env).stdout)
        ok = data["components"]["janitor"]["quarantine_batch_count"] == 7
        record("隔离区计数来自 janitorctl 投影（非自行扫盘）", ok,
               f"batch_count={data['components']['janitor']['quarantine_batch_count']}")
    finally:
        sb.cleanup()


def case_launchctl_unusable_is_null():
    """launchctl 不可用 → launchd_loaded 为 null，不伪装 false。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        env["CMUX_STACK_LAUNCHCTL"] = str(sb.root / "no-such-launchctl")
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        w = data["components"]["watcher"]
        ok = w["launchd_loaded"] is None
        record("launchctl 不可用 → launchd_loaded=null（不伪装 false）", ok,
               f"launchd_loaded={w['launchd_loaded']}")

        p2 = run_stack(sb, ["doctor", "--json"], env)
        d2 = json.loads(p2.stdout)
        c2 = {f["check"]: f["ok"] for f in d2["findings"]}
        ok2 = c2.get("launchctl usable") is False and p2.returncode == EXIT_DEGRADED
        record("doctor 把 launchctl 不可用列为失败项", ok2, f"rc={p2.returncode}")
    finally:
        sb.cleanup()


def case_human_output_renders():
    """非 JSON 渲染路径不能崩，且不得泄漏哨兵。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["status"], env)
        ok = (p.returncode == EXIT_OK and "cmux-stack" in p.stdout
              and "watcher" in p.stdout and "Traceback" not in p.stderr
              and SENTINEL_TOKEN not in p.stdout)
        record("人读渲染路径正常且无泄漏", ok, f"rc={p.returncode} 行数={len(p.stdout.splitlines())}")
    finally:
        sb.cleanup()


# ==================================================================== R1 审计新增
#
# 监督方 R1 独立审计提出两条契约缺口，本节是它们的承重测试。两条的共同根因都是
# 106.14.4 那次 28 分钟事故的同型问题：**除 launchd 加载状态之外，本页每一个字段
# 都是「某个被调度的进程写的文件」**。一旦没人被调度，那些文件就永久停在最后一次
# 健康读数上，于是「PID 还活着」「health=healthy」「stale=False」全都还在，
# 而实际上已经停摆。所以判据顺序必须是：**先问谁在调度，再问它说了什么。**

WATCHER_LABEL = f"{LABEL_PREFIX}.cmux-codex-continue"
JANITOR_LABEL = f"{LABEL_PREFIX}.cmux-janitor"
GUARD_LABEL = f"{LABEL_PREFIX}.cmux-janitor-guard"


def case_r1_unloaded_watcher_with_live_pid_is_not_healthy():
    """P1-1 核心：label 被踢掉但 PID 还活着 → 必须 degraded，且理由点名 launchd。

    修复前这里报 healthy=True：`pid_alive` 来自 daemon-runtime.json，那是**上一次**
    启动时写下的，进程被踢出 launchd 后照样活着直到它自己死掉，然后再没人拉起它。
    """
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(pid_alive=True), janitor=janitor_status(),
                     ccp=ccp_status(), unloaded=(WATCHER_LABEL,))
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        w = data["components"]["watcher"]
        ok = (w["launchd_loaded"] is False and w["pid_alive"] is True
              and w["healthy"] is False and p.returncode == EXIT_DEGRADED
              and "watcher" in data["unhealthy"]
              and "launchd" in (w["reason"] or ""))
        record("P1-1 watcher label 未加载但 PID 存活 → degraded 而非 healthy", ok,
               f"loaded={w['launchd_loaded']} pid_alive={w['pid_alive']} "
               f"healthy={w['healthy']} rc={p.returncode}")
    finally:
        sb.cleanup()


def case_r1_up_bootstraps_unloaded_service_with_live_pid():
    """P1-1 的实际危害：修复前 up 会因「已健康」跳过唯一需要恢复的组件。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(pid_alive=True), janitor=janitor_status(),
                     ccp=ccp_status(), unloaded=(WATCHER_LABEL,))
        p = run_stack(sb, ["up", "--json"], env)
        data = json.loads(p.stdout)
        w = next(x for x in data["plan"] if x["component"] == "watcher")
        ok = w["action"] == "bootstrap" and w.get("label") == WATCHER_LABEL
        record("P1-1 up 对「未加载+PID 存活」必须计划 bootstrap（服务恢复不被跳过）", ok,
               f"action={w['action']} why={w.get('why')}")
        # 干跑绝不写
        ok2 = data["applied"] is False and not [c for c in sb.launchctl_calls()
                                                if "bootstrap" in c]
        record("P1-1 该计划在干跑下零 launchctl 写操作", ok2,
               f"applied={data['applied']} 写操作={[c for c in sb.launchctl_calls() if 'bootstrap' in c]}")
    finally:
        sb.cleanup()


def case_r1_unknown_load_state_is_fail_closed():
    """P1-1 三态：launchctl 答不出 → healthy=None，且 up 拒绝在未知上写。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(pid_alive=True), janitor=janitor_status(),
                     ccp=ccp_status())
        # 「答不出」= launchctl 根本没跑起来（OSError），不是「跑了并返回非 0」。
        # 非 0 退出是一个明确的答复：label 未加载。我最初用 launchctl_rc=2 来模拟
        # 未知，测出的却是 loaded=False —— 装置与被测语义不匹配，测的是我自己的
        # 误解。指向不存在的二进制才是真正的 None 路径（同既有 null 用例）。
        env["CMUX_STACK_LAUNCHCTL"] = str(sb.root / "no-such-launchctl")
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        w = data["components"]["watcher"]
        # None 既不是 False（会凭空造出停机）也不是 True（会隐瞒停机）
        ok = (w["launchd_loaded"] is None and w["healthy"] is None
              and "unknown" in (w["reason"] or "").lower())
        record("P1-1 launchctl 答不出 → healthy=null（不伪装 false 也不伪装健康）", ok,
               f"loaded={w['launchd_loaded']} healthy={w['healthy']} reason={w['reason']}")

        # overall 不得是 ok：未知不能渲染成绿
        ok2 = data["overall"] != "ok" and p.returncode != EXIT_OK
        record("P1-1 存在未知组件时 overall 不得为 ok", ok2,
               f"overall={data['overall']} rc={p.returncode}")

        p2 = run_stack(sb, ["up", "--json"], env)
        d2 = json.loads(p2.stdout)
        w2 = next(x for x in d2["plan"] if x["component"] == "watcher")
        ok3 = w2["action"] == "blocked" and "unknown" in (w2.get("why") or "").lower()
        record("P1-1 up 在未知加载态上 fail closed（不盲目 bootstrap）", ok3,
               f"action={w2['action']} why={w2.get('why')}")
    finally:
        sb.cleanup()


def case_r1_healthy_loaded_component_stays_noop():
    """P1-1 回归：修复不得把「健康且已加载」变成会被重启的对象。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
        p = run_stack(sb, ["up", "--json"], env)
        data = json.loads(p.stdout)
        acts = {x["component"]: x["action"] for x in data["plan"]}
        ok = all(a == "none" for a in acts.values()) and not sb.launchctl_calls_write()
        record("P1-1 健康且已加载 → 仍是 no-op（不重启在飞 watcher）", ok,
               f"actions={acts}")
    finally:
        sb.cleanup()


def case_r1_loaded_but_unhealthy_is_not_bootstrapped():
    """P1-1 边界：已加载但组件自报不健康 → bootstrap 不是解药，必须 none 并点名。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(pid_alive=False), janitor=janitor_status(),
                     ccp=ccp_status())
        p = run_stack(sb, ["up", "--json"], env)
        data = json.loads(p.stdout)
        w = next(x for x in data["plan"] if x["component"] == "watcher")
        ok = w["action"] == "none" and "unhealthy" in (w.get("why") or "")
        record("P1-1 已加载但不健康 → 不 bootstrap，如实说明原因", ok,
               f"action={w['action']} why={w.get('why')}")
    finally:
        sb.cleanup()


def case_r1_unloaded_janitor_is_degraded_and_bootstrappable():
    """P1-1 同样适用于清扫器：label 没了就没人清扫，其余字段都是历史读数。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(),
                     ccp=ccp_status(), unloaded=(JANITOR_LABEL,))
        p = run_stack(sb, ["status", "--json"], env)
        j = json.loads(p.stdout)["components"]["janitor"]
        ok = j["healthy"] is False and "launchd" in (j["reason"] or "")
        record("P1-1 janitor label 未加载 → degraded（health=healthy 只是冻结读数）", ok,
               f"healthy={j['healthy']} reason={j['reason']}")

        p2 = run_stack(sb, ["up", "--json"], env)
        plan = next(x for x in json.loads(p2.stdout)["plan"] if x["component"] == "janitor")
        ok2 = plan["action"] == "bootstrap" and plan.get("label") == JANITOR_LABEL
        record("P1-1 up 对未加载的 janitor 计划 bootstrap", ok2, f"action={plan['action']}")
    finally:
        sb.cleanup()


def case_r1_unloaded_guard_is_degraded():
    """P1-1 单边失效：只有守卫没被调度。

    指南把这一态称作本页最危险的东西——**没被调度的 guard 永远不会跳闸**，
    于是它停在 healthy 的读数反而成了「一切正常」的伪证。
    """
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(health="healthy"),
                     ccp=ccp_status(), unloaded=(GUARD_LABEL,))
        p = run_stack(sb, ["status", "--json"], env)
        j = json.loads(p.stdout)["components"]["janitor"]
        ok = (j["launchd_loaded"] is True and j["guard_launchd_loaded"] is False
              and j["healthy"] is False and "guard" in (j["reason"] or "").lower())
        record("P1-1 仅守卫未加载 → degraded（不跳闸的守卫证明不了任何事）", ok,
               f"janitor_loaded={j['launchd_loaded']} guard_loaded={j['guard_launchd_loaded']} "
               f"healthy={j['healthy']}")
    finally:
        sb.cleanup()


def case_s1_up_recovers_booted_out_guard():
    """S1（2026-09-01）：janitor 组件 = 清扫器 + 守卫两个 LaunchAgent。

    修复前 up 只看清扫器的 label：守卫被 bootout 而清扫器仍加载时，走到
    「已加载但不健康 → 不重启」分支，被踢掉的 guard 永远没人恢复——而
    无守卫的清扫器正是 guard 存在要防止的形态。
    """
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(), janitor=janitor_status(health="healthy"),
                     ccp=ccp_status(), unloaded=(GUARD_LABEL,))
        p = run_stack(sb, ["up", "--json"], env)
        data = json.loads(p.stdout)
        boots = [x for x in data["plan"]
                 if x["component"] == "janitor" and x["action"] == "bootstrap"]
        ok = (len(boots) == 1 and boots[0].get("label") == GUARD_LABEL
              and data["applied"] is False and not sb.launchctl_calls_write())
        record("S1 仅守卫被 bootout → up 计划 bootstrap 守卫 label（干跑零写）", ok,
               f"boots={[(x.get('label'), x['action']) for x in boots]}")

        env2 = sb.env(watcher=watcher_status(), janitor=janitor_status(health="healthy"),
                      ccp=ccp_status(), unloaded=(JANITOR_LABEL, GUARD_LABEL))
        p2 = run_stack(sb, ["up", "--json"], env2)
        boots2 = [x for x in json.loads(p2.stdout)["plan"]
                  if x["component"] == "janitor" and x["action"] == "bootstrap"]
        labels = sorted(x.get("label") for x in boots2)
        ok2 = labels == sorted([JANITOR_LABEL, GUARD_LABEL])
        record("S1 清扫器+守卫都被 bootout → 两个 label 各自计划 bootstrap", ok2,
               f"labels={labels}")
    finally:
        sb.cleanup()


def case_s3_unhealthy_count_strong_typing():
    """S3（2026-09-01）：unhealthy_count 是健康门禁的输入，必须强类型。

    旧真值判断 `not (x or 0)` 把 None 和 "" 都读成「0 个不健康」——上游
    status 坏掉时反而喂出 healthy=True（fail open）。缺失/字符串/负数/布尔
    一律投影为 None（未知），未知不是健康。
    """
    sb = Sandbox()
    try:
        breakers = (
            ("缺失", lambda d: d.pop("unhealthy_count")),
            ("字符串0", lambda d: d.__setitem__("unhealthy_count", "0")),
            ("空串", lambda d: d.__setitem__("unhealthy_count", "")),
            ("负数", lambda d: d.__setitem__("unhealthy_count", -1)),
            ("布尔False", lambda d: d.__setitem__("unhealthy_count", False)),
        )
        for label, mutate in breakers:
            payload = ccp_status()
            mutate(payload)
            env = sb.env(watcher=watcher_status(), janitor=janitor_status(),
                         ccp=payload)
            p = run_stack(sb, ["status", "--json"], env)
            row = json.loads(p.stdout)["components"]["profiles"]
            ok = (row["unhealthy_count"] is None and row["healthy"] is False
                  and "unreadable" in (row["reason"] or "")
                  and p.returncode == EXIT_DEGRADED)
            record(f"S3 unhealthy_count {label} → None + 不健康（未知不伪装成 0）", ok,
                   f"投影={row['unhealthy_count']!r} healthy={row['healthy']} "
                   f"reason={row['reason']!r} rc={p.returncode}")
    finally:
        sb.cleanup()


def case_r1_active_stale_janitor_is_degraded():
    """P1-2：未暂停却陈旧 → degraded 且理由点名。指南口径：陈旧不是健康。"""
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(),
                     janitor=janitor_status(paused=False, j_stale=True),
                     ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        j = data["components"]["janitor"]
        ok = (j["janitor_stale"] is True and j["paused"] is False
              and j["healthy"] is False and p.returncode == EXIT_DEGRADED
              and "stale" in (j["reason"] or "").lower())
        record("P1-2 未暂停 + janitor 陈旧 → degraded 且点名", ok,
               f"stale={j['janitor_stale']} paused={j['paused']} "
               f"healthy={j['healthy']} reason={j['reason']}")
    finally:
        sb.cleanup()


def case_r1_active_stale_guard_is_degraded():
    """P1-2：守卫陈旧【永远】是故障，与暂停无关。

    依据是实测的生产事实：guard.sh 的 StartInterval 是 60 秒，且无条件写
    guard-state.json；janitor 的 DISABLED 哨兵**不会**让守卫停写。所以守卫陈旧
    只有一个解释——守卫本身停了。
    """
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(),
                     janitor=janitor_status(paused=False, g_stale=True),
                     ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        j = json.loads(p.stdout)["components"]["janitor"]
        ok = j["guard_stale"] is True and j["healthy"] is False and "guard" in (j["reason"] or "").lower()
        record("P1-2 守卫陈旧（未暂停）→ degraded 且点名守卫", ok,
               f"g_stale={j['guard_stale']} healthy={j['healthy']} reason={j['reason']}")
    finally:
        sb.cleanup()


def case_r1_paused_stale_guard_still_degraded():
    """P1-2 关键区分：暂停【不能】为守卫陈旧开脱。

    这是本轮唯一需要实测生产源码才能定的判据。若把「暂停」当成两种陈旧的统一豁免，
    就会在守卫真的死掉时报绿。
    """
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(),
                     janitor=janitor_status(paused=True, g_stale=True),
                     ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        j = json.loads(p.stdout)["components"]["janitor"]
        ok = j["paused"] is True and j["healthy"] is False and "guard" in (j["reason"] or "").lower()
        record("P1-2 暂停 + 守卫陈旧 → 仍 degraded（暂停不豁免守卫）", ok,
               f"paused={j['paused']} g_stale={j['guard_stale']} healthy={j['healthy']}")
    finally:
        sb.cleanup()


def case_r1_paused_stale_janitor_stays_healthy():
    """P1-2 保留项：暂停 + janitor 陈旧 = 设计态，必须仍判健康并说明理由。

    janitorctl 自己的注释写明：暂停时 GATE 0 提前退出，janitor 只在真实 run 内
    发布，所以「暂停、上次测量于 N 分钟前」是**诚实**的设计态。把它判成故障会把
    操作者推向「取消暂停」——正是必须避免的动作。
    """
    sb = Sandbox()
    try:
        env = sb.env(watcher=watcher_status(),
                     janitor=janitor_status(paused=True, j_stale=True),
                     ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        data = json.loads(p.stdout)
        j = data["components"]["janitor"]
        ok = (j["paused"] is True and j["janitor_stale"] is True
              and j["healthy"] is True and p.returncode == EXIT_OK
              and "paused" in (j["reason"] or "").lower())
        record("P1-2 暂停 + janitor 陈旧 → 仍健康（设计态，不诱导 rearm）", ok,
               f"paused={j['paused']} stale={j['janitor_stale']} "
               f"healthy={j['healthy']} reason={j['reason']}")
    finally:
        sb.cleanup()


def case_r1_invalid_state_files_are_not_green():
    """P1-2 附带：不可解析的状态文件不得读成健康（present/invalid 必须被消费）。"""
    sb = Sandbox()
    try:
        for label, kwargs, needle in (
            ("guard invalid", {"g_invalid": True}, "guard"),
            ("janitor invalid", {"j_invalid": True}, "janitor"),
        ):
            env = sb.env(watcher=watcher_status(),
                         janitor=janitor_status(**kwargs), ccp=ccp_status())
            p = run_stack(sb, ["status", "--json"], env)
            j = json.loads(p.stdout)["components"]["janitor"]
            ok = j["healthy"] is False and needle in (j["reason"] or "").lower()
            record(f"P1-2 {label} → degraded（不可解析不是健康）", ok,
                   f"healthy={j['healthy']} reason={j['reason']}")
    finally:
        sb.cleanup()


def case_r1_missing_scalars_are_not_green():
    """P1-2 附带：公开标量整块缺失时不得变绿。

    真实 janitorctl 在 fail-closed 路径上可能只吐半张表。缺失必须落到
    「未测量」而不是「0/健康」——同 106.4.3「未测量必须是 null，不能伪装成 0」。
    """
    sb = Sandbox()
    try:
        # control/guard/janitor 全缺：只有 schema_version
        env = sb.env(watcher=watcher_status(), janitor={"schema_version": 1},
                     ccp=ccp_status())
        p = run_stack(sb, ["status", "--json"], env)
        j = json.loads(p.stdout)["components"]["janitor"]
        # present=False 意味着从未发布过状态，绝不能读成健康
        ok = j["healthy"] is not True
        record("P1-2 公开标量整块缺失 → 不得判健康", ok,
               f"healthy={j['healthy']} reason={j['reason']} present={j['guard_present']}")

        # 数值字段缺失必须是 null，不是 0
        ok2 = j["quarantine_batch_count"] is None
        record("P1-2 缺失的数值字段是 null 而非 0（未测量≠零）", ok2,
               f"batch_count={j['quarantine_batch_count']}")
    finally:
        sb.cleanup()


def case_r1_verdict_precedence_is_scheduling_first():
    """结构断言：两个 verdict 函数里，加载态判据必须排在任何文件派生字段之前。

    这条是防回归的结构闸门。106.14.4 的教训是「顺序」本身就是契约：只要有人把
    pid_alive / health / stale 挪到加载态之前，事故就会原样复现，而**功能测试可能
    仍然全绿**（单场景下两种顺序常常给出同一答案）。

    用 AST 而不是字符串偏移，理由是前两版探针都因为「测源文本」而失败：

      1. 第一版探针写 `'launchd_loaded") is False'`，源码真实写法中间有
         `row.get(`，0 命中直接 ValueError 崩溃 —— 凭记忆敲探针
         （[[bundle-grep-probes-must-come-from-source]]）。
      2. 第二版改成子串偏移后，`paused` 在 **docstring 散文**里就出现了，偏移算成
         负数而判失败；同时另外三个字段的「通过」也可能只是命中了散文而非代码，
         即**绿得没有理由**。字符串偏移无法区分注释与代码。

    还有一个用 AST 才能表达的点：`_watcher_verdict` 先 `loaded = row.get(...)` 再
    `if loaded is False`，`_janitor_verdict` 则直接内联 `row.get(...)`。同一个契约
    在两种写法下的源文本完全不同，只有解析到语义层才能统一断言。
    """
    import ast

    tree = ast.parse(STACK.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def row_fields(node: ast.AST, aliases: dict) -> set:
        """这个节点读了 row 的哪些键（含经局部变量别名的间接读取）。"""
        found = set()
        for sub in ast.walk(node):
            # row.get("x")
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "get"
                    and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "row"
                    and sub.args and isinstance(sub.args[0], ast.Constant)):
                found.add(sub.args[0].value)
            # 局部别名，例如 loaded = row.get("launchd_loaded") 之后的 `loaded`
            elif isinstance(sub, ast.Name) and sub.id in aliases:
                found.add(aliases[sub.id])
        return found

    for fn, later_fields in (("_watcher_verdict", {"pid_alive", "global_paused"}),
                             ("_janitor_verdict", {"guard_tripped", "janitor_stale",
                                                   "guard_stale", "paused"})):
        node = funcs[fn]
        aliases: dict[str, str] = {}
        load_rank = None
        later_ranks: dict[str, int] = {}

        for rank, stmt in enumerate(node.body):
            # 先登记别名赋值：loaded = row.get("launchd_loaded")
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 \
                    and isinstance(stmt.targets[0], ast.Name):
                got = row_fields(stmt.value, {})
                if len(got) == 1:
                    aliases[stmt.targets[0].id] = next(iter(got))
                continue
            if not isinstance(stmt, ast.If):
                continue
            # 只看条件表达式，不看分支体：契约是「按什么顺序判定」
            fields = row_fields(stmt.test, aliases)
            loads = {f for f in fields if "launchd_loaded" in f}
            if loads and load_rank is None:
                load_rank = rank
            for f in fields & later_fields:
                later_ranks.setdefault(f, rank)

        # 加载态必须存在，且严格早于每一个文件派生字段
        ok = load_rank is not None and all(r > load_rank for r in later_ranks.values())
        record(f"P1-1 结构：{fn} 先判加载态再判文件派生字段", ok,
               f"加载态在第 {load_rank} 个判据；其余={later_ranks}")

        # 附加断言：确实解析到了全部关心的字段，避免「一个都没匹配上所以真空通过」
        record(f"P1-1 结构：{fn} 的文件派生字段全部被解析到", later_ranks.keys() == later_fields,
               f"解析到={sorted(later_ranks)} 期望={sorted(later_fields)}")


# ---------------------------------------- R3 实施：install / uninstall（PATH 安装）
#
# 每一条都跑在 CMUX_STACK_BINDIR 指向的临时目录上。这不是为了方便：安装用例的
# 全部风险就在于写 PATH，若测试对着真实 /opt/homebrew/bin 跑，一次断言写错就会
# 动到 ccc 所在的目录。临时目录把爆炸半径钉死在沙箱里，最后再反向断言真实路径
# 始终 ABSENT。

REAL_BINDIR = pathlib.Path("/opt/homebrew/bin")
REAL_LINK = REAL_BINDIR / "cmux-stack"


def _install_env(sb: "Sandbox", bindir: pathlib.Path) -> dict:
    """沙箱 env + 把安装目标指向临时 bindir。"""
    env = sb.env(watcher=watcher_status(), janitor=janitor_status(), ccp=ccp_status())
    env["CMUX_STACK_BINDIR"] = str(bindir)
    return env


def case_install_dry_run_writes_nothing():
    """默认干跑：只说要做什么，不建链接。"""
    sb = Sandbox()
    try:
        bindir = sb.root / "bin-dry"
        bindir.mkdir()
        p = run_stack(sb, ["install", "--json"], _install_env(sb, bindir))
        data = json.loads(p.stdout)
        link = bindir / "cmux-stack"
        ok = (p.returncode == EXIT_OK and data["applied"] is False
              and not link.exists() and not link.is_symlink())
        record("install 默认干跑：不创建链接", ok,
               f"rc={p.returncode} applied={data['applied']} 链接存在={link.exists()}")

        # 干跑必须仍然把目标与将执行的命令说清楚，否则用户无法判断要不要 --apply
        plan = data.get("plan") or [{}]
        ok2 = bool(plan[0].get("target")) and bool(plan[0].get("link"))
        record("install 干跑公布 target 与 link", ok2, f"plan={plan[0]}")
    finally:
        sb.cleanup()


def case_install_apply_creates_symlink():
    """--apply 才落地，且指向仓库里的 bin/cmux-stack。"""
    sb = Sandbox()
    try:
        bindir = sb.root / "bin-apply"
        bindir.mkdir()
        p = run_stack(sb, ["install", "--apply", "--json"], _install_env(sb, bindir))
        data = json.loads(p.stdout)
        link = bindir / "cmux-stack"
        ok = (p.returncode == EXIT_OK and data["applied"] is True
              and link.is_symlink() and link.resolve() == STACK.resolve())
        record("install --apply 创建符号链接且指向本仓库", ok,
               f"rc={p.returncode} is_symlink={link.is_symlink()} "
               f"target={link.resolve() if link.is_symlink() else None}")
    finally:
        sb.cleanup()


def case_install_is_idempotent():
    """重复安装不报错（幂等），且不重建链接。"""
    sb = Sandbox()
    try:
        bindir = sb.root / "bin-idem"
        bindir.mkdir()
        env = _install_env(sb, bindir)
        run_stack(sb, ["install", "--apply", "--json"], env)
        link = bindir / "cmux-stack"
        first_ino = link.lstat().st_ino

        p2 = run_stack(sb, ["install", "--apply", "--json"], env)
        d2 = json.loads(p2.stdout)
        ok = (p2.returncode == EXIT_OK and link.is_symlink()
              and link.lstat().st_ino == first_ino)
        record("install 幂等：重复 --apply 不报错、不重建", ok,
               f"rc={p2.returncode} inode 不变={link.lstat().st_ino == first_ino} "
               f"note={(d2.get('note') or '')[:40]}")
    finally:
        sb.cleanup()


def case_install_refuses_to_replace_someone_else():
    """路径已被别人占用 → 拒绝覆盖，且那个链接必须完好无损。

    这是本组最重要的一条。/opt/homebrew/bin 里住着 ccc 与 cmux-codex-continue，
    一个会覆盖的安装器等于有能力踢掉 watcher 的入口。
    """
    sb = Sandbox()
    try:
        bindir = sb.root / "bin-refuse"
        bindir.mkdir()
        other = sb.root / "someone-else.txt"
        other.write_text("not ours", encoding="utf-8")
        link = bindir / "cmux-stack"
        link.symlink_to(other)

        p = run_stack(sb, ["install", "--apply", "--json"], _install_env(sb, bindir))
        still_theirs = link.is_symlink() and link.resolve() == other.resolve()
        ok = p.returncode != EXIT_OK and still_theirs
        record("install 拒覆盖他人链接，且原链接完好", ok,
               f"rc={p.returncode} 仍指向他人={still_theirs}")

        # 拒绝时必须说清它实际指向哪里，否则用户无法判断该不该手工处理
        blob = p.stdout + p.stderr
        record("install 拒覆盖时公布实际指向", "someone-else" in blob,
               f"提到实际目标={'someone-else' in blob}")
    finally:
        sb.cleanup()


def case_uninstall_only_removes_our_own_link():
    """卸载只删指向自己的链接，绝不删同名他物。"""
    sb = Sandbox()
    try:
        bindir = sb.root / "bin-uninst"
        bindir.mkdir()
        other = sb.root / "someone-else-2.txt"
        other.write_text("not ours", encoding="utf-8")
        link = bindir / "cmux-stack"
        link.symlink_to(other)

        p = run_stack(sb, ["uninstall", "--apply", "--json"], _install_env(sb, bindir))
        survived = link.is_symlink() and link.resolve() == other.resolve()
        record("uninstall 不删他人的同名链接", survived,
               f"rc={p.returncode} 他人链接仍在={survived}")

        # 换成我们自己的链接，这次必须删掉
        link.unlink()
        env = _install_env(sb, bindir)
        run_stack(sb, ["install", "--apply", "--json"], env)
        p2 = run_stack(sb, ["uninstall", "--apply", "--json"], env)
        gone = not link.exists() and not link.is_symlink()
        record("uninstall 删除自己的链接", p2.returncode == EXIT_OK and gone,
               f"rc={p2.returncode} 已删除={gone}")
    finally:
        sb.cleanup()


def case_uninstall_dry_run_writes_nothing():
    sb = Sandbox()
    try:
        bindir = sb.root / "bin-uninst-dry"
        bindir.mkdir()
        env = _install_env(sb, bindir)
        run_stack(sb, ["install", "--apply", "--json"], env)
        link = bindir / "cmux-stack"

        p = run_stack(sb, ["uninstall", "--json"], env)
        data = json.loads(p.stdout)
        ok = (p.returncode == EXIT_OK and data["applied"] is False
              and link.is_symlink())
        record("uninstall 默认干跑：链接仍在", ok,
               f"rc={p.returncode} applied={data['applied']} 链接仍在={link.is_symlink()}")
    finally:
        sb.cleanup()


def case_install_never_touches_the_real_bindir():
    """反向断言：安装用例跑完后，真实 PATH 上的三个链接都与跑之前一致。

    这条原来断言的是「真实 cmux-stack 始终 ABSENT」。那在 G1 授权安装之前是对的，
    之后就成了一条过期断言——用户已明确授权把它装进 PATH，链接现在合法存在，
    断言却还在要求它不存在。

    被守护的性质从来不是「那个链接不存在」，而是**「测试不碰真实 bindir」**。
    所以改成前后快照对比：无论真实链接当前是有还是没有，跑完必须与跑之前逐字符
    相同。这样 G1 装了它以后断言仍然承重，而不是靠「恰好没装」才通过。
    watcher 的两个链接一并纳入同一次快照——它们与我们的安装目标同在一个目录，
    是「安装器越界」最先被破坏的东西。
    """

    def snapshot():
        state = {}
        for name in ("cmux-stack", "ccc", "cmux-codex-continue"):
            path = REAL_BINDIR / name
            if path.is_symlink():
                state[name] = f"symlink->{path.resolve()}"
            elif path.exists():
                state[name] = "regular-file"
            else:
                state[name] = "absent"
        return state

    before = snapshot()

    sb = Sandbox()
    try:
        bindir = sb.root / "bin-scope"
        bindir.mkdir()
        env = _install_env(sb, bindir)
        run_stack(sb, ["install", "--apply", "--json"], env)
        run_stack(sb, ["uninstall", "--apply", "--json"], env)
    finally:
        sb.cleanup()

    after = snapshot()
    record("真实 bindir 三个链接前后逐字符一致（测试从不碰真实 PATH）",
           before == after,
           f"前后一致={before == after} before={before} after={after}")

    # 沙箱确实被用过：若 CMUX_STACK_BINDIR 没生效，上面那条会因为「什么都没发生」
    # 而真空通过。这条钉住管辖非空。
    record("安装用例确实跑在沙箱 bindir 上（断言非空转）",
           before["cmux-stack"] == after["cmux-stack"],
           f"真实 cmux-stack 状态={after['cmux-stack']}")


def case_install_does_not_reuse_watcher_link_helper():
    """静态断言：安装代码不得引用 watcher 那个会连带操作 ccc 的函数。

    R2 查明 install_cli_link() 把 APP_NAME 与 ccc 硬编码在循环里，复用它会把
    cmux-stack 链到 watcher 入口并连带增删 ccc。这条闸门守的是「以后有人figure
    省事去 import 它」。
    """
    import ast

    src = STACK.read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {"install_cli_link", "uninstall_cli_link", "DEFAULT_SHORT_CLI_LINK"}

    # 判据必须是 AST，不能是子串。第一版用 `name in src`，于是命中了解释「为什么
    # 不能复用」的注释散文本身——一条永远无法通过的断言，且它报告的是不存在的缺陷。
    # 同类错误本轮出现多次：探针匹配散文而非代码。只看真实的调用与导入。
    called: set[str] = set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden:
                called.add(func.id)
            elif isinstance(func, ast.Attribute) and func.attr in forbidden:
                called.add(func.attr)
        elif isinstance(node, ast.Name) and node.id in forbidden:
            # 常量引用（例如把 DEFAULT_SHORT_CLI_LINK 读进来）同样算复用
            called.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            if "cmux_codex_watch" in module:
                imported.add(module)
            for alias in node.names:
                if "cmux_codex_watch" in alias.name or alias.name in forbidden:
                    imported.add(alias.name)

    record("安装代码未复用 watcher 的链接函数（AST 判据，不匹配注释散文）",
           not called and not imported,
           f"调用={sorted(called) or '无'} 导入={sorted(imported) or '无'}")

    # 单链接：不得出现短名别名
    # 判据字符串先绑到变量：Python 3.9 的 f-string 表达式部分不允许出现反斜杠，
    # 而这个判据本身带引号转义。把它挪出 f-string 是唯一的兼容写法。
    single_link_marker = 'INSTALL_NAME = "cmux-stack"'
    has_single = single_link_marker in src
    record("不建短名别名（只有一个链接名）", has_single,
           f"单链接常量存在={has_single}")


def case_terminal_and_hook_coverage_are_required_for_health():
    sb = Sandbox()
    try:
        for field in ("observation_coverage", "claude_hook_coverage", "continuation_health"):
            for verdict, expected in (("degraded", "degraded"), ("unknown", "unknown"), (None, "unknown")):
                payload = watcher_status()
                if verdict is None:
                    payload.pop(field)
                else:
                    payload[field]["status"] = verdict
                    payload[field]["private_payload"] = SENTINEL_TOKEN
                response = run_stack(sb, ["status", "--json", "--component", "watcher"],
                                     sb.env(watcher=payload))
                data = json.loads(response.stdout)
                expected_rc = EXIT_DEGRADED if expected == "degraded" else EXIT_FAIL_CLOSED
                record(f"{field}={verdict} keeps overall {expected}",
                       data["overall"] == expected and response.returncode == expected_rc)
                record(f"{field}={verdict} projects only public coverage fields",
                       SENTINEL_TOKEN not in response.stdout and not sb.launchctl_calls_write())
        payload = watcher_status()
        payload["observation_coverage"]["status"] = "unknown"
        payload["claude_hook_coverage"]["status"] = "degraded"
        payload["continuation_health"]["status"] = "unknown"
        response = run_stack(sb, ["status", "--json", "--component", "watcher"], sb.env(watcher=payload))
        record("definite Hook gap outranks unknown observation",
               json.loads(response.stdout)["overall"] == "degraded" and response.returncode == EXIT_DEGRADED)
    finally:
        sb.cleanup()


def main() -> int:
    if not STACK.is_file():
        print(f"控制器不存在: {STACK}", file=sys.stderr)
        return 1
    print(f"\ncmux-stack 测试 —— 全 fake，不碰真实服务\n控制器: {STACK}\n")

    case_all_healthy_schema()
    case_freshness_is_separate_from_health()
    case_terminal_and_hook_coverage_are_required_for_health()
    case_janitor_source_comparison_is_read_only_and_bounded()
    case_no_credentials_leak()
    case_degraded_watcher_dead()
    case_paused_janitor_is_healthy()
    case_guard_tripped_degraded()
    case_guard_violations_degraded()
    case_stale_states_surfaced()
    case_missing_component_absent()
    case_all_missing_fail_closed()
    case_bad_json_fail_closed()
    case_component_failure_isolation()
    case_up_idempotent_healthy_watcher()
    case_up_apply_only_bootstraps_sandbox_label()
    case_down_requires_explicit_component()
    case_down_dry_run_then_apply()
    case_update_plan_not_atomic()
    case_usage_errors()
    case_component_narrowing_works()
    case_doctor_checks()
    case_doctor_flags_source_drift()
    case_profile_tty_passthrough()
    case_profile_missing_absent()
    case_ccp_unhealthy_degraded()
    case_never_touches_real_cmuxterm()
    case_launchctl_unusable_is_null()
    case_human_output_renders()

    # ---- R1 审计（P1-1 launchd 三态 / P1-2 陈旧不是健康）----
    case_r1_unloaded_watcher_with_live_pid_is_not_healthy()
    case_r1_up_bootstraps_unloaded_service_with_live_pid()
    case_r1_unknown_load_state_is_fail_closed()
    case_r1_healthy_loaded_component_stays_noop()
    case_r1_loaded_but_unhealthy_is_not_bootstrapped()
    case_r1_unloaded_janitor_is_degraded_and_bootstrappable()
    case_r1_unloaded_guard_is_degraded()
    case_s1_up_recovers_booted_out_guard()
    case_s3_unhealthy_count_strong_typing()
    case_r1_active_stale_janitor_is_degraded()
    case_r1_active_stale_guard_is_degraded()
    case_r1_paused_stale_guard_still_degraded()
    case_r1_paused_stale_janitor_stays_healthy()
    case_r1_invalid_state_files_are_not_green()
    case_r1_missing_scalars_are_not_green()
    case_r1_verdict_precedence_is_scheduling_first()

    # R3 实施：PATH 安装
    case_install_dry_run_writes_nothing()
    case_install_apply_creates_symlink()
    case_install_is_idempotent()
    case_install_refuses_to_replace_someone_else()
    case_uninstall_only_removes_our_own_link()
    case_uninstall_dry_run_writes_nothing()
    case_install_never_touches_the_real_bindir()
    case_install_does_not_reuse_watcher_link_helper()

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("=" * 70)
    print(f"结果: {passed}/{total} 通过")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name} — {detail}")
    print("=" * 70)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())

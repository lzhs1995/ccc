#!/usr/bin/env python3
"""ccp-new — 纯 CLI profile 管理器：新建 / 修改 / 删除 / 列表。

用户只提供三样：名称 / URL / API key。其余全部继承 _template.json。

设计约束（都有实测依据，勿凭直觉改）：
  1. profile 必须自包含全部模板键，不能只写 URL+token。
     `--settings` 与 ~/.claude/settings.json 的 env 块是【逐键深合并】，
     profile 里没声明的键会从 baseline 渗漏 —— 实测过：只写 base_url 不写
     token，请求打到了新 host 却带着 cc-switch 当前那家的 token。
     ANTHROPIC_API_KEY 保持模板原值（空串）：空串=中和，键缺失=渗漏。
  2. 新建与修改落盘前都必须做上游认证连通性验证。公益站的 key 状态会变
     （同一个号几分钟内 200 → 403「分组已删除」），写一个不能用的 profile
     等于埋雷。
  3. 原子提交：tmp + os.replace + fsync。验证失败时新增不留文件、修改不动
     旧文件与旧 .bak，退出码非 0。
  4. 覆盖/修改同名先备份为固定名 <name>.json.bak（不带时间戳）—— 带时间戳
     会让每次改 key 都在盘上多留一份明文旧凭据。
  5. 删除【不占用】.bak 槽位（那是「上一版」，删除若写它会冲掉覆盖历史），
     而是 os.replace 移入 _trash/<name>.json.<UTC时间戳>。_trash 以 _ 开头，
     ccp 与 ccp-check 都不扫描，天然隔离；文件从不真正消失，可原地恢复。
  6. 验证用【认证 GET /v1/models】，绝不 POST /v1/messages —— 后者会触发
     真实生成、产生 token 消耗与计费，超出「连通性 + 凭据有效」的边界。
  7. 必须带 Claude Code User-Agent：实测 sub.100xlabs.space 的 Cloudflare
     对 Python-urllib/3.x UA 返回 403，同 URL/key 换 claude-cli UA 即 200；
     且假 key 配该 UA 仍返回 401，证明 UA 只过 WAF、不绕鉴权。

退出码：
  0 成功   1 上游验证失败   2 用法/输入错误   130 用户取消
"""

from __future__ import annotations

import fcntl
import getpass
import json
import os
import re
import secrets
import shlex
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit

DIR = Path(os.environ.get("CCP_PROFILE_DIR") or (Path.home() / ".claude-profiles"))
TEMPLATE = DIR / "_template.json"
TRASH = DIR / "_trash"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

VERIFY_TIMEOUT = float(os.environ.get("CCP_VERIFY_TIMEOUT") or 30)
VERIFY_MODEL_KEY = "ANTHROPIC_MODEL"
# Cloudflare 1010 = 按浏览器签名拦截。urllib 默认 UA 是 Python-urllib/3.x，
# sub.100xlabs.space 实测会 403；Claude Code 真实 UA 同 key 返回 200。
# 单一真源。改默认值会让百倍系 profile 的验证全部误判为失败。
# 覆盖方式：CCP_VERIFY_UA=... （只此一个 header，故覆盖必定生效）
VERIFY_USER_AGENT = os.environ.get("CCP_VERIFY_UA") or "claude-cli/2.1.227 (external, cli)"

EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 130

# ------------------------------------------------------- 键分类（展示与验证用）
#
# 【硬验证名单】改这三个键必须上游点头，失败硬拒。改其它键只做 advisory。
# 依据：现网 14 个 profile 与模板【只有】这两三个键值不同（AUTH_TOKEN/BASE_URL），
# 即「provider 差异」就是它们；其余 34 键是继承来的公共配置。
# 为什么其余不硬拒：会造成构造性死锁 —— 要改的正是坏掉的 HTTPS_PROXY，
# 强制验证必因该代理坏而失败，于是永远修不了导致失败的那个字段。
GATED_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")

# 展示分组。硬编码，未命中的键落「其它」——新键不会消失，只是不分组。
# 现网 37 键已逐键核对覆盖 37/37（2+1+11+3+5+6+9）。
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("凭据", ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")),
    ("端点", ("ANTHROPIC_BASE_URL",)),
    ("模型", ()),      # 动态：键名含 MODEL
    ("上下文", ("ANTHROPIC_BETAS", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
              "CLAUDE_CODE_AUTO_COMPACT_WINDOW")),
    ("代理", ()),      # 动态：键名含 PROXY
    ("遥测", ()),      # 动态：TELEMETRY / ERROR_REPORTING / FEEDBACK
    ("其它", ()),
)

# 敏感键判定：移植 cc-switch-cli services/provider/common_config.rs:24
# is_sensitive_config_key（后缀 / 精确 / 包含 三段），不手维护黑名单。
_SENS_SUFFIX = ("_KEY", "_API_KEY", "_ACCESS_KEY", "_ACCESS_KEY_ID", "_KEY_ID",
                "_PRIVATE_KEY", "_APIKEY", "_ACCESSKEY", "_SECRETKEY", "_APITOKEN",
                "_AUTH_TOKEN", "_TOKEN", "_PAT", "_PWD", "_PASS", "_PASSPHRASE", "_CREDS")
_SENS_EXACT = ("APIKEY", "API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIALS")
_SENS_CONTAIN = ("SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE_KEY", "BEARER_TOKEN")

# 值形状敏感：URL 内嵌 userinfo（http://user:pass@host）。
# 【必须有这条】实测上面那个分类器在我们 37 键上只命中 ANTHROPIC_API_KEY 与
# ANTHROPIC_AUTH_TOKEN，不含 5 个 proxy 键。现网 proxy 值全为空串故无泄漏，
# 但一旦有人写 HTTPS_PROXY=http://user:pass@host，按键名判就会明文打印密码。
_USERINFO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://[^/@\s]+:[^/@\s]+@")

# 原型污染键（cc-switch-cli provider_json.rs:60 同款），新增键时拒绝
FORBIDDEN_KEY_NAMES = ("__proto__", "constructor", "prototype")
KEY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ENV_KEYS = 200          # 防误粘贴整个 settings.json

# 外部编辑器入口：只有显式 CCP_EDITOR 才启用；否则始终使用内置编辑器。
# 不读取通用 VISUAL/EDITOR，避免 shell 默认 Vim 把用户送进另一套退出语法。
# 无显式配置时使用内置字段编辑器，让 Enter/Esc 的保存/放弃语义由本程序
# 直接控制；只有显式 CCP_EDITOR 才启用外部编辑器。
BUILTIN_EDITOR = "__ccp_builtin__"


# ---------------------------------------------------------------- validation

class InputError(Exception):
    """交互流程中的可恢复输入错误：回菜单，不退出进程。"""


def fail(msg: str, code: int = EXIT_USAGE) -> None:
    print(f"✗ {msg}", file=sys.stderr)
    raise SystemExit(code)


def check_name(name: str, *, soft: bool = False) -> str:
    """soft=True 时抛 InputError（交互模式回菜单），否则 SystemExit。"""
    name = (name or "").strip()
    err = None
    if not name:
        err = "名称不能为空"
    elif name.startswith("_") or name.startswith("."):
        err = "名称不能以 . 或 _ 开头（_* 会被 ccp 当作非 profile 跳过）"
    elif not NAME_RE.match(name):
        err = "名称只允许 字母/数字/. _ -"
    if err:
        if soft:
            raise InputError(err)
        fail(err)
    return name


def check_url(url: str, *, soft: bool = False) -> str:
    url = (url or "").strip().rstrip("/")
    err = None
    if not url:
        err = "URL 不能为空"
    else:
        if "://" not in url:
            url = "https://" + url
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            err = f"URL scheme 只支持 http/https：{parts.scheme or '(空)'}"
        elif not parts.hostname:
            err = f"URL 缺少主机名：{url}"
    if err:
        if soft:
            raise InputError(err)
        fail(err)
    return url


def check_key(key: str, *, soft: bool = False) -> str:
    key = (key or "").strip()
    if not key:
        if soft:
            raise InputError("API key 不能为空")
        fail("API key 不能为空")
    return key


def load_template() -> dict:
    if not TEMPLATE.is_file():
        fail(f"模板不存在：{TEMPLATE}")
    try:
        data = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"模板不是合法 JSON：{exc}")
    if not isinstance(data, dict) or not isinstance(data.get("env"), dict):
        fail(f"模板缺少 env 字典：{TEMPLATE}")
    if not data["env"]:
        fail(f"模板 env 为空：{TEMPLATE}")
    return data


def list_profiles() -> list[str]:
    """以磁盘为准动态扫描：顶层 *.json，排除 _* 与点号文件。

    点号必须排掉，因为 ``Path.glob("*.json")`` **会**匹配前导点的文件，而整块编辑
    （菜单 b）会在同目录写 ``.edit-<pid>-<ts>.json`` 作为临时文件。一次崩掉的编辑
    留下的残骸于是被当成一个 profile：它顶层是裸 env、没有 ``env`` 包装层，
    ``read_profile`` 判它 structure_ok=False，整个栈就报 degraded——一个假红点，
    而编辑器越常用它越常亮。

    实测口径：``_MANIFEST.json`` / ``_template.json`` 靠 ``_`` 排除；
    ``.edit-*`` 靠点号排除。二者都不是 profile。
    """
    if not DIR.is_dir():
        return []
    return sorted(p.stem for p in DIR.glob("*.json")
                  if not p.name.startswith(("_", ".")))


def read_profile(name: str) -> dict:
    path = DIR / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise InputError(f"profile 不存在：{name}") from None
    except json.JSONDecodeError as exc:
        raise InputError(f"{name}.json 不是合法 JSON：{exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("env"), dict):
        raise InputError(f"{name}.json 缺少 env 字典")
    return data


def mask(key: str) -> str:
    if not key:
        return "(空)"
    return f"…{key[-4:]}" if len(key) >= 4 else "(过短)"


# ------------------------------------------------------------- 分类 / 掩码判定

def is_sensitive_key(name: str) -> bool:
    """键名判敏感。移植 cc-switch-cli is_sensitive_config_key 的三段规则。"""
    upper = name.upper()
    return (upper in _SENS_EXACT
            or any(upper.endswith(s) for s in _SENS_SUFFIX)
            or any(n in upper for n in _SENS_CONTAIN))


def is_sensitive_value(value: str) -> bool:
    """值判敏感：URL 里内嵌 user:pass@ 就当凭据（键名判不出来，见常量注释）。"""
    return bool(_USERINFO_RE.match((value or "").strip()))


def should_mask(name: str, value: str) -> bool:
    return is_sensitive_key(name) or is_sensitive_value(value)


def show_value(name: str, value: str, *, reveal: bool = False) -> str:
    """渲染一个键值。reveal=True 时一律明文（用户显式要求看凭据时用）。"""
    if value == "":
        return "''            (空串=中和)"
    if reveal or not should_mask(name, value):
        return repr(value)
    if is_sensitive_value(value) and not is_sensitive_key(name):
        # 键名无辜、值里有 userinfo：掩掉 userinfo 段，其余保留（否则没法排查）
        return repr(_USERINFO_RE.sub(lambda m: m.group(0).split("://")[0] + "://***:***@", value))
    return f"{value[:3]}…{value[-4:]}" if len(value) > 10 else "…(已掩码)"


def group_of(name: str) -> str:
    """键 → 展示分组。未命中一律落「其它」，新键不会从展示里消失。"""
    for label, fixed in GROUPS:
        if fixed and name in fixed:
            return label
    upper = name.upper()
    if "MODEL" in upper:
        return "模型"
    if "PROXY" in upper:
        return "代理"
    if "TELEMETRY" in upper or "ERROR_REPORTING" in upper or "FEEDBACK" in upper:
        return "遥测"
    return "其它"


def grouped_env(env: dict) -> list[tuple[str, list[str]]]:
    """按 GROUPS 顺序返回 [(组名, [键...])]，组内键名排序。空组不返回。"""
    buckets: dict[str, list[str]] = {label: [] for label, _ in GROUPS}
    for name in env:
        buckets[group_of(name)].append(name)
    return [(label, sorted(buckets[label])) for label, _ in GROUPS if buckets[label]]


def template_diff(env: dict) -> tuple[list[str], list[str], list[str]]:
    """与模板比：(值不同的键, profile 独有的超集键, 模板有而 profile 缺的键)。

    超集键必须显式列出来：baibei 系有模板没有的 ANTHROPIC_CUSTOM_HEADERS，
    编辑路径任何时候都不能把它裁掉（少一个键就多一处渗漏面）。
    """
    tpl = load_template()["env"]
    changed = sorted(k for k in env if k in tpl and env[k] != tpl[k])
    extra = sorted(k for k in env if k not in tpl)
    missing = sorted(k for k in tpl if k not in env)
    return changed, extra, missing


def summarize(name: str) -> str:
    try:
        env = read_profile(name)["env"]
    except InputError as exc:
        return f"{name:<14} <{exc}>"
    return (f"{name:<14} {env.get('ANTHROPIC_BASE_URL', '?'):<30} "
            f"{env.get(VERIFY_MODEL_KEY, '?'):<20} "
            f"{mask(env.get('ANTHROPIC_AUTH_TOKEN') or env.get('ANTHROPIC_API_KEY') or '')}")


# -------------------------------------------------------------------- verify

class VerifyResult:
    def __init__(self, ok: bool, kind: str, detail: str) -> None:
        self.ok = ok
        self.kind = kind
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def verify_upstream(url: str, key: str, model: str) -> VerifyResult:
    """有界超时的最小认证探测。只认真实响应，不做推断。

    用【认证 GET /v1/models】而非 POST /v1/messages（见文件头约束 6）。
    绝不 fallback 到生成请求：探测不到就报失败，不猜。

    区分：2xx 成功 / 401 未授权 / 403 禁止 / 4xx 其它 / 5xx 服务端 /
          超时 / 连接失败 / 响应不是合法 JSON。

    model 参数保留仅为签名兼容，GET 探测不使用它。
    """
    endpoint = url.rstrip("/") + "/v1/models"

    req = urllib.request.Request(endpoint, method="GET")
    req.add_header("accept", "application/json")
    req.add_header("authorization", f"Bearer {key}")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("user-agent", VERIFY_USER_AGENT)

    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=VERIFY_TIMEOUT, context=ctx) as resp:
            raw = resp.read(65536).decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(4096).decode("utf-8", "replace")
        except Exception:
            pass
        snippet = " ".join(body.split())[:200]
        if exc.code in (401, 407):
            return VerifyResult(False, "UNAUTHORIZED", f"HTTP {exc.code} 凭据被拒 {snippet}")
        if exc.code == 403:
            lowered = (body + snippet).lower()
            if "1010" in lowered or "error-1010" in lowered:
                return VerifyResult(
                    False,
                    "CLOUDFLARE_1010",
                    "HTTP 403 Cloudflare 1010（按 UA/浏览器签名拦截，不是 key 分组）。"
                    f" 探测器 UA={VERIFY_USER_AGENT!r} {snippet}",
                )
            return VerifyResult(False, "FORBIDDEN", f"HTTP {exc.code} 禁止访问 {snippet}")
        if 500 <= exc.code < 600:
            return VerifyResult(False, "SERVER_ERROR", f"HTTP {exc.code} 上游错误 {snippet}")
        return VerifyResult(False, "HTTP_ERROR", f"HTTP {exc.code} {snippet}")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return VerifyResult(False, "TIMEOUT", f"{VERIFY_TIMEOUT:.0f}s 内无响应")
        return VerifyResult(False, "CONNECT_FAILED", f"连接失败：{reason}")
    except TimeoutError:
        return VerifyResult(False, "TIMEOUT", f"{VERIFY_TIMEOUT:.0f}s 内无响应")
    except Exception as exc:  # noqa: BLE001 - 探测不得让向导崩掉
        return VerifyResult(False, "ERROR", f"{type(exc).__name__}: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        snippet = " ".join(raw.split())[:200]
        return VerifyResult(False, "INVALID_JSON", f"HTTP {status} 响应不是 JSON：{snippet}")

    if not isinstance(data, dict):
        return VerifyResult(False, "INVALID_JSON", f"HTTP {status} 响应不是 JSON 对象")
    if data.get("type") == "error" or "error" in data:
        err = data.get("error")
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return VerifyResult(False, "API_ERROR", f"HTTP {status} {msg}")
    if 200 <= status < 300:
        # GET /v1/models 的正常形状是 {"data": [ {...}, ... ]}。
        # 只报数量，不解析型号：这里只证「凭据可用 + 端点连通」。
        listing = data.get("data")
        if isinstance(listing, list):
            return VerifyResult(True, "OK", f"HTTP {status} /v1/models 返回 {len(listing)} 个模型")
        return VerifyResult(True, "OK", f"HTTP {status} /v1/models 返回合法 JSON")
    return VerifyResult(False, "HTTP_ERROR", f"HTTP {status}")


def probe_model() -> str:
    model = load_template()["env"].get(VERIFY_MODEL_KEY) or ""
    # [1M] 是 beta 头标记不是模型名（实测 wire model 恒为裸名），探测时剥掉
    return re.sub(r"\[.*?\]$", "", model).strip() or "claude-opus-5"


# -------------------------------------------------------------------- commit

def build_profile(url: str, key: str, *, base_env: dict | None = None) -> dict:
    """模板 deepcopy 为公共配置真源；base_env 里模板没有的键予以保留。

    保留超集键是刻意的：baibei 系有模板没有的 ANTHROPIC_CUSTOM_HEADERS，
    修改时若按模板裁掉，等于静默改变 profile 形状（见文件头约束 1，
    少一个键就多一处渗漏面）。所以「继承模板」+「不丢已有键」两者都要。
    """
    data = deepcopy(load_template())
    env = data["env"]
    if base_env:
        for k, v in base_env.items():
            if k not in env:
                env[k] = v
    env["ANTHROPIC_BASE_URL"] = url
    env["ANTHROPIC_AUTH_TOKEN"] = key
    # ANTHROPIC_API_KEY 保持模板原值，不覆盖也不删除（见文件头约束 1）
    return data


def _write_fsync(path: Path, raw: bytes, *, exclusive: bool = False) -> None:
    """0600 写入并 fsync。给原子提交当积木用，单独存在是为了两条写入路径同源。

    exclusive=True 时用 O_EXCL：随机名临时文件必须是本次调用新建的，
    路径已存在说明撞名或有残留，宁可失败也不覆盖别人的半成品。
    """
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())


def _dir_fsync(path: Path) -> None:
    """fsync 目录项本身：os.replace 只保证重命名原子，不保证掉电后目录项落盘。"""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _tmp_name(dest: Path, tag: str) -> Path:
    """随机命名的同目录临时路径。固定名（旧版 .<name>.tmp）在两个并发提交里
    会互相 O_TRUNC 对方刚 fsync 完的内容；随机名 + O_EXCL 让碰撞变成显式失败。"""
    return dest.parent / f".{dest.name}.{os.getpid()}.{secrets.token_hex(4)}.{tag}"


def _rm_quiet(path: Path) -> None:
    """清理临时文件。临时文件含明文凭据，清理本身不许再抛异常掩盖真实错误。"""
    try:
        path.unlink()
    except OSError:
        pass


def atomic_commit(dest: Path, raw: bytes, *, backup: bool = True) -> Path | None:
    """把 raw 原子写入 dest，可选先备份为 <dest>.bak。返回备份路径或 None。

    【顺序是关键，勿改回「先备份再写新内容」】2026-08-30 故障注入实测：
    旧顺序一旦新内容写入失败（磁盘满 / 临时路径被占），.bak 已经被刷成
    「当前代」，上一代回滚点当场消失 —— 目标文件确实没变，但你已经回滚不到
    改动前的那份凭据了（实测 .bak 里的 token 从 GEN0 变成 GEN1）。
    所以先把新内容写进 tmp 并 fsync（此时 dest 与 .bak 都没碰），再备份，
    最后一步才 os.replace 换入。

    任何一步失败：把 .bak 恢复到进函数前的字节，清掉两个临时文件（含明文
    凭据），并转成 InputError —— 交互路径不该看见 raw traceback。

    【错误信息必须区分「换入前失败」与「换入后失败」】R2 故障注入实测：
    os.chmod 失败发生在 os.replace 之后，此时新内容【已经生效】。旧版本
    对所有失败都说「目标文件与 .bak 都已保持改动前的原状」，等于在凭据
    已经换成新的那一刻报告「什么都没变」—— 用户会照着这句话去重试或回滚，
    实际操作的是已经生效的新文件。

    【C1 加固 2026-09-01】三件并发/掉电防护：
    1. per-profile flock（.<name>.lock，阻塞独占）：两个并发提交串行化，
       否则「读 .bak → 写 tmp → 备份 → 换入」的交错会让回滚点互相踩踏；
    2. 随机名 + O_EXCL 临时文件：旧固定名 .<name>.tmp 会被并发方 O_TRUNC，
       fsync 完的内容在换入前被清空；
    3. 换入后 fsync 父目录：os.replace 只保证重命名原子，不保证掉电后
       目录项落盘。锁文件本身不删除（unlink 与 flock 有经典竞态）。
    """
    dest.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = _tmp_name(dest, "tmp")
    tmp_bak = _tmp_name(dest, "bak.tmp")
    restore_tmp = _tmp_name(dest, "restore.tmp")
    bak_path = dest.parent / f"{dest.name}.bak"
    made_backup: Path | None = None
    bak_before: bytes | None = None                  # 先初始化：except 分支会读它
    swapped = False                                 # dest 是否已被换成新内容

    lock_fd = os.open(dest.parent / f".{dest.name}.lock",
                      os.O_RDONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # 读既有 .bak 也必须在 try 内：R2 实测 .bak 不可读时（PermissionError）
        # 旧版本让 raw traceback 穿透到交互菜单，违反本函数自己的契约。
        bak_before = bak_path.read_bytes() if bak_path.is_file() else None
        _write_fsync(tmp, raw, exclusive=True)      # 1) 新内容先落盘，dest/.bak 未动
        if backup and dest.exists():                # 2) 再备份旧 dest
            _write_fsync(tmp_bak, dest.read_bytes(), exclusive=True)
            os.replace(tmp_bak, bak_path)
            made_backup = bak_path
        os.replace(tmp, dest)                       # 3) 最后一步原子换入
        swapped = True
        os.chmod(dest, 0o600)
        _dir_fsync(dest.parent)                     # 4) 目录项落盘，重命名才算持久
    except OSError as exc:
        if swapped:
            # 换入已成功、只是收尾失败（如 chmod / 目录 fsync）。不许谎报
            # 「原状」，也不许把已生效的新内容悄悄回滚 —— 照实说清现状与
            # 需要人工确认的点。
            raise InputError(
                f"写入已生效但收尾失败：{exc}\n"
                f"  新内容【已经】写入 {dest.name}"
                + (f"，旧内容在 {bak_path.name}" if made_backup else "")
                + f"\n  请手动确认权限：chmod 600 {dest}"
            ) from None
        if made_backup is not None:                 # 步骤 2 已覆盖 .bak，回滚它
            try:
                if bak_before is None:
                    _rm_quiet(bak_path)
                else:
                    _write_fsync(restore_tmp, bak_before, exclusive=True)
                    os.replace(restore_tmp, bak_path)
            except OSError:
                pass
        raise InputError(
            f"写入失败：{exc}\n  目标文件与 .bak 都已保持改动前的原状，临时文件已清理"
        ) from None
    finally:
        _rm_quiet(tmp)
        _rm_quiet(tmp_bak)
        _rm_quiet(restore_tmp)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    return made_backup


def commit(name: str, data: dict) -> tuple[Path, Path | None]:
    """原子提交一个 profile。返回 (目标路径, 备份路径或 None)。"""
    DIR.mkdir(mode=0o700, exist_ok=True)
    dest = DIR / f"{name}.json"
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    return dest, atomic_commit(dest, raw)


def to_trash(path: Path) -> Path:
    """os.replace 移入 _trash（同一文件系统，原子）。不 unlink，可恢复。"""
    TRASH.mkdir(mode=0o700, exist_ok=True)
    os.chmod(TRASH, 0o700)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = TRASH / f"{path.name}.{stamp}"
    n = 1
    while target.exists():
        n += 1
        target = TRASH / f"{path.name}.{stamp}.{n}"
    os.replace(path, target)
    os.chmod(target, 0o600)
    return target


def launch_hint(name: str) -> str:
    return (
        "\n启动命令（复制即用）:\n"
        "\n  新会话:\n"
        "    source ~/.zshrc\n"
        f"    ccp {name}\n"
        "\n  续会话（换成你的 session-id）:\n"
        "    source ~/.zshrc\n"
        f"    ccp {name} --resume <session-id>\n"
    )


# ------------------------------------------------------------------------ io

def ask(prompt: str, *, secret: bool = False) -> str:
    """读一行输入。secret=True 时不回显（API key 不该留在屏幕和 scrollback）。

    getpass 在无 TTY 时会退化成读 stdin 并打 warning，仍能工作（测试用得上）；
    取消语义与普通输入一致：EOF / Ctrl-C → 130。
    """
    try:
        if secret:
            return getpass.getpass(prompt)
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        raise SystemExit(EXIT_CANCELLED) from None


def ask_soft(prompt: str, *, secret: bool = False) -> str | None:
    """菜单内的输入：EOF/Ctrl-C 返回 None（回上一层），不炸掉进程。"""
    try:
        return getpass.getpass(prompt) if secret else input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None


# ---------------------------------------------------------------------- flow

def run(name: str, url: str, key: str, *, assume_yes: bool = False) -> int:
    """非交互路径：ccp-new NAME URL KEY / -y NAME URL KEY。保持旧行为。"""
    name = check_name(name)
    url = check_url(url)
    key = check_key(key)

    dest = DIR / f"{name}.json"
    overwriting = dest.exists()
    pm = probe_model()

    print(f"\n名称 : {name}{'   (覆盖已有)' if overwriting else ''}")
    print(f"URL  : {url}")
    print(f"KEY  : {mask(key)} (len={len(key)})")
    print(f"模型 : {pm}")

    if overwriting and not assume_yes:
        ans = ask(f"\n{name} 已存在，覆盖？旧文件会存为 {name}.json.bak [y/N]: ")
        if ans.strip().lower() not in ("y", "yes"):
            print("已取消，未改动任何文件。")
            return EXIT_CANCELLED

    print(f"\n验证上游（超时 {VERIFY_TIMEOUT:.0f}s）…")
    res = verify_upstream(url, key, pm)
    if not res.ok:
        print(f"✗ 上游验证失败 — {res}", file=sys.stderr)
        if overwriting:
            print(f"  未改动 {dest.name}，也未更新 .bak", file=sys.stderr)
        else:
            print("  未创建任何文件", file=sys.stderr)
        return EXIT_VERIFY_FAILED
    print(f"✓ 上游可用 — {res}")

    base_env = None
    if overwriting:
        try:
            base_env = read_profile(name)["env"]
        except InputError:
            base_env = None
    path, backup = commit(name, build_profile(url, key, base_env=base_env))

    print(f"\n✓ 已写入 {path}  ({len(json.loads(path.read_text())['env'])} 键, 权限 600)")
    if backup:
        print(f"  旧文件已备份 {backup.name}（回滚: cp {backup} {path}）")
    print(launch_hint(name), end="")
    return EXIT_OK


# ------------------------------------------------------------------- actions

def pick_profile(action: str) -> str | None:
    """数字选择已有 profile。空行/EOF 返回 None（回菜单）。"""
    names = list_profiles()
    if not names:
        print("（还没有任何 profile）")
        return None
    print(f"\n选择要{action}的 profile：")
    for i, n in enumerate(names, 1):
        print(f"  [{i}] {summarize(n)}")
    raw = ask_soft("\n序号（空行返回）: ")
    if raw is None or not raw.strip():
        return None
    raw = raw.strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(names)):
        print(f"✗ 请输入 1..{len(names)} 之间的序号", file=sys.stderr)
        return None
    return names[int(raw) - 1]


def action_new() -> int:
    print("\n— 新建 profile —")
    print("其余配置全部继承模板，只需填 3 项。空行返回菜单。")
    raw_name = ask_soft("ccp 名称        : ")
    if raw_name is None or not raw_name.strip():
        return EXIT_CANCELLED
    raw_url = ask_soft("URL             : ")
    if raw_url is None or not raw_url.strip():
        return EXIT_CANCELLED
    raw_key = ask_soft("API key（不回显）: ", secret=True)
    if raw_key is None or not raw_key.strip():
        return EXIT_CANCELLED

    name = check_name(raw_name, soft=True)
    url = check_url(raw_url, soft=True)
    key = check_key(raw_key, soft=True)

    dest = DIR / f"{name}.json"
    if dest.exists():
        ans = ask_soft(f"{name} 已存在，覆盖？旧文件存为 {name}.json.bak [y/N]: ")
        if ans is None or ans.strip().lower() not in ("y", "yes"):
            print("已取消，未改动任何文件。")
            return EXIT_CANCELLED
    return _verify_then_commit(name, url, key,
                              base_env=read_profile(name)["env"] if dest.exists() else None,
                              overwriting=dest.exists())


def action_edit() -> int:
    name = pick_profile("修改")
    if name is None:
        return EXIT_CANCELLED
    env = read_profile(name)["env"]
    cur_url = env.get("ANTHROPIC_BASE_URL", "")
    cur_key = env.get("ANTHROPIC_AUTH_TOKEN", "")

    print(f"\n— 修改 {name} —")
    print(f"  当前 URL   : {cur_url}")
    print(f"  当前 KEY   : {mask(cur_key)}")
    print(f"  当前 模型  : {env.get(VERIFY_MODEL_KEY, '?')}")
    print("回车 = 保留当前值。空的 API key = 沿用旧 key。")

    raw_name = ask_soft(f"\n新名称 [{name}]: ")
    if raw_name is None:
        return EXIT_CANCELLED
    new_name = check_name(raw_name.strip() or name, soft=True)

    raw_url = ask_soft(f"新 URL [{cur_url}]: ")
    if raw_url is None:
        return EXIT_CANCELLED
    new_url = check_url(raw_url.strip() or cur_url, soft=True)

    raw_key = ask_soft("新 API key（不回显，空=沿用旧）: ", secret=True)
    if raw_key is None:
        return EXIT_CANCELLED
    new_key = check_key(raw_key.strip() or cur_key, soft=True)

    renaming = new_name != name
    if renaming and (DIR / f"{new_name}.json").exists():
        raise InputError(f"目标名已存在，拒绝覆盖：{new_name}")

    print(f"\n名称 : {name}" + (f"  →  {new_name}" if renaming else "  (不变)"))
    print(f"URL  : {new_url}" + ("" if new_url == cur_url else "   (已改)"))
    print(f"KEY  : {mask(new_key)}" + ("  (沿用)" if new_key == cur_key else "   (已改)"))

    rc = _verify_then_commit(new_name, new_url, new_key, base_env=env,
                             overwriting=not renaming)
    if rc == EXIT_OK and renaming:
        moved = to_trash(DIR / f"{name}.json")
        print(f"  原 {name}.json 已移入回收站：{moved}")
        old_bak = DIR / f"{name}.json.bak"
        if old_bak.exists():
            print(f"  原备份也已移入：{to_trash(old_bak)}")
    return rc


def validate_env(env: object, *, allow_new_keys: bool = True) -> dict:
    """落盘前的形状校验。任何一条不过就抛 InputError，调用方一律不写文件。

    内联 ccp-check 的三条判据（union 缺键 / DANGER 缺键 / 凭据双空），
    这样非法内容在本工具里就被拦住，而不是等 ccp 启动时才被闸门拒掉。
    """
    if not isinstance(env, dict):
        raise InputError("env 必须是 JSON 对象")
    if not env:
        raise InputError("env 不能为空")
    if len(env) > MAX_ENV_KEYS:
        raise InputError(f"env 键数 {len(env)} 超过上限 {MAX_ENV_KEYS}（误粘贴了整个 settings.json？）")

    bad_type = sorted(k for k, v in env.items() if not isinstance(v, str))
    if bad_type:
        raise InputError(
            "env 的值必须全部是字符串（Claude Code 的 env 块只吃字符串，"
            f"数字/布尔/null 会静默行为异常）：{', '.join(bad_type)}")

    forbidden = sorted(k for k in env if k in FORBIDDEN_KEY_NAMES)
    if forbidden:
        raise InputError(f"禁止的键名（原型污染）：{', '.join(forbidden)}")
    malformed = sorted(k for k in env if not KEY_NAME_RE.match(k))
    if malformed:
        raise InputError(f"键名形状非法（须匹配 ^[A-Za-z_][A-Za-z0-9_]*$）：{', '.join(malformed)}")

    tpl = load_template()["env"]
    missing = sorted(k for k in tpl if k not in env)
    if missing:
        raise InputError(
            f"缺 {len(missing)} 个模板键，会从 ~/.claude/settings.json 渗漏 baseline 值："
            f"{', '.join(missing)}（要清空请置为空串，不要删键）")
    if not allow_new_keys:
        added = sorted(k for k in env if k not in tpl)
        if added:
            raise InputError(f"此路径不允许新增键：{', '.join(added)}")

    if not (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")):
        raise InputError("ANTHROPIC_AUTH_TOKEN 与 ANTHROPIC_API_KEY 不能双空（无凭据）")
    return env


def verify_env_tiered(env: dict, changed: list[str]) -> tuple[bool, str]:
    """按改动的键分级验证。返回 (是否允许落盘, 打印用摘要)。

    凭据/端点被改 → 硬验证，失败即禁止落盘。
    其余键被改   → advisory：照实打印真实失败原因，但【允许】落盘。
    """
    gated = [k for k in changed if k in GATED_KEYS]
    url = env.get("ANTHROPIC_BASE_URL", "")
    key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or ""

    if os.environ.get("CCP_SKIP_VERIFY") == "1":
        if gated:
            raise InputError(
                "CCP_SKIP_VERIFY=1 不能跳过凭据/端点验证："
                f"{', '.join(gated)} 属硬验证名单（写一个打不通的凭据等于埋雷）")
        return True, "⚠ 已跳过上游验证（CCP_SKIP_VERIFY=1）—— 未证实上游可用"

    tier = "硬验证" if gated else "advisory"
    print(f"\n验证上游（{tier}，超时 {VERIFY_TIMEOUT:.0f}s）…")
    res = verify_upstream(url, key, probe_model())
    if res.ok:
        return True, f"✓ 上游可用 — {res}"

    if gated:
        return False, (f"✗ 上游验证失败 — {res}\n"
                       f"  改动含硬验证键（{', '.join(gated)}），已拒绝落盘")
    require = os.environ.get("CCP_EDIT_REQUIRE_VERIFY") == "1"
    if require:
        return False, (f"✗ 上游验证失败 — {res}\n"
                       "  CCP_EDIT_REQUIRE_VERIFY=1 要求非凭据改动也必须通过，已拒绝落盘")
    return True, (f"⚠ advisory：上游验证未通过 — {res}\n"
                  "  改动不含凭据/端点键，仍按你的要求落盘。"
                  "（要让这类改动也硬拒：CCP_EDIT_REQUIRE_VERIFY=1）")


def commit_env(name: str, env: dict, changed: list[str], *,
               label: str = "profile", doc: dict | None = None) -> int:
    """校验 → 分级验证 → 原子落盘。失败时目标文件与 .bak 都不动。

    doc = profile 的完整原文档；只替换它的 env，其余顶层键【原样保留】。
    【勿改回写死 {"env": env}】2026-08-30 实测：那样写会把 env 之外的顶层键
    静默删掉。现网 14 个 profile 恰好只有 env 所以没暴露，但同一形状的
    ~/.claude/settings.json 真实带着 permissions/hooks/statusLine 等 11 个
    顶层键 —— 一旦有人照它建 profile，编辑一次就把这些键连带清掉。
    """
    validate_env(env)
    allowed, summary = verify_env_tiered(env, changed)
    print(summary)
    if not allowed:
        print(f"  未改动 {name}.json，也未更新 .bak", file=sys.stderr)
        return EXIT_VERIFY_FAILED

    payload = deepcopy(doc) if isinstance(doc, dict) else {}
    payload["env"] = env
    path, backup = commit(name, payload)
    written = json.loads(path.read_text(encoding="utf-8"))["env"]
    print(f"\n✓ 已写入 {path}  ({len(written)} 键, 权限 600)")
    print(f"  本次改动 {len(changed)} 个键：{', '.join(changed)}")
    if backup:
        print(f"  旧文件已备份 {backup.name}（回滚: cp {backup} {path}）")
    if label == "profile":
        print(launch_hint(name), end="")
    return EXIT_OK


def _verify_then_commit(name: str, url: str, key: str, *,
                        base_env: dict | None, overwriting: bool) -> int:
    print(f"\n验证上游（超时 {VERIFY_TIMEOUT:.0f}s）…")
    res = verify_upstream(url, key, probe_model())
    if not res.ok:
        print(f"✗ 上游验证失败 — {res}", file=sys.stderr)
        if overwriting:
            print(f"  未改动 {name}.json，也未更新 .bak", file=sys.stderr)
        else:
            print("  未创建任何文件", file=sys.stderr)
        return EXIT_VERIFY_FAILED
    print(f"✓ 上游可用 — {res}")

    path, backup = commit(name, build_profile(url, key, base_env=base_env))
    keys = len(json.loads(path.read_text(encoding="utf-8"))["env"])
    print(f"\n✓ 已写入 {path}  ({keys} 键, 权限 600)")
    if backup:
        print(f"  旧文件已备份 {backup.name}（回滚: cp {backup} {path}）")
    print(launch_hint(name), end="")
    return EXIT_OK


# ------------------------------------------------------------ 展示：差异 / 全键

def action_show() -> int:
    """[s] 只列与模板不同的键。现网 linxi-4y 就 2 行，不糊 37 行。"""
    name = pick_profile("查看差异")
    if name is None:
        return EXIT_CANCELLED
    env = read_profile(name)["env"]
    tpl = load_template()["env"]
    changed, extra, missing = template_diff(env)

    print(f"\n— {name} 与模板的差异 —")
    print(f"  模板 {len(tpl)} 键 / 本 profile {len(env)} 键")
    if not changed and not extra and not missing:
        print("\n  与模板逐键完全一致（含凭据为空）——这通常意味着还没配 URL/key。")
        return EXIT_OK

    if changed:
        print(f"\n  值不同 {len(changed)} 个：")
        for k in changed:
            print(f"    {k:<38} 模板={show_value(k, tpl[k])}")
            print(f"    {'':<38} 本档={show_value(k, env[k])}")
    if extra:
        print(f"\n  本 profile 独有（超集键，编辑时一律保留不裁）{len(extra)} 个：")
        for k in extra:
            print(f"    {k:<38} {show_value(k, env[k])}")
    if missing:
        # 这是渗漏面，必须显著提示：缺键会从 ~/.claude/settings.json 继承 baseline 值
        print(f"\n  ⚠ 缺模板键 {len(missing)} 个（会渗漏 baseline 值，ccp 启动会被闸门拒）：")
        for k in missing:
            print(f"    {k}")
    return EXIT_OK


def action_view() -> int:
    """[v] 分组展示全键。默认掩码，输入 k 才明文（凭据要能直接取用）。"""
    name = pick_profile("查看全部配置")
    if name is None:
        return EXIT_CANCELLED
    return _render_env(name, read_profile(name)["env"], reveal=False)


def _render_env(name: str, env: dict, *, reveal: bool) -> int:
    tpl = load_template()["env"]
    while True:
        print(f"\n— {name} 全部配置（{len(env)} 键{'，明文' if reveal else '，敏感项已掩码'}）—")
        for label, keys in grouped_env(env):
            print(f"\n  [{label}]")
            for k in keys:
                flag = ""
                if k not in tpl:
                    flag = "  ← 超集键"
                elif env[k] != tpl[k]:
                    flag = "  ← 已改"
                print(f"    {k:<38} {show_value(k, env[k], reveal=reveal)}{flag}")
        if reveal:
            return EXIT_OK
        ans = ask_soft("\n输入 k 显示明文凭据，回车返回: ")
        if ans is None or ans.strip().lower() != "k":
            return EXIT_OK
        reveal = True


# --------------------------------------------------------------- 修改：单键 / 整块

def action_field() -> int:
    """[f] 改单个键的值。禁增删键；'-' 表示置空串（中和），不是删键。"""
    name = pick_profile("改单个配置项")
    if name is None:
        return EXIT_CANCELLED
    doc = read_profile(name)
    env = dict(doc["env"])
    tpl = load_template()["env"]

    keys: list[str] = []
    print(f"\n— 改 {name} 的单个配置项 —")
    for label, group_keys in grouped_env(env):
        print(f"\n  [{label}]")
        for k in group_keys:
            keys.append(k)
            mark = "" if k in tpl else "  ← 超集键"
            print(f"  [{len(keys):>2}] {k:<38} {show_value(k, env[k])}{mark}")

    raw = ask_soft(f"\n序号 1..{len(keys)}（空行返回）: ")
    if raw is None or not raw.strip():
        return EXIT_CANCELLED
    raw = raw.strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(keys)):
        raise InputError(f"请输入 1..{len(keys)} 之间的序号")
    key = keys[int(raw) - 1]
    old = env[key]

    print(f"\n  键     : {key}")
    print(f"  当前值 : {show_value(key, old, reveal=True)}")
    if key in tpl:
        print(f"  模板值 : {show_value(key, tpl[key], reveal=True)}")
    print("  回车 = 不改；输入 - = 置空串（中和，不删键）")
    if key in GATED_KEYS:
        print(f"  ⚠ {key} 属硬验证名单：改动必须通过 GET /v1/models，失败拒绝落盘")

    new = ask_soft("  新值: ", secret=is_sensitive_key(key))
    if new is None:
        return EXIT_CANCELLED
    new = new.strip()
    if not new:
        print("未改动任何值。")
        return EXIT_CANCELLED
    new = "" if new == "-" else new
    if new == old:
        print("新值与当前值相同，未改动任何文件。")
        return EXIT_CANCELLED

    if key == "ANTHROPIC_BASE_URL" and new:
        new = check_url(new, soft=True)

    env[key] = new
    print(f"\n  {key}")
    print(f"    旧 {show_value(key, old)}")
    print(f"    新 {show_value(key, new)}")
    return commit_env(name, env, [key], doc=doc)


def find_editor() -> list[str]:
    """返回用户明确指定的外部编辑器；否则始终返回内置字段编辑器。

    ``VISUAL``/``EDITOR`` 是 shell 的通用变量，常常被无意设置为 Vim。
    ccp-new 不再因这两个环境变量把用户送进另一套退出语法；需要外部
    编辑器时必须显式设置 ``CCP_EDITOR``，这样默认 Enter/Esc 规则始终稳定。
    """
    var = "CCP_EDITOR"
    raw = (os.environ.get(var) or "").strip()
    if raw:
        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            raise InputError(f"${var} 不是合法命令行（引号不配对？）：{raw} — {exc}") from None
        if not argv:
            return [BUILTIN_EDITOR]
        exe = shutil.which(argv[0])
        if not exe:
            raise InputError(f"${var}={raw} 指定的编辑器不存在于 PATH：{argv[0]}")
        return [exe, *argv[1:]]
    return [BUILTIN_EDITOR]


def editor_exit_hint(argv: list[str]) -> str:
    """显示当前编辑器的准确保存/退出方法，避免把用户丢进命令模式。"""

    name = Path(argv[0]).name.lower() if argv else ""
    if name == BUILTIN_EDITOR:
        return "内置编辑器：e 编辑选中值；Enter 保存并选择 y/n；Esc 放弃并选择 y/n。"
    if name in {"nano", "pico"}:
        return "nano：Ctrl-X 退出；保存询问按 Y，再按 Enter；放弃保存按 N。"
    if name in {"vim", "vi", "nvim"}:
        return "Vim：按 Esc 后输入 :wq 回车保存；输入 :q! 回车放弃。单独输入 qa 不会退出。"
    if name in {"emacs", "emacsclient"}:
        return "Emacs：Ctrl-X Ctrl-S 保存；Ctrl-X Ctrl-C 退出。"
    return "请使用当前编辑器的保存/退出命令；不保存可用 Ctrl-C 退出并放弃改动。"


def _builtin_draw(stdscr: object, row: int, text: str, width: int,
                  attr: int = 0) -> None:
    """在有限宽度终端内安全绘制一行（供内置编辑器使用）。"""
    if width <= 1:
        return
    try:
        stdscr.addnstr(row, 0, text, max(1, width - 1), attr)
    except Exception:
        # curses 在窗口刚缩小时可能对最后一个单元格抛 error；下一帧会重画。
        return


def _builtin_get_text(stdscr: object, key: str, current: str) -> tuple[bool, str]:
    """编辑一个字符串值。Enter 接受，Esc/Ctrl-C 取消本次字段编辑。"""
    import curses

    try:
        height, width = stdscr.getmaxyx()
    except Exception:
        height, width = 24, 100
    sensitive = is_sensitive_key(key)
    chars: list[str] = []
    try:
        curses.curs_set(1)
        curses.noecho()
    except curses.error:
        pass
    while True:
        stdscr.erase()
        _builtin_draw(stdscr, 0, f"编辑配置值：{key}", width)
        _builtin_draw(stdscr, 1, "Enter 接受本次值；Esc 取消本次字段编辑；Ctrl-C 取消", width)
        _builtin_draw(stdscr, 2, "空 Enter = 保持原值；输入 - = 置空串（中和）", width)
        _builtin_draw(stdscr, 4, f"当前值：{show_value(key, current)}", width)
        shown = "*" * len(chars) if sensitive else "".join(chars)
        _builtin_draw(stdscr, 6, f"新值：{shown}", width)
        try:
            stdscr.move(6, min(width - 2, 6 + len(shown)))
            stdscr.refresh()
            raw = stdscr.get_wch()
        except (AttributeError, curses.error):
            raw = stdscr.getch()
        # get_wch() returns one-character strings for ordinary keys (including
        # Enter/Esc); the getch() fallback returns integer key codes.
        if raw in (10, 13, "\n", "\r"):
            value = "".join(chars)
            return True, current if value == "" else ("" if value == "-" else value)
        if raw in (27, 3, "\x1b", "\x03"):
            return False, current
        if raw in (curses.KEY_BACKSPACE, 8, 127, "\b", "\x7f"):
            if chars:
                chars.pop()
            continue
        if isinstance(raw, int):
            if 0 <= raw < 256:
                raw = chr(raw)
            else:
                continue
        if raw.isprintable() and raw not in "\r\n":
            chars.append(raw)


def _builtin_confirm(stdscr: object, prompt: str) -> bool:
    """只接受 y/n 的确认框；其它输入保持在确认框，不误触提交。"""
    import curses

    try:
        _height, width = stdscr.getmaxyx()
    except Exception:
        width = 100
    while True:
        _builtin_draw(stdscr, max(0, (getattr(stdscr, "getmaxyx", lambda: (24, width))()[0] - 2)),
                      f"{prompt} [y/n]", width)
        try:
            stdscr.refresh()
            raw = stdscr.get_wch()
        except (AttributeError, curses.error):
            raw = stdscr.getch()
        if isinstance(raw, int):
            if 0 <= raw < 256:
                raw = chr(raw)
            else:
                continue
        if raw.lower() == "y":
            return True
        if raw.lower() == "n":
            return False


def _builtin_editor_loop(stdscr: object, payload: dict, hint: str) -> dict | None:
    """内置整块配置编辑器：Enter 保存，Esc 放弃，均再确认 y/n。"""
    import curses

    env = dict(payload)
    keys = sorted(env)
    selected = 0
    offset = 0
    try:
        curses.curs_set(0)
        stdscr.keypad(True)
    except curses.error:
        pass
    while True:
        try:
            height, width = stdscr.getmaxyx()
        except Exception:
            height, width = 24, 100
        visible = max(1, height - 6)
        if selected < offset:
            offset = selected
        if selected >= offset + visible:
            offset = selected - visible + 1
        stdscr.erase()
        _builtin_draw(stdscr, 0, "ccp-new 整块配置编辑（内置模式）", width)
        _builtin_draw(stdscr, 1, "↑↓/jk 选择值；e 编辑选中项；Enter 保存；Esc 放弃；q 放弃", width)
        _builtin_draw(stdscr, 2, hint, width)
        for screen_row, index in enumerate(range(offset, min(len(keys), offset + visible)), 4):
            name = keys[index]
            marker = "> " if index == selected else "  "
            _builtin_draw(stdscr, screen_row,
                          f"{marker}{name:<38} {show_value(name, env[name])}", width,
                          curses.A_REVERSE if index == selected else 0)
        _builtin_draw(stdscr, height - 2, "Enter 保存当前 JSON 修改？随后选择 y/n；Esc 放弃？随后选择 y/n", width)
        stdscr.refresh()
        raw = stdscr.getch()
        if raw in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif raw in (curses.KEY_DOWN, ord("j")):
            selected = min(max(0, len(keys) - 1), selected + 1)
        elif raw in (ord("e"), ord("E")) and keys:
            accepted, value = _builtin_get_text(stdscr, keys[selected], env[keys[selected]])
            if accepted:
                env[keys[selected]] = value
        elif raw in (10, 13, "\n", "\r"):
            if _builtin_confirm(stdscr, "保存当前 JSON 修改？"):
                return env
        elif raw in (27, "\x1b", ord("q"), ord("Q"), "q", "Q"):
            if _builtin_confirm(stdscr, "放弃当前 JSON 修改？"):
                return None


def builtin_edit_json(payload: dict, *, hint: str) -> dict | None:
    """运行内置字段编辑器，不创建临时明文 JSON 文件。"""
    import curses

    try:
        return curses.wrapper(lambda stdscr: _builtin_editor_loop(stdscr, payload, hint))
    except curses.error as exc:
        raise InputError(f"当前终端不支持内置编辑器：{exc}。可设置 CCP_EDITOR 使用外部编辑器。") from None


def edit_json_in_editor(payload: dict, *, hint: str) -> dict | None:
    """把 env 写成临时 JSON 交外部编辑器，回读解析。None = 用户放弃。

    临时文件 0600 且落在 profile 目录（同一文件系统 + 目录本身 0700），
    不用 /tmp：env 里含明文凭据，/tmp 是全局可读目录。
    """
    argv = find_editor()
    if argv == [BUILTIN_EDITOR]:
        print("\n  内置编辑器：Enter 保存并选择 y/n；Esc 放弃并选择 y/n。")
        return builtin_edit_json(payload, hint=hint)
    DIR.mkdir(mode=0o700, exist_ok=True)
    tmp = DIR / f".edit-{os.getpid()}-{int(time.time())}.json"
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(body)
    before = tmp.read_bytes()
    try:
        print(f"\n  编辑器 : {' '.join(argv)}")
        print(f"  临时文件: {tmp}  (0600)")
        print(f"  {hint}")
        print(f"  {editor_exit_hint(argv)}")
        print("  返回本菜单后才会校验并写入；编辑器内放弃 = 不改任何 profile。")
        try:
            rc = subprocess.call([*argv, str(tmp)])
        except OSError as exc:
            raise InputError(f"启动编辑器失败：{' '.join(argv)} — {exc}") from None
        if rc != 0:
            raise InputError(f"编辑器退出码 {rc}，按放弃处理，未改动任何文件")
        after = tmp.read_bytes()
        if after == before:
            print("  内容未变，未改动任何文件。")
            return None
        try:
            data = json.loads(after.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise InputError(f"编辑后的文件不是 UTF-8：{exc}") from None
        except json.JSONDecodeError as exc:
            raise InputError(
                f"编辑后的内容不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
                " —— 未改动任何文件，可重新进入编辑") from None
        if not isinstance(data, dict):
            raise InputError("编辑后的顶层必须是 JSON 对象")
        return data
    finally:
        # 明文凭据不留在盘上；用 missing_ok 以免编辑器自己把文件搬走时炸掉
        tmp.unlink(missing_ok=True)


def action_block() -> int:
    """[b] 整块编辑 env。落盘前逐键 diff，改了哪些键就按哪一级验证。"""
    name = pick_profile("整块编辑 env")
    if name is None:
        return EXIT_CANCELLED
    doc = read_profile(name)
    old = doc["env"]
    other_top = [k for k in doc if k != "env"]
    print(f"\n— 整块编辑 {name}（{len(old)} 键）—")
    print("  规则：只能改【值】。删键会被拒（缺键=渗漏 baseline），要清空请写 \"\"。")
    if other_top:
        # 编辑器里只给 env，其余顶层键原样保留（实测过：直接写 {"env":...}
        # 会把 permissions/statusLine 这类顶层键静默吃掉）
        print(f"  另有 {len(other_top)} 个顶层键不在本次编辑范围、原样保留："
              f"{', '.join(other_top)}")
    new = edit_json_in_editor(old, hint="改完保存退出。值必须都是字符串。")
    if new is None:
        return EXIT_CANCELLED

    validate_env(new)
    added = sorted(k for k in new if k not in old)
    removed = sorted(k for k in old if k not in new)
    if removed:
        raise InputError(
            f"不允许删键（缺键会从 ~/.claude/settings.json 渗漏 baseline 值）："
            f"{', '.join(removed)} —— 要清空请置为空串。未改动任何文件")
    changed = sorted(k for k in new if k in old and new[k] != old[k])
    if not changed and not added:
        print("逐键比对无差异，未改动任何文件。")
        return EXIT_CANCELLED

    print(f"\n  改动 {len(changed)} 个键：")
    for k in changed:
        print(f"    {k:<38} {show_value(k, old[k])}  →  {show_value(k, new[k])}")
    if added:
        print(f"  新增 {len(added)} 个键：")
        for k in added:
            print(f"    {k:<38} {show_value(k, new[k])}")
    return commit_env(name, new, changed + added, doc=doc)


def action_template() -> int:
    """[t] 编辑模板。只准改值：禁增删键、禁写非空 URL/token/key。

    禁凭据的理由：模板是所有后续新建的公共基线，把非空 token 写进去等于
    让每个新 profile 默认带上同一份凭据，且模板本身不在 ccp-check 扫描范围。
    """
    tpl = load_template()
    env = tpl["env"]
    print(f"\n— 编辑模板 _template.json（{len(env)} 键）—")
    print("  模板是【新建】时的公共基线。改它不影响已有 profile（不回溯）。")
    print("  硬约束：禁增删键；ANTHROPIC_BASE_URL / AUTH_TOKEN / API_KEY 必须保持空串。")
    ans = ask_soft("  继续？[y/N]: ")
    if ans is None or ans.strip().lower() not in ("y", "yes"):
        return EXIT_CANCELLED

    new = edit_json_in_editor(env, hint="只改值。三个凭据/端点键必须留空串。")
    if new is None:
        return EXIT_CANCELLED

    added = sorted(k for k in new if k not in env)
    removed = sorted(k for k in env if k not in new)
    if added or removed:
        raise InputError(
            "模板禁止增删键（union 是 37 键且被 ccp-check 当判据，"
            f"加键会让所有已有 profile 变成『缺键』被拒）：新增={added or '无'} 删除={removed or '无'}")
    bad_type = sorted(k for k, v in new.items() if not isinstance(v, str))
    if bad_type:
        raise InputError(f"模板值必须都是字符串：{', '.join(bad_type)}")
    leaked = sorted(k for k in GATED_KEYS if (new.get(k) or "").strip())
    if leaked:
        raise InputError(
            f"模板里这些键必须保持空串：{', '.join(leaked)}"
            " —— 写进模板等于把凭据/端点变成所有新建 profile 的默认值")

    changed = sorted(k for k in new if new[k] != env[k])
    if not changed:
        print("逐键比对无差异，未改动任何文件。")
        return EXIT_CANCELLED
    print(f"\n  模板改动 {len(changed)} 个键：")
    for k in changed:
        print(f"    {k:<38} {show_value(k, env[k])}  →  {show_value(k, new[k])}")
    print("\n  注：模板不做上游验证（它没有凭据可验），也不回溯已有 profile。")
    ans = ask_soft("  确认写入模板？[y/N]: ")
    if ans is None or ans.strip().lower() not in ("y", "yes"):
        print("已取消，未改动任何文件。")
        return EXIT_CANCELLED

    # 与 profile 走【同一个】原子提交原语：先写 tmp+fsync，再备份，最后 replace。
    # 手抄第二份写入逻辑是这轮 P1 的来源（旧模板路径先刷 .bak，写失败时把
    # 上一代模板回滚点冲成了当前代，实测 .bak 从 fe1d17a7 变成 d9f31bad）。
    tpl["env"] = new
    raw = (json.dumps(tpl, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    bak = atomic_commit(TEMPLATE, raw)
    print(f"\n✓ 已写入 {TEMPLATE}  ({len(new)} 键, 权限 600)")
    if bak:
        print(f"  旧模板已备份 {bak.name}（回滚: cp {bak} {TEMPLATE}）")
    return EXIT_OK


def action_delete() -> int:
    name = pick_profile("删除")
    if name is None:
        return EXIT_CANCELLED
    env = read_profile(name)["env"]
    path = DIR / f"{name}.json"

    print(f"\n— 删除 {name} —")
    print(f"  URL   : {env.get('ANTHROPIC_BASE_URL', '?')}")
    print(f"  模型  : {env.get(VERIFY_MODEL_KEY, '?')}")
    print(f"  KEY   : {mask(env.get('ANTHROPIC_AUTH_TOKEN') or '')}")
    print("\n这是本工具唯一不可逆动作（文件会移入回收站，可恢复）。")
    typed = ask_soft(f"请输入完整 profile 名以确认删除（输入 {name}）: ")
    if typed is None or typed.strip() != name:
        print("名称不匹配，已取消，未删除任何文件。")
        return EXIT_CANCELLED

    moved = to_trash(path)
    print(f"\n✓ 已移入回收站：{moved}")
    print("  恢复命令（绝对路径，复制即用）:")
    print(f"    cp {moved} {path} && chmod 600 {path}")
    print(f"  注：{name}.json.bak 未被占用/改动；ccp-check 不检查 _trash。")
    return EXIT_OK


def action_list() -> int:
    names = list_profiles()
    if not names:
        print("（还没有任何 profile）")
        return EXIT_OK
    print(f"\n{'名称':<14} {'URL':<30} {'模型':<20} KEY尾4")
    print("-" * 78)
    for n in names:
        print(f"  {summarize(n)}")
    return EXIT_OK


# ------------------------------------------------------- 只读状态（--status --json）
#
# 供外部控制器（cmux-stack）编排用。三条硬约束：
#   1. 只读 / 离线 / 不写盘。绝不调用 verify_upstream —— 那会打真实上游并可能计费，
#      状态查询必须是零副作用的，否则控制器每次 status 都在消耗额度。
#   2. 【构造式白名单】：逐字段显式构造输出，绝不「取 env 再删敏感键」。
#      删除式过滤对【今后新增的键】默认泄漏；构造式对新键默认不泄漏。
#      这是本函数唯一可接受的形状，勿改成 dict(env) 再 pop。
#   3. 只发布布尔/计数/校验结论。不出 token、不出尾 4 位（mask() 也不许用）、
#      不出 URL（含 host——host 本身即「哪家供应商」的情报且可能带 userinfo）、
#      不出 raw profile JSON、不出模型名。
#
# 为什么连 base_url 都不发布：ccp-list 已在交互路径给人看了。控制器是给 TUI /
# 日志 / metrics 消费的，那些面按 forbidden 清单禁写完整 URL。
STATUS_SCHEMA_VERSION = 1


def _status_profile(name: str) -> dict:
    """单个 profile 的结论式状态。任何读取失败都降级为布尔，不外泄异常正文。

    异常文案里可能带路径与 JSON 片段（json.JSONDecodeError 会带原文上下文），
    所以这里只发布 readable/structure_ok，不发布 exc 字符串。
    """
    path = DIR / f"{name}.json"
    row: dict = {
        "name": name,
        "readable": False,
        "structure_ok": False,
        "key_count": None,
        "has_credential": None,
        "template_complete": None,
        "missing_key_count": None,
        "extra_key_count": None,
        "mode": None,
        "mtime": None,
    }
    try:
        st = path.stat()
        row["mode"] = oct(st.st_mode & 0o777)
        row["mtime"] = int(st.st_mtime)
    except OSError:
        return row
    try:
        env = read_profile(name)["env"]
    except InputError:
        # 文件在但坏（非法 JSON / 缺 env）：readable 保持 False，结构化上报。
        return row
    row["readable"] = True
    row["structure_ok"] = True
    row["key_count"] = len(env)
    # 存在性布尔，不是值、不是长度、不是尾部。
    row["has_credential"] = bool(
        (env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
        or (env.get("ANTHROPIC_API_KEY") or "").strip()
    )
    try:
        _changed, extra, missing = template_diff(env)
    except SystemExit:
        # load_template() 内部走 fail() → SystemExit。模板坏不该让 status 崩，
        # 而应作为「模板不健康」在顶层字段体现，这里只标记未知。
        return row
    row["missing_key_count"] = len(missing)
    row["extra_key_count"] = len(extra)
    row["template_complete"] = not missing
    return row


def build_status() -> dict:
    """离线只读状态快照。不打网络、不写盘、不改任何文件。"""
    template_ok = False
    template_key_count = None
    try:
        tpl_env = load_template()["env"]
        template_ok = True
        template_key_count = len(tpl_env)
    except SystemExit:
        pass

    manifest_ok = False
    manifest_union_count = None
    try:
        man = json.loads((DIR / "_MANIFEST.json").read_text(encoding="utf-8"))
        union = man.get("union_keys")
        if isinstance(union, list) and union:
            manifest_ok = True
            manifest_union_count = len(union)
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    names = list_profiles()
    profiles = [_status_profile(n) for n in names]
    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "component": "ccp-new",
        "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "read_only": True,
        "profile_dir_present": DIR.is_dir(),
        "source_sha256": _self_sha256(),
        "template": {"present": TEMPLATE.is_file(), "healthy": template_ok,
                     "key_count": template_key_count},
        "manifest": {"present": (DIR / "_MANIFEST.json").is_file(),
                     "healthy": manifest_ok, "union_key_count": manifest_union_count},
        "profile_count": len(names),
        "profiles": profiles,
        "unhealthy_count": sum(1 for p in profiles
                               if not p["structure_ok"] or p["template_complete"] is not True
                               or p["has_credential"] is not True),
    }


def _self_sha256() -> str:
    import hashlib

    try:
        return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()
    except OSError:
        return ""


def cmd_status(as_json: bool) -> int:
    """--status。退出码：0 全健康 / 1 有 profile 不健康或模板/manifest 坏。"""
    status = build_status()
    if as_json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
    else:
        print(f"\nccp-new 状态（只读）  schema={status['schema_version']}")
        print(f"  目录        : {'存在' if status['profile_dir_present'] else '缺失'}")
        print(f"  模板        : {'健康' if status['template']['healthy'] else '不健康'}"
              f"  {status['template']['key_count']} 键")
        print(f"  manifest    : {'健康' if status['manifest']['healthy'] else '不健康'}"
              f"  union {status['manifest']['union_key_count']} 键")
        print(f"  profile 数  : {status['profile_count']}（不健康 {status['unhealthy_count']}）")
        for row in status["profiles"]:
            if not row["structure_ok"]:
                verdict = "坏"
            elif row["template_complete"] is not True:
                verdict = f"缺{row['missing_key_count']}键"
            elif row["has_credential"] is not True:
                verdict = "无凭据"
            else:
                verdict = "OK"
            print(f"    {row['name']:<14} {verdict:<8} {row['key_count']} 键  {row['mode']}")
    healthy = (status["unhealthy_count"] == 0 and status["template"]["healthy"]
               and status["manifest"]["healthy"] and status["profile_dir_present"])
    return EXIT_OK if healthy else EXIT_VERIFY_FAILED


MENU = """
============================================================
ccp-new — profile 管理
============================================================
  新建/删除   [n] 新建      [d] 删除
  查看        [l] 列表      [s] 与模板差异   [v] 全部键值
  修改        [e] 三项改    [f] 改单个键     [b] 整块编辑器
  模板        [t] 改模板（影响【今后】新建，不回溯已有）
              [q] / [qa] 退出
"""


def menu() -> int:
    last = EXIT_OK
    while True:
        names = list_profiles()
        print(MENU, end="")
        print(f"已有 {len(names)} 个: {', '.join(names) if names else '（无）'}")
        choice = ask_soft("\n选择 > ")
        if choice is None:
            return last                      # EOF / Ctrl-C → 安全退出
        choice = choice.strip().lower()
        if not choice or choice in ("q", "qa", "quit", "exit"):
            print("再见。")
            return last
        try:
            if choice in ("n", "new"):
                last = action_new()
            elif choice in ("e", "edit"):
                last = action_edit()
            elif choice in ("d", "del", "delete"):
                last = action_delete()
            elif choice in ("l", "ls", "list"):
                last = action_list()
            elif choice in ("s", "show", "diff"):
                last = action_show()
            elif choice in ("v", "view"):
                last = action_view()
            elif choice in ("f", "field"):
                last = action_field()
            elif choice in ("b", "block"):
                last = action_block()
            elif choice in ("t", "tpl", "template"):
                last = action_template()
            else:
                print(f"✗ 无此选项：{choice}", file=sys.stderr)
        except InputError as exc:
            # 可恢复错误：回菜单，不退出。便于当场改输入重试。
            print(f"✗ {exc}", file=sys.stderr)
            last = EXIT_USAGE


USAGE = """用法:
  ccp-new                          交互菜单（新建/修改/删除/列表）
  ccp-new <名称> <URL> <API-key>   直接新建或覆盖
  ccp-new -y <名称> <URL> <KEY>    覆盖同名不再询问
  ccp-new --status [--json]        只读状态快照（离线、不写盘、不打上游）

菜单动作:
  [n] 新建   [e] 改三项（名称/URL/key）  [d] 删除   [l] 列表
  [s] 看与模板的差异（只列不同的键）
  [v] 看全部键（按 凭据/端点/模型/上下文/代理/遥测/其它 分组，凭据默认掩码）
  [f] 改单个键的值      [b] 整块编辑（内置字段编辑器）
      e 编辑值；Enter 保存并选 y/n；Esc 放弃并选 y/n
  [t] 改模板（影响此后【新建】的 profile，不回溯已有）

说明:
  - 新建只需 名称/URL/API key，其余配置全部继承 _template.json
  - 分级验证：改 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL
    → 必须 GET /v1/models 通过，失败硬拒；改其它键 → 失败仅 advisory 警告仍落盘
    （否则会构造性死锁：要修的正是坏掉的代理键，强制验证必因它坏而失败）
  - 键只能置空串（输入 - ）不能删：空串=中和，键缺失=从 baseline 渗漏
  - 覆盖/修改同名时旧文件存为 <名称>.json.bak（固定名，不带时间戳）
  - 删除移入 _trash/<名称>.json.<UTC时间戳>，不占用 .bak，可原地恢复

退出码: 0 成功 / 1 上游验证失败 / 2 输入错误 / 130 取消
环境变量:
  CCP_PROFILE_DIR            profile 目录（默认 ~/.claude-profiles）
  CCP_VERIFY_TIMEOUT         上游探测超时秒数（默认 30）
  CCP_VERIFY_UA              探测用 User-Agent（默认 claude-cli/...）
  CCP_EDITOR                 显式指定外部整块编辑器；未指定时始终使用内置 Enter/Esc 编辑器
  VISUAL / EDITOR            不影响 ccp-new；避免 shell 默认 Vim 把用户带入另一套退出语法
  CCP_EDIT_REQUIRE_VERIFY=1  让非凭据改动的验证失败也硬拒（默认 advisory）
  CCP_SKIP_VERIFY=1          跳过非凭据改动的验证；【无法】跳过凭据/端点硬验证"""


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return EXIT_OK
    if args and args[0] in ("--ui", "ui"):
        print("网页入口已移除，请直接运行 ccp-new（交互菜单）。", file=sys.stderr)
        return EXIT_USAGE

    # --status 必须在 -y 剥离之前判定：它不是「新建」的一种变体，不接受 -y，
    # 也不接受 NAME/URL/KEY。放在后面会让 `--status extra` 落进三参数分支。
    if args and args[0] == "--status":
        rest = args[1:]
        if rest and rest != ["--json"]:
            print("用法: ccp-new --status [--json]", file=sys.stderr)
            return EXIT_USAGE
        return cmd_status(rest == ["--json"])

    assume_yes = False
    if args and args[0] in ("-y", "--yes"):
        assume_yes = True
        args = args[1:]

    if not args:
        return menu()
    if len(args) != 3:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    return run(args[0], args[1], args[2], assume_yes=assume_yes)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(EXIT_CANCELLED) from None

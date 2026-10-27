# CCC 原生 Codex 启动 → 托管净化代理（V28 候选，显式 opt-in）。
# 只在 CCC_CODEX_MANAGED_PROXY=1 时生效：解析「本次实际选中的」provider 及其 base_url（config.toml +
# profile + CLI -c/-p/--local-provider 覆盖），确保私有端口上的托管代理健康，然后只追加这一个
# provider 的 base_url 覆盖，并把 127.0.0.1/localhost 合并进 NO_PROXY/no_proxy。其余设置（模型、密钥、
# 其他 provider）原样保留。任何无法安全确定的情况返回具名拒绝（ProxyLaunchRefused），不改全局配置。
import json
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

PORT = 18766                      # 新托管端口；18765 属于现役手工代理，永不使用
FORBIDDEN_PORTS = {18765}
OPT_IN = "CCC_CODEX_MANAGED_PROXY"
READ_ONLY_FLAGS = {"--help", "-h", "--version", "-V"}
READ_ONLY_COMMANDS = {"login", "logout", "completion", "mcp", "features", "update", "help"}
PROVIDER_ID = re.compile(r"^[A-Za-z0-9_-]+$")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# 本地 OSS provider 选择器：所选 provider 无可安全覆盖的远端 base_url，具名拒绝而不猜 openai。
LOCAL_PROVIDER_SELECTORS = {"--oss", "--lmstudio", "--local-provider"}


class ProxyLaunchRefused(RuntimeError):
    """具名拒绝：code 是稳定的机器可读名，message 给人看。"""

    def __init__(self, code, detail=""):
        super().__init__("%s %s" % (code, detail) if detail else code)
        self.code = code


def is_read_only(args):
    return any(a in READ_ONLY_FLAGS for a in args) or bool(args and args[0] in READ_ONLY_COMMANDS)


def _toml_value(text):
    try:
        return tomllib.loads("v = " + text)["v"]
    except tomllib.TOMLDecodeError:
        return text  # Codex 同样把无法解析的 -c 值当字符串


def cli_overrides(args):
    """返回 (dotted-key→值 列表, profile)。只认识影响 provider 选择的参数；未知参数原样忽略。"""
    pairs, profile, i = [], None, 0
    while i < len(args):
        a = args[i]
        if a in ("-c", "--config") and i + 1 < len(args):
            raw, i = args[i + 1], i + 2
        elif a.startswith("--config="):
            raw, i = a.split("=", 1)[1], i + 1
        elif a in ("-p", "--profile") and i + 1 < len(args):
            profile, i = args[i + 1], i + 2
            continue
        elif a == "--local-provider" and i + 1 < len(args):
            pairs.append(("model_provider", args[i + 1]))
            i += 2
            continue
        else:
            i += 1
            continue
        if "=" not in raw:
            raise ProxyLaunchRefused("CONFIG_OVERRIDE_UNPARSEABLE", raw.split("=", 1)[0])
        key, value = raw.split("=", 1)
        pairs.append((key.strip(), _toml_value(value.strip())))
    return pairs, profile


def _codex_home(env):
    return Path(env.get("CODEX_HOME") or Path.home() / ".codex")


def resolve_provider(args, env):
    """返回 (provider_id, base_url)。顺序：CLI 覆盖 > profile > 顶层 > 内置 openai。"""
    path = _codex_home(env) / "config.toml"
    try:
        config = tomllib.loads(path.read_text()) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProxyLaunchRefused("CONFIG_UNREADABLE", str(path)) from exc
    pairs, cli_profile = cli_overrides(args)
    overrides = dict(pairs)
    profile = cli_profile or overrides.get("profile") or config.get("profile")
    profile_table = {}
    if profile:
        profile_table = (config.get("profiles") or {}).get(profile)
        if not isinstance(profile_table, dict):
            raise ProxyLaunchRefused("PROFILE_NOT_FOUND", str(profile))
    provider = overrides.get("model_provider") or profile_table.get("model_provider") \
        or config.get("model_provider") or "openai"
    if not isinstance(provider, str) or not PROVIDER_ID.match(provider):
        raise ProxyLaunchRefused("PROVIDER_ID_UNSUPPORTED", repr(provider))
    table = (config.get("model_providers") or {}).get(provider)
    base_url = overrides.get("model_providers.%s.base_url" % provider)
    if base_url is None and isinstance(table, dict):
        base_url = table.get("base_url")
    if table is None and base_url is None:
        # 内置 provider 没有可安全覆盖的 base_url 记录：不猜默认地址。
        raise ProxyLaunchRefused("BUILTIN_PROVIDER_UNSUPPORTED", provider)
    if not isinstance(base_url, str) or not base_url:
        raise ProxyLaunchRefused("BASE_URL_MISSING", provider)
    return provider, base_url


def split_base_url(base_url):
    """https://host[:port]/path → (上游 origin, path)。代理按 UPSTREAM + 请求路径转发。"""
    parts = urlsplit(base_url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ProxyLaunchRefused("BASE_URL_UNSUPPORTED", base_url)
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ProxyLaunchRefused("BASE_URL_UNSUPPORTED", "query/fragment/userinfo")
    if parts.hostname in LOOPBACK:
        raise ProxyLaunchRefused("BASE_URL_ALREADY_LOOPBACK", base_url)
    return "%s://%s" % (parts.scheme, parts.netloc), parts.path.rstrip("/")


def merged_no_proxy(existing):
    items = [x.strip() for x in (existing or "").split(",") if x.strip()]
    for host in ("127.0.0.1", "localhost"):
        if host not in items:
            items.append(host)
    return ",".join(items)


def _override_key(arg, nxt):
    """若 arg 是 -c/--config 覆盖，返回 (key, 占用的 argv 个数)；否则 (None, 1)。"""
    if arg in ("-c", "--config") and nxt is not None:
        return nxt.split("=", 1)[0].strip(), 2
    if arg.startswith("--config="):
        return arg.split("=", 1)[1].split("=", 1)[0].strip(), 1
    return None, 1


def collapse_base_url_overrides(args, provider):
    """删除所选 provider 的原 base_url CLI 覆盖（其值已被 resolve 当作上游）；其余 argv 原样保序。
    能整体改写该 provider 的覆盖（model_providers / model_providers.<id>）无法安全合并 → 具名拒绝。"""
    target = "model_providers.%s.base_url" % provider
    whole = {"model_providers", "model_providers.%s" % provider}
    out, i = [], 0
    while i < len(args):
        key, width = _override_key(args[i], args[i + 1] if i + 1 < len(args) else None)
        if key == target:
            i += width
            continue
        if key in whole or (key and "base_url" in key and key.startswith("model_providers") and key != target
                            and key.replace('"', "").replace("'", "") == target):
            raise ProxyLaunchRefused("CONFIG_OVERRIDE_AMBIGUOUS", key)
        out.extend(args[i:i + width])
        i += width
    return out


def prepare(args, env, service_factory, port=PORT):
    """返回 (新 args, env 增量)。未 opt-in 或只读命令：原样返回、不碰服务。"""
    if env.get(OPT_IN) != "1" or is_read_only(args):
        return list(args), {}
    if port in FORBIDDEN_PORTS:
        raise ProxyLaunchRefused("PORT_FORBIDDEN", str(port))
    selector = next((a.split("=", 1)[0] for a in args if a.split("=", 1)[0] in LOCAL_PROVIDER_SELECTORS), None)
    if selector:
        raise ProxyLaunchRefused("LOCAL_PROVIDER_SELECTOR_UNSUPPORTED", selector)
    provider, base_url = resolve_provider(args, env)
    rest = collapse_base_url_overrides(args, provider)  # 服务 ensure 之前完成全部拒绝判定
    upstream, path = split_base_url(base_url)
    # 外来监听/坏记录/脚本 pin 不符 → ServiceError，向上转成具名拒绝；不停止、不接管任何进程。
    try:
        service_factory(port=port, upstream=upstream).ensure()
    except RuntimeError as exc:
        raise ProxyLaunchRefused("PROXY_SERVICE_REFUSED", str(exc)) from exc
    local = "http://127.0.0.1:%d%s" % (port, path)
    override = "model_providers.%s.base_url=%s" % (provider, json.dumps(local))
    delta = {"NO_PROXY": merged_no_proxy(env.get("NO_PROXY")),
             "no_proxy": merged_no_proxy(env.get("no_proxy"))}
    return ["-c", override, *rest], delta


def default_service_factory(env):
    """真实服务：脚本与 SHA 必须显式 pin（Root 安装时写入），状态目录私有。"""
    script, sha = env.get("CCC_CODEX_PROXY_SCRIPT"), env.get("CCC_CODEX_PROXY_SCRIPT_SHA256")
    state = env.get("CCC_CODEX_PROXY_STATE_DIR")
    if not (script and sha and state):
        raise ProxyLaunchRefused("PROXY_SERVICE_UNPINNED", "script/sha256/state-dir env required")
    from ccc_proxy_service import ProxyService

    def factory(port, upstream):
        return ProxyService(state, script, sha, port, upstream)
    return factory

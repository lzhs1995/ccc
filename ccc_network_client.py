"""Small, nonblocking network-status consumer for CCC's watcher and TUI.

No HTTP, probe subprocess, connection switching, native success event, or
terminal input lives here. Unknown/stale observer state is never an outage.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
import urllib.parse


def validate_options(value):
    if not isinstance(value, dict) or type(value.get("enabled", False)) is not bool:
        raise RuntimeError("network_guard must be an object with boolean enabled")
    if value.get("enabled") and (not isinstance(value.get("config_path"), str)
                                  or not Path(value["config_path"]).is_absolute()):
        raise RuntimeError("enabled network_guard needs an absolute config_path")


def bounded_json(path):
    with Path(path).open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("network status is oversized")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("network status must be an object")
    return value


def error_host(turn):
    error = turn.get("error") or {}
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    hosts = {urllib.parse.urlsplit(url).hostname for url in re.findall(r'https?://[^\s)\]"\'>]+', message)}
    hosts.discard(None)
    return hosts.pop() if len(hosts) == 1 else ""


def _merge(base, change):
    for key, value in change.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
    base.update({k: v for k, v in change.items() if not (isinstance(v, dict) and isinstance(base.get(k), dict))})
    return base


def configured_host(turn, target):
    """Resolve only a verified native process's unchanged startup config.

    Names such as `custom` are not service bindings. Respect its CODEX_HOME,
    profile and -c overrides, and refuse a config edited after process start.
    The native failed-turn URL, when present, is stronger evidence.
    """
    try:
        import tomllib
    except ImportError:
        return ""  # Python 3.10 still binds using the native failure URL.
    from ccc_guard_scope import arguments, birth
    pid = turn.get("pid")
    before = birth(pid, codex=True)
    if before is None or before[0] != int(turn.get("process_start") or 0):
        return ""
    argv, env = arguments(pid)
    if (str(env.get("CMUX_SURFACE_ID", "")).upper() != str(target["surface_id"]).upper()
            or str(env.get("CMUX_WORKSPACE_ID", "")).upper() != str(target["workspace_id"]).upper()
            or "--remote" in argv or "--oss" in argv):
        return ""
    codex_dir = Path(env.get("CODEX_HOME") or Path(env.get("HOME") or Path.home()) / ".codex")
    path = codex_dir / "config.toml"
    if not path.is_absolute() or path.stat().st_mtime > before[0] + 1:
        return ""
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        return ""
    config = tomllib.loads(raw.decode())
    overrides, profile = {}, ""
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in {"-c", "--config", "-p", "--profile"}:
            if index + 1 >= len(argv):
                return ""
            index += 1
            if arg in {"-p", "--profile"}:
                profile = argv[index]
            else:
                _merge(overrides, tomllib.loads(argv[index]))
        elif arg.startswith("--config="):
            _merge(overrides, tomllib.loads(arg.split("=", 1)[1]))
        elif arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]
        elif arg.startswith("-c") and len(arg) > 2:
            _merge(overrides, tomllib.loads(arg[2:]))
        index += 1
    selected_profile = profile or config.get("profile", "")
    if selected_profile:
        profiles = config.get("profiles", {})
        if selected_profile not in profiles:
            return ""
        _merge(config, copy.deepcopy(profiles[selected_profile]))
    _merge(config, overrides)
    provider = turn.get("model_provider") or config.get("model_provider") or "openai"
    configured_provider = config.get("model_provider") or "openai"
    if provider != configured_provider:
        return ""
    entry = config.get("model_providers", {}).get(provider, {})
    url = entry.get("base_url") or (env.get("OPENAI_BASE_URL") if provider == "openai" else "")
    if not url or birth(pid, codex=True) != before:
        return ""
    return urllib.parse.urlsplit(url).hostname or ""


class NetworkClient:
    def __init__(self):
        self.lock = threading.RLock()
        self.cache = (0, "", {})
        self.hinted_at = {}

    def snapshot(self, options, *, now=None):
        now = time.time() if now is None else now
        if not options.get("enabled"):
            return {"phase": "disabled", "at": now}
        path = str(options.get("config_path") or "")
        with self.lock:
            if path != self.cache[1] or not 0 <= now - self.cache[0] < .5:
                try:
                    config = bounded_json(path)
                    value = bounded_json(Path(config["state_dir"]) / "status.json")
                    if value.get("version") != 1 or value.get("service_host") != config.get("service_host"):
                        raise ValueError("network status identity does not match")
                except (OSError, ValueError, KeyError, TypeError):
                    value = {"phase": "observer_fault", "at": now}
                self.cache = now, path, value
            value = dict(self.cache[2])
        at = value.get("at", 0)
        if not isinstance(at, (int, float)) or not 0 <= now - at <= 10:
            value["phase"] = "observer_stale"
        return value

    def hint(self, snapshot, *, surface_id="", route_id="", now=None):
        now = time.time() if now is None else now
        path = snapshot.get("hint_socket", "")
        if not isinstance(path, str) or not path.startswith("/tmp/ccc-network-"):
            return False
        key = (path, surface_id, route_id)
        with self.lock:
            if 0 <= now - self.hinted_at.get(key, 0) < 2:
                return False
            self.hinted_at[key] = now
            if len(self.hinted_at) > 1024:
                self.hinted_at = {k: at for k, at in self.hinted_at.items() if now - at < 60}
        payload = json.dumps({"service_host": snapshot.get("service_host"), "surface_id": surface_id,
                              "route_id": route_id}).encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                sock.sendto(payload, path)
            return True
        except OSError:
            return False

    def verdict(self, options, target, turn, *, now=None):
        if not options.get("enabled") or not isinstance(turn, dict):
            return {"blocked": False, "phase": "disabled", "service_host": ""}
        snapshot = self.snapshot(options, now=now)
        expected = snapshot.get("service_host")
        if not expected:
            return {"blocked": False, "phase": snapshot["phase"], "service_host": ""}
        try:
            host = error_host(turn) or configured_host(turn, target)
        except (OSError, ValueError, TypeError, RuntimeError):
            host = ""
        if host != expected:
            return {"blocked": False, "phase": "unbound", "service_host": host}
        self.hint(snapshot, surface_id=str(target["surface_id"]), now=now)
        return {"blocked": snapshot.get("mode") == "manage" and snapshot["phase"] == "network_wait",
                "phase": snapshot["phase"], "service_host": host}


def summary(snapshot):
    phase = snapshot.get("phase")
    if phase == "disabled":
        return ""
    labels = {"healthy": "可用", "fallback": "东京兜底", "network_wait": "等待网络恢复",
              "suspect": "正在复核", "checking": "正在探测", "observe": "观察模式",
              "api_attention": "API 限流或配置异常", "observer_fault": "探测器异常",
              "observer_stale": "探测器心跳过期", "unmanaged_selection": "策略组待接入", "stopped": "已停止"}
    return f"AnyRouter {labels.get(phase, '启动中')} · {snapshot.get('active_pool', '—')} · 可用 {snapshot.get('ready', 0)}/{len(snapshot.get('routes', []))} · 隔离 {snapshot.get('quarantined', 0)}"


def command(options, action, route=""):
    validate_options(options)
    client = NetworkClient()
    if action == "install":
        if not options.get("enabled"):
            raise RuntimeError("network_guard is not enabled in this CCC config")
        from ccc_network_guard import install
        return install(options["config_path"])
    snapshot = client.snapshot(options)
    if action == "probe":
        matches = [r["id"] for r in snapshot.get("routes", []) if route in {r["id"], r["label"], r["name"]}]
        if route and len(matches) != 1:
            raise RuntimeError("route must identify one network candidate")
        if not client.hint(snapshot, route_id=matches[0] if matches else ""):
            raise RuntimeError("network guard did not accept the recheck hint")
        return {"queued": True, "route_id": matches[0] if matches else "current", "rate_limit_unchanged": True}
    return snapshot

"""One optional, service-aware network guard, independent of CCC's B guardian.

Only this process probes and changes one configured Mihomo selector. It cannot
send terminal input, emit Codex lifecycle events, reload Clash, or close users'
connections. The watcher consumes status.json and sends best-effort hints.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import fcntl
import hashlib
import http.server
import json
import math
import os
import plistlib
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from ccc_mihomo import Controller, ProbeResult, ResponsesProbe, Route, ShadowCore, atomic_json, inventory, validate_dependencies


DEFAULTS = {
    "current_interval_sec": 2, "standby_interval_sec": 5, "other_interval_sec": 60,
    "light_fresh_sec": 20, "cooldown_sec": 60, "recover_successes": 3,
    "failures_before_isolation": 2, "concurrency": 4,
    "deep_min_interval_sec": 30, "deep_per_minute": 2,
    "current_deep_sec": 120, "standby_deep_sec": 300, "other_deep_sec": 3600,
    "qualification_ttl_sec": 7200, "inventory_interval_sec": 60,
}
LOCAL_FAILURES = {"blocked", "timeout", "transport", "truncated"}
API_ATTENTION = {"auth", "permission", "rate_limit", "upstream", "contract"}


def read_json(path, default=None):
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("network document exceeds size limit")
        return json.loads(raw)
    except FileNotFoundError:
        return default


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("network config version must be 1 (no implicit activation)")
    if config.get("mode") not in {"observe", "manage"}:
        raise ValueError("network mode must be observe or manage")
    for key in ("state_dir", "binary", "controller_socket"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise ValueError(f"network {key} must be an absolute path")
    if not config.get("interface") or str(config["interface"]).startswith(("utun", "tun")):
        raise ValueError("isolated probes require an explicit physical interface")
    for key in ("group", "provider", "offline_proxy", "service_host"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f"network {key} is required")
    if config["offline_proxy"] == "DIRECT":
        raise ValueError("DIRECT cannot stand in for a failed service route")
    pools = config.get("commercial_pools")
    if not isinstance(pools, list) or not pools or len(set(pools)) != len(pools):
        raise ValueError("commercial_pools must be a nonempty ordered list")
    if not isinstance(config.get("sources"), list) or not config["sources"]:
        raise ValueError("at least one local subscription source is required")
    for source in config["sources"]:
        if not isinstance(source, dict) or source.get("pool") not in pools or not Path(source.get("path", "")).is_absolute():
            raise ValueError("each subscription needs an absolute path and a commercial pool")
    probe = config.get("probe") or {}
    endpoint = urllib.parse.urlsplit(probe.get("url", ""))
    if endpoint.scheme != "https" or endpoint.hostname != config["service_host"] or not probe.get("model"):
        raise ValueError("probe must use HTTPS on the configured service host and an explicit model")
    for key, default, low, high in (("timeout_sec", 5, .1, 10), ("deep_timeout_sec", 45, 1, 90)):
        number = probe.get(key, default)
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not low <= number <= high:
            raise ValueError(f"probe {key} must be between {low} and {high}")
    by_pool = probe.get("light_timeout_by_pool", {})
    allowed_pools = {*pools, (config.get("fallback") or {}).get("pool")}
    if (not isinstance(by_pool, dict) or any(pool not in allowed_pools or isinstance(value, bool)
            or not isinstance(value, (int, float)) or not .1 <= value <= 10 for pool, value in by_pool.items())):
        raise ValueError("light_timeout_by_pool needs known pool names and deadlines between .1 and 10 seconds")
    publishing = config.get("publish") or {}
    if type(config.get("publish_transit", False)) is not bool:
        raise ValueError("publish_transit must be a boolean")
    token = publishing.get("token", "")
    port = publishing.get("port", 0)
    if (not isinstance(token, str) or len(token) < 24 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in token)
            or isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535):
        raise ValueError("publish needs a loopback port and a random URL-safe token of at least 24 characters")
    policy = {**DEFAULTS, **config.get("policy", {})}
    for key, value in policy.items():
        if key not in DEFAULTS or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"invalid network policy {key}")
    for key in ("concurrency", "recover_successes", "failures_before_isolation", "deep_per_minute"):
        if not isinstance(policy[key], int):
            raise ValueError(f"network {key} must be an integer")
    if (policy["concurrency"] > 4 or policy["deep_per_minute"] > 2 or policy["deep_min_interval_sec"] < 30
            or policy["cooldown_sec"] < 60 or policy["recover_successes"] < 3):
        raise ValueError("probe concurrency/rate or isolation recovery exceeds the supported safety limits")
    config["policy"] = policy
    return config


def contract_digest(config, *, legacy=False):
    probe = dict(config["probe"])
    if not legacy:
        for key in ("timeout_sec", "deep_timeout_sec", "light_timeout_by_pool"):
            probe.pop(key, None)  # Scheduling/deadline changes do not change API identity.
    if probe.get("auth_file"):
        try:
            probe["credential_digest"] = hashlib.sha256(Path(probe["auth_file"]).read_bytes()).hexdigest()
        except OSError:
            probe["credential_digest"] = "unreadable"
    if probe.get("auth_env"):
        probe["credential_digest"] = hashlib.sha256(os.environ.get(probe["auth_env"], "").encode()).hexdigest()
    return hashlib.sha256(json.dumps(probe, sort_keys=True).encode()).hexdigest()


def private_socket_dir(config):
    identity = config["controller_socket"] + "\0" + config["group"]
    digest = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return Path("/tmp") / f"ccc-network-{os.getuid()}-{digest}"


@contextlib.contextmanager
def singleton(config):
    # Lock the controller/group, not just the config path: two different
    # config files must never become competing writers for one selector.
    root = private_socket_dir(config)
    root.mkdir(mode=0o700, exist_ok=True)
    st = root.lstat()
    if root.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError("network socket directory is not private")
    with (root / "guard.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another network guard already owns this selector") from exc
        yield root


@dataclasses.dataclass
class Health:
    qualified: bool = False
    quarantined: bool = False
    quarantine_until: float = 0
    failures: int = 0
    light_failures: int = 0
    deep_failures: int = 0
    failure_at: float = 0
    successes: int = 0
    light_at: float = 0
    light_attempt_at: float = 0
    light_ok_at: float = 0
    light_kind: str = "unknown"
    deep_at: float = 0
    deep_ok_at: float = 0
    deep_kind: str = "unknown"
    deep_attempt_at: float = 0
    elapsed_ms: float = 0
    status: int = 0


def recent_deep_starts(starts, now, minimum_interval=60):
    # Clock correction does not refund an already consumed request. Keep
    # future reservations until their window expires on the corrected clock,
    # and use the latest timestamp for the minimum spacing after a restart.
    window = max(60, minimum_interval)
    return sorted(x for x in starts if type(x) in (int, float)
                  and math.isfinite(x) and x >= 0 and now - x < window)


class Engine:
    """Pure route policy. Probe results never constitute terminal-send grants."""
    def __init__(self, config, routes, saved=None, now=None):
        self.config = config
        self.policy = {**DEFAULTS, **config.get("policy", {})}
        self.routes = {r.id: r for r in routes}
        self.active = set(self.routes)
        self.health = {r: Health() for r in self.routes}
        self.current = ""
        self.active_pool = config["commercial_pools"][0]
        self.deep_starts = []
        self.last_switch = 0
        self.hint_at = 0
        self.hint_route = ""
        self.seed_consumed = False
        now = time.time() if now is None else now
        if saved:
            allowed = {f.name for f in dataclasses.fields(Health)}
            for rid, row in saved.get("health", {}).items():
                if rid not in self.routes or not isinstance(row, dict):
                    continue
                try:
                    health = Health(**{k: v for k, v in row.items() if k in allowed})
                    stamps = [health.light_at, health.light_attempt_at, health.light_ok_at, health.deep_at,
                              health.deep_ok_at, health.deep_attempt_at, health.failure_at]
                    if any(not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0 or t > now + 1 for t in stamps):
                        continue
                    if (any(type(x) is not bool for x in (health.qualified, health.quarantined))
                            or any(type(x) is not int or x < 0 for x in (health.failures, health.successes,
                                                                       health.light_failures, health.deep_failures))
                            or not isinstance(health.quarantine_until, (int, float)) or not math.isfinite(health.quarantine_until)):
                        continue
                    if now - health.deep_ok_at > self.policy["qualification_ttl_sec"]:
                        health.qualified = False
                    if "light_failures" not in row:
                        health.light_failures = health.failures
                    self.health[rid] = health
                except (TypeError, ValueError):
                    continue
            self.current = saved.get("current", "") if saved.get("current") in self.routes else ""
            self.active_pool = saved.get("active_pool", self.active_pool)
            self.deep_starts = recent_deep_starts(saved.get("deep_starts", []), now,
                                                  self.policy["deep_min_interval_sec"])
            self.seed_consumed = bool(saved.get("seed_consumed"))

    def update_inventory(self, routes):
        self.active = {r.id for r in routes}
        for route in routes:
            self.routes[route.id] = route
            self.health.setdefault(route.id, Health())
        # A disappeared subscription entry cannot delete the active, already
        # verified connection path. Retire it after a replacement is selected.
        for rid in set(self.routes) - self.active - {self.current}:
            del self.routes[rid]
            self.health.pop(rid, None)

    def record(self, rid, result, now, *, started_at=None):
        if rid not in self.health:
            return
        h = self.health[rid]
        started_at = now if started_at is None else started_at
        # A slow SSE from before a newer failure is not recovery evidence.
        # Light and generation results are independent: a JSON validator can
        # remain reachable while every generation stream is truncated.
        if result.deep and result.kind == "healthy" and started_at < h.failure_at:
            return
        h.elapsed_ms, h.status = result.elapsed_ms, result.status
        if result.deep:
            h.deep_at, h.deep_kind = now, result.kind
        else:
            h.light_at, h.light_kind = now, result.kind
        if result.kind == "accessible" and not result.deep:
            h.light_ok_at = now
            h.light_failures = 0
            h.successes = h.successes + 1 if now >= h.quarantine_until else 0
        elif result.kind == "healthy" and result.deep:
            # Recovery requires NEW light successes after cooldown, followed
            # by this real complete SSE. A timer alone cannot clear quarantine.
            needed = self.policy["recover_successes"] if h.quarantined else 1
            fresh = (0 <= now - h.light_ok_at <= self.policy["light_fresh_sec"]
                     or 0 <= started_at - h.light_ok_at <= self.policy["light_fresh_sec"])
            if (started_at >= h.quarantine_until and h.successes >= needed and fresh):
                h.deep_ok_at = now
                h.qualified, h.quarantined, h.deep_failures = True, False, 0
        elif result.kind in LOCAL_FAILURES:
            h.successes = 0
            h.failure_at = now
            if result.deep:
                h.deep_failures += 1
            else:
                h.light_failures += 1
            h.failures = h.light_failures + h.deep_failures
            if result.kind == "blocked" or h.failures >= self.policy["failures_before_isolation"]:
                h.qualified = False
                h.quarantined = True
                h.quarantine_until = now + self.policy["cooldown_sec"]
        # Auth, quota, model and upstream errors do not quarantine IPs.
        h.failures = h.light_failures + h.deep_failures

    def seed(self, rows, now):
        if self.seed_consumed:
            return
        for rid, row in rows.items():
            if (rid not in self.routes or row.get("kind") != "healthy" or row.get("deep") is not True
                    or not isinstance(row.get("at"), (int, float)) or not 0 <= now - row["at"] < 900):
                continue
            # Offline acceptance imports one real result per exact full-chain
            # identity. A later restart cannot re-import it and erase isolation.
            h = self.health[rid]
            h.deep_ok_at, h.deep_at, h.deep_kind = row["at"], row["at"], "healthy"
            h.qualified, h.quarantined = True, False
        self.seed_consumed = True

    def qualified(self, rid, now):
        h = self.health[rid]
        return h.qualified and not h.quarantined and 0 <= now - h.deep_ok_at <= self.policy["qualification_ttl_sec"]

    def ready(self, rid, now):
        h = self.health[rid]
        return (self.qualified(rid, now) and h.failures == 0
                and 0 <= now - max(h.light_ok_at, h.deep_ok_at) <= self.policy["light_fresh_sec"])

    def ranked(self, ids):
        return sorted(ids, key=lambda rid: (self.routes[rid].priority, self.routes[rid].name))

    def standbys(self, now):
        chosen = []
        pools = [self.active_pool, *self.config["commercial_pools"]]
        pools += [r.pool for r in self.routes.values() if r.pool not in pools]
        for pool in dict.fromkeys(pools):
            ids = [rid for rid, r in self.routes.items() if r.pool == pool and rid != self.current]
            ids = sorted(ids, key=lambda rid: (not self.ready(rid, now), not self.qualified(rid, now),
                                               self.routes[rid].priority))
            chosen.extend(ids[:2 if pool not in self.config["commercial_pools"] else 1])
        return list(dict.fromkeys(chosen))

    def decision(self, now):
        ready = [rid for rid in self.routes if self.ready(rid, now)]
        commercial = [rid for rid in ready if self.routes[rid].pool in self.config["commercial_pools"]]
        active = self.health.get(self.current)
        if (active and self.qualified(self.current, now) and active.deep_kind in API_ATTENTION
                and active.deep_at >= active.deep_ok_at):
            return self.current, "api_attention"
        if self.current in ready and self.routes[self.current].pool in self.config["commercial_pools"]:
            return self.current, "healthy"
        h = self.health.get(self.current)
        if h and self.qualified(self.current, now) and h.failures:
            return self.current, "suspect"
        if h and self.qualified(self.current, now) and h.light_kind in API_ATTENTION:
            return self.current, "api_attention"
        # Keep a healthy commercial pool; try another node in it before moving
        # airports. Recovery of the other airport does not cause preemption.
        for pool in dict.fromkeys([self.active_pool, *self.config["commercial_pools"]]):
            available = self.ranked([rid for rid in commercial if self.routes[rid].pool == pool])
            if available:
                return available[0], "healthy"
        fallback = self.ranked([rid for rid in ready if rid not in commercial])
        if fallback:
            return fallback[0], "fallback"
        h = self.health.get(self.current)
        if h and h.quarantined:
            return "", "network_wait"
        if h and (h.light_kind in API_ATTENTION or h.deep_kind in API_ATTENTION):
            return self.current, "api_attention"
        return self.current, "checking"

    def light_due(self, now, in_flight):
        hot = set(self.standbys(now))
        def order(rid):
            interval = (self.policy["current_interval_sec"] if rid == self.current else
                        self.policy["standby_interval_sec"] if rid in hot else self.policy["other_interval_sec"])
            h = self.health[rid]
            due = (h.light_attempt_at or h.light_at) + interval
            if rid == self.current and self.hint_at > h.light_at:
                due = 0
            return (0 if rid == self.current else 1 if rid in hot else 2, due)
        return sorted((rid for rid in self.routes if rid not in in_flight and order(rid)[1] <= now), key=order)

    def deep_due(self, now, in_flight):
        self.deep_starts = recent_deep_starts(self.deep_starts, now, self.policy["deep_min_interval_sec"])
        if (sum(now - stamp < 60 for stamp in self.deep_starts) >= self.policy["deep_per_minute"]
                or self.deep_starts and now - self.deep_starts[-1] < self.policy["deep_min_interval_sec"]):
            return None
        hot = set(self.standbys(now))
        candidates = []
        for rid, h in self.health.items():
            if (rid in in_flight or now - h.light_ok_at > self.policy["light_fresh_sec"] or h.light_failures
                    or h.quarantined and (now < h.quarantine_until or h.successes < self.policy["recover_successes"])
                    or now - h.deep_attempt_at < self.policy["deep_min_interval_sec"]):
                continue
            period = (self.policy["current_deep_sec"] if rid == self.current else
                      self.policy["standby_deep_sec"] if rid in hot else self.policy["other_deep_sec"])
            urgent = self.hint_at > h.deep_attempt_at and rid == (self.hint_route or self.current)
            if self.qualified(rid, now) and now - h.deep_ok_at < period and not urgent and not h.deep_failures:
                continue
            pool_missing = not any(self.ready(other, now) and self.routes[other].pool == self.routes[rid].pool
                                   for other in self.routes)
            candidates.append((0 if urgent or rid == self.current else 1 if pool_missing else 2 if rid in hot else 3,
                               h.deep_attempt_at, self.routes[rid].priority, rid))
        return min(candidates)[-1] if candidates else None

    def reserve_deep(self, rid, now):
        self.deep_starts.append(now)
        self.health[rid].deep_attempt_at = now

    def saved(self):
        return {"health": {rid: dataclasses.asdict(h) for rid, h in self.health.items()},
                "current": self.current, "active_pool": self.active_pool,
                "deep_starts": self.deep_starts, "seed_consumed": self.seed_consumed}


class ProviderServer:
    def __init__(self, port, token, offline_proxy="AR/Offline"):
        # A restart must not briefly replace the cached live provider with
        # Offline before Director has read the actual selection. Failed HTTP
        # refreshes preserve Mihomo's current definitions until reconciliation.
        self.payload = None
        self.lock = threading.Lock()
        self.offline_proxy = offline_proxy
        self.catalog = {}
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                with owner.lock:
                    payload = (owner.payload if self.path == f"/{token}/proxies" else
                               owner.catalog.get(self.path.removeprefix(f"/{token}/transit/"))
                               if self.path.startswith(f"/{token}/transit/") else None)
                if payload is None and self.path == f"/{token}/proxies":
                    self.send_error(503, "provider reconciliation pending")
                    return
                if payload is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *_):
                pass  # Provider URLs contain a local bearer secret.
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="network-provider")
        self.thread.start()

    def set(self, routes):
        # Mihomo rejects a provider containing zero proxies. The final entry
        # is always an explicit reject. Put live routes FIRST so the initial
        # migration can replace an obsolete cached selection without a gap.
        proxies = {}
        for item in routes:
            for proxy in item.proxies:
                if proxy["name"] in proxies and proxies[proxy["name"]] != proxy:
                    raise ValueError("conflicting provider dependency")
                proxies[proxy["name"]] = proxy
        if self.offline_proxy in proxies:
            raise ValueError("offline proxy name conflicts with a route")
        proxies[self.offline_proxy] = {"name": self.offline_proxy, "type": "reject"}
        payload = (json.dumps({"proxies": list(proxies.values())}, ensure_ascii=False) + "\n").encode()
        with self.lock:
            changed = payload != self.payload
            self.payload = payload
        return changed

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def set_catalog(self, routes, pools):
        # Service-mode Verge copies local file providers into a private runtime.
        # Optional loopback catalogs keep GENERAL transit subscriptions current
        # without asking the privileged service to copy files or restart. These
        # endpoints are never used by the service-admitted AnyRouter selector.
        catalogs = {}
        for pool in pools:
            proxies = [{**r.proxies[-1], "name": r.label} for r in routes
                       if r.pool == pool and len(r.proxies) == 1]
            if not proxies:
                proxies = [{"name": "Transit-Unavailable", "type": "reject"}]
            catalogs[urllib.parse.quote(pool, safe="")] = (json.dumps({"proxies": proxies}, ensure_ascii=False) + "\n").encode()
        with self.lock:
            self.catalog = catalogs


class Director:
    """Publish -> refresh -> select -> read back. Never reload or DELETE."""
    def __init__(self, config, engine, controller, publisher):
        self.config, self.engine, self.controller, self.publisher = config, engine, controller, publisher
        self.published = set()
        self.pending_refresh = False
        self.actual_name = ""

    def sync(self, now):
        e, c = self.engine, self.config
        group = self.controller.get("/proxies/" + urllib.parse.quote(c["group"], safe=""))
        actual = group.get("now", "")
        ids = {r.name: rid for rid, r in e.routes.items()}
        known = ids.get(actual) or c.get("bootstrap_aliases", {}).get(actual)
        if known in e.routes:
            e.current = known
            e.active_pool = e.routes[known].pool
        elif actual == c["offline_proxy"]:
            e.current = ""
        self.actual_name = actual
        target, phase = e.decision(now)
        if not target and actual == c["offline_proxy"]:
            phase = "network_wait"
        approved = {rid for rid in e.routes if e.qualified(rid, now)}
        # Do not remove the old selection until the replacement is committed.
        if known in e.routes:
            approved.add(known)
        ordered = sorted(e.ranked(approved), key=lambda rid: rid != e.current)
        if self.publisher.set([e.routes[rid] for rid in ordered]):
            self.pending_refresh = True
        if c["mode"] != "manage":
            return "observe", target
        if actual not in ids and actual not in c.get("bootstrap_aliases", {}) and actual != c["offline_proxy"]:
            return "unmanaged_selection", target
        if self.pending_refresh:
            self.controller.refresh(c["provider"])
            self.published, self.pending_refresh = approved, False
            group = self.controller.get("/proxies/" + urllib.parse.quote(c["group"], safe=""))
        desired = e.routes[target].name if target else c["offline_proxy"] if phase == "network_wait" else actual
        if desired != actual:
            if desired not in group.get("all", []):
                raise RuntimeError("verified route is not present in the live selector")
            self.controller.select(c["group"], desired)
            observed = self.controller.get("/proxies/" + urllib.parse.quote(c["group"], safe=""))
            if observed.get("now") != desired:
                raise RuntimeError("Mihomo selection readback did not match")
            self.actual_name = desired
            e.current = target
            e.last_switch = now
            if target:
                e.active_pool = e.routes[target].pool
        # Pruning on the next tick preserves the selection transaction and
        # allows in-flight connections to retain their existing proxy objects.
        return phase, target


class Guard:
    def __init__(self, config_path):
        self.path = Path(config_path)
        self.config = load_config(self.path)
        self.root = Path(self.config["state_dir"])
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.stop = threading.Event()
        self.engine = None
        self.core = None
        self.director = None
        self.jobs = {}
        self.retired = []
        self.shadow_error = ""
        self.error = ""
        self.phase = "starting"
        self.hint_count = 0

    def save(self):
        atomic_json(self.root / "health.json", {**self.engine.saved(), "contract": self.contract,
            "routes": [r.record() for r in self.engine.routes.values()]})

    def snapshot(self, now):
        e = self.engine
        rows = [{"id": rid, "name": r.name, "label": r.label, "pool": r.pool,
                 **dataclasses.asdict(e.health[rid]), "qualified": e.qualified(rid, now),
                 "ready": e.ready(rid, now)} for rid, r in e.routes.items()]
        return {"version": 1, "pid": os.getpid(), "at": now, "mode": self.config["mode"],
                "service_host": self.config["service_host"], "phase": self.phase,
                "current_id": e.current, "current": self.director.actual_name,
                "active_pool": e.active_pool, "last_switch": e.last_switch,
                "error": self.error or self.shadow_error, "routes": rows,
                "ready": sum(r["ready"] for r in rows), "qualified": sum(r["qualified"] for r in rows),
                "quarantined": sum(r["quarantined"] for r in rows),
                "probe_in_flight": len(self.jobs),
                "deep_in_last_minute": sum(now - stamp < 60 for stamp in e.deep_starts),
                "hint_count": self.hint_count, "hint_socket": str(private_socket_dir(self.config) / "hint.sock"),
                "shadow_pid": self.core.process.pid if self.core and self.core.process else None}

    def new_shadow(self, routes):
        core = ShadowCore(self.root / "shadows" / uuid.uuid4().hex, self.config["binary"], self.config["interface"])
        try:
            core.configure(routes)
            return core, routes
        except Exception:
            core.close()
            raise

    def consume_hints(self, sock, now):
        for _ in range(128):
            try:
                raw = sock.recv(2048)
                value = json.loads(raw)
            except BlockingIOError:
                break
            except (OSError, ValueError):
                continue
            if not isinstance(value, dict) or value.get("service_host") != self.config["service_host"]:
                continue
            self.hint_count += 1
            # Fifty errors in a workspace are one recheck, not fifty requests.
            if now - self.engine.hint_at >= 2:
                self.engine.hint_at = now
                self.engine.hint_route = value.get("route_id", "") if value.get("route_id") in self.engine.routes else ""

    def run(self):
        with singleton(self.config) as sockets:
            return self._run(sockets)

    def _run(self, sockets):
        os.umask(0o077)
        self.contract = contract_digest(self.config)
        saved = read_json(self.root / "health.json", {})
        if saved.get("contract") == contract_digest(self.config, legacy=True):
            saved["contract"] = self.contract
        try:
            routes = inventory(self.config)
        except (OSError, ValueError, KeyError, RuntimeError):
            # A transient subscription write cannot strand a restarted guard.
            # Only exact saved definitions under the same API contract qualify.
            if saved.get("contract") != self.contract:
                raise
            routes = [Route.restore(r) for r in saved.get("routes", [])]
            if not routes:
                raise
            validate_dependencies(self.config, routes)
            self.shadow_error = "subscription unavailable; retaining the last saved inventory"
        if saved.get("contract") == self.contract:
            for old in saved.get("routes", []):
                if old.get("id") == saved.get("current") and not any(r.id == old["id"] for r in routes):
                    routes.append(Route.restore(old))
        else:
            # Preserve rate reservations even if credentials/probe contract
            # changed, as well as isolation history. New credentials require
            # new admission; they cannot erase a route's quarantine.
            for h in saved.get("health", {}).values():
                h["qualified"] = False
                h["deep_ok_at"] = 0
                h["deep_attempt_at"] = 0
        self.engine = Engine(self.config, routes, saved)
        if self.config.get("seed_file") and not self.engine.seed_consumed:
            self.engine.seed(read_json(self.config["seed_file"], {}), time.time())
        publisher = ProviderServer(self.config["publish"]["port"], self.config["publish"]["token"], self.config["offline_proxy"])
        if self.config.get("publish_transit"):
            publisher.set_catalog(routes, self.config["commercial_pools"])
        control = Controller(self.config["controller_socket"], self.config.get("controller_secret", ""), timeout=2)
        self.director = Director(self.config, self.engine, control, publisher)
        hint_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        hint_path = sockets / "hint.sock"
        with contextlib.suppress(FileNotFoundError):
            hint_path.unlink()
        hint_socket.bind(str(hint_path))
        hint_socket.setblocking(False)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.engine.policy["concurrency"], thread_name_prefix="network-probe")
        setup = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="network-inventory")
        pending_core = setup.submit(self.new_shadow, routes)
        next_core_retry, retry_delay = 0, 5
        next_inventory, next_config, next_sync, next_save, next_snapshot = 0, 0, 0, 0, 0
        try:
            self.save()
            while not self.stop.wait(.1):
                now = time.time()
                self.consume_hints(hint_socket, now)
                for job, (rid, deep, owner, began, contract) in list(self.jobs.items()):
                    if job.done():
                        del self.jobs[job]
                        try:
                            result = job.result()
                        except Exception:
                            result = ProbeResult("observer_error", detail="probe worker failed", deep=deep)
                        if contract == self.contract and owner.process.poll() is None:
                            self.engine.record(rid, result, now, started_at=began)
                for old in list(self.retired):
                    if not any(owner is old for _, _, owner, _, _ in self.jobs.values()):
                        old.close()
                        self.retired.remove(old)
                if pending_core and pending_core.done():
                    try:
                        next_core, routes = pending_core.result()
                        if self.core:
                            self.retired.append(self.core)
                        self.core = next_core
                        self.engine.update_inventory(routes)
                        if self.config.get("publish_transit"):
                            publisher.set_catalog(routes, self.config["commercial_pools"])
                        self.shadow_error = ""
                        retry_delay = 5
                    except Exception as exc:
                        self.shadow_error = f"isolated probe core unavailable: {type(exc).__name__}"
                        next_core_retry = now + retry_delay
                        retry_delay = min(60, retry_delay * 2)
                    pending_core = None
                    next_inventory = now + self.engine.policy["inventory_interval_sec"]
                if now >= next_config:
                    try:
                        updated = load_config(self.path)
                        immutable = ("controller_socket", "group", "provider", "state_dir", "binary", "interface", "publish")
                        if any(updated.get(k) != self.config.get(k) for k in immutable):
                            raise ValueError("network transport changes require a network-guard restart")
                        if contract_digest(updated) != self.contract:
                            self.contract = contract_digest(updated)
                            for h in self.engine.health.values():
                                h.qualified = False
                                h.deep_attempt_at = 0
                            next_inventory = 0
                        self.config = updated
                        self.engine.config = updated
                        self.engine.policy = updated["policy"]
                        self.director.config = updated
                        self.error = ""
                    except Exception as exc:
                        self.error = f"config refresh failed: {type(exc).__name__}"
                    next_config = now + 2
                if self.core is None or self.core.process.poll() is not None:
                    self.shadow_error = "isolated probe core exited; restarting only its replacement"
                    if pending_core is None and now >= next_core_retry:
                        pending_core = setup.submit(self.new_shadow, list(self.engine.routes.values()))
                if pending_core is None and now >= next_inventory:
                    try:
                        latest = inventory(self.config)
                        wanted = {r.id for r in latest}
                        if self.engine.current in self.engine.routes and self.engine.current not in wanted:
                            latest.append(self.engine.routes[self.engine.current])
                            wanted.add(self.engine.current)
                        if wanted != set(self.engine.routes):
                            pending_core = setup.submit(self.new_shadow, latest)
                        elif self.config.get("publish_transit"):
                            publisher.set_catalog(latest, self.config["commercial_pools"])
                    except Exception as exc:
                        self.shadow_error = f"subscription refresh failed; retaining verified routes: {type(exc).__name__}"
                    next_inventory = now + self.engine.policy["inventory_interval_sec"]
                if self.core and self.core.process.poll() is None:
                    occupied = {rid for rid, deep, _, _, _ in self.jobs.values() if not deep}
                    probe = ResponsesProbe(self.config["probe"], self.core.ports)
                    if len(self.jobs) < self.engine.policy["concurrency"] and not any(deep for _, deep, _, _, _ in self.jobs.values()):
                        rid = self.engine.deep_due(now, occupied)
                        if rid:
                            self.engine.reserve_deep(rid, now)
                            self.save()  # Reserve before a billable request can run.
                            job = executor.submit(probe.run, self.engine.routes[rid], True)
                            self.jobs[job] = (rid, True, self.core, now, self.contract)
                    for rid in self.engine.light_due(now, occupied):
                        if len(self.jobs) >= self.engine.policy["concurrency"]:
                            break
                        self.engine.health[rid].light_attempt_at = now
                        job = executor.submit(probe.run, self.engine.routes[rid], False)
                        self.jobs[job] = (rid, False, self.core, now, self.contract)
                if now >= next_sync:
                    try:
                        self.save()  # Proof and full route definition precede publication.
                        self.phase, _ = self.director.sync(now)
                        if self.shadow_error:
                            self.phase = "observer_fault"
                    except Exception as exc:
                        self.phase = "observer_fault"
                        self.error = f"controller reconciliation failed: {type(exc).__name__}"
                    next_sync = now + 1
                if now >= next_save:
                    self.save()
                    next_save = now + 5
                if now >= next_snapshot:
                    atomic_json(self.root / "status.json", self.snapshot(time.time()))
                    next_snapshot = now + 1
        finally:
            hint_socket.close()
            atomic_json(self.root / "status.json", {"version": 1, "at": time.time(), "phase": "stopped", "mode": self.config["mode"]})
            if self.core:
                self.core.close()
            for core in self.retired:
                core.close()
            if pending_core:
                with contextlib.suppress(Exception):
                    pending_core.result(timeout=30)[0].close()
            executor.shutdown(wait=True, cancel_futures=True)
            setup.shutdown(wait=True, cancel_futures=True)
            publisher.close()
            with contextlib.suppress(FileNotFoundError):
                hint_path.unlink()
        return 0


def install(config_path, *, source=None):
    """Activate only this optional service. CCC's B services are untouched."""
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    source = Path(source or __file__).resolve()
    label = (os.environ.get("CCC_LABEL_PREFIX") or f"com.{os.environ.get('USER') or Path.home().name}") + ".ccc-network-guard"
    path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    root = Path(config["state_dir"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"Label": label, "ProgramArguments": [sys.executable, "-B", str(source), "--config", str(config_path)],
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 5, "ProcessType": "Background",
        "WorkingDirectory": str(source.parent),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONDONTWRITEBYTECODE": "1"},
        "StandardOutPath": str(root / "service.out.log"), "StandardErrorPath": str(root / "service.err.log")}
    domain, service = f"gui/{os.getuid()}", f"gui/{os.getuid()}/{label}"
    previous = path.read_bytes() if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_bytes(plistlib.dumps(payload))
    os.replace(temporary, path)
    subprocess.run(["/bin/launchctl", "bootout", service], capture_output=True)
    try:
        from cmux_codex_watch import _bootstrap_runtime_service
        _bootstrap_runtime_service(domain, path)
    except Exception:
        if previous is not None:
            path.write_bytes(previous)
            subprocess.run(["/bin/launchctl", "bootstrap", domain, str(path)], capture_output=True)
        raise
    return {"label": label, "plist": str(path), "config": str(config_path), "mode": config["mode"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    guard = Guard(args.config)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: guard.stop.set())
    return guard.run()


if __name__ == "__main__":
    raise SystemExit(main())

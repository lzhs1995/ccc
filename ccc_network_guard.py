"""One optional, service-aware network guard, independent of CCC's B guardian.

Only this process probes and changes one configured Mihomo selector. It cannot
send terminal input, emit Codex lifecycle events, reload Clash, or close users'
connections. The watcher consumes status.json and sends best-effort hints.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import copy
import dataclasses
import errno
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

from ccc_mihomo import (Controller, ControllerError, PhysicalLink, ProbeResult, ResponsesProbe,
                        Route, ShadowCore, atomic_json, effective_service_route, inventory,
                        probe_credential, probe_key, reachability_mode, validate_dependencies)


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
    if "outer_group" in config and (not isinstance(config["outer_group"], str)
            or not config["outer_group"] or config["outer_group"] in {config["group"], "GLOBAL"}
            or config["group"] == "GLOBAL"):
        raise ValueError("outer_group must be a separate user-owned selector")
    pools = config.get("commercial_pools")
    if not isinstance(pools, list) or not pools or len(set(pools)) != len(pools):
        raise ValueError("commercial_pools must be a nonempty ordered list")
    if not isinstance(config.get("sources"), list) or not config["sources"]:
        raise ValueError("at least one local subscription source is required")
    for source in config["sources"]:
        if not isinstance(source, dict) or source.get("pool") not in pools or not Path(source.get("path", "")).is_absolute():
            raise ValueError("each subscription needs an absolute path and a commercial pool")
    probe = config.get("probe") or {}
    if (not isinstance(probe, dict) or not isinstance(probe.get("validation_mode", "response"), str)
            or probe.get("validation_mode", "response") not in {"response", "reachability"}):
        raise ValueError("probe validation_mode must be response or reachability")
    endpoint = urllib.parse.urlsplit(probe.get("url", ""))
    if (endpoint.scheme != "https" or endpoint.hostname != config["service_host"]
            or endpoint.username or endpoint.password or endpoint.fragment
            or not reachability_mode(probe) and not probe.get("model")):
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


def contract_digest(config, *, legacy=False, legacy_credential=False, credential=None, source_digest=None):
    probe = dict(config["probe"])
    if reachability_mode(probe):
        # Anonymous reachability has no account/model identity. Never read an
        # auth file or allow its rotation to revoke otherwise reachable exits.
        wire = {"validation_mode": "reachability", "classifier_version": 1,
                "url": probe["url"], "body": "invalid-json-anonymous",
                "user_agent": probe.get("user_agent", "codex_cli_rs/0.156.1")}
        return hashlib.sha256(json.dumps(wire, sort_keys=True).encode()).hexdigest()
    if not legacy:
        for key in ("timeout_sec", "deep_timeout_sec", "light_timeout_by_pool"):
            probe.pop(key, None)  # Scheduling/deadline changes do not change API identity.
    if (legacy or legacy_credential) and probe.get("auth_file"):
        try:
            probe["credential_digest"] = (source_digest if source_digest is not None else
                                          hashlib.sha256(Path(probe["auth_file"]).read_bytes()).hexdigest())
        except OSError:
            probe["credential_digest"] = "unreadable"
    if probe.get("auth_env"):
        key = os.environ.get(probe["auth_env"], "") if credential is None else credential
        probe["credential_digest"] = hashlib.sha256(key.encode()).hexdigest()
    elif not (legacy or legacy_credential) and probe.get("auth_file"):
        try:
            # Only the credential sent by ResponsesProbe identifies the API
            # contract. Formatting/metadata writes must not revoke all routes.
            key = probe_key(probe) if credential is None else credential
            probe["credential_digest"] = hashlib.sha256(key.encode()).hexdigest()
        except (OSError, ValueError):
            probe["credential_digest"] = "unreadable"
    return hashlib.sha256(json.dumps(probe, sort_keys=True).encode()).hexdigest()


def probe_binding(config):
    if reachability_mode(config["probe"]):
        return "", contract_digest(config), set()
    key, source_digest = probe_credential(config["probe"])
    arguments = {"credential": key, "source_digest": source_digest}
    return (key, contract_digest(config, **arguments),
            {contract_digest(config, legacy=True, **arguments),
             contract_digest(config, legacy_credential=True, **arguments)})


class ApiProbeState:
    """Bounded API retry evidence, independent of route quarantine and budget."""
    WINDOW = 1800
    MAX_DELAY = 300

    def __init__(self, now, saved=None, *, monotonic=None):
        self.monotonic = monotonic
        self.mono_until = None
        self.since = now
        self.history = []
        self.kind, self.streak, self.paths = "", 0, []
        self.last_at, self.until, self.delay = 0, 0, 0
        self.recovery = ""
        if saved is None:
            return
        def stamp(value):
            return type(value) in (int, float) and math.isfinite(value) and value >= 0
        valid = (isinstance(saved, dict) and saved.get("version") == 1
                 and isinstance(saved.get("kind"), str) and saved["kind"] in API_ATTENTION | {""}
                 and type(saved.get("streak")) is int and 0 <= saved["streak"] <= 1024
                 and all(stamp(saved.get(k)) for k in ("since", "last_at", "until", "delay"))
                 and saved["delay"] <= self.MAX_DELAY
                 and ((saved["delay"] == 0 and saved["until"] == 0)
                      or (saved["delay"] > 0 and saved["until"] > 0))
                 and isinstance(saved.get("paths"), list) and len(saved["paths"]) <= 8
                 and all(isinstance(r, str) and len(r) <= 128 for r in saved["paths"]))
        if not valid:
            # Corrupt optional telemetry is not route failure. Defer a bounded
            # recovery check; never repair it by erasing the paid budget.
            self.recovery = "invalid_api_state"
            self.delay, self.until = self.MAX_DELAY, now + self.MAX_DELAY
        else:
            self.since = min(now, saved["since"])
            self.kind, self.streak = saved["kind"], saved["streak"]
            self.paths = list(dict.fromkeys(saved["paths"]))
            self.last_at, self.delay = saved["last_at"], saved["delay"]
            self.until = saved["until"]
            if self.delay:
                # A restart cannot prove elapsed monotonic time. Re-arm the
                # existing finite delay, retaining history and reservations.
                self.until = now + self.delay
                self.recovery = "restart"
            rows = saved.get("history", [])
            if isinstance(rows, list):
                for row in rows[-128:]:
                    if (isinstance(row, dict) and stamp(row.get("at")) and 0 <= now - row["at"] <= self.WINDOW
                            and isinstance(row.get("kind"), str)
                            and row["kind"] in API_ATTENTION | LOCAL_FAILURES | {"healthy", "observer_error"}
                            and type(row.get("status")) is int and 0 <= row["status"] <= 599):
                        self.history.append({k: row[k] for k in ("at", "kind", "status")})
        if self.delay and self.monotonic is not None:
            self.mono_until = self.monotonic() + self.delay

    def record(self, rid, result, now):
        if not result.deep:
            return
        self.since = min(self.since, now)
        self.history = [r for r in self.history if 0 <= now - r["at"] <= self.WINDOW][-127:]
        self.history.append({"at": now, "kind": result.kind, "status": result.status})
        self.recovery = ""
        if result.kind not in API_ATTENTION:
            self.kind, self.streak, self.paths = "", 0, []
            self.last_at, self.until, self.delay = now, 0, 0
            self.mono_until, self.recovery = None, ""
            return
        if result.kind != self.kind or now - self.last_at > 600:
            self.streak, self.paths = 0, []
            self.delay, self.until, self.mono_until = 0, 0, None
        self.kind, self.last_at = result.kind, now
        self.streak = min(1024, self.streak + 1)
        self.paths = list(dict.fromkeys([*self.paths, rid]))[-8:]
        if self.streak >= 3 and len(self.paths) >= 2:
            self.delay = min(self.MAX_DELAY, 60 * 2 ** min(3, self.streak - 3))
            self.until = now + self.delay
            self.mono_until = self.monotonic() + self.delay if self.monotonic is not None else None
            self.recovery = ""

    def remaining(self, now):
        if self.mono_until is not None:
            return max(0, self.mono_until - self.monotonic())
        return min(self.MAX_DELAY, max(0, self.until - now))

    def snapshot(self, now):
        history = [r for r in self.history if 0 <= now - r["at"] <= self.WINDOW]
        counts = {}
        for row in history:
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
        return {"window_sec": self.WINDOW, "observed_since": max(self.since, now - self.WINDOW),
                "completed": len(history), "outcomes": counts, "last_result": history[-1] if history else None,
                "backoff": {"kind": self.kind, "consecutive_failures": self.streak,
                            "distinct_paths": len(self.paths), "remaining_sec": round(self.remaining(now), 3),
                            "maximum_sec": self.MAX_DELAY, "recovery": self.recovery}}

    def saved(self):
        return {"version": 1, "since": self.since, "history": self.history,
                "kind": self.kind, "streak": self.streak, "paths": self.paths,
                "last_at": self.last_at, "delay": self.delay, "until": self.until}


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
    light_stage: str = ""
    light_detail: str = ""
    deep_at: float = 0
    deep_ok_at: float = 0
    deep_kind: str = "unknown"
    deep_stage: str = ""
    deep_detail: str = ""
    deep_attempt_at: float = 0
    elapsed_ms: float = 0
    status: int = 0


@dataclasses.dataclass
class ReachabilityHealth:
    """Separate admission from retained legacy model/API evidence."""
    admitted: bool = False
    quarantined: bool = False
    quarantine_until: float = 0
    failures: int = 0
    successes: int = 0
    failure_at: float = 0
    at: float = 0
    ok_at: float = 0
    kind: str = "unknown"
    status: int = 0
    detail: str = ""
    stage: str = ""
    elapsed_ms: float = 0

    def record(self, result, now, started_at, policy):
        if result.deep or now < self.at or result.kind == "accessible" and started_at < self.failure_at:
            return
        self.at, self.kind, self.status = now, result.kind, result.status
        self.detail, self.stage, self.elapsed_ms = result.detail, result.stage, result.elapsed_ms
        if result.kind == "accessible":
            self.ok_at, self.failures = now, 0
            self.successes = self.successes + 1 if started_at >= self.quarantine_until else 0
            needed = policy["recover_successes"] if self.quarantined else 1
            if self.successes >= needed:
                self.admitted, self.quarantined = True, False
        elif result.kind in LOCAL_FAILURES:
            self.successes, self.failure_at = 0, now
            self.failures += 1
            if result.kind == "blocked" or self.failures >= policy["failures_before_isolation"]:
                self.admitted, self.quarantined = False, True
                self.quarantine_until = now + policy["cooldown_sec"]
        else:
            # Unknown HTML, observer faults and ambiguous responses neither
            # prove a ban nor supply a recovery success.
            self.successes = 0


def recent_deep_starts(starts, now, minimum_interval=60):
    # Clock correction does not refund an already consumed request. Keep
    # future reservations until their window expires on the corrected clock,
    # and use the latest timestamp for the minimum spacing after a restart.
    window = max(60, minimum_interval)
    return sorted(x for x in starts if type(x) in (int, float)
                  and math.isfinite(x) and x >= 0 and now - x < window)


class Engine:
    """Pure route policy. Probe results never constitute terminal-send grants."""
    def __init__(self, config, routes, saved=None, now=None, *, monotonic=None,
                 reuse_reachability_evidence=True):
        self.config = config
        self.policy = {**DEFAULTS, **config.get("policy", {})}
        self.routes = {r.id: r for r in routes}
        self.active = set(self.routes)
        self.health = {r: Health() for r in self.routes}
        self.reachability = {r: ReachabilityHealth() for r in self.routes}
        self.current = ""
        self.active_pool = config["commercial_pools"][0]
        self.deep_starts = []
        self.deep_pending = None
        self.deep_settled_at = 0
        self.deep_recovery = None
        self._monotonic = monotonic
        self._deep_settled_mono = None
        self.last_switch = 0
        self.hint_at = 0
        self.hint_route = ""
        self.seed_consumed = False
        now = time.time() if now is None else now
        self.api = ApiProbeState(now, (saved or {}).get("api_probe"), monotonic=monotonic)
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
            budget = saved.get("deep_budget")
            settled = budget.get("settled_at") if isinstance(budget, dict) else None
            valid = (isinstance(budget, dict) and budget.get("version") == 1
                     and type(settled) in (int, float) and math.isfinite(settled) and settled >= 0)
            if valid:
                self.deep_settled_at = settled
                self.deep_recovery = budget.get("last_recovery")
            # A previous process cannot prove the actual dispatch time of an
            # unfinished/legacy reservation. Even a settled restart waits a
            # full interval, because wall-clock correction may span restarts.
            # Keep its spent history; this is a new barrier, never a refund.
            if not valid or budget.get("pending") is not None or settled or self.deep_starts:
                pending = budget.get("pending") if isinstance(budget, dict) else None
                self.deep_recovery = {"at": now,
                    "reason": "legacy_or_invalid" if not valid else "unfinished" if pending else "restart",
                    "reservation": pending}
                self.settle_deep(now)
        if self.reachability_enabled:
            self.reset_reachability()
            if saved and saved.get("validation_mode") == "reachability":
                for rid, value in saved.get("reachability_health", {}).items():
                    if rid not in self.routes or not isinstance(value, dict):
                        continue
                    try:
                        h = ReachabilityHealth(**value)
                        if (any(type(v) is not bool for v in (h.admitted, h.quarantined))
                                or any(type(v) is not int or v < 0 for v in (h.failures, h.successes))
                                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= now + 1
                                       for v in (h.at, h.ok_at, h.failure_at))
                                or type(h.quarantine_until) not in (int, float)
                                or not math.isfinite(h.quarantine_until) or h.quarantine_until < 0
                                or not isinstance(h.kind, str)
                                or h.kind not in {"unknown", "accessible", "blocked", "timeout", "transport",
                                                  "truncated", "contract", "observer_error"}
                                or type(h.status) is not int or not 0 <= h.status <= 599
                                or not isinstance(h.detail, str) or not isinstance(h.stage, str)
                                or type(h.elapsed_ms) not in (int, float)
                                or not math.isfinite(h.elapsed_ms) or h.elapsed_ms < 0
                                or h.ok_at > h.at or h.failure_at > h.at
                                or h.admitted and not h.ok_at):
                            continue
                        # Persisted wall time cannot impose an unbounded wait
                        # after a clock correction. A new response is still
                        # required; clamping never grants admission.
                        h.quarantine_until = min(h.quarantine_until, now + self.policy["cooldown_sec"])
                        if not reuse_reachability_evidence:
                            h = ReachabilityHealth(quarantined=h.quarantined,
                                                   quarantine_until=h.quarantine_until)
                        self.reachability[rid] = h
                    except (TypeError, ValueError):
                        continue

    @property
    def reachability_enabled(self):
        return reachability_mode(self.config.get("probe", {}))

    def reset_reachability(self, *, preserve_current=False):
        # Old quarantine is evidence to recover from, not a reason to require
        # paid generation forever. Retain the old Health record unchanged.
        previous = self.reachability if preserve_current else self.health
        self.reachability = {rid: ReachabilityHealth(quarantined=h.quarantined,
            quarantine_until=h.quarantine_until) for rid, h in previous.items()}

    def light_freshness(self):
        # A normal 60-second polling interval must not expire after 20 seconds.
        # The existing light window is a bounded scheduling margin; late data
        # still expires rather than being kept alive by a dashboard refresh.
        return (max(self.policy["light_fresh_sec"], self.policy["other_interval_sec"] + self.policy["light_fresh_sec"])
                if self.reachability_enabled else self.policy["light_fresh_sec"])

    def update_inventory(self, routes):
        self.active = {r.id for r in routes}
        for route in routes:
            self.routes[route.id] = route
            self.health.setdefault(route.id, Health())
            self.reachability.setdefault(route.id, ReachabilityHealth())
        # A disappeared subscription entry cannot delete the active, already
        # verified connection path. Retire it after a replacement is selected.
        for rid in set(self.routes) - self.active - {self.current}:
            del self.routes[rid]
            self.health.pop(rid, None)
            self.reachability.pop(rid, None)

    def record(self, rid, result, now, *, started_at=None):
        if rid not in self.health:
            return
        h = self.health[rid]
        started_at = now if started_at is None else started_at
        if self.reachability_enabled:
            self.reachability[rid].record(result, now, started_at, self.policy)
            if not result.deep:
                # Schedule using actual completion time; retained deep/API and
                # paid-budget records are not rewritten as reachability proof.
                h.light_at, h.light_kind = now, result.kind
                h.light_stage, h.light_detail = result.stage, result.detail
                if result.kind == "accessible":
                    h.light_ok_at = now
            return
        # A slow SSE from before a newer failure is not recovery evidence.
        # Light and generation results are independent: a JSON validator can
        # remain reachable while every generation stream is truncated.
        if result.deep and result.kind == "healthy" and started_at < h.failure_at:
            return
        self.api.record(rid, result, now)
        h.elapsed_ms, h.status = result.elapsed_ms, result.status
        if result.deep:
            h.deep_at, h.deep_kind = now, result.kind
            h.deep_stage, h.deep_detail = result.stage, result.detail
        else:
            h.light_at, h.light_kind = now, result.kind
            h.light_stage, h.light_detail = result.stage, result.detail
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
        if self.reachability_enabled:
            return  # Old model completions cannot seed this distinct contract.
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
        if self.reachability_enabled:
            h = self.reachability[rid]
            return h.admitted and not h.quarantined and 0 < h.ok_at <= now and now - h.ok_at <= self.light_freshness()
        h = self.health[rid]
        return h.qualified and not h.quarantined and 0 <= now - h.deep_ok_at <= self.policy["qualification_ttl_sec"]

    def ready(self, rid, now):
        if self.reachability_enabled:
            h = self.reachability[rid]
            return self.qualified(rid, now) and h.failures == 0 and h.kind == "accessible"
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
            def recovery_order(rid):
                h = self.health[rid]
                if self.reachability_enabled:
                    h = self.reachability[rid]
                    accessible = (h.kind == "accessible" and not h.failures
                                  and 0 <= now - h.ok_at <= self.light_freshness())
                else:
                    accessible = (h.light_kind == "accessible" and not h.light_failures
                                  and 0 <= now - h.light_ok_at <= self.policy["light_fresh_sec"])
                return (not self.ready(rid, now), not self.qualified(rid, now),
                        not accessible, self.routes[rid].priority)
            ids = sorted(ids, key=recovery_order)
            chosen.extend(ids[:2 if pool not in self.config["commercial_pools"] else 1])
        return list(dict.fromkeys(chosen))

    def decision(self, now):
        if self.reachability_enabled:
            return self.reachability_decision(now)
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

    def reachability_decision(self, now):
        ready = [rid for rid in self.routes if self.ready(rid, now)]
        pools = self.config["commercial_pools"]
        current = self.reachability.get(self.current)
        if self.current in ready and self.routes[self.current].pool in pools:
            return self.current, "healthy"
        if (current and current.admitted and not current.quarantined
                and current.failures and self.qualified(self.current, now)):
            return self.current, "suspect"
        for pool in dict.fromkeys([self.active_pool, *pools]):
            if pool not in pools:
                continue
            available = self.ranked([rid for rid in ready if self.routes[rid].pool == pool])
            if available:
                return available[0], "healthy"
        fallback = self.ranked([rid for rid in ready if self.routes[rid].pool not in pools])
        if fallback:
            return fallback[0], "fallback"
        if current and current.quarantined:
            return "", "network_wait"
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
            # The current path always gets first service. All other paths
            # compete by due time, so slow hot probes cannot starve inventory.
            return (0 if rid == self.current else 1, due)
        return sorted((rid for rid in self.routes if rid not in in_flight and order(rid)[1] <= now), key=order)

    def deep_due(self, now, in_flight):
        if self.reachability_enabled:
            return None
        interval = max(self.policy["deep_min_interval_sec"], 60 / self.policy["deep_per_minute"])
        self.deep_starts = recent_deep_starts(self.deep_starts, now, interval)
        if (self.api.remaining(now) > 0 or self.deep_pending is not None
                or self.deep_settled_at and now - self.deep_settled_at < interval
                or self._deep_settled_mono is not None and self._monotonic() - self._deep_settled_mono < interval
                or sum(now - stamp < 60 for stamp in self.deep_starts) >= self.policy["deep_per_minute"]
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
            # A previously qualified current route must not monopolize paid
            # checks after an API failure. Check another eligible path while
            # this one waits its normal interval; selection stays unchanged.
            if (self.qualified(rid, now) and h.deep_kind in API_ATTENTION
                    and now - h.deep_at < min(period, 300)):
                continue
            urgent = self.hint_at > h.deep_attempt_at and rid == (self.hint_route or self.current)
            if self.qualified(rid, now) and now - h.deep_ok_at < period and not urgent and not h.deep_failures:
                continue
            pool_missing = not any(self.ready(other, now) and self.routes[other].pool == self.routes[rid].pool
                                   for other in self.routes)
            candidates.append((0 if urgent or rid == self.current else 1 if pool_missing else 2 if rid in hot else 3,
                               h.deep_attempt_at, self.routes[rid].priority, rid))
        return min(candidates)[-1] if candidates else None

    def reserve_deep(self, rid, now):
        if self.reachability_enabled:
            raise RuntimeError("model probes disabled in reachability mode")
        if self.deep_pending is not None:
            raise RuntimeError("an unfinished deep reservation still owns the budget")
        self.deep_starts.append(now)
        self.deep_pending = {"route_id": rid, "reserved_at": now}
        self.health[rid].deep_attempt_at = now

    def settle_deep(self, now):
        # Only call once the worker is known to have finished, or when no
        # worker was submitted. Queue/fsync/journal delays therefore cannot
        # shorten the next actual request's spacing. Collection may be late;
        # using its clock conservatively adds delay, without refreshing health.
        self.deep_pending = None
        self.deep_settled_at = max(self.deep_settled_at, now)
        if self._monotonic is not None:
            self._deep_settled_mono = self._monotonic()

    def saved(self):
        return {"health": {rid: dataclasses.asdict(h) for rid, h in self.health.items()},
                "validation_mode": self.config.get("probe", {}).get("validation_mode", "response"),
                "reachability_health": {rid: dataclasses.asdict(h) for rid, h in self.reachability.items()},
                "current": self.current, "active_pool": self.active_pool,
                "deep_starts": self.deep_starts, "seed_consumed": self.seed_consumed,
                "api_probe": self.api.saved(),
                "deep_budget": {"version": 1, "pending": self.deep_pending,
                                "settled_at": self.deep_settled_at, "last_recovery": self.deep_recovery}}


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
        self.effective_route = {"managed": False, "kind": "unknown", "selection": "", "chain": []}
        self.reconciliation_error = ""

    def sync(self, now, *, link=None, observer_error=""):
        # A manual choice may happen while an automatic refresh is in flight.
        self.effective_route = effective_service_route(self.controller, self.config)
        failure = None
        try:
            phase, target = self._sync(now, link=link, observer_error=observer_error)
            self.reconciliation_error = ""
        except Exception as exc:
            failure = exc
            self.reconciliation_error = "automatic selector reconciliation failed: " + type(exc).__name__
            phase, target = "observer_fault", self.engine.current
        self.effective_route = effective_service_route(self.controller, self.config)
        if not self.effective_route["managed"]:
            kind = self.effective_route["kind"]
            phase = "manual" if kind == "manual" else "inactive" if kind == "inactive" else "observer_fault"
        elif failure is not None:
            raise failure
        return phase, target

    def _sync(self, now, *, link=None, observer_error=""):
        e, c = self.engine, self.config
        if not (self.effective_route["managed"] or c.get("outer_group") in self.effective_route.get("chain", [])):
            # A same-named group in an unrelated profile is not ours to edit.
            self.actual_name = ""
            return "inactive", e.current
        generation = link.current() if link is not None else None
        try:
            group = self.controller.get("/proxies/" + urllib.parse.quote(c["group"], safe=""))
        except ControllerError as exc:
            if exc.status != 404:
                raise
            self.actual_name = ""
            return "inactive", e.current
        actual = group.get("now", "")
        ids = {r.name: rid for rid, r in e.routes.items()}
        known = ids.get(actual) or c.get("bootstrap_aliases", {}).get(actual)
        if known in e.routes:
            e.current = known
            e.active_pool = e.routes[known].pool
        elif actual == c["offline_proxy"]:
            e.current = ""
        self.actual_name = actual

        def observer_unavailable():
            return bool(observer_error) or (link is not None and not link.accepts(generation))

        def held():
            return ("network_wait" if actual == c["offline_proxy"] else "observer_fault", e.current)

        # Link loss is not a node verdict. Keep the currently selected path and
        # the last published provider, including across a network-guard restart.
        if observer_unavailable():
            return held()
        target, phase = e.decision(now)
        if not target and actual == c["offline_proxy"]:
            phase = "network_wait"
        approved = {rid for rid in e.routes if e.qualified(rid, now)}
        # Do not remove the old selection until the replacement is committed.
        if known in e.routes:
            approved.add(known)
        ordered = sorted(e.ranked(approved), key=lambda rid: rid != e.current)
        if observer_unavailable():
            return held()
        if self.publisher.set([e.routes[rid] for rid in ordered]):
            self.pending_refresh = True
        if c["mode"] != "manage":
            return "observe", target
        if actual not in ids and actual not in c.get("bootstrap_aliases", {}) and actual != c["offline_proxy"]:
            return "unmanaged_selection", target
        if self.pending_refresh:
            if observer_unavailable():
                return held()
            self.controller.refresh(c["provider"])
            self.published, self.pending_refresh = approved, False
            group = self.controller.get("/proxies/" + urllib.parse.quote(c["group"], safe=""))
        desired = e.routes[target].name if target else c["offline_proxy"] if phase == "network_wait" else actual
        if desired != actual:
            if observer_unavailable():
                return held()
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
        self.inventory_error = ""
        self.error = ""
        self.phase = "starting"
        self.hint_count = 0
        self.link = PhysicalLink(self.config["interface"])
        self.last_probe_event = None
        self.discarded_probes = 0
        self.journal_error = ""
        self.storage_errors = {}
        self.control_error = ""
        self.probe_observer_errors = {}
        self._credential = None
        self.credential_error = ""
        self.probe_binding_at = 0

    def journal(self, event):
        # Fixed, bounded metadata only: never credentials, response bodies or
        # subscription definitions. Logging failure cannot halt routing.
        path = self.root / "events.ndjson"
        try:
            if path.exists() and path.stat().st_size >= 2 * 1024 * 1024:
                os.replace(path, self.root / "events.1.ndjson")
            with path.open("a") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            self.journal_error = ""
        except OSError as exc:
            self.journal_error = "event journal unavailable: " + type(exc).__name__

    def write_state(self, filename, value):
        # Disk-full/status errors cannot terminate the provider or bypass the
        # cleanup path. A failed health write also prevents a paid dispatch.
        try:
            atomic_json(self.root / filename, value)
            self.storage_errors.pop(filename, None)
            return True
        except OSError as exc:
            self.storage_errors[filename] = "persistence unavailable: " + errno.errorcode.get(exc.errno, type(exc).__name__)
            return False

    def save(self):
        return self.write_state("health.json", {**self.engine.saved(), "contract": self.contract,
            "probe_binding_at": self.probe_binding_at,
            "routes": [r.record() for r in self.engine.routes.values()]})

    def bind_probe(self, config, now):
        previous_error = self.credential_error
        try:
            credential, contract, _ = probe_binding(config)
        except (OSError, ValueError):
            self._credential = None
            self.credential_error = "probe credential unavailable; API checks suspended"
            if not previous_error:
                self.journal({"event": "probe_credential_unavailable", "at": now, "contract": self.contract})
            return False
        self._credential, self.credential_error = credential, ""
        changed = contract != self.contract
        if changed:
            previous, self.contract = self.contract, contract
            self.probe_binding_at = now
            if reachability_mode(config["probe"]):
                self.engine.reset_reachability(preserve_current=self.engine.reachability_enabled)
            else:
                for health in self.engine.health.values():
                    health.qualified = False
                    health.deep_attempt_at = 0
            # Account/model identity changed. Keep route isolation, old
            # evidence and paid reservations; begin a separately bound sample.
            if not reachability_mode(config["probe"]):
                self.engine.api = ApiProbeState(now, monotonic=self.engine._monotonic)
            self.journal({"event": "probe_contract_changed", "at": now, "previous": previous,
                          "contract": contract, "model": config["probe"].get("model"),
                          "validation_mode": config["probe"].get("validation_mode", "response")})
        elif previous_error:
            self.journal({"event": "probe_credential_restored", "at": now, "contract": contract})
        return changed

    def observer_error(self):
        return (self.shadow_error or self.error or self.credential_error or next(iter(self.storage_errors.values()), "")
                or self.probe_observer_errors.get(self.engine.current, ""))

    def automatic_status(self, now, rows):
        if self.engine.reachability_enabled:
            return self.reachability_status(now, rows)
        api = self.engine.api.snapshot(now)
        ready = sum(row["ready"] for row in rows)
        accessible = sum(row["light_kind"] == "accessible" and not row["light_failures"]
                         and 0 <= now - row["light_ok_at"] <= self.engine.policy["light_fresh_sec"] for row in rows)
        last = api["last_result"] or {}
        if self.credential_error:
            state, reason = "credential_unavailable", "探测凭据暂不可读，已暂停验证"
        elif self.observer_error() or not self.link.status()["available"]:
            state, reason = "observer_error", "本机探测暂不可用，保留已有线路证据"
        elif api["backoff"]["recovery"] == "invalid_api_state":
            state, reason = "observer_error", "探测退避记录不完整，等待恢复检查"
        elif api["backoff"]["remaining_sec"] > 0:
            if api["backoff"]["consecutive_failures"] >= 3 and api["backoff"]["distinct_paths"] >= 2:
                state, reason = "api_backoff", "多条路径连续 API 异常，等待恢复检查"
            else:
                state, reason = "checking", "等待保守恢复检查"
        elif ready:
            state, reason = "ready", "已有完整 API 答复验证通过的候选"
        elif last.get("kind") == "observer_error":
            state, reason = "observer_error", "最近一次本机探测失败，等待重检"
        elif last.get("kind") in API_ATTENTION:
            state = last["kind"]
            reason = {"upstream": "上游 API 服务异常", "rate_limit": "API 额度或速率受限",
                      "auth": "API 拒绝当前凭据", "permission": "API 账号或模型权限不足",
                      "contract": "API 响应不符合验证要求"}[state]
        elif accessible:
            state, reason = "checking", "接口可达，等待完整 API 答复验证"
        elif last.get("kind") in LOCAL_FAILURES:
            state, reason = "route_failure", "最近一次线路或流式传输验证失败"
        else:
            state, reason = "checking", "等待线路与 API 验证"
        probe = self.config["probe"]
        endpoint = urllib.parse.urlsplit(probe.get("url", ""))
        source = ({"kind": "environment", "name": probe["auth_env"]} if probe.get("auth_env") else
                  {"kind": "file", "path": probe["auth_file"], "key": probe.get("auth_key", "OPENAI_API_KEY")}
                  if probe.get("auth_file") else {"kind": "local_test"})
        return {"state": state, "reason": reason, "candidate_count": len(rows), "ready": ready,
                "light_accessible": accessible, "summary": f"自动候选 {ready}/{len(rows)} · {reason}",
                "api": api, "probe": {"model": probe.get("model"), "host": endpoint.hostname,
                    "path": endpoint.path, "credential_source": source, "credential_available": self._credential is not None,
                    "contract": self.contract, "bound_since": self.probe_binding_at},
                "latency_note": "Clash 通用测速与完整 API 验证独立；Timeout 不代表全部手动节点不可用"}

    def reachability_status(self, now, rows):
        counts = dict.fromkeys(("reachable", "blocked", "failed", "uncertain", "pending"), 0)
        fresh = self.engine.light_freshness()
        for row in rows:
            h = self.engine.reachability[row["id"]]
            kind = ("pending" if not h.at or not 0 <= now - h.at <= fresh else
                    "reachable" if h.kind == "accessible" else "blocked" if h.kind == "blocked" else
                    "failed" if h.kind in {"timeout", "transport", "truncated"} else
                    "pending" if h.kind == "observer_error" else "uncertain")
            counts[kind] += 1
        ready = sum(row["ready"] for row in rows)
        recovering = sum(h.kind == "accessible" and h.quarantined for h in self.engine.reachability.values())
        if self.observer_error() or not self.link.status()["available"]:
            state, reason = "observer_error", "本机探测暂不可用，等待重检"
        elif ready:
            state, reason = "ready", "接口可达；账号额度与模型响应未验证"
        elif recovering:
            state, reason = "recovering", "接口已可达，等待冷却后的连续恢复检查"
        elif counts["pending"]:
            state, reason = "checking", "正在收集新的可达性证据"
        else:
            state, reason = "no_candidate", "暂无可选线路；查看拦截、传输失败与待确认分类"
        last = max((h.at for h in self.engine.reachability.values()), default=0)
        endpoint = urllib.parse.urlsplit(self.config["probe"]["url"])
        return {"state": state, "reason": reason, "validation_mode": "reachability",
                "candidate_count": len(rows), "ready": ready, "counts": counts,
                "recovering": recovering, "last_checked_at": last, "fresh_for_sec": fresh,
                "light_accessible": counts["reachable"], "model_probes_enabled": False,
                "summary": (f"可达 {counts['reachable']}/{len(rows)} · 可选 {ready} · "
                    f"拦截 {counts['blocked']} · 失败 {counts['failed']} · 待确认 {counts['uncertain']} · 待检 {counts['pending']}"),
                "probe": {"host": endpoint.hostname, "path": endpoint.path, "credential_source": {"kind": "none"},
                          "contract": self.contract, "bound_since": self.probe_binding_at},
                "latency_note": "无 Key、无模型任务的接口可达性检测；HTTP 401/429/500 不等于节点被封"}

    def snapshot(self, now):
        e = self.engine
        physical = self.link.status()
        rows = [{"id": rid, "name": r.name, "label": r.label, "pool": r.pool,
                 **dataclasses.asdict(e.health[rid]), "qualified": e.qualified(rid, now),
                 "ready": e.ready(rid, now)} for rid, r in e.routes.items()]
        if e.reachability_enabled:
            for row in rows:
                h = e.reachability[row["id"]]
                row.update(legacy_quarantined=row["quarantined"], quarantined=h.quarantined,
                           reachability=dataclasses.asdict(h), elapsed_ms=h.elapsed_ms, status=h.status)
        return {"version": 1, "pid": os.getpid(), "at": now, "mode": self.config["mode"],
                "validation_mode": self.config["probe"].get("validation_mode", "response"),
                "group": self.config["group"], "outer_group": self.config.get("outer_group"),
                "service_host": self.config["service_host"], "phase": self.phase,
                "current_id": e.current, "current": self.director.actual_name,
                "active_pool": e.active_pool, "last_switch": e.last_switch,
                "error": self.control_error or self.observer_error() or physical["detail"] or self.inventory_error or self.journal_error,
                "inventory_error": self.inventory_error, "routes": rows,
                "effective_route": dict(self.director.effective_route),
                "storage_errors": dict(self.storage_errors),
                "probe_observer_errors": dict(self.probe_observer_errors),
                "physical_link": physical, "last_probe_event": self.last_probe_event,
                "automatic": self.automatic_status(now, rows),
                "discarded_probes": self.discarded_probes, "journal_error": self.journal_error,
                "ready": sum(r["ready"] for r in rows), "qualified": sum(r["qualified"] for r in rows),
                "quarantined": sum(r["quarantined"] for r in rows),
                "probe_in_flight": len(self.jobs),
                "deep_in_last_minute": sum(now - stamp < 60 for stamp in e.deep_starts),
                "deep_budget": e.saved()["deep_budget"],
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

    @staticmethod
    def run_probe(probe, item, deep, link, generation):
        # Completion belongs to the worker, not a later controller/config tick.
        # Delayed collection must not make an old response appear fresh.
        try:
            if link.accepts(generation):
                result = probe.run(item, deep)
            else:
                result = ProbeResult("observer_error", detail="physical interface changed before probe dispatch",
                                     deep=deep, stage="setup")
        except Exception:
            result = ProbeResult("observer_error", detail="probe worker failed", deep=deep)
        return result, time.time()

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
        saved = read_json(self.root / "health.json", {})
        now = time.time()
        try:
            self._credential, self.contract, legacy = probe_binding(self.config)
            if saved.get("contract") in legacy:
                saved["contract"] = self.contract
        except (OSError, ValueError):
            # Retain the last bound evidence for inspection. With no readable
            # credential we neither dispatch nor alter the automatic provider.
            self.contract = saved.get("contract", "")
            self.credential_error = "probe credential unavailable; API checks suspended"
        bound_at = saved.get("probe_binding_at") if saved.get("contract") == self.contract else None
        self.probe_binding_at = (bound_at if type(bound_at) in (int, float)
                                 and math.isfinite(bound_at) and 0 <= bound_at <= now else now)
        try:
            routes = inventory(self.config)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
            # A transient subscription write cannot strand a restarted guard.
            # Only exact saved definitions under the same API contract qualify.
            if saved.get("contract") != self.contract:
                raise
            routes = [Route.restore(r) for r in saved.get("routes", [])]
            if not routes:
                raise
            validate_dependencies(self.config, routes)
            self.inventory_error = "subscription unavailable; retaining the last saved inventory"
        if saved.get("contract") == self.contract:
            for old in saved.get("routes", []):
                if old.get("id") == saved.get("current") and not any(r.id == old["id"] for r in routes):
                    routes.append(Route.restore(old))
        elif not reachability_mode(self.config["probe"]):
            # Preserve rate reservations even if credentials/probe contract
            # changed, as well as isolation history. New credentials require
            # new admission; they cannot erase a route's quarantine.
            for h in saved.get("health", {}).values():
                h["qualified"] = False
                h["deep_ok_at"] = 0
                h["deep_attempt_at"] = 0
            saved.pop("api_probe", None)
        self.engine = Engine(self.config, routes, saved, monotonic=time.monotonic,
                             reuse_reachability_evidence=saved.get("contract") == self.contract)
        if self._credential is not None and self.config.get("seed_file") and not self.engine.seed_consumed:
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
        setup = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="network-shadow")
        inventory_worker = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="network-inventory")
        self.link.start()
        pending_core = setup.submit(self.new_shadow, routes)
        pending_inventory = None
        inventory_config = None
        next_core_retry, retry_delay = 0, 5
        next_inventory, next_config, next_sync, next_save, next_snapshot = 0, 0, 0, 0, 0
        try:
            self.save()
            while not self.stop.wait(.1):
                now = time.time()
                for change in self.link.changes():
                    self.journal({"event": "physical_link", **change})
                # Refresh identity before collecting old workers. Every worker
                # receives a frozen key/model paired with its journal contract.
                if now >= next_config:
                    try:
                        updated = load_config(self.path)
                        immutable = ("controller_socket", "group", "outer_group", "provider", "state_dir", "binary", "interface", "publish")
                        if any(updated.get(k) != self.config.get(k) for k in immutable):
                            raise ValueError("network transport changes require a network-guard restart")
                        if self.bind_probe(updated, time.time()):
                            next_inventory = 0
                        self.config = updated
                        self.engine.config = updated
                        self.engine.policy = updated["policy"]
                        self.director.config = updated
                        self.error = ""
                    except Exception as exc:
                        self.error = f"config refresh failed: {type(exc).__name__}"
                    next_config = time.time() + 2
                self.consume_hints(hint_socket, now)
                completed = []
                for job, (rid, deep, owner, began, contract, generation) in list(self.jobs.items()):
                    if job.done():
                        del self.jobs[job]
                        if deep:
                            # Budget settlement precedes contract/core/link
                            # rejection and also covers cancelled/failed jobs.
                            self.engine.settle_deep(time.time())
                        try:
                            result, completed_at = job.result()
                        except Exception:
                            result = ProbeResult("observer_error", detail="probe worker failed", deep=deep)
                            completed_at = now
                        completed.append((completed_at, rid, result, began, owner, contract, generation))
                # Submission order is not completion order. An older SSE must
                # see any intervening failure before it can clear quarantine.
                for completed_at, rid, result, began, owner, contract, generation in sorted(
                        completed, key=lambda item: (item[0], item[2].kind not in LOCAL_FAILURES)):
                    reason = ("probe_credential_unavailable" if self.credential_error else
                              "obsolete_contract" if contract != self.contract else
                              "probe_core_stopped" if owner.process.poll() is not None else
                              "physical_interface_generation_changed" if not self.link.accepts(generation) else "")
                    event = {"event": "probe", "at": completed_at, "collected_at": time.time(),
                             "contract": contract,
                             "route_id": rid, "started_at": began, "generation": generation,
                             "accepted": not reason, "discard_reason": reason, **dataclasses.asdict(result)}
                    if reason:
                        self.discarded_probes += 1
                    else:
                        self.engine.record(rid, result, completed_at, started_at=began)
                        if result.kind == "observer_error":
                            self.probe_observer_errors[rid] = result.detail or "local probe observer unavailable"
                        else:
                            self.probe_observer_errors.pop(rid, None)
                    self.journal(event)
                    self.last_probe_event = event
                for old in list(self.retired):
                    if not any(owner is old for _, _, owner, _, _, _ in self.jobs.values()):
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
                        self.probe_observer_errors.clear()
                        retry_delay = 5
                    except Exception as exc:
                        self.shadow_error = f"isolated probe core unavailable: {type(exc).__name__}"
                        next_core_retry = now + retry_delay
                        retry_delay = min(60, retry_delay * 2)
                    pending_core = None
                    next_inventory = now + self.engine.policy["inventory_interval_sec"]
                if self.core is None or self.core.process.poll() is not None:
                    self.shadow_error = "isolated probe core exited; restarting only its replacement"
                    if pending_core is None and now >= next_core_retry:
                        pending_core = setup.submit(self.new_shadow, list(self.engine.routes.values()))
                if pending_inventory is not None and pending_inventory.done() and pending_core is None:
                    try:
                        latest = pending_inventory.result()
                        if inventory_config == self.config:
                            wanted = {r.id for r in latest}
                            if self.engine.current in self.engine.routes and self.engine.current not in wanted:
                                latest.append(self.engine.routes[self.engine.current])
                                wanted.add(self.engine.current)
                            if wanted != set(self.engine.routes):
                                pending_core = setup.submit(self.new_shadow, latest)
                            elif self.config.get("publish_transit"):
                                publisher.set_catalog(latest, self.config["commercial_pools"])
                            self.inventory_error = ""
                    except Exception as exc:
                        if inventory_config == self.config:
                            self.inventory_error = f"subscription refresh failed; retaining verified routes: {type(exc).__name__}"
                    pending_inventory = None
                    next_inventory = (time.time() + self.engine.policy["inventory_interval_sec"]
                                      if inventory_config == self.config else 0)
                if pending_core is None and pending_inventory is None and now >= next_inventory:
                    inventory_config = copy.deepcopy(self.config)
                    pending_inventory = inventory_worker.submit(inventory, inventory_config)
                # Local configuration/publication work may have taken time.
                # Do not reserve a billable request using the tick's old clock.
                now = time.time()
                generation = self.link.current()
                if (generation is not None and self.core and self.core.process.poll() is None
                        and not self.error and self._credential is not None):
                    occupied = {rid for rid, deep, _, _, _, _ in self.jobs.values() if not deep}
                    probe = ResponsesProbe(self.config["probe"], self.core.ports, credential=self._credential)
                    if len(self.jobs) < self.engine.policy["concurrency"] and not any(deep for _, deep, _, _, _, _ in self.jobs.values()):
                        rid = self.engine.deep_due(now, occupied)
                        if rid:
                            self.engine.reserve_deep(rid, now)
                            if self.save():  # Never dispatch an unpersisted reservation.
                                self.journal({"event": "deep_reserved", "at": now, "route_id": rid,
                                              "contract": self.contract, "model": self.config["probe"].get("model"),
                                              "generation": generation})
                                job = executor.submit(self.run_probe, probe, self.engine.routes[rid], True, self.link, generation)
                                self.jobs[job] = (rid, True, self.core, now, self.contract, generation)
                            else:
                                # No worker was submitted. Retain the spent
                                # reservation and wait a full interval anyway.
                                self.engine.settle_deep(time.time())
                    for rid in self.engine.light_due(now, occupied):
                        if len(self.jobs) >= self.engine.policy["concurrency"]:
                            break
                        self.engine.health[rid].light_attempt_at = now
                        job = executor.submit(self.run_probe, probe, self.engine.routes[rid], False, self.link, generation)
                        self.jobs[job] = (rid, False, self.core, now, self.contract, generation)
                if now >= next_sync:
                    try:
                        self.save()  # Proof and full route definition precede publication.
                        previous_selection = self.director.actual_name
                        self.phase, _ = self.director.sync(now, link=self.link, observer_error=self.observer_error())
                        self.control_error = self.director.reconciliation_error
                        if self.director.actual_name != previous_selection:
                            self.journal({"event": "selection", "at": time.time(), "phase": self.phase,
                                          "previous": previous_selection, "current": self.director.actual_name})
                    except Exception as exc:
                        self.phase = "observer_fault"
                        self.control_error = f"controller reconciliation failed: {type(exc).__name__}"
                        self.director.effective_route = {"managed": False, "kind": "unknown", "selection": "", "chain": []}
                    next_sync = now + 1
                if now >= next_save:
                    self.save()
                    next_save = now + 5
                if now >= next_snapshot:
                    self.write_state("status.json", self.snapshot(time.time()))
                    next_snapshot = now + 1
        finally:
            self.link.close()
            hint_socket.close()
            self.write_state("status.json", {"version": 1, "at": time.time(), "phase": "stopped", "mode": self.config["mode"]})
            if self.core:
                self.core.close()
            for core in self.retired:
                core.close()
            if pending_core:
                with contextlib.suppress(Exception):
                    pending_core.result(timeout=30)[0].close()
            executor.shutdown(wait=True, cancel_futures=True)
            setup.shutdown(wait=True, cancel_futures=True)
            inventory_worker.shutdown(wait=True, cancel_futures=True)
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
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 5, "ProcessType": "Interactive",
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

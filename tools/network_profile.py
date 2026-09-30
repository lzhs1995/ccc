#!/usr/bin/env python3
"""Prepare an AnyRouter profile with an independent, persistent manual catalog.

This tool writes a NEW local candidate. It cannot call a controller, switch a
profile, restart Clash, change a guard, or start API requests.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ccc_mihomo import atomic_json, document, inventory
from ccc_network_guard import load_config


MANUAL_GROUP = "AnyRouter-Manual"


def manual_name(item):
    return f"AR-Manual/{item.pool}/{item.label} [{item.id[:8]}]"


def build_profile(profile, config, routes, *, default_id, extra_manual=()):
    """Return a new profile; no route is declared API-healthy by this function."""
    outer = config.get("outer_group")
    automatic = config["group"]
    if not outer or len({outer, automatic, MANUAL_GROUP, "GLOBAL"}) != 4:
        raise ValueError("automatic, outer and manual selectors must be distinct")
    by_id = {r.id: r for r in routes}
    if default_id not in by_id or not routes:
        raise ValueError("the preserved default must identify a complete local route")
    result = copy.deepcopy(profile)
    groups = result.setdefault("proxy-groups", [])
    prior = next((g for g in groups if g.get("name") == outer), None)
    if prior is None:
        raise ValueError("outer selector does not exist in the source profile")
    nodes = result.setdefault("proxies", [])
    names = {p["name"]: p for p in nodes}
    if any(name in names for name in (outer, automatic, MANUAL_GROUP)):
        raise ValueError("selector name conflicts with a proxy")
    def include(proxy):
        proxy = copy.deepcopy(proxy)
        name = proxy["name"]
        if name in names and names[name] != proxy:
            raise ValueError("manual proxy conflicts with an existing definition: " + name)
        if name not in names:
            nodes.append(proxy)
            names[name] = proxy
    ordered = sorted(routes, key=lambda item: (item.id != default_id,
        [*config["commercial_pools"], (config.get("fallback") or {}).get("pool")].index(item.pool),
        item.priority, item.name))
    choices = []
    for item in ordered:
        for dependency in item.proxies[:-1]:
            include(dependency)
        proxy = {**item.proxies[-1], "name": manual_name(item)}
        include(proxy)
        choices.append(proxy["name"])
    for name in extra_manual:
        proxy = names.get(name)
        if not proxy or proxy.get("type") in {"direct", "reject", "reject-drop"} or proxy.get("dialer-proxy"):
            raise ValueError("extra manual choice must be an existing independent exit")
        choices.append(name)
    choices = list(dict.fromkeys(choices))
    include({"name": config["offline_proxy"], "type": "reject"})
    providers = result.setdefault("proxy-providers", {})
    provider = config["provider"]
    old_provider = config.get("previous_provider", provider)
    providers[provider] = {"type": "http",
        "url": f"http://127.0.0.1:{config['publish']['port']}/{config['publish']['token']}/proxies",
        "path": f"./providers/{provider}.json", "proxy": "DIRECT", "interval": 86400,
        "health-check": {"enable": False}}
    if old_provider != provider:
        providers.pop(old_provider, None)
    # Preserve explicitly added user choices. Automatic admission never owns
    # or filters any part of this outer/manual catalog.
    outer_group = {**prior, "type": "select", "proxies": list(dict.fromkeys([
        MANUAL_GROUP, automatic, *[n for n in prior.get("proxies", [])
        if n not in {config["offline_proxy"], "DIRECT", "REJECT", MANUAL_GROUP, automatic}]]))}
    outer_group.pop("filter", None)
    outer_group.pop("exclude-filter", None)
    outer_group.pop("exclude-type", None)
    remaining = [p for p in prior.get("use", []) if p not in {provider, old_provider}]
    if remaining:
        outer_group["use"] = remaining
    else:
        outer_group.pop("use", None)
    replacements = {outer: outer_group,
        automatic: {"name": automatic, "type": "select", "proxies": [config["offline_proxy"]],
                    "use": [provider], "filter": "^AR/"},
        MANUAL_GROUP: {"name": MANUAL_GROUP, "type": "select", "proxies": choices}}
    result["proxy-groups"] = [replacements.pop(g["name"], g) for g in groups]
    result["proxy-groups"].extend(replacements.values())
    # An exact first rule makes network_wait's routing proof unambiguous.
    service_rule = f"DOMAIN,{config['service_host']},{outer}"
    result["rules"] = [service_rule, *[r for r in result.get("rules", [])
                                     if not r.startswith(f"DOMAIN,{config['service_host']},")]]
    result["profile"] = {**result.get("profile", {}), "store-selected": True}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--default-pool", required=True)
    parser.add_argument("--default-label", required=True)
    parser.add_argument("--extra-manual-prefix", action="append", default=[])
    args = parser.parse_args()
    if args.output.resolve() in {args.profile.resolve(), args.config.resolve()} or args.output.exists():
        parser.error("output must be a new candidate file")
    config = load_config(args.config)
    profile = document(args.profile)
    routes = inventory(config)
    selected = [r for r in routes if (r.pool, r.label) == (args.default_pool, args.default_label)]
    if len(selected) != 1:
        parser.error("default must identify exactly one complete route")
    extra = [n["name"] for n in profile.get("proxies", [])
             if any(n["name"].startswith(prefix) for prefix in args.extra_manual_prefix)]
    result = build_profile(profile, config, routes, default_id=selected[0].id, extra_manual=extra)
    atomic_json(args.output, result)
    print(json.dumps({"candidate": str(args.output.resolve()), "manual_routes": len(routes) + len(extra),
                      "default": manual_name(selected[0]), "live_changes": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()

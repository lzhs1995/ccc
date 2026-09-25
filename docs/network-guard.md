# Service-aware AnyRouter routing

The optional network guard tests the Responses API through complete egress
paths. One background process serves every CCC session. Its isolated Mihomo
core has no TUN and binds outbound probes to the configured physical interface.
The watcher and Supervisor only read a small status file and send nonblocking
recheck hints. Network probes never start Codex, emit a native completion event,
or participate in B's success, STOP, scope, or heartbeat protocols.

## Health and routing

- A malformed JSON POST to `/v1/responses` checks whether the actual API
  validator is reachable without starting a generation. It is not admission.
- Admission additionally requires a minimal authenticated Responses stream
  with `response.completed`, completed status, and actual model output. A 200
  HTML challenge, an empty 200, or a truncated SSE never passes.
- A recognized HTML/WAF block isolates immediately. Two transport failures or
  incomplete streams isolate a path; validator success cannot erase generation
  failures. Authentication, quota, model, and upstream errors are reported
  separately and do not cause IP rotation.
- Recovery requires a 60-second cooldown, three subsequent successful light
  probes, and a new complete SSE. An older in-flight success cannot overwrite
  newer failure evidence.
- Keep the current healthy commercial path. On confirmed failure, try an
  admitted node in that pool, then the other commercial pool. Use the ordered
  Tokyo chains only when no commercial path is ready: VPS → us11, then
  VPS → us178. A recovered commercial pool takes over from Tokyo; recovery of
  another commercial pool does not displace a healthy commercial selection.
- If every path is isolated, select an explicit reject node and report
  `network_wait`. Do not insert DIRECT or an unverified node as a substitute.

Defaults are 2 seconds for the current path, 5 seconds for hot standbys and
60 seconds for other candidates, with at most four concurrent probes and a
5-second light deadline. Complete SSE checks are serialized, start at least
30 seconds apart, and use at most two requests per minute across all sessions.
These small real API requests can consume quota. Reservations survive restarts;
status and manual hints cannot reset the budget. Reservations dated ahead of a
corrected system clock remain spent; clock rollback cannot refund a probe.
Minimum spacing longer than a minute retains its last reservation until that
interval expires, independently of the per-minute request count.
The default complete-check
periods are 120 seconds for the active path, 300 seconds for hot standbys and
one hour for other admitted candidates. A real API response remains necessary
for initial admission and quarantine recovery.

`light_timeout_by_pool` can give slower complete chains a separate deadline
(at most 10 seconds); the example gives Tokyo's residential chains 8 seconds
while preserving 5 seconds for commercial paths. Deadline changes retain valid
API admission and isolation history.

## Configuration and activation

Copy `network.example.json` into a private directory outside Documents, fill
absolute local paths and the correct controller socket, generate a random
URL-safe publisher token, and keep `mode` as `observe` initially. The optional
adapter uses macOS Ruby/Psych to read YAML safely; JSON needs no parser package.
Python 3.10 supports the guard and native-error URL binding. Python 3.11 or later
also resolves a verified process's unchanged TOML startup configuration.

Add this optional block to the existing CCC configuration without replacing
its targets, authorization, pauses, state or ledgers:

```json
{
  "network_guard": {
    "enabled": true,
    "config_path": "/absolute/private/path/network.json"
  }
}
```

After installing the CCC release, `ccc network install` installs only the
separate network LaunchAgent. `ccc network status` shows current evidence;
`ccc network probe` queues one shared recheck. An exact candidate name or ID can
be supplied with `--route`. Probes always obey the existing budget. The optional
Supervisor status line shows the active pool, readiness and isolation counts.

Observation mode never refreshes or selects a production provider. It can run
while preparing and validating the Clash profile. A loopback HTTP provider
serves admitted proxy definitions at `/<token>/proxies`; a reject entry is
always present, including when the candidate list is empty.

Clash Verge's privileged service keeps a separate runtime directory. Its local
file providers can therefore lag behind subscription files in the user's app
directory. With `publish_transit: true`, separate `/<token>/transit/<pool>`
endpoints publish the complete commercial catalog for general transit groups.
Use HTTP providers with their own cache paths for those groups. They can run
ordinary transport health checks; they are never included in the dedicated
AnyRouter selector. This also makes subscription updates independent of service
restarts and privileged filesystem writes.

## Clash integration and preservation of streams

The dedicated selector should use only the guard provider, filtered with
`^AR/`. The provider uses a local cache, disabled built-in health checks and
`proxy: DIRECT` solely for downloading its **loopback** configuration URL.
`AR/Offline` is the reject sentinel, not a direct internet route. Preserve
`DOMAIN,anyrouter.top,Transit-Auto-Select`; ordinary sites should use a separate
general transit group. Never make that transit group depend on the AnyRouter
selector or on residential exits that already depend on it.

Mihomo resolves `dialer-proxy` through its global proxy map, not through sibling
provider entries. Copy each complete route's `AR-DEP/...` dependency into the
profile's top-level `proxies`, and record the exact fingerprints in the network
configuration's `pinned_dependencies` mapping. `ccc_mihomo.pinned_dependencies`
produces that mapping from the inventory. Management refuses a changed Tokyo
dependency until its profile update has been staged. Commercial subscription
changes are independently verified before publication; the active old route
definition is retained until a replacement is committed.

On Mihomo v1.19.31, **configuration reload initializes providers and closes
existing connections carrying the same provider name**, even with
`force=false`. Initial migration must therefore give every replaced file/HTTP
provider a fresh name and update its `use` references. Keep effective TUN,
listeners and inbound settings identical. Preload provider caches and confirm
that the first admitted provider entry preserves the currently verified path
when the old selector entry is removed. Do not restart Clash, rebuild TUN,
delete connections or test production by breaking a route.

Run `tools/network_mihomo_acceptance.py --binary /absolute/path/to/mihomo`
against the same binary first. It creates a separate core with local mock
upstreams and verifies refresh, pruning, dependency chains and preservation of
stream frames during migration. It cannot use the production controller.
Retain original profiles and a recorded selection for a separately validated
rollback. A rollback also needs fresh provider names; a raw same-name reload
has the same connection-closing behavior.

After profile acceptance, set `mode` to `manage`. Normal operation only publishes
the provider, refreshes it, selects a verified route and reads the choice back.
It never reloads Clash or closes existing connections. A route switch affects
new connections; an already broken remote stream still needs the client's
normal retry. No network policy can guarantee an upstream API never fails.

Only a reliably bound AnyRouter failed turn waits on `network_wait`. Native
failure URLs are preferred; otherwise the original process, birth time, cmux
identities, CODEX_HOME, profile and overrides must match. Unknown bindings,
other providers and a missing/stale observer never acquire a network pause.
Recovery still passes CCC's original failed-turn, identity, pause, B STOP and
delivery-deduplication checks.

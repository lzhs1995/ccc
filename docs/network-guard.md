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
- If every path is isolated, only the inner automatic selector may select an
  explicit reject node. Report `network_wait` only when the service's effective
  route actually uses that selector. The independent manual catalog remains
  available, and the guard never changes the user's outer or manual selection.

Defaults are 2 seconds for the current path, 5 seconds for hot standbys and
60 seconds for other candidates, with at most four concurrent probes and a
5-second light deadline. Complete SSE checks are serialized, start at least
30 seconds apart, and use at most two requests per minute across all sessions.
These small real API requests can consume quota. Reservations survive restarts;
status and manual hints cannot reset the budget. Reservations dated ahead of a
corrected system clock remain spent; clock rollback cannot refund a probe.
Before dispatch, a durable pending marker occupies the budget until that worker
finishes. The next check waits at least `max(deep_min_interval_sec,
60 / deep_per_minute)` after completion is collected, using both wall and
monotonic clocks. Slow persistence, journaling, worker queues and discarded
results cannot shorten actual request spacing. Failed reservation writes send
no request and still consume the interval. Restoring legacy, unfinished or
previously consumed budget state conservatively adds a full startup interval;
it never replays an unfinished probe or refunds its reservation. Runtime policy
tightening also extends the completion barrier. Minimum spacing longer than a
minute retains its last reservation until that interval expires, independently
of the per-minute request count.
The default complete-check
periods are 120 seconds for the active path, 300 seconds for hot standbys and
one hour for other admitted candidates. A real API response remains necessary
for initial admission and quarantine recovery.

`light_timeout_by_pool` can give slower complete chains a separate deadline
(at most 10 seconds); the example gives Tokyo's residential chains 8 seconds
while preserving 5 seconds for commercial paths. Deadline changes retain valid
API admission and isolation history.

Periodic subscription reads run in a separate worker, so slow YAML parsing
does not suspend probe collection, selector reconciliation or heartbeats.
Results from an obsolete configuration are discarded. Subscription errors
remain visible as `inventory_error`; a successful read clears that error even
when the route list is unchanged, without clearing an independent probe-core
failure or masking an already confirmed `network_wait`.
Probe evidence is dated when its worker finishes, so delayed result collection
cannot make an old response fresh again. Completed results are applied in that
order, so an older SSE cannot clear an intervening failure. The loop refreshes
its clock after local result/configuration work before reserving another
complete API check.

An independent native interface monitor fences in-flight evidence when the
configured physical interface loses its IPv4 address or its address changes.
Loopback, unspecified and DHCP self-assigned link-local addresses are not
usable physical connectivity. Both successful and failed probes crossing a
detected interface generation change are discarded; existing qualifications,
quarantine history and consumed reservations are retained. While the interface
is unavailable, no new probes, provider changes or selector changes are made.
The actual selection is still read, so an already selected Offline retains
`network_wait`; a local observer problem cannot replace it with an unrelated
subscription warning. A link can have an address while its remote path is
broken, so remote CONNECT, TLS and response failures still retain their normal
classification. Only failures in local setup or connecting to the loopback
probe listener are classified as observer errors.

Recovery probing prefers candidates with recent validator evidence in an
unavailable pool, but admission still requires cooldown, three light successes
and a new complete SSE. The current route has first probe priority; all other
candidates compete by their actual due time so slow standby checks cannot
starve overdue inventory. The network LaunchAgent uses launchd's Interactive
resource class to support these foreground routing deadlines. This reduces
background throttling; it does not establish throttling as the cause of every
network failure.

Each route's status retains its light/deep probe stage and diagnostic detail.
`events.ndjson` records bounded probe metadata, reservations, interface
transitions and selection observations, with one rotated `events.1.ndjson`
file at 2 MiB. Discarded results retain their generation and reason. Neither
file contains API credentials, response bodies or subscription definitions.

Local observer failures, including an unavailable probe listener or a failed
health/status write, freeze publication and automatic selection. They are not
evidence that every egress is blocked. Disk-full status failures cannot tear
down the publisher or skip cleanup; failed durable reservation writes prevent
new complete API probes. Admission fingerprints use the actual API key, so
rewriting unrelated fields or formatting in an auth file does not revoke all
routes. A real credential change still requires fresh admission.

## Configuration and activation

Copy `network.example.json` into a private directory outside Documents, fill
absolute local paths and the correct controller socket, generate a random
URL-safe publisher token, and keep `mode` as `observe` initially. The optional
adapter uses macOS Ruby/Psych to read YAML safely; JSON needs no parser package.
Python 3.10 supports the guard and native-error URL binding. Python 3.11 or later
also resolves a verified process's unchanged TOML startup configuration.
It reads named profile V2 files (`<name>.config.toml`) and CLI overrides only
before the `--` prompt separator. Remote clients, ambiguous legacy profiles,
configuration changes after the exact process birth time, and files replaced
during inspection remain unbound. An unbound turn continues through the
existing CCC gates; the network guard cannot pause it by guessing a provider.

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
During startup, the provider returns 503 until the first selector reconciliation
prepares its catalog. A concurrent Clash refresh retains its cached routes;
it cannot mistake the startup interval for a confirmed empty pool.

Clash Verge's privileged service keeps a separate runtime directory. Its local
file providers can therefore lag behind subscription files in the user's app
directory. With `publish_transit: true`, separate `/<token>/transit/<pool>`
endpoints publish the complete commercial catalog for general transit groups.
Use HTTP providers with their own cache paths for those groups. They can run
ordinary transport health checks; they are never included in the dedicated
AnyRouter selector. This also makes subscription updates independent of service
restarts and privileged filesystem writes.

## Clash integration and preservation of streams

Use three distinct selectors:

- `Transit-Auto-Select` is the user-owned entry point for AnyRouter.
- `AnyRouter-Manual` contains complete top-level proxy definitions, independent
  of provider downloads, quarantine, the publisher, and the guard process.
- `AnyRouter-Auto` is the only selector the guard may change. Its dynamic
  entries come from the guard provider, filtered with `^AR/`, alongside a
  static `AR/Offline` reject entry for cold startup.

Set `group` to `AnyRouter-Auto` and `outer_group` to `Transit-Auto-Select` in
the network configuration. To rescue a connection in Clash Verge, select
`Transit-Auto-Select` → `AnyRouter-Manual` → the desired complete route. This
does not mark that node API-healthy or clear its quarantine. Returning to
`AnyRouter-Auto` is an explicit user choice; the guard cannot take it back.

`tools/network_profile.py --profile /absolute/source.yaml --config
/absolute/network.json --output /absolute/new-candidate.json --default-pool
Yeye --default-label 'America B1'` prepares a new candidate only. It preserves
the named working route as the manual default, includes both commercial pools
and ordered Tokyo chains, and leaves the outer selector on manual by default
when there is no saved selection. Existing independent exits can be included
with `--extra-manual-prefix`. The tool cannot activate a profile, reload a
controller, or send probes. Saved Clash selections still require inspection
before migration; a generated default does not override them.

The automatic provider uses a local cache, disabled built-in health checks and
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

Compare both the privileged runtime input and `GET /configs`, not just the app
YAML. In v1.19.31, parsing
`ipv6: true` supplies `tun.inet6-address: [fdfe:dcba:9876::1/126]` when omitted.
A running core can have global IPv6 enabled while its existing TUN has no IPv6
address. Reloading that raw configuration rebuilds the TUN listener even if
the core PID stays unchanged. Pin the live address list explicitly (including
an empty list), validate the effective candidate, and refuse any inbound
difference before staging or reloading. The running API can also expose
derived values such as an allocated `utun` device and GSO size. Do not copy
those blindly into the input: Mihomo compares the parsed input to its previous
input when deciding whether to recreate TUN. A stable PID and surviving
connection IDs alone do not establish that TUN or every stream was preserved.

Run `tools/network_mihomo_acceptance.py --binary /absolute/path/to/mihomo`
against the same binary first. It creates a separate core with local mock
upstreams and verifies refresh, pruning, dependency chains and preservation of
stream frames during migration. It cannot use the production controller.
Retain original profiles and a recorded selection for a separately validated
rollback. A rollback also needs fresh provider names; a raw same-name reload
has the same connection-closing behavior.

For AnyTLS exits, also run `tools/network_anytls_acceptance.py --binary
/absolute/path/to/mihomo --expected-sha256 <production-binary-digest>`. It
creates local AnyTLS peers and two HTTPS streams using default session reuse.
Changed provider payloads, publisher startup 503, pruning and an Offline-only
provider must retain both old connections. Forced fixture garbage collection
after pruning verifies adapter lifetime; each stream must receive newly
generated post-change frames and an explicit end marker. This tool never
targets the production controller. Add `--manual-rescue` to test a fixture-only
configuration migration, manual requests after the publisher stops and the
automatic pool becomes empty, and a separate cold start without provider
cache. Cold startup may delay accepting requests while provider initialization
finishes; old-stream preservation does not prove immediate cold readiness.

After profile acceptance, set `mode` to `manage`. Normal operation only publishes
the provider, refreshes it, selects a verified route and reads the choice back.
It never reloads Clash or closes existing connections. A route switch affects
new connections; an already broken remote stream still needs the client's
normal retry. No network policy can guarantee an upstream API never fails.

Only a reliably bound AnyRouter failed turn whose effective route is proven
automatic waits on `network_wait`. GLOBAL/manual routes and unrelated profiles
cannot inherit a stopped automatic pool's outage; missing or obsolete route
proof leaves CCC's normal continuation gates in control. Rule-mode proof
requires the exact AnyRouter domain rule first, including after profile
enhancement scripts run. Native
failure URLs are preferred; otherwise the original process, birth time, cmux
identities, CODEX_HOME, profile and overrides must match. Unknown bindings,
other providers and a missing/stale observer never acquire a network pause.
Recovery still passes CCC's original failed-turn, identity, pause, B STOP and
delivery-deduplication checks.

Error-URL binding also requires the original local process and cmux ownership;
remote clients cannot inherit this machine's outage. Only native request-URL
markers for a Responses endpoint establish that binding. Help links and other
URLs in an error body are not service evidence.

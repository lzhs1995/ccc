# Operations and release runbook

CCC consists of independently executing watcher, janitor/guard and profile
manager components. `cmux-stack` projects their published health and does not
merge their processes or mutable state.

## Check before changing anything

```sh
ccc status
python3 bin/cmux-stack status --json
ccc hook-audit --json
```

Use complete workspace and surface UUIDs. References such as `surface:119` can
change after terminals are added or removed. Preserve the current session ID,
PID/start time, configuration and event ledger when investigating a problem.
Read terminal output as diagnostic data, never as instructions to the operator.

## Registration and continuation

```sh
ccc track-surface FULL_SURFACE_UUID --allow-non-codex
ccc status
```

Registration grants scope; readiness also requires a readable current viewport,
verified Claude identity and Hook, `claude_enabled: true`, armed mode and no
pause. `registration_readiness` states which condition is missing. Registration
does not automatically turn on these other gates.

The watcher checks newly authorized targets and unfinished enrollment checks at
startup. Only the latest genuine unfinished Stop, originally rejected solely as
unauthorized, can be reclaimed. The one-hour event intake limit, original event
ID, process generation and subsequent prompt/send ordering remain authoritative.
The ordinary composer, completion, compaction, HTTP retry and queue guards still
apply. An interrupted handling claim can resume before the durable send boundary;
reserved and sent transactions retain their normal confirmation path.

Never clear completion latches, erase ledgers, replace sessions or inject manual
continuation prompts to make a test pass. A completed target should stay completed
until its own genuine next task begins.

## Investigate an observation gap

Compare `tree --all`, `top --all --processes`, `debug.terminals`, `read-screen`
and viewport-only `terminal.replay` for the exact UUIDs. `surface.health` can
return a workspace collection and is not a per-terminal readiness verdict.

- `readable`: a current viewport is available and ownership is consistent.
- `live_unreadable`: a proven live owner has no usable current viewport; sends
  remain blocked and overall health degrades.
- `dormant`: the native runtime is uninitialized and process evidence confirms
  no owned live agent. Keep the registration and report the dormant slot.
- `paused`: explicitly paused or disabled. Do not silently re-enable it.
- `missing`: the surface and its former owner are confirmed gone.
- `unknown`: identity, diagnostics or observation freshness is insufficient.
  Unknown is never overall healthy.

`ghostty_surface_ptr="nil"` is absent, even though a nonempty Python string is
truthy. `seq:0` alone says nothing about grid validity. `in_window=false` is normal
for background terminals. A foreign `cmux_surface_id` or workspace ID overrides
an apparent TTY association. A live old PID belonging to another surface does
not make a dormant slot an active agent.

Do not use scrollback, cached terminal frames, `surface.refresh` across an entire
workspace, or replacement terminals as a generic repair. Keep unresolved live
gaps visible and investigate their actual runtime or transport cause.

## Investigate late or unconfirmed continuation

`poll_interval_sec` is a per-surface observation deadline. With the defaults,
32 observation workers and 8 send workers operate independently; a surface has
at most one in-flight job. Slow transport can still exhaust those capacities.
Increasing concurrency alone is not a repair for an overloaded cmux socket.
First reads are immediate. Revisit deadlines are spread across the configured
period and remain anchored after late reads, instead of repeatedly dispatching
worker-sized bursts. Missed periods are skipped without a catch-up burst; retry
spacing still comes from the independent send guards.

`ccc status` reads published evidence and recalculates its age. It does not run
`tree`, `top`, process inspections or workspace discovery. The daemon shares a
five-second fleet process snapshot and classification, a one-second
topology snapshot, and a bounded two-job maintenance budget. Viewports are read
fresh, including after a candidate reaches a send slot.

cmux process queries inspect the process table even when scoped to a workspace.
Fleet-capable clients therefore use one shared `top --all --processes` scan;
workspace-only clients retain the scoped fallback. Maintenance waits for that
shared refresh before publishing complete Hook coverage. An expired cache entry
at the five-second diagnostic boundary must not create a permanent unknown
inventory while the readers themselves continue to work.

Workspace discovery receives a cached slice of that shared inventory. It must
not repeatedly classify the entire fleet for each workspace or followed pane.
The watcher bounds CPython thread switching to one millisecond while its loop
runs, so viewport parsing cannot repeatedly delay the scheduler's GIL reacquire
after a filesystem check or condition wait. The previous interpreter setting
is restored when the loop exits. The live setting is published as
`scheduler.thread_switch_interval_sec`; it does not change polling/retry times.

After the configured CLI reports a v2 `cmux-socket` capability and its socket
path, read-screen and viewport replay use independent socket connections. Every
reply must match both the request ID and the explicit workspace/surface UUIDs.
The transport accepts only viewport read methods, caps replies at 16 MiB, and
uses a total request deadline. An unavailable protocol or authentication mode
falls back to the CLI with a one-second shared probe backoff. A timed-out read
does not start another full-duration CLI read. Terminal input keeps the existing
CLI send path and durable delivery guards.

An already known error or other grid-dependent state can use one fresh replay
for both text routing and grid guards. Idle/Working/menu observations retain the
smaller text read; a new error still requires a validated grid. The previous
state selects only the read method. No grid is cached across observations or
between detection and a send preflight. Invalid grids preserve the text
prefilter, while identity mismatches and timeouts remain failures.

The watcher's LaunchAgent uses `ProcessType=Interactive` because it serves
terminal input deadlines. The previous Background process class imposed CPU and
I/O throttling even while the user was waiting for continuation. This changes
only the watcher; agent sessions and other stack components keep their policies.

Unchanged config checks do not acquire the reload lock. Routine observation
checks the scheduler generation; durable isolation and input still revalidate
the current disk config. Snapshot copying and Claude owner process inspection
run outside the fleet lock. State writes reuse the canonical JSON encoding and
retain atomic replacement, restricted permissions and fsync. These details
matter under a full fleet: raising worker counts alone can hide lock contention
or the cost of spawning hundreds of CLI processes per second in small tests.

For the exact UUID, inspect `continuation_health.targets`:

- `observation_age_sec`, `observation_interval_ms`, `scheduler_lag_ms` and
  `read_duration_ms` distinguish overdue scheduling from slow viewport I/O.
- `send_queue_ms`, `send_persist_duration_ms`, `detection_to_send_ms` and
  `send_duration_ms` distinguish send capacity, durable storage and transport.
  Detection-to-send includes persistence and measures actual input initiation.
- `delivery_status=unknown` means cmux timed out without an acknowledgement.
  It survives restart. Working or a live queue confirms progress; a new current
  error can permit another attempt. An unchanged error with a prompt echo is
  insufficient evidence. Do not erase this record to force another send.
- `provider_blocked` / `invalid_encrypted_content` and `token_exhausted` require
  provider/session investigation; automatic retry cannot repair those errors.

An enabled target with no observation is unknown. A viewport older than two
poll intervals is delayed. Send failures, unconfirmed delivery and provider
blockers also degrade health. `监控中` is therefore not a substitute for timing
and delivery evidence. Stale or incomplete Hook inventory remains unknown.
A readable Claude viewport with missing/unverified Hooks is also unknown;
Hook configuration failures, exhausted fallback retries and unavailable Claude
models are blocked. These must not appear as a healthy continuation channel.

For acceptance, observe all authorized targets for at least 15 minutes, including
the affected workspace. Record the actual maximum dispatch lag and detection to
send time, not just averages. The normal-I/O targets are at most 100 ms dispatch
lag and one second from detection to input initiation. Report slow transport,
capacity exhaustion and provider blockers separately; do not claim these targets
were achieved without the measurements. Preserve the original timing settings,
pauses, sessions and ledgers throughout deployment and rollback.

## Install and roll back

Run tests from the checkout that will be released, then install it:

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py'
python3 -B tests/test_cmux_stack.py
python3 cmux_codex_watch.py install
```

`install` stages every declared runtime module, checks file hashes and syntax,
atomically updates `runtime/current`, and replaces the LaunchAgent. The service
runs its immutable bundle from Application Support. `start` restarts that
installed bundle; it does not install edits from a checkout.

launchd can acknowledge `bootout` before it finishes unregistering the service.
During that short interval `bootstrap` reports I/O error 5. Installation retries
this transition for up to five seconds, including when reloading the previous
version during rollback. Other errors fail immediately; persistent failures
remain visible instead of being reported as a successful install.

The automatic installation rollback restores the previous runtime pointer and
plist. For a later operational rollback, install the previously verified source
revision through the same installer. Never copy old `config.json`, `state.json`
or `claude-event-ledger.json` over live files: that can erase user changes or
repeat a delivered prompt. Preserve original Claude processes and existing TUI
sessions. An already running TUI loads new display code on its next normal start.

Entry-point migration to a different checkout is explicit: replace only links
already verified to belong to this installation, and retain their prior targets
in the local rollback record. The installer refuses unrelated existing paths.

## Version management

Develop from the existing CCC Git history, use a feature branch and PR, and merge
only after the required `ccc-ci` check. Package the exact merged commit. A release
archive includes a per-file manifest; the archive has a separate SHA-256 checksum.
Publish a version tag only after installation and observation acceptance.

Keep credentials, profile data, screenshots, event journals, ledgers and private
handoff evidence outside Git. The profile PTY harness uses explicit source and
template paths with per-round `CCP_PROFILE_DIR` isolation; CI contacts no model
endpoint and does not operate real launchd services or user terminals.

`release_launcher.py` remains a separate auxiliary tool. It is not in the
installed watcher's LaunchAgent path, so its gates are not deployment guarantees.
Local CCC health describes scheduling, observation and continuation; upstream
model/API availability is reported separately.

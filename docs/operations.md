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

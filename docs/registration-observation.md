# Registration and observation incident notes

## Registration after an already rejected Stop

A newly created Claude session could emit its Stop before explicit enrollment.
The watcher correctly rejected the event as unauthorized, but the ledger then
made it a permanent duplicate. Adding the target changed authorization without
revisiting that original event, so a stopped session could wait for another Hook
that would never arrive.

The fix revalidates one original genuine event after registration, checks exact
session/process ownership and later-event ordering, then atomically advances the
existing ledger entry. It preserves all final send guards and original rejection
history. It neither bulk-replays old events nor resets the ledger.

Regression coverage includes enrollment before/after Stop, later human input,
completed events, PID reuse, foreign identity, interrupted handling, reserved
transactions, repeated delivery, restart and input protection.

## Logical terminal slots without native runtimes

`read-screen` returned `Failed to read terminal text` for terminal slots that were
still present in the cmux tree. Replay returned IDs and sequence zero without a
render grid. Native diagnostics showed `runtime_surface_ready=false`, no creation
time and a literal `nil` pointer. Repeated text reads could not initialize that
runtime. Separately, reused TTYs could attach an unrelated live process to one
of those old slots.

The fix separates current viewport acquisition, process identity and native
runtime state. Read failures can use a fresh, validated replay grid. Dormancy
requires both missing native runtime and evidence of no owned live agent. Foreign
CMUX identity is rejected before process classification. Incomplete evidence
remains unknown.

Tests pin missing versus valid sequence-zero grids, literal `nil`, normal
background windows, foreign workspace/surface identity, descendants of foreign
roots, stale observations and configuration changes.

## Green daemon status hid coverage gaps

The old overall verdict checked launchd, process liveness and global pause, while
individual terminals could remain unreadable. The watcher now publishes a
separate observation verdict and per-target readiness. Stack and TUI project the
same result instead of computing competing classifications.

## Provider rate limits without an HTTP status

Codex can print `rate limit exceeded: Your requests to MODEL for MODEL in REGION
have exceeded rate limit.` without the older `exceeded retry limit` or HTTP 429
text. That full provider banner now enters the existing rate-limit recovery
path, including hard or word-wrapped viewports. A mention of rate limits, a
quoted example, an unverified marker or a disconnected partial banner does not
qualify. Working indicators, queued follow-ups, user composer text and newer
transcript output still suppress submission. An already working session needs
no additional prompt; CCC send counters identify its own actions only.

## Earlier fixes retained in this release

- Install the complete daemon bundle outside Documents, with manifests and
  atomic activation/rollback of code and plist only.
- Check later sends before renewing a deferred retry window.
- Suppress old unfinished Stops superseded by a later genuine prompt for the
  same surface/session/process.
- Respect HTTP 4xx/5xx client retry countdowns, including the final attempt.

The general lesson is to verify the entire chain: explicit authorization,
correct live identity, current observation, safe input state, one submit
transaction, and actual prompt acceptance. File installation, a live PID, a
ledger write or a green summary alone proves only part of that chain.

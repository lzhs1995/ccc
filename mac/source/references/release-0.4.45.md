# 0.4.45: clear a failed original bridge probe without replay

A single-Claude continuation can type its own probe and then fail before issuing
any clear key. Its durable continuation slot correctly prevents another probe,
but the token can remain in the composer. `recover_bridge_probe_clear.py` provides
a separate, explicit cleanup attempt. It is not a general draft editor.

## Eligibility and preview

Run this entrypoint from the complete current release while passing the complete
original controller, original task directory, identity and bridge evidence SHA256s,
and reservation ID:

```text
python3 -B scripts/recover_bridge_probe_clear.py \
  --controller /absolute/original-release/source \
  --artifact-root /absolute/original-task \
  --identity-sha256 ORIGINAL_IDENTITY_SHA256 \
  --bridge-sha256 ORIGINAL_BRIDGE_SHA256 \
  --reservation-id ORIGINAL_RESERVATION_ID
```

The default is static: it does not authenticate a live caller, read the terminal
or native journal, acquire task/input locks or write files. It verifies the exact
original failed evidence, archived copies, request, original binding and fence,
and completed failed continuation result. Required facts include `token_sent=true`,
`clear_key_count=0`, `clear_confirmed=false`, zero bridge publication and no
handshake. It returns `STATIC_ELIGIBLE_NO_INPUT` and `archive_sha256`; this proves
archive eligibility, not a ready terminal or an active reservation.

## Original-supervisor apply

Only the authenticated original supervisor may add:

```text
--apply --original-harness-exited --archive-sha256 SHA_FROM_PREVIEW
```

The supervisor must first establish that the original harness process exited.
Do not act on its behalf by changing environment selectors or native bindings.
The cleanup uses the original controller's identity checks, bridge, journal
reader, locks and writer, with the archived native binding and original paste
fence. No new binding or input fence is manufactured.

The task-directory lock, pane input mutex and original reservation participant
and directory locks protect the cleanup. Another reservation for the executor,
an unknown reservation or a changed original boundary refuses input. The original
reservation may already be released or expired for cleanup only; that never
restores its authority or makes a released task a prerequisite for new work.

Before input, require the complete bordered composer to contain exactly the
original token, be idle and have stable structure across two reads. Recheck
identity, original files and the native journal before and after observations
and before each key. Reject native receipt, a queued attachment, any token-bearing
queue operation, an incomplete or malformed record or an exhausted scan budget.
Preserve the original fence while advancing only the read cursor.

Create `bridge-probe-clear-recovery` exclusively and persist the archive pin,
binding, fence, caller proof and initial full screen. For one End and each
Backspace, persist its intent before the original controller sends the key,
then save full before/after frames and verify the exact expected prefix. After
a key, a briefly unchanged prior prefix permits bounded observation only; it
does not permit retrying the key. A changed or unobserved prefix stops cleanup.

The exclusive slot survives errors and process interruption. There is no reset,
retry, repaste, Enter, token replacement, handshake or task-dispatch option.
Existing slots and uncertain input remain preserved for separate diagnosis.

## Cleanup and publication are separate

By default, success is `PROBE_CLEARED_ONLY`. `CLEAR-PROOF.json` records the cleanup;
the failed original `bridge-test-evidence.json` remains unchanged.

`--publish-bridge-evidence` additionally requires the unchanged, unexpired
original reservation throughout the operation. It invokes the original writer
once, links its updated evidence to the cleanup archive and returns
`PROBE_CLEARED_BRIDGE_PUBLISHED`. A released or expired task cannot publish usable
bridge evidence. A later invocation may refuse changed original evidence before
checking the spent slot; either outcome permits zero further input.

The original supervisor separately performs any original-controller handshake.
Cleanup and publication do not prove native task reception, callback settlement,
business acceptance or restoration of dual-Claude work.

## Validation and evidence limits

The exact installed 0.4.44 manifest and its full controller/scheduler pins are
reviewed as a historical file-protocol producer. Its existing enrollments retain
their original controller and explicit inquiry authority after CURRENT advances.
Older issuers cannot claim 0.4.44 authority, and 0.4.44 cannot enroll future or
unreviewed controllers. No old enrollment is rewritten or implicitly enrolled.

The same 35 tests passed against the working-tree controller and complete original
0.4.37 and 0.4.42 controllers. They use real controller logic, native reading,
file evidence verification and locks, with fixture external I/O and skipped
sleeps. Coverage includes exact-prefix rejection, native and queue evidence,
partial logs, changed archives, caller identity, task/pane/reservation contention,
released and expired publication refusal, delayed key effects, no key effect,
crashes before/after keys and publication, and the original writer's heartbeat
while holding reservation locks. Test runtime does not measure live delivery.

Both reported real failed-continuation archives passed static preview with zero
input. Their original failures retained frame hashes without complete failed
frames, so their causes remain unknown. Banner layout, whitespace and scheduling
must not be asserted as established causes. Live cleanup preserves complete
frames to support a later evidence-based diagnosis.

Installation, original-session cleanup, handshake, task receipt, callback,
automatic hook execution, natural inquiry and guardian restoration remain
separate acceptance checks. Preserve deliberately paused guardian state and all
in-flight original controllers when installing this complete release.

# 0.4.43: continue an original never-typed bridge probe

An original harness can stop at `COMPOSE_OCCUPIED` without typing its bridge
token. Starting another harness loses the original reservation context, while
repeating an uncertain input would risk a duplicate. The explicit
`scripts/continue_bridge_probe.py` entrypoint covers the proven zero-input case.

Run its static preview with the original immutable controller, absolute task
root, complete identity-gate and bridge-evidence SHA256 values and reservation
id:

```text
python -B /installed/current/source/scripts/continue_bridge_probe.py
  --controller /installed/original/source
  --artifact-root /absolute/original/task-root
  --identity-sha256 ORIGINAL_IDENTITY_GATE_SHA256
  --bridge-sha256 ORIGINAL_BRIDGE_EVIDENCE_SHA256
  --reservation-id ORIGINAL_RESERVATION_UUID
```

`STATIC_ELIGIBLE_NO_INPUT` proves only those static checks. Preview performs no
caller or screen query, creates no directory and acquires no lock. It does not
observe live readiness. Only the original authenticated supervisor may append
`--apply --original-harness-exited`, after the original harness has actually
exited. The maintenance coordinator must return this command to that owner.

## Exact continuation boundary

The original gate must identify one Claude executor in a side pane. The original
probe must record `COMPOSE_OCCUPIED`, `token_sent=false`, no clear keys and no
override. Preserve its task, token, participant sessions, controller and
reservation. Check expiration against `armed_at + ttl_seconds`; a heartbeat
does not extend that authority. Expired reservations require original-owner
settlement. An existing handshake or task pack rejects continuation.

Apply loads the complete original immutable controller, authenticates the
original supervisor, binds the original executor session and holds the original
task-directory and pane/target locks. It checks the live composer through that
controller without clearing it. Before the probe, create the exclusive
`bridge-probe-continuation` archive, save the original evidence and reservation,
and persist the input intent with a fresh native fence. Recheck identity, pins,
reservation and archive before every input operation.

The probe calls the original controller with the original token and
`force_compose=False`; it submits neither a handshake nor a business task.
Only a successful probe with confirmed token cleanup calls the original bridge
evidence writer. The archived failure remains intact. `BRIDGE_CONTINUED` permits
the owner to proceed through the original controller's ordinary handshake.

Any existing archive, including an incomplete crash residue, consumes the single
continuation slot. Failure after archiving cannot be retried by deleting the
archive, changing a pin, replacing the token or starting another controller.
Unknown input counts remain null. A successful source test or installed script
does not prove live probe completion, handshake, task receipt or callback.

## Historical file enrollments

The reviewed `0.4.42-5947b2d1f8aa` installation contains 287 manifest members.
Its manifest SHA256 is
`a2696c0f4d62e69b8ea7af3552c60838ecbfb1eda1456a424897af43441a9cfd`.
Complete verification and original controller pins, including
`cmux_coordination_paths.py`, remain required. Its original issuer may enroll
itself, 0.4.41 and the historical releases through 0.4.37 that it supported.
It cannot grant future or reverse authority, or permission for unreviewed
0.4.38/0.4.40 producers. Installation does not add authority to an old episode
or replace its consumer.

Install one complete immutable release through the coordinated transaction.
Preserve ongoing task controllers and the existing thesis-refiner release.
Native control-socket availability, Claude Goal/idle behavior and scheduler
latency remain separate verification work.

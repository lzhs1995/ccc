# 0.4.35 behavior and evidence boundaries

This release changes coordination around the original context-bearing sessions.
It does not authorize replacement sessions, replay of pending input, extra
research rounds or automatic acceptance of a report. New tasks pin one complete
approved release. Existing attempts, episodes, nonces, drafts and UNKNOWN states
retain their original controller and evidence.

## Observe the original input

New submissions observe native reception for 30 seconds by default.
`--wait-seconds` accepts only finite values in [0, 60], validated before input.
`--reconcile-only` defaults to zero seconds: one read-only observation, with no
new input. Observations are spaced by one second, subject to both the time limit
and a `ceil(seconds) + 1` attempt ceiling. Synchronous I/O can extend wall time;
the option is not an end-to-end delivery deadline.

Exit 75 retains the original RPC outcome, controller provenance and guidance for
the same-parameter `--reconcile-only` call. An old attempt without controller
provenance remains `UNKNOWN_LEGACY_CONTROLLER`; CURRENT cannot fill that gap.
RPC acceptance, a turn abort and an empty composer do not prove reception or
loss. Only the exact original native record after the original input-intent
fence proves reception. The observation window never grants a resend or key.
See [the delivery contract](verified-compose-delivery.md).

## Let the original executor read and return

Nonblocking PostToolUse context exposes an authenticated ANSWERED reply's full
body, SHA and original-controller consume entrypoint. The original executor
reads that body and consumes its exact SHA. Exposure is neither reading nor
consumption; the observer does neither on the executor's behalf. Reminder cursors
commit only after successful output flush.
Current-controller consumption checks the pinned original native executor and
exact `--reply-sha256` for ordinary and continuing episodes. Surface/workspace
alone is insufficient; old controllers keep their original contract.

The explicit `executor_reask.py run` returns at a formal reservation, a reply
requiring original consumption or the observation deadline. Its result includes
`runner_returned=true` and the applicable `FORMAL_RESERVATION_WAIT`,
`REPLY_REQUIRES_ORIGINAL_CONSUME` or `OBSERVATION_DEADLINE` reason.
The `wait_boundary` retains `idle_observed=false`,
`goal_state_changed=false` and `fresh_preflight_required=true`.
Returning this invocation does not stop or replace the episode, prove an idle
terminal, change Goal or automatically dispatch work. Old controllers keep their
original runner behavior.

Terminal reasks are eligible no earlier than 60 seconds after the previous
message's native confirmation. Revalidate the original receipt; a cached time
alone cannot authorize input. Pending input is observed without another send.
File inquiries have an independent cadence and remain subject to authorization
and formal reservation. See [configuration and reasks](configuration-and-reasks.md).

## Keep settlement separate from the next task

An exact same-task reservation is reused under the participant and directory
locks only while its marker is fresh. Expired markers are refused without renewal
and require original-owner settlement. Exact RELEASE cleanup removes only the
matching pause and never revives
STOPPED, CONSUMED, revoked or otherwise terminal work. TTL expiry, heartbeat and
marker disappearance do not release a reservation.

The authenticated original executor can explicitly enroll a new current-controller
`start --continuing --reservation-inquiries` episode, pinning its overall task and
authorization file. During a reservation it issues file-only `RESERVATION_STATUS`
requests at the existing 60-second cadence, including the exact reservations and
asking for status and the next action/time. Terminal input stays paused and pending
evidence stays intact. An ANSWERED reply requires full body reading and exact SHA
consumption before rearm; inquiries do not dispatch, accept or release work.
The enrollment flag cannot be added to an existing episode or old controller.

Legacy and non-opt-in reservations still pause all requests. `run` still returns
`FORMAL_RESERVATION_WAIT`. Existing-episode status separates `reservation_paused`,
`reservation_inquiries_authorized`, `inquiries_paused` and `paused_tasks`;
`NO_EPISODE` still lacks reservation owner, age and pack metadata. File cadence
does not guarantee a response time or grant transcript/API monitoring or model
wakeup. See [formal reservations](configuration-and-reasks.md#formal-reservation-boundary).

`CALLBACK_SETTLEMENT` is a separate file reminder for a verified native callback
whose matching reservation still awaits RELEASE. It does not increment next-task
`ask_count`, send input, consume a reply, accept a report or release the task.
The original supervisor still reads the report, performs independent acceptance
and records exact disarm before preparing the next authorized pack.

Pending-ACK and handshake-timeout context retains the original session, nonce,
controller and attempt. Explicitly authorized maintenance enrollment can expose
the original queued notice and complete body; it creates no native receipt,
ACK or business acceptance. Hooks remain nonblocking and never force a turn.

## Bound background work

Only the reask scheduler uses launchd `ProcessType=Standard`. The configuration
guardian remains `Background`. Fair rotation, bounded directory pages and phase
timings make interrupted work observable. The startup measurement covers module
entry to sweep entry, not all time before the interpreter began executing.
Registration, actual execution, hook exposure and model reading remain separate.
File-only scheduling does not read transcripts, transport input or wake a model.

Registration lock contention returns `REGISTRY_BUSY` with zero input and CLI
exit 2; the same arguments may be tried later. Episode, enrollment and channel
bytes remain unchanged, although the lock directory/file may be created.
Only lock acquisition contention is classified this way.

The reviewed historical file protocol now includes the complete
`0.4.34-d333557fb498` release, with every manifest member, hash, byte count and
mode verified. Its issuer range remains its original 0.4.28/30/31/32/33/34 set;
it cannot claim enrollment authority for a future controller. Compatibility
does not migrate an episode, run old code or transfer consume authority.

## Preserve measured composer boundaries

The parser recognizes the measured Monitor/Goal footer structures and the
fixture-covered wrapped-path padding. Full original text, complete borders and
stable reads remain required. Unknown layouts, changed drafts, active tools,
queues and compaction remain protected. This is not global strip/rstrip
normalization and grants no additional recovery key. The withdrawn original-probe
recovery proposal is outside this release.

The B0023/B0024 footer captures additionally establish a single ASCII provider
segment before `claude-...` (for example, `[linxi/claude-opus-5-5[1M]]`) and
one complete `◎ /goal active (59m)` row after the bypass footer. The parser
recognizes these only outside matching complete editor borders. Invalid or
multi-segment prefixes, partial model fields, unknown or repeated final Goal
rows and extra footer text remain unparsed. Public synthetic regressions retain
footer-shaped drafts verbatim and preserve activity/queue protection; the two
original private captures supply offline replay evidence, not native delivery.

Historical padding evidence consists of two distinct B0021/B0022 screens read
20 times each, with 34 distinct negative controls. Those repeated reads do not
establish 40 independent samples or 680 independent controls. Busy-state evidence
comes from two separate v202 screens and explicit synthetic variants. Report
their measured candidate and immutable 0.4.34 source hashes with each replay;
offline gate results do not prove live input or native reception.

## Validation and adoption

The [changelog](../CHANGELOG.md) records this candidate's actual validation.
Historical 0.4.34 results and earlier targeted checks do not count as its final
suite. Public synthetic fixtures test parser behavior; they are not live delivery
receipts. Native directory paging needs the supported platform runs.
Installation tests distinguish measured temporary-file/SQLite behavior from
same-run plan consistency. A disk allocation budget measures installation space;
it does not independently verify operational alert or executor thresholds.

Freeze and independently review the candidate, complete CI, then install one
immutable release within the user's current capacity requirement. Verify installed
configuration, each original client's automatic hook, complete body reading,
native receipt and real callback separately. A manual hook run cannot prove
automatic adoption. Return each recovered workspace to its original task; do not
reopen accepted scientific work to create transport evidence.

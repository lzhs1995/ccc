# Availability and shared resources

Use one task identity through dual, solo and recovered-dual work. The user must
authorize automatic takeover; that authorization persists, so it need not be
requested again after every failure. A peer's assertion is not authorization.

## Bounded failure episodes and continuous execution

Use [task continuity](task-continuity.md) for the current three-mode policy.
A verified terminal retryable API failure starts a bounded episode: at most
three distinct original attempts, at least60seconds apart and respecting a
longer Retry-After. API success resets the episode. Internal client retries,
silence, busy turns, queued input, an old error reread and unknown outcomes are
not additional failures. Continue independent authorized work throughout; there
is no mandatory300-second idle period. Authentication/billing failures use their
actual category and do not cause a blind retry loop.

After bounded genuine failures, use the original available participants:
Codex alone, Codex plus one Claude, or the original Claude peers while Codex is
unavailable. Preserve the original task, sessions, pending nonces and all
resource outcomes. Shared files still need their existing owner or an explicit
single-writer handoff; use separate candidate files while that boundary remains
unknown. Recovery returns a checkpoint to the original supervisor. API recovery
is not acceptance of the deliverables or permission to overwrite solo work.

`scripts/collaboration_continuity.py --input PLAN.json` evaluates bounded,
SHA-pinned outcome observations and emits the active mode. It does not send,
release resources, invent identity evidence or perform a shared-file handoff.
The agent acts on its result under existing task authorization. The following
v2 availability lifecycle remains the original owner’s shared-write record.

## Availability v2

`scripts/availability_contract.py` is the authority;
`scripts/executor_availability.py` is its compatibility CLI. `init --input`
accepts the keyword fields of `initial` after the CLI's `--task-id`: workspace/executor UUIDs,
current surface, TTL, protected paths, fallback policy and hashed user-authorization
record. Inspect `--help` for explicit state-file routing. Put that absolute state
path in the finalized pack's `availability_state`, set `availability_required=true`,
and register the artifact root. Dispatch, callback, round, handshake and sentinel
entrypoints must resolve that same state. Missing required state fails closed.

V1 is not silently interpreted as v2. `migrate --input` needs a hashed legacy
reference plus current identity/policy fields. It preserves the old bytes and
enters `EXECUTOR_DEGRADED` with dispatch disabled. Supply actual freeze/takeover
evidence or a safe handoff and fresh handshake before advancing. Migration is
not proof of availability.

Transitions require hashed artifacts, not prose alone:

- `ACTIVE -> EXECUTOR_DEGRADED|UNAVAILABLE_AUTH|UNAVAILABLE_BILLING|UNAVAILABLE_QUOTA`:
  retain the actual failure.
- `... -> SOLO_TAKEOVER`: current user authorization, checkpoint, original UUID,
  pending nonces, resume phase, protected paths, frozen executor, stopped sentinel
  and no executor writers. Solo reviews are `solo_self_review`.
- `SOLO_TAKEOVER -> HANDOFF_READY`: completed phase boundary and no writers.
  Direct unavailable-to-handoff is allowed after observed recovery when no solo
  work took place; do not invent a solo history.
- `HANDOFF_READY -> ACTIVE`: fresh same-task handshake after the handoff plus
  matching workspace/executor UUID. Surface numbering may be refreshed with a
  fresh identity observation, never by choosing an unrelated executor.

An expired observation requires `reobserve`: it refreshes identity/TTL but keeps
SOLO or unavailable state unchanged. Delivery queued at receiver, detector
uncertainty, late callbacks and a supervisor's insufficient budget are not
executor billing failures. Do not resend a possibly accepted nonce.

## Shared resources

`scripts/resource_broker.py` provides durable FIFO tickets and process-group
ownership backed by SQLite and real flock checks. All tasks sharing an account
must use the same root/account key and pinned external transport locks.
Different notebooks or browser targets do not create extra NotebookLM quota.
This implementation supports POSIX hosts; it does not claim a Windows lock backend.

Configure `word-zotero` once with the application's existing automation lock.
Use `request -> grant -> run -> release`, passing the returned ticket/token.
An expired ticket stays owned until reconciled; file existence, TTL expiry or a
dead coordinator does not grant ownership. Only the exact waiting owner may
`withdraw` an unused queue entry. Scope resource messages to the task and surface
UUID; do not borrow another task's executor session.

Word release requires a hashed, task-bound observation with integer
`documents=0`, integer `windows=0`, `modal=false`, `pending=false`,
`zotero_current_doc=false`, and `zotero_current_window=false`, plus empty owned
processes and actual free external locks. `-1743` is UNKNOWN, not zero; do not
reset permissions or close unrelated documents merely to satisfy the receipt.

NLM release instead requires `in_flight_requests=[]` and a hashed terminal
receipt bound to the exact request and lease, with remote terminal state and
empty process group. A no-invocation proof is allowed only when the broker itself
recorded zero invocations. Client exit alone cannot settle a browser/server request.
An uncertain request retains its account quarantine and exact lease until resolved.
Return the network window before doing offline adjudication.

NotebookLM task allocation, dedupe and quota models live in `thesis-refiner`;
this broker serializes resource ownership. Default collaboration batches are at
most two queries or ten minutes, then hand over after the active request settles.
Reserve 20% of measured budget for final review/recovery; unknown compute balance
must stay unknown. Neither rule is permission to bypass an account's service limits.

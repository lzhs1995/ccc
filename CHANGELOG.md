# Changelog

## 0.2.17

- Add an optional independent AnyRouter network guard with isolated Mihomo probes, real Responses/SSE admission, persistent quarantine recovery and shared probe budgets. Keep a healthy commercial pool, rotate on confirmed route failure and reserve ordered Tokyo residential chains for fallback.
- Preserve consumed probe-budget reservations across wall-clock rollback, including guard restarts, instead of admitting an extra billable probe.
- Honor configured probe spacing longer than one minute across restarts while keeping the per-minute budget independent.
- Publish only admitted routes through a loopback provider, retain active definitions until selection commits, and pin complete-chain dependencies. Normal failover does not reload Clash or close connections. Add a same-binary stream-preservation acceptance tool for initial profile migration.
- Show network health in the Supervisor and expose `ccc network status`, `probe` and `install`. Only verified AnyRouter failed turns wait for a confirmed network outage; original B STOP, scope, heartbeat, pause and native-turn gates remain independent.

## 0.2.16

- Fence new B workspace input synchronously when a native model event arrives, before awaiting membership verification. Only confirmed current membership authorizes success and interruption; moved surfaces release the provisional fence without stopping the old workspace.
- Require structurally valid, contiguous B job slots for both original and migrated protection rules. Reject malformed records instead of treating their presence as authorization.
- Preserve every original session on any migration failure before history preflight completes, including discovery and fence-persistence errors. Keep post-preflight setup failures on the existing scoped stop path.
- Require verified stopping within one second before B can rearm a successful pool. Late or incomplete proof keeps the pool paused until an explicit W recovery.
- Keep stalled goals on the verified native goal-resume path. Missing or already-consumed goal evidence cannot fall through to an ordinary continuation prompt.

## 0.2.15

- Interrupt every Codex in a genuine B batch workspace on its first fresh native model response. Fence new/queued requests immediately, verify native backend exit, and escalate exact processes at 350/650 ms against a one-second deadline. Current workspace UUIDs and microsecond process identities exclude other workspaces and moved surfaces.
- Add a private native RPC relay, independent watchdog, persistent stop records and original-session migration. Early success cancels unfinished batch slots. W restores the same sessions without replaying prompts or filling cancelled slots; B after confirmed success starts a new batch. Persist empty sessions without model calls and suppress hidden title-generation requests during connection tests.
- Reconcile exited fork processes before reporting a coverage failure. Finish original-history preflight before migration can stop a process; refuse destructive migration when the original writer owns an unlinked or replaced history file.
- Treat the global Dock as window-owned even when cmux displays it under the selected workspace, so B never interrupts unrelated global Dock sessions.
- Recover a verified native stalled goal even when its original rollout was unlinked or replaced. Require the live writer, original SQLite identities, matching terminal error and current blocked goal; issue one native `/goal resume` with separately verified draft and Enter. Ordinary slash-command sending remains disabled.
- Add local-upstream acceptance for real cmux B50, early cancellation, W/B lifecycle, native/watchdog failure, moved-surface isolation and unlinked-rollout goal recovery.

## 0.2.14

- Recognize the dim background-terminal status bar when verified RGB composer particles occupy its usual blank separator. Keep the same position, complete text, dim-style and wrapping checks; real output and unverified particles still block recovery. This fixes completed rate-limit/high-demand turns incorrectly marked as superseded on animated Codex surfaces.

## 0.2.13

- Keep active native Codex reconnects out of the send queue before topology preflight, so a provider outage cannot fill every send slot with tasks that are still retrying. Revalidate completion again before actual input.
- Match both `We're` and `We’re` in the complete high-demand banner, including wrapped text. A typographic apostrophe must not leave an authorized failed task classified as idle.
- Recognize native Hook termination cards without an exit status, as well as timeouts, when a current provider failure needs continuation. Hook failures alone never trigger input; explicit hook decisions and newer task output still block it.
- Add a backed-up, opt-in migration for the legacy Codex shell/launchd binary-copy loop, and document matched official CLI/helper upgrades without resetting sessions or TCC permissions.
- Confirm the original batch task through combined AGENTS/environment context and incremental, partial JSONL records. Persist proof before releasing startup protection; recover legacy holds from every relevant batch.
- Separate startup holds from operator exclusions and show `整池／启动中`. Repeated workspace authorization is idempotent and preserves manual P, exclusions and per-surface pauses.
- Share timestamped tree/process inventories across the watcher, panel and batch workers. Coalesce failed scans, limit global process discovery to once per five seconds, and continue local confirmation during RPC failures.
- Share four startup permits and at most two starts per second. Menus and drafts yield their permit; uncertain RPCs keep their durable record without monopolizing capacity. A waiting pool cannot block all other pools, and capacity checks read only active jobs.
- Give new batches separate SQLite runtimes, seeded through read-only native metadata backups. Avoid both the busy global log database and a full reindex of old rollouts. Discover new Codex children directly from the bootstrap shell when system.top is unavailable.
- Confirm the first task without waiting for topology refresh, releasing its startup permit promptly. Read native writer descriptors directly on macOS instead of launching lsof for each poll; incomplete reads, descriptor reuse and process/session changes still reject evidence.
- Let send preflights join an in-flight cross-process topology refresh with a bounded wait, retaining fresh-snapshot requirements instead of repeatedly caching a transient `tree refresh pending` failure.
- Read and rank janitor candidate timestamps in one process, retaining oldest-first selection and all disposal checks without spawning a separate `stat` for every candidate.
- Queue B when macOS PTYs are exhausted instead of creating unusable tabs. Recover proven pre-session database/PTY startup failures in the same surface, preserving native sessions, drafts and operator pauses. Include an optional LaunchDaemon for macOS's supported 999-PTY ceiling.
- Retry cmux's exact pre-dispatch polling rejection with exponential backoff, including legacy batch slots stranded with stale launch receipts. Preserve uncertain deliveries and recheck original surfaces, native sessions, drafts and pool authorization before each retry.

## 0.2.12

- Confirm a new native Codex session before its first rollout exists, using the
  original process and UUIDv7 writer lock. Ignore read-only history index files
  when recovering the active transcript.
- Submit each batch prompt with a separately recorded Enter after verifying
  its exact draft. Handle terminal spans that merge prompt padding and text;
  preserve extra user text and ambiguous delivery instead of resubmitting.
- Confirm first-task starts from both native user-message formats. Retain the
  original launch time when retrying a paused batch.
- Recognize the complete rate-limit banner when native Codex adds a second
  prefix, while retaining draft, menu and quoted-example protection.

## 0.2.8

- Wake guarded observation from original native failures; retry a temporarily
  unavailable observation every second without rereading unchanged transcripts.
  Drafts, menus, queues and acknowledged input stop priority retries.
- Use the advertised control socket for inventory and Codex continuation,
  require process-inclusive inventory, and preserve uncertain delivery records.
  Handle animated composer overlays, word-wrapped errors, native HTTP failures,
  and stale Working text above a later failure.
- Add `P` for workspace pause/Interrupt and `W` for workspace resume. Persist
  the pool pause before input, drain in-flight Codex input with a workspace
  barrier, then send Escape to live main-area Codex. Keep original sessions,
  individual pauses/exclusions, and the pool gate on partial transport failure.
- Retry transient cmux read/socket failures without permanently pausing authorized
  surfaces. Preserve explicit pauses and delivery records.
- Scan the full visible viewport when terminal grids contain bottom padding.
  Recognize the exact two-line Codex Hook timeout diagnostic without treating it
  as new task progress; other Hook errors and user/approval text stay protected.
- Wait for the original Codex task to end before continuation, and revalidate
  that task at the input boundary. Persist one submission per failed task.
  Recover missing Hook identity from the live process's original open transcript,
  checking PID/start time and both cmux UUIDs without fabricating Hook events.
- Keep the native-task guard active while shared process identity is refreshing;
  a missing Hook plus a pending process snapshot is not a legacy-client bypass.
- Reconcile timed-out input using two fresh empty-composer observations and an
  unchanged failed task. Verified absent input can retry instead of remaining
  blocked forever. Queued CCC prompts retain a durable, single-attempt recovery
  record; user drafts and ambiguous delivery remain protected.
- Remove the unwanted delayed-observation label from the Supervisor.
- Expire janitor quarantine after three hours, run expiry independently, and
  drain approved backlogs in bounded batches with restartable progress records.

## 0.2.7

Includes the previously unpublished 0.2.2–0.2.6 working drafts.

- Replace whole-batch polling with independent per-UUID deadlines, bounded
  observation/send pools (32/8 by default), and completion wakeups. Slow readers
  and sends no longer hold every target until a batch finishes. Share process
  snapshots and their classification, and bound background maintenance work.
- Read fresh viewports over the CLI-discovered cmux v2 socket without spawning
  a process for every read/replay. Validate request and target identities, bound
  response size and total timeout, and retain CLI fallback for legacy/auth modes.
- Reuse one current grid for known error candidates while retaining lightweight
  text checks for idle/Working/menu states. Share a single fleet process scan,
  finish diagnostic refreshes before declaring Hook inventory complete, and
  remove launchd Background throttling from the deadline-sensitive watcher.
- Keep missing/unverified Claude Hooks and known Hook/model blockers out of the
  healthy continuation count even when the terminal viewport is readable.
- Preserve workspace-scoped discovery over the shared fleet snapshot and bound
  watcher thread switching to reduce deadline-thread GIL starvation.
- Spread revisit deadlines across the configured period, retaining immediate
  first reads and avoiding cadence drift or bursts after missed deadlines.
- Remove unchanged-config lock convoys and whole-fleet copies/owner inspections
  from shared locks. Wake at observation deadlines, persist the already encoded
  state once, and prevent Claude event-error persistence from deadlocking.
- Coalesce concurrent durable state writes and publish health outside the
  scheduler. Persist every send attempt before input; recheck authorization
  after persistence. Ignore stale workers after pause or registration changes.
- Re-read each send candidate and retain Working, composer, queue, Dock,
  manager, completion and Claude Hook guards. A send timeout remains
  `delivery_unknown` across restart until read-only progress evidence resolves
  it. A lone prompt echo below an old error is insufficient confirmation.
- Report actual read age, dispatch lag, send queue/persistence/transport times
  and delivery outcomes in status and Supervisor. Stack health requires this
  evidence, and status reads no longer run live process/discovery scans.
- Recover current high-demand and rate-limit banners across wraps, reconnect
  details and background-terminal chrome on the configured repeat interval.
  Keep the 60-second floor for reconnect-only stalls. Recognize the renamed
  tool-call queue banner and retain current errors beneath continuation echoes.
- Follow live Codex tabs in explicitly monitored, unpaused panes while honoring
  current registration and workspace exclusions at the input boundary.
- Show a current `400 invalid_encrypted_content` as a provider blocker without
  sending retry prompts or replacing the session.

## 0.2.1

- Recognize current Codex reconnect errors across split spans, wrapped status
  lines and nested `high demand` details while retaining genuine Working guards.
- Enforce a 60-second reconnect repeat floor across timer/request changes and
  watcher restarts. Ordinary error retries keep their configured interval.
- Recognize both queue headers across wraps and a lone pending continuation;
  accepted prompt echoes supersede older errors until a new error appears.
- Ignore verified RGB braille overlays with dim composer placeholders, while
  protecting typed placeholder text and actual braille input.
- Report provider quota-specific 401 errors as `token_exhausted` / `额度耗尽`
  without sending or changing credentials, sessions or monitoring settings.

## 0.2.0

- Revalidate the latest genuine Stop rejected before explicit Claude enrollment,
  preserving its ID, ordering, rejection provenance and submit deduplication.
- Report component-owned terminal observation coverage and registration readiness;
  active gaps and unknown evidence no longer hide behind a healthy daemon PID.
- Recover current text reads through validated viewport replay; distinguish
  uninitialized native runtimes and reject foreign processes attached by TTY reuse.
- Install checksummed immutable watcher bundles outside Documents and retain
  rollback without restoring configuration, state or event ledgers.
- Wait through launchd's asynchronous removal interval with bounded bootstrap
  retries, including the rollback path.
- Retain deferred Stop ordering, later-prompt supersession and HTTP retry guards.
- Recover Codex provider `rate limit exceeded` banners without requiring a 429
  prefix, including wrapped text, while preserving working/input/queue guards.
- Bring the current TUI, stack, janitor, profile manager, diagnostics and regression
  suites into the existing public repository with Python 3.10+ compatibility.
- Add isolated profile PTY tests, macOS CI, release manifests and operations notes.

## 0.1.0

Initial public source release.

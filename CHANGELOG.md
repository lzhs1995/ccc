# Changelog

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

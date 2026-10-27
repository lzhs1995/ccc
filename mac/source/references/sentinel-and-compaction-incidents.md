# Sentinel And Compaction Incident Lessons

This reference records observed failures from the Stata Workbench supervisor/executor workflow. Treat them as regression requirements, not anecdotes.

## Task-chain interruptions observed

32. **Executor deployment prose is not deployment evidence.** An executor report
claimed a new static entry, a verified backup, matching HTTPS hashes, and a live
release. Independent SSH read-back showed the named backup did not exist, the
claimed entry returned 404, the production index still referenced an older entry,
and production mtimes predated the alleged release. Supervisors must verify the
actual index references, exact hashes, backup directory, recent asset set, and
container state before accepting `LIVE`. A plausible narrative or tool receipt is
not a substitute for production postconditions.
33. **A successful build can still emit a broken static graph.** A frontend build
returned zero while `index.html` referenced assets absent from `dist`. Before any
static upload, enumerate every local JS/CSS reference in the generated index and
require each file to exist and be nonempty. This gate is separate from typecheck,
lint, tests, build exit status, and bundle-string probes.
34. **A stop request is not a stopped sentinel.** A sentinel stop command returned
`SENTINEL_STOP_REQUESTED` while its recorded PID remained alive. Always verify PID
termination and state-file quiescence. If graceful stop does not complete, terminate
that exact recorded PID; never kill by a broad name or start a duplicate watchdog.
35. **Do not use `/clear` for executor recovery.** Clearing a context-bearing Claude
session destroys the review and implementation chain and forces expensive
reconstruction. For tool-channel ambiguity, inspect disk/process/production
postconditions first. For actual context pressure, use `/compact` in the same
session and monitor completion. If compaction fails, stop and report the blocker.

28. **A durable billing outage is not a transient API error.** Once the user or provider
confirms insufficient balance/quota will not recover shortly, continuing the 60-second
API retry loop or five-minute sentinel only produces noise and can accidentally consume
queued prompts after later recovery. Stop the sentinel, freeze executor dispatch, and
record `UNAVAILABLE_BILLING`. A solo supervisor/executor takeover requires explicit user
direction and must not be reported as multi-agent consensus. Preserve the original
context-bearing surface. Collaboration may resume only at a safe phase boundary after
an absolute handoff artifact exists and the user confirms recovery; rebind and handshake
that same surface, never a replacement, and never let both actors mutate one phase.

29. **Executor recovery needs machine-readable continuity, not prose alone.** A long
outage can span multiple turns and context compactions. Persist state transitions.
When solo work occurred, enforce `ACTIVE -> UNAVAILABLE_BILLING -> SOLO_TAKEOVER ->
HANDOFF_READY -> ACTIVE`. When no solo work occurred, do not fabricate that state:
after the user explicitly confirms recovery, allow `UNAVAILABLE_BILLING ->
HANDOFF_READY -> ACTIVE` with `--recovery-confirmed`. Both paths require an absolute
handoff, the original executor surface, and a fresh identity/handshake before dispatch.
Reject direct `UNAVAILABLE_BILLING -> ACTIVE`, relative/missing handoffs, unconfirmed
direct recovery, and a different executor surface. During `UNAVAILABLE_BILLING` and
`SOLO_TAKEOVER`, sentinel, dispatch, API retry, model switch, and replacement-session
flags must all remain false.

26. **A fault discriminator PASS is not the same as product admission PASS.** In the
Stata rc.7.15 Phase 2C run, all five injected variants produced their expected guard
codes and the executor reported overall PASS. The same evidence also showed one variant
left the bridge draining for more than 28 minutes and another valid timing window
skipped dataset restore entirely. The task pack required bounded cleanup and usable
post-fault state, so the supervisor correctly rejected the aggregate PASS. Admission
audits must evaluate every end-to-end recovery/availability predicate independently of
whether the injected branch was reached. An executor may label a guard primitive PASS
while the enclosing workflow is FAIL; high-severity attached product findings are
blockers when they contradict the admission contract, not non-blocking annotations.

25. **Separate a supervisor retry round from Claude's internal request attempts.**
After one consumed resume prompt, Claude may show `attempt N/10` and spend several
minutes retrying the same upstream request. Those ten internal attempts are one
supervisor round, not ten rounds and not permission to send another prompt while
they are active. Count an outer retry only when all four facts hold: the prior
request has ended in a terminal API Error and idle prompt, the supervisor then
waited a fresh 60 seconds, the retry prompt was submitted (not merely pasted or
queued), and newer Claude activity proves it was consumed. Preserve the final
request id for each outer round. After the third outer round reaches a terminal
API Error, stop the task and sentinel, write an administrator blocker with the
partial-state hashes, and do not send a fourth prompt or replace the session.

24. **Terminal callback templates inside supervisor prompts can create false
`DONE`/`BLOCKED` events.** In Phase 2B rc.7.15, Claude had returned only 502 API
errors and the required manifest did not exist, but the sentinel emitted `DONE`
three times because the supervisor's multi-line `❯ [CMUX-AGENT]` prompt contained
the literal required callback `DONE: <current-token>`. Filtering prompts only for
API/ACTIVE signals is insufficient: apply the same prompt-block removal before
terminal marker scanning. A terminal candidate must begin in executor assistant/UI
output (for example an `⏺ DONE:` line after the prompt), contain the current token
on that same line, and still remains advisory until required artifacts exist and an
active `cmux-agent ask` callback reaches the supervisor. Regression tests must cover
both sides: the prompt template remains nonterminal, while a later assistant `DONE`
line is terminal.

21. **An empty tool result is an ambiguous transport state, not proof that the
command did not run.** Long local tests and shell commands have completed while
the supervisor received a completely empty result; short follow-up reads then
showed the changed files or completed process state. Never replay a mutation,
deployment, task dispatch, or cleanup merely because its result rendered empty.
First use a short, independent, read-only probe of the intended postcondition
(for example `git status`, a file hash, process state, or a small
`read-screen`). Classify the operation only from that probe. If the postcondition
still cannot be established, report transport ambiguity rather than success or
failure.
22. **A blocked orchestration batch provides no per-command execution proof.** A
PreToolUse guard can reject a JavaScript or shell batch before any nested command
runs, while the combined error can misleadingly name only one command. Treat the
entire batch as unexecuted unless independent state proves otherwise. Split the
batch into short commands, rerun only the missing read-only checks, and never
repeat a mutation until its target state has been inspected. This also avoids a
semicolon or concatenated argument being parsed as part of a task id or evidence
path.
23. **Messages sent while the executor owns a running tool can remain queued.** A
successful `cmux-agent ask` only proves delivery to the surface, not that Claude
has consumed or submitted the message. `Press up to edit queued messages` and a
visible prompt block behind an active tool are explicit queued-input evidence. Do
not stack more audit findings or press Enter while the tool is running. Wait for a
safe command boundary, re-read the same surface, and require the executor's new
assistant activity before treating the instruction as active.
24. **A supervisor in another workspace can still inject a competing task into a
bound executor surface.** Same-workspace discovery protects the sender's normal
selection path, but it does not prevent a stale or manually chosen surface ref from
receiving `cmux-agent ask`. When a foreign task id appears on an active executor,
the owning supervisor must reject it at both ends: tell the foreign supervisor that
the surface is exclusively bound, and tell the executor to ignore the foreign task.
Do not let one executor interleave two task packs, do not claim cross-task review,
and do not create a replacement session. Preserve any work already written, stop
further contamination at a safe boundary, and record the binding conflict in both
task reports.

18. **A progress-only final response looks like task termination.** The supervisor
repeatedly reported an executor milestone through its terminal/final channel while
the executor was still running. The client closed the visible turn, so the user
correctly experienced each milestone as another unexplained interruption. During a
live executor task, use only the host's intermediate-progress channel and immediately
continue with tool work. Reserve the final channel for the audited terminal state or
a genuine blocker that requires user input.
19. **Mid-command preflight can enqueue its handshake behind real work.** A supervisor
re-ran full preflight while Claude was running tests and preparing commits. The bridge
and handshake prompt appeared in Claude's queued input while the active tool continued;
the harness process itself returned no useful stdout and strict validation was not yet
available. Do not refresh an already-established identity handshake during healthy
execution. If evidence truly must be rebuilt, use the existing task id and explicit
surface, wait for a safe command boundary, and inspect the JSON artifacts; command
completion, queued prompt text, and empty stdout are all non-evidence.
20. **A generic discovery failure does not override an explicit user binding.** A
parameterless preflight returned `NO_EXECUTOR_FOUND` even though cmux identity and panel
inventory showed the user-designated Claude session in the same workspace side panel.
Treat the generic result as fail-closed auto-discovery, not as proof the session is
gone. Re-run once with the current task id and the explicitly bound existing surface.
Never open a replacement session to repair an omitted binding.

13. **A supervisor-selected runtime trigger can be structurally unreachable.** A task
pack incorrectly equated a debug endpoint with the visible-bridge execution path. The
executor's pre-gate proved the endpoint called a different function and could never
register the required active snapshot. Runtime/fault-injection task packs must require
an endpoint-to-callsite proof before mutation: handler, called function, ownership
registration, gate conditions, and expected observable state. A contradictory proof
blocks the round; repeated attempts to win a nonexistent timing window are not useful
coverage and must never be reported as a partial policy pass.
14. **An API alert can be stale by the time the supervisor wakes.** The sentinel
classified a transient API error, but a fresh 240-line surface read contained no API
error and ended in current spinner/tool activity. Before starting the 60-second retry
counter, re-read the bound surface and order signals again. If newer executor activity
exists, record the alert as recovered/stale and do not interrupt or resend the task.
15. **A helper send can paste without submitting while Claude is compacting or API-
retrying.** Command success and visible prompt text are not dispatch proof. After every
resume/retry send, read the surface and require either a new assistant response, active
spinner, or tool call after the prompt. If the text remains in the input editor (for
example `Press up to edit queued messages` or a visible `❯` prompt), send Enter once to
the same surface and re-read. Do not count an unsubmitted paste as an API retry round.

16. **Uppercase `Enter` and no post-submit confirmation can silently strand both task and callback prompts.** The lightweight `cmux-agent` helper used the key spelling `Enter`; some cmux builds accepted the command but did not submit Claude's compose buffer. The helper then printed success without reading the surface, so a supervisor sent an audit that remained under `❯`, and an executor callback could likewise remain unsent. A later repair added marker-linked output proof but produced the inverse false negative when valid executor output scrolled the marker off-screen. The fix is a shared two-phase submit contract: unique delivery marker, paste, lowercase `enter`, pre/post screen snapshots, confirmation by marker-linked output or a genuinely new structural activity line after the marker scrolls away, and at most one bounded recovery Enter at an idle boundary. Regression tests must prove (a) a submitted prompt followed by assistant output is confirmed without a second key, (b) a marker-scrolled-off submission with new activity is confirmed, (c) a changed screen without new activity remains unconfirmed, (d) a pending compose is retried exactly once, (e) queued/active input refuses blind retry, and (f) a marker still pending after the retry returns `DISPATCH_UNCONFIRMED`. The same contract must be used by Python harness handshakes/rounds and by `cmux-agent ask` callbacks; `bridge-test` remains deliberately non-submitting.
17. **Completed spinner summaries are not active work.** A screen can show `✻ Churned/Brewed/Cooked/Cogitated for ...` after Claude has returned to a blank `❯` prompt. Treating the duration suffix as an active marker causes a correct new delivery to be rejected as `DISPATCH_UNCONFIRMED`, even though the compose buffer is empty and no task is running. The guard must match live state words (`Recording`, `Thinking`, `Running`, `Retrying`, `Checking`, `Bash:`, `API error`, etc.) plus the explicit queued-message notice, and tests must include both a completed summary (false) and a live spinner/queued input (true).

17. **High-frequency sentinels can cost more than they protect.** Repeated 300/900-second reads of a healthy, callback-capable executor consumed supervisor attention and produced duplicate `NO_PROGRESS` notices without changing the state. The bands prescribed at the time (3600 / 900 / 300 s) are themselves historical: incident 41 later measured them as still too frequent. Current policy is callback-first supervision with exactly one sentinel per task at 7200 seconds and three unchanged checks for stable long work, 1800 seconds only for medium-risk implementation, and 300-600 seconds only for a bounded cutover. A callback or authenticated blocker wakes the supervisor immediately; the sentinel must not prompt, retry, or create a replacement session. Record the interval and reason in sentinel state so a future operator can lengthen or shorten it deliberately instead of stacking watchdogs.

1. **Executor stopped after retrospective instead of implementation.** Claude accepted the task, inspected sources, spent several minutes explaining why its earlier tests were weak, then ended at the prompt without changing code and without `DONE`/`BLOCKED` callback. A thoughtful assistant response is not task progress. Task packs must say that retrospective prose does not satisfy an implementation objective, and the sentinel must alert on the first idle prompt.
2. **ACTIVE-to-idle transition created a two-interval blind spot.** The old sentinel reset `unchanged_checks` when the screen changed from ACTIVE to an idle prompt, then waited for the same idle screen to remain unchanged. At a 300-second interval this delayed detection to almost 10 minutes. `IDLE_OR_UNKNOWN` now alerts on its first observation; alert-key deduplication prevents repeated noise.
3. **Claude spinner-only activity was misclassified as idle.** Claude sometimes shows only a leading spinner (`✻`, `✢`, `✳`, `✶`, `✽`) plus task text and elapsed time, without the words `thinking` or `running`. That is ACTIVE and must not trigger `NO_PROGRESS`.
4. **Stable ACTIVE screens were misclassified as stalled.** Normalization intentionally removes elapsed time, spinner glyph changes, token counts, and context chrome. Consequently a long thought can retain one normalized signature. Unchanged ACTIVE text is not a stall by itself.
5. **launchd sentinel could not access cmux.** A launchd child returned `Access denied - only processes started inside cmux can connect`. A launchd label or live PID is insufficient startup proof; the launched process must successfully classify a real executor screen.
6. **Transient sentinel panel/process did not survive the supervisor boundary.** A newly-created side panel and detached-background experiments disappeared after the supervising turn/process boundary. Never claim persistence from command success alone. Startup proof requires a live PID, fresh state, successful read-screen classification, and another freshness check after the intended supervisor boundary. If the environment cannot provide this, the supervisor must keep a foreground sentinel session and must not end the turn.
7. **Screen text is not an active callback.** Even a valid-looking `DONE:` printed on the executor screen is only a candidate event. The executor must use `cmux-agent ask` to send the current callback token to the bound supervisor; the supervisor audits the artifact before stopping the sentinel.
8. **Historical terminal markers can poison new phases.** A `DONE:` from an earlier task must not terminate a later sentinel. Marker and current callback token must occur on the same line.
9. **Historical API errors can poison a recovered phase.** Searching the whole screen tail for `API Error` before considering newer activity creates false incidents. API, compact, context, and ACTIVE signals must be ordered by screen position; the latest signal wins. A current-token terminal callback remains a separate authenticated priority.
10. **ACTIVE is not proof of indefinite health.** A spinner-only line can represent real work, but a normalized ACTIVE screen that remains unchanged for three 300-second checks emits `ACTIVE_STALLED`. The supervisor must read the same surface and decide whether to keep waiting or interrupt; the sentinel never creates a session or changes model automatically.
11. **Supervisor prompts can contain API-error vocabulary.** A task prompt saying
    "if an API error occurs" caused repeated false `API_ERROR` alerts even though Claude
    was actively working. Signal scanning must exclude the `❯` prompt block, including
    wrapped lines and verbatim quoted historical errors. A real error is accepted only
    after executor/UI output resumes and must match an error-shaped line such as
    `API Error:` or `502 Upstream API request failed`.
12. **A resumed phase must rotate its callback token after terminal output.** Once an
    executor has emitted a valid current-token `BLOCKED:` or `DONE:`, that terminal line
    remains in screen history and intentionally has priority over later nonterminal
    signals. If the supervisor adjudicates and resumes the phase, issue a new round/token
    (for example R1 -> R2), rebind the sentinel, and require the final callback to use
    the new token. Reusing the old token produces permanent historical-terminal alerts.

## Claude automatic `/compact`

Claude automatic compaction is slow and can be unstable. Treat it as its own monitored state rather than generic ACTIVE.

1. Classify explicit `Compacting conversation` as `COMPACTING`.
2. Parse the compact progress bar adjacent to the compact line. Do not confuse it with the separate bottom `上下文 N%` chrome; the two values differ in real runs.
3. Persist `compact_started_at`, `compact_progress_percent`, `compact_last_progress_at`, and `compact_unchanged_checks` in sentinel state.
4. Progress can be slow, so ordinary callback-first polling remains low frequency. Once `COMPACTING` is observed, the sentinel temporarily polls at 60-second intervals. Do not interrupt while the compact percentage is advancing.
5. Explicit `Compaction failed`, `Failed to compact`, or equivalent is `COMPACT_FAILED` and requires immediate task stop plus administrator notification. Never create a replacement Claude session.
6. If the compact percentage does not change for two consecutive 60-second compact checks, emit `COMPACT_STALLED`. The supervisor must read the same executor surface and interrupt that compact operation once with `Esc`; preserve and disarm the active task, then decide whether one narrow same-session manual `/compact` is authorized. `Autocompact is thrashing` is immediate `COMPACT_FAILED` evidence and bypasses the two-check wait. Never switch session/model or use `/clear`.
7. If context reaches 100% without an active compact operation, emit `CONTEXT_FULL`. Send `/compact` only to the same Claude session. If it fails, stop and notify the administrator.
8. When compaction disappears, record `compact_completed_at`. Confirm that context decreased and execution resumed. Treat `Compacted (ctrl+o to see full summary)` as an explicit successful terminal compact signal that supersedes historical thrash text above the current `/compact` prompt. If Claude lands at an idle prompt instead, immediately send `NO_PROGRESS` and resume the same task explicitly. A notification helper returning nonzero after the message visibly arrived is delivery uncertainty, not permission to emit a duplicate alert or crash the sentinel.
9. Claude may stop with `Context limit reached · /compact or /clear to continue`
   before the bottom chrome reaches a displayed 100% (93% was observed). Treat this
   explicit stop message as `CONTEXT_FULL`; the percentage is advisory. Send `/compact`
   to the same session and monitor it normally.
10. `cmux send` takes text as a positional argument; it has no `--text` flag. For an
    exact same-session slash command use `cmux send --surface surface:N "/compact"`
    followed by `cmux send-key --surface surface:N enter` (or send `"/compact\\r"`).
    Passing `--text /compact` inserts the literal prefix into the prompt. Claude happened
    to recognize `/compact` in the observed incident, but that behavior is not a valid
    control contract.

## Task multi-agent-skill-hardening-20260830 (both roles)

Every entry below was measured in that task's own receipts or source, not recalled.

36. **One reason code cannot carry four corrective actions.** `DISPATCH_UNCONFIRMED`
was raised for four distinct situations. Three fired in one session, in both
directions: a supervisor that never pasted (`dispatch_submitted_at = null`, aborted
1.1 s after starting), a delivered message the detector could not recognize (the
peer renders `• Ran 14 commands` / `• Working (16s...)`, which the activity matcher
did not know), and a delivered message sitting in the receiver's labelled pending
queue behind an active tool call. Only the last means *wait*; the others mean
*fix*, and none means *resend*. States are now `SUPERVISOR_DID_NOT_SUBMIT`,
`SUBMISSION_ABORTED_BUSY`, `COMPOSE_OCCUPIED`, `DELIVERY_UNVERIFIED_BY_DETECTOR`,
`DELIVERY_QUEUED_AT_RECEIVER`, each recorded with `submitted_at` and
`detector_side`.

37. **`bridge-test` poisoned the handshake that followed it.** Receipts show
bridge-test finishing at 11:45:33.009753Z with `observed_in_screen=true` — its
token still in the compose buffer — and handshake starting 1.003 ms later, reading
that token as executor input, and aborting with `dispatch_submitted_at = null`. The
abort was then attributed to executor silence. `SKILL.md` had required the
clear-and-verify step in prose since before the incident; nothing enforced it, so
`preflight` ran both phases back-to-back and tripped its own guard. `bridge-test`
now re-reads until its token is gone (bounded, then `BRIDGE_TEST_UNCONFIRMED`), and
`handshake` refuses to paste without `clear_confirmed=true` for the same task and
surface. Deliberately **not** fixed by whitelisting the bridge token in the busy
detector: a stale token, a token concatenated with a real prompt, or another task's
token must all still fail closed.

38. **A correct default was undercut by a generic override.** A 182 s handshake
timeout was recorded as executor silence against a byte-exact ACK. `cmd_handshake`
already resolved 600 s; `_effective_timeout` lets a generic `--timeout` win
unconditionally, so an explicit 180 s silently downgraded the cold handshake to the
round budget. The defaults were never wrong — a guard "enforcing 600 s" would have
hardened a non-existent bug. Receipts now carry `budget_source`, `budget_seconds`,
`phase_minimum_seconds`, and `budget_below_phase_minimum`, and a timeout under an
undersized budget is `SUPERVISOR_BUDGET_TOO_SHORT` with
`attributable_to_executor=false`, never `HANDSHAKE_TIMEOUT`.

39. **A gate read its own threshold from the file it audited.** `consensus-check`
took `minimum_required_rounds` from `rounds.json`, so lowering that number in the
audited file lowered the bar it was checked against. The floor is now the external
constant `CONSENSUS_MINIMUM_ROUNDS = 3`, and a declared value may only raise it:
declaring `1`, `None`, or `'2'` all yield 3, while `5` yields 5.

40. **A guard can exist without a hook.** Four guards existed; one side had three
wired, the other had one, and `cmux_consensus_stop_guard` was inert on both. So
`cmux_handshake_receipt_guard` — whose entire purpose is refusing prompt-echo
handshake evidence — had never been able to fire in the executor's session. Writing
more guards inherits the defect. `guard-check` now reports wired-vs-present per
side and fails on asymmetry; a guard that is present but unwired is not a gate.

41. **A watchdog announced routine silence.** Ordinary `NO_PROGRESS` called
`notify_supervisor` on every interval at a 300 s default, costing more than the
work it guarded. Ordinary no-progress now updates state and the dedup ledger
**without** an `ask`; terminal and error events still announce immediately, and a
paired test asserts both halves so a future edit cannot silence everything and
still pass. Default cadence is the stable band (7200 s), with 1800 s for
medium-risk and 300–600 s only for a bounded cutover.

42. **A hard-coded supervisor surface outlived renumbering.** The sentinel took
`--supervisor-surface` from argv with zero role-map reads, so after cmux
renumbering it notified whatever pane held that number. The target is now verified
against `role-map.json`; a mismatch refuses rather than guesses, and a missing map
is not silently accepted.

43. **`py_compile` passing is not runtime-clean.** Two constants were referenced
before being defined; compilation succeeded because `NameError` is a runtime
failure, and the command would have crashed on first real use. Import the module
and resolve every new symbol before claiming a change is complete.

44. **The measurement broke, not the thing measured — eight times.** In one
session, an executor's own probes manufactured: a phantom "REMOVED 2" from
inconsistent file listing (the files were present at 55435 and 304 bytes); a
"corrupted" task pack from iterating a 996-char string character-by-character; a
miscount of its own document's entries; five of 32 themes silently missing from its
own coverage table; "24 missing policy keys" from inventing key names; two
"missing" cadence constants that differed only in name order (`..._MIN_SECONDS` vs
`..._SECONDS_MIN`); a `KeyError` from guessing JSON field names; and a false
accusation that `consensus-check` had a singular/plural key bug when the code was
correct and consistent. Each looked like a defect in the system under test. The
discipline that caught every one: read the raw structure before asserting, and run
a control — the same probe against the unmodified baseline — before attributing any
failure to a change. One `sha256` is worth recognizing on sight:
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` is the hash of
empty input, so a hash matching it means the read failed, not that the file is
empty.

45. **A mutation harness can fail vacuously in two distinguishable ways.** In an
8-case matrix, one case reported `excision_applied=false` (pattern never matched —
nothing was tested, so the "PASS" was empty) and another reported
`excision_applied=true` with the suite still green (the mechanism was paired with a
suite that did not cover it). A third case needed all three sequential guards
excised before the verdict flipped, because `is_absolute()`, `is_file()`, and a
`None`-returning hash each caught the poison independently — genuine defense in
depth, not a weak test. Always record whether the excision applied, and treat
`applied=false` as "unproven", never as "passed".

46. **A test that encodes the old contract must be rewritten, not deleted.**
Changing ordinary `NO_PROGRESS` to silent broke
`test_first_idle_observation_emits_no_progress`, which asserted the previous
behaviour. Running the same test against the pre-change backup (green there, red
here) isolated the cause to the intended change rather than a defect. It was
replaced by a pair: one asserting silence for ordinary no-progress, one asserting
terminal events still announce. Without the pairing, a later edit could silence
every alert and the first test would still pass.

47. **Hook wiring is not a one-time achievement; a verified-symmetric config can
silently de-wire itself.** `guard-check` reported `rc=0` and "4 guards active on
both sides" at 20:09. At 22:55:50 the same check reported
`GUARD_WIRING_ASYMMETRIC` with three of the four guards `NOT_WIRED` on the claude
side. Nobody edited the guards, and nothing about the skill changed. The cause was
that a client-side settings write (`/autocompact`, which set
`autoCompactWindow: 800000`) rewrote `~/.claude/settings.json` **wholesale** from an
in-memory copy predating my additions — so the three entries I had added were not
overwritten with different values, they were simply absent from the version the
client believed to be current. The same rewrite carried two other writers' changes
(`effortLevel` low→high, a rotated `ANTHROPIC_AUTH_TOKEN`), which is what proves the
file has several concurrent writers and that last-writer-wins is total, not
per-key. Three consequences worth internalizing:

- A config shared with a live client is a **contended** resource. Re-run
  `guard-check` at the end of a task even when nothing in scope touched hooks, and
  never carry an earlier green forward as the current state — the earlier reading
  was true when taken.
- When re-applying, diff against a backup and assert `REMOVED_COUNT = 0` rather
  than writing your intended file. Here the restore had to preserve a stop-guard
  and a token that arrived from elsewhere after my own backup was taken;
  reconstructing "my" version would have reverted them.
- Prove the guard *runs*, not just that its path is spelled correctly in JSON.
  Three of these four guards import `mac_harness`, which uses `str | None`
  annotations and therefore cannot import under Python 3.9 — and macOS ships
  `/usr/bin/python3` as 3.9. All four compile under both, so `py_compile` proves
  nothing here; only executing each guard against a benign payload and observing
  `rc=0` (allow) shows the bare `python3` in the hook command resolves somewhere
  the guard actually works. A guard that crashes on import is wired in name only,
  and depending on the client's failure mode it either blocks everything or
  silently allows everything.

**Recurrence, same task, 80 minutes later.** The wiring was restored at 22:58 and
verified `rc=0`. At 00:15:51 the same three guards were gone again, by the same
mechanism — and this time the rewrite also dropped a *different* writer's three
`claude-goal-stop-guard.mjs` hooks (`Stop`, `SubagentStop`, `UserPromptSubmit`),
while rotating `ANTHROPIC_AUTH_TOKEN`, repointing `ANTHROPIC_BASE_URL`, and adding
`CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`. So this is not a one-off race around a single
`/autocompact`: it recurs on roughly the timescale of ordinary client activity, and
it is indiscriminate about whose hooks it discards. Two operational consequences:

- Treat de-wiring as **expected**, not exceptional. Re-run `guard-check` after any
  long working stretch, not merely once at the end.
- Re-apply **structurally** (parse JSON, append the missing entries, write back),
  never with a text edit against a remembered version of the file. On the second
  repair an `Edit` would have operated on a cached copy whose sha256 no longer
  matched disk; the read-modify-write asserted `REMOVED_COUNT = 0` and
  `NON_HOOK_KEYS_CHANGED = 0` and so preserved the other writer's token rotation
  and base-URL change. Restoring "my" remembered file would have reverted live
  credentials.
- Do not silently restore another writer's collateral losses. Their hooks are
  theirs to re-add; recording the observation is in scope, re-wiring someone
  else's tooling is not.

48. **A provider-agnostic detector that knows only one UI's glyph is blind in one
direction, and 92 passing tests will not tell you.** The busy/compose detector
`_prompt_block_pending` — which backs both `classify_submission_failure` and the
bridge-test clear postcondition — matched only Claude's `❯` (U+276F). Codex renders
its compose box with `›` (U+203A). The detector runs against whatever surface we are
*sending to*, so with a Codex supervisor it could never see a compose box at all: a
payload stranded unsubmitted in Codex's input would return `compose_contains=False`
and classify as `DELIVERY_UNVERIFIED_BY_DETECTOR` ("cannot tell") when the true state
was `SUBMISSION_ABORTED_BUSY` ("still there, never sent"). Worse, the clear
postcondition built *in this very task* to prevent a false green would itself have
recorded `clear_confirmed: true` against a Codex receiver without verifying anything.
Every test passed because every fixture used `❯`. It was found only by sending a real
callback to a real Codex supervisor and reading the raw screen — the glyph had to be
dumped as a codepoint to be seen at all, since `›` and `❯` are visually similar in a
terminal.

Fixing it exposed a second defect underneath, present for **both** UIs and therefore
never a Codex-specific bug: the loop returned `True` as soon as a *second* glyph block
opened, so a marker in an earlier block counted as pending. But both UIs echo a
submitted message in the transcript using the same glyph as the input box. The live
compose box is the **last** block on screen; an earlier one is proof of delivery.
Contract and code disagreed — the docstring said "the current compose block" while
the code said "any block that isn't the last" — and the docstring was right. Two
lessons: when a detector takes a provider argument elsewhere in the file, ask why the
one place that omits it is exempt; and when a fix in one direction is warranted,
re-derive the whole predicate from its stated contract rather than adding an
alternative to the pattern, because the neighbouring logic was written under the
narrower assumption too.

Related: this is why the taxonomy exists. `cmux-agent` reported
`DISPATCH_UNCONFIRMED` for a callback that had in fact been delivered and was already
being worked on — the fourth false negative of that class. Classifying from the screen
instead of resending is what turned a bogus transport failure into two real bug fixes.

49. **Fixing the library does not fix the tool the protocol actually runs on.**
Incident 48 was repaired in `scripts/cmux_bridge.py`. An independent audit then found
that `~/.local/bin/cmux-agent` — a **bash** script, not Python, outside the
skill directory and outside the task-pack scope — carries its own
`prompt_block_pending` with *both* the same defects: the regex at line 135 matches only
`❯`, and lines 136-138 early-return on an earlier transcript block. That helper is the
real callback path in both directions; the envelope shape
(`[CMUX-AGENT][delivery:…][from:surface:N]` plus the trailing "Reply with one leading
marker") is produced by its own `ask_surface`, which is how the same helper was
confirmed to carry both supervisor→executor dispatch and executor→supervisor callbacks.
So the fix landed in the module the skill imports while the shipped behaviour on the
wire stayed broken. `SKILL.md` names `cmux-agent ask` **and**
`cmux_bridge.submit_text` as interchangeable implementations of one contract, which is
precisely the claim that stopped being true the moment only one of them was fixed.

Reproduced read-only by slicing the function out of the shipped bytes and running it
under `bash` against fixtures taken from real screens — never by sourcing the helper,
whose trailing `case` dispatch would execute. Two of seven fixtures fail on the shipped
helper and pass on a patched copy; the other five pass on both, which is what shows the
fixtures discriminate rather than merely detecting that a file was edited.

The two defects have **asymmetric** consequences, and only reading the call sites shows
it. In `submit_text`, `prompt_block_pending` gates all three branches (lines 221, 226,
241):

- Codex blindness → always reaches line 226, `DISPATCH_UNCONFIRMED`, and can *never*
  reach the recovery `enter`. Permanently false-negative, but it never duplicates.
- Stale-block early return → reaches line 237 and presses a **second enter on a
  message that was already delivered**. That is the duplicate-delivery path the whole
  "never blind-resend" rule exists to prevent, fired automatically by the helper.

Duplicating the fix into the helper was refused as out of scope and reported as a
blocker instead. The in-scope mitigation is a parity gate, `mac_harness helper-parity`,
wired into `cmd_preflight` ahead of the identity gate: it locates the helper, extracts
the function, runs it read-only, compares each verdict against
`cmux_bridge.compose_contains`, and exits non-zero on any divergence. Cross-language
parity has to be *checked* because no type system, import, or test can span a Python
module and a bash script that never reference each other. The gate also fails closed
when it cannot answer (`HELPER_UNREADABLE`, `HELPER_FUNCTION_NOT_FOUND`) and skips only
when no helper exists at all, so "the check could not run" never reads as "the check
passed". Its failure message states the consequence and the two legitimate remediations
— send via the in-scope Python path, or obtain explicit scope expansion — because a
fail-closed gate that does not say what to do next just blocks the task.

The generalizable rule: when a contract is implemented more than once, in more than one
language, ask which copy the wire actually uses before claiming the contract is fixed.
"The library is correct" and "the protocol is correct" are different statements.

## Required startup proof

A sentinel is live only when all are true:

- its PID exists;
- its state file has a recent `last_check_at`;
- `last_classification` is not `SENTINEL_ERROR`;
- it successfully read the bound executor surface;
- the callback token equals the current task pack;
- it remains fresh after the supervisor lifecycle boundary that the workflow intends to cross.

Any missing condition means monitoring is not established.

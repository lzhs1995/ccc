# Continue the same task as availability changes

The task objective, accepted work, original sessions, callback nonces and
resource receipts survive a mode change. Task progress takes priority over
waiting for matching turn boundaries. Exchange ready messages while work or
Goal runs; do not pause another agent merely to communicate.

| Available participants | Execution |
| --- | --- |
| Codex and both original Claude executors | Assign separate outputs, promptly ACK and return results; Codex reviews and integrates. |
| Codex and one Claude | Keep the healthy Claude working; Codex covers the missing role. |
| Codex alone | After bounded actual failures, continue as Codex SOLO and label self-review. |
| Both original Claude executors, Codex unavailable | Agree claims and an integration owner, execute the existing plan and cross-review. |
| One original Claude, Codex unavailable | Continue authorized independent work and preserve a checkpoint for recovery. |
| Uncertain availability | Verify original evidence while independent work continues; silence is not an outage. |

Use `scripts/collaboration_continuity.py --input PLAN.json` after actual API
outcomes change. Its `collaboration-continuity-v1` input has the overall task ID,
workspace UUID, the original supervisor and two executors (each surface UUID,
session ID, workspace and task), `automatic_fallback_authorized=true`, a
SHA-pinned `authorization`, `max_subagents=2`, and bounded `observations`.
The evaluator verifies file hashes and declared identities; the submitting agent
must establish the outcome from the cited native/API evidence. It does not infer
a real outage merely from a self-authored label. Each observation records
original `attempt_id`, session/surface/task,
`at_epoch`, `kind`, `terminal`, optional `http_status`/`retry_after_seconds`, and
a small exact evidence file (`path`, `sha256`). Preserve large original logs;
the small artifact references their exact records rather than copying a GB log.

Three distinct terminal retryable API failures establish a bounded failure
episode. Wait at least60seconds, or a longer provider Retry-After, between
actual attempts; do not enqueue retries behind an in-flight call. API success
resets the count. Unrelated queue/status observations do not erase API outcomes. Authentication or billing errors need their own diagnosis.
Queued input, a missing socket, a tool exit, no reply and stale error text do
not establish API failure. The evaluator emits a plan, not new shared-write or
resource authority; it never sends, changes a lease or marks a task accepted.

## Enforced proactive inquiry

For a new authorized overall task, the original Claude executor enrolls once:

```sh
"$PYTHON" -B "$MAC_SOURCE/scripts/executor_reask.py" start \
  --supervisor "$ORIGINAL_CODEX_UUID" --task "$TASK_ID" \
  --overall-task "$OVERALL_TASK" --authorization-file "$AUTHORIZATION_FILE" \
  --continuing --reservation-inquiries --proactive-hook --file-only
```

This uses the existing registration transaction and pins the original executor,
controller, task and user authority. Installing a release does not enroll all
old tasks. An old episode must keep its original evidence; only its original
executor may stop and explicitly register a new episode under existing user
authorization. Preserve reservations and uncertain input through that boundary.

The existing `cmux_executor_idle_guard.py` Stop registration, through the shared
wrapper, enforces the new task-scoped obligation. It authenticates the actual
original Claude hook, reads its registered state and returns a continuation
instruction. Other tasks and child agents acquire no obligation from inherited
surface variables. It does not launch transport or a child process. A repeated
empty Stop targets the previous prompt plus60seconds, with at most55seconds
inside a single hook invocation, checking stop,
revocation and a changed reply every second without owning the episode lock.
A new reply or first-seen pending handshake bypasses the remaining delay.
If the hook is reentered within5seconds, it returns at the55second cap; the
original runner still enforces the60second inquiry interval. This prevents an API-only tight loop
without abandoning the continuing task. The installed command timeout is75seconds;
unregistered tasks return immediately. The foreground model must run useful work, or:

```sh
"$PYTHON" -B "$MAC_SOURCE/scripts/executor_reask.py" run \
  --caller-uuid "$ORIGINAL_CLAUDE_UUID" --max-seconds 60
```

If only the reply remains and the state is WAITING_REPLY, run another bounded
invocation. Each due round requests review/progress/next work. Formal reservation
does not suppress authorized foreground STATUS; it still does not release the
reservation. The file scheduler remains file-only and is a fallback, not proof
that a model was awakened. No catch-up bursts after a delayed sweep.

Terminal cadence begins at the preceding original native receipt plus60seconds.
An uncertain input is reconciled under its original controller; do not paste it
again or send a duplicate callback. File inquiries continue meanwhile. The
supervisor receiving the inquiry is not the supervisor answering it. ANSWERED
requires the original executor to read the complete reply and exact-SHA consume;
then follow its task, ETA or stop instruction. Explicit user stop, revocation
and task completion terminate the continuing obligation. Stop-hook reentry
does not silently abandon an authorized unresolved episode.

Claude's documented Stop hook does not run for API failures or user interrupts.
It can request continuation, and tool use resets the consecutive no-tool
continuation cap (currently eight without a tool call). Do not change that cap
or claim a Stop hook can restore an unavailable provider
or awaken an exited client. Validate actual hooks after upgrades, including
explicit `--settings` profiles. See [Claude hook semantics](https://code.claude.com/docs/en/hooks#stop).

## Codex unavailable: original Claude peer work

Verify the original same-workspace/same-overall-task peer by session and UUID,
not cwd, title or a surface number. Use the shared sender or an actual supported
peer messaging tool. Agree each CLAIM (scope, output, owner, merge owner) before
writing shared artifacts. Use exclusive claim creation and separate candidates;
a dead process or expired timestamp does not release an unknown shared resource.

Freeze definitions and input hashes before independent checks. At least one
review should use a different relevant reading surface. Publish a summary index
of outputs, checks, disagreements, active operations and next steps. Label the
cross-review as peer review, not original-supervisor acceptance. When Codex
returns, exchange the checkpoint immediately and reconcile existing outcomes;
do not replay finished tasks or silently overwrite work completed during outage.

The whole task may have at most two child agents in flight, not two per Claude.
Use children only for independent bounded outputs. Record parent/child IDs,
scope, expected result and original task binding. Child completion is verified
only after the parent has received and read the matching result; a spawn ID,
tool success or a written file alone is insufficient. Confirm actual model and
elapsed execution if those constraints matter. The parent retains responsibility.

## One maintenance and release path

Report problems and measured experience to the designated maintenance owner
surface1000020040 / A93448E1-E329-493D-9B45-568D5FC0DAD0. Business agents keep
working on their task and submit evidence; they do not edit shared hooks or
publish competing releases. The maintenance owner uses isolated branches,
commits a reviewable revision, records the reviewed commit, fixes findings,
checks the final tree in CI and publishes immutable releases. Thesis-refiner
consumes this runtime rather than copying it. Installation, client loading,
automatic hook execution, native reception, full reading and acceptance are
separate observations. Do not report all sessions healthy from one passing link.

## Scoped work inside the already authorized task

An original supervisor may send a bounded work order directly to its existing
executor without running a fresh formal handshake for each instruction. Pin
the overall task, original participants, authorization, immutable input files
and hashes, output paths, current writer, requested checks and reply marker.
The executor checks all pins and returns `SCOPED_WORK_ACCEPTED|<id>|sha256=<hash>`;
finish with a unique result marker and exact report path/SHA. A scoped work order
does not manufacture a formal harness receipt, new shared-write permission or
RELEASE. Preserve any unrelated original pending pack.

Use the shared ordinary sender or its exact reference serializer; do not hand-
assemble MESSAGE_REFERENCE_V2. A combined user record can prove receipt via the
unique marker after the original fence. Generated compact summaries, transcript-
only user records and queued attachments cannot. Record receipt, complete body
reading, work acceptance and result integration separately.

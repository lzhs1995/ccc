# Native standby and one-shot activation

Status: implementation in progress. The user explicitly authorized native
processes to start before the button press on 2026-09-29 (Asia/Shanghai), with
no model requests until activation. Cold run99 remains a startup failure at
18.199797 seconds. Standby is a new policy, not a reinterpretation of that run.

The current candidate implements the lifecycle ledger, original writer/session
observation, guarded atomic socket paste and a separate no-prompt launcher with
postactivation Hook binding. The launcher supports b/N only. Its caller must
provide a current effective configuration generation; that provider and the
preparation manager are still outstanding. The normal start/worker/recovery
entrypoints reject or skip standby descriptors so they cannot submit a task
while the dedicated manager is unconnected. There is no standby UI entrypoint
or running cohort yet. Tests use synthetic process evidence and isolated local
Unix sockets; they do not establish zero-model readiness or performance.

## Ownership and preparation

A standby cohort contains exactly 50 original native processes in one selected,
authorized workspace. Prepare them unfocused in that same workspace; do not
move them from a shared staging workspace at activation. Preparation is an
explicit workspace option with a visible preparing/ready status. The option
may replenish a consumed cohort only after the preceding batch settles and
current authorization permits it. Do not prepare pools for every workspace
merely because the panel displays them. Only one unconsumed cohort may belong
to a workspace. Global pause, workspace pause/disable and batch cancellation
apply during preparation as well as activation.

Persist a new standby policy before launch. Legacy jobs keep their policy and
original bytes; no old job or exited session can become standby through reuse.
Preserve CODEX_HOME, selected profile, provider, skills, rules, native binary,
working-directory policy and all existing authorization guards. Each original
launch binds cohort/job/index/launch UUID, workspace/surface UUID, PID and
microsecond birth, exact argv, native executable identity, boot identity and
the effective launch configuration generation. Start the native CLI without
an initial prompt. A separate standby claim policy distinguishes that argv
from the existing argv-first-task protocol.

Preparation is not ready merely because a terminal exists. Before activation,
bind the original session through its live writable native UUID writer lock,
including the lock's device/inode, with PID/microsecond-birth/argv inspection
on both sides and the original TUI startup prefix. Do not require the first
SessionStart Hook at this stage: run99's original Hooks were recorded after
UserTurn. After activation that first Hook must agree with the pinned standby
session, and cannot introduce a replacement. The same live PID/birth/argv and
workspace membership must still hold. Native startup and
initial skills discovery must have completed, the composer must be empty and
idle, and no task, user input, queued prompt, pending approval, model request or
automatic title request may have occurred. Readiness must be invalidated when
these conditions change. A private loopback provider will prove zero requests
through a bounded idle interval before any real-provider standby is used.

## Activation

Keep input_read and confirmation_accepted as distinct UI origins. A selected
workspace with an incomplete cohort reports preparation progress; it cannot
claim the one-second startup contract or silently create a cold replacement.
Confirmation text is explicit that activation submits the selected b/N/B
operation to the 50 ready original sessions. Initial implementation covers
b and native N; legacy B remains a separately tested entrypoint and is not
silently converted to the short-check prompt.

After confirmation, acquire the workspace operation lock and verify the
complete unique cohort and live authorization. Bind the UI action UUID, mode,
workspace, boot, cohort generation and effective configuration to a durable
activation record. Existing preparation metadata remains immutable. A repeat
of the same action observes the existing activation; a different action
cannot reuse or overwrite it. Never label this job_created/new_job: true.
Record standby_activated/new_activation: true as its own timing-chain event.

The live manager should already hold the per-original delivery channels and
readiness snapshot. At activation, check each original live identity and
authorization again, persist its one-shot input claim, and issue one atomic
terminal paste with enter through the existing scoped transport. Do not split
paste and submit across unguarded calls. A failed or uncertain RPC is consumed;
it must not be retried by another action or after a restart. All 50 must have
their own input ID and first native turn. A partial activation remains partial
and fails acceptance; do not fill missing slots with replacement processes.

After each original first task and prompt are verified, release its existing
initial hold into the continuation machinery. Retain private-check retry
policy, unique failure/input/next-turn accounting and automatic B cutoff
disabled. Preparation is never counted as a task or continuation.

The ledger transport callback takes a write_guard context manager. Enter it
around the actual socket write, then release it before waiting for the ACK.
This short critical section orders concurrent invalidation against writes;
it must not serialize 50 ACK waits. Fresh identity/authorization callbacks run
after acquiring that lock, since connection setup and lock admission can both
wait while a PID or permission changes. The callback must include the existing
connected input guard's complete live authorization. Original writer-lock path
and device/inode, when provided by the native observer, stay fixed in the
activation and per-input claims and are checked by the postactivation Hook.
Any ledger validation exception
permanently invalidates readiness, including malformed generation values and
missing or changed original evidence.

## Invalidations and recovery

Session, PID birth, launch, surface, workspace, binary, boot, runtime source,
selected profile/provider, skills/rules/config generation changes invalidate
readiness permanently for that cohort generation. A-B-A transitions cannot
restore old readiness. A daemon restart clears in-memory readiness; persisted
claims cannot mint it. Recover an unactivated cohort only by freshly inspecting
all original processes, their unchanged evidence and zero-task state. A
consumed activation is observation-only after restart; never resend.

Cancel or expire only owned idle processes after rechecking exact identity and
workspace membership. Do not signal processes that have switched sessions,
been moved, received user input or become foreign. Mark such slots invalid
and retain evidence. Failed preparation does not automatically loop and create
more processes. Keep handles for every native, worker and sampling process.

## Verification and timing

1. Offline lifecycle tests: exact 50 unique originals; malformed identities;
   session/PID reuse and A-B-A; cross workspace/mode/boot/config; duplicate
   actions; partial claims; storage errors; cancellation; daemon restart;
   stale skills generation; original guards remain effective.
2. Native single-cohort instrumentation check only after independent review:
   prove actual no-prompt argv, original preactivation writer/session identity,
   matching postactivation Hook, idle readiness and zero
   loopback model requests, then one activation/one original first task and
   continuation/cleanup. Single-process evidence is not a 50-session claim.
3. Real UI50: freeze candidate and fixture/evaluator hashes, obtain a fresh
   coordinated network window, cover both UI origins, prove all 50 original
   task starts within one second and report preparation duration separately.
   Report observed monotonic upper bounds and native wall timestamps together.
4. Independent terminal receipt binds action/cohort/job/workspace/mode/boot
   and original identities; collected_at is not the end of the run. A sample
   window must cover the UI origin through this terminal record.
5. Verify b/N/B semantics and sustained500 unique continuations under one
   second before installation/release. New standby code requires its own full
   regression, CI and immutable package review; 7de1c09 does not certify it.

Configuration and skills generation validation is an open implementation
requirement, not permission to use stale cached skills. Shared global config
must not be rewritten for the optimization. No production warm pool has been
started at this checkpoint.

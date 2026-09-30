# Native standby and one-shot activation

Status: implementation in progress. The user explicitly authorized native
processes to start before the button press on 2026-09-29 (Asia/Shanghai), with
no model requests until activation. Cold run99 remains a startup failure at
18.199797 seconds. Standby is a new policy, not a reinterpretation of that run.

The current candidate implements the lifecycle ledger, original writer/session
observation, guarded atomic socket paste and a separate no-prompt launcher with
postactivation Hook binding. The launcher supports b/N only. A separate manager
now collects complete readiness observations and attempts one activation; it
never upgrades identity-only observations to readiness. A generation checker
pins a declared dependency graph, including symbolic-link targets and optional
missing files. The real adapter must preserve the original profile, skills and
permissions and observe native readiness. A complete export of native memory
configuration and warning-free skills are not user requirements. File-source discovery, a live
bootstrap generation bridge, preparation ownership, guarded activation and
first-task observation are implemented separately. The normal start/worker/recovery
entrypoints reject or skip standby descriptors so they cannot submit a task
while the dedicated manager is unconnected. The b/N entrypoint now contacts
the original live owner; an absent or unready owner cannot cold-launch a
replacement. No production standby cohort is running yet. Tests use synthetic process evidence, temporary-file
Darwin events and isolated local Unix sockets; they do not establish zero-model
readiness or performance.

The optional generation event mode arms kernel vnode watches over declared
content, links, and ancestor path identities, then rescans and checks pending
events. A dependency change, registration/read error, fork or close permanently
invalidates the pin. It never accepts an empty queue after consuming a dependency change. Activation
checks use nonblocking event reads plus effective-setting hashes instead of
rescanning every skill file for every slot. Ancestors outside the declared graph
watch identity changes rather than unrelated child writes. Missing dependencies
watch their nearest existing parent and check the specific first missing child
on directory events; unrelated job/log siblings are allowed. Directory vnode
events have no child names, so this proves current absence, not the absence of
a transient appearance-and-removal between checks. Once an appearance is
observed, invalidation is permanent. Existing-file content round trips and
ancestor permission or identity changes still invalidate. The
descriptor cap fails closed; no polling fallback silently certifies readiness.

Manager cancellation uses a short status lock separate from the operation lock
held while waiting for callbacks. Late ACKs remain recorded delivery facts and
cannot overwrite invalidation. Failed invalidation persistence still clears
in-memory readiness; close always shuts down the executor. ACK counts are not
native task-start counts or startup acceptance.

## Ownership and preparation

`ccc_standby_factory.admit` assembles the preparation owner, activation manager
and private UI endpoint under the original workspace admission/input locks.
It creates only empty working directories and immutable job/owner records;
native creation begins at an explicit `prepare()` call. A completed ordinary
batch can precede admission after its worker lock is obtained. Existing standby
jobs cannot yet be replaced, even after first-task confirmation: batch settlement
and replenishment remain separate work. The caller must supply a live source
pin and actual readiness reader. This assembly layer supplies neither a
synthetic readiness proof nor a production source/request observer.

Admission requires an explicit target environment. The same normalized value
is passed to file-source capture and stored in a job-bound private 0600
envelope for the original bootstraps. Only the new surface's terminal identity
comes from its shell; HOME, CODEX_HOME, PATH, credentials and provider settings
come from the selected target. Parent agent identifiers are removed. The
launcher sets its three documented runtime/event overrides, fixes PWD, and
uses execve with that exact environment. Public claims and bootstrap commands
contain hashes, not credential values. Envelope changes permanently invalidate
the bridge; launch guards and subsequent original-process inspection check
the environment binding. Selecting the actual user's target environment is
the production caller's responsibility, with no ambient-shell fallback.

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
idle, and no task, user prompt, queued prompt, pending approval, model request or
automatic title request may have occurred. Readiness must be invalidated when
these conditions change. A private loopback provider will prove zero requests
through a bounded idle interval before any real-provider standby is used.

A single local `/pwd` preparation command is implemented. The
reference source records the original `from_tui/op/ListSkills` after the main
loop has dequeued it and then awaits refresh inline. A later, newly rendered
`/pwd` response can therefore prove that this wait returned. It does not prove
refresh success; preserve native skill configuration and native error/warning
behavior, without adding a new requirement that all skills be warning-free.
The command must be separately consumed and counted as preparation control
input, never as a task. Its actual zero-model behavior, empty pending queue and
composer, original identity and final generation checks still need the native
adapter and production verification. Native run102 observed one original
process, one `/pwd`, and zero requests at its private loopback provider. Its
private configuration disabled plugins/apps and host skill discovery, so it
does not establish production skill preservation, readiness or startup latency.

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

`ccc_standby_timing.py` implements a separate version 2 receipt. The manager's
commit callback runs after durable activation consumption and before submission
of any send worker. `activation-ui.json` binds both original UI clocks and the
runtime hashes to the immutable preparation job, cohort, activation, attempt,
original roster and boot. It does not add job_created to the legacy v1 chain.
`FirstTaskObserver` records a stable task_started observation in
`standby-first-observation-{index}.json`, even if the matching prompt has not
arrived yet. Only its subsequent confirmed first-task receipt can certify that
observation. Reopening the observer preserves the first monotonic timestamp and
rechecks its original transcript prefix.

`activation-terminal.json` is a distinct native-observation terminal. Complete
requires 50 unique original confirmed tasks; timeout, cancellation and failure
remain non-passing. The evaluator reports upper bounds from both input_read and
confirmation_accepted. ACKs and action_finished cannot substitute for task
observations or this terminal. A slow observation leaves latency unproven; it
does not establish that the native task started late. The producer is connected
to the live owner and b/N UI entrypoint. Verification remains offline; real UI
startup latency has not passed.

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

FirstTaskObserver binds the original job, activation, input claim and startup
Hook to the native first task. A not-yet-persisted Hook transcript is pending.
Confirmation is persisted before the exact slot hold is released; unrelated
holds and operator exclusions are retained. Cached and restarted observations
verify the original transcript prefix again, allowing append-only progress.
This observer sends no input and cannot establish readiness on its own.

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
5. Verify b/N/B semantics and ten batches of 50: five workspaces with 100
   original sessions each, all 500 simultaneously alive. Verify each original's
   continuation under one second separately; 500 continuation events do not
   substitute for 500 live sessions. New standby code requires its own full
   regression, CI and immutable package review; 7de1c09 does not certify it.

## Resident entrypoint

`ccc_standby_runner.py` owns one production caller. It reads an explicitly
selected private invocation with its original SHA256; the native environment
is supplied in that file rather than inferred from the runner's shell.
The invocation binds the original native argv, provider, upstream URL,
environment, CCC config, workspace, b/N mode, cmux binary/socket and lifetime.
It preserves selected skills, profile and permissions. Each selected provider
URL is routed through its own local observer while upstream proxy and CA
selection remain separate from native loopback bypass.

Without `--prepare`, the runner admits one cohort and starts no native process.
With `--prepare`, it creates the original 50 idle sessions once; the real b/N
UI still owns activation. `runner-open.json` contains the immutable job path,
cohort/workspace/mode/boot and source hashes for observation to begin before
the real UI action exists. SIGINT, SIGTERM and the explicit lifetime revoke
worker callbacks and close the owner and route handles. They do not kill native
processes. First-task completion keeps the routes alive for continuation.
`runner-closed.json` is never a job or whole-run terminal.

The runner's intent is consumed once. Reusing its evidence directory cannot
start another cohort. Settlement, subsequent cohort admission and a separate
whole-run terminal are still pending implementation. Configuration and skills
sources are pinned for the admitted generation; shared global config must not
be rewritten for this optimization. No production warm pool has been started
at this checkpoint.

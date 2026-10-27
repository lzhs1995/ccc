# Shared daemon caller identity and bounded callback closeout

A managed CLI daemon can inherit the terminal environment of its first client.
That environment identifies the daemon origin, not necessarily the current task.
Resolve a managed Codex caller using the current thread selector, verified tool
ancestry and a unique same-user active native client. Require executable,
PID/birth, TTY and live cmux surface/workspace UUIDs to agree; recheck the proof
around tree reads and before each input. Ambiguity or drift denies input. Never
override CMUX environment values, use focus, relax the workspace boundary or
create a replacement session to make a check pass. Ordinary clients retain
their original checks. This is not an atomic OS snapshot-and-send guarantee.

Use the same resolved caller for transport, harness identity and active-marker
ownership. A correct send check alone does not repair a registry using inherited
workspace values. Test both positive routing and foreign-workspace/identity-drift
rejection. Test the assembled runtime, preserving existing delivery guards.

Hook readers must use that same identity too: Stop callback/ACK checks, executor
closeout and lease lookup resolve workspace and surface together. Reuse one
snapshot only within a hook evaluation; never cache it across calls or rewrite
process environment. Discovery failure leaves task scope empty so ordinary tools
and Stop can continue; transport still refuses unauthenticated input. A managed
caller cannot downgrade to inherited identity mid-resolution. Boolean Stop reentry
still exits before discovery without confirming or releasing a task.

CLI subprocess fixtures must isolate kernel ancestry and marker directories.
The test-only driver executes the real hook main with those private inputs and
preserves the invoking environment; managed identity tests install explicit
ancestry fixtures. Production guards remain unchanged.
`test_hook_identity.py` covers native/foreign markers, leases, returned callback
journals, discovery failure and successive caller changes. Preserve explicit
relative-root rejection even when no shell write target was extracted.

### Native hooks and tool shells have different ancestry

A managed daemon can spawn a hook directly after the intermediate shell execs
away, without CODEX_THREAD_ID. Authenticate the managed ancestor first, then use
the supported hook payload's session_id to select the active native foreground thread.
Any thread selector present in the hook or intermediate ancestry must agree.
The payload cannot supply terminal identity: retain kernel PID/birth/executable,
TTY and live cmux UUID checks, including the process reread after the last tree.
Tool-shell callers keep their separate matching-thread ancestry requirement.

For ordinary macOS terminals, a root-owned /usr/bin/login can terminate ancestry
discovery after exact kernel path, protected file owner/mode and repeated
PID/birth/path checks. If the initial full BSD read is permission-denied, the
[stable-child boundary](root-login-permission-boundary.md) permits only this
protected login using public short BSD reads and its pinned, still-live child.
Neither form supplies caller identity; arbitrary foreign processes remain errors. Hook resolution shares one 2.5-second monotonic budget
under the registered five-second Stop timeout. Nested reads cannot extend it,
and results returned after the deadline are rejected.

Before discovery, Stop/closeout check for eligible active markers, and lease
checks for parseable markers or an explicit absolute artifact root. No marker
means no workspace-owned task gate. An explicit root cannot hide another marker
or invalid root. Even with enrolled markers, unresolved identity cannot impose
a global tool/Stop lock. Preserve the original markers and receipts, select no
task jurisdiction, and leave diagnostics and authorized solo work available.
Explicit artifact-root validation and live transport authentication remain intact.

`test_hook_native_caller.py` covers direct and intermediate daemon children,
missing/conflicting selectors, ordinary login boundaries, drift and bounded
commands. Offline entrypoint tests retain real main/verdicts and private inputs.
These tests do not establish active native-hook execution. Record observed
runtime argv and payload provenance separately: changing hooks.json does not
prove that an existing daemon or thread loaded the new release.

Codex Hook trust is another installation boundary. Its key includes the source
file, event and group/handler indices; its hash covers the normalized handler
configuration, not the script's bytes. A new release path or reordered group can
therefore require review even when the script passed its tests. Compare the exact
discovered identities with `hooks.state`, verify the reviewed scripts separately,
and, within existing installation authority, back up and update only the reviewed
`trusted_hash` leaves. Preserve unrelated settings and previous trust entries.
Verify the semantic diff before starting a new fixture with its original config.
Do not disable hooks or replace the user's config to bypass startup review.

Check refresh semantics for the installed Codex version before trusting from its
TUI: the inspected implementation sets `reloadUserConfig=true` even for one item,
which can affect all loaded threads and MCP connections. Persisting exact trust
leaves without requesting reload and observing a new fixture is distinct from
proving adoption by existing threads. Per-slot generated hook commands need their
own freshly computed identities; a copied trust hash is not sufficient.

## Intermediate process boundary

For both embedded and managed clients, every process between the resolver leaf
and the selected Codex client/daemon must be a supported shell or rtk proxy:
`sh`, `bash`, `zsh`, `dash`, `ksh`, `fish`, `csh`, `tcsh`, or `rtk`.
Use the executable path read from the kernel, not its argv label. The Python
interpreter running the resolver itself is the leaf and is allowed. Intermediate
interpreters and unknown wrappers are refused because they may host another
model CLI; copied or absent selectors do not confer authority.

This bounded compatibility rule closes the inherited-identity path through
another model and applies equally to tools and supported hook payloads. It
does not replace process, foreground, UUID, TTY or drift validation, and is not
arbitrary executable attestation. An unsupported wrapper leaves observer scope
unresolved without blocking ordinary tools or Stop; explicit input and inbox
consumption still require authenticated identity.

## Embedded native clients

From 0.4.39, the shared resolver also handles tools and native hooks whose actual
ancestors include an embedded Codex TUI without a managed daemon. Select that
ancestor, not a global process scan or a resume argument. Its kernel identity,
current foreground record, thread selector, surface/workspace UUIDs and TTY must
agree with the tool and current cmux tree. Recheck the process chain and native
selection around tree reads. Missing or invalid foreground evidence refuses
authentication. An unmanaged `codex app-server` is not an embedded TUI.

Native hooks may use the supported event payload's session ID when no selector
is inherited; any supplied environment or intermediate selector must agree.
The hook payload does not supply terminal identity. A child thread cannot borrow
the root foreground or consume its inbox. Ordinary tools still require their
own kernel-observed thread selector. Discovery failure leaves ordinary tools
and Stop available, while explicit input retains its authentication checks.

The public proof contains `source=embedded-native-client-v1`, `daemon_pid=null`
and the authenticated foreground thread. Reply consumption accepts the real
caller snapshot's `surface_id`, `workspace_id` and `pane_id`, or historical
`*_uuid` aliases. All supplied aliases must match; a conflicting alias cannot
override the native field. The complete original `caller_proof` must still match,
so a new embedded proof never migrates a managed episode.

This identity path creates no daemon, control socket, thread, message or receipt.
In particular, a valid embedded caller does not make native Codex input available
when its authenticated control socket is absent. Installation, actual automatic
hook invocation and original native reception need separate evidence.

## Reused PIDs and optional foreground files

The foreground filename contains the PID, while the authenticated process
identity includes its kernel birth time. A legacy client can reuse a terminated
client's PID without publishing a foreground record. Its predecessor's optional
record must not invalidate a different live caller during the client inventory.

From 0.4.30, first validate the original record's directory and file ownership,
permissions, schema, PID, epoch and thread UUIDs, marker, and unchanged second
read. Only an optional record published before this process's birth, with no
previously verified selection for this exact PID/birth, returns legacy absence.
It does not add that process to the seen set or alter the original record.
Native selection can then use only an exact current resume argument under the
existing unique-client and kernel/TTY/UUID checks; it never uses the old thread.

Required-but-missing observers, invalid or future timestamps, malformed content
and unreadable records make that candidate ineligible; they never authorize an
argv fallback or invalidate a different fully authenticated caller. Keep these
exclusions out of the selected proof so a foreign error cannot become caller
drift. If no candidate qualifies, report the unavailable selections diagnostically.
Recheck the actual selected caller strictly. A changed or missing selected record,
multiple authenticated matches, or a second candidate becoming a match during
the scan still rejects input. Unrelated selection switches and errors do not.
The native receiver reader keeps its endpoint validation. No invalid record
becomes valid and no record is rewritten or deleted. Publish it in the complete
shared release so tools, hooks and native binding use the same resolver. Record
source tests, installed caller proof and actual client hook execution separately.

## Recycled TTY names are not caller identity

Resolve the active native client first, then join its kernel-read
surface/workspace UUIDs to the current tree. Require that exact surface row to
be terminal and to match the client's TTY. An unrelated tree row retaining the
same recycled TTY name must neither select nor veto the caller. Duplicate caller
UUIDs, multiple native clients, a mismatch on the selected row, process drift
and cross-workspace targets still deny input. Never set CMUX identity variables
to a user-supplied value to work around a rejected proof.

Attribute errors to their failing layer: unresolved supervisor native client,
wrong target, missing ACK, provider failure and uncertain delivery are distinct.
A caller-resolution error is not evidence that Claude's identity is invalid.
Once a current-task ACK is validated, reuse it and dispatch the scoped work;
do not reopen a healthy handshake for status checks.

The non-submitting bridge test starts only with proven empty compose. Clear at
most the actual test token length, bounded by the existing cap, rather than
always issuing 256 backspaces. Every key retains the UUID check and the final
compose-clear postcondition remains mandatory. This reduces work without
turning token disappearance or a key return into delivery confirmation.

Ship resolver and harness changes as a new fixed release. Update assembled
launchers and hook paths with compare-before-write backups, preserve inline
post-submit checks and existing task journals, then verify the installed caller
and negative guard cases. Source tests alone do not fix installed entrypoints;
configuration updates do not prove that long-lived clients hot-reloaded.

Completed reports need a bounded callback, not an open-ended polling job. Separate
compose, submitted, queued, native consumption and report acceptance. For a
queued callback preserve the attempt and continue independent work. A native
user record can reconcile a missing receipt only through the original journal,
with the task/report/nonce and original-controller checks intact and zero input.
Do not replay a callback or rewrite an old controller to make it confirm.

After a confirmed terminal boundary assign the next useful independent task.
Use separate task IDs, output scopes and nonces for two original executors;
serialize terminal input when they occupy tabs of one pane. Allow at least the
600-second handshake phase budget and continue as soon as a valid ACK arrives.
If a provider fails, preserve the original session and use authorized SOLO
takeover while the executor recovers. Judge efficiency by accepted work advanced,
not the number of messages, watchers or repeated reviews.

## Closeout without consuming executor time

Read the receipt at the absolute path bound by the task pack. A relative `ls`
from a changed working directory cannot establish that a receipt is missing.
When a report is already accepted, a transport delay is a separate closeout
item; do not repeat the audit or ask the user to relay the same callback.
Record the next independent assignment, and dispatch at a verified safe
boundary. A native API retry is not idle capacity: preserve the session and
continue independent supervisor work under existing SOLO authority.

A post-submit detector may return unconfirmed before the message becomes
visible. Preserve that result and inspect the original attempt; a later exact
receiver message is new evidence, not permission to send it again. Do not turn
this recovery into continuous polling or another model-consuming conversation.

For offline harness tests, fixture workspace IDs must not reach live process
ancestry. The suite runner supplies an ordinary-client process fixture; identity
tests override it with their explicit daemon and drift cases. Do not weaken the
production guard or alter CMUX environment values to make tests or routing pass.


## Carry caller identity through every phase

A valid identity gate is insufficient if discovery, provider detection, marker
ownership, naming or screen reads fall back to the daemon's inherited workspace.
Use the resolved native caller throughout, select actual live tree members,
and address naming/reads with workspace and surface UUIDs. A focused executor
or old title does not identify the supervisor; global dock panels are not
workspace members. Recheck identity before mutations.

Preflight failure before challenge dispatch is a supervisor tooling failure,
not proof of an unreachable executor. Preserve its artifacts, disarm only that
failed task and use a new task/nonce after repair. Cover the assembled path as
well as identity helpers. Once an executor accepts useful work, continue the
main product task instead of adding coordination-only reviews.


## Attribute a blocked Stop to its actual task

With two executors in one workspace, a Stop failure must name the marker that
produced that verdict. Return the failing marker from the same evaluation; do
not rescan the registry and print its first entry. A wrong task hint can send an
executor looking for another task's receipt even when the underlying participant
check is correct. Test callback failures, evidence contradictions and missing
summaries with a different executor's marker first, plus registry changes after
evaluation. Keep callback, report-hash and reentry requirements intact.

For an already-submitted task, later user prompts may separate its marker from
visible activity. That makes screen attribution inconclusive; it does not prove
that the executor never received the task. Preserve the attempt and do not add
reminder prompts, repeat the task or relax cross-prompt evidence checks. Read a
completed report independently and record report acceptance separately from the
transport receipt. While useful work is already assigned, continue supervisor
work instead of creating another communication review or repeatedly polling.



## Compaction and delayed observation are separate from idle capacity

An executor compacting after a submitted challenge is not idle or unreachable.
Record its actual progress and the original handshake handle; do not stack another
challenge or generic continuation message behind it. A pending failure-shaped
receipt is not final while its original process is still observing a late ACK.
Follow that handle to its terminal result, then reuse the exact nonce for supported
read-only recovery. If the handshake ends without proof, record the outcome and
continue independent work under existing authority; do not claim it succeeded.

A null submission timestamp proves only an absent recorded timestamp. The input
journal can show paste and Enter even when a short screen detector saw nothing.
Determine zero input from the attempt, not from that null alone. Keep report
acceptance independent from missing transport receipts and give the executor no
polling assignment merely to make the supervisor's detector catch up.

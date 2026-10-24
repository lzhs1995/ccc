# Supervisor protocol

The supervisor owns the task scope, current authorization and final evidence
review. The executor receives a concrete, bounded deliverable. Shared fixes
have one maintainer; other supervisors submit the exact failing command,
version, caller, stage, original result and reply path, then continue their work.
Use [evidence and handoffs](collaboration-evidence-and-handoffs.md) for task
notifications, bounded review, independent reply paths and the complete inbox
ledger. These reporting rules do not add an idle-turn communication gate.

## Establish current scope once

1. Read the source artifact supplied by the user or owner and verify its pins.
   A supplied report does not require pane-first rediscovery. If no source is
   supplied, inspect the designated executor and obtain its exact artifact path;
   do not select a report by newest mtime or broad keyword search.
2. Resolve the current caller with `cmux-agent self`, then authenticate both
   endpoints' workspace, surface, pane, process and native session. Reuse the
   designated context-bearing executor and preserve its current model.
3. Pin one complete installed release for new work. Keep old attempts, task
   packs, nonces, reports and controller paths unchanged. Existing user
   authorization continues to apply; communication permission alone does not
   expand business scope or transfer shared writes.

Ordinary messages use current same-workspace bindings. An authorized successor
uses [successor-rebind-v2](configuration-and-reasks.md#successor-supervisor-rebind)
for the specified original executor, including across workspaces. Old-owner
approval or old callback settlement is not a prerequisite for this contact.
Identity failure pauses only the affected terminal operation, not ordinary tools.

## One preflight, then the formal task pack

For a new task, run one complete preflight with the existing executor:

```sh
python3 <installed-release>/scripts/mac_harness.py preflight \
  --task-id <task-id> --artifact-root /absolute/task-artifacts \
  --executor-surface <verified-surface> --executor claude
```

Preflight includes setup, helper parity, identity, visible naming,
handshake and validation. Do not stack separate setup/bridge/handshake calls
around it or refresh a healthy current-task handshake for routine status checks.

Send ready prompts in either direction while either agent works, including
native `/goal` turns. Neither an idle receiver nor pausing Goal is a communication
prerequisite. Use the supported native/terminal entrypoint and let the receiver
coordinate execution. Shared write ownership remains explicit.

The optional `bridge-test` diagnostic is **not zero-input**: it types a short owned token,
clears only that token or its known prefix, and requires `clear_confirmed`.
It retains its stricter cleanup conditions and is not part of fresh preflight.
A failed original probe is preserved under its original controller; omitting a
new diagnostic cannot waive old evidence. See
[busy communication](busy-bidirectional-communication.md).

The first challenge gives the absolute pending handshake receipt and required
skill paths. The executor reads them and returns the exact task/provider/nonce
ACK from its real assistant response. Do not put paper review, registry searches
or formal consensus inside the handshake. Use the phase budget; a supervisor's
too-short observation is not executor silence. Recover a genuine late ACK under
the original nonce and controller without resending. Native receipt and ACK are
separate facts; recompute stale validation after its inputs change.

Finalize the task pack with the objective, scope, allowed writes, prohibitions,
input pins, verification criteria, authorization source, role map, required
skill and report/callback contract. Let the harness produce the actual schema;
do not invent pane identities or fill the executor's verdict.

Generate the single-line `TASK_PACK_V2` using `cmux_bridge.task_pack_notice`
(or `task-notice --task-pack /absolute/task-pack.json`) from the finalized
pack. Send it only through `submit_task_pack`; its complete pack SHA and
`READ_AND_OBEY_REQUIRED_SKILL_FIRST` bind the instruction. Do not handwrite
multiline dispatch prompts or disguise a task pack as an ordinary message.

Formal multi-agent plan consensus uses the review artifacts and floor in
SKILL.md when that workflow applies. Do not add it to an already-authorized SOLO
task or reopen accepted research solely to test maintenance.

## Prove reception before attributing failure

Follow [verified native delivery](verified-compose-delivery.md). For new Codex
input, persist NATIVE_INPUT_INTENT with the complete payload, exact native
request and a fresh EOF fence. Use one turn/steer for the freshly observed
active turn or one turn/start for the idle original thread. Do not inspect or
operate on its composer, press keys or enqueue a follow-up. An ambiguous RPC
return permits only original-transcript observation, never another input.

For Claude terminal input, the original PASTE_INTENT fixes the complete payload
and a fresh EOF fence. Paste once, wait for the entire unchanged stable draft,
then submit once using the supported Enter action. Historical Codex terminal
attempts remain under their original controller and are never replayed through
the native route.

Only a newly appended complete native user record after that fence, exactly
equal to the payload, proves `NATIVE_RECEIVED`. Preserve every character.
Keys, empty composer, screen activity, ACK, queue banners and Claude
`queued_command` do not prove reception. A notice receipt proves only the
notice; body reading, execution and business acceptance remain separate.

| Observation | Action |
| --- | --- |
| Affirmative zero-input evidence | Repair the failing preparation/classifier; retain the original attempt |
| Occupied or unknown Claude composer | Preserve the draft; continue independent work |
| Queue/client acceptance without exact native user | Keep pending; reconcile the original attempt without input |
| Exact original native user, receipt missing | Use the original controller's supported settlement; never resend |
| Genuine ACK with an expired observation window | Reconcile the original task/provider/nonce and regenerate derived validation |
| Missing original binding/fence | Keep unconfirmed; do not backfill evidence or choose another transcript |

A null submission timestamp alone is not proof of zero input. Do not retry with
a fresh nonce, force-clear a draft or switch controllers. Supported original
terminal recovery has one shared additional-key budget; native input has none.
Task packs and callbacks use zero-input reconciliation. The narrow original zero-input wire repair described
in SKILL.md is not a general resend route.

The helper is for inventory, inspection and guarded ordinary messages only.
Its executable bytes, same-release adapter and Python must pass parity. Formal
task dispatch and completion callbacks never fall back to an ordinary helper
message or raw terminal sender.

## Callback, acceptance and the next task

The executor freezes its scoped report, then explicitly calls
`cmux_bridge.submit_completion_callback` for that original task pack. The
dedicated entrypoint binds task id, completion nonce, report SHA and receiver
identity. Merely invoking a module/validator or printing DONE does not send a
callback. Wrong imports, controller mismatch and pre-input refusal are local
invocation failures, not Enter-delivery failures.

Read the supplied frozen report or explicitly inspect authenticated active
markers; installed PostToolUse discovers bound file requests, not old reports. Independently
verify the original artifacts, incomplete items, negative controls and final
state. Preserve prior failures; publish corrections separately. Native receipt,
official callback receipt, business acceptance, task disarm and shared-resource
release are distinct. A new supervisor cannot impersonate the old callback
caller or manufacture a missing official receipt.

After acceptance/disarm, return the finding and the next bounded task or a
concrete dependency to the executor. Do not leave an acknowledged request only
in the supervisor's own thread: use its exact mailbox or supported original
route. A TASK reply does not itself dispatch a task pack.

## Waiting without locking either session

All installed workflow hooks are advisory observers. They record provenance and
exit zero, without invoking old gates, emitting turn-control results, sending
notices or forcing reasks. Supervisor PostToolUse may expose authenticated,
deduplicated file requests through supported additionalContext; telemetry failure
does not suppress that separate attempt. No tool event means no hook discovery,
so this cannot wake an idle supervisor. Ordinary tools and honest turn completion
remain available to enrolled and ordinary sessions.

At meaningful tool/task boundaries, explicitly consume the supervisor inbox:
verify pinned bodies and issue a reply bound to caller, supervisor, task,
episode and issued marker. Follow
[configuration and reasks](configuration-and-reasks.md). Default
`executor_ready.py persist` observes one episode for at most 300 seconds.
The separately user-authorized `executor_reask.py` runner asks every 60 seconds
during its explicit invocation, with a polling budget of at most 540 seconds;
synchronous I/O may extend wall time. The independent 0.4.27 file scheduler can
continue authorized, explicitly enrolled requests at 60-second intervals. It
does not enroll legacy episodes, invoke transport or wake an idle model. Verify
its actual sweep separately from installation and from reading the request. A waiting episode after the
command returns does not prove a live runner. Current advisory hooks do not
refresh the legacy idle binding needed by the authenticated-new-dispatch exit.
Write the bound issued mailbox reply first, let the explicit wait return, then
perform a fresh preflight and formal dispatch; a TASK reply alone is not dispatch.
While an original terminal attempt is unconfirmed, reconcile that same message
without resending. Its automatic reask runner uses file inquiries meanwhile.
Independent new information may be sent promptly through the supported entrypoint,
which preserves any actual draft. Preserve old pending attempts even if the
original receiver process exits.

After dispatch, continue independent work. Use one task-bound sentinel where
needed: 7200 seconds for stable long work, 1800 for medium risk, and 300–600 only
for bounded high-risk windows. A screen DONE/BLOCKED token is an observation,
not task acceptance. Bind the current callback token, explicitly stop the
sentinel at the actual terminal boundary, and verify its PID has exited and
state has stopped advancing; STOP_REQUESTED alone is insufficient. A launchd
process without a successful real cmux screen read is not a working sentinel.

For compaction, use 60-second observations and two unchanged checks; distinguish
compact progress from context percentage. Explicit thrashing is immediate
failure. Any intervention is bounded, in the original session and within
existing authority. Never clear context, replace the executor or switch model
as a recovery shortcut.

Classify API errors before retrying. Explicit insufficient balance is BILLING
even when the provider labels it authentication_failed, 403 or /login. Other
authentication, billing, quota and no-account failures permit no blind retries.
Retryable errors require bounded same-session attempts at least 60 seconds
after the preceding attempt ends. The operator's unavailability evidence rule
requires at least 300 consecutive seconds of real failures and a fresh failure
at/after that threshold; actual success resets the clock. This is not a claim
that a new autonomous timer has been installed. Queues, silence and retry
counts do not establish the failure interval.

Before an already-authorized SOLO takeover of shared work, preserve the
checkpoint, freeze executor writes, stop its sentinel and verify a safe single
writer boundary. An error log alone does not prove those steps. Keep original
sessions and resume communication promptly; coordinate shared writes at their
actual handoff boundary. Attribute repeated
continuation inputs to their actual owner using evidence; do not assume the
advisory hook sent them or change another runner.

## Configuration and honest coverage

Preserve providers, credentials, foreign hooks and existing client context.
Use the one pinned configuration guardian with install locking and compare-and-
swap; do not create a watcher per session. It repairs owned registrations across
cc-switch changes, while Markdown only describes the contract.

Verify configuration, actual automatic hook provenance, native reception, ACK,
callback and business acceptance separately for each designated pair. Manual
hook probes and settings counts cannot certify current-client adoption. An
unverified or API-unavailable peer need not hold every other workspace's formal
task. Report the exact remaining boundary and resume authorized work.

## Actual hook thread and bounded inbox

Use authenticated foreground identity for root hooks and preserve its thread id
in public proof. For child hooks, keep actual child, inherited root and verified
parent identifiers separate; parent association does not grant root ownership
or permit the child to consume root mail. Each actual supervisor tool event
exposes at most eight bound file requests, rotates fairly and can repeat an
unanswered reminder after 60 seconds. Cursor updates follow successful context
flush. File exposure is not body reading, reply, native reception or acceptance.
This observer never sends or emits a workflow block.

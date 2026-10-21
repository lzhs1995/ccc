# Bounded collaboration: spend time on the user's deliverable

This protocol clarifies operator behavior. It does not install a hook, add an API,
change an active task's pinned runtime, or prove that a running client reloaded.
Use the task's actual supported bridge and receipt schema. A newer local helper
must not be assumed present in a public or older pinned installation.

## Dispatch only useful independent work

Before dispatch, record the unresolved question, expected artifact, input pins,
write boundary, stop condition and which supervisor work can proceed concurrently.
Prefer complementary work such as implementation versus independent verification.
Do not ask both executors to collect the same data, repeat an accepted audit or
write the same repository. Shared acquisition and shared-file edits have one owner.

Two authorized context-bearing executors can help when both tasks meet that test.
Their task IDs, nonces and output roots stay separate. Input to surfaces sharing
a pane is serialized and the exact UUID is checked each time. Shared Word/Zotero
access remains serial; a second agent does not create a second application lease.
An executor may remain idle when no valuable independent task remains.
Do not fill the panel with busywork. Closing a prior task's shared write scope
does not prevent either agent from receiving new information.

While a useful task is running, prepare the next unresolved question and its
stable inputs. Send ready prompts through the supported entrypoint promptly,
including during callback observation, normal work or native Goal continuation.
The receiver coordinates execution order. A healthy ACK should lead promptly
to finalization and dispatch through the normal task lifecycle. Preserve each
task's evidence and agree on shared write ownership before changing writers;
neither task closeout nor an idle turn is a prerequisite for communication.

State the independence of a review precisely. Grouping or recounting a table
derived by the supervisor verifies that summary, not its derivation. If the
unresolved claim concerns classification, reconstruct it from the original
commands, samples or other primary inputs. Controls must exercise a nonempty
case; an empty set that makes an aggregate predicate true is not a passing
control. Compare the actual classified members when equal totals can hide
different membership.

## Identify the real caller and coordinate authorized resource requests

Resolve the originating client's workspace and surface UUIDs before deciding
whether a peer is local or cross-workspace. A shared daemon can inherit another
client's environment, and the focused window can belong to another task; neither
alone establishes the caller. Reconcile identification with the current client
and role-map evidence. Preserve a mismatch and correct the binding before a send;
do not relocate an executor or assume a new permission is needed from that mismatch.

When the user has authorized direct resource-coordinator communication, the
supervisor carries the scoped request to the verified coordinator through the
supported interface. Do not ask the user to relay that same request. Keep the
original request identity and evidence, preserve receiver drafts and FIFO order,
and do not take over another task's executor or release its resource. If the
current interface cannot deliver safely, retain the pending request, identify the
transport limitation and continue independent authorized work. Do not turn that
limitation into a repeated request for permission already granted.

This is an operator responsibility, not a claim that cross-workspace transport,
queue promotion or caller-identity recovery has been implemented. Check the
actual pinned interface; a prose change does not create a missing capability.

## A handshake proves identity, not scientific agreement

The initial challenge includes the absolute pending receipt and required skill.
The executor reads them, checks identity/nonce and returns the exact ACK before
research, registry searches or source audits. Reuse a healthy current-task
handshake. Set a finite observation budget compatible with the harness phase;
a shorter local wait is not evidence that the executor failed.

A late ACK belongs to the original challenge. Use a supported read-only recovery
path against its original evidence; do not issue another challenge just because
the first screen detector timed out. Formal consensus rounds apply only to an
explicit formal consensus phase, not to channel readiness or ordinary status.

## Both directions require exact native reception

Use `submit_task_pack` for task dispatch and `submit_completion_callback` for
completion, from the bound guarded bridge. A helper return code, echoed prompt,
report file or disappearing marker is insufficient evidence of delivery.

Keep the following distinctions in the evidence ledger; they are descriptive
states, not new runtime receipt enums or a reason to require extra ACKs.

| State | Required observation and next action |
| --- | --- |
| Not submitted | Complete payload remains in compose. Preserve the draft and original attempt; no blind Enter, Tab, paste or force-clear. |
| Submitted | The supported bridge verifies submission of the exact payload to the bound target. Submission alone proves neither queue position nor consumption. |
| Queued | UI queue or Claude queued_command is pending, not reception. Observe the same attempt without resending or promoting it over existing input. |
| Received | Only a new complete native user after the original PASTE_INTENT EOF fence, bound to the same receiver process/session/transcript and exactly equal to the payload, yields NATIVE_RECEIVED. Reception does not prove execution. |
| Processed | The requested business action has its own outcome evidence. A read notice is insufficient; a resource release needs the actual lifecycle result. Record success or failure separately. |
| Unknown | Evidence is missing, truncated or ambiguous. Preserve identity/journal and do bounded read-only recovery. |

Recovery may not change the task, report hash, nonce or original attempt to make
it pass. If the pinned version lacks a read-only recovery API, checkpoint the
evidence for the supervisor; do not emulate it by recalling a sender that may
paste again. A native record must be a real receiver event, not a quote, tool
output or supervisor-authored note. Only a supported verifier can turn it into
the required receipt; narrative acceptance cannot forge one.

Do not wait for `supervisor busy=false` as an extra callback prerequisite. Invoke
the guarded entrypoint once and let it assess the input state. Do not manually
add a Tab-to-queue workaround or interrupt an active command to clear compose.

Full native proof, the shared one-key recovery budget, permitted task-bound
diagnostics and finite idle/CCC waiting follow
[verified native delivery](verified-compose-delivery.md).

## Close callback work instead of engineering indefinitely

Completion has separate facts: business report accepted, original callback
verified, task monitor/marker disarmed, shared writes handed over, and any shared
resource released. None implies the others or gates ordinary communication.

The initial callback is followed, if needed, by one bounded read-only recovery
window under the existing attempt. At its end, preserve unresolved transport
evidence and hand it to the supervisor. Do not launch a second watcher, poll each
second, lengthen the retry budget, delete the journal, or rewrite a frozen report.
An ordinary honest handoff may end in WAITING_SUPERVISOR under the bound closeout
checks; it creates no receipt, acceptance or disarm.
The supervisor continues independent authorized work while resolving the missing
fact through the existing lifecycle. Both agents may exchange ready prompts;
the receiver schedules separately scoped work without replaying the pending input.

When the original callback is verified and the supervisor has disarmed the
task's marker/monitor, end that task's callback probes. Do not require another ACK
of the closeout notice. Do not reopen it to improve a classifier or collect more
conformance measurements. If the runtime still blocks or restarts work, retain
that failure and make it a separate maintenance scope; do not claim a prose rule
fixed runtime enforcement. Use supported message delivery during an active
command; do not use raw Ctrl-C to clear a draft. Reuse the original executor
without waiting for an idle turn. Verify shared-write handoff before a different
writer edits those same files.

Persistent provider failures follow SKILL.md and the
[failure-window evidence rule](availability-and-resources.md#retryable-claude-api-failures-evidence-before-takeover):
finite same-session retries for retryable Claude API failures, at least 300
consecutive seconds from a real failure to a fresh failure, and reset on any
actual API success. Counts, queued input and unknown outcomes do not prove this
interval. Billing/auth/quota failures permit no blind retries; an explicit user
stop is separately classified. Continue independent research during the wait.
Authorized SOLO takeover still requires frozen executor writes and a verified
shared-write handoff. That requirement does not restrict communication, lengthen
callback probes or release resources.

## Measure benefit and state the limits

At each accepted deliverable record the useful finding or unresolved question,
what was independently checked, what was only reused, and coordination overhead
(handshake, callback, recovery, resource wait). Do not add unlike counts or use
agent uptime as productivity. A cached receipt check is not a new model run or a
new scientific review; mixed content/liveness checks need separate denominators.

Give the main deliverable priority over lesson maintenance. Capture a short
reusable lesson after accepting the relevant evidence, then return to the task.
Do not expand communication tooling or repeat completed research merely to
produce a richer retrospective.

An observed failure pattern is accepted work followed by prolonged callback
diagnostics despite a confirmed original receipt. The remedy is the explicit
terminal boundary above, not a larger retry limit. A document change, offline
test, installation, active-client loading and actual delivery are five distinct
claims. Public lessons contain generic rules; private transcripts, research data,
accounts and machine-specific paths stay in the task evidence.

## Verify the operation at the real boundary

A shared tool daemon can report a different terminal from the requesting agent.
Before calling a surface mismatch a migration, reconcile the current thread's
unique live client, process birth, TTY and live cmux UUIDs. Forward only that
verified client's identity to its child operation. Do not select a client from
the desired surface number or rewrite shared daemon state. Ambiguous evidence
still stops the send; this check prevents asking the user to move an unchanged
terminal after a false mismatch.

Use a captured real response to test the narrow failure before dispatching more
review. A fixture derived from intended values can miss differences in the live
application. On a pre-write failure, preserve the original operation and its
zero-write evidence, release its resources through the existing coordinator,
and prepare a successor only for the observed difference. This is not permission
to replay the old operation or weaken unknown-value checks.

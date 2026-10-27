# Lessons Converted Into Controls

| Failure | Maintained rule | Regression surface |
| --- | --- | --- |
| Context was lost by restarting a peer | Reuse exact context-bearing session; never clear | test_cmux_agent_panel_guard.py |
| Screen DONE never reached supervisor | Report hash + explicit nonce-bound callback + exact native receipt + original-controller settlement | test_r3_hardening.py |
| Prompt echoed its own ACK | Parse assistant blocks, not pasted challenge | test_mac_harness_receipts.py |
| Old surface ref addressed another panel | UUID/workspace binding, fresh identity | test_workspace_uuid_alignment.py |
| Idle placeholder blocked real work | Exact known virtual suggestions; unknown text remains occupied | test_r3_hardening.py |
| Callback delivered twice | Classify delivery; never blind-resend | test_r3_hardening.py |
| Compact stalled for 15 minutes | 60-second compact samples, two unchanged checks, bounded intervention | test_cmux_executor_sentinel.py |
| Billing errors caused endless retries | Classify explicit insufficient balance before generic login/403; preserve transport and budget priority. Verify a safe takeover separately | test_availability_contract.py, test_executor_availability.py |
| A stale task or unresolved caller blocked ordinary tools and Stop | Workflow hooks never run old gates; explicit senders retain endpoint and draft checks. Bound file-request context cannot force reasks or imply callback acceptance | test_workflow_advisory.py |
| A clipped git branch made an empty Claude composer look occupied | Accept the measured ellipsis only at a physical footer row end under matching full borders; preserve real drafts and unknown layouts | test_live_opus_footer.py |
| File-only inquiries had a writer but no automatic reader | Supervisor PostToolUse authenticates and deduplicates requests independently of telemetry; exact mailbox reply precedes fresh dispatch. No idle-supervisor wakeup or automatic settlement claim | test_workflow_advisory.py, test_supervisor_inbox.py |
| Guard existed but did not run | Both clients wired; benign execution and negative entrypoint tests | test_install.py, test_r3_hardening.py |
| File-edit prose was parsed as a shell launch | Check tool identity first; distinguish literal strings in a strict quoted Python stdin heredoc using AST byte offsets. Inspect actual executable arguments, aliases, argv expressions and shell wrappers separately; imports alone do not turn prose into commands. Retain conservative scanning for unresolved executable values, expansion, pipes and adjacent shell commands; cover command/cmd inputs and UTF-8/CRLF in real hook processes | test_cmux_agent_panel_guard.py |
| Later PASS hid earlier objections | Explicit resolution links; fresh artifact hashes | test_r3_hardening.py |
| Concurrent evidence heartbeat was lost | Atomic state and concurrency tests | test_heartbeat_concurrency.py |

## Probe Discipline

Use the producer's actual schema. Null, false, absent and zero are different
observations. A SHA of empty bytes is evidence of empty bytes, not proof of a
failed read: independently check the read outcome and byte count. Never reconstruct
hashes from prefixes. A generated report cannot contain timestamps produced only
after that immutable report was read; use a separate readback receipt.

Run positive and negative controls with measured branch/mutation hit counts.
If the harness itself fails, correct the probe and preserve the failed observation;
do not turn a zero-hit mutation or an exception into a product defect or a PASS.

## Avoid Process Inflation

See [efficiency and task closeout](efficiency-and-closeout.md) for phase budgets,
bounded callback settlement, authorized SOLO continuation and acceptance records.

Apply [bounded collaboration](bounded-collaboration.md): freeze the business
report, reconcile the original send without replay, and stop task-specific
callback diagnostics after verified receipt and marker disarm. That protocol is
an operator rule, not a claim that a new runtime hook has been installed. Track
accepted findings and coordination overhead separately; two occupied panels do
not establish a speedup.

Scope each phase to a concrete outcome. Keep operational cleanup, product fixes,
historical evidence and final admission separate. Do not reopen settled findings
without new evidence. A repeated unproductive review is not progress. Use the
formal consensus floor only when formal multi-agent consensus is the requested
workflow; do not invent peer review during a SOLO task.

The detailed incident archive predates this public packaging and contains local
observations. It is not normative for model choice, input ownership or present
provider capability. SKILL.md and the tested public defaults take precedence.

## Direct input and durable file reminders

New Codex messages use the authenticated original thread's native steer/start
route once, with an input intent and fresh journal fence. A queued UI banner or
successful key is not reception. Never convert an old pending callback into a
new native send. Reconcile its original evidence without input.

Public identity proof must retain the authenticated foreground thread. Record
actual, inherited and parent thread identifiers separately: an inherited root
selector is not a child's identity and must not expose or consume the root inbox.
Bound file reminders to eight per tool event, rotate fairly, and advance cursors
only after context is flushed. A failed flush must leave a reminder discoverable.

An authorized 60-second file scheduler needs explicit live-pair enrollment,
immutable-controller verification, finite capacity and time budgets, and
nonblocking locks. File-only ticks do not reconcile transport or read transcripts.
Archive real episode transitions, not every minute; preserve unknown pending
attempts. Registration, actual execution, hook context and model reading are
distinct observations. Neither a scheduled file write nor a hook can promise
to wake an idle model. Regression coverage belongs in test_hook_thread.py,
test_supervisor_inbox.py, test_executor_reask.py and test_executor_reask_scheduler.py.

# Evidence and handoffs

Send a ready prompt through the supported entrypoint while either agent works.
The receiver schedules its response. Preserve actual drafts, original attempts
and shared write ownership. These evidence rules help agents coordinate; they
do not require an idle turn, pausing Goal or waiting for an unrelated callback.

## Child tasks and result integration

Use child tasks only within an authorized task and delegation scope. Record the
following separately, with the actual task id, source and timestamp when present:

| Observation | What it establishes |
| --- | --- |
| Foreground tool result | What the invoking tool returned |
| Child transcript start | The child actually started |
| First assistant record | The child produced an assistant record |
| API error | The observed request failed; keep the error classification |
| Task completion | The reported task ended, with its real outcome |
| Notification enqueue | A result notification entered a queue |
| Notification dequeue | That recorded notification left the queue |
| Result in main context | The parent can actually read the matched result |
| Parent integration | The parent reviewed and incorporated the scoped result |

A missing `is_error` flag does not prove success. Error text in a foreground
result does not by itself prove rejection before the child started. Count only
notifications identified as child-agent results, deduplicated by task id; do not
include background shell notifications. A dequeue without a task id or other
verified pairing metadata cannot prove receipt of a particular child result.

Use bounded observations after meaningful tool events and keep independent work
moving. Do not poll until a desired success appears. Long read-only reviews may
run in the background when authorized and supported by the client; otherwise
split foreground work and state its expected duration. Five minutes is a design
reference, not a new mandatory cutoff or background-execution requirement.

## Read-only claims and peer checks

Keep both the tool audit and task-directory changes, with their coverage limits.
No observed write call does not prove zero writes. A shared temporary-directory
diff does not identify its author. Attribute changes only with matching evidence.

A peer check names:

- the author, source path, complete SHA256 and exact byte range or whole-file scope;
- the claim retested, method, actual result and negative controls;
- unexamined material, disagreements and any dependence on the original author;
- separate reply paths for each reviewer or recipient.

Keep drafts at independent paths and freeze the source before a hash-bound
review. The main executor checks key facts, resolves contradictions and reports
remaining gaps. Re-anchor page numbers, table labels and positions in the
original text or document geometry before citing them.

An authorized read-only substitute may fill a specific gap with the same evidence
limits. This does not transfer shared editing, export or publication ownership.
Do not label a dependent recheck as an independent review.

## Receipt, cadence and complete inboxes

Keep RPC acceptance, queueing, native receipt, full referenced-body reading, ACK,
callback, business acceptance and exact RELEASE separate. A file's existence or
queue banner cannot stand in for native delivery. Files are an explicitly
recorded alternate channel; supported new native messages remain available.

Verifying a task pack's SHA proves integrity, not task acceptance. An executor's
reported acceptance time is a self-report until the corresponding native record
and complete acceptance statement are checked. If no acceptance statement was
sent, record that fact; never substitute the pin-verification or report-writing
time. Keep native receipt, body reading and an explicit `TASK_ACCEPTED` statement
separate even when they occur close together.

Calculate a deadline from the task's specified starting event only when that
event and its timestamp are evidenced. An unknown start stays unknown; it does
not justify a fabricated deadline or stop independent authorized work. When a
callback endpoint is unavailable, the supervisor may review the actual scoped
files and issue an authorized exact RELEASE, preserving the callback failure.
File review and resource release do not establish callback delivery.

For maintenance inboxes, persist a cursor and a path/content-hash ledger, use a
suitable overlap window and enumerate all unprocessed items. A latest-five list
is navigation, not proof of complete coverage. Process fairly and advance the
cursor only after preserving the actual disposition. A changed body is a new
version; retain the earlier decision and any superseding correction.

Use exact enqueue and observation windows when explaining latency. Mark untested
causes as untested. Terminal reask cadence is measured from native confirmation,
while file cadence is independent. An uncertain original is reconciled without
replay; independent new information can be sent immediately through the supported
entrypoint. Neither reasoning length nor compaction counts create a communication
ban or prove why delivery was late.

## Ownership, review deadlines and recovery

Merge and publication ownership comes from the user or an authorized supervisor.
One designated maintainer integrates shared runtime, hook and skill changes;
other sessions submit exact evidence and continue their assigned work. Existing
authorization remains valid and need not be requested again.

Status reports name current work, unprocessed items and the next supported action.
Give an ETA only when evidence supports it. Combine duplicate increments while
preserving useful new facts; neither a one-fact limit nor callback settlement is
a prerequisite for timely communication.

Before asking the user to intervene, inspect the current binding and relevant
original-task evidence. Do not impose a global transcript scan or wait for a
callback before sending a ready prompt. A handoff records exact files, functions,
evidence, completed verification and the next executable action. Resume there
after compaction rather than restarting the investigation.

A bounded review may write its outline early and finish as `PARTIAL` or
`NOT_EXAMINED` at its real deadline. Do not silently add 60 seconds, reopen an
accepted round or upgrade partial coverage to PASS. Future task packs state the
budget's starting event, deadline and expected first evidence.

## Runtime provenance and explicit profiles

Distinguish CCC, native Goal, scheduled tasks and Stop hooks by their actual
process, configuration and event evidence. Repeated turns alone do not identify
which component initiated them. Do not pause or reconfigure user Goal/cron to
manufacture an idle communication window.

Verify actual model, environment and explicit `--settings` sources separately.
An observed precedence relationship is specific to the tested client version
and launch path; one sample does not establish a universal ordering. Updating
profile rows in a database does not prove an explicit settings file changed or
that every active client loaded it.

For an identified profile use the shared
[explicit profile repair](configuration-and-reasks.md#explicit-settings-profile-repair).
Preserve non-hook fields, foreign hooks, model, credentials and original sessions.
Do not restart a session to demonstrate hook adoption. Configuration, automatic
hook provenance, authenticated thread, native reception and accepted work each
need their own evidence. An unverified thread remains unverified.

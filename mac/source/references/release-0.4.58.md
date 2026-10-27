# 0.4.58: preserve drafts and immutable inquiry notices

A refused raw send followed by unconditional Enter can submit a user's unfinished
draft. Concurrent writers can also combine two notices in one input area. New
terminal attempts therefore retain the shared pane/target locks, check the input
state before mutation, and make one protected `paste --submit` request without
force. Parse the host result: submitted, pasted without submission, host queued
and unknown are distinct. A failed or unknown operation permits observation of
the original attempt only. Recovery requires the entire original input and the
original unused one-key budget; a matching marker alone is insufficient.

Normal work, native Goal and compaction remain valid times to communicate.
This change preserves real drafts and dialogs; it does not restore idle-turn
gates. A natively authenticated Claude 2.1.303 can have no agent registration in
cmux. Only then may a complete, actually empty Claude input frame supplement the
host state. An incomplete frame or unknown query result never grants input.

The input-state check and host paste are separate RPCs. They do not guarantee
atomic exclusion of concurrent user typing or a process change inside the host
request. Post-request identity checks prevent confirmation and further input
after a change. Raw senders must join the same input locks; returning success is
not reception. Keep this limitation explicit until the host supplies a single
conditional input operation.

Supervisor inquiry notices now point to immutable snapshots of the bytes already
read, rather than a rolling CURRENT file. A private staging file is completely
written, made read-only and synced before publication. A nonblocking publication
lock and deterministic staging name recover an interrupted pre-link write or
post-link second hard link on the next attempt for that SHA. Other staging names
are not scanned or deleted. Conflicting final snapshots are not overwritten;
unvisited orphans remain subject to authorized archive handling. A failed
snapshot skips only that request and records bounded failure telemetry.

Original controllers, pending attempts and continuing inquiry registrations are
preserved, including the complete verified 0.4.57 controller and its original
issuer scope. Exact known CLI draft/dialog refusals record zero actual input
separately from the two retained intents; generic rc=1 remains unknown. This
diagnostic does not automatically retry or reset an attempt. Source tests,
installation, automatic hook use, native user receipt,
full body reading and task acceptance require separate evidence. The complete
current delivery contract is [here](verified-compose-delivery.md).

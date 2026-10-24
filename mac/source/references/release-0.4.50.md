# 0.4.50: bounded scheduler history

A single overwritten scheduler STATUS cannot show which invocation ran, where a
sweep stopped, or whether a later invocation encountered the previous worker.
The scheduler now keeps a separate bounded diagnostic history after each sweep.

## Diagnostic contract

`executor-reask-scheduler-v1/SWEEP-HISTORY.json` retains at most 120 entries and
512 KiB. Entries identify the source, process, sweep and phase durations,
registration order, rotation position, pending enrollment and compact episode
results. A busy enrollment keeps its filename without inventing a caller.
Prompt bodies and full reservation payloads are not copied into this history.

A separate nonblocking `history.lock` and 0.25-second runtime budget keep
diagnostics independent of file inquiries. Contention, budget exhaustion and
uncertain writes are explicit outcomes; they do not change completed inquiry
work. Invalid, oversized or unsafe existing history remains untouched. A failure
after replacement can leave the new file visible and is reported as unconfirmed,
not as proof that no write occurred.

An invocation that sees `SCHEDULER_BUSY` can record its own observation without
overwriting the active worker's STATUS or rotation cursor. Its PID and timing
describe itself, not the other worker. Missing history is not proof that no
invocation ran.

This is diagnostic coverage, not a demonstrated fix for historical long sweep
gaps. The historical root cause remains undetermined. Source tests and isolated
timings do not establish real executor adoption or end-to-end latency.

## Historical compatibility

The complete immutable `0.4.49-689dda6455be` release was independently verified
against commit `251bc8c107f3b44b49d7c9bdac532590d2056df8`: 304 members and
4,366,215 source bytes. Its manifest SHA256 is
`67c72f782ec5b055fe534b20b7bc7ba876a5a4914685d2877a32693d3ea81279`.

The 14 controller members, explicit reservation-inquiry capability and original
16-controller issuer scope are statically pinned. Earlier issuers gain no
reverse or future authority. Compatibility executes no historical code and
does not rewrite an old controller, reservation, authorization or pending input.

## Adoption and communication

Installing this release updates owned entrypoints and jobs. It does not
register an executor or authorize new inquiries on its behalf. Existing
registration, automatic hook exposure, complete body reading, exact mailbox
reply and `consume --reply-sha256` remain distinct acceptance evidence.

Send ready prompts in either direction during normal work and native Goal.
Do not pause Goal or wait for idle solely to communicate. Preserve identity,
actual drafts, original-attempt deduplication and shared write ownership.

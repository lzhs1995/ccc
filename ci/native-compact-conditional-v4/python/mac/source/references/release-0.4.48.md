# 0.4.48: auto-compact footer hints and receipt evidence

The measured Claude elapsed footer can end with a percentage hint such as
`3% until auto-compact · /model opus[1m]`. This release recognizes that exact
suffix after a valid elapsed field outside complete composer borders. The
percentage accepts ASCII digits from 0 through 100. Split footer rows and the
already supported busy/Goal layouts use the same parser.

A remaining-context hint is not active compaction. Real compacting/reconnecting
states and unknown or malformed footer text retain their original refusal.
The parser never trims, repairs or normalizes the composer. New messages still
require the complete exact payload and supported native delivery. Queued input,
native receipt, acceptance and callback settlement are separate; historical
attempts are reconciled through their original controller, never replayed.

Two historical frames failed under 0.4.47 and parsed under this candidate with
identical composer bytes. Those frames were captured after the original dispatch;
they establish a parser defect, not the precise state at the moment of dispatch.
The original tasks subsequently queued and were reviewed, so this release does
not authorize resending them. Public regression fixtures use synthetic content.

The complete `0.4.47-46395e3b21e5` manifest and all 301 members were checked
against the released Git tree. File scheduling preserves its original 14-member
controller, scheduler, descriptor, explicit reservation-inquiry enrollment and
14-controller historical issuer scope. Older issuers gain no authority over
newer controllers. No historical code is executed and no episode is migrated.

[Evidence and handoffs](collaboration-evidence-and-handoffs.md) distinguishes
integrity verification from explicit `TASK_ACCEPTED`. Acceptance timestamps need
the corresponding original evidence; an unknown budget start remains unknown.
Authorized file review and exact RELEASE may proceed while a callback endpoint
is unavailable, but neither establishes callback delivery.

This release retains prompt-ready bidirectional communication during normal work
and native Goal, without pausing Goal or requiring idle turns. Identity, real
drafts, original-message deduplication and shared write ownership remain intact.

Offline tests, publication, complete installation, actual automatic hook loading
and original-session native receipts are separate evidence. An absent original
Codex control socket remains an unresolved runtime endpoint problem; a footer
fix or disk configuration update does not repair it. The inherited partial
Claude review is not an overall PASS for this release.

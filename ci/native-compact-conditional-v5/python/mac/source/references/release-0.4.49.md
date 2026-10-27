# 0.4.49: explicit inquiry re-enrollment preserves reservations

An executor stopping an older continuing-inquiry episode could not explicitly
register a replacement with reservation inquiries: the registration preview saw
the correctly preserved old stop file and rejected the new episode before commit.

Only an authenticated original executor's explicit `--resume-stopped` registration
of a genuinely new episode can preview past that stop. The preview cannot publish
state. The existing registration transaction archives the original state,
enrollment and stop bytes, then clears the exact previous stop after commit.
Failed registration and concurrent stop handling retain their original safeguards.

The new episode preserves recorded formal reservations for the same caller,
supervisor and workspace. A missing marker is not RELEASE. New continuing file
inquiries during those reservations require the executor's explicit
`--continuing --reservation-inquiries` and pinned authorization. Installing this
release does not add that permission to an old episode. Its old controller,
pending terminal message and journals remain intact; do not edit or replay them.

## Historical protocol

The complete `0.4.48-3b4304d24f90` immutable manifest and all 303 members were
verified against the published commit `2c5728bc17f1172910174878949b336b837905dc`.
The manifest SHA256 is
`1044ff4ba71cc9cb7ea51fd48bc73db366b5850f2e384935b31399f87e756f5c`.
Its 14-member controller, explicit inquiry capability and original 15-controller
issuer scope are pinned separately. Old issuers gain no support for future
controllers. Runtime compatibility interprets verified data without importing
historical source or changing old enrollment authority.

## Evidence and adoption

The original 102-check regression and 45 protocol/release checks passed. A separate fixed-snapshot experiment
passed four checks on the candidate, while 0.4.48 failed the explicit stop/resume
case. Synthetic tests also preserve a missing-marker reservation, reject a
foreign executor or absent resume flag, and reject a publishing preview bypass.
The experiments use isolated state and file inquiries with zero terminal input.

The original executor must perform any live adoption through its supported
entrypoint and existing authorization. Preserve the old episode and transaction
evidence. Verify actual registration, reservation inquiries, supervisor exposure,
body reading and reply consumption separately. Offline tests and installation do
not establish these observations. The outstanding original Claude review remains
separate from source validation.

Prompt-ready bidirectional communication during normal work and native Goal
remains enabled. Neither agent needs to pause Goal or wait for an idle turn;
identity, real drafts, original-attempt deduplication and shared write ownership
remain in effect.

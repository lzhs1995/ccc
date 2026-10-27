# 0.4.51: reply guidance during ongoing work

The automatic PostToolUse reply advisory still told executors that a supervisor
must observe a fresh *idle* preflight before sending a separate task pack. This
contradicted the current communication contract and appeared in a real executor's
automatic hook output.

The advisory now tells both agents to send ready messages through the supported
entrypoint during normal work or native Goal. A separate task pack still requires
fresh identity, handshake and validation. Neither agent needs to wait for idle
or pause Goal solely to communicate.

Exact reply reading and original-controller consumption remain explicit.
Consuming a reply does not dispatch a pack or release a formal reservation.
The advisory itself sends no input and cannot prove reading or acceptance.
Draft protection, original-attempt deduplication and shared write ownership remain
independent of normal receiver activity.

## Existing 0.4.50 episodes

The complete immutable `0.4.50-3dd2945b5069` release was statically verified
against commit `ba476465060647cab89c09e1adff04b835ce8fc8`: 306 members and
4,393,937 source bytes. Its manifest SHA256 is
`4c91053a01d9a69e497635452330f15ed06a3931d4b50886c1e42e3d36bee2cc`.

Its 14 controller members, explicit reservation-inquiry capability and original
17-controller issuer scope are pinned together. Earlier issuers gain no reverse
or future authority. Compatibility executes no historical code and does not
rewrite episodes, controllers, reservations, authorizations or pending input.
An already enrolled 0.4.50 executor does not need to stop or register again just
to keep its authorized file requests running.

## Evidence boundary

The verified defect is misleading automatic advisory text. It does not establish
that the sender still enforces an idle gate or explain all historical stalls.
Offline tests cover the existing hook output path and old-issuer authorization
boundaries. Publication and installation do not establish active-client adoption,
body reading, callback delivery or completed real reservation-inquiry cycles.
Those observations remain separate and are collected along real business work.


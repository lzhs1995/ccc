# 0.4.37: bounded scheduler verification reuse

Repeated full release reads can consume the file scheduler's sweep budget before
it reaches a waiting executor. Each sweep now owns one release-verification
snapshot. Its first check verifies the complete pinned release. Each reuse stats
every verified member and parent directory, comparing device, inode, size, mode,
mtime_ns and ctime_ns. A changed or missing path invalidates the snapshot and
requires complete verification. A new sweep starts without cached trust.

CURRENT approval remains a complete release check. Binding, enrollment,
authorization, STOP/revocation, reservations and replies are read live. The
256-enrollment cap, 10-second budget and fair cursor remain. The sweep result's
`release_verification` object records `full_verifications`, `reuses`,
`invalidations` and `scope=ONE_SWEEP`.

The reviewed immutable 0.4.36 producer and issuer retain their exact 13 controller
members and existing boolean reservation-inquiry capability. Historical issuers
cannot enroll future controllers. Compatibility does not consume replies, send
input, migrate an episode, settle a callback or execute an old release's code.

The scheduler delta passed 141 related tests. A read-only comparison on eight
live enrollments produced identical decisions: validation took 8.108 seconds
without sweep reuse and 3.324 seconds with reuse; file reads fell from 5,031 to
981 and bytes read from 64,893,348 to 13,030,929. Each mode was sampled once in
sequence. These figures are not a latency distribution, complete sweep benchmark
or proof that production detection latency is fixed.

Accept live behavior separately: observe an actual scheduled sweep and its
verification counts, then correlate the original request/reply and detection
times. Complete body reading and exact original-controller consumption remain
executor actions. Automatic hook invocation and original native callback receipts
also need their own evidence. Installation alone proves none of these.

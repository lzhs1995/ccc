# 0.4.42: explicit reply-consumption contention

An executor can try to consume a verified supervisor reply while the file
scheduler holds the same episode lock. Previously the nonblocking flock failure
escaped as a traceback. `consume` now returns machine-readable `EPISODE_BUSY`
with exit code 2, allowing the executor to retry its original arguments.

```json
{
  "outcome": "EPISODE_BUSY",
  "input_operations": 0,
  "consume_result": {
    "status": "EPISODE_BUSY",
    "consumed": false,
    "episode_state": null,
    "reservation_paused": null,
    "retry_same_arguments": true,
    "retry_after_seconds": 1
  }
}
```

Null state means unobserved. The busy path performs no episode load,
authentication, consumption, rearm, write or terminal input. The one-second
delay is advice to the caller, not an automatic retry or a lock-availability
guarantee. Once the lock is acquired, the original executor identity and exact
reply SHA remain mandatory. A `BlockingIOError` from the locked operation
remains a real failure; only the lock-acquisition exception becomes busy.

The focused consumption, lifecycle and registration suites passed 78 tests in
16.358 seconds. Coverage includes actual flock contention, CLI JSON and exit
status, no unlocked reads or authentication, unchanged episode bytes, one exact
rearm after release, and a locked-I/O negative control.

The cross-release file interpreter also recognizes the installed
`0.4.41-28b36490b149` release after CURRENT advances. Its 286-member manifest
has SHA256 `693cf37272b326392bfdb5506746ca64e6582c90a618785c8b91e20bd05a8222`.
The original controller pins, including the shared coordination-path module,
remain required. Its historical issuer can enroll only itself and the reviewed
releases through 0.4.37 that it originally supported. It cannot grant future
authority; no compatibility permission is inferred for 0.4.38 or 0.4.40.
Original enrollment and reservation-inquiry authorization remain mandatory.
The protocol and complete-release verification suites passed 39 tests in
21.760 seconds, including original coordination-path pins and rejected reverse
or future issuer authority.

Install one complete immutable release through the coordinated installation
transaction. Preserve in-flight controllers and original callback attempts.
This patch does not change the scheduler's ten-second budget, automate receipt
consumption, wake idle clients or repair native control-socket availability.
Installation, automatic client hooks, full body reading and native callback
confirmation retain their separate evidence requirements.

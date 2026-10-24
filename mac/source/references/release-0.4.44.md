# 0.4.44: keep configuration planning outside the SQLite writer lock

A configuration check previously acquired `BEGIN IMMEDIATE` before parsing
profiles or discovering changes. Even a no-op reserved the SQLite writer and
could delay CC Switch. This release separates observation, planning and writing.

1. Read a bounded snapshot through a read-only connection; close its transaction
   and connection before processing JSON.
2. Normalize profiles and the common snippet and serialize changes with no
   database connection held. An unchanged plan never opens a writable connection.
3. For actual changes only, check the database identity and open one short write
   transaction. Every changed profile and common snippet retains original-value
   compare-and-swap. One conflict rolls back the entire plan, including earlier
   successful updates; concurrent user edits survive.
4. Clear SQLite progress callbacks, roll back uncommitted work and close the
   connection on success, conflict, exception or budget exhaustion.

The profile snapshot accepts at most 1,024 profiles, 8 MiB per JSON document and
32 MiB of combined JSON. The profile phase has a five-second cooperative budget;
the write phase has a one-second cooperative budget within that deadline.
SQLite progress callbacks interrupt long SQL work, with explicit checks at phase
and row boundaries. These are not hard process deadlines for stalled filesystem
I/O, Python parsing or scheduler starvation. SQLite waits at most 250 ms per
lock attempt and the guardian does not loop/retry in-process. A future scheduled
invocation reads a new snapshot rather than reusing a stale plan.

The single Background launchd job retains the installation lock, a 60-second
interval and a matching throttle. It watches only `.claude/settings.json`,
`.codex/hooks.json` and CURRENT. It no longer watches configuration directories
or the cc-switch DB/WAL/request log. Profile imports are discovered on the
interval. This narrows event triggers; it does not prove which historical event
caused the reported lock or establish that it was a self-trigger.

## Observable outcomes

The top-level CLI propagates these profile results with exit code 2:

| Status | Meaning |
| --- | --- |
| `LOCK_BUSY` | A SQLite reader/writer lock could not be obtained within the bounded wait. |
| `CONCURRENT_EDIT` | Original-value CAS failed or the database changed during planning. |
| `PROFILE_BUDGET_EXHAUSTED` | A phase deadline was reached; uncommitted work was rolled back. |
| `PROFILE_SNAPSHOT_LIMIT` | The row or combined snapshot byte limit was exceeded. |

Diagnostics record snapshot, planning, lock-wait, transaction and total durations,
whether a write transaction began and whether it committed. They do not include
profile values, credentials or SQL parameters. Live hook files are reconciled
independently and may be repaired when a profile result is busy; inspect both
axes. `HOOKS_DISABLED_EXTERNALLY` remains an explicit boundary and is not toggled.

## Validation and restoration

The 17 focused tests passed locally. Real independent SQLite connections cover
writer commits during repeated no-op normalization, later-profile and common
conflicts rolling back earlier updates, bounded contention, exception/budget
cleanup and database replacement. Existing foreign-hook, provider-selection and
credential-preservation checks remain passing. These are source tests.

Install a complete immutable release; preserve original task controllers,
unknown inputs, callbacks and reservations. A deliberately paused guardian stays
paused until its responsible coordinator arranges controlled restoration of the
corrected job. Record actual CC Switch startup/responsiveness, unchanged non-hook
profile fields, job registration and a natural invocation separately. Registration
alone does not prove a loaded client executed its hooks or received a message.

This change does not create a native Codex control socket, release a task,
clear a Claude Goal or start another executor. The original bridge continuation
and historical file-enrollment rules from 0.4.43 remain unchanged.

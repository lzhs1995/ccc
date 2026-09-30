# Live Hook identity when the wall clock moves backward

A Claude process can remain alive while the system clock moves backward. A
later Hook may then have a wall timestamp earlier than the kernel's recorded
process start time. The ordinary timestamp check treats this as unverified.

The watcher can retain a narrowly bound live identity receipt for that case.
Only a real socket event accepted by the existing authorization and lifecycle
checks can create the receipt. The inbox overwrites source and clock metadata
and records the event PID's microsecond kernel birth identity. When processing
the event, the watcher requires that same birth identity before and after a
fresh process-generation inspection. This prevents a queued event from being
bound to a PID reused before processing.

The receipt binds the runtime object, daemon instance, surface, workspace, PID,
microsecond birth, process generation, session and monotonic acceptance time.
An observed identity change, failed birth lookup or invalid monotonic ordering
permanently invalidates it. Returning from session A to B and back to A cannot
revive the receipt; a new qualifying live Hook is required.

The receipt is not serialized in state.json. Journal replay, registration
revalidation, deferred events and synthetic fallback cannot create one. A
daemon restart therefore requires new live evidence if the ordinary wall-time
check still fails. Read-only reports built solely from state files remain
conservative and report unverified; they cannot reconstruct private daemon
evidence. This change does not authorize replay or clear completion, pause,
deduplication or input-send guards.

This is a candidate fix. Existing installed sessions have not been certified
or repaired merely by passing its regression tests. Installation and native
startup/continuation performance acceptance remain separate gates.

# 0.4.38: reduce file-observer startup cost

The file scheduler checks formal reservations through `executor_reask_lifecycle`.
That observer previously imported `mac_harness` just to obtain the active-marker
directory, which also loaded terminal transport, interruption handling and helper
rendering. Both reader and harness now import the directory constant from
`cmux_coordination_paths.py`. Marker parsing, locking and boundary decisions keep
their existing behavior. The harness establishes its sibling import path first,
preserving loading by absolute file path from an isolated interpreter.

Authorization, STOP/revocation, ownership and workspace checks remain live.
Missing markers never release a reservation. Reply detection and exact
executor-owned consumption retain their existing entrypoints. The 10-second
sweep budget, fair cursor, enrollment cap and per-sweep release verification
remain unchanged. The file scheduler does not send input or wake an idle model.

New controllers pin the directory module. The reviewed immutable 0.4.37 release
retains its complete manifest, original 13 controller members and boolean
reservation-inquiry capability. Controller shape validation also recognizes the
explicit trusted historical member sets; complete release and member hashes must
still pass. Historical controllers cannot add the new pin, new controllers cannot
omit it, and historical issuers cannot enroll a future controller. Compatibility
never migrates an episode, changes pending evidence or executes old release code.

Focused tests cover seven fresh-interpreter boundary scenarios with writes,
process creation, socket connections and transport imports refused during the
observation. Two related runs passed 99 and 49 tests respectively; counts describe
each run, not a new full-suite total. The original reviewer found an import-order
regression; an isolated by-path load test reproduced it before the fix, and all
four fresh-interpreter tests passed after the fix. A synthetic empty-workspace benchmark used
three alternating baseline/candidate process pairs. Median import plus first
observation was 0.257/0.085 seconds, and full process wall time was 0.359/0.200
seconds, with identical decisions. Host scheduling and caches were uncontrolled.

These source results remove a measured startup cost; they do not establish the
cause of every previous sweep timeout. Observe naturally scheduled sweeps after
installation and correlate original request/reply/consume times. Automatic hooks,
complete body reading and original native callback receipts each require their
own evidence. Preserve every unresolved attempt under its original controller.

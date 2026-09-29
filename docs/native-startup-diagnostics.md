# Per-original-process startup logs

An internal diagnostic caller may use `start(..., _native_trace=True)` with a
new unguarded b/N batch. This persists `native_trace_policy=per-launch-log-v1`
before launch. It is not a TUI setting and does not make startup faster.
Existing jobs cannot acquire the policy through reuse; an incompatible request
is rejected while preserving the original job. Default argv remains unchanged.

The native argv adds one `log_dir` override under
`<job>/native-trace/<index>/<original launch UUID>`, outside the empty working
directory. All three diagnostic directories must be owned, private and real
directories. Before exec, the destination is empty. The permanent initial argv
claim records the requested path and all three directory identities. Both exec
authorization checks revalidate the policy, full argv, path, ownership,
permissions, directory identity and emptiness. Existing prompt, Hook, process
identity and authorization checks remain in force.

A requested path is not proof of an effective log writer. The diagnostic
fixture must independently bind a live native PID/birth/surface/workspace/argv
to its original claim, observe a writable open file descriptor and matching
inode, and retain its prefix hash. After native cleanup it must freeze the same
file, verify that the prefix is unchanged, and reject replaced directories,
replaced files or unstable reads. Every original job/index/launch needs nonempty
evidence; extra files from one process cannot substitute for another.

Per-process logs address the ambiguity of shared startup logs. They do not by
themselves establish a critical path or performance improvement. Keep TUI
session_start, StartupThreadStarted, UserTurn and original task timestamps
separate; asynchronous skill events need not occur in a fixed order. Real UI50,
sustained500, release and installation acceptance remain separate gates.

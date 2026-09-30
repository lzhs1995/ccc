# Native continuation performance acceptance

`tools/native_dispatch_acceptance.py` exercises real cmux terminals and original
Codex CLI sessions against a private loopback model provider. It creates and
cleans only its own workspaces. It does not contact an external model provider.

The current candidate is **not ready for release**. The inherited 93 run and
instrumented 96 run each completed 250 unique continuations across 50 original
sessions below one second, but the last first-task events arrived 16.688 and
16.780 seconds after the worker launch barrier. Neither measured the UI button
path. No current 500-session performance acceptance has passed.

## Result contract

- `functional_passed` requires all requested original sessions to finish, all
  failed turns to have one confirmed continuation, successful cleanup, unchanged
  runtime sources, and no changes to pre-existing native identities.
- `performance.startup_passed` requires every first task within one second of
  the recorded worker barrier. This excludes UI, workspace creation,
  authorization, and daemon initialization; it must not be described as button
  latency.
- `performance.continuation_passed` requires all expected per-session rounds,
  unique failed turns, unique next native turns, and unique original inputs.
  Forwarding, acknowledgment, and the next native task must each fall below one
  second. Missing, negative, nonfinite, duplicated, or unbound data fails.
- `passed` additionally requires both performance conditions and unchanged
  fixture and timing-evaluator hashes. It controls the process exit status.

The default fixture directly exercises the `private_check` (b) entrypoint.
Passing it does not certify the N or legacy B entrypoint, an external provider,
the UI button path, or a larger concurrency level. Report these separately.

## Evidence and debugging

The result retains wall and monotonic launch anchors, the first task per original
session, all continuation bindings, controller RPCs, source hashes, and cleanup
results. A failed run is immutable evidence; use a new output directory for the
next changed candidate or diagnostic question.

The proxy pins the private environment once for all surface creation paths.
Do not add a second Worker-level environment wrapper: long startup input can be
truncated before the interactive shell is ready. Unknown create or input
acknowledgments never authorize replay.

Native diagnostic logs require an explicit private `log_dir`; enabling scoped
`RUST_LOG` alone may produce no file. Diagnostic logging can affect timing, so
label instrumented runs. Skill warmup and shell snapshot span times overlap and
must not be summed as a claimed startup critical path.

# Native foreground integration

This branch contains an isolated publisher and an exact patch for upstream
Codex commit `6aaac694803004a63c6f393d153c96d22521a6b9`.
It does not replace the CCC runtime from this repository's older main tree.

The publisher records only a completed native foreground selection. A heartbeat
renews that committed selection's five-second monotonic lease. Selection changes,
reconnects, failed withdrawal, reset, replay-only clients and teardown revoke
or refuse positive evidence. The shared server's startup arguments are not
foreground identity.

GitHub Actions compiles and tests the actual TUI App on macOS arm64, then builds
an immutable binary with its source, patch and binary hashes. Applying the patch,
passing tests, producing a binary and installing or adopting it are separate
steps. The workflow performs no installation and sends no agent input.

The integration manifest is generated only after the App changes are frozen.

The upstream release tag changes 166 local package versions from `0.0.0` to
`0.164.0` without refreshing `Cargo.lock`. This integration includes that exact
version refresh. CI also runs upstream's `cargo update --workspace` procedure
and verifies the complete resulting lock hash for candidate and baseline before
running `--locked` tests. Registry and Git dependencies keep their original pins.

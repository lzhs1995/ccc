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
`0.164.0` without refreshing `Cargo.lock`, while its frozen snapshots expect
`0.0.0`. This integration retains the exact release version refresh. CI explicitly
sets the test version to `0.0.0` for candidate and baseline, verifies each complete
test lock, and runs all TUI library assertions with upstream's pinned nextest
0.9.103 runner. It does not update assertions or snapshots. After success, it
restores and verifies the exact release manifest and lock, tests all 13 native
foreground lifecycle cases at `0.164.0`, and performs the locked release build.
Registry and Git dependencies keep their original pins throughout.

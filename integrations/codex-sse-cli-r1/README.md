# Real R3 Codex CLI smoke

This is an additive GitHub Actions extension for the already reviewed R3 SSE
parser patch on official commit `6aaac694803004a63c6f393d153c96d22521a6b9`.
It builds `codex-cli --bin codex` using Rust 1.99.1 and the `dev-small` profile.
The unchanged dependency lock uses the same explicitly recorded test workspace
version 0.0.0 as the preceding source-validation job. No installed client is
replaced and this is not a production release.

The fake supplier accepts exactly one POST to `/v1/responses`, flushes a complete
`event: error` frame, and keeps the HTTP body open without EOF, `[DONE]`, a
Content-Length or a terminal chunk. Its fixed synthetic message is
`SYNTHETIC_WRITER_REJECTED_NO_REAL_ACCOUNT`. The provider uses no authentication,
disables WebSockets and has zero request/stream retries. The configured stream
idle timeout is 60 seconds; a real natural CLI exit must occur within 8 seconds
of the flushed error, separately from the 60-second startup deadline.

Acceptance requires actual CLI exit code 1, one `thread.started`, one
`turn.started`, exactly one final `turn.failed` carrying the original message,
no completed turn, assistant output or tool item, one supplier request, client
socket close before server shutdown, and clean owned-child/handler cleanup.
A harness timeout/kill, startup permission error, early server EOF, missing
JSON, wrong message or extra request fails acceptance. All such evidence remains.

The R3 CLI JSON schema exposes only `error.message`. It does **not** expose the
structured upstream error code. Typed code retention is established separately
by the existing R3 API fixtures; this smoke must not claim otherwise. The real
source chain is `CodexErr::to_error_event` → app-server `TurnError` → exec
`ThreadErrorEvent`, whose only field is `message`. No schema changes are added.

The CLI child runs under default-deny macOS Seatbelt with one fake loopback
destination. The policy follows the previously successful heartbeat smoke:
system/runtime file reads, exact executable, private writable files, specific
sysctls and the Codex managed-preferences IPC domain. It does not grant keychain,
user HOME data, unrelated network, process fork or tool subprocess access.
The child receives an allowlisted environment with temporary HOME/CODEX_HOME/
XDG paths. These are disposable runner paths; no user credentials, configuration,
sessions, real provider URLs or installation identity are copied into them.

The monitored Cargo build and smoke both require 30 GiB free space before start
and monitor the same floor. Only their own process groups can be terminated.
The workflow uploads explicit compiler logs, binary/source hashes and smoke
metadata/stdout/stderr. It uploads neither the large binary nor private runtime
directories. Source inputs are rechecked after compilation. No local compiler,
live model request, remote push or workflow dispatch occurred during preparation.

Publication adds this public directory at `integrations/codex-sse-cli-r1/` and
replaces only `.github/workflows/codex-sse-error-r2.yml` with the additive workflow.
The existing six R3 package files remain byte-identical. The branch remains
`ci/codex-sse-error-20261112`; push paths add only this smoke package.

`test_smoke.py` tests Python metadata verdicts, environment and policy shape with
synthetic fixtures. It is not real Codex execution evidence. The exact baseline
parser regression remains in the original source-validation gates; this initial
CLI smoke executes the candidate only and does not fabricate a CLI RED result.

R2 preserves the first real CI failure. That run emitted exactly one pre-turn
`item.completed` of type `error`: the fixed `gpt-test` metadata fallback
diagnostic. The verifier now permits only that exact item at index 1, between
`thread.started` and `turn.started`, once, with the original item id and message.
Other item types, text, locations, duplicates or extra fields remain rejected.
The supplier, CLI command, model, deadlines, build inputs and source are unchanged.
The original run is not relabeled PASS; a new real CI attempt is required.

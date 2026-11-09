This is a prepared, unexecuted fixture for the r4 native automatic-hook change. It is not an installed hook or a runtime success report.

Admission is fixed to GitHub repository lzhs1995/ccc, macOS, build run 42561656291 / attempt 1, head b977911d8596c311b70d43a0c8b9c920aa2f4fd1, request SHA256 5cd620c961a3ebc6f7655a474b601387b7295ace06d8e1444f2f65580e0edfea. The successful build RESULT, its authenticated artifact, effective source SHA and binary SHA are still required. Root must authenticate those original records and the bundle manifest before supplying the required CLI arguments. This bundle must not accept a synthetic success receipt. There is no download, build, publisher or retry script.

The runner uses the original r3 small PTY/provider design: one fresh private 0700 directory under RUNNER_TEMP, an isolated HOME and CODEX_HOME, the admitted binary with --no-daemon, and an HTTP Responses provider bound only to 127.0.0.1. It copies the original pinned codex-code-mode-host alongside the candidate. No cmux/Herdr host, user credential, inherited business state, vendor endpoint, production hook or controller is accessed. Only this owned TUI may be terminated during cleanup.

Private hook setup uses CODEX_HOME/hooks.json, features.hooks=true and exactly two synchronous command handlers, one PreToolUse and one PostToolUse, matcher Bash, timeout 10 seconds. Official exec_command exposes Bash even when invoked through functions.exec. Each declaration's exact normalized sorted compact JSON SHA is installed only in this private user config's hooks.state. No --bypass-hook-trust or active user trust edit is used. ConfiguredHandler::run_id gives pre-tool-use:0:<hooks.json> and post-tool-use:1:<hooks.json>; each actual native reply must match.

The provider issues one root exec, then one actual spawn_agent fork_turns=none, then one child exec. Both execs automatically reach original native Hooks::new, command_runner and its Core observer. Only native hooks.json invokes hook_probe.py --event. Each handler reads actual stdin, records it, overrides CODEX_THREAD_ID with the shared root and execs the same Python process. Kernel PID/full birth must stay unchanged, while the registry's thread remains the actual root or child. Four actual hook invocations are required, with event/turn/tool ID/handler and native query nonce/epoch/peer/owner checks. Extra or duplicate events fail instead of overwriting evidence or retrying.

PostToolUse additionally exercises the exact pure validate_reply function extracted from the frozen r4 inbox candidate. Root native proof plus actual root header must pass that pure function. Child native proof must fail even with shared root session_id, root transcript facts and removed child payload tags. Each model-issued exec also queries the original socket and supplies an explicitly fabricated Post payload to that pure function; its unified-exec origin must be rejected. This is a negative control, not manual execution of an automatic hook or the installed inbox helper.

Acceptance requires the subsequent real provider request for each thread to contain both actual hook additionalContext markers, the real native root/child metadata relation, and the original provider tool call/output pairs. Code-mode outer call IDs remain distinct from the inner native trigger ID; Pre, actual exec and Post are checked against their common inner ID. The result preserves raw stdin, kernel/socket evidence, native rollout copies, provider requests, private config/trust pins and cleanup. An incomplete run stays failed; it is not replayed.

Scope limits: reader_acceptance.py is a byte-exact pure function extraction only. NativeHookCaller._client/_binary/_writer, cmux identify/tree and TTY host binding, the installed metadata-path resolver, root policy selection, inbox bounded discovery, successful-output cursor commit and mailbox body consumption are not exercised. No fake host response or monkeypatched dependency fills those gaps. Even a successful isolated result keeps cmux_host_verified, live_root_hook_verified and complete_inbox_or_bidirectional_receipt_verified false.

Required command parameters for later authenticated runner use:
--root <fresh private runner directory>
--binary <actual admitted r4 binary>
--host <original pinned code-mode-host>
--build-result <actual successful build RESULT>
--build-run 42561656291
--build-result-sha256 <authenticated RESULT SHA256>
--reviewed-source-sha256 <actual reviewed effective-source SHA256>

Do not run this locally. Current preparation performs syntax/source checks only; it repeats no original 13 Python, 17 Rust or 180/300 tests. Real hook execution and trust loading remain pending.

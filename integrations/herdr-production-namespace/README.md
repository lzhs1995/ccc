# Herdr v3 recovery candidate with production namespace

This candidate preserves an agent's recovery intent after passive process exit, allowing the same saved session to resume. An explicit release revokes that intent, so a later restart does not resurrect the released agent. It uses the reviewed v3 patch against Herdr 0.9.5 commit `2ecf19a92fc3a7fff75b6ce3655ef04f4625ec01`.

The isolated macOS arm64 workflow validates the official source archive, complete source inventories, Cargo lock and Zig 0.16.0 archive. It compiles the server and unit target with `-C debug-assertions=off` only on the final Herdr crate, using dev/test dependency profiles. The resulting namespace is `herdr`; this is a nonoptimized development candidate, not an official release build. Rust 1.99.0 matches the earlier local validation and differs from upstream's declared 1.96.1.

The actual test binary must list exactly 3656 nonignored cases and 11 upstream ignored cases. Fixed nextest 0.9.146 reuses that exact binary and executes the complete nonignored suite with zero retries. The workflow preserves raw compiler metadata, command lines, JUnit, source hashes and a candidate archive. It does not upload build caches or install or restart clients.

Freeze SHA256: `c977160d40cf0f2ee829cbdd710bfaa8a8eb86c0273ff088610133c69bbd0527`.
Patch SHA256: `3a18fdb4c86a77acfe2471bb6610db3f283281e9d4e75c890f8d3a105f661966`.
Complete candidate source SHA256: `754fee71803818259fc27038fca672ed14979c633199bd971c3a6e9b3855857e` (2959 files, from 2957 upstream files).

After CI, the downloaded artifact still needs exact run and digest validation and a private real TUI recovery/release test. Simulated-agent tests do not establish actual AGY account authentication or physical power-loss recovery. Production adoption and restart remain separate steps.


Run 42001597263 compiled the server and unit binary and passed the exact 3656 nonignored tests (11 ignored), but failed its final full-source identity gate. The original run did not retain the after-source inventory, so the changed paths are unknown. The next run records and compares complete source manifests and differences after fetch, server compilation, unit compilation, test listing and tests; it still rejects every changed or added file. Nextest storage is now explicitly outside the source tree. A real 0.9.146 reuse experiment found that the default store created only empty directories and did not change the file manifest, so store relocation is isolation hardening, not a proven root-cause fix.


Run 42006537214 retained exact phase evidence: after-fetch matched the frozen tree; after-server added 1296 files, all below `vendor/libghostty-vt/zig-pkg`, with no removed or modified original files. Official Zig 0.16.0 source places unpacked packages under its build root regardless of `ZIG_GLOBAL_CACHE_DIR` / `ZIG_LOCAL_CACHE_DIR`. `--system` changes dependency fetching and build semantics, so this workflow does not enable it.

The CI now copies only the verified 1339 original vendor inputs into an external output directory and checks every byte and executable bit. A pinned wrapper accepts only the original vendor cwd and exact original build arguments/OUT_DIR prefix; it changes cwd to that verified copy and directly execs the same official Zig with unchanged argv. `version` is forwarded without changing cwd. Any unexpected command, flags, binary, original input or copied input aborts. The original Herdr patch and complete source inventories are unchanged.

Evidence includes the wrapper source/hash, real Zig hash, fsynced invocation intents, exact copied input manifest, full mirror manifests and separate generated package inventories after server/unit/tests. The official Zig package hash checks remain enabled. All five original source equality gates remain strict; generated dependency files are allowed only in the external mirror package directory. Offline wrapper tests and tiny real `zig fetch` / wrapper `version` checks cover this isolation mechanism without compiling Herdr. A new successful full CI and private TUI recovery test are still required.

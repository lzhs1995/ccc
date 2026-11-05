# Native compaction recovery build candidate

This candidate adds bounded native control for stalled compaction across Core, API, TUI and Goal, while preserving foreground identity and existing protocol behavior. This branch builds and verifies a candidate; it does not install a client or restart running tasks.

The build pins official Codex commit `6aaac694803004a63c6f393d153c96d22521a6b9`, V187 foreground base commit `a0f7fa7216673009375405ca99ca5c0a4b8a997f`, 73 overlay files and a complete 9461-member source inventory. The overlay includes the real generator output from [run 41988236011](https://github.com/lzhs1995/ccc/actions/runs/41988236011), whose compatibility comparison preserved all existing API semantics.

Freeze SHA256: `1d2da60a12be38f2560629e0a7b490e3d0ee7cf79302046acc9844bc133e6b29`.
Release source SHA256: `497fed49f63a2bc576f91224671c226f976de2184efed78ae4c3b2dbf9a60ab3`.
Test source SHA256: `6fd9c0ad6d3450cc562054bc2b00a23219d85ef08c01ab9ba7cf3e61c6d34cbd`.

The macOS arm64 job reconstructs and checks the complete source, verifies formatting and dependency locks, compiles affected test targets, runs the affected library suites and all 96 named required cases with zero retries, then builds the release CLI. Its actual JSON schema export must match all 460 frozen files. Default upstream ignored tests remain ignored; every named required case must execute and pass. Historical test successes do not satisfy these gates.

A successful CI run still does not establish actual 180/300-second recovery, production adoption or power-loss recovery. Those remain separate live acceptance steps. The generated binary is an unsigned development candidate, not an official signed release.

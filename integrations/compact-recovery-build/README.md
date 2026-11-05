# Native compaction recovery build candidate

This candidate adds bounded native control for stalled compaction across Core, API, TUI and Goal. It also keeps the committed foreground identity stable during animation-only Draw events, so an active animation does not continually invalidate the continuation guard's identity reads. Input, pending paste, modal, editor, replay, thread change and disconnect paths retain the original withdrawal checks.

The isolated build pins official Codex commit `6aaac694803004a63c6f393d153c96d22521a6b9`, V187 foreground base `a0f7fa7216673009375405ca99ca5c0a4b8a997f`, 74 overlay files and the complete 9461-member source inventory. It includes the compatible generated schemas from [run 41988236011](https://github.com/lzhs1995/ccc/actions/runs/41988236011); protocol, schema and dependency locks are unchanged by the Draw revision.

Freeze SHA256: `a0df2d3ef222cac4e1062864a6a2c7daaea47a3e95af8bad763f59c2c601570b`.
Release source SHA256: `c83499a8c9d742e3b203546b9249160037faa38a52b7fce0e54adb8230d52c17`.
Test source SHA256: `df0af272252da9952b5a5db35eeb4be700b43e38e31d4a7ed939abdd391efa7d`.

The macOS arm64 job reconstructs and checks the full source, formatting and dependency locks. It compiles affected test targets, runs affected library suites and all 102 required cases with zero retries, then builds the release CLI and verifies its 460 actual JSON exports. Six new App cases cover repeated Draw with a real shared lock and unsafe Draw/non-Draw transitions. A prior full build exposed a test macro import ambiguity; the explicit import is fixed here. Default upstream ignored tests remain ignored, while every required case must execute and pass.

CI success does not establish the real default-animation continuation flow, actual 180/300-second recovery, production adoption or power-loss recovery. Those are separate acceptance steps. This branch does not install or restart clients. Its binary is an unsigned development candidate, not an official signed release.

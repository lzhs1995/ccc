# Native compaction recovery schema preparation

This branch stages an isolated schema generation job for reviewed Core, API, TUI, Goal, and native foreground recovery changes. It does not install or restart clients. The job uses a pinned official Codex source and a separate pinned V187 foreground base.

The bootstrap manifest fixes 62 overlay members and the complete 9457-member source. It generates schema candidates only; complete compilation, test suites, real 180/300-second recovery, and deployment remain separate gates. The local workspace/native formatting checks used Rust 1.95 rustfmt; the workflow uses the original Rust 1.99.1 toolchain.

Source SHA256: 35c84c4a875e7388c2a66823ce03e07442a7ae2f6c113787beb967bfc7c63310. Bootstrap manifest SHA256: 5d112be225421a694f2fe9defc28ff2d989b45a75180ddbe95e3c37b7e9b04c4.

Only pushes affecting the exact candidate branch and paths trigger the schema job. Manual dispatch is available when registered. No old test success is imported as evidence for the candidate.

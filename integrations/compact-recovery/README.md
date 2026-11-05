# Native compaction recovery schema preparation

This branch stages an isolated schema generation job for reviewed Core, API, TUI, Goal, and native foreground recovery changes. It does not install or restart clients. The job uses a pinned official Codex source and a separate pinned V187 foreground base.

The bootstrap manifest fixes 62 overlay members and the complete 9457-member source. It generates schema candidates only; complete compilation, test suites, real 180/300-second recovery, and deployment remain separate gates. The local workspace/native formatting checks used Rust 1.95 rustfmt; the workflow uses the original Rust 1.99.1 toolchain.

Source SHA256: 7013d28d993ccdc5119873acde2a2c3c38e818647cac0e17f849e10ae073a4c5. Bootstrap manifest SHA256: c6bb394e19fb29435b50276fce7a38fbaf711bc54dbfe89b4a8c621d0d143582.

Only pushes affecting the exact candidate branch and paths trigger the schema job. Manual dispatch is available when registered. No old test success is imported as evidence for the candidate.

The compatibility revision preserves the original empty compact response's treatment of unknown fields and adds a regression test. The earlier schema generation evidence remains separate; this source requires a new schema generation and complete build.

The second schema-generation source restores the existing compact-start response compatibility and adds a regression test; generated protocol files are not manually edited.

# Native compaction recovery schema preparation

This branch stages the reviewed Core, API, TUI, Goal and native foreground recovery changes against pinned official Codex source and the separate V187 foreground base. It does not install or restart clients.

The bootstrap manifest fixes 62 overlay members and the complete 9457-member source. Source SHA256: `97907f664f19cded25dcb9360eb007c18a351d2bfb50eb5e6faa90faec9342b8`. Bootstrap manifest SHA256: `fe9b9b031cc23364a1035608c8bae5fb3f8f323390005fffccacea995d2ea668`.

[Schema generation run 41988236011](https://github.com/lzhs1995/ccc/actions/runs/41988236011) passed on commit `aacdb9badd1606b5c98d1630a3e4d0fd27ddd7bd`. Its artifact SHA256 is `75dbc4fc0ed1cf94ac811099a4587592aa2f5ccb023f0669b03eddcea82d9ec6`. Independent structural comparison verified four added and seven changed schema files, with all existing API semantics preserved. The original compact-start response still accepts unknown fields; the new recovery request rejects them.

Full compilation, fresh tests, actual 180/300-second recovery and deployment are separate gates. The full build bundle is staged separately under `integrations/compact-recovery-build`. Historical test results are not reused as candidate evidence.

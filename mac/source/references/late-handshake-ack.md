# Reconcile a late handshake ACK

An expired observation window does not invalidate a genuine later ACK. Do not
rerun preflight or handshake: those operations can create a different nonce.
Keep the original session, task, provider, receipt, dispatch attempt and payload.

`scripts/reconcile_handshake_ack.py` is an explicit coordinator. It verifies the
complete immutable original release and imports that release's native reader,
identity gates, receipt writer and validator. It does not modify or migrate the
original controller. Compatibility is tested against the complete 0.4.32 release;
an unsupported original release is refused rather than silently upgraded.

Version 0.4.33 also supports the measured 0.4.32 terminal row
`REJECTED / DRAFT_NOT_READY / STRANDED_IN_COMPOSE`. The original attempt must
contain one `PASTE_INTENT`, one `PASTED` and only subsequent `DRAFT_OBSERVATION`
events, with the matching draft error and no submitted timestamp. It still needs
the exact later assistant ACK in the bound original session after the original
fence. A screen ACK alone cannot qualify. Other rejection states remain refused.
The original supervisor runs the new coordinator with `--controller` pointing
to the complete original release; it must not edit that release or rewrite the
failed receipt to `EXPIRED` to use this route.

Version 0.4.34 accepts the exact ACK as a native assistant string or as the sole
text block accompanied by structurally valid `thinking` / `redacted_thinking`
blocks. Reasoning contents remain opaque and are never copied into proof or
archive output. The text block must equal the complete expected ACK byte for
byte; duplicate/extra text blocks, tool blocks, unexpected fields and malformed
reasoning blocks fail. Sidechain/meta flags must be absent or boolean false.
The original session, role, record UUID, time and post-intent fence checks still
apply. This recognizes a real ACK; it does not infer notice reception, repair
the composer, send input or bypass the original supervisor's apply authority.

Pass `--controller`, `--receipt`, `--receipt-sha256`, `--attempt`,
`--attempt-sha256` and `--text-file`. The last file contains the exact original
wire bytes, not the long referenced body. A reconstructed reference notice is
usable only when its complete SHA matches the original receipt and attempt.
Default preview is bounded and read-only. Both live observation and apply must
pass the original controller's caller rules; a maintenance coordinator is not a
substitute for the original supervisor.

Version 0.4.36 also authenticates an embedded original Codex TUI when that
controller's `caller_snapshot()` returns no managed-daemon proof. The coordinator
uses the original controller's kernel process/ancestry, foreground and cmux
readers. It requires the actual tool thread, original supervisor UUID and
workspace, native foreground thread, matching TTY and stable repeated reads;
another supervisor or an intervening different thread is refused. Environment
selectors alone, resume arguments and a maintenance coordinator do not grant
ownership. The original workspace gate is still checked before writes, and the
authenticated proof is recorded in the result and archive intent. This support
is confined to ACK reconciliation; it does not change general hook identity,
recover an exited executor process or supply a native input control socket.

Apply requires preview `READY_ZERO_INPUT_ACK_RECONCILIATION`, the same pinned
inputs and `--apply --original-harness-exited`. The original supervisor must
verify that its terminal harness has actually exited. The new directory lock
cannot coordinate an old harness that never used it. The apply path checks the
original supervisor's authenticated foreground thread, original delivery lock,
receipt directory, full executor panel and original ACK record after its fence.
It archives the original receipt and supporting evidence before calling the
original writer and validator. A matching active marker can receive the original
writer's heartbeat side effect; the result records that separately.

Retain a partial archive or interrupted write. Do not delete it to retry. A
repeat call can report an already reconciled result only when the original pins,
archive and current result all agree. Changed pins, incomplete evidence or
uncertain concurrent writes require inspection, not a replacement attempt.

Queued command attachments, body reading, an exact assistant ACK and a complete
native user receipt are different observations. Original 0.4.31 handshake
semantics permit a valid bound ACK while retaining an unconfirmed dispatch. This
entrypoint preserves that distinction and does not upgrade the notice receipt.
An observed attachment is not proof that the client will never emit a native
user record. Validation PASS does not dispatch or accept business work; the
original supervisor must finalize the original pack and use its dedicated
`TASK_PACK_V2` route under the same controller.

The behavior cases cover wrong sessions/nonces/roles/fences, missing native
ACKs, preview without writes, original owner identity, multi-executor aggregation,
locks, symlinks, concurrent changes, partial archives, writer failures, repeated
calls and marker heartbeat effects. Their real file/journal/parser/writer behavior
is also run with the complete original controller; external process and terminal
observations remain fixtures, not claims of live receiver adoption.

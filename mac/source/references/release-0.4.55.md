# 0.4.55: queued handshakes and partial dual-panel continuation

UUID selectors are resolved through the authenticated workspace proof before
forming a gate. The current surface ref and UUID must agree; two selectors for
one surface cannot count as two executors. Per-selector provider assignments
and expected UUIDs remain attached to the resolved participant.

A direct send returning NATIVE_QUEUED or an uncertain native observation is not
SUPERVISOR_DID_NOT_SUBMIT. The handshake waits for its original nonce within its
original ACK budget, without replaying input. Queue evidence remains separate
from a fresh native user message containing the marker and from business acceptance.

`reconcile_handshake_ack.py` accepts a valid ordered prefix of the original
panel and resolves each historical ref through the original gate. Its narrow
0.4.54 compatibility case requires the original MESSAGE_NOT_CONFIRMED result,
marker binding, recorded paste and Enter, and an exact original native assistant
ACK. It archives the old receipt and preserves the original attempt. A verified
first ACK in a two-executor gate still publishes AWAITING_EXECUTOR_ACK and fails
whole-panel validation until the second executor is proven.

After that reconciliation, `scripts/continue_handshake.py` previews the next
never-started executor in a two-executor panel. Required arguments are the
complete immutable `--controller`, absolute `--artifact-root`, exact current
`--identity-sha256`, `--receipt-sha256` and original `--reservation-id`.
Only the authenticated original supervisor may add `--apply
--original-harness-exited`. The original reservation must remain valid and every
started nonce must have its bound original native ACK. No task pack may exist.

A durable ordinal-specific intent precedes one invocation of the original
controller's handshake function for the second executor. The first nonce,
receipt row, attempts and reservation terms are retained. The same slot never
replays after a failure or interruption. If the old controller again labels a
queued send as rejected, reconcile that new original nonce; do not rerun the
continuation or replace its controller. The helper sends no formal task pack.

Fresh `handshake` refuses an existing receipt. Existing work uses its explicit
original reconciliation/continuation path. Current validation independently
checks coverage of all gate members, including historical incomplete aggregates.

These fixes do not restart Claude, pause Goal, transfer write ownership, alter
old input budgets, or convert queued_command into native user reception.

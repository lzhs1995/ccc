# Long prompts and exact delivery

A folded or partially visible composer does not prove that the whole prompt is
ready. Keep `_stable_owned_draft` and its consecutive complete-draft equality
checks. Do not infer ownership from character counts, folded placeholders, a
message prefix or an empty composer.

For a **new** ordinary message, the shared helper uses
`cmux_prompt_reference.py` before its first paste. The harness uses the same
module for handshake and review challenges. Messages within 700 UTF-8 bytes and
without any CR, LF or tab stay inline. Larger messages and messages containing
any CR, LF or tab, up to 1 MiB, are persisted as exact SHA-addressed UTF-8
files under `~/.local/state/multi-agent-collaboration/message-bodies-v1` and sent
as a single-line MESSAGE_REFERENCE_V2 notice carrying the original marker, absolute path, SHA256 and byte
count. This is a conservative wire budget, not a universal terminal limit.
The full stable-draft check still applies to the notice. A short message's final
newline can be hidden by terminal line framing; preserve it in the body rather
than stripping it or relaxing exact equality. Shell command substitution also
removes trailing newlines and must not be used to preserve the original payload.

The body is exclusively created, flushed, made read-only and verified before
input. Preserve Unicode, tabs, CRLF, blank lines and trailing spaces exactly.
The original attempt pins the body's device/inode, size, timestamps and SHA.
The sender rechecks those pins before input and during reconciliation. The
existing PostToolUse native-delivery guard checks the same original pins before
and after reading native receiver evidence. A missing, writable, replaced or
changed body refuses further input or a success claim; never reconstruct an
original pin after the fact. A new raw `submit-text` request over the budget or
containing any CR, LF or tab is refused before paste and must be prepared through this shared
ordinary route. An already-persisted attempt retains its original bytes and can
still be reconciled read-only, including a legacy terminal newline.

Only a complete exact native user record for the **wire notice** after the
original `PASTE_INTENT` fence proves its reception. Such evidence is explicitly
`confirmation_scope=reference_notice`, `body_read_confirmed=false`. The receiver
must independently verify and read the file before following its reply contract
within the already-authorized task. Body access, task acceptance, execution and
completion remain separate evidence. Queue banners, Enter/Tab and ACKs do not
establish native reception.

Formal task packs and callbacks retain `submit_task_pack` and
`submit_completion_callback`; never hide either inside an ordinary body to
bypass their binding checks. A new short notice cannot repair an old long
attempt. Saved helper requests and native journals retain their exact payload,
marker, nonce, receiver binding, original fence and controller. Reconcile those
read-only, or use only the original controller's already-authorized recovery;
do not repaste, rewrite a draft or reset the shared recovery-key budget.

Install the whole compatible release so helper, adapter, body module, journal,
harness and hook agree. Installation and offline tests do not prove a running
client loaded the hook or a live Claude-to-supervisor message was received.
Record those observations separately. Resume the original business task once
its actual delivery boundary is satisfied; no extra maintenance review rounds
are implied by this change.

## Task and callback wire formats

New formal task packs use `cmux_bridge.task_pack_notice(path)` and
`submit_task_pack`; TASK_PACK_V2 is a single line that pins the whole frozen
pack SHA. Validation still resolves required skill, identity, authorization and
completion contract from that pack. Ordinary references cannot carry task packs
or callbacks. Completion keeps the exact dedicated line and uses
`submit_completion_callback`. Every new paste, including callbacks, passes the
same 700-byte/no-CR-LF-tab guard. A too-long callback is a zero-input contract
error, not permission to truncate it.

Historical MESSAGE_REFERENCE_V1 notices retain their original four lines and
remain readable by the original controller. Do not repaste an old notice through
V2. Single-line transport prevents known multiline folding and tab expansion;
it never replaces stable full composer equality or new complete native evidence.

# 0.4.52: Claude Code upgrade identity and handshake settlement

Claude Code may run from `~/.local/share/claude/versions/2.1.303` after an
upgrade. Earlier process discovery discarded the numeric executable name, so
an existing terminal could fail identity capture before handshake or re-ask
registration. Discovery now includes version labels and authenticates the
canonical kernel executable, exact argv target, ownership, mode and file identity.
Workspace, TTY, process birth, uniqueness and live-session checks remain.
A numeric name alone grants no identity.

Handshake contract errors now settle as rejected with supervisor attribution.
Only the original sender evidence is recorded. Missing evidence does not mean
zero input; an error after paste preserves that intent. No ACK polling, retry,
submission key or resend follows the error.

## Upgrade checks for every active pair

1. Record the actual executable, PID, birth and original native session for each
   supervisor and executor. An updated launcher does not upgrade every live process.
2. Inspect global and explicit startup settings for disabled hooks, missing files
   and source-version mismatch. Registration in a settings file is not execution.
3. Observe automatic PreToolUse/PostToolUse/Stop records under the current parent
   process. Preserve unverified scope labels; old-PID records do not prove adoption.
4. Along authorized work, confirm native handshake/dispatch, full body reading and
   callback or exact mailbox consumption separately. Normal work and Goal are
   compatible with prompt-ready communication.

If a same-session restart leaves an episode bound to an exited PID, preserve
the old state and unconsumed reply. After read-only identity preflight with the
new release, the original executor may explicitly stop the old episode and
start a new one using the supported `--resume-stopped` transaction and original
authorization. Never rewrite the old PID or mark its reply consumed. Existing
formal reservations and pending input remain; continuing reservation inquiries
still need explicit authorization. Do not stop an otherwise working episode
merely to change versions.

## Compatibility and limits

The complete immutable 0.4.51 release was statically verified against commit
`744899c0140f0f88c4b42a5077a90bf25145fbd5`. Its manifest SHA256 is
`eaa374d774766d606753a748c40dd3460dd518be09c9a2fc7ce80928014445a3`.
Its 14 controller members, reservation-inquiry capability and original
18-controller issuer range are pinned together. Historical issuers gain no
reverse or future authority; no historical code is executed for compatibility.

Publication does not prove all live sessions adopted the release. This release
does not repair an absent Codex control endpoint or authorize an initial key on
an already stranded draft. Those original attempts retain their own evidence
and recovery requirements.

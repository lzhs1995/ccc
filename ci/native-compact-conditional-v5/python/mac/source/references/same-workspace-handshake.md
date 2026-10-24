> Current policy (0.4.19): [non-blocking recovery](nonblocking-recovery.md) overrides historical session-wide tool/Stop blocks and old-supervisor handshake restrictions below. Installed workflow hooks are advisory; explicit transport evidence and draft protection remain.

# Same-workspace handshake guard

## Same-workspace handshake: mandatory transport boundary

Before discovery, naming, bridge-test, handshake, task dispatch, callback, paste
or key submission, resolve the **live caller** from `cmux identify --json` and
join it to `cmux tree --all --json --id-format both`. Compare nonempty workspace
UUIDs, not labels, titles, focused panes, remembered refs or inherited summaries.
Both participants must be terminal surfaces in different panes of that workspace.

A user-designated executor is an additional exact UUID constraint. Supply
`--expected-workspace-uuid` and repeatable `--expected-executor-uuid` to the harness.
If the designation conflicts with the live caller, refuse input immediately;
do not select an old peer, alter environment identity, move a surface, launch a
replacement, or claim a successful handshake. Existing sessions remain intact.

`scripts/cmux_workspace_guard.py` provides the shared fail-closed check.
`cmux_agent_panel_guard.py` invokes it before help/recovery exceptions. Raw
`cmux send/send-key`, `cmux-agent ask/broadcast` and terminal-write RPC paths are
blocked by the hook; use `cmux_bridge`, which rechecks every paste/key and addresses
both workspace and surface by UUID. The harness rechecks saved gate UUIDs before
later phases. Missing identity, ambiguous refs, stale UUIDs, target movement,
unknown cmux state and a changed designated peer deny before input. No force,
message-prefix, timeout, recovery-authority or environment bypass exists.

An optional caller-scoped file at
`~/.local/state/multi-agent-collaboration/workspace-scope/<CALLER_UUID>.json`
preserves an explicit user designation across invocations. Schema:
`{"version":1,"caller_surface_uuid":"UUID","workspace_uuid":"UUID","target_surface_uuids":["UUID"],"authorization_source":"user instruction"}`.
Only a new explicit user designation may replace that intent; do not clear it
because a target is unavailable. The scope narrows access and cannot permit
cross-workspace input. Resource coordination across workspaces uses existing
file/queue receipts, not executor-handshake transport or a STATUS-prefix exemption.

Install via `scripts/manage_install.py`; it registers the guard for both clients.
Run the installed hook with a cross-workspace/raw-input negative case and verify
exit 2 and zero input before claiming enforcement. Client settings registration,
active hook execution and imported Python modules are separate states; a process
that already imported an older transport has not magically hot-reloaded.
There is no atomic cmux snapshot-plus-send API: UUID addressing closes ref reuse,
and a fresh check precedes each input, but this is a supported-transport guard,
not an OS sandbox against arbitrary self-written socket clients.


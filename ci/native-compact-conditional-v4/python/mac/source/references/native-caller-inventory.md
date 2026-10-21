> Current policy (0.4.19): [non-blocking recovery](nonblocking-recovery.md) overrides historical session-wide tool/Stop blocks and old-supervisor handshake restrictions below. Installed workflow hooks are advisory; explicit transport evidence and draft protection remain.

# Native caller inventory failures

## Recovery without a session lock

Identity discovery is a transport prerequisite, never a global permission to
work. A task hook that cannot authenticate its caller must select no task and
return success without repeated blocking output. This applies to enrolled and
unenrolled sessions alike: shell/file tools, `cmux-agent self`, diagnostics,
goal bookkeeping, authorized solo work and honest Stop remain available.
Do not clear task markers, thaw or edit frozen evidence, fabricate an identity,
confirm callbacks, or silently accept work while bypassing an unresolved scope.
After discovery recovers, the next evaluation rechecks the live task normally;
there is no cached bypass or permanent disarm.

All active Codex supervisors may initiate a fresh handshake within user scope.
A successor need not use the predecessor's startup command or session id.
Use native foreground-thread selection and current kernel/workspace evidence;
legacy argv recognition remains compatibility only, not a required launch mode.
Business-task inheritance never transfers the previous process identity.
Terminal input still requires verified live sender and receiver UUIDs. A missing
identity may stop that input, but must not stop local recovery or the task.

Regression coverage: enrolled supervisor and executor resolution failure,
timeouts, malformed proof, ordinary diagnostics/edits/bookkeeping and Stop;
unchanged task evidence, restored enforcement after recovery, context cleanup,
and actual bridge rejection of missing/ambiguous or cross-workspace identities.

A zero-input identity refusal occurs before executor delivery. Do not call it a
Claude API failure, unavailable executor, or missed ACK. Preserve the first
failure and continue authorized independent work while repairing discovery.

## Darwin process inventory

A live Codex process can be absent from `pgrep -x codex` while kernel executable
and argv inspection still identify the expected original resumed session. A
negative name query is not proof of process absence. Enumerate same-user `ps`
PID/executable rows, then independently validate each candidate with the existing
kernel executable, UID, birth, argv, terminal and cmux UUID checks. Discovery is
not authentication. Duplicate matching clients, changed process identity and
cross-workspace peers still deny input.

An unrelated discovery row may disappear between `ps` and the kernel read.
Only an initial ESRCH or a verified owned zombie is safely excluded; an unknown
read, denied permission, or exit during a partial read still refuses. Read a
nonmatching candidate twice with unchanged kernel identity and argv before
excluding it. Relative argv paths on these stable nonmatches do not authenticate
a caller. Matching session candidates retain strict executable-path validation
and the final identity recheck; a second match remains ambiguous.

Global dock surfaces can appear nested under a workspace in the cmux tree.
Their `dock_scope=global` flag excludes them from local peer discovery and from
both caller and target bindings. Duplicate UUID rows still refuse; filtering
must not erase an ambiguous local/global identity.

Do not infer the caller from focus, titles or remembered surface numbers.
New or switched Codex threads use the native TUI foreground selection, together
with kernel PID/birth/executable/UID/argv, managed tool ancestry, terminal and
live cmux UUID checks. Authenticate its opt-in marker, private canonical files,
schema, process lifetime and unchanged source before and after discovery.
A null selection never revives the startup argv. Missing required or previously
observed records, malformed records, drift and duplicate clients refuse input.
Only clients without native observation support retain legacy resume matching.
Keep inherited business-session identity separate from the current caller;
a discovery refusal is local and does not establish executor unavailability.

## Bounded diagnosis and acceptance

1. Resolve the caller before spending an ACK timeout. A deterministic local
   refusal triggers one evidence capture and repair, not repeated handshakes.
2. Keep existing executor context. Allocate independent work only to an actually
   available bound peer; do not queue reviews behind existing tasks.
3. External helper parity failure concerns that helper, not the executor. Use
   the shipped guarded bridge for both directions and preserve all identity gates.
4. Verify identity, then a nonce-bound ACK. Check after Enter: compose, queued,
   consumed and accepted are distinct states. Unknown delivery is not resent.
5. Report source tests, installed runtime, live ACK and callback separately.
   Passing a read-only guard does not establish bidirectional delivery.

Regression mapping: `test_cmux_daemon_identity.py` covers executable-path
inventory including spaces, failed/malformed/duplicate inventories, exact
basename selection, missing/duplicate sessions and identity drift;
`test_cmux_workspace_guard.py` preserves workspace and UUID boundaries.
`test_caller_inventory_boundaries.py` covers typed exit evidence, stable excluded
candidates, changed/unknown/duplicate caller candidates, and nested global docks.

## Interrupted Claude with a residual Bash display

The current interruption line and an empty composer can coexist with an old
Bash status display. Neither the display alone nor the absence of one child
process establishes a safe boundary. `cmux_claude_interruption.py` permits only
the harness's non-submitting `B<ordinal>_<8hex>` probe when all of these agree:

- The designated native Claude session, process, terminal and workspace binding.
- A complete bordered empty composer, the current interruption line, one Bash
  status display and no queued input. The exact optional goal-duration suffix
  on the standalone clear hint is accepted outside the composer only.
- The final three native records: Bash tool use, the matching rejected result,
  and user interruption, with matching session, record parents, tool id,
  assistant id, literal error flag and ordered timestamps.
- A bounded unchanged transcript tail and unchanged process identities; every
  remaining descendant or process-group peer must predate the rejected tool.

The harness rechecks these facts before typing and before every cleanup key.
Cleanup removes only the still-visible prefix of its own short probe. Native
appends, changed identities, foreign input or a new queue permanently invalidate
that attempt; it never clears user text, presses Enter or resumes the rejected
tool. Continue the original task after a valid probe. Do not recreate its id,
repeat an already submitted challenge or migrate an in-flight controller.

`test_claude_interrupted_probe.py` covers native-chain corruption, file/process
drift, queued or foreign drafts and cleanup races. Formal messages and callbacks
retain their existing one-attempt and exact native-reception rules. Passing this
probe is not an ACK, successful delivery or business acceptance.

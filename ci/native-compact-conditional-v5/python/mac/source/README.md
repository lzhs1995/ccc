# Current release: 0.4.54

Send ready messages directly in both directions, including during work, Goal and
compaction. Raw cmux send/send-key/paste is allowed. The optional bridge uses
native Codex input when available and falls back to a single terminal paste and
Enter only before any native input intent. Confirm a new user message containing
the unique marker in the original receiver transcript. Never repaste an uncertain
message; at most one recorded extra Enter may recover residual input after compaction ends.
See [direct delivery](references/verified-compose-delivery.md).

## Earlier release notes (historical behavior, not new-message admission rules)

Current source: **0.4.54**. Communicate between distinct existing terminal tabs
in one pane, preserving authenticated identities and serialized input. See the
[0.4.54 contract](references/release-0.4.54.md).

The 0.4.53 renderer: Match the measured Claude 2.1.303 reference renderer
without relaxing exact content or duplicate-input checks. See the
[0.4.53 contract](references/release-0.4.53.md).

The 0.4.52 release: Recognize authenticated Claude Code versioned native
executables and settle handshake contract failures without duplicate input.
Verify each upgraded client's actual process, startup settings, automatic hooks
and real communication separately; see the [0.4.52 contract](references/release-0.4.52.md).

Automatic reply guidance permits ready messages during
normal work and native Goal. Fresh task preflight verifies identity, handshake
and validation without waiting for idle. Reviewed 0.4.50 file episodes preserve
their original controller and authority. See the
[0.4.51 release contract](references/release-0.4.51.md).

Bounded sweep history records scheduler timing,
rotation and contention without changing file inquiries. The complete reviewed
0.4.49 file protocol keeps its original authority. See the
[0.4.50 release contract](references/release-0.4.50.md).

Explicit inquiry re-enrollment by the original executor
can preview past its previous stop while preserving formal reservations. The
existing registration transaction clears that stop only after commit. Continuing
reservation inquiries require explicit authorization; installed versions do not
upgrade old episodes. The complete 0.4.48 file protocol remains supported under
its original pins. See the [0.4.49 release contract](references/release-0.4.49.md).

Recognize the measured auto-compact percentage hint
in a Claude elapsed footer without altering any composer bytes. Normal work and
Goal remain compatible with prompt-ready delivery; actual compaction, unknown
syntax and occupied drafts retain their checks. The reviewed 0.4.47 file protocol
keeps its original authority. See the [0.4.48 release contract](references/release-0.4.48.md) and
[evidence and handoffs](references/collaboration-evidence-and-handoffs.md).

Ready prompts can be sent in either direction during
active work and native `/goal` turns. Fresh preflight skips the optional
input-and-clear probe, so communication requires neither an idle turn nor a Goal
pause. Real drafts, original-attempt deduplication and native receipt checks remain.
See [busy communication](references/busy-bidirectional-communication.md) and the
[0.4.46 release contract](references/release-0.4.46.md).

A failed original continuation can clear its own
unsubmitted probe through `scripts/recover_bridge_probe_clear.py` when it sent
the token but issued zero clear keys. Preview is static; apply belongs to the
original supervisor, pins the archive and uses the complete original controller.
Each key has a durable intent and fresh identity, native-log and composer checks.
An interrupted or uncertain attempt cannot replay. Clearing submits no message;
publishing bridge evidence additionally requires the original active reservation.
See the [0.4.45 recovery contract](references/release-0.4.45.md).

Configuration checks close their bounded database
snapshot before normalization. Unchanged profiles never acquire a writer lock;
real changes use a short transaction that rolls back on any concurrent edit.
Busy and budget outcomes are visible. One job watches the live hook files and
CURRENT manifest, with profile imports discovered on its 60-second interval.
See the [0.4.44 release contract](references/release-0.4.44.md).

The [original bridge continuation](references/release-0.4.43.md) remains limited
to a never-typed token under its original supervisor, reservation and controller.

Reply consumption reports episode lock contention as
`EPISODE_BUSY` JSON with exit code 2. It leaves the reply unconsumed and does not
read episode state without the lock. Retry the same caller and reply SHA after
the suggested one-second delay. The command does not retry automatically.
See the [0.4.42 release contract](references/release-0.4.42.md).

Retain at least 10GB after installation and rollback costs; below 20GB, start
proactive space management.
Verified historical episodes retain file reservation
inquiries only when their original enrollment explicitly authorized them.
The scheduler uses the existing complete-release protocol checks and the same
sweep verification cache; it grants no new capability or controller migration.
Embedded and managed Codex identity also reject an intervening model CLI or
unknown process. Only supported shell/rtk intermediates relay identity; the
leaf resolver interpreter is allowed. See the
[0.4.40 release contract](references/release-0.4.40.md).

An embedded Codex TUI can authenticate its own tools
and native hooks through actual ancestry, matching native foreground selection,
kernel process identity, UUIDs, TTY and stable rereads. Children cannot borrow a
parent's identity. Original consumer proofs remain pinned; native `*_id` and
historical `*_uuid` fields must agree when both are present. This does not create
a native control socket or prove an existing client loaded the hooks. See the
[0.4.39 release contract](references/release-0.4.39.md).

File reservation checks read their shared directory
without loading the terminal harness and bridge. Authorization, STOP, ownership,
reservation and reply checks remain live. Complete release verification is reused
within one sweep, with every verified member and parent checked before reuse.
Reviewed 0.4.37 episodes retain their original capability and controller pins.
See the [0.4.38 release contract](references/release-0.4.38.md).

New submissions observe native reception for 30 seconds
by default, with a finite 0–60 second option. Reconciliation defaults to one
read-only observation and retains the original attempt, controller and RPC
diagnostic. A timeout or aborted turn never authorizes another input.

Nonblocking hook context makes unconsumed replies, pending handshake work and
callback settlement visible at actual tool boundaries. Exact reservations and
RELEASE records preserve task ownership; callback settlement never dispatches
the next task. Terminal reasks wait at least 60 seconds after the prior native
confirmation. The reask scheduler uses Standard scheduling; the configuration
guardian remains Background. Bounded discovery and measured Claude composer
layouts preserve original drafts and pending evidence.

The reviewed file protocol includes the complete 0.4.37 release without granting
historical issuers future authority. See [0.4.35 behavior and evidence boundaries](references/release-0.4.35.md)
and [validation history](CHANGELOG.md). Source tests, installation, actual
automatic hooks, full body reading and live native reception are separate checks.

Version **0.4.34** added late handshake reconciliation for a Claude native assistant record containing one exact ACK text block alongside structurally valid reasoning blocks. Extra text, tool blocks, malformed metadata and any ACK whitespace or nonce difference remain rejected; reasoning contents are never emitted in reconciliation results or archives. The original supervisor and complete original controller remain required. See [late ACK reconciliation](references/late-handshake-ack.md).

File scheduling retains the fully verified 0.4.33 v3 producer and v2 issuer,
including its historical enrollment of the 0.4.32 controller. An older issuer
cannot claim support for a future controller. No registration, consumption or
transport authority is inferred from compatibility.
For 0.4.34, the 40 ACK behavior tests passed locally and through the original
0.4.32 controller. Its final offline suite passed all 1,912 tests with no failures
or errors; those historical results are not 0.4.35 validation.

The Claude footer parser recognizes the HUD's measured input/cache token breakdown below complete composer borders. It preserves draft text and continues to refuse ordinary input during compaction or reconnect. New Codex-bound input goes directly to the original native thread: active-turn steer or idle-turn start, with no composer or Tab queue. Uncertain requests are observed without resending. Workflow hooks remain nonblocking, and supervisor PostToolUse exposes authenticated file requests without sending or settling work. An explicitly enrolled Claude maintenance notice can expose its verified original queued body at a tool boundary; it never changes the original transport receipt. See the [delivery contract](references/verified-compose-delivery.md) and [request lifecycle](references/configuration-and-reasks.md).

Version **0.4.33** also retains file scheduling for the fully verified 0.4.32
v3 producer and v2 issuer, including its original registrations of reviewed
older controllers. Complete manifest and descriptor checks preserve the
original controller and pending evidence; consumption remains executor-owned.
It also provides original-supervisor ACK reconciliation for a handshake rejected
after pasting without submission. Exact native ACK proof and the original
controller remain required; see [late ACK reconciliation](references/late-handshake-ack.md).
The 35 ACK behavior tests pass locally and through the original 0.4.32 controller;
the final combined offline suite passed 1,906 tests with zero failures or errors.

Version **0.4.32** adds CURRENT's reviewed cross-release v2 file interpreter,
recoverable registration transactions and explicit registration of an existing
episode using its original enrollment SHA. The file scheduler preserves original
controllers, UNKNOWN and pending evidence without loading old release code or
sending input. Formal reservations pause same-supervisor inquiries; a valid
newer reservation by the legitimate new supervisor ends the old supervisor's
inquiry, even for an unsupported old producer. Only exact RELEASE clears a
same-supervisor pause. That pause blocks new requests while retaining discovery
of an already-bound reply; only the original authenticated executor and its
original controller may consume it. Explicit `start --file-only` rejects
transcript channels.
This version also fixes a single-quoted `; Codex alone` launch false positive
while retaining checks on executable shell code. A separate explicit zero-input
recovery entrypoint defaults to a read-only history plan; applying it pins the
complete history SHA and appends one attempt through the original controller
only when every earlier attempt proves no input. See [SKILL.md](SKILL.md) for
the exact evidence and readiness checks. Its full offline suite passed 1,885
tests with zero failures or errors. Installation and active-client adoption need
their own evidence. See the [cross-release file contract](references/configuration-and-reasks.md#cross-release-file-contract).

The bounded handshake observer preserves the original timeout and send attempt
while reporting a late ACK without input. Validate registration, real scheduler
execution, automatic hooks, full body reading and two real continuing rounds
separately; offline tests and file ticks do not establish those live outcomes.

Caller discovery now distinguishes a validated optional foreground file from an
earlier use of the same PID. It preserves current-process, required-observer and
unique-client checks; it does not rewrite old records or select their threads.
See [PID reuse](references/shared-daemon-caller.md#reused-pids-and-optional-foreground-files).

# Multi-Agent Collaboration

New and successor Codex supervisors use their active
native foreground thread. Caller discovery failure cannot lock ordinary tools or
Stop; task evidence stays intact and terminal input still verifies both endpoints.
See [recovery without a session lock](references/native-caller-inventory.md#recovery-without-a-session-lock).

Evidence-bound collaboration for Codex and Claude Code in macOS cmux.
中文：复用完整上下文会话，明确分工，以真实报告和主动 callback 完成协作；
压缩故障及时干预，不以重开会话、假 ACK 或反复轮询代替恢复。

New messages use bounded single-line notices for pinned
bodies; formal packs and callbacks retain their dedicated task-bound routes.
Native RPC acceptance, Enter or Tab only requests submission. Reception requires the unique marker in a new user message of the bound receiver's native journal. See the
[delivery contract](references/verified-compose-delivery.md).

When present, the Codex control socket must authenticate as the current user
and expose the exact loaded receiver thread. If it is unavailable before input,
use terminal fallback. After an input intent, reconcile the original record and
never change transport or repaste. Claude sends directly without an idle or
composer gate. Old attempts retain their original controller and contract.

## Install

Requirements: Python 3.10+, macOS, cmux, and existing CLI agent sessions.
No provider credentials or agent subscriptions are included.

```sh
git clone https://github.com/lzhs1995/multi-agent-collaboration.git
cd multi-agent-collaboration
git checkout main
python3 scripts/manage_install.py install
python3 scripts/manage_install.py install --apply
python3 scripts/manage_install.py doctor
```

Install defaults to dry-run. It links this checkout into both clients' skill
directories and merges only this project's hooks. Existing foreign skill
directories are refused, never overwritten. Keep the checkout at its installed
path. For a live shared installation, stage a complete immutable release and
coordinate the upgrade with an installation lock, compare-and-swap checks and
rollback snapshots. The basic installer is not a transaction spanning the whole
installation. Preserve in-flight attempts under their original controller;
independent authorized work need not wait. Verify active-client adoption
separately, and never replace an agent session to upgrade its configuration.

```sh
python3 scripts/manage_install.py uninstall --apply
```

Uninstall removes only entries and links owned by this checkout. JSON backups
remain beside the original configs and may contain credentials: keep them private.
Other settings and hooks survive. Use `--home /absolute/sandbox` to test installation
without touching real client configuration.

For explicitly authorized 60-second executor follow-ups, add `--executor-reask`
to installation and doctor. To preserve owned hooks across cc-switch changes,
use the single [configuration guardian](references/configuration-and-reasks.md)
with a complete immutable release and its pinned CURRENT manifest. It repairs
hook registrations only; Markdown guidance does not itself register a hook, and
configuration repair does not prove a running client has loaded it.

## Use

Ask the supervisor to use `$multi-agent-collaboration`, then provide the objective
and designated existing executor. Both agents must read SKILL.md. The harness
binds their identity and task; scripts do not log in or start a replacement agent.

```sh
python3 scripts/mac_harness.py --help
python3 scripts/mac_harness.py preflight --task-id example-review \
  --artifact-root /absolute/path/to/example-review --executor-surface surface:7 --executor claude
```

Replace the example surface with a freshly verified binding. Do not paste task
prompts manually: use the finalized task-pack and callback entrypoints described
in [SKILL.md](SKILL.md). Public defaults preserve the current model and refuse
unknown compose text. Machine-specific preferences belong in local task settings.

The next-task chain is callback → independent acceptance/exact disarm → prepare
the next authorized pack → fresh live preflight → guarded dispatch. Explicit
60-second reasks are a fallback; file timers cannot wake an idle model.
ANSWERED awaiting consumption does not authorize repeated dispatch or a replacement
episode. Track accepted unique coverage, verified issue closure and actual stage
latency p50/p95, with sample counts and pending observations; ask_count is only a
diagnostic. Reply-detected-to-consumed time alone proves neither business idle time
nor its cause. See [dispatch cadence and metrics](references/configuration-and-reasks.md#dispatch-cadence-and-metrics).

## Verification And Boundaries

```sh
python3 scripts/test_all.py
```

Tests use isolated files and mocked agent surfaces, without sending live prompts.
Offline coverage is not certification of every cmux/client version. `doctor`
proves configured hooks can execute benign payloads; a real task still needs the
current-session handshake and current-task guards. Linux is an offline-test target,
not a live UI transport. There is no Windows/tmux or ACP adapter in this release.

See [lessons](references/lessons.md), [security](SECURITY.md), and
[version history](CHANGELOG.md). MIT; cmux, Codex and Claude remain separate products.

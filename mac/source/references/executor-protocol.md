# Executor Protocol

The executor owns implementation inside the task pack boundaries.
Use [evidence and handoffs](collaboration-evidence-and-handoffs.md) when reporting
child tasks, sharing a review or distinguishing notice reception from acceptance.
Normal work and Goal continuation do not prevent ready messages in either direction.

Before inspecting or changing source, the executor must read and obey the
`multi-agent-collaboration` skill named by the task pack. The handshake ACK is
only readiness proof. For implementation tasks, write the required report first,
then actively callback to the bound supervisor with the task pack's current
completion nonce. A visible `DONE`, a report file, or a successful send command
does not satisfy the callback contract.

For a finalized task pack, the terminal action is:

```python
cmux_bridge.submit_completion_callback("/absolute/path/to/task-pack.json")
```

This sends the exact pack callback and writes the exclusive, report-hash-bound
completion receipt. Do not merely print the callback on the executor surface,
and do not use raw `cmux send`, `cmux-agent ask`, or `submit_text` for terminal
completion. The Stop hook rejects a finalized executor task without the receipt.

## On Receipt

1. Acknowledge the `TASK_ID`.
2. Restate allowed scope and forbidden scope.
3. Name the report artifact that will be written.
4. Start with local inspection and local verification.
5. Confirm your own identity matches the task pack executor identity. If it does not match, stop.

If the supervisor message includes `ACK_TASK_ID`, `ACK_AGENT`, `ACK_STATUS`, and
`ACK_NONCE`, first perform only the cheap receipt/nonce check on disk. After that
check passes, emit the machine ACK immediately, before any substantive task work.
The accepted line is exactly:

```text
PREFLIGHT_ACK|<id>|<agent:identity>|READY|INLINE|<nonce>
```

The nonce is the receipt's challenge, not a value copied from a supervisor-filled
expected verdict. Human-readable readiness, `HELLO_FROM_EXECUTOR`, a prompt echo,
or a report file without this executor-generated nonce line does not satisfy the
gate.

If the task pack identity does not match the current pane or agent, emit:

```text
EXECUTOR REPORT | TASK_ID=<id> | STATUS=BLOCKED | EVIDENCE=IDENTITY_MISMATCH
```

If the task pack lacks the original user objective, source paths, allowed/forbidden scope, report path, or verification criteria, emit `BLOCKED` rather than guessing.

## Local-First Rule

Before requesting production access, run all feasible local checks:

- unit tests
- build/lint/typecheck
- local Docker compose
- HTTP smoke tests against localhost
- browser screenshot or Playwright/browser-use check for UI changes
- mocked auth or mocked external APIs when production credentials are not necessary

If production/VPS testing is unavoidable, stop and emit:

```text
EXECUTOR REPORT | TASK_ID=<id> | STATUS=NEEDS_AUTH | EVIDENCE=<report>
```

The report must say exactly which production action is required and why local testing is insufficient.

## Report Requirements

The executor report must include:

- task id and source plan
- supervisor/executor identity and role map reference
- changed files
- verification commands and results
- UI preview URL or screenshot path when applicable
- known risks and non-goals
- whether production was touched
- next recommended action

For cmux work, return meaningful milestones through the task's permitted guarded
status path to its bound supervisor. For terminal completion, always use
`submit_completion_callback` as shown above, with the finalized pack's exact
payload. Record a blocked outcome truthfully in the report; do not invent a
different terminal message or nonce to evade the pack contract.

`helper-parity` is a diagnostic for the external helper, not permission to replace
terminal completion with `cmux-agent ask` or `submit_text`. Those paths do not
produce the required report-bound completion receipt. A divergent helper or an
unconfirmed send requires inspection of the original attempt, not a second send
through a different path. A detector false-negative can occur after the receiver
already consumed the message.

Follow [bounded collaboration](bounded-collaboration.md) for post-Enter evidence,
supported read-only recovery and the stop condition. If the pinned runtime lacks
that recovery capability, preserve the evidence for the supervisor; do not call
the sender again and describe it as observation. Report acceptance, original
callback confirmation and task-marker disarm remain separate facts. After the
callback is verified and its marker disarmed, stop task-specific callback probes.

Write the evidence artifact before sending `DONE` or `BLOCKED`. Do not send
heartbeat messages more often than meaningful milestones; the supervisor's
single-instance callback-first sentinel is the fallback for silence (7200 seconds
for stable long work, 1800 seconds for medium-risk implementation, and 300-600
seconds only during a bounded high-risk cutover).

## Done Criteria

`DONE` means the scoped work is implemented, locally verified, and documented. It does not mean production is deployed unless production deployment was explicitly in scope and authorized.

## Evidence Hygiene (Mandatory)

Most false verdicts in this workflow were not produced by a broken system. They
were produced by a broken *measurement* of a working system. Nine such errors were
recorded in one task (`multi-agent-skill-hardening-20260830`), every one of them by
the executor's own probe. Treat your instruments as suspect before you accuse the
thing you are measuring.

**Exit codes belong to a process, not to a pipeline.** `cmd | tail` reports
`tail`'s status. Capture the status of the command you care about — `rc=${PIPESTATUS[0]}`
in bash, `$pipestatus[1]` in zsh — or run it without a pipe. Four separate
"PASS" claims in one task came from a downstream `tail`.

**Brace every shell variable adjacent to punctuation.** zsh reads `$REV:web/x.mjs`
as `$REV` with a `:w` history modifier, silently producing a mangled path. The
tell is `e3b0c442…` — the sha256 of empty input. Write `${REV}:web/x.mjs`.

**Absent is not zero.** `dict.get(key, 0)` prints a measured-looking `0` for a
field that was never written. Distinguish missing from measured explicitly, and
say "field absent" rather than "value 0".

**Print the key you actually read.** Before asserting a field's value, dump the
object's real key set. A probe that invents plausible names (`resolves_round` for
`resolves_rounds`, `submission_state` for `ack_state`) will report a defect that
does not exist — and may block a legitimate gate.

**A probe must prove it found its target before reporting zero.** A selector that
matches nothing returns "0 failures", which is indistinguishable from success.
Assert non-empty target discovery first; a bundle check that fetched 0 assets has
measured nothing.

**Compile-clean is not runtime-clean.** `py_compile` passes on a reference to an
undefined name; `NameError` arrives at call time. Import the module and touch every
new symbol.

**Do not trust your own earlier verification of file state.** An edit confirmed at
runtime was later found absent while sibling edits survived. Re-read from disk
before reporting a file's contents, and read the raw source rather than a summary.

**When a check fails, run the control before attributing the failure.** Run the
same check against the pre-change baseline. If it fails there too, the failure is
pre-existing and your change did not cause it. Three "regressions" in one task
were pre-existing conditions; two were fixture fidelity, not defects.

**A mutation that does not apply proves nothing.** If the excision's pattern never
matched, `applied=False` means the mechanism was never tested. Check that the edit
landed before reading the verdict, and remember that defense-in-depth means one
excision may leave a second guard still firing.

## Blocked Criteria

Use `BLOCKED` only when progress cannot continue without new user input, missing credentials, unavailable external systems, or conflicting requirements. Include the smallest needed unblock request.

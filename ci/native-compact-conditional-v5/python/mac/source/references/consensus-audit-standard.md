# Consensus And Audit Standard

Use this standard for both plan consensus loops and implementation audit loops.

## Minimum Rounds

For important multi-agent work, run at least 3 rounds unless the user explicitly waives the loop:

1. Agent A proposes, Agent B reviews.
2. Agent A revises, Agent B re-reviews.
3. Both agents confirm there are no material open conflicts.

For implementation, use the same pattern:

1. Executor implements and self-verifies.
2. Supervisor audits and returns fixes.
3. Executor fixes and supervisor re-audits.

More rounds are required if material conflicts remain.

For plan-review loops with a Grok/Composer executor, prefer this callback line so the supervisor can identify fresh reviews without searching stale files:

```text
GROK_PLAN_REVIEW | TASK_ID=<id> | ROUND=<R1|R2|R3> | VERDICT=PASS|PASS_WITH_CHANGES|FAIL
```

## Consensus Criteria

Consensus is reached only when all conditions hold:

- Objective is restated identically by both agents.
- User-facing behavior is unambiguous.
- Data flow and system ownership are named.
- Allowed and forbidden scopes are explicit.
- Security and production gates are explicit.
- Role map and callback path are explicit for multi-agent execution.
- Verification commands and pass criteria are concrete.
- Rollback or cleanup path exists for risky work.
- There are no unresolved P0/P1 disagreements.

## Disagreement Levels

- `P0`: blocks execution; user or further design needed.
- `P1`: should be resolved before production.
- `P2`: improvement; may be scheduled after current milestone.
- `P3`: note or style preference.

## Failure Modes

Mark the loop as not converged when:

- one agent reviews a stale artifact
- the source path is unclear
- the plan hides production access behind vague words
- "local preview" is treated as production proof
- UX requirements are split across multiple user-visible systems when the user asked for one simple flow
- an executor calls code delivery "accepted" without supervisor audit

## Non-Vacuity Obligation (Mandatory For Any New Gate)

A gate that has only ever been observed passing is not known to work. Every new or
strengthened check needs a **two-sided** proof driven through the wired entry point:

1. **Control** — shipped code, suite green.
2. **Excision** — remove the mechanism's semantics; the suite **must** redden.
3. **Semantics-preserving mutation** — rename a variable, invert an equality, hoist a
   predicate; the suite **must stay green**.
4. **Restore** — byte-identical, verified by hash, not by intent.

If the excision does not redden, either the tests do not cover the mechanism or the
mechanism does nothing. Both are findings. Two caveats learned the hard way: an
excision reported as *not applied* proves nothing (the pattern missed, so the test
never ran — check `applied` before reading the verdict), and a mechanism with layered
guards needs **all** layers excised, since a later layer can mask an earlier one and
produce a false "unproven" result.

Helper unit tests alone never satisfy this. The mutation must run the entry point the
hook or command actually invokes, with the config it actually loads.

## Failure Modes That Produce False Verdicts

Beyond a stale artifact or an unclear source path, treat these as non-converged:

- **A false green wearing a red hat.** When a gate fails, check its sibling samples
  for the same defect signature. One run failed at step 2 while an identical defect
  passed elsewhere by coincidence; fixing only the loud one leaves the bug.
- **An expected negative reported as a gap.** `cosign verify-attestation` failing
  because the workflow calls `sign` and never `attest` is correct behavior. Explain
  it; do not silently list it as missing coverage.
- **A gate present but non-load-bearing.** A marker floor that passes with emptied
  markers is decoration. Mutate the manifest, not just the code.
- **A field with no reader.** Contract inventories must count readers. A declared
  field nobody reads is advisory at best; label it or delete it.
- **A fixed sleep encoding machine speed as correctness.** Wait on a causal
  acknowledgement with a bounded loud timeout, never a fixed delay.
- **Cleanup proven only on the happy path.** Force each acquisition and release
  failure and check process deltas.
- **An owner-exempt required check.** A required context can be green having skipped
  every check. Report execution, not just conclusion.
- **The measurement, not the thing measured.** Before reporting an anomaly, verify
  the probe: wrong key names, shell-eaten path fragments, pipeline exit codes, and
  inconsistent file listings all manufacture findings that do not exist. Run the
  control against known-good input first.

## Audit Verdicts

Use these verdicts:

- `PASS`: meets scope and verification.
- `PASS_WITH_P2`: acceptable, with non-blocking follow-ups.
- `CONDITIONAL_PASS`: usable only after listed P1 items.
- `FAIL`: P0 issue or missing core verification.

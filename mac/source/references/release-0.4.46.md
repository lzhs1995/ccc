# 0.4.46: busy communication and original diagnostic recovery

Fresh preflight no longer runs the optional bridge probe. The guarded sender
already admits supported Claude input during active work and Codex native
steer/start. A ready prompt can therefore be sent without waiting for an idle
turn or pausing native Goal. Receiver scheduling and shared write handoff remain
the agents' responsibility.

No-probe receipts record `optional-probe-v1`, `NOT_RUN` and null clear fields.
Late ACK reconciliation recognizes that capability only in the complete original
controller. Explicit failed probe evidence cannot be ignored. The receipt schema
covers emitted pending, success, failure and multiple-executor records.

The original probe-clear recovery also offers explicit cursor observation with
durable key intents. It remains opt-in, under the original controller, original
archive and original supervisor. It cannot submit or grant a second recovery
budget. Helper-route refusals report the actual interpreter, hook/controller and
source hashes so fixes target the executing registration.

Offline regressions cover busy Goal delivery, queued originals without resends,
receipt schema consistency, original-controller reconciliation, helper provenance
and probe cleanup. Actual installation, automatic hooks, original-session native
reception, ACK and accepted work must each have their own evidence.

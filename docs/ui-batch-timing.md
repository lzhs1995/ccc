# UI batch timing evidence, version 1

The TUI records B, b and N on both workspace headers and individual rows.
`input_read` is the time `getch()` returns the keyboard or mouse event. It is
not the physical operating-system click time. `confirmation_accepted` is a
separate origin after the user accepts the confirmation. Neither interval is
silently excluded from acceptance.

Each action has a canonical UUID. The sidecar is
`<config directory>/ui-actions/<action_id>.json`. It contains `version: 1`,
`action_id`, `workspace_id`, `mode` (B/b/N), `input_kind`, `row_kind`, source
hashes and ordered `events`. Each event contains `phase`, `wall` (Unix seconds),
`monotonic` (seconds, not nanoseconds) and `boot_id`. Different or unknown boot
identities cannot establish cross-process elapsed time. Wall jumps invalidate
acceptance.

The accepted chain is input_read, confirmation_accepted, action_enqueued,
action_started, cli_received, job_created, action_finished. `job_created`
contains `job_id` and `new_job: true`. The new job persists the exact preceding
trace in `ui_timing_origin`. Repeated actions preserve that original field and
record job_reused; they never count as 50 new sessions. Cancellation, busy
rejection, failed commands, incomplete stages and recording errors cannot pass.
Timing writes are observational: a failed write does not bypass authorization
or prevent an otherwise authorized batch from launching.

Each original slot stores its surface/workspace/session/PID/microsecond birth
in `ui_original_identity`. The first validated task_started record supplies
the native `task_at` wall timestamp. Its first read supplies
`confirmation.first_task_observed` and a matching identity. Confirmation also
requires the original prompt and transcript checks; blocked proof is invalid.
Recovered confirmed jobs do not acquire a fresh observation timestamp.

Run the read-only collector with the selected config and action UUID:

```sh
python3 -B tools/ui_batch_acceptance.py --config /absolute/config.json \
  --action-id <action UUID> --output /absolute/result.json
```

The result binds `version`, `action_id`, `workspace_id`, `mode`, `job_id`,
`boot_id`, the sidecar events, absolute trace/job paths and SHA256 hashes.
The collector parses the exact hashed bytes and rechecks both files before
emitting a verdict. Runtime hashes and the collector's own SHA256 are recorded
before and after collection; changes invalidate acceptance. Output creation is
exclusive so an existing receipt is never overwritten. Network observers must match action, workspace,
mode, job and boot before comparing `events[*].monotonic`. Preserve both input
and confirmation coverage; a worker-barrier sample is not UI coverage.
`collected_at` is collection time, not batch terminal time or a substitute for
the first-task observations.

`startup_passed` requires all 50 distinct original identities, current source
hashes, a consistent creation/observation chain and both observation upper
bounds at most one second. `not_proven` includes slow observation: it does not
by itself prove the native task actually started late. Raw native wall times
remain separate from observation times. This tool does not validate continued
external connectivity, continuation latency, sustained 500 sessions or release
readiness. Native97's earlier 17.154889-second startup failure remains recorded;
these offline checks are not a new native/UI performance measurement.

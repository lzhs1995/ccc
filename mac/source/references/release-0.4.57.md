# 0.4.57 audit receipt repair

The proactive Stop wrapper used audit modes the writer rejected. Prompts and the
original inquiry runner still ran, but continuation and fail-open outcome
receipts were silently omitted. The wrapper now uses the supported advisory
mode and retains the specific outcome in scope_status. The latest receipt for
that thread, event and hook replaces its initial observation, by design.
Exception details are not copied into shared receipts.

The release preserves the complete 0.4.56 file controller, original issuer
scope and explicitly granted proactive-hook capability. Older controllers do
not gain that capability. Existing tasks retain original process identity,
controller, nonce, pending attempt and reply-consumption rules.

An installed or manually invoked hook is not automatic client adoption. Verify
actual original-process events. In Claude Code 2.1.303, an explicit --settings
file may remain pinned in startup memory even after its on-disk contents change;
check user and explicit settings sources separately. A refreshed global file
alone does not prove old startup hooks stopped running.

# 0.4.39: authenticate the original embedded Codex caller

This candidate was not released or installed separately. Its identity support
is incorporated into [0.4.40](release-0.4.40.md), including the review correction
for intermediate model clients. The evidence below describes the earlier delta.

The shared caller resolver previously returned no native proof when the actual
tool ancestors contained an embedded Codex TUI without a managed daemon. That
left tools, native hooks and reply-consumer authentication without the proof
needed for their bound workspace and thread.

The resolver now selects the actual ancestor TUI and requires a matching current
native foreground record, kernel process identity, tool ancestry, thread selector,
surface/workspace UUIDs and terminal TTY. Process ancestry and foreground are
reread before accepting the proof; the existing workspace/tree checks remain.
Hooks may use their supported native event payload when no thread selector is
inherited, but every supplied selector must agree. Children cannot acquire root
authority. Missing or malformed foreground evidence never falls back to resume
arguments, and an unmanaged app-server is not accepted as a TUI.

Public proof records `source=embedded-native-client-v1` and `daemon_pid=null`.
It exposes no full environment or private foreground record. Managed-daemon
resolution keeps its existing path. Reply consumption accepts native `*_id`
UUID fields as well as historical `*_uuid` aliases, requiring agreement from
every supplied field. Full original caller-proof equality remains mandatory;
this does not migrate or rebind an existing managed episode.

The delta passed 189 related identity, hook, workspace and consume tests in
5.114 seconds, including 28 new embedded-caller cases. The new cases exercise
tool and native-hook ancestry, absent and conflicting selectors, child/root
separation, UUID and TTY disagreement, PID/ancestry/foreground drift, real hook
resolution and original consumer-proof preservation. A live read-only caller
and consumer-authentication probe passed for the original maintenance session.
That probe consumed no mailbox and sent no input. Reuse those results unless
the source changes or a new unresolved concern requires another check.

This release adds no historical manifest to the file-protocol allowlist. The
reviewed installed history still ends at 0.4.37. The published 0.4.38 source has
no approved installation manifest at this boundary; its version string alone
cannot authorize a historical controller. Original episodes, pins and uncertain
attempts remain unchanged.

The patch creates no control socket and cannot repair its absence. Source tests,
installation, live automatic-hook invocation, natural scheduler execution,
complete body reading and an original native callback are separate checks.
Preserve the original callback controller and journal; do not replay a pending
attempt to validate this identity change.

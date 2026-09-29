# Codex Hook termination and empty command output

`hook exited without a status code` and `hook process terminated without an
exit code` mean the hook did not exit normally. Exit 137 is SIGKILL. Neither
message alone proves an out-of-memory event, a corrupt session or a bad model.
Check the command exit code as well as stdout; successful `true` is intentionally
silent, while a killed `echo` is a failed command channel.

## Confirmed local failure

The legacy `codex-tcc-keep` helper ran from both shell startup files and two
launchd jobs. Its watched paths included the Codex entry point that it rewrote
on every run. Concurrent fallback copies used the same `.codex.new` filename.
The updater also selected an older Homebrew cask after an official upgrade;
the upgraded executable was observed reverting to the older version.

Retire this copier before upgrading. Stop and disable its user and system
launchd jobs, remove only its exact shell startup invocation, and preserve a
backup. `scripts/retire_codex_copy_loop.py` previews the file migration by default;
`--apply` requires both jobs already disabled and atomically replaces only the
recognized legacy helpers with inert retirement notices. It leaves TCC grants,
Codex configuration, sessions, hooks and the cmux bundled wrapper intact.

This migration is specific to that legacy copier. It is not a general TCC
repair and must not be added as another shell-startup or WatchPaths task.

## Upgrade and verification

Use the official complete release package with its published SHA-256 and verify
the native signatures. Keep the CLI, `codex-code-mode-host` and packaged resources
from the same version in an immutable release directory. Atomically switch
verified entry-point symlinks; never patch or truncate an executing Mach-O file.
Do not let a second package updater overwrite those entry points.

Codex 0.155.0 includes the Unix Hook controlling-terminal fix in
[openai/codex#43876](https://github.com/openai/codex/pull/43876); 0.156.1 includes
that change. Existing CLI processes keep their loaded version until their normal
exit. Updating a symlink does not upgrade a running session. Preserve those
sessions instead of killing the fleet to obtain a uniform version number.

Verify repeated login-shell startup and direct/RTK commands, then a real Codex
tool call against a loopback fixture. Confirm the original native rollout and
process identity. Repeat the entry-point hash check after shell startup to catch
automatic version rollback. Keep load spikes and unavailable GUI inventory
separate from a verified hook repair; do not attribute every SIGKILL to the
copier without operating-system evidence.

## CCC recovery rule

CCC ignores only complete native Hook timeout/signal-termination cards when
checking whether a current provider error was superseded. They never cause a
continuation by themselves. A new answer, explicit hook exit code, hook decision,
approval UI, draft, queue, running task or uncertain original process still
blocks input. The normal original-task, authorization and delivery-ledger gates
apply after classification.

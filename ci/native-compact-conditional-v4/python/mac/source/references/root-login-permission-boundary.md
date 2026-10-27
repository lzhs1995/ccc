# Permission-limited system login ancestors

On macOS, the initial `PROC_PIDTBSDINFO` read of root-owned `/usr/bin/login`
can return EPERM even while its ordinary-user shell and Claude descendants are
alive and correctly identified. An ancestry walker that reads full BSD identity
before recognizing that boundary can consequently block every tool and Stop.
Classify the observed local identity failure before applying API availability
rules. Preserve the original sessions, task packs and callback attempts.

## Narrow boundary proof

`cmux_daemon_identity.py` permits a fallback only for initial EPERM/EACCES from
an ancestry read that explicitly allows the system-login boundary:

- The preceding child was already read through the strict same-user kernel
  reader. Its PID, birth, parent PID, executable, argv and selected environment
  must equal that saved identity before and after the parent checks.
- Public `PROC_PIDT_SHORTBSDINFO` reads agree twice on parent identity fields;
  effective UID is root, real UID is the caller's UID, and the process is live.
- The kernel path is exactly `/usr/bin/login` twice. The system binary is
  root-owned and not group/other writable.
- The entire ordinary ancestry is rechecked using the same child binding.
  A parent exit and PID reuse reparents its original child and fails that proof.

Short BSD has no birth timestamp. The resulting object deliberately has no
invented parent birth and is labelled `short_bsd_stable_child`. It can terminate
ancestry only; it cannot identify a caller, managed daemon, workspace or surface.
Absent processes, later permission failures, unknown or short reads, arbitrary
root programs and changed child identities still fail closed. The shared Hook
deadline and subsequent live UUID checks remain in force.

## Validation and efficient continuation

`test_root_login_permission_boundary.py` adds 14 tests for the valid boundary and
permission/error scope, UID/path protection, child reparent/birth/argv/environment
drift, short reads and final ancestry rechecks. Run it with the existing identity
and real Hook entrypoint tests; keep deployed-version and candidate evidence
separate. A read-only live process probe is useful evidence but does not show
that an existing Claude process has executed the repaired Hook.

Use one coordinated installation writer, a new immutable release and
compare-before-write configuration updates. Do not modify an installed release
or a running task's controller. After the original executor actually executes a
tool through the repaired Hook, continue its existing bounded task once through
the guarded channel. Do not resend its task or replay completed native actions.
Keep source validation, installation, runtime recovery and product acceptance
as distinct outcomes; continue independent product work while repair is pending.

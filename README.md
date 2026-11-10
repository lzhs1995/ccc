<picture>
  <source srcset="docs/assets/ccc-banner.svg" type="image/svg+xml">
  <img src="docs/assets/ccc-banner.png" alt="CCC — guarded recovery for terminal agents" width="900">
</picture>

# CCC

[English](README.md) · [繁體中文](README.zh-TW.md) · [Agent quick start](docs/agent-quickstart.md)

CCC is a macOS recovery manager for terminal-based coding agents. Its current
implementation observes **cmux** sessions and provides opt-in continuation for
supported Codex and Claude Code states, with session identity, input and delivery
guards.

The project direction is **cmux for outer workspaces and terminals, Herdr for
inner sessions and panes**. Herdr integration is not included in the runtime
at the code revision documented here. Installing this repository does not by
itself make nested sessions recover automatically after a reboot.

## What is available

This table describes the source at `79cc892`; it is not a claim that every client
version or every command has been tested on your machine.

| Area | Available now | Boundary |
| --- | --- | --- |
| cmux + Codex | Observation, explicit enrollment, guarded continuation and supported native Goal recovery | Requires the original session/process and current input/turn evidence; no replacement session |
| cmux + Claude Code | Hook-aware Stop handling, observation and guarded continuation | Disabled by default; enable deliberately and verify Hook/readiness evidence |
| Supervisor and audit | Terminal status view, session audit and component-owned health projection | A running daemon or readable screen alone does not establish readiness |
| Optional tools | Profile manager, quarantine-first janitor and network guard | Separate components with their own configuration and effects; see their guides |
| Herdr inner panes | Integration direction | No Herdr runtime adapter or Herdr CLI subcommand in this revision |
| Other coding-agent CLIs | Shared documentation entry points | Documentation does not implement or validate a runtime adapter |

Integration work is tracked separately in [draft PR #50](https://github.com/lzhs1995/ccc/pull/50)
and the [experimental cmux release branch](https://github.com/lzhs1995/ccc/tree/ccc/cmux-release-package-20261110-r1).
Read their exact code and evidence before use. An open PR, experimental build
or successful isolated test does not establish support in `main`, loaded client
code or end-to-end recovery after a physical restart.

## Start with the CLI

Requirements: **macOS 13+**, **Python 3.10+**, and [cmux](https://cmuxterm.app/).
The core uses the Python standard library. The optional access-check mode
requires Python 3.11+; it is not part of this quick start.

```sh
git clone https://github.com/lzhs1995/ccc.git
cd ccc
python3 -B cmux_codex_watch.py --help
python3 -B bin/cmux-stack --help
```

Both help commands were checked for this documentation revision. The examples
below match the parser; installation and live continuation were not performed
as part of this documentation change.

When you intend to install or update the local watcher, follow the
[operations runbook](docs/operations.md), then run:

```sh
python3 -B cmux_codex_watch.py install
python3 -B cmux_codex_watch.py status
```

`install` changes the local runtime and LaunchAgent. A new configuration starts
in `dry-run` with Claude continuation disabled; an existing configuration is
preserved. `start` restarts installed code and does not install checkout edits.
The installed aliases are `ccc` and `cmux-codex-continue`; the source form above
also works without relying on an alias being on `PATH`.

To authorize continuation, replace `FULL_SURFACE_UUID` with the **complete UUID
of the intended existing Codex surface**, review the status, then arm:

```sh
python3 -B cmux_codex_watch.py track-surface FULL_SURFACE_UUID
python3 -B cmux_codex_watch.py status
python3 -B cmux_codex_watch.py arm
```

`arm` changes the global mode and clears the global pause. Review **all existing
enrollments** first; individual target pauses and identity/input guards still
apply. For Claude Code, the existing registration option is
`track-surface FULL_SURFACE_UUID --allow-non-codex`; it also requires
`claude_enabled: true` and verified Hooks/readiness. The option is not support
for arbitrary agent CLIs. See [registration and observation](docs/registration-observation.md).

To inspect an installation without asking it to continue a task:

```sh
python3 -B cmux_codex_watch.py status
python3 -B cmux_codex_watch.py hook-audit --json
python3 -B cmux_codex_watch.py context-audit --json
python3 -B bin/cmux-stack status --json
```

Use `python3 -B cmux_codex_watch.py tui` for the supervisor. Opening it does not
enroll a workspace; its enrollment, batch and interrupt controls have real
side effects. Detailed controls stay in the [advanced operations reference](docs/advanced-operations.md).

## Recovery boundaries

- **429 / 503:** complete, current native retryable error banners can enter the
  guarded recovery path. A quoted error, provider outage, draft, menu, queued
  input, running turn or missing identity is not permission to send. CCC cannot
  guarantee upstream availability or repair account authorization failures.
- **Original identity:** keep the original session ID (SID), process generation,
  workspace/surface UUIDs and delivery ledger. Titles, reusable surface numbers
  and terminal text do not establish ownership.
- **Unknown delivery:** retain the original attempt and reconcile its evidence.
  Do not blindly resend text, press Enter, clear a ledger or create a replacement
  session. Any later retry must satisfy the original session and delivery guards.
- **Reboot recovery:** a registered watcher, installed binary or saved layout is
  not an end-to-end restore result. Verify the actual loaded runtime, saved
  identities and original sessions after a normal, authorized restart.

### What 180 and 300 seconds mean

The intended integrated recovery policy uses a **180-second warning /
300-second guarded intervention boundary**. This is an integration target,
not a timer policy implemented by this revision or an unconditional cancel/retry.

The existing source has different, separate timers:

| Timer | Current meaning |
| --- | --- |
| `claude_context_stall_sec = 180` | Claude compaction has made no progress; the absolute context timeout is 900 seconds |
| `claude_context_enforcement` | Off by default; observation and enforcement are separate |
| `LOG_STALL_SEC = 300` in the session audit | The watcher log has not advanced; this is a diagnostic threshold, not a session recovery deadline |

See [the watcher](cmux_codex_watch.py) and [the audit](ccc_session_audit.py).
These timers do not authorize stopping a process or replaying an uncertain
request. Do not claim the proposed 180/300 behavior is active from this README.

## Documentation and coding agents

- [Shared agent instructions](AGENTS.md) and [agent quick start](docs/agent-quickstart.md)
- [Operations and releases](docs/operations.md)
- [Registration and observation](docs/registration-observation.md)
- [Codex Hooks](docs/codex-hooks.md)
- [Optional network guard](docs/network-guard.md)
- [Advanced operations](docs/advanced-operations.md) — the prior detailed English / Simplified Chinese reference, preserved in full
- [Changelog](CHANGELOG.md) · [Documentation index for tools](llms.txt)

`AGENTS.md`, `CLAUDE.md`, `GEMINI.md` and `llms.txt` lead to the same instructions.
Automatic discovery depends on the client; explicitly ask it to read
`AGENTS.md` if needed. These files neither install CCC nor add runtime support.

Keep credentials, real profiles, transcripts and runtime journals out of
commits and issue attachments. Status output may contain session metadata;
use placeholders or synthetic identities in public examples. The supplied
[config example](config.example.json) and [profile template](templates/_template.example.json)
are starting points, not production credentials.

## License

[MIT](LICENSE).

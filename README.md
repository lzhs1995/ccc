# CCC

CCC is a macOS command-line toolkit for Claude Code and Codex operations:

- `cmux_codex_watch.py` observes configured Claude/Codex surfaces and applies the guarded continuation policy.
- `cmux_supervisor_tui.py` displays workspace titles, collaboration roles, component health and explicit enrollment controls.
- `ccc_session_audit.py` prints audit summaries without changing sessions.
- `janitor/src/` contains the quarantine-first cmux cleanup worker and guard.
- `ccp_new.py` manages profiles interactively; credentials remain in a user-selected directory.
- `bin/cmux-stack` projects component status without merging the execution processes.

The UI follows a compact terminal vocabulary inspired by mature CLI tools such as
[mole](https://github.com/tw93/mole): restrained colour, clear section rules,
short status symbols, and a readable degraded/unknown distinction.

## Requirements

- macOS 13 or newer
- Python 3.10 or newer
- [cmux](https://cmuxterm.app/) for surface discovery and terminal I/O

The code uses only the Python standard library. No profile, token, session
transcript, or runtime state is bundled in this repository.

## Local setup

```sh
python3 -m py_compile cmux_supervisor_tui.py cmux_codex_watch.py ccc_session_audit.py ccp_new.py bin/cmux-stack
export CCP_PROFILE_DIR="$HOME/.config/ccc/profiles"
export CMUX_STACK_CCP="$PWD/ccp_new.py"
export CMUX_STACK_WATCHER="$PWD/cmux_codex_watch.py"
export CMUX_STACK_JANITORCTL="$PWD/janitor/src/cmux-janitorctl"
```

Copy `templates/_template.example.json` to `$CCP_PROFILE_DIR/_template.json`
and fill credentials only on the local machine. The profile manager validates
before replacing a file and keeps its own rollback/trash rules.

Install the watcher with the interpreter and checkout you intend to maintain:

```sh
python3 cmux_codex_watch.py install
python3 bin/cmux-stack install --apply
```

The watcher installs a checksummed bundle under
`~/Library/Application Support/cmux-codex-continue/runtime/releases/` and points
launchd at that bundle. It never runs the daemon from a Documents checkout.
`ccc start` restarts installed code; run `ccc install` after changing source.
Existing configuration, input, sessions, completion latches and event ledgers
are preserved. A new installation defaults to dry-run with Claude continuation
disabled. Enable `claude_enabled` in the local config only for intentional use,
explicitly register a surface, then use `ccc arm`:

```sh
ccc track-surface FULL_SURFACE_UUID --allow-non-codex
ccc status
ccc arm
```

`scripts/render-launchagents.sh` renders installation files without loading
services. `cmux-stack` mutations require their documented `--apply` switch.

## Verification

Run the source-only checks before any LaunchAgent action:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -B tests/test_cmux_stack.py
python3 -B tools/ccp_pty_stress.py --script ./ccp_new.py --rounds 3
bash -n janitor/src/cmux-janitor.sh janitor/src/guard.sh
python3 bin/cmux-stack status --json
python3 ccp_new.py --status --json
```

Production deployment is intentionally outside this repository's automated
tests. Keep generated plists, credentials, logs, and local handoff artifacts out
of Git. See `.gitignore` for the default exclusions.

## Safety model

The control plane reports component-owned state; it does not recompute a second
health verdict. The janitor moves candidates to quarantine before disposal and
fails closed when its live store or quarantine setting is not trustworthy.
`ccp_new.py` is a foreground editor by design, so credentials are never written
by a background LaunchAgent.

## Supervisor view contract

The supervisor is a read-only projection of cmux-owned state. Its table keeps
the following columns stable: `监控`, `位置`, `程序`, `Hook`, `上下文`, `画面`,
`错误`, `续跑`, `协作`, `标题`, and (when the terminal is wide enough) `session`.
`程序` identifies the client (`Codex`, `Claude`, `grok`, `Copilot`, `gh`,
`shell`, `其他`, or `未知`); it never carries a health verdict. `画面` is the
state vocabulary (`空闲`, `运行中`, `菜单`, `待续跑`, `已排队`, `已过时`,
`额度耗尽`, `正在输入`, `看不清`, `非Codex`, `Claude关`, `Hook等待`, `输入保护`,
`发送中`, `已完成`, `已续跑`, `Hook待验`, `Hook缺失`, `Hook旧版`, `身份冲突`,
or `需人工`).

`R` 只是重扫，不会登记任何东西。New
workspaces remain `未登记` until the operator explicitly chooses `a` (one
surface) or `w` (the whole workspace). The stale/unknown thresholds used by
the source are documented in the source comments and are intentionally more
conservative than the send path; a stale observation is never converted into
an authorization.

- **监控**：`未登记` / `监控中` / `空转` / `整池空转` / `已暂停` / `整池` / `已排除` / `待检测` / `投递待验` / `发送失败` / `读取异常` / `服务阻塞`。登记仍保留，投递异常直接显示；焦点行显示最近检查的年龄。
- **程序**：`Codex` / `Claude` / `grok` / `Copilot` / `gh` / `shell` / `其他` / `未知`。程序列只显示身份；空转属于「监控」列。
- **画面**：`空闲` / `运行中` / `菜单` / `待续跑` / `已排队` / `已过时` / `额度耗尽` / `正在输入` / `看不清` / `非Codex` / `Claude关` / `Hook等待` / `输入保护` / `发送中` / `已完成` / `已续跑` / `Hook待验` / `Hook未验` / `配置待核` / `模型错误` / `身份冲突` / `需人工` / `未初始化` / `等待压缩` / `压缩中` / `读不出` / `提交中` / `未确认` / `投递待验` / `发送失败` / `服务阻塞`。完成、压缩和客户端重试不代表续跑器故障。
- **Hook**：当前进程代次的 Hook 验证结果；历史身份记录本身不能授予发送权限。
- **错误**：最近的观测原因或错误类型，与当前画面、续跑计数分别展示。
- **续跑**：当前 episode 的累计发送次数。

上下文读数过期阈值为 `CONTEXT_STALE_SEC` = 120 秒。终端观测采用独立阈值
`max(30 秒, 3 × poll_interval_sec)`；过期或身份不明时显示未知。
续跑时效另外按 `2 × poll_interval_sec` 检查，诊断数据保留在 `ccc status` 中；后台身份诊断的宽限不能放宽续跑时限。

## Registration recovery and observation coverage (v0.2.0)

Explicit registration triggers background readiness checks. If the latest genuine
unfinished Stop was rejected solely because the surface was not authorized, CCC
can revalidate that original event once, within its existing one-hour intake
window. Full workspace/surface UUID, session, process start and explicit CMUX
process ownership must agree. A later prompt, completion, send or pending submit
transaction prevents replay. The original event ID and rejection history remain
in the ledger; a restart never resets deduplication.

Current viewport text is preferred. A failed text read can fall back to a fresh,
identity-checked render grid. A replay response containing only `seq: 0` and IDs
is unreadable; a valid grid at sequence zero is valid. No scrollback or historical
screen is used as send authority. TTY reuse and foreign process identity cannot
prove that an agent belongs to a terminal slot.

`ccc status` adds `observation_coverage` and `registration_readiness` while keeping
existing fields. Coverage distinguishes readable targets, live unreadable agents,
dormant native runtimes, paused targets, closed targets and unknown evidence.
`cmux-stack status --json` projects the component-owned verdict and safe counts.
Live coverage gaps degrade the stack; incomplete or stale evidence is unknown.
A daemon PID by itself cannot make the entire stack healthy. Unregistered agents
remain outside continuation authorization.

Python 3.10+ remains supported. `CMUX_STACK_WATCHER_LABEL`,
`CMUX_STACK_JANITOR_LABEL` and `CMUX_STACK_GUARD_LABEL` override labels individually;
otherwise `CMUX_STACK_LABEL_PREFIX` or the current user's prefix supplies them.
The watcher itself uses `CCC_LABEL_PREFIX`; custom controller labels must match
the labels installed by the corresponding component.
The launcher follows symlinks and honors `PYTHON`.

See [the operations runbook](docs/operations.md) and
[the incident and regression notes](docs/registration-observation.md).
`release_launcher.py` is included as an auxiliary tool; the installed watcher
LaunchAgent does not pass through its separate approval gates.

Codex recovery also recognizes the complete provider `rate limit exceeded:
Your requests to MODEL for MODEL in REGION have exceeded rate limit.` banner,
including terminal wraps. Working indicators, queued follow-ups, user input,
and newer output retain priority over error recovery.

The compact status vocabulary is deliberately borrowed from mature CLI tools:
`✓` means healthy, `⚠` means degraded or needs attention, `✗` means failed,
and `→` marks an action or transition. Colours are a secondary cue: green,
yellow, red, purple, and muted grey are paired with text so a no-colour terminal
has the same meaning. Horizontal rules use stable ASCII characters and rows are
clipped to their display width before curses writes them.

## License

MIT. See [LICENSE](LICENSE).

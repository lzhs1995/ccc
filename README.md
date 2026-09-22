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

Inventory refreshes and control commands run outside the keyboard thread. A
slow cmux scan or pool interrupt therefore leaves navigation, refresh and quit
available. Repeated refreshes coalesce and old results cannot overwrite newer
requests. The selected workspace has clickable controls (the same keys work):

- `w` 整池授权：覆盖该池现有和后续 Codex，保留单路排除。
- `P` 暂停 + Interrupt：先落盘停止整池续跑、取消批量创建，再向本池 Codex 请求 Escape；保留原 session。未确认的进程或投递单独报失败。
- `W` 恢复整池：恢复续跑，保留单路暂停、排除；不会自行重启被取消的创建任务。
- `B` 新开50 + 授权：在选定 workspace 的主区域 pane 新建50个后台 Codex 标签页。每路确认原 session 和空输入框后发送一次 `show me u power`，再按原 transcript 的 `task_started` 确认启动并交给续跑器。

`B` requires global sending to be enabled and the selected pool to be unpaused.
It never changes another pool or silently clears existing pauses. At most four
new sessions wait for startup together. Progress reports created, ready,
submitted, started and incomplete counts. Startup dialogs and drafts are left
for the operator. Repeating `B` resumes an incomplete batch; a lost create reply
is recovered from its startup receipt and an uncertain prompt is not resent.
After all 50 slots finish, another confirmed `B` starts a new batch. Jobs run in
the background and survive closing the panel; `P` cancels their authorization.
CLI equivalent: `ccc batch-workspace FULL_WORKSPACE_UUID`.

- **监控**：`未登记` / `监控中` / `空转` / `整池空转` / `已暂停` / `整池` / `已排除` / `待检测` / `投递待验` / `发送失败` / `读取异常` / `服务阻塞`。登记仍保留，投递异常直接显示；焦点行显示最近检查的年龄。
- **程序**：`Codex` / `Claude` / `grok` / `Copilot` / `gh` / `shell` / `其他` / `未知`。程序列只显示身份；空转属于「监控」列。
- **画面**：`空闲` / `运行中` / `菜单` / `待续跑` / `已排队` / `已过时` / `额度耗尽` / `正在输入` / `看不清` / `非Codex` / `Claude关` / `Hook等待` / `输入保护` / `发送中` / `已完成` / `已续跑` / `Hook待验` / `Hook未验` / `配置待核` / `模型错误` / `身份冲突` / `需人工` / `未初始化` / `等待压缩` / `压缩中` / `读不出` / `提交中` / `未确认` / `投递待验` / `发送失败` / `服务阻塞`。完成、压缩和客户端重试不代表续跑器故障。
- **Hook**：当前进程代次的 Hook 验证结果；历史身份记录本身不能授予发送权限。
- **错误**：最近的观测原因或错误类型，与当前画面、续跑计数分别展示。
- **续跑**：当前 episode 的累计发送次数。

上下文读数过期阈值为 `CONTEXT_STALE_SEC` = 120 秒。终端观测采用独立阈值
`max(30 秒, 3 × poll_interval_sec)`；过期或身份不明时显示未知。
续跑时效另外按 `2 × poll_interval_sec` 检查，诊断数据保留在 `ccc status` 中；后台身份诊断的宽限不能放宽续跑时限。

Codex failed-turn events also wake the matching workspace/surface observation
ahead of the fleet scan. A read-only watcher checks known original transcript
metadata every 250 ms and reads at most 16 KiB only after a file changes. This is
a scheduling hint: fresh viewport, current native failed turn, input protection,
authorization and delivery deduplication still gate every send. Pending hints
survive in-flight reads; regular scans retain capacity under a stream of errors.
The interval is a scheduling target, not a guaranteed end-to-end latency.

When that original transcript has a known lifecycle and its bound PID/start
identity is current, redundant routine viewport reads run every 10 seconds.
Failure events still request an immediate read and retry failed observations
every second. Missing, unreadable, mismatched or stale native coverage restores
the configured regular scan interval. On macOS, identity checks use the public
`libproc` API directly: even a PID-filtered `ps` can stall under fleet load.
The send boundary checks the process identity again without using the advisory
monitoring cache. No native monitoring state authorizes a terminal write.

Legacy sessions without a SessionStart binding revalidate their original open
transcript. During a missing GUI inventory refresh, the previous PID is only a
hint: PID/start, exact workspace/surface environment and the open transcript
must still agree. Darwin reads these placement variables through
`KERN_PROCARGS2`, keeping process checks off the slow `ps` path. Conflicting
fresh inventory, reused PIDs and multiple open sessions still block input.

Recovery recognizes the native dim placeholder when styled animation covers the
composer prompt, including narrow-window high-demand banners split inside words.
After a Codex resume, the native Hook trust notice can appear below the previous
provider failure. Its complete text and native warning style (including wrapped
lines) do not count as newer model output. Other warnings, partial notices, user
drafts, menus and queued input keep their existing guards. The complete native
`Connection failed: error sending request` banner is a retryable transport error;
stranded-prompt recovery recognizes it using the same original-session checks.

Complete native HTTP 408/429/500/502/503/504 error banners are retryable. User
drafts, menus, aborted turns, quotas and non-retryable status codes remain blocked.
Older Working chrome above a newer failure card does not hide that failure;
the original native turn must still have completed before any continuation.

On advertised v2 automation sockets, tree/process snapshots and Codex delivery
use the official RPC endpoint with explicit UUIDs. This avoids CLI selector
resolution timeouts. Process snapshots require `include_processes: true` both
in the request and response; missing process data is an unavailable observation,
never an empty agent inventory. Input uses one connection and one attempt. A
lost, malformed or mismatched acknowledgement remains uncertain in the ledger;
CCC never retries that write through a second transport.

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

### Workspace pause / Interrupt

In the Supervisor, select a workspace header or any surface in that pool and
press **P** to pause the whole pool and send **Escape** to its live main-area
Codex sessions. **W** resumes pool monitoring. These actions use the same
workspace UUID as whole-pool authorization. The pool pause covers explicit,
discovered and newly opened surfaces; individual pauses/exclusions survive
resume. A partial interrupt failure leaves the entire pool paused and reports
the failed UUIDs. Transport acknowledgement reports an interrupt request,
not proof that every native task has stopped.

```sh
cmux-codex-continue pause-workspace WORKSPACE_UUID
cmux-codex-continue resume-workspace WORKSPACE_UUID
```

Native failure hints keep their priority when the first read or identity lookup
is temporarily unavailable. Unchanged transcripts are not reread, and retrying
a hint never authorizes input or clears a delivery record.

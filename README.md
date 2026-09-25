# CCC

CCC is a macOS command-line toolkit for Claude Code and Codex operations:

- `cmux_codex_watch.py` observes configured Claude/Codex surfaces and applies the guarded continuation policy.
- `cmux_supervisor_tui.py` displays workspace titles, collaboration roles, component health and explicit enrollment controls.
- `ccc_session_audit.py` prints audit summaries without changing sessions.
- `janitor/src/` contains the quarantine-first cmux cleanup worker and guard.
- `ccp_new.py` manages profiles interactively; credentials remain in a user-selected directory.
- `bin/cmux-stack` projects component status without merging the execution processes.
- `ccc network` optionally shares AnyRouter API probes and verified route failover across sessions; see [network guard setup](docs/network-guard.md).

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
- `B` 新开50 + 授权：在选定 workspace 的主区域 pane 新建最多50个后台 Codex 标签页。每路确认原 session 和空输入框后发送一次 `show me u power`。任一路收到新的模型响应，即关闭本池请求入口，并在1秒内 Interrupt 本池全部 Codex（含成功路和原有会话），取消剩余创建名额。

`B` requires global sending to be enabled and the selected pool to be unpaused.
It never changes another pool or silently clears existing pauses. At most four
new sessions wait for startup together. Progress reports created, ready,
submitted, started and incomplete counts. Startup dialogs and drafts are left
for the operator. Repeating `B` resumes an incomplete batch; a lost create reply
is recovered from its startup receipt and an uncertain prompt is not resent.
After all 50 slots finish, another confirmed `B` starts a new batch. Jobs run in
the background and survive closing the panel; `P` cancels their authorization.
CLI equivalent: `ccc batch-workspace FULL_WORKSPACE_UUID`.

Codex 0.154 creates its first rollout only after the first prompt. For a newly
created batch slot, CCC verifies the native writer lock's new session UUID and
the original process before writing text. It then checks the exact draft and
presses Enter separately; a pasted newline alone is not submission evidence.
The original rollout must contain both the exact prompt and `task_started`,
including the current `response_item` user-message format, before that slot is
released to the guard. An uncertain Enter is never repeated. Process fallback
ignores read-only history files that Codex opens while indexing older sessions.

- **监控**：`未登记` / `监控中` / `空转` / `整池空转` / `已暂停` / `整池` / `整池／启动中` / `已排除` / `待检测` / `投递待验` / `发送失败` / `读取异常` / `服务阻塞`。登记仍保留，投递异常直接显示；焦点行显示最近检查的年龄。
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
Provider rate-limit banners also accept the one extra `rate limit exceeded:`
prefix added by the native CLI. Quoted examples and extra prose still fail the
complete-banner check.
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

### 批量启动与授权恢复

`B 新开50+授权` 先保存50个持久化名额，后台绑定目标 workspace 的主区域 pane。尚未接入时重复按 B 复用未完成批次；首个成功响应会停止全池并取消余下名额。停止已确认后再次按 B 创建新批次。全系统共享4个启动许可，相邻创建至少间隔0.5秒。等待人工确认/草稿的路会让出许可；创建回执不明的记录持续核对，30秒后让出许可，但不会重发创建请求。

B 的实时保护只接受配置中 `batch_guard.origin_job_id` 与真实 `workspace-batches/JOB_UUID/job.json` 一致的工作区。每次输入、成功判定和信号发送均核对当前 cmux workspace/surface 完整 UUID；强制信号还核对本机 PID 的微秒级创建时间与进程归属。工作区名称、临时 `surface:N` 编号、相同目录均不能扩大停止范围。人工排除的会话仍属于本池的成本停止范围，但原排除设置会保留。

全局 Dock 属于窗口，即使界面把它显示在当前 workspace 下，也不进入该 workspace 的 B 停止范围。移入全局 Dock 的 surface 同样按新的归属排除。

**接入成功**：当前活跃原生 thread/turn 的非空模型文本、推理、计划增量，或可证明由模型发起的工具事件。登录、授权、`task_started`、HTTP 200、`Working`、工具输出、用户引用与旧 transcript 都不算成功。旧会话恢复返回的历史不会触发保护。

**停止已确认**：所有目标的原生任务已结束，且请求后端已经退出。`turn/interrupt` 的 RPC 回执仅表示请求已送达。首个模型事件立即锁住入口，并并行发送原生 Interrupt；350ms 时向仍未停止的精确进程发 SIGTERM，650ms 时发 SIGKILL，1000ms 前核验结果。超时、成员关系不明或进程身份变化均显示保护异常，不能显示“全部已停”。取消后关闭后端标准输入，阻止内部 goal、队列和自动重试继续发请求；surface 和原会话日志保留。

每个受保护 surface 使用 `codex app-server` 与私有 Unix WebSocket relay，普通 TUI 通过 `codex --remote` 连接。独立 watchdog 监测守护进程心跳；观察、控制或全池覆盖失败时关闭本池入口并按同样的 UUID/进程约束停止。B 启动前原位接管旧会话，保留原 surface/session、目录、配置及可验证的草稿；接管未完成则保持停池并报告异常。条件启动器在非 B 工作区直接执行原 Codex 二进制，不启用自动中断。直接绕过启动器产生未受观察的本池进程，会被覆盖核对识别并触发保护停池。

旧会话的迁移预检在停止或替换进程前完成。原进程若仍持有已删除或被替换的历史文件，B 会关闭批量启动和续跑入口、报告历史无法安全恢复，并保留原进程；不能为完成接管而销毁唯一存活的历史。

启动保护记录在 `batch_start_holds`，面板显示 `整池／启动中`，不计入人工暂停。原 session 日志必须包含提交后的首个 task_started 和精确的 `show me u power` 才解除保护；AGENTS 与 environment_context 合并消息、大消息、未写完的 JSONL 行均支持增量确认。回执丢失或 worker 重启不会重开同一名额、重发 prompt 或 Enter。守卫持续核对历史批次，只清理有原始证据的批次保护，保留人工排除和暂停。

`w` 可重复执行授权和核对，也不解除整池暂停。`W` 在实时保护就绪后恢复原会话，不重放旧提示、不新建 surface、不补开被取消的名额；需要新批次时显式按 B。原生后端被强制结束后，W 仍使用原 session ID。

面板、普通续跑器与批量 worker 共享带时间戳的清单；常规 `system.top` 扫描间隔至少5秒。B 实时保护独立使用原生事件、直接 libproc 身份核对及合并的当前 cmux 成员查询，不依赖旧清单或延迟落盘的日志。启动等待不再因25秒或360秒到期而被永久放弃。

本地验收（只连接回环模拟上游，不使用生产凭据）：

```sh
python3 -B tools/guard_native_acceptance.py --surfaces 60 --output /tmp/ccc-guard-native-acceptance
python3 -B tools/guard_native_acceptance.py --surfaces 2 --rearm --response tool --output /tmp/ccc-guard-rearm-acceptance
python3 -B tools/guard_cmux_acceptance.py --count 50 --legacy --output /tmp/ccc-guard-cmux-acceptance
python3 -B tools/guard_cmux_acceptance.py --count 50 --early 2 --legacy --lifecycle --output /tmp/ccc-guard-early-acceptance
python3 -B tools/guard_cmux_acceptance.py --count 2 --fault watchdog-stall --lifecycle --output /tmp/ccc-guard-fault-acceptance
python3 -B tools/goal_cmux_acceptance.py --output /tmp/ccc-goal-acceptance
```

原生界面显示 `Goal stalled (/goal resume)` 且出现可恢复限流错误时，续跑器另行核验精确进程、原 session writer lock、原生 goals/logs 数据库的设备与 inode、当前 blocked goal 和对应 Turn error。即使原 rollout 已失联，也可通过一次 `/goal resume` 恢复原 goal；不伪造 `task_complete`，不追加普通提示词，不把旧错误或仍在运行的 goal 当作恢复许可。命令先写入已验证的空输入框，确认完整草稿后仅发送一次 Enter。

每个新批次使用自己的 `workspace-batches/JOB_UUID/native-db`。首次启动通过 SQLite 只读备份复制已有元数据，保留真实的历史索引完成状态；不复制数 GB 的日志和分页历史库，也不改 `CODEX_HOME`、凭据、hooks 和原始会话日志。这样既避免全局日志库写锁，也避免每个新窗口重新扫描全部历史。新 shell 回执还绑定父进程代次，启动检测可直接核验其 Codex 子进程，不必等待全量 `system.top`。

macOS 默认 `kern.tty.ptmx_max=511`，当前内核支持的硬上限为999。每个终端通常占一个 PTY；达到上限时 B 保留未完成名额、等待空位，w 仍可正常授权。`launchd/ccc-pty-limit.plist` 是可选的管理员配置，安装到 `/Library/LaunchDaemons/local.ccc.pty-limit.plist` 并由系统 launchd 加载后，每次开机设置999；常规 CCC 安装不会自动更改它。提高 PTY 上限不会消除 CPU、内存和磁盘负载限制。

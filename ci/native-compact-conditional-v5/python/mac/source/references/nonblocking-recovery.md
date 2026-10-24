# Non-blocking coordination and successor recovery

0.4.19 supersedes historical instructions that freeze every tool, require an old
supervisor for handshakes, or block Stop while awaiting a reply. Installed workflow
hooks use cmux_workflow_advisory.py; historical checker modules are diagnostic only.
The sender still refuses duplicate input or an unverified target; Claude terminal
input also protects occupied drafts. Native Codex input leaves its composer intact.
That refusal applies to one delivery and must not end authorized work.

The raw cmux identify caller may belong to the shared daemon. Use cmux-agent self,
which invokes the native foreground-thread resolver also used by the transport.
Do not infer failure or a cross-workspace caller from inherited environment.

For an authorized successor or maintenance coordinator, bind exact endpoint UUIDs
and user-message evidence with successor-rebind-v2. Maintenance communication does
not require prior task settlement, prove old supervisor failure, replay old packs,
or authorize shared writes. Keep old task evidence and create distinct new scope.

Before input, journal the complete payload, live process/session/transcript and
fresh EOF fence. New Codex input uses one native steer/start, without composer or
queue operations. Claude terminal input uses one paste and one submit maximum.
Uncertain results reconcile read-only; never replay a historical queued callback
through the native route. See the [delivery contract](verified-compose-delivery.md).
A new exact native user record after the fence proves reception. ACK, body reading,
callback and acceptance remain separate. Never present installation, manual hook
execution, a queue item or a saved report as proof of all live clients adopting it.

One owner integrates shared fixes. Other agents send evidence to that owner while
continuing their original work. Use zero, one or two executors for independent
outputs. API failure triggers bounded recovery then authorized SOLO takeover.

## Consume pending supervisor requests and repair zero-input notices

At a major tool boundary, before a new long batch, and before reporting progress,
inspect the current supervisor surface for queued follow-up inputs. Verify each
pinned UTF-8 body and SHA, read it, and reply to its exact mailbox. Record consumed
and pending items; duplicate notice markers do not need duplicate replies. A UI
queue, body reading, native receipt, ACK and business acceptance are separate.
Do not leave maintenance requests unread while repeating status probes.

Since 0.4.26, supervisor PostToolUse offers authenticated file requests as bounded,
deduplicated additionalContext without invoking the old gate. It neither replies
nor accepts reports and cannot wake a supervisor with no tool event. A reasking
executor should receive its bound mailbox reply first, then finish its explicit
wait before fresh preflight/dispatch. Each runner call has a polling budget of
at most 540 seconds; synchronous I/O may extend wall time. A persisted
WAITING_REPLY is not a running process. Version 0.4.27 adds a separate explicitly
enrolled 60-second file scheduler; job registration does not enroll old episodes.
It never sends or wakes an idle model. The supervisor's actual thread receives
at most eight file reminders per tool event, with fair rotation and cursors
committed after output flush; a child cannot consume the parent's inbox. Keep old pending attempts
when their original receiver process has exited; do not rebind or replay them.

For new formal tasks, generate `TASK_PACK_V2` with `cmux_bridge.task_pack_notice`
(or `task-notice --task-pack /absolute/task-pack.json`) from the finalized pack.
Never hand-assemble a multiline task prompt. Since 0.4.23, wire validation happens
before a task journal is reserved. The explicit `scripts/repair_task_notice.py`
coordinator handles only a first `NO_INPUT` wire-format rejection with `events=[]`.
It verifies the pinned original attempt, original payload and unchanged pack;
requires the same caller, receiver process/session and original lock; and uses
the complete original immutable controller for terminal input and native proof.
The read-only default must return `READY_ZERO_INPUT` before `--apply`. It appends
only the original journal's one second attempt, retaining the original bytes,
completion nonce and callback controller. A crash consumes this budget. Pasted,
queued, uncertain, changed or already-received attempts cannot use this route.
The corrected TASK_PACK_V2 marker is the task id; this is not a new business task.

A busy Claude may expose a `queued_command` attachment while working on a task.
Preserve that observation without declaring an exact native-user receipt. Never
resend merely because an ACK or report preceded that record. Executors must wait
for a finalized, hash-pinned task notice rather than treating a draft pack found
on disk as authority. Continue independent authorized work in SOLO when a peer
is unavailable; maintenance does not reopen accepted research reviews.

0.4.24 also recognizes local SQLite and disk-space inventory imports in quoted Python document heredocs. Agent names after semicolons inside literal document data are not CLI launches; adjacent real shell launches remain checked. Verify the exact rejected command through the real hook before claiming recovery.

## 正常握手默认允许（2026-10-12）

用户授权协作或接棒后，主管可直接向指定原会话发起新握手、维护联络、进度询问和
独立新任务的准备，不再等待旧主管批准、旧 callback 结算或无关 pending 清空。
不因继任、旧报告未读或旧轮次未完成而要求用户重复授权。健康的当前握手直接复用；
迟到 ACK 沿原 nonce 核收；已证明零输入的拒绝修复后可重新做当前预检。
握手仅确认身份和通信，不附加科研审查、三轮共识或旧任务审计作为前置条件。

0.4.28 对 reask 的独立新任务/新主管请求读取原始请求绑定，将无关 inherited
pending 的完整内容和原请求另行持久保留，再释放新 episode 的发送位置。
原请求、journal、未确认状态均不改写，不重放旧正文；同任务同主管的未知发送仍
沿原次观察，单纯换 controller 或重启同一 episode 不解除它。

Claude footer 的 Unicode `…` 和 ASCII `...` 行尾截断均支持，包括 git cwd
和 `▸` 摘要；它们只在完整输入框边框外解释。正常空输入框可以握手，真实草稿
保持原样，两标签共用同一 pane 时串行发送。收到正文、ACK、执行和业务验收分别记录。
# Queued Claude maintenance

An executor can receive a `queued_command` attachment and still spend repeated
tool rounds waiting for a dead supervisor. Do not repaste that notice, send an
extra Enter, reset the session or relabel the queue as native reception. Old
callback ownership does not prevent reading an authorized successor notice.

From 0.4.31, the shared maintainer can explicitly enroll that original
`successor-attempt-v2` maintenance attempt with
`scripts/claude_maintenance_context.py --attempt /absolute/attempt.json
--queued-offset ORIGINAL_OFFSET --queued-bytes ORIGINAL_RECORD_BYTES
--maintenance-request /absolute/authorized-request.txt --reply /absolute/new-reply.json`.
Obtain the offset and byte count from the actual original native queue record,
not a screen capture or an invented fence. The registry is private and exclusive;
it enrolls only this session and attempt. Installation alone enrolls nobody.

The existing native-delivery PostToolUse advisory verifies that complete queued
record and body, the original authorization, transcript device/inode and fence,
and the hook's actual Claude process. It emits the complete body as additional
context at a natural tool boundary. Read it before another old-supervisor wait,
then write your own bound reply with actual write and reservation state. A
maintenance notice is not a formal task pack. The successor retains dispatch.

Exposure never modifies the original journal or proves native user reception,
body reading, a safe write boundary or task acceptance. Keep those observations
separate. Successful output is reminded no more often than every 60 seconds;
only an exact body-read reply suppresses the reminder. A failed output flush
does not consume it. Unknown or absent enrollment, mismatched evidence and
storage failures emit no blocking response. Stop never forces another turn.

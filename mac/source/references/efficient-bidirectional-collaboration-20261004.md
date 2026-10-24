# 高效握手、多执行者与双向投递

协作节奏沿用本文；投递、恢复、封口与等待的唯一现行操作合同见[原生投递与有界等待](verified-compose-delivery.md)。旧屏幕消费和分立 Enter/Tab 预算不再授予操作权限；下列注明日期的历史验证保留原范围。

## 先备任务，再握手

先备好任务目标、精确输入、允许写入目录、报告路径、完成条件和验证方法，再对当前同workspace的原会话做真实身份核验、握手与finalize。健康ACK不重复获取；迟到ACK先核原task/nonce。分别记录准备、握手、投递、执行、回调、核收耗时。握手上限不是强制等待，不因设置600秒就等满600秒；观察预算过短不能归因执行者，不强制使用实测时间的三倍。

## 以任务收益决定一个或两个执行者

复用用户指定且携带上下文的会话。第二执行者仅接独立工作，例如数据文件核查与协作工具审查；各有task_id、nonce、输出根，互不改对方结果。只有supervisor整合最终材料和维护公共脚本。两个会话占同一pane的不同tab时如实记拓扑，不称两个额外side split。共享Stata/Office/网盘仍服从原资源队列，executor数量不等于资源并发数。

只有需要计划共识的工作才走既有共识流程；普通已授权的一次审阅不因此增加科研审轮。执行者仅在任务包授权目录写入；supervisor的安装权限另列且须来自用户，不能反向改写已经冻结的任务包。

明确retryable服务故障在原尝试结束后同会话有限重试、每次至少60秒；认证、欠费、额度失败不盲试。投递不确定也不能重贴。重试次数不证明失效：须按[失败窗口规则](availability-and-resources.md#retryable-claude-api-failures-evidence-before-takeover)从首次真实API失败起连续至少300秒、阈值处有新鲜失败且当前尝试已终态，期间任一真实成功即重置；排队、静屏、未知投递和握手超时都不计入。满足后沿用户既有授权固定原终态、冻结executor写入并转solo_self_review，继续本机能做的工作。保留原会话，不clear、不新建替代。恢复协作在安全边界使用原UUID。

## 双向发送铁律：核原生全文记录

prompt、任务包和 callback 均使用原受保护 bridge。原 `PASTE_INTENT`
固定真实 caller、同 workspace/surface/process/session/transcript、完整
payload 与新鲜 EOF fence。只有该 fence 后新增、全文完全相等的 native
user 才为 `NATIVE_RECEIVED`；保留全部空白，不拼接记录或查 marker 代替。
Claude `queued_command` 和 UI queue 仍 pending。活动、ACK、空 composer、
按键成功与报告已读均不能代替原生接收证据。

首次只粘贴一次，有界等待完整稳定原草稿。忙碌 Codex 明示
`tab to queue message` 且完整原文匹配时首次直接 Tab；其他清晰受支持
状态 Enter。自动/显式恢复共用最多一次补键，意图落盘即耗用。
当前 `--recover-stranded` 只属于 `submit-text`，须满足原 `NATIVE_PENDING`、
`PASTE_INTENT`/`ENTER_SENT` 及全部原绑定/历史/草稿门禁；任务和 callback
只读核原次。缺原绑定或 fence 不追补，UNKNOWN、压缩、排队、重连或
结构改变不补键，不重贴、不换 nonce。详细条件以统一合同为准。

## 回调日志与旧任务收尾

先保存固定报告，再调用`submit-completion-callback --task-pack /absolute/task-pack.json`。实际尝试目录与任务包的`completion_receipt`同目录，名称为回执stem加`-attempts/`。新版journal绑定任务包SHA、报告SHA、nonce、原executor UUID与目标，并先写PASTE_INTENT后输入；同inode锁避免两个进程重复回调。记录为零输入的失败最多另试一次；任何可能已粘贴的尝试禁止重贴。

`--reconcile-only`只读取已有真实尝试和接收端，终端输入为0；可以持久化本次观察，且仅在原 fence 后新增的完整精确 native user 满足原绑定时保存回执。不另发恢复消息。若旧`<receipt>.pending.json`存在，即使内容损坏也拒绝新发送和自动迁移，交supervisor按原证据结案。缺journal不能补造历史尝试，报告SHA出现在supervisor文件只能证明该报告被核查，不能自动生成transport receipt。原生记录不可核验时如实保留未确认；滚屏和屏幕活动不改变核收条件。

旧任务若真实callback已经进入supervisor会话且报告已独立核收，可由supervisor保存原marker及核收依据后，仅`disarm --task-id`该已终态任务。明确记录正式bridge receipt缺失；不得伪造receipt、反复回调、全局禁用Stop hook或让已完成executor无限修复回调。Stop hook提供完成门禁，supervisor负责真实旧任务的有据结案。

## 安装、复测与版本

维护源与实际安装两边都保留本文及对应入口。实体目录安装若被manage_install.py判为foreign，保留目录；按已授权窄维护调用现有mutation_locks及replace_bytes，先核原字节，备份后安装，保留mode与before/after SHA。不得将实体目录强换symlink或整树覆盖。发送器、journal reader、hook、启动器和文档适配须来自同一完整固定版本，不拆开覆盖；历史任务沿原控制器。

安装完成、测试通过、新进程实际导入和旧客户端热加载是四件事；不为更新skill重启正在工作的应用。已冻结任务包保留旧pins，后继显式记录维护版本，不冒称旧输入未变。通用文档不含研究数据；本机维护patch另归档，无Git元数据不得声称已提交或发布。


### Handshake detector recovery (2026-10-04)

A `DELIVERY_UNVERIFIED_BY_DETECTOR` or `DELIVERY_QUEUED_AT_RECEIVER` result must not end the handshake before the configured ACK wait runs. Retain the dispatch error and original task/provider/nonce; use the existing strict assistant-response parser for a bounded read-only wait. Never resend text or Enter. A matching ACK may complete the handshake while the original transport uncertainty remains recorded; do not fabricate a dispatch-submitted timestamp. Compose-busy and never-submitted states still fail immediately. No matching ACK means failure, not permission to resend or proof of executor silence. This source change does not retroactively rewrite frozen receipts or prove live-client reload.

### 当前Claude页脚识别（2026-10-05）
真实边框、模型行和完整已知页脚同时满足时，允许识别计时行与运行中Bash状态行；未知尾行、shell提示或缺边框仍拒绝。识别为agent仅证明输入界面类型，不等于身份、空compose、任务可接收或消息已消费。不能因UNKNOWN重新握手或重贴未确认回调。


## Complete-message post-submit confirmation

Both directions require one new native user record after the fresh EOF fence saved by the original PASTE_INTENT, from the same bound workspace/surface/process/session/transcript, exactly equal to the complete payload. Screen activity, ACKs, queue records, fragments across records and whitespace normalization cannot confirm reception. Read-only reconciliation applies the same native rule and sends no input. Missing original binding/fence cannot be backfilled; preserve the original attempt without repasting.


## Durable task dispatch

`submit-task-pack` now records task identity, complete payload hash, task-pack hash,
workspace/caller/target/pane UUIDs and each input intent under
`~/.local/state/multi-agent-collaboration/task-dispatch-v1/` before terminal input.
The post-submit hook must read this same journal format. It revalidates the
original pack, payload, live UUIDs, receipt, attempt, original native binding/fence
and exact new native user; read-only reconciliation retains its pinned observation.
A new sender journal without a matching hook reader is an incomplete upgrade:
do not deploy it merely because sender-only tests pass. Missing or changed
evidence remains unconfirmed, and the hook never creates a receipt or sends input.
The target lock excludes simultaneous dispatches from this controller. A changed
prompt or pack cannot create a replacement attempt for the same caller/task.
A process interruption after paste intent is uncertain, never permission to paste
again. Only a recorded zero-input attempt permits one explicit retry. Existing
delivery10 `deliveries-v1` records require their original controller; this journal
does not migrate or replace them.

After an uncertain attempt, invoke the same `submit-task-pack` command with
`--reconcile-only`, preserving surface, text, pack and marker. This only observes:
no paste, Enter or Tab. It requires an exact whole new native user after the original
PASTE_INTENT fence before writing a receipt. A queued, partial or cross-record message
remains unconfirmed. An existing receipt rejects another dispatch. The default
marker is the task ID and must occur literally in the prompt; an explicit marker
must also occur in the prompt. Forced composer replacement is refused for durable
task dispatch. Preserve the original draft and use the original recovery flow.

This contract covers the canonical task dispatch entrypoint. Generic status and
handshake transport retain their own existing control flow; this is not a claim
that every legacy sender has been migrated or that a running client hot-reloaded.

The post-submit hook also reads the canonical callback's original `*-attempts`
journal and completion receipt. It revalidates the task pack, report bytes/hash,
executor and receiver identity, original attempt, original native binding/fence,
and the exact complete new native user record.
Read-only reconciliation must bind its zero-input observation. A receipt flag
alone is insufficient. Missing or changed evidence remains unconfirmed; the hook
never resends input or manufactures a receipt. This prevents a valid callback
from being rejected merely because a hook understands only a legacy journal.

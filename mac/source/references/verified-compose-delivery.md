# 直接通信、草稿保护与原生送达核验

双方 prompt 就绪即可发送，包括忙碌、原生 Goal 续轮与压缩期间。
允许直接 cmux send/send-key/paste；bridge 是可选的绑定与回执入口，不是通信许可。
不为通信暂停 Goal，不要求空闲窗口或完整稳定 composer。原会话不替换，
不清空草稿，不把通信当成共享写入归属或业务接受。

## 直发失败不得继续按键

允许忙碌时通信不授权提交用户正在编辑的草稿。直接调用 cmux 时必须检查
send/paste 的返回结果：命令拒绝、非零退出或结果未知时，立即停止该次发送，
不得在 sleep 后无条件 send-key Enter，也不得用 --force 绕过用户草稿保护。
当前 bridge 在输入意图之前显式读取 `surface.input_state`，核目标UUID与空输入区；
查询失败、字段不明、草稿或对话框均为零文本输入。正常工作、Goal和压缩不在
拒绝条件中。随后使用单条 `cmux --json paste --submit`，不用 `--force`。
例外识别：已由原生进程/会话绑定核实的Claude 2.1.303可能在cmux注册表中
显示agent=false。此时cmux的empty默认值不算草稿证据；另外读取真实Claude
输入框，要求上下完整边框间确实为空。只读输入区，不用工作状态或footer稳定
作为条件；查询本身失败、输入框不全、文字或对话框仍拒绝。
必须解析返回正文：`submitted=true` 才记录已发提交键；false记录已粘贴未提交；
底层 `delivery=queued` 仅记录主机排队，不补键。非零退出、超时或未知结果都只
核原次，不假定零输入、不重贴，也不另发 Enter。

补键必须同时满足原次唯一预算、输入区完整内容与本次原文相等、转录尚未收到，
并在按键前重读输入区。只有 marker 相同不够；出现用户前缀或后缀便不按键。
粘贴与提交的两个意图在单条主机请求前落盘；input_operations记录意图预算，
不是系统调用次数或实际已接收字节数。失败粘贴没有补键权限。
CLI 对原目标给出完整已知的“草稿/对话框，未发送”拒绝时，单列
`REFUSED_BEFORE_PASTE` 和实际输入0，保留两条原意图。其他非零退出、信号、
额外输出、不同目标或未知/本地化诊断仍为 UNKNOWN，不根据 rc=1 推断零输入。
已知拒绝不自动重试或重置 journal；后续恢复须另核原证据，不给补键权限。

限制：当前 cmux 的 input_state 查询与 terminal.paste 是两个RPC，检查之后
用户仍可能输入，尚无原子比较并提交保证。CLI查询失败会放行，所以bridge自行
严格预检；这缩小风险窗口，不能宣称绝对不会提交并发用户草稿。补键的重读与
按键同样不原子。根治需要cmux主机在同一RPC内检查输入归属并执行。
未注册agent的Claude目标完全依赖上述真实读屏检查，不能计入CLI自己的草稿保护；
额外读屏也不能消除检查后用户输入的窗口。
同理，单次主机请求中的进程变化无法在粘贴与主机提交之间由调用方截断；
请求后身份变化会阻止确认和任何后续按键，不冒称首次主机提交未发生。

多个发送者必须共用原pane/target输入锁，保持整个粘贴/提交过程串行。raw cmux
本身不替代此互斥；不能把另一个发送者的草稿当作空框或共用同一正文。不同
消息同时就绪可以立即请求发送，但不能并发写同一个输入区。

## 发送与唯一送达标准

使用唯一 marker，固定目标 surface UUID 和原生 session/transcript；bridge
在原 attempt 中持久化 payload、身份和输入前 EOF fence。当前新 attempt 固定
`receipt_contract=native-user-marker-v1` 与原 marker，旧 attempt 不改合同。
只有目标原转录在原 fence 后新增**含该 marker 的 user 消息**证明送达。
客户端包装、附加上下文或空白变化不再使已到达消息失败。marker 回执不证明
业务正文被独立执行，也不授权拼接或提交接收端原有草稿。assistant、tool、
Hook、历史记录和 Claude queued_command 不证明收到。读取完整正文、ACK、
callback 业务核收与资源释放分别记录。短通知送达不等于其 SHA 正文已读。

Codex bridge 优先调用已绑定原线程的 turn/steer 或 turn/start；control socket
缺失/拒绝连接，或原线程元数据明确不能提供输入方法、且尚未产生输入意图时，
自动转终端直发。身份不符不是降级理由。RPC 输入意图一旦落盘，超时和
未知结果只能核原次，不再转终端，避免双送。Claude 默认直接终端发送。

终端在输入区空闲时一次原文粘贴并请求主机提交；不等工作回合空闲或完整屏幕稳定。默认有界观察
30 秒（显式0–60秒）。未见 marker user 且原生状态仍为 pending 时，若屏幕
显示压缩时在有限预算内等压缩结束，未结束则保留补键预算并返回 pending；
输入区完整本次原文仍在且已不在压缩时，先再核一次转录和输入区，然后最多补一个 Enter。
补键意图先落盘；不再次粘贴，不因崩溃、重试或换版重置预算。已入原生队列
先观察，不补键。超出本轮观察窗不等于丢失；使用原参数 --reconcile-only。

压缩只从当前输入区外的最新活动状态行判断，正文提到 compacting 不算压缩。
若压缩结束后仍未确认且完整本次原文留在输入区，原发送方可对同一普通消息、
正式任务或 callback 使用原参数加 `--recover-stranded`；继任联络使用
`successor_rebind_cli.py recover` 加原 artifact、text-file、marker。
这些入口只接受当前 marker 合同下恰好一次已确认粘贴，且主机明确报告
首次提交键已发或未发的原次；后者仍共用原次一次补键预算。不要求原 ENTER 截图。
仍压缩、原文不完整、混入其他文字、主机或原生已排队时不补键；原生已收件
只核收。一次补键意图已落盘后，失败、崩溃和再次调用均不恢复该预算。
历史消息中的 marker 不授权提交当前其他草稿。初次直发保留上述空输入区预检。
旧合同仍走原恢复规则；`--reconcile-only` 始终零输入。

原 pane/target 输入锁及逐操作身份核验仍保留。直接 raw cmux 返回成功只证明
操作返回；操作者必须记录并核目标原转录，不能把 Enter 当送达。

## 历史原次

历史原 controller、payload、nonce、binding/fence 与预算保留，按其原规则核收。
新规则不授权对历史已粘贴、排队或结果未知的消息重新粘贴或重发。
缺原绑定不补造成功证据；已收到消息不因 receipt 文件缺失重新发送。

## 封口、Stop 与主管报告发现

PostToolUse 成功结果须符合客户端官方 schema：仅通过
`hookSpecificOutput.hookEventName=PostToolUse` 与字符串 `additionalContext`
传回完整核验结果，不能把内部 `action/results` 直接放在 JSON 顶层。
Codex 对未知顶层字段拒绝整份输出；原生记录存在与自动 hook 输出被接受
分别核验。现役工作流观察器始终 exit 0；身份、遥测或发现失败不阻断工具，
也不生成收到或接受的证据。显式诊断的失败状态与全局 hook 分开记录。
离线核官方 schema 后，再取活跃客户端实际自动调用记录；手动调用不算。

只读 CLI 帮助不是一次投递：精确的 bridge `--help` / `-h` 调用，包括
Python 脚本启动及 Codex 并行工具中的独立命令，不要求 task pack、身份
查询或原生回执。同批次真实 send/callback 仍逐条核验；正文中的 `--help`、
Python `-c` 和动态改写命令不能享此例外。遇到只读帮助被误报，保留原次
工具证据并修解析器，不重发旧 callback，不补造任务包或回执。

报告完成后先冻结报告、task pack 和原 attempt 既有记录，再走原 callback。
原包提交后不得擅自扩展研究、改动回调绑定报告或重复发送；追加工作需有
独立明确范围。普通工具、维护及诚实结束回合不受旧任务封锁。
原 controller 可零输入 reconcile；核收如需追加观察或原子
发布 receipt，必须沿原控制器及持久锁，不改写历史。诊断入口是
`scripts/cmux_callback_diagnose.py --task-pack <原任务包绝对路径>`；
它不发键、不造回执、不清 marker；该诊断的范围不限制其他已授权工具。

现役 Stop/SubagentStop 只经 `cmux_workflow_advisory.py` 记录实际调用并放行，
不调用旧 checker，不输出 deny、block 或 `continue:false`。结束回合无需
先取得主管回复、callback receipt 或旧任务 disarm。诚实说明未核收边界，
不把正常结束冒称 callback confirmed、共识或业务通过。
[历史 Stop 生命周期](stop-hook-lifecycle-20261005.md) 仅供旧控制器诊断，
不能恢复全局会话门禁；原任务、报告和回执原样保留。

从 0.4.26 起，主管 PostToolUse 的 advisory 入口有界调用
`supervisor_inbox.discover`，核验当前主管后提供去重的文件请求上下文。
遥测失败不阻止这次独立发现。它不读取旧报告、不答复、不发键、不接受任务，
无工具事件时也不能唤醒空闲主管。主管仍显式读取固定报告或认证的 active
markers，再按原证据分别核收通信、裁决业务及定向解除原任务。
发现请求不是投递确认、业务接受或 disarm；不依赖回执的主线工作继续。

## 默认 idle 观察与 CCC 有明确等待期限

idle Stop 只记录实际调用并允许结束，不写旧式 idle binding、不启动求派。
没有求派记录、后台进程、主管答复或 disarm 都不能成为 Stop 阻断理由。
`cmux_executor_idle_guard.py` 的历史实现仅供显式诊断，不由现役观察器调用。

需要主动请求时，显式 `executor_ready.py ask`（旧拼写 `request`）
最多提交同一原请求一次。未确认保留同一 payload、marker、nonce 和原
attempt；后继只能核原次，不能周期性重贴。主管答复必须绑定原请求，
沿原 bridge 到达原 surface，或写到该请求给出的精确 mailbox。
mailbox 必须携带原 loop record 的 `episode_id`、`caller_surface_uuid`
和 `task_id`（即使 task_id 是空字符串也不能省略）。idle binding 通知的
独立 `episode_id` 不代替 loop record，也不证明已建立等待段。

显式 `executor_ready.py persist` 只读原请求、绑定 transcript 和 mailbox，
默认 60 秒、最长 300 秒；原期限与已用预算沿原 episode 保存，不因重启
延长。任务、主管或绑定 transcript 改变时保留原段并记
`UNRESOLVED_IDLE_EPISODE`，不自动另开一段；损坏的原请求/停止记录
保守终止，不自动重发。真实答复、新任务、
operator stop 或期限耗尽都结束观察。`WAITING_DEPENDENCY`、
`TIMED_OUT` 与收到答复分别记录；超时不是投递失败证明、业务完成或
下一轮自动催派许可。保持原状态，等待有实际授权的新事件。

CCC 在合法报告等待边界与 `WAITING_SUPERVISOR` 同步，并保留有限期限；
不得把等待改写为无限模型续轮。native Goal 是独立机制，不能从 CCC 配置、
本地 hook 放行或源码修改推断其实际状态。等待结束、工具可用性、任务接受
和新任务派发分别验收，不通过全局关闭保护来消除循环。

## 用户明确授权的每60秒主动求派

默认 executor_ready 的有界单次观察不撤销用户另行授权的主动求派。
显式 `scripts/executor_reask.py run` 执行期间每5秒观察、每60秒最多发一个
新 marker；单次轮询预算最多540秒，同步 I/O 可延长实际耗时。命令返回后
原 episode 仍可为 WAITING_REPLY，这不证明 runner 仍活跃。必要时按授权
继续原 episode，不重贴旧请求、不重置原 attempt 的按键预算。
全局 `cmux_executor_reask_stop_guard.py` 注册经 advisory 观察器，不强迫续轮。
0.4.35终端新询问从上一条消息原生确认后至少60秒起算，并重新核原receipt；
缓存时间不能单独授权输入，文件求派独立计时。正式预留或待原控制器消费的
回复使本次run返回，给出 `runner_returned=true` 及明确原因；
`wait_boundary` 保留 `idle_observed=false`、`goal_state_changed=false` 和
`fresh_preflight_required=true`。这不停止或重开episode，不证明终端已空闲。
绑定主管答复或 operator stop 可结束等待；旧 AUTHENTICATED_NEW_DISPATCH
出口依赖较新的 idle binding，而现役观察器不写它，不能仅凭新包声称已触发。

派单前由绑定主管先按原请求模板写精确 mailbox，令显式等待返回，再做新鲜
现场预检及正式派发。TASK 邮箱答复本身不是任务包。原接收进程退出时保留
terminal_pending、原 binding 和 fence；历史完整原生收件可独核，但不冒造
官方 receipt，不删除 pending 或重开 episode 来绕过去重。

新求派使用同版 executor_ready、正文存储和 bridge。超过700 UTF-8字节或
带尾换行时固定完整正文再发送短通知，返回的通知 receipt 不证明正文已读。
新 Codex 求派优先原生 steer/start；原生方法不可用且零输入时终端直发，不等空闲。
原未确认请求只读核收；已有不确定输入时后继请求仍使用已配置文件通道，
不重放原次。原生入口不可用或不适合 Claude 终端输入时使用文件通道；每轮记录实际写入或错误，
空 channels、轮数增长和历史 marker 均不证明送达。历史 marker 无原次
receipt 时只保留 UNVERIFIED；真实队列由同版 bridge 的实际结构识别。

这个兼容更新不宣告其他未完成的 reask 生命周期方案已上线。安装须保存原
active episode、旧控制器与未决证据，不把默认 idle hook 全局改成无限续轮。

## 可选 helper 与直接发送

安装器通过 `scripts/render_cmux_agent.py` 从固定基线渲染 helper，并核验完整
字节及同版 adapter。该核验用于安装一致性，不是通信许可。直接 cmux、
shell 包装与替代发送入口不再因本技能而被禁止；实际命令错误由运行时报告。
helper 调用 bridge 以保留绑定、原次与回执；操作者也可直接发送并独立核目标
转录。正式 task/callback 的 helper 保留任务绑定入口。reconcile 保留原 intent
且零输入，缺证明只报 pending，不报收到。

## 一致版本与证据边界

发送器、journal reader、PostToolUse、Stop、idle、启动器和技能适配层
必须来自相容的完整固定版本。实际 sender 与原 receipt validator 核当前
投递及其原 attempt；未确认不能报成功，普通工具不扫旧账。
`cmux_native_delivery_guard.py` 的全局注册同样经过 advisory 观察器，旧 checker
仅供显式诊断；sender 仍核端点、原次去重及原生 marker 证据，不要求 composer 准入。
旧屏幕/send-proof hook 的退役由受支持安装器限定本包注册，保留其他任务及 foreign 配置。

配置落盘、完整版本安装、新进程导入、真实 hook 执行、当前客户端加载、
实际 native user 入站、Stop/CCC/Goal 等待及业务验收分别留证。真实验收
必须覆盖 Claude → supervisor；反方向成功不能替代。新任务解析当前
skill 的真实完整 release，在途原次仍保留原 controller。
离线测试不替代现场；一次成功也不能保证未来所有 UI 传输绝无失败。
能保证的合同是：未知不报成功、保留原次、恢复有界、证据不被重写。

## 新粘贴必须为单行（0.4.4）

普通消息及握手全文含任意 CR/LF/tab 或超过700 UTF-8字节时，保存完整只读正文，
再发单行 MESSAGE_REFERENCE_V2；保留全部字节和SHA。新任务使用专用
TASK_PACK_V2 单行通知并绑定完整任务包SHA；callback保持专用原文，不改成普通引用。
公共新粘贴门禁同时覆盖三条路径。旧V1四行通知仅沿原controller核收，不能重贴
或迁移原nonce。短通知原生收到只证明通知；完整正文另核读入，业务另行接受。

完整实机验收分别记录：原fence后完整 native user、真实callback原次及回执、
活跃客户端自动hook的实际调用。手动hook测试、配置存在和离线套件不能代替自动执行。
维护通过后接回用户原任务，沿原主管核未完边界，不擅自加派新研究或旧章节。

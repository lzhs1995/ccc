> Current policy (0.4.19): [non-blocking recovery](nonblocking-recovery.md) overrides historical session-wide tool/Stop blocks and old-supervisor handshake restrictions below. Installed workflow hooks are advisory; explicit transport evidence and draft protection remain.

# 执行者交付后收口：运行约束与边界

本页复用[原生投递与有界等待](verified-compose-delivery.md)。
冻结报告和原回调终态形成工作边界；通信未确认不要求执行者无限续轮。

## 封口保留原次只读入口

`cmux_executor_closeout_guard.py`（PreToolUse）与
`cmux_consensus_stop_guard.py` 复用 `executor_closeout.py`，核原任务、
执行者、workspace、报告与任务包 SHA、原 attempt 真实返回及持久锁。
报告、任务包和既有 attempt 记录冻结，禁止追加测试、改稿、扩展科研、
重复 callback 或为恢复更换 marker。主管和其他任务不受该任务封口。

严格 task-bound 的只读诊断及原 controller 零输入 reconcile 仍可执行。
诊断入口为 `scripts/cmux_callback_diagnose.py --task-pack <原绝对路径>`；
不能借它发键、生成 receipt、disarm、扫描全盘或运行任意 shell。
原 controller 的核收沿原锁追加观察/原子写 receipt，不改历史。
若原版本无此入口，保留证据交主管，不复制一个新模块进旧固定 runtime。

绑定、报告终态、原返回和锁均成立时，Stop 接受普通诚实说明，无须唯一
精确 STATUS 模板。合法等待输出 `WAITING_SUPERVISOR`、
`continue:false`、`suppressOutput:true`；不生成 receipt、不 disarm，
不授予共识或业务验收。无证据而明确声称 callback confirmed、共识通过
或研究完成时仍拦截。缺失、在途或漂移证据不能借此出口伪装终态。
原次 NO_INPUT 真实返回也不强迫执行者消耗一次新发送。

唯一零输入后继：仅当原 journal 只有 `attempt-0001.json`，其 `NO_INPUT`、
`events=[]`、task/report/attempt SHA 和身份均核符，且 receipt 与历史
`.pending.json` 均不存在时，允许直接同步运行 `rtk proxy <原Python> -B
<原required_skill目录>/scripts/cmux_bridge.py submit-completion-callback
--task-pack <原绝对路径>` 一次。不能用变量、shell包装、后台或附加命令。
只比较这条完整静态命令的 argv，允许等价的单引号、双引号及无空格中文
路径引用；先严格排除动态展开、注释、重定向和控制尾缀，再解析参数。
任务包缺少可选 `callback_command` 时从原绑定派生；显式空值、错误命令
或不同原控制器仍拒绝。引用等价不放宽 task/report/attempt 与发送预算。
`cmux_callback_no_input_successor.py` 仅作 PreToolUse 判定，不是新发送器。
原 controller 重新核实时身份、草稿、锁及最多两次 journal 预算；第二次
仍失败则交主管，不循环。已粘贴、排队、未知、第二份 attempt 或任何回执
不适用。修复封口后不改旧包、不迁移 controller、不重放已有输入。

## 原次恢复不会增加新的发送预算

先核迟到 native user，再考虑原 controller 支持的恢复分支。
首次粘贴一次，完整草稿稳定后提交一次；忙碌 Codex 明示
`tab to queue message` 且完整草稿匹配时首次直接 Tab，其他清晰
受支持状态用 Enter。Tab 和 Claude `queued_command` 都只是 pending。

自动与显式恢复共享最多一次补键，可为现场支持的 Enter 或 Tab，
意图落盘即消耗。原身份、binding/fence、完整未改草稿及记录须不变；
历史出现 UNKNOWN、压缩、排队、重连或结构变化即禁止补键。
旧 queue-only 接口不另授一次 Tab。封口诊断本身没有输入权限。
缺输入前 binding/fence 的旧任务不追补、不迁移、不重发。

## 主管负责有界核收与接续

主管经认证 active markers 有界发现原冻结报告
（`scripts/cmux_supervisor_report_guard.py`），独立审读原件并裁决。
发现、原生接收、正式 receipt、业务接受和 disarm 分别记录。
原 controller 未确认时保留未确认，不能为得到回执改报告或让执行者空转。

封口只作用于当前仍 armed 的任务，不证明 API 故障或永久停用会话。
核收并安全 disarm 后，主管沿原受保护通道给出下一动作、依赖及负责人；
有独立待办就正式派单，没有就允许待命。通知未确认不推翻原任务已成立
的裁决，也不重开旧 callback。见[核收后反馈](efficiency-and-closeout.md#核收后把结论和下一步交回执行者)。

idle Stop 只记一次持久状态并放行；显式 `executor_ready.py persist`
仅读原请求和答复，最长 300 秒，不无限每 60 秒求派。CCC 等待主管须有
期限；native Goal、CCC、真实 Stop、收到答复和业务验收分别核证。

## 握手与验证保持有界

首次挑战只含身份、固定 skill 和 pending receipt 路径；真实健康 ACK
直接复用，不把 600 秒默认预算当必须等待时长。confirmed 回执仍须核
原完整任务包 SHA。握手、业务复核、交付核收不相互增加轮次。

派单写明具体问题、必要验证、输出目录及停止条件。两 executor 只接
互相独立的工作，共享文件由一人整合。故障按既有有界恢复与 SOLO 授权
处理，保留原会话并先确认没有并发写入。

通过真实 stdin/子进程入口验证封口允许的诊断、诚实 Stop、虚假确认拒绝、
在途锁、身份/SHA 漂移及主管豁免。源码测试、完整版本安装、运行中客户端
加载、实际 hook、原生入站、Stop/CCC/Goal 及研究验收分别留证。
旧固定任务包和历史失败保持原字节，配置写入不是热加载证明。

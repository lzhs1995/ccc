# 同窗格中的既有终端标签页

已授权的原会话可以位于同一pane的不同terminal surface。主管与执行者、
两个执行者之间都可及时发送准备好的消息，不因共享pane、正常工作或Goal续轮
而拒收。原会话不需要搬移，也不因此新建或替换执行者。

普通消息和广播沿当前共享入口解析live caller与完整tree，固定workspace、
surface和pane UUID。自发自收、重复surface、非terminal、UUID/ref冲突、
指定范围外目标及绑定变化仍拒绝。广播第一次固定的目标集不会在重试时扩张。
跨workspace通信仍需既有显式maintenance授权，不能从tab位置推导权限。

正式握手的新identity-gate记录`terminal_channels_version=1`，并保存每位
参与者和workspace proof的一致性证据。每位执行者仍各有nonce与真实ACK；
validation重新检查全部terminal身份，不能只凭`side_panel=true`通过。
`side_panel=false`表示共享布局，不表示握手失败或共同使用一个原生会话。
旧gate不添加新能力，原尝试、原controller及一次输入预算保留。

输入继续使用原pane/target两把锁，串行化最后预检、粘贴和提交。
锁释放后模型可以独立工作；不持锁等待回答。Claude的真实草稿、完整原文和
原生收件校验保留；Codex只走认证的原线程native输入，不改用composer。
新建执行者仍须用户授权和原spawn双门禁。

离线回归覆盖同pane握手、每人独立ACK、广播原目标集、UUID/ref漂移、
重复参与者和真实flock争用。测试通过不等于现役进程加载或原生收件；
发布后还需用原会话必要的新信息留存输入与收件证据，不能重放历史通知。

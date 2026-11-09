# 原 caller 身份隔离验证包（仅准备，未运行）

本包验证真正 embedded TUI 的 ToolCtx→local OS spawn→同进程 registry→原 kernel-peer socket。它不安装客户端、不连接现役 cmux/Herdr、不使用真实模型账号、不重新测试180/300。

## 文件与依赖

运行代码只有 fixture.py、tool_probe.py、fixture_common.py，均仅依赖 Python 3 标准库。fixture_common.py 是原r28三个helper的14个纯函数摘录；EXTRACTION.json逐函数给原源码SHA/行号/摘录SHA。仅 prepare_native_home 去掉同模块handoff限定名，其余正文逐字复用。没有旧503闭包、observer/controller、SQLite资料、业务日志、私有报告或供应商凭据。

环境：隔离 GitHub hosted macos-15/arm64 runner；Python3；libproc.dylib；/bin/zsh。不需要 cmux、Herdr、Xcode、Rust或VPS。脚本不下载或编译任何二进制。

外部输入仅四项：

1. 新caller CI的 codex 主程序及它的真实RESULT.json；必须由Root先核GitHub run/head/attempt/API artifact id/digest/size和完整proof。
2. Root已核RESULT文件SHA，以及对应实际格式后effective源码SHA。
3. 新run ID固定42557187399 / attempt1（当前仅in_progress，尚无成功产物）；固定head=14e8870b4e032809fb5effcb0d657016f503296b，request=f5f3544f20b0a5b52e898d9dd79a4c5466f1aeffb76530d52399efbaeb5764df。必须passed=true/attempt1/无旧测试rerun/未安装。42553480280只用于明确拒绝，绝不是可用产物。旧r9 binary也不能匹配此请求。
4. 已认证的官方 codex-code-mode-host，68,543,072B，SHA416721e33af8822041dc650793512b3e58d43e0be4f3a5c6187eb4dfaad3148a。

官方host来源固定于OFFICIAL-HOST-CONTRACT.json：npm包@openai/codex 0.164.0-darwin-arm64，metadata=https://registry.npmjs.org/@openai/codex/0.164.0-darwin-arm64；archive=https://registry.npmjs.org/@openai/codex/-/codex-0.164.0-darwin-arm64.tgz；成员package/vendor/aarch64-apple-darwin/bin/codex-code-mode-host。原r28获取器核metadata dist、整archive SHA512 integrity/SHA1和唯一regular成员/bytes/SHA；Root可以原样复用该runner步骤或已有已认证host。不要为本机下载完整app，不把host签名变成改版主程序的官方签名。

## 未来执行接口

先在runner认证本包MANIFEST中每个文件；由Root填真实成功产物参数。下面只表示接口，当前没有可替代真实pin的默认值：

```text
python3 -BE /ABS/BUNDLE/fixture.py \
  --root /ABS/RUNNER_TEMP/caller-fresh-0700 \
  --binary /ABS/VERIFIED-ARTIFACT/codex-aarch64-apple-darwin \
  --host /ABS/VERIFIED-OFFICIAL/codex-code-mode-host \
  --build-result /ABS/VERIFIED-ARTIFACT/RESULT.json \
  --build-result-sha256 REAL_RESULT_SHA256 \
  --build-run REAL_SUCCESSFUL_NEW_RUN_ID \
  --reviewed-source-sha256 REAL_REVIEWED_EFFECTIVE_SOURCE_SHA256
```

root必须已经存在、为空、0700且在真实RUNNER_TEMP内；使用足够短路径使原Unix socket少于104字节。每次只能一个全新目录；失败后留原件，不覆盖、不自动重试。脚本没有git/gh、artifact选择或厂商查询逻辑；任意伪造的RESULT文件和自报hash不能代替前面的Root artifact/API核收。

## 实际执行范围与判据

复制已核主程序/host到私有package；只在私有home写两原marker和local_fixture配置。真实 --no-daemon TUI通过新PTY启动。唯一provider是本脚本的127.0.0.1随机端口确定性Responses服务，无供应商实调。初始测试文字通过argv进入全新fixture线程；终端只回答有界DSR光标查询，没有Enter、业务面板输入或原native control请求。

provider只输出真实已广告exec工具，然后一次原生V2 spawn_agent建立隔离child，再输出其exec工具。两角色分别在真实工具PID内exec自身并把CODEX_THREAD_ID覆成root；原PID/birth不变，root仍应回root SID，child必须回独立child SID。socket由内核serverpeer/PIDbirth、原capability/inode、nonce及epoch绑定。返回foreground=null、send/controller/inboxAuthority=false；独立真实foreground观察仍是root。

child额外验证错误epoch、birth、nonce与wire注入threadId四次拒绝；未登记的runner进程也必须拒绝。角色成功输出必须在同SID原生response_item调用/返回对中可见，且root/child真实task_complete均到达；code-mode的native inner callId与provider outer callId分别记录，不虚构不持久化的ExecCommandBegin/End。全部raw transcript与provider请求都只来自此私有fixture。

## 有界运行与清理

整体观察窗120秒；provider等待ready30秒、child45秒，单socket超时3秒且回复≤65536B；每条模型请求≤8MiB；每份原生history≤16MiB且只允许本fixture两SID。发生未知/错误保存FAILURE，不补输入、不重发工具，不启动另一会话绕过。

只清理本脚本Popen拥有的同PID/birth TUI：TERM后等10秒，仍同代才KILL后等5秒；不发进程组信号、不查杀任意进程。关闭本PTY及本loopback provider，worker join最多3秒。CLEANUP只声称已回收本TUI，不把未单独观察的全部helper进程树当已验；GitHub隔离job仍是最终容器边界。此有限策略与两个脚本相符，不能用于现役进程。

本包只验证unified_exec实际root/child工具来源。官方hooks/engine/command_runner.rs直接spawn，未经过本r3 unified_exec registry；即使本fixture全部通过，也不能宣称自动hook身份或root inbox自动加载已通过。该缺口由后续独立增量处理，原sender/controller保持。

## 当前状态

PACKAGE_PREPARED_RUNTIME_NOT_RUN。这里只做纯Python AST/import/提取一致性和构造数据的错误参数拒绝，既不是客户端启动，也不是ToolCtx/模型/供应商测试。run42557187399来自Root的实际新CI观察；artifact/effective SHA未由本包猜测。源包待独审后由Root封装发布；现役hook、真实收件/生产采用继续未验。

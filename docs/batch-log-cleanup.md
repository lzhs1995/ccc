# CCC 批次日志回收

现有 cmux janitor 仅处理 `.sb-*` 和未发布的 staging 快照。CCC 的
`~/Library/Application Support/cmux-codex-continue/workspace-batches` 不在其范围内。
批次 `complete` 表示首任务确认，不表示原生进程退出。

本工具只允许删除每个批次 `native-db` 下的 `logs_<数字>.sqlite` 及其
`-wal`、`-shm`，覆盖旧 flat、新 template/slots 布局。不会删除 state、queue、
goals、memories、thread_history、job、claim、Hook、会话正文或输入消费账本。
被清日志是历史诊断资料；删除不可撤回，不使用快照隔离区，也不会复制大型数据库。

默认生成预览清单。人工处置必须提供该清单 SHA256，清单有效期 30 分钟；
预览仍不等于删除授权。每批处置必须取得原 worker/config 锁、重新读取配置、
cmux 全拓扑、进程表和全机打开文件；持久化删除意图后再次检查。
工作区或 surface 存活、记录 PID 存活（含不能确定的 PID 复用）、进程引用、
打开文件、未知配置归属、非终态、近期使用、查询异常均拒绝。
目录和文件必须属于当前用户，拒符号链接、硬链接和他人可写文件；unlink 使用
逐层 O_NOFOLLOW 打开的目录 FD，并再次核对父链及文件身份。

自动模式由单独 launchd 任务 `com.<当前用户名>.cmux-ccc-batch-logs`（可用 `CCC_LABEL_PREFIX` 覆盖前缀） 每 30 分钟执行，
安装包默认 `enabled=false`。启用需要明确的 `--install --enable`。
正常闲置期限 7 天；可用空间不足 10 GiB 时改为 24 小时，所有活跃排除仍有效。
每轮最多 32 批，按日志占用降序选择；只保留最近 32 个小型计划/回执文件。
既有 janitor 的 MODE、DISABLED、guard 故障及超过 5 分钟的 guard 状态都会阻止
自动删除。新组件不修改既有快照清理脚本或其 3 小时隔离期限。

运行示例（仓库根目录，Python 3.10+；本机使用指定的 3.14 路径）：

```sh
rtk proxy /opt/homebrew/opt/python@3.14/bin/python3.14 -B tools/batch_log_cleanup.py --output /absolute/path/preview.json
rtk proxy /opt/homebrew/opt/python@3.14/bin/python3.14 -B tools/batch_log_cleanup.py --apply-plan /absolute/path/preview.json --approve-sha256 <清单SHA256> --output /absolute/path/receipt.jsonl
rtk proxy /opt/homebrew/opt/python@3.14/bin/python3.14 -B tools/install_batch_log_cleanup.py --output /absolute/path/deployment-bundle
```

回执记录删除前后的实际可用空间和成功 unlink 字节。APFS clone、快照以及其他进程
写盘会影响释放量；文件的分配占用不能承诺为 `df` 的净增量。中断或部分删除
根据逐批 intent 与现存文件复核，不把整批预计值当成功释放量。

本次工具不证明待机启动性能，也不改动待机实现。清理缓解空间压力后继续完成
已获授权的待机管理器、点击激活和真实 UI 50 路验收。

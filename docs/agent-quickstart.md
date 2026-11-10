# Agent quick start / Agent 快速開始

[English overview](../README.md) · [繁體中文總覽](../README.zh-TW.md) · [Shared instructions](../AGENTS.md)

## English

Give any coding assistant this starting instruction:

> Read AGENTS.md and docs/agent-quickstart.md in this checkout. Inspect the
> actual parser and help for any command you plan to use. Work on TASK_DESCRIPTION
> within the requested scope. Preserve original sessions and uncertain attempts;
> do not install services or send terminal input merely to verify documentation.

`TASK_DESCRIPTION` is a placeholder. `AGENTS.md` is the common source of
instructions; `CLAUDE.md`, `GEMINI.md` and `llms.txt` are navigation aids. If your
client does not load them automatically, ask it to read them explicitly.
A documentation entry does not make that client a supported recovery target.

From the repository root, inspect help without starting the watcher:

```sh
python3 -B cmux_codex_watch.py --help
python3 -B bin/cmux-stack --help
```

The two commands above were run successfully for the documentation update.
The following entry points are source/parser references, not a claim of live
execution in that update:

| Need | Existing entry | Effect to understand |
| --- | --- | --- |
| Published watcher state | `python3 -B cmux_codex_watch.py status` | Diagnostics may contain private session metadata |
| Hook/context evidence | `hook-audit --json`, `context-audit --json` under the watcher entry | Inspect results; these are not repair commands |
| Component status | `python3 -B bin/cmux-stack status --json` | Reports independent components; absent components remain absent |
| Supervisor | `python3 -B cmux_codex_watch.py tui` | Interactive controls can enroll, create batches or interrupt |
| Runtime install | `python3 -B cmux_codex_watch.py install` | Changes runtime and launchd; follow the operations guide |

There is no Herdr subcommand in the documented parser. Do not invent a
`ccc herdr`, `ccc restore-all` or universal client-adapter command. The proposed
180-second warning / 300-second intervention policy is not the current main
runtime: its Claude context stall is 180 seconds, absolute timeout 900 seconds,
and session-audit log stall is a separate 300-second diagnostic.

For a code change, name the affected module and use its isolated checks. For a
documentation change, check links, parser examples, language parity and private
data. Do not rerun native acceptance tools as a documentation check: some
create real terminals, submit prompts or affect services. Report help execution,
source inspection, isolated tests and live recovery separately.

## 繁體中文

可以給任何程式設計助理這段起始指示：

> 閱讀本工作目錄中的 AGENTS.md 與 docs/agent-quickstart.md。使用命令前，
> 先核對實際參數解析器與 help。依照本次授權範圍處理 TASK_DESCRIPTION。
> 保留原會話與結果不明的操作；不要為了驗證文件而安裝服務或輸入終端命令。

`TASK_DESCRIPTION` 是占位符。`AGENTS.md` 是共用指示，其他入口文件只提供導覽。
若客戶端未自動讀取，請明確要求它閱讀。存在文件入口，不表示該客戶端已成為
支援的恢復目標。

在倉庫根目錄執行上面的兩個 `--help` 命令即可核對入口，不會啟動守衛。
本次文件更新只實際執行這兩項 help；表格中的狀態、稽核、面板與安裝命令，
是依原始碼與參數解析器核對的用法，不是本次真實執行回執。

本版本沒有 Herdr 子命令，也沒有一條能恢復所有代理程式的通用命令。
180 秒警示／300 秒介入仍是整合目標；目前的 180 秒 Claude 壓縮停滯、
900 秒整體逾時，以及 300 秒日誌稽核，是不同機制。

修改程式時，找出受影響模組並使用相關隔離檢查；修改文件時，核對連結、
命令、雙語一致性與私人資料即可。不要把建立真實終端或送出提示詞的原生
驗收工具當文件測試。429／503 不會取消原 SID、輸入與投遞保護；UNKNOWN
必須保留原次證據，不能靠清除紀錄或另建會話繞過。

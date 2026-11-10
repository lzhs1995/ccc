<picture>
  <source srcset="docs/assets/ccc-banner.svg" type="image/svg+xml">
  <img src="docs/assets/ccc-banner.png" alt="CCC — 終端代理程式的受保護恢復管理" width="900">
</picture>

# CCC

[English](README.md) · [繁體中文](README.zh-TW.md) · [Agent 快速開始](docs/agent-quickstart.md)

CCC 是 macOS 上的終端程式設計代理恢復管理器。目前透過 **cmux** 觀察會話，
在使用者明確啟用後，對支援的 Codex 與 Claude Code 狀態提供受保護的續跑，
並核對原會話身分、輸入狀態與投遞紀錄。

專案方向是 **cmux 管理外層工作區與終端，Herdr 管理內層會話與分割窗格**。
本頁對應版本尚未包含 Herdr 執行階段整合；安裝這個倉庫，並不表示重新開機後
就能自動找回所有巢狀會話。

## 目前可以做什麼

下表對應原始碼 `79cc892`，不是所有客戶端版本或所有命令皆已在你的電腦實測的聲明。

| 範圍 | 現有功能 | 使用邊界 |
| --- | --- | --- |
| cmux + Codex | 觀察、明確登記、受保護續跑，以及支援狀態下的原生 Goal 恢復 | 必須核對原會話、程序與當前輸入／回合證據；不另建替代會話 |
| cmux + Claude Code | 具 Hook 證據的 Stop 處理、觀察與續跑 | 預設關閉；須明確啟用，並核對 Hook 與就緒狀態 |
| 監督面板與稽核 | 終端狀態面板、會話稽核、各元件自行提供的健康狀態 | 程序存在或畫面可讀，不等於已可安全續跑 |
| 選用工具 | 設定檔管理器、先隔離再清理的 janitor、網路守衛 | 各自有設定與作用範圍，請閱讀對應說明 |
| Herdr 內層窗格 | 整合方向 | 本版本沒有 Herdr 執行階段介面或 Herdr CLI 子命令 |
| 其他程式設計代理 CLI | 共用文件入口 | 文件不會自動實作或驗證其他 CLI 的執行階段支援 |

整合工作另見[草稿 PR #50](https://github.com/lzhs1995/ccc/pull/50)與
[實驗性 cmux 發佈分支](https://github.com/lzhs1995/ccc/tree/ccc/cmux-release-package-20261110-r1)。
使用前應核對其實際程式與證據。尚未合併的 PR、實驗性建置或隔離測試成功，
都不代表 `main` 已支援、現役客戶端已載入，或實際重新開機後已完整恢復。

## 從 CLI 開始

需求：**macOS 13+**、**Python 3.10+** 與 [cmux](https://cmuxterm.app/)。
核心使用 Python 標準函式庫。選用的 access-check 模式需要 Python 3.11+，
不屬於這份快速開始的必要步驟。

```sh
git clone https://github.com/lzhs1995/ccc.git
cd ccc
python3 -B cmux_codex_watch.py --help
python3 -B bin/cmux-stack --help
```

這次文件整理已實際核對上述兩個 help 命令。下列範例與參數解析器一致，
但本次沒有執行安裝或真實業務續跑。

當你確定要安裝或更新本機守衛器時，先閱讀[操作與發佈手冊](docs/operations.md)，再執行：

```sh
python3 -B cmux_codex_watch.py install
python3 -B cmux_codex_watch.py status
```

`install` 會變更本機執行套件與 LaunchAgent。全新設定預設為 `dry-run`，
Claude 續跑預設關閉；既有設定會保留。`start` 只重新啟動已安裝程式，
不會安裝目前工作目錄中的修改。安裝別名為 `ccc` 與 `cmux-codex-continue`；
上面的原始碼呼叫方式不依賴別名是否位於 `PATH`。

要授權續跑，請將 `FULL_SURFACE_UUID` 換成**指定既有 Codex 終端的完整 UUID**，
檢查狀態後才啟用：

```sh
python3 -B cmux_codex_watch.py track-surface FULL_SURFACE_UUID
python3 -B cmux_codex_watch.py status
python3 -B cmux_codex_watch.py arm
```

`arm` 會修改全域模式並解除全域暫停；請先檢查**所有既有登記**。
各目標的個別暫停與身分／輸入保護仍然有效。Claude Code 的既有登記選項是
`track-surface FULL_SURFACE_UUID --allow-non-codex`，還必須有
`claude_enabled: true` 及已驗證的 Hook／就緒證據。這個選項不是任意代理 CLI 的支援開關。
詳見[登記與觀察說明](docs/registration-observation.md)。

檢查既有安裝，而不要求它提交續跑：

```sh
python3 -B cmux_codex_watch.py status
python3 -B cmux_codex_watch.py hook-audit --json
python3 -B cmux_codex_watch.py context-audit --json
python3 -B bin/cmux-stack status --json
```

使用 `python3 -B cmux_codex_watch.py tui` 開啟監督面板。開啟面板本身不會登記工作區；
其中的登記、批次啟動與中斷控制會產生真實作用。完整按鍵與操作保留在
[進階操作參考](docs/advanced-operations.md)。

## 恢復邊界

- **429／503：**完整、當前且可重試的原生錯誤提示，才可能進入受保護恢復路徑。
  引用的錯誤文字、服務商故障、草稿、選單、排隊輸入、仍在執行的回合或身分缺失，
  都不構成輸入許可。CCC 不保證上游可用，也不能修復帳號授權失敗。
- **原身分：**保留原 session ID（SID）、程序建立代次、工作區／終端 UUID 與投遞紀錄。
  標題、可重用的終端編號與畫面文字不能證明歸屬。
- **投遞結果不明：**保留原次操作並核對其證據，不盲目重貼文字、補按 Enter、清空紀錄
  或另建替代會話。後續重試仍須通過原會話與投遞保護。
- **重新開機：**守衛已登記、程式已安裝或版面已儲存，都不等於完整恢復成功。
  必須在正常且已獲授權的重新啟動後，核對實際載入程式、儲存身分與原會話。

### 180 秒與 300 秒的意思

整合方向採用 **180 秒警示／300 秒受保護介入**的邊界。這是待整合目標，
不是本版本已實作的統一計時規則，也不是無條件取消或重試。

目前原始碼使用的是不同、各自獨立的時間判斷：

| 計時項目 | 本版本含義 |
| --- | --- |
| `claude_context_stall_sec = 180` | Claude 壓縮內容連續無進展；整體內容處理逾時為 900 秒 |
| `claude_context_enforcement` | 預設關閉；觀察與強制限制是兩回事 |
| 會話稽核的 `LOG_STALL_SEC = 300` | 守衛日誌沒有繼續更新；這是診斷門檻，不是會話恢復期限 |

請對照[守衛原始碼](cmux_codex_watch.py)與[稽核原始碼](ccc_session_audit.py)。
這些時間值不會授權停止程序或重送結果不明的請求；不能因閱讀本頁，就宣稱
180／300 整合行為已生效。

## 文件與程式設計代理

- [共用 Agent 指示](AGENTS.md)與[Agent 快速開始](docs/agent-quickstart.md)
- [操作與發佈](docs/operations.md)
- [登記與觀察](docs/registration-observation.md)
- [Codex Hooks](docs/codex-hooks.md)
- [選用網路守衛](docs/network-guard.md)
- [進階操作](docs/advanced-operations.md)：完整保留原有英文／簡體中文詳細參考
- [變更紀錄](CHANGELOG.md) · [工具用文件索引](llms.txt)

`AGENTS.md`、`CLAUDE.md`、`GEMINI.md` 與 `llms.txt` 都指向同一套文件。
是否自動讀取取決於客戶端；需要時請明確要求它閱讀 `AGENTS.md`。
這些文件不會安裝 CCC，也不會新增執行階段支援。

不要把憑證、真實設定檔、業務轉錄或執行日誌加入提交與 issue 附件。
狀態輸出可能含會話資訊；公開範例請使用占位符或合成身分。
[設定範例](config.example.json)與[設定檔範本](templates/_template.example.json)
是開始設定的參考，不是正式憑證。

## 授權

[MIT](LICENSE)。

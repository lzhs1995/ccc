此入口只在隔離的 GitHub macOS 26 runner 驗證已保存的 Release App，不重新編譯、不在使用者電腦安裝。原來源是 run `42808450553`／attempt `1`／head `03540ebd2f4ff0c12b34eda5b2841623ce5b14ea`：build 與簽名包完成，但 runtime 在首個 capabilities 命令之前的 peer 身份檢查失敗。既有紀錄沒有保存該檢查的兩次值，尚不能判定是程序身份或 socket 屬性變化。

`RETAINED-SOURCE.json` 固定原 App artifact `13766044692` 的 API 身份、外 ZIP 大小及 digest、內 ZIP 大小及 SHA256，並固定原小型證據和 build/package/failure 來源鏈。驗證 job 先讀取並核對這些原件，再在全新 runner 帳戶執行完整原驗收：簽名與完整 bundle、LaunchServices 環境、舊與新 CLI、CCC viewport、同一路徑、兩代 GUI、原 workspace/surface UUID、自然快照寫入與清理。

`source_run` 永久指向原 build；`validation_run` 指向獨立的新 job。原 run 的 failure 不會改寫。只有新 runtime 與清理全部成功，才形成新驗證結果；完成後仍須核對 GitHub 新 run 的終態。這不代表使用者業務會話已採用，也不代表 Herdr 原生恢復或實體斷電已驗證。

`release_paired.py` 保留原程序與 socket 全部身份判斷，失敗時新增前後值供診斷。沒有忽略 timestamp、接受新 inode 或自動重試。UNKNOWN 和原始 partial 會保留。`evidence_revalidation.py` 匯出有大小上限的小型證據，包含原 failure 及新驗證回執，排除完整 App ZIP。GH token 不會進入 GUI 子程序的環境。

新 workflow 只匹配 `ccc/cmux-release-runtime-revalidation-20261110-r1`。舊建置 workflow 仍只匹配 `ccc/cmux-release-package-20261110-r3`，新分支不會觸發舊建置。

This entry verifies the retained Release App on an isolated GitHub macOS 26 runner. It does not rebuild or install anything on a user's machine. Source run `42808450553`, attempt `1`, head `03540ebd2f4ff0c12b34eda5b2841623ce5b14ea` completed its build and signed package, then failed its runtime peer check before the first capabilities command. The original record does not distinguish a process change from a socket metadata change.

The pinned source contract binds App artifact `13766044692`, its API identity, both archive hashes and sizes, the original small evidence, and the build/package/failure chain. A separate validation run checks the complete original runtime contract, including signatures, both CLI generations, stable socket routing, two GUI generations, original workspace and surface identities, natural snapshot writes, viewport reads, and cleanup. The original failure remains unchanged.

Peer diagnostics retain every original identity predicate and add the actual before/after values. There is no timestamp exception, replacement-inode acceptance, or automatic retry. The new evidence export preserves both run contexts and the original failure while excluding full App archives. A successful validation still requires the final GitHub run check and does not establish adoption by existing business sessions.

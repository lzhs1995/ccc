此入口在隔離的 GitHub macOS 26 runner 驗證已保存的 Release App，不重新編譯或在使用者電腦安裝。原來源是 run `42808450553`／attempt `1`／head `03540ebd2f4ff0c12b34eda5b2841623ce5b14ea`：build 與簽名包完成，runtime 的 peer 檢查失敗。第一個獨立驗證 run `42844611650`／attempt `1` 也失敗，新增的前後觀測證明該次只有 socket ctime 改變。兩次失敗紀錄均保留。

`RETAINED-SOURCE.json` 固定原 App artifact `13766044692` 的 API 身份、外 ZIP 大小及 digest、內 ZIP 大小及 SHA256，並固定原小型證據和 build/package/failure 來源鏈。驗證 job 先讀取並核對這些原件，再在全新 runner 帳戶執行完整原驗收：簽名與完整 bundle、LaunchServices 環境、舊與新 CLI、CCC viewport、同一路徑、兩代 GUI、原 workspace/surface UUID、自然快照寫入與清理。

`source_run` 永久指向原 build；`validation_run` 指向獨立的新 job。原 run 的 failure 不會改寫。只有新 runtime 與清理全部成功，才形成新驗證結果；完成後仍須核對 GitHub 新 run 的終態。這不代表使用者業務會話已採用，也不代表 Herdr 原生恢復或實體斷電已驗證。

固定版本的 cmux 會透過私有硬連結重申 socket 權限；macOS 實測確認相同 mode 的 chmod 仍會改變 ctime。這能產生同型差異，尚不能指認失敗 run 的唯一觸發者。r2 只在 `peer()` 的兩次 socket 比較中排除 ctime，仍記錄全部九個觀測欄位；其餘八欄、已驗證的 PID、程序出生時間、執行檔路徑與完整 hash，以及當前類型與權限檢查均保留。一般檔案仍使用原九欄 `file_identity`。

其餘八欄仍是保守狀態門檻：若觀測恰好碰到硬連結維護期間的 nlink 變化，仍會拒絕。本次未新增 socket birthtime，也不宣稱容忍任意 metadata 競態。UNKNOWN、原始 partial 與原拒絕順序保留，沒有自動重試。`evidence_revalidation.py` 匯出有大小上限的小型證據，包含原 failure 及新驗證回執，排除完整 App ZIP。GH token 不會進入 GUI 子程序的環境。

新 workflow 只匹配 `ccc/cmux-release-runtime-revalidation-20261111-r4`，只接受新 run 的 attempt `1`。舊建置 workflow 仍只匹配 `ccc/cmux-release-package-20261110-r3`，新分支不會觸發舊建置。

This entry verifies the retained Release App on an isolated GitHub macOS 26 runner without rebuilding or installing on a user's machine. Source run `42808450553`, attempt `1`, head `03540ebd2f4ff0c12b34eda5b2841623ce5b14ea` completed its build and signed package, then failed its runtime peer check. The first independent validation run `42844611650`, attempt `1`, also failed; its added observations showed only socket ctime changed. Both failures remain unchanged.

The pinned source contract binds App artifact `13766044692`, its API identity, both archive hashes and sizes, the original small evidence, and the build/package/failure chain. A separate validation run checks the complete original runtime contract, including signatures, both CLI generations, stable socket routing, two GUI generations, original workspace and surface identities, natural snapshot writes, viewport reads, and cleanup. The original failure remains unchanged.

The pinned cmux source reapplies socket permissions through a private hard link. Native macOS observations confirm that same-mode chmod changes ctime; this explains how the observed difference can occur without identifying the failed run's unique actor. Revision r2 omits ctime only from the two socket comparisons in `peer()`. Diagnostics keep all nine fields. The other eight fields, authenticated PID, process birth, executable path and full hash, and current socket type and permissions remain checked. Regular files retain the original nine-field `file_identity`.

These eight socket fields remain conservative state checks: a transient nlink change during hard-link maintenance can still refuse an observation. This revision adds no socket birthtime and does not accept arbitrary metadata races. UNKNOWN records, partial results and refusal order are preserved, with no automatic retry. The evidence export binds distinct source and validation runs and excludes full App archives. A successful validation still requires the final GitHub run check and does not establish adoption by existing business sessions. The new branch `ccc/cmux-release-runtime-revalidation-20261111-r4` runs only the runtime workflow, with validation attempt `1`.

第二次驗證 run `42863486833`／attempt `1` 的 failure 仍保留。r3 的目的僅是記錄首次 preverify 失敗的 inventory 差異：使用已核驗的原 PACKAGE-MANIFEST 與同次實際觀測，留下有上限的 expected／actual 數量及 added／removed／changed 診斷。原兩段 socket ctime 修正、全部驗收門檻與主錯誤保持；診斷寫入失敗也不能掩蓋原錯誤。診斷 run 不是已修復或 runtime 通過的證明。

r3 驗證源以已審公開 main `a369955406018db2504adb9a39afe9c5265eca75` 為父提交，疊加本驗證包；原 App 的 source_run、來源提交與 artifact 仍獨立固定，舊驗證紀錄不變。

Validation run `42863486833`, attempt `1`, remains a recorded failure. Revision r3 only adds bounded inventory diagnostics to the first preverification failure, comparing the authenticated original PACKAGE-MANIFEST with the actual observation from that same attempt. Expected and actual counts and added, removed and changed members help diagnose the refusal. Both socket ctime fixes, all acceptance gates and the original error remain; diagnostic write failure cannot replace that error. A diagnostic run does not establish a fix or a runtime pass.

The r3 validation source overlays the reviewed public main commit `a369955406018db2504adb9a39afe9c5265eca75`. The original App source run, source commit and artifact remain independently pinned; previous validation records remain unchanged.

r3 驗證 run `42873689697`／attempt `1` 的失敗與原觀測保留。r4 將固定 Release App 支援的 `--cmux-disable-bundle-icon-persistence` 納入公開 `release_paired.launch`：每次配對啟動、包括前後兩代 GUI，都透過 `/usr/bin/open -a /Applications/cmux.app --args --cmux-disable-bundle-icon-persistence` 傳入；啟動回執記錄完整 argv 與策略。這不是僅在 CI 命令加參數，原 LaunchServices sealed environment、完整 bundle inventory、簽名、執行檔 hash、CLI／UUID／自然快照與清理驗收全部保留，沒有移除 Icon\r、放寬完整性檢查或無參重試。

此交付要求使用上述配對啟動入口。裸 Finder／不帶參數的新啟動會由上游重設持久化 default，未被本策略覆蓋。完整正常自動啟動接線仍未完成；本版不宣稱已完成使用者自動恢復、業務採用或真實 runtime 驗證。原 App、source run `42808450553`／attempt `1`、artifact `13766044692`、既有失敗與回執不變；r4 仍以已審公開 main `a369955406018db2504adb9a39afe9c5265eca75` 為父提交。

The failure and original observations from r3 run `42873689697`, attempt `1`, remain preserved. Revision r4 makes the fixed Release App's supported `--cmux-disable-bundle-icon-persistence` argument part of the public `release_paired.launch` entry. Every paired launch, including both GUI generations, uses `/usr/bin/open -a /Applications/cmux.app --args --cmux-disable-bundle-icon-persistence`; its receipt records the exact argv and policy. This applies to the shared launch entry, beyond the CI command. The sealed LaunchServices environment, complete bundle inventory, signatures, executable hashes, CLI and UUID checks, natural snapshots, and cleanup remain strict. There is no Icon\r removal, integrity exception, or fallback launch without the argument.

Use the paired launch entry for this policy. A fresh bare Finder launch without the argument resets the upstream persistence default and is outside this coverage. Complete normal automatic startup wiring remains unfinished. This revision does not establish user automatic recovery, business adoption, or a real runtime pass. The original App, source run `42808450553` attempt `1`, artifact `13766044692`, and all existing failures and receipts remain unchanged. The r4 overlay retains reviewed public main `a369955406018db2504adb9a39afe9c5265eca75` as its parent.

# Natural startup and original session restore

This integration prepares a new cmux Release dependency for CCC. It preserves
the accepted Herdr restore fix and adds a startup compatibility repair. It is
not the default CCC installer or proof that every business session has recovered.

The earlier package required `--cmux-disable-bundle-icon-persistence` to keep
Finder icon persistence from modifying its sealed application bundle. Upstream
`SessionRestorePolicy` treated that configuration flag as an explicit open
request and skipped session restore. This change recognizes that exact flag and
adds a signed package option, `CMUXDisableBundleIconPersistence: true`, so ordinary
LaunchServices launches require no extra arguments. The main app and the Dock
plugin both read the app's metadata. Builds without the option keep upstream
icon behavior; displayed app and Dock icons remain available.

## Source and validation

The fixed upstream commit is `2e109a1739d320fe36220afb3e7775b59c1acd47`.
The accepted base, including the earlier Herdr changes, remains tree
`40e8ea03ce1435f11e90b35f98d554a13b9b8685`.

| Stage | Required tree |
| --- | --- |
| New restore tests against accepted production code | `2ac402069e1c937b23be75126c0fdb663f5905cd` |
| Complete startup fix and new icon policy tests | `f0e2d0f26d1d5dde021ef2729aeb3f987993fe0a` |

The `native` job compiles the actual app-host test product. Eleven restore tests
use an identical execution command before and after the fix: the two relevant
assertions must fail before the fix, then all eleven must pass. The new signed
icon feature has a separate eleven-test invocation after its APIs exist; no
pre-fix compilation failure counts as a regression result. The original Herdr
regression, boundary, binding and package cases also run on the new tree.

The dependent `release` job authenticates the original native artifact from the
same run, attempt and preparation commit. It reconstructs the same source tree,
builds Release once, signs the complete package and retains a new inventory and
archive pin. The original source/test pin remains evidence for its original base;
it is never rewritten to claim that the old binary contains this fix.

The runtime acceptance uses a disposable GitHub macOS account. It performs two
ordinary `/usr/bin/open -a /Applications/cmux.app` launches, requires the saved
workspace and surface UUIDs to return, verifies a fresh natural save, checks the
full bundle and signatures, and cleans up its owned GUI process. The full App
stays on GitHub; the evidence archive is separate.

## Agent entry points

- Read `native/contract.json` for source identities and the exact test sets.
- Read `package/source_contract.py` for the preserved base and current-source
  admission; unresolved pins must reject execution.
- Read the workflow alongside this directory for job ordering, budgets and
  artifact handoff. The native job is capped at 350 minutes; Release at 240
  minutes, including a single 180-minute compile budget.
- Treat native test success, a signed archive, runtime recovery, client adoption
  and recovery of real agent work as separate results. Inspect their original
  receipts before describing a stage as complete.

These commands and scripts do not authorize installing or restarting a user's
client. Original sessions, controllers, delivery attempts, unknown outcomes,
drafts and budgets remain part of the CCC adoption contract. A disposable
process kill is not a physical power-loss test. Local nine-session Herdr recovery,
CCC retry/Goal behavior and actual agent collaboration still need their own
acceptance evidence.

## 繁體中文

這是 CCC 所需的 cmux Release 相容性修補與驗證流程，不是預設 CCC 安裝器。
原本用來保護 App 完整性的圖示設定參數，被 cmux 誤判為明確開啟請求，導致跳過
會話恢復。修補會辨認這個精確參數，並在新套件的簽名中加入布林設定，讓普通啟動
不必再帶參數。主程式與 Dock 外掛都讀取 App 的設定；沒有此設定的官方套件維持原行為。

原 Herdr 修補與證據保留。原生工作先驗證同一組十一項恢復測試由預期失敗轉為通過，
再執行十一項圖示設定測試及既有 Herdr 相鄰測試。Release 工作只接受同次原生成功
證據，重新建置、簽名並驗證兩次普通啟動、原 UUID、自然保存及完整 App。

測試成功、套件完成、實際恢復與本機採用必須分開回報。流程不會自行重啟現役任務；
原九個 Herdr 會話、CCC 續跑與 Goal／投遞不明防重、雙向協作及實體斷電恢復，仍須
各自完成實際驗收。

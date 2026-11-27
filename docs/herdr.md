# Multi-Herdr guard · 多实例 Herdr 守卫 · 多實例 Herdr 守衛

## English

CCC can follow several Herdr servers inside cmux on macOS. The guarded input
paths support Codex and Claude Code. The native reader checks Herdr 0.9.5
protocol 22, the private socket peer, cmux placement, the foreground process
and its selected conversation. A prompt requires a current recoverable failure,
an eligible composer and authorization from CCC. Unknown delivery stays in
the original ledger. A provider can continue refusing subsequent turns.

Run these commands from the checkout with Python 3.14. The default first
installation uses a separate local state directory and reads the default
and named Herdr endpoints. The parent of the instance path must exist.

```sh
instance="$HOME/.ccc-herdr-observe"
python3 -B integrations/herdr/manage.py install --instance "$instance"
python3 -B integrations/herdr/manage.py verify --instance "$instance"
python3 -B integrations/herdr/manage.py once --instance "$instance"
```

Use `--herdr`, `--cmux` and `--discovery-root` to select nonstandard
paths. The optional `--protected-surface UUID` and `--protected-pid PID`
exclude a specific surface or process for this instance.

For a first input-enabled installation, select the configuration actually read
by the direct CCC watcher. It must already be armed, unpaused and have the
`discovery_excluded_surface_ids` list. The inner guard uses this configuration,
the original provider database and the lock at `herdr-observer/state/observer.lock`.
An existing guard state requires its owning controller's handoff. A fresh
installer will exit when that state exists. Register and authorize the intended
workspaces according to the direct watcher's policy.

```sh
instance="$HOME/.ccc-herdr-guard"
control="$HOME/Library/Application Support/cmux-codex-continue/config.json"
python3 -B integrations/herdr/manage.py install --instance "$instance" --arm --control "$control"
python3 -B integrations/herdr/manage.py verify --instance "$instance"
python3 -B integrations/herdr/manage.py serve --instance "$instance"
```

`serve` stays in the foreground. End that command with Ctrl-C and deactivate
the selected release with the following command. The manager refuses rollback
while its service holds the lock. Pending attempts, provider budgets and snapshots
stay on disk. Rollback of a first installation deactivates its selected release.

```sh
python3 -B integrations/herdr/manage.py rollback --instance "$instance"
```

The packaged guard includes native selection, turn inspection, intent
persistence, provider budgeting, prompt submission and receipt handling.
The direct cmux installation retains its own command and release. The empty-fleet
test executes a cycle in each mode, checks lock contention, detects a modified
dependency and confirms pending attempts survive rollback. Live agent delivery
and reboot recovery require separate acceptance.

```sh
python3 -B -m unittest discover -s tests -p test_public_herdr_install.py -v
python3 -B scripts/check_public_content.py --export
```

## 简体中文

在 macOS 仓库根目录使用 Python 3.14。上方第一组命令完成观察模式安装、校验和一轮运行。
守卫扫描默认与命名 Herdr 实例，通过私有 socket 对端、cmux 位置、前台进程和当前原生
会话核对 Codex、Claude Code 面板。原生读取器对应 Herdr 0.9.5、协议 22。

启用自动续跑时使用第二组命令，将 `control` 指向外层 CCC 实际读取的配置。
该配置须已启用、处于未暂停状态，且包含 `discovery_excluded_surface_ids` 列表。
每次输入前按原会话、失败轮次、草稿和授权重新检查；投递不明时保留原记录。
已有守卫数据目录由原主控办理交接。前台运行结束后执行第三组回滚命令，停用该版本，
保留原账本和预算。隔离测试覆盖空实例的完整守卫循环。真实收件及重启恢复另行验收。

## 繁體中文

在 macOS 倉庫根目錄使用 Python 3.14。第一組指令完成觀察模式安裝、驗證及單輪執行。
守衛搜尋預設與命名的 Herdr 實例，經由私有 socket 對端、cmux 位置、前景程序及目前
選取的原生會話核對 Codex、Claude Code 面板。原生讀取器對應 Herdr 0.9.5、協定 22。

啟用自動續跑時使用第二組指令，將 `control` 指向外層 CCC 實際讀取的設定。
該設定須已啟用、處於未暫停狀態，且包含 `discovery_excluded_surface_ids` 清單。
每次輸入前依原會話、失敗輪次、草稿及授權重新核對；投遞結果不明時保留原紀錄。
已有守衛資料目錄由原主控辦理交接。結束前景服務後執行第三組回復指令，停用該版本，
保留原帳本及預算。隔離測試涵蓋空實例的完整守衛循環。真實收件及重啟恢復另行驗收。

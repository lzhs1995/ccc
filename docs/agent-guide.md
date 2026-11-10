# Agent operator guide / Agent 操作指南

For Codex, Claude Code and other agents acting as CCC operators. Reading this
guide does not grant permission to type into someone else's terminal.
Automatic recovery targets in the public installer are Codex and Claude Code in
direct cmux surfaces; see [Herdr status](herdr.md) for nested panels.

## 1. Inspect the checkout and installation

From the repository root:

```sh
git rev-parse HEAD
python3 cmux_codex_watch.py --help
python3 cmux_codex_watch.py status
python3 cmux_codex_watch.py list
python3 bin/cmux-stack status --json
```

Watcher `status` prints JSON; it does not take a `--json` flag. The stack command
does. Record the source revision and installed runtime. Missing data means unknown,
not an empty healthy fleet. Do not install to make a read-only check succeed.

## 2. Understand side effects

| Command | Effect |
| --- | --- |
| `python3 cmux_codex_watch.py --help` | Show supported arguments |
| `python3 cmux_codex_watch.py status` | Read published status and age |
| `python3 cmux_codex_watch.py list` | List configured targets |
| `python3 bin/cmux-stack status --json` | Project component status |
| `python3 cmux_codex_watch.py install` | Write files and load/replace the CCC LaunchAgent |
| `ccc track-surface UUID --allow-non-codex` | Change enrollment for that surface |
| `ccc arm` / `ccc dry-run` | Enable sending / return to dry-run |
| `ccc start` | Restart the installed CCC service; does not adopt source edits |
| `ccc tui` | Open the interactive supervisor; controls can change state |

Installation, enrollment, arming and restart are distinct. Preserve the user's
authorization and no-restart preferences. A fresh install defaults to dry-run;
reinstalling an existing setup retains its configuration.

## 3. Establish the original target

Record full workspace/surface UUIDs, native session ID, process generation and
current task. Do not choose by short reference, title, newest file or focus.
For Claude, validate Hook ownership/readiness. For Codex, validate the selected
native session, failed turn and Goal state where relevant.

Use the intended target's existing controller. Keep pauses, exclusions,
completion latches, drafts, questions and pending input intact. An outer Herdr
surface does not identify a selected inner panel.

## 4. Diagnose why a task did not continue

- **Working or reconnecting:** do not stack continuation prompts.
- **Draft or question:** preserve pending input.
- **Completed or paused task:** do not reopen it as an error recovery.
- **Unbound/stale identity:** improve observation; do not bypass identity checks.
- **Retryable native failure:** let the authorized path act and verify its receipt.
- **Unknown delivery:** reconcile the original attempt; no blind Enter or paste.
- **Provider/account issue:** report it separately; prompts cannot create quota.

An observation interval is a target, not a guarantee. Measure actual cycle time
and detection-to-input latency. Avoid repeated whole-transcript scans; use an
authenticated, bounded record window and state its time coverage.

## 5. Verify recovery

Record detection → authorized attempt → native receipt → resumed work. A command,
Working label or Goal-start record alone is insufficient. Look for the original
session's subsequent tool result or meaningful task progress, and verify that
the next real failure can recover too. Keep error recurrence visible.

Report source revision, sanitized target alias, timestamps, reason, delivery
state, actual progress and limits. Do not upload real transcripts, credentials,
user paths or screenshots of private tasks.

## 繁體中文速查

1. 用 `status`、`list` 與 `--help` 檢查，不要順手安裝或啟用輸入。
2. 核對完整目標及原生會話；保留控制器、草稿、問題、暫停及投遞紀錄。
3. 分清工作中、重連、失敗、過期及投遞不明；不明時不可盲目重送。
4. 驗收須看到原會話產生實際工作結果，且下一次出錯仍能恢復。
5. Herdr 內部面板尚未納入預設安裝，不能把外層 surface 當成內部 agent。
6. 發布前脫敏，只保留必要的版本、假名、時間及結論，勿貼私人紀錄。

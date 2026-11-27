# Working with CCC

Read [README.md](README.md), [简体中文介绍](README.zh-CN.md) or [繁體中文介紹](README.zh-TW.md), then
[the agent guide](docs/agent-guide.md). These instructions apply to coding agents
editing this repository. Installing CCC does not enroll an agent or authorize input.
`CLAUDE.md`, `GEMINI.md` and `llms.txt` link here; clients without automatic
discovery can read this file explicitly. Use the same rules through any client.

## Scope and source of truth

- `main` contains the published cmux watcher, supervisor and supporting tools.
- The separate Herdr installer accepts `install`, `verify`, `once`, `serve`
  and `rollback`. Consult the [public operator guide](docs/herdr.md).
- Preserve existing session IDs, controllers, attempts, ledgers, budgets and drafts.
- Observation and status are not input authorization. Unknown delivery is not
  permission to resend. Never clear a ledger or replace a session to pass a test.
- Do not restart a user's agents, cmux, Herdr, gateways or services as a side
  effect of documentation work or source validation.

## Development

Use Python 3.10+ and the standard library for runtime code. Test dependencies are
listed in `requirements-test.txt`. Keep changes in a branch and submit a PR;
the required `ccc-ci` check must pass before merging. Run the relevant tests for
your change; use the full suite for runtime releases. See
[CONTRIBUTING.md](CONTRIBUTING.md) for commands and publication checks.

Build only on a local development machine or GitHub Actions. Never compile or
build images on a VPS or production server.

## Public artifacts

Never commit credentials, tokens, account profiles, transcripts, screenshots of
real sessions, generated state, private handoffs or personal absolute paths.
Use synthetic fixtures and `/Users/you` or `/home/user` placeholders. Run
`python3 -B scripts/check_public_content.py` before publishing. It is a bounded
pattern check, not proof that all sensitive information or Git history is clean.

Keep English and Traditional Chinese overview instructions aligned. Report
source change, tests, installed code, loaded runtime, native receipt and actual
task progress separately. Include unresolved limits in the handoff.

## 繁體中文摘要

先讀繁體中文介紹及 agent 操作指南。保留原會話、草稿、控制器、投遞紀錄與預算；
遇到投遞不明時保留原紀錄。Herdr 有獨立的首次安裝與回復入口。請將
私人紀錄或憑證留在本機。文件工作不可
順帶重啟使用者的業務 agent 或服務；VPS 永久禁止建置。

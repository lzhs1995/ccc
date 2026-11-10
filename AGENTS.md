# Working with CCC

Read [README.md](README.md) or [繁體中文介紹](README.zh-TW.md), then
[the agent guide](docs/agent-guide.md). These instructions apply to coding agents
editing this repository. Installing CCC does not enroll an agent or authorize input.
`CLAUDE.md`, `GEMINI.md` and `llms.txt` link here; clients without automatic
discovery can read this file explicitly. Use the same rules through any client.

## Scope and source of truth

- `main` contains the published cmux watcher, supervisor and supporting tools.
- Herdr integration is under development. See [support status](docs/herdr.md).
  Do not describe an experimental branch, local installation, or passing fixture
  as a feature available in the default installation.
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
觀察到面板不等於獲准輸入，投遞不明時不可盲目重送。Herdr 功能仍在開發，不能把
本機驗證寫成主分支已提供。不要把私人紀錄或憑證提交到公開倉庫。文件工作不可
順帶重啟使用者的業務 agent 或服務；VPS 永久禁止建置。

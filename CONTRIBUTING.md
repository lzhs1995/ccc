# Contributing / 參與貢獻

Use an issue for a reproducible problem, a feature branch for changes, and a pull
request for review. Keep credentials and private session data out of public issues.
Follow [AGENTS.md](AGENTS.md) whether you work manually or through an AI agent.

## Local checks

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-test.txt
python3 -B scripts/check_public_content.py
python3 -B -m unittest discover -s tests -p 'test_*.py'
```

The full CI also runs stack contract, isolated profile-editor PTY, shell syntax
and release packaging checks. Runtime tests must use temporary homes, synthetic
sessions and local fixtures; they must not type into a user's real terminal or
contact a paid model endpoint. Small documentation changes need link/content
verification, not an invented runtime test.

## Describe the result

Explain the trigger, observed behavior, intended behavior and tests. For recovery
changes, distinguish detection, authorization, input attempt, native receipt and
subsequent useful work. State evidence freshness and exclusions. Never interpret
an error-free test, a queued message, or a running process as task completion.

## Publishing

The public-content workflow scans the checked-out tree for common secret and
private-path patterns. Review its findings; do not broadly whitelist a failing
directory. A release requires the existing `ccc-ci` check, a clean committed tree,
the package manifest and real installation/observation acceptance described in
[operations](docs/operations.md). A documentation PR does not create a new runtime
release or adopt experimental branches.

English and `README.zh-TW.md` must agree on requirements, commands and support
status. Keep screenshots synthetic. The original SVG branding is in `assets/`.

繁體中文：請以議題描述可重現問題，以分支及 PR 提交變更。公開資料必須脫敏；
測試使用隔離的假資料。主分支、實驗分支、本機安裝與實際恢復是不同階段，請分開
說明。修改安裝方法或支援範圍時，同步更新中英文介紹。

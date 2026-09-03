# CCC

CCC is a macOS command-line toolkit for Claude Code operations:

- `cmux_codex_watch.py` keeps configured Claude surfaces observable and applies the fail-closed continuation policy.
- `cmux_supervisor_tui.py` provides the read-only supervisor view, including workspace titles and collaboration roles.
- `ccc_session_audit.py` prints audit summaries without changing sessions.
- `janitor/src/` contains the quarantine-first cmux cleanup worker and guard.
- `ccp_new.py` manages profiles interactively; credentials remain in a user-selected directory.
- `bin/cmux-stack` projects component status without merging the execution processes.

The UI follows a compact terminal vocabulary inspired by mature CLI tools such as
[mole](https://github.com/tw93/mole): restrained colour, clear section rules,
short status symbols, and a readable degraded/unknown distinction.

## Requirements

- macOS 13 or newer
- Python 3.10 or newer
- [cmux](https://cmuxterm.app/) for surface discovery and terminal I/O

The code uses only the Python standard library. No profile, token, session
transcript, or runtime state is bundled in this repository.

## Local setup

```sh
python3 -m py_compile cmux_supervisor_tui.py cmux_codex_watch.py ccc_session_audit.py ccp_new.py bin/cmux-stack
export CCP_PROFILE_DIR="$HOME/.config/ccc/profiles"
export CMUX_STACK_CCP="$PWD/ccp_new.py"
export CMUX_STACK_WATCHER="$PWD/cmux_codex_watch.py"
export CMUX_STACK_JANITORCTL="$PWD/janitor/src/cmux-janitorctl"
```

Copy `templates/_template.example.json` to `$CCP_PROFILE_DIR/_template.json`
and fill credentials only on the local machine. The profile manager validates
before replacing a file and keeps its own rollback/trash rules.

LaunchAgent files are templates, not active installation. To render them into
your own user domain:

```sh
./scripts/render-launchagents.sh
```

Review every generated plist and load it only after an explicit local decision.
`cmux-stack` itself is read-only unless its command explicitly says `--apply`.

## Verification

Run the source-only checks before any LaunchAgent action:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
bash -n janitor/src/cmux-janitor.sh janitor/src/guard.sh
python3 bin/cmux-stack status --json
python3 ccp_new.py --status --json
```

Production deployment is intentionally outside this repository's automated
tests. Keep generated plists, credentials, logs, and local handoff artifacts out
of Git. See `.gitignore` for the default exclusions.

## Safety model

The control plane reports component-owned state; it does not recompute a second
health verdict. The janitor moves candidates to quarantine before disposal and
fails closed when its live store or quarantine setting is not trustworthy.
`ccp_new.py` is a foreground editor by design, so credentials are never written
by a background LaunchAgent.

## Supervisor view contract

The supervisor is a read-only projection of cmux-owned state. Its table keeps
the following columns stable: `监控`, `位置`, `程序`, `Hook`, `上下文`, `画面`,
`错误`, `续跑`, `协作`, `标题`, and (when the terminal is wide enough) `session`.
`程序` identifies the client (`Codex`, `Claude`, `grok`, `Copilot`, `gh`,
`shell`, `其他`, or `未知`); it never carries a health verdict. `画面` is the
state vocabulary (`空闲`, `运行中`, `菜单`, `待续跑`, `已排队`, `已过时`,
`正在输入`, `看不清`, `非Codex`, `Claude关`, `Hook等待`, `输入保护`,
`发送中`, `已完成`, `已续跑`, `Hook待验`, `Hook缺失`, `Hook旧版`, `身份冲突`,
or `需人工`).

`R` only rescans and never registers a surface or authorizes sending. New
workspaces remain `未登记` until the operator explicitly chooses `a` (one
surface) or `w` (the whole workspace). The stale/unknown thresholds used by
the source are documented in the source comments and are intentionally more
conservative than the send path; a stale observation is never converted into
an authorization.

The compact status vocabulary is deliberately borrowed from mature CLI tools:
`✓` means healthy, `⚠` means degraded or needs attention, `✗` means failed,
and `→` marks an action or transition. Colours are a secondary cue: green,
yellow, red, purple, and muted grey are paired with text so a no-colour terminal
has the same meaning. Horizontal rules use stable ASCII characters and rows are
clipped to their display width before curses writes them.

## License

MIT. See [LICENSE](LICENSE).

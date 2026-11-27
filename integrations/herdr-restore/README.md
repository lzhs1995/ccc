# Herdr offline restore

Use Python 3.10+ with a v3 Herdr session snapshot. The input can be a private
content-addressed backup at `snapshots/NAME/SHA256.json`. Select the intended
snapshot by the panes it contains. An empty snapshot written after an outage can
be newer than the snapshot you need. Content-addressed backups are checked
against their filename and parent session directory.

## Inspect the reconstruction

From the directory containing `restore.py`, set `SNAPSHOT` to the snapshot file
and `SESSION` to its saved session name. Pick a fresh output directory.

```sh
python3 -B restore.py stage --snapshot "$SNAPSHOT" --session "$SESSION" --out ./preview
python3 -m json.tool ./preview/plan.json
```

`session.json` preserves workspace order and public IDs, tab order, pane labels,
working directories, split orientation/ratios, focused panes, zoom, active tab
and workspace selection. The default output starts shells only. Herdr can assign
fresh terminal IDs and reset runtime-only agent display names on cold boot.
The plan records each saved placement, the selected native ID, and a reason for
each held seat. Files in the resulting directory contain private session data.

## Stage native restoration

Use a second fresh directory. An isolated session is started separately by the
operator or installer. The following command only generates the two files.

```sh
python3 -B restore.py stage --snapshot "$SNAPSHOT" --session "$SESSION" --out ./native --auto-enter
```

The optional `--fleet-manifest FILE` supplies Codex/Claude history proofs from
the v2 backup. `--home DIR` selects the native history root for a rehearsal;
the server must use the same home. `--launchers FILE` accepts a private JSON
map from agent kind to an absolute executable, for example a profile-specific
AGY wrapper. Reuse the original authenticated profile and executable when
its account, provider configuration or environment differs from the default CLI.

| Agent | Required on-disk evidence | Native invocation |
| --- | --- | --- |
| AGY | Saved source and UUID, same cascade ID in trajectory metadata, and at least one step | `agy --conversation ID` |
| Grok | CWD-specific session directory, original ID and cwd in summary, and a readable chat record | `grok --resume ID` |
| Codex | Snapshot ID and exact fleet binding, SHA-256 history prefix and matching session metadata | `codex [saved provider option] resume ID` |
| Claude | Snapshot ID and exact fleet binding, SHA-256 history prefix and matching session ID | `claude --resume ID` |

For an offline rehearsal, set a private config and socket namespace, give
the server the original session name, and put the generated `session.json`
under the rehearsal configuration's `sessions/NAME/` directory (the root
config directory for `default`). Keep `plan.json` at its generated path.
Herdr's `[session] resume_agents_on_restore = true` must be enabled.
Start the isolated server with `herdr --session "$SESSION" server`.
Attach a private terminal client to that same server with
`herdr --session "$SESSION"` so it has terminal geometry for pending agents.
If the same session name is running elsewhere, separate the Herdr API and
client socket paths as well as the home/config directories before starting.
Use short socket paths on macOS.

On cold start Herdr reads the generated `agent_resume.argv` and starts the
adapter in each eligible seat. The adapter checks the pane's live foreground
PID, workspace/tab/terminal and native history, journals a single intent,
and replaces its own process with the native agent. This avoids sending text
or Enter into another terminal. Keep the stage directory and program path
stable until completion. Inspect native CLI session selection to confirm real
business recovery; an `exec_intent` file records an attempt only. An unresolved
intent should be read back before preparing a further attempt.

## Offline tests

```sh
python3 -B -m unittest discover -s . -p test_restore.py -v
```

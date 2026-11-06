#!/usr/bin/env bash
# Focused green-only package gate; official watchdog, exactly one invocation.
set -euo pipefail
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
python3 "$NESTED_PREPARATION/contract.py" admit "$NESTED_PREPARATION"
evidence="$NESTED_EVIDENCE/package"
mkdir "$evidence"
command=(python3 scripts/ci/hung_test_watchdog.py
  --stall-seconds 180 --timeout-seconds 900 --sample-seconds 5
  --label TerminalSurfaceStartupRestorePolicyTests --log "$evidence/execution.log"
  -- swift test --package-path Packages/macOS/CmuxTerminal
  --filter TerminalSurfaceStartupRestorePolicyTests)
printf '%s\n' "${command[@]}" > "$evidence/command-argv.txt"
status=0
"${command[@]}" < /dev/null > "$evidence/wrapper.log" 2>&1 || status=$?
printf '%s\n' "$status" > "$evidence/exit-code.txt"
python3 scripts/ci/require_swift_test_execution.py --log "$evidence/execution.log" \
  > "$evidence/executed-count.txt"
python3 "$NESTED_PREPARATION/contract.py" package-verdict "$NESTED_PREPARATION" "$PWD" "$evidence"

#!/usr/bin/env bash
# Identical test selector and command for red and green. Only result paths differ.
set -euo pipefail
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
case "$NESTED_PHASE" in red|green|boundary|existing-binding) ;; *) exit 64 ;; esac
selector=$(python3 "$NESTED_PREPARATION/contract.py" selector "$NESTED_PREPARATION" "$NESTED_PHASE")
evidence="$NESTED_EVIDENCE/$NESTED_PHASE"
mkdir "$evidence"
command=(bash "$NESTED_PREPARATION/console-in-source.sh" ./scripts/ci/run-app-host-xcodebuild.sh
  -project cmux.xcodeproj -scheme cmux-unit -configuration Debug
  -derivedDataPath "$NESTED_DERIVED" -destination platform=macOS
  "-only-testing:$selector" -parallel-testing-enabled NO
  -maximum-test-execution-time-allowance 120 test-without-building)
printf '%s\n' "${command[@]}" > "$evidence/command-argv.txt"
if [ "$NESTED_PHASE" = green ]; then
  cmp "$NESTED_EVIDENCE/red/command-argv.txt" "$evidence/command-argv.txt"
fi
status=0
"${command[@]}" -resultBundlePath "$evidence/result.xcresult" \
  > "$evidence/execution.log" 2>&1 || status=$?
printf '%s\n' "$status" > "$evidence/exit-code.txt"
# Fail closed on missing, skipped or zero executions. Original log/status stay.
./scripts/ci/require_selected_test_execution.sh "$evidence/execution.log" "$selector" \
  > "$evidence/executed-count.txt"
# The official wrapper already bounds and preserves Apple's typed export.
cp "$evidence/result.tests.json" "$evidence/tests.json"
test -s "$evidence/tests.json"
python3 scripts/ci/focused_test_selectors.py check-executed \
  --selectors "$selector" --tests-json "$evidence/tests.json"
python3 "$NESTED_PREPARATION/contract.py" verdict "$NESTED_PREPARATION" "$NESTED_PHASE" "$PWD" "$evidence"

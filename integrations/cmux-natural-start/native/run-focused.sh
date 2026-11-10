#!/usr/bin/env bash
# Identical test selector and command for red and green. Only result paths differ.
set -euo pipefail
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
case "$NESTED_PHASE" in red|green|icon|regression|boundary|existing-binding) ;; *) exit 64 ;; esac
test "${CMUX_DERIVED_DATA_PATH:?missing app-host DerivedData}" = "$NESTED_DERIVED"
selector=$(python3 -BE "$NESTED_PREPARATION/contract.py" selectors "$NESTED_PREPARATION" "$NESTED_PHASE")
IFS=, read -r -a selectors <<< "$selector"
test "${#selectors[@]}" -gt 0
evidence="$NESTED_EVIDENCE/$NESTED_PHASE"
mkdir "$evidence"
python3 -BE "$NESTED_PREPARATION/contract.py" phase-intent "$NESTED_PREPARATION" "$NESTED_PHASE" "$PWD" "$evidence"
command=(bash "$NESTED_PREPARATION/console-in-source.sh"
  /usr/bin/env CMUX_XCODEBUILD_NONINTERACTIVE_RESTART_BUDGET=0
  ./scripts/ci/run-app-host-xcodebuild.sh
  -project cmux.xcodeproj -scheme cmux-unit -configuration Debug
  -derivedDataPath "$NESTED_DERIVED" -destination platform=macOS)
for exact_selector in "${selectors[@]}"; do
  command+=("-only-testing:$exact_selector")
done
command+=(-parallel-testing-enabled NO
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
# The upstream wrapper only exports its generated result path, not a caller
# -resultBundlePath. Export this exact finalized bundle with its bounded tool.
test -f "$evidence/result.xcresult/Info.plist"
for kind in summary tests; do
  python3 -BE scripts/ci/run_with_timeout.py --timeout-seconds 120 -- \
    xcrun xcresulttool get test-results "$kind" \
      --path "$evidence/result.xcresult" --compact \
      > "$evidence/result.$kind.json" 2> "$evidence/result.$kind.err"
  test -s "$evidence/result.$kind.json"
done
cp "$evidence/result.tests.json" "$evidence/tests.json"
test -s "$evidence/tests.json"
python3 -BE scripts/ci/focused_test_selectors.py check-executed \
  --selectors "$selector" --tests-json "$evidence/tests.json"
python3 -BE "$NESTED_PREPARATION/contract.py" verdict "$NESTED_PREPARATION" "$NESTED_PHASE" "$PWD" "$evidence"

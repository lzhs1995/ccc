#!/usr/bin/env bash
# Preparation only. Builds and retains a uniquely tagged app on disposable CI.
set -euo pipefail
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
test "${GITHUB_ACTIONS:-}" = true
test "${CMUX_DEV_BACKEND_MODE:-}" = local
test "${CMUX_GHOSTTYKIT_PREPROVISIONED:-}" = 1
test "${CMUX_ALLOW_UNSIGNED_DEV_APP:-0}" = 0
case "$GITHUB_RUN_ID:$GITHUB_RUN_ATTEMPT" in *[!0-9:]*|:*) exit 64 ;; esac
python3 "$NESTED_PREPARATION/contract.py" admit "$NESTED_PREPARATION"
for phase in red green boundary existing-binding package; do
  test -s "$NESTED_EVIDENCE/$phase/VERDICT.json"
done
test "$(git rev-parse HEAD)" = "$(cat "$NESTED_EVIDENCE/green-commit.txt")"
test "$(git rev-parse HEAD^)" = "$(cat "$NESTED_EVIDENCE/red-commit.txt")"
test "$(git rev-parse HEAD^^)" = 2e109a1739d320fe36220afb3e7775b59c1acd47
test -z "$(git status --porcelain --untracked-files=normal)"
tag="nested-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
app_name="cmux Nested Restore ${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
bundle_id="com.cmuxterm.app.debug.nestedrestore.r${GITHUB_RUN_ID}a${GITHUB_RUN_ATTEMPT}"
derived="$RUNNER_TEMP/nested-retained-derived"
app="$derived/Build/Products/Debug/$app_name.app"
test ! -e "$derived"
mkdir "$NESTED_EVIDENCE/retained-app"
# No --launch: the official wrapper publishes a unique CI-only tag but never
# opens this app. --build-only deletes its temporary final app, so is unsuitable.
./scripts/reload.sh --tag "$tag" --name "$app_name" --bundle-id "$bundle_id" \
  --derived-data "$derived" --no-global-cli-links \
  2>&1 | tee "$NESTED_EVIDENCE/retained-app/build.log"
test -d "$app"
test -z "$(git status --porcelain --untracked-files=normal)"
python3 "$NESTED_PREPARATION/attest-app.py" "$app" "$bundle_id" "$tag" \
  "$NESTED_PREPARATION" "$NESTED_EVIDENCE" "$PWD"

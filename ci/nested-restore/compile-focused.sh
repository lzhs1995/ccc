#!/usr/bin/env bash
# Preparation only; invoke in the exact upstream checkout on a fresh CI runner.
set -euo pipefail
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
mkdir -p "$NESTED_DERIVED" "$NESTED_EVIDENCE"
revision=$(git rev-parse HEAD)
./scripts/test-unit.sh \
  -derivedDataPath "$NESTED_DERIVED" \
  'SWIFT_ACTIVE_COMPILATION_CONDITIONS=$(inherited) CMUX_CI_APP_HOST_ISOLATION_REQUIRED' \
  "CMUX_CALLER_PATH=$PATH" \
  CMUX_SKIP_ZIG_BUILD=1 \
  -showBuildTimingSummary \
  build-for-testing 2>&1 | tee "$NESTED_EVIDENCE/build-$revision.log"

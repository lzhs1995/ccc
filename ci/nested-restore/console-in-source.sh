#!/usr/bin/env bash
# Scope the official console wrapper's working directory to this checkout.
# GITHUB_WORKSPACE is changed only in this subprocess, never in GITHUB_ENV.
set -euo pipefail
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
test "$#" -gt 0
source_root="${CMUX_CI_RUNTIME_SOURCE_ROOT:?missing exact runtime source root}"
test "$(pwd -P)" = "$source_root"
test -x "$source_root/scripts/ci/run-in-console-session.sh"
exec env GITHUB_WORKSPACE="$source_root" \
  "$source_root/scripts/ci/run-in-console-session.sh" "$@"

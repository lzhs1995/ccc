#!/usr/bin/env bash
# Runs after the real console hop, before any build. No process control.
set -euo pipefail
test "$#" = 1
test "$(pwd -P)" = "${CMUX_CI_RUNTIME_SOURCE_ROOT:?missing source root}"
test "$GITHUB_WORKSPACE" = "$CMUX_CI_RUNTIME_SOURCE_ROOT"
test "${CMUX_DERIVED_DATA_PATH:?missing app-host DerivedData}" = "$1"
test "${CMUX_XCODEBUILD_NONINTERACTIVE_RESTART_BUDGET:?missing restart budget}" = 0
test "${CMUX_APP_HOST_XCODEBUILD_ATTEMPTS:?missing attempt count}" = 1
test "${CMUX_CI_APP_HOST_ISOLATION_REQUIRED:?missing isolation policy}" = 1
test -d cmux.xcodeproj
test -x scripts/ci/run-app-host-xcodebuild.sh
test -x scripts/ci/cleanup-app-host-home.sh
source scripts/ci/app-host-processes.sh
cmux_validate_app_host_derived_data "$CMUX_DERIVED_DATA_PATH"
test "$CMUX_VALIDATED_APP_HOST_DERIVED_DATA" = "$1"
printf 'NESTED_APP_HOST_ENV_OK=%s\nRESTART_BUDGET=0\nATTEMPTS=1\n' "$CMUX_VALIDATED_APP_HOST_DERIVED_DATA"

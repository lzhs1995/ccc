# Local compaction heartbeat validation

This package reconstructs the frozen R5 candidate on the public
`yetone/magpie` commit `86254d4049cb6be4a2ccdab02edf0bd59860c603`.
It preserves the preceding portable continuation, response deadline,
summary extraction, authorization, held heartbeat and passing heartbeat fixes.
The manifest identifies all 5,697 effective source files. The historical
input allowlist has 33 paths; 31 differ from the public baseline. Two files
already match the baseline and need no patch hunks.

The proposed publication branch is `ci/magpie-localcompact-20261112`, separate
from the Rust validation branch. Publish only this directory to
`validation/magpie-localcompact-r1/`, plus the separately frozen workflow at
`.github/workflows/magpie-localcompact.yml`. Local provenance maps, original
source overlays, SDKs, caches, attempt logs and application-check directories
are excluded from the publication allowlist.

`PINNED-FILES.json` pins every package input except itself. The workflow pins
the SHA256 of that file and verifies all inputs before importing a package
script. The workflow checks out the exact public base, applies
`candidate.patch`, and verifies the complete source membership and hashes.
Locked modules are downloaded before tests; tests use `GOPROXY=off`,
`GOSUMDB=off`, `GOWORK=off`, `GOTOOLCHAIN=local`, and `-mod=readonly`.
No dependencies or frozen source are updated by this package.

The four serial gates are:

| Case | Required actual Go test result |
| --- | --- |
| focused | All 11 named top-level tests pass once, including every executed subtest |
| compat | All 36 named top-level tests pass once, including every executed subtest |
| race | All 11 focused tests pass three times with `-race` |
| red | Only `TestLocalCompactHeartbeatSlow/success` and its parent fail, with the exact missing-complete-heartbeat assertion |

All invocations retain `-p=1 -mod=readonly -tags=production -json
-timeout=120s`. `SOURCE-MANIFEST.json` fixes the complete filters and test names.
The RED patch removes only the outer local-compaction lifecycle integration
from `codex_backend.go`; the fixture and preceding accepted fixes remain.
It must compile and actually reach `real client received no complete ping`.
A compilation error, timeout, panic, unrelated assertion, skipped test,
zero-test result, or unexpected count cannot satisfy RED. The runner restores
the candidate source after RED and verifies its full hashes again.

On the prepared GitHub runner, the four commands are:

```sh
python3 -B validation/magpie-localcompact-r1/run_case.py focused --source magpie --output results/focused --package-pin "$PACKAGE_PIN"
python3 -B validation/magpie-localcompact-r1/run_case.py compat --source magpie --output results/compat --package-pin "$PACKAGE_PIN"
python3 -B validation/magpie-localcompact-r1/run_case.py race --source magpie --output results/race --package-pin "$PACKAGE_PIN"
python3 -B validation/magpie-localcompact-r1/run_case.py red --source magpie --output results/red --package-pin "$PACKAGE_PIN"
```

The target is Darwin arm64 with Go 1.26.8, CGO enabled and a macOS 12.0
deployment target. The original local build used MacOSX26.5.sdk. GitHub's
macOS 15 arm64 image may provide a different SDK/clang; each actual run records
the exact versions. These are source-level behavioral tests and do not prove
byte identity with an existing production binary. No binary deployment or
production service restart is included.

The runner requires two free-space samples of at least 30 GiB and monitors
the same floor while its own Go child runs. It uses isolated temporary and
XDG directories. The upstream test harness establishes its own temporary HOME;
the selected fixtures use fake loopback suppliers and need no account keys.
The workflow has read-only repository permission, stores no checkout
credentials, and receives no user credentials or real model configuration.
Network access is used for public checkout and locked module downloads.

`test_verify.py` exercises the Python verifier and cleanup logic with synthetic
JSON and mocks. Its success is not evidence that the Go candidate passed.
`summarize.py` revalidates all four real JSON logs and clean runner states.
The workflow uploads every attempted gate's logs and result, including failures.
At package preparation time no Go gate has been run by this package, and no
remote branch, workflow or service has been changed.

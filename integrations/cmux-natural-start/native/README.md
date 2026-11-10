# Native checks for cmux natural startup

This lane proves the configuration-only launch fix with a behavioral red and
green, then checks the new signed bundle icon policy and adjacent restore
behavior. It prepares no user installation and never restarts an existing cmux.

The unchanged upstream `2e109a1739d320fe36220afb3e7775b59c1acd47` plus the
original nested restore patches must reconstruct base tree
`40e8ea03ce1435f11e90b35f98d554a13b9b8685`. A real accepted-base commit is
recorded; it does not impersonate the historical `febf361…` commit.

1. `source_chain.py base` reconstructs that base and records its actual commit.
2. `source_chain.py red` applies only the eight added restore tests, producing
   tree `2ac402069e1c937b23be75126c0fdb663f5905cd`.
3. `source_chain.py green` applies the four production edits and five new icon
   tests, producing tree `f0e2d0f26d1d5dde021ef2729aeb3f987993fe0a`.

The full `cmuxTests` target is compiled with the original `test-unit.sh`
build-for-testing recipe. `-only-testing` filters execution, not compilation.
Restore red and green use one identical invocation with eleven exact method
selectors. Red requires exactly two named assertion failures and nine passes;
compilation failure, timeout, skipped cases or zero tests are rejected. Icon
uses a separate eleven-method green invocation because its new APIs do not
exist in the old source; there is no original-API icon red claim.

After green, the original 11 regression, 6 boundary, 4 binding and 6 package
cases execute again on the new green tree. Old successful receipts are retained
as historical evidence and cannot satisfy these gates.

## Workflow interface

Set `NESTED_PREPARATION` (and optionally `NATIVE_PREPARATION`) to this directory,
`NESTED_EVIDENCE` to a new runner-temp directory, `NESTED_DERIVED` and
`CMUX_DERIVED_DATA_PATH` to the same isolated DerivedData directory, and
`CMUX_CI_RUNTIME_SOURCE_ROOT` to the canonical separate source checkout.

```sh
python3 -BE "$NESTED_PREPARATION/contract.py" admit "$NESTED_PREPARATION"
python3 -BE "$NESTED_PREPARATION/source_chain.py" base "$NESTED_PREPARATION" "$CMUX_CI_RUNTIME_SOURCE_ROOT" "$NESTED_EVIDENCE"
# Repeat the same source_chain interface with red, then green at their steps.
# compile-focused.sh uses NESTED_PHASE=red or green.
# run-focused.sh phases: red, green, icon, regression, boundary, existing-binding.
# run-package.sh runs the package phase after the green compile.
python3 -BE "$NESTED_PREPARATION/evidence_native.py" export \
  --preparation "$NESTED_PREPARATION" --evidence "$NESTED_EVIDENCE" \
  --output "$RUNNER_TEMP/cmux-natural-native-small"
```

Keep the reviewed 350-minute native job, 150-minute cold compile, 60-minute
green compile, ten-minute focused phase and seventeen-minute package budgets.
The console preflight, attempts=1, restart budget=0, isolated home preparation
and owned cleanup remain required. Do not add the old tagged app build.

Upload the small directory as
`cmux-natural-native-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}`. The Release job
must `needs` the successful native job and pass its artifact ID and digest:

```sh
python3 -BE native/download_native.py --artifact-id "$NATIVE_ARTIFACT_ID" \
  --artifact-digest "$NATIVE_ARTIFACT_DIGEST" \
  --output "$RUNNER_TEMP/cmux-natural-native-admitted"
```

The token is inherited through `GH_TOKEN`; it is never an argument or receipt.
Three one-shot GETs read this run attempt, the artifact metadata and the complete
ZIP. Metadata, needs output and actual ZIP digest must agree. ZIP and expanded
content each have a 64 MiB ceiling. Paths, duplicates, links, encrypted/special
members and CRC failures are refused. Timeout and partial files remain under
the sibling `*-download` directory as UNKNOWN, with no retry.

## Receipt and Release admission

`NATIVE-SUCCESS.json` binds repository, preparation head, run/attempt, base and
current source trees, actual base/red/green commit chain, contract SHA and all
seven phase verdict hashes. `MANIFEST.json` covers every exported regular file.
The export includes source records, compile receipts and complete compile logs,
each phase's original argv, logs, exit/count, typed tests and summary, verdicts,
and fixed patch/source inputs. It includes no app or raw xcresult bundle.
Preflight and owned cleanup logs are included. An early failure still exports
every available member in this fixed scope, plus `EXPORT-STATUS.json` listing
missing or rejected files. That export exits nonzero and has no
`NATIVE-SUCCESS.json`; an `always()` upload can retain the failed attempt without
admitting it to Release. Oversized or changing files are reported as failures,
never silently presented as complete evidence.
The source metadata correction is an explicitly labeled public projection;
its original private path is omitted and its original byte hash is retained.

`evidence_native.verify(path, expected_run=..., expected_source=...,
expected_contract_sha256=...)` authenticates the complete manifest and
re-evaluates the original typed results using the parsers shipped with this
published code. It never imports code from the artifact. Equality is JSON type
strict: `true` cannot authenticate run attempt `1`. Missing/pending source pins,
foreign source/attempt, incomplete evidence or compile failures refuse Release
admission. Physical reboot, provider login and adoption by an active client
remain separate runtime work.

## Local preparation verification

`python3.14 -BE -m unittest -v test_native` exercises offline source/result,
shell invocation and download/ZIP failure fixtures. These are portable tooling
checks; they are not native Swift or application execution receipts.

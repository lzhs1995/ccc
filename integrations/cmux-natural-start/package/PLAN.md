# Natural-start Release package preparation

This package uses the native `Release` configuration of upstream cmux
`2e109a1739d320fe36220afb3e7775b59c1acd47`, with the already accepted tests/fix
patches applied to reproduce tree `40e8ea03ce1435f11e90b35f98d554a13b9b8685`.
The original green source commit is `febf361281fdc0f463e84d9d5e4dd65d107dd818`.
It is the accepted base, not the new product. The additional restore-test patch
must produce tree `2ac402069e1c937b23be75126c0fdb663f5905cd`; the complete
natural-start fix must then produce `f0e2d0f26d1d5dde021ef2729aeb3f987993fe0a`.
Before building Release, the separate native job must provide current red/green,
icon and adjacent Herdr test evidence. The original Debug archive supplies only
its separately identified old CLI for compatibility testing.

## Prepared operations

1. Authenticate the native artifact from the same workflow run, attempt and
   preparation commit; verify its exact source chain and complete typed results.
   On the dependent hosted GitHub `macos-26` runner, perform a real-Aqua fresh
   account preflight. Pin the old accepted CLI from its original artifact;
   neither the old GUI nor a CCC service is launched.
2. Apply the two original patches and then the new tests/fix patches in a separate
   fixed-upstream checkout. Require all three trees, native Release product identity,
   no DEBUG compilation condition, and the official pinned toolchain dependencies.
   Run one app-only build. The Release job does not rerun the native job's tests.
3. Copy the entire compiled bundle to a fresh private package directory. Apply
   the reviewed local metadata, sign the actual nested-code closure, record
   every code identity, verify the whole bundle, archive it, read it back and
   issue a new complete package pin. Preserve the original native-admission bytes,
   their SHA and the validated summary in the package. The old APP-PIN cannot
   admit this package.
4. Install only in the disposable runner's previously absent
   `/Applications/cmux.app`. The signed Boolean option
   `CMUXDisableBundleIconPersistence=true` preserves the bundle while retaining
   displayed app/Dock icons. Open through ordinary LaunchServices without extra
   arguments and observe
   its real process environment. Verify default/new/old CLI routes, the stable
   marker, automation mode and a direct viewport request. Use one synthetic
   shell workspace, observe its natural save, terminate only that authenticated
   owned GUI generation, reopen once, then require the same workspace/surface
   UUIDs, stable IDs, cwd and layout. Reuse the same cached socket pathname across
   generations. Leave the saved evidence intact and clean up the owned GUI.
5. Retain the full `cmux-release.zip` as its own GitHub artifact. Export evidence
   separately, after binding the real full-artifact upload ID, digest, URL,
   source head, run ID and attempt. No complete app or old artifact belongs in
   the small evidence archive.

## Route and package contract

The package uses the existing production bundle ID `com.cmuxterm.app` and the
native per-account stable socket rule:

```text
<actual account home>/.local/state/cmux/cmux.sock
```

It never embeds the user's or the CI account's absolute home as a socket
override. The app's signed `LSEnvironment` selects automation mode, production
endpoints, and its intended installation paths. A caller's explicit conflicting
socket is refused, not redirected. Existing Debug override files are left
intact; the fixed Release authentication configuration does not read them.

The package is a local ad-hoc Release build. It has no upstream developer Team
or notarization claim. Preserve the entire compile output, record existing valid
nested signatures and fill missing local seals. The main entitlement set is
derived from the exact Release target's `Resources/cmux.entitlements`, excluding
its ungranted keychain group. Cloud tunnel activation, CodeRouter secure handoff,
browser passkey entitlements and other Team-dependent features are outside this
terminal/workspace/session acceptance.

## Time and evidence limits

- One compile attempt, at most 180 minutes for the build command; prerequisite
  commands have independent bounded timeouts. Workflow ceiling: 240 minutes.
- New package acceptance: 180 seconds shared across both GUI generations;
  acceptance elapsed time is recorded and must pass the gate. Final owned GUI
  cleanup has a separate 20-second gate and its own outcome.
- Command intent precedes execution. Timeout outcomes are UNKNOWN with original
  partial output retained; no blind retry or request replay.
- Each saved snapshot projection and its SHA/fence are derived from the same
  bounded file read. Actual restore input is separately retained.
- Small evidence ceiling: 64 MiB including the export inventory. Long command
  streams are compressed losslessly with original bytes/SHA and readback checks.
  A size or export failure never claims complete evidence.

## Post-run review and first business adoption

Preparation, signed archive admission, isolated runtime acceptance and existing
business-session adoption are separate states. A green workflow is insufficient
on its own. The reviewer must read the exact small artifact, verify its API
digest and complete export inventory, inspect the new build/package/runtime and
cleanup receipts, and separately verify the completed run's exact source head,
attempt and conclusion. Full app consumption stays on GitHub.

The old CLI in this test is the originally accepted artifact's CLI, not a claim
about the current user's installed CLI binary. The direct cached-path probe
models the path contract; it does not run the live CCC guard or prove its
business auto-continue behavior. The current original guard's real continuity
and its guarded control path remain required at natural adoption.

First adoption must also admit the existing four outer Herdr surfaces and their
real process/session ownership against the current natural save. Existing inner
auto-resume bindings must not be blindly replayed. The launcher rejects saved
resume bindings until this separate original-session ownership admission is
settled. No existing original controller, snapshot, trust record, CURRENT
selector, application or session is replaced by these preparation scripts.

At preparation closeout: no GitHub workflow has been dispatched, no new Release
app has been built, and no user application has been installed, launched,
restarted or signaled. Physical power-loss behavior remains unverified.

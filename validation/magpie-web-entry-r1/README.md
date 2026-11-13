# Real Magpie web entry acceptance

This package is an additional GHA-only gate for the exact candidate artifact already built by the passing native smoke job. It starts that executable with `serve --web=127.0.0.1:<owned-port>`, a synthetic private XDG configuration and a default-deny sandbox. HOME and CODEX_HOME are inherited, not overridden; neither is readable or writable through this policy. No real upstream, user configuration, credentials or sessions are used.

It records `codesign --verify --strict`, gateway health, the exact synthetic model GET, missing/wrong-key rejection, synthetic cookie login and the authenticated embedded page. All HTTP requests are GET. There are zero model POSTs; model completion is established separately by the four native cases for this same artifact SHA. This receipt never substitutes for them.

Every HTTP body/metadata, codesign log, gateway log, synthetic seed and exact policy is hashed. The child owns both listeners. Capacity admission is 30 GiB plus 128 MiB, with two initial samples and checks around every request. Only the owned child group is terminated and reaped; handlers and both listeners must be gone, the private directory removed and the artifact unchanged.

Run (on the disposable Darwin arm64 Actions runner only):

```
python3 -B validation/magpie-web-entry-r1/web_entry.py --build results/candidate/BUILD.json --output results/web-entry --package-pin <exact PINNED-FILES.json SHA256>
```

Read-only adapter contract:

```
verify_web(web_root: Path, candidate_folder: Path, artifact: dict) -> dict
```

`artifact` is exactly `{"sha256": ..., "bytes": ...}` from the separately verified candidate build. `web_root` contains original WEB-ACCEPTANCE.json and all raw evidence; `candidate_folder` contains the original BUILD.json. The return has schema `magpie-gha-web-entry-v1`, status PASS, artifact/build/receipt pins and model_posts 0. Importing and calling it starts no process and needs no executable download. The adapter pins this module and its package before import, independently validates build/source provenance, and requires the exact same candidate artifact for the four native cases. Web evidence may come from another original run only when that SHA and byte count are identical.

Module dependencies are Python standard library plus the same-directory `common.py`, `settings.json`, `plugins.yml`, and `PINNED-FILES.json`. Do not resolve `common` from a different package. Small tests create synthetic receipt graphs; their success is preparation evidence only, not real binary acceptance.

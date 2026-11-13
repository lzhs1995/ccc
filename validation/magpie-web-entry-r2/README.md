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

R2 fixes the cookie parser's interpretation of Chrome's Priority=High extension. It requires one exact named cookie for the Web port and rejects extra cookies, wrong keys, duplicate headers and unexpected Priority attributes. The same parser supplies the live request cookie.

`rejudge.py` only rechecks immutable archive 13962716444 from run 43257559926, commit 758f6e23c372dc607963a0bbf07e34c3718c4962. It pins the exact original FAIL receipt, build, binary, archive identity and original Web package. All six original HTTP responses, strict signature, sandbox, capacity and cleanup are revalidated using an in-memory normalized view. A separate rejudgment records the original FAIL and error; no original file is changed and no service, binary or HTTP request is run. This does not make the original GitHub workflow green.

The web-only workflow retrieves original artifact 13962716444, verifies the full ZIP SHA-256, and extracts only the exact pinned candidate binary and BUILD.json. It performs no compilation or native four-case repetition. Its new Web receipt has a distinct run ID and pins the same original binary/build. This allows review of both original four-case evidence and a fresh Web run without silently accepting a failed workflow or mixing binary identities.

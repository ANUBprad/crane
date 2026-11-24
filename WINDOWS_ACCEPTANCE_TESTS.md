# CRANE — Windows Acceptance Tests

This checklist tracks Windows release-readiness evidence. It distinguishes clean GitHub-hosted runner evidence from clean physical Windows 10/11 x64 acceptance. Do not treat a development machine, source checkout, or historical local evidence as clean-machine acceptance.

## Current status (release code HEAD `07f0c84ddc077136529bfff09c8991b22854c967`)

- GitHub-hosted `windows-2025` CI: **PASS** for build, formatting/vet, Go/Python tests (76), release packaging, packaged install/`agent --version`/`agent doctor`/basic task smoke, same-host reproducibility, and the full isolated Windows acceptance harness. Run: [45255583424](https://github.com/ANUBprad/crane/actions/runs/45255583424), completed `2026-11-23T20:17:45Z`. Artifact: [`crane-windows-ci-55`](https://github.com/ANUBprad/crane/actions/runs/45255583424/artifacts/14858007684), 150,073,641 bytes, uploaded ZIP SHA-256 `ac451412083eb08b4f493fb7dff8700bcc0a276a48a6cab9bf4fa0474bb6c0e0`.
- Same-host reproducibility evidence from that run: all six `0.1.2-repro` outputs were byte-for-byte identical, including ZIP SHA-256 `274489c418f26abe33088d5616e4248fa06e4c735eb2f5fc4d48a1450d2cae17` and manifest SHA-256 `b8760908bd728ebf9786b8e3fb288e7f5827ee643ccae7bded100a3825ab07aa`. The acceptance harness reported `ACCEPTANCE RESULT: ALL ISOLATED CHECKS PASSED`.
- Cross-environment reproducibility: `windows-2022` vs `windows-2025` job reported `CROSS-ENV RESULT=PASS (7 artifacts byte-for-byte identical)` in the same run.
- `scripts/acceptance-windows.ps1`: implemented; covers install, version/doctor smoke, basic task/result, upgrade, rollback, uninstall/data preservation, PATH, and integrity/failure cases. It requires prebuilt `0.1.0`-`0.1.2` release directories, uses isolated `LOCALAPPDATA`, and temporarily modifies/restores the real user PATH for the final PATH check. Run sequentially on a machine where that PATH mutation is acceptable.
- `scripts/test-release-reproducibility.ps1`: implemented; compares two independent builds with identical declared inputs for byte-for-byte ZIP/manifest/SHA256SUMS equality. Same-host evidence only unless cross-environment inputs are controlled.
- Clean physical Windows 10/11 x64 acceptance: **not performed**.
- Authenticode signing/publisher authenticity verification: **not available** (no legitimate certificate/credentials; artifacts are unsigned, integrity-only).
- Controlled official release host/domain: **not verified/published**.
- SBOM: CycloneDX 1.5 inventory is generated for every release, listed in `release-manifest.json`, covered by `SHA256SUMS.txt`, required by both installers, and validated by the acceptance harness. Local release generation produced 82 components (57 PyPI, 25 Go) with all five manifest artifacts hash-matched.
- GitHub Pages: workflow run [45248258548](https://github.com/ANUBprad/crane/actions/runs/45248258548) failed because the repository has no Pages site (`has_pages=false`, `GET /pages` 404). `configure-pages@v5` cannot enable it with the default `GITHUB_TOKEN`; owner action is Settings → Pages → Build and deployment → Source: GitHub Actions.
- Self-hosted runners: `GET /repos/ANUBprad/crane/actions/runners` returned `total_count: 0`; the clean Windows 10/11 workflow was therefore not dispatched.
- Local dev-host isolated validation (not clean-machine acceptance): Windows 11 Home x64 build 26300, PowerShell 7.7.0. A fresh release built locally with `sbom.cdx.json`, installed into a temporary root with `CRANE_SKIP_PATH_UPDATE=1` and isolated `LOCALAPPDATA`; user PATH was unchanged and `agent --version` reported the expected version. A manifest without the `sbom` kind was rejected.
- No new live Ollama model-driven acceptance was executed in this continuation; `agent doctor` only reported endpoint/model availability.

## Clean GitHub-hosted runner checklist

- [x] Windows runner builds Go binary and packages a release artifact (`windows-ci.yml`).
- [x] Go formatting, `go vet`, `go test ./...`, Python compile/tests run on `windows-2025`.
- [x] Packaged install smoke: `agent --version`, `agent doctor`, basic task submission on runner.
- [x] Full `scripts/acceptance-windows.ps1` run on clean GitHub-hosted runner with recorded artifact versions/hashes/commit.
- [x] `scripts/test-release-reproducibility.ps1` run with recorded inputs/hashes on runner or controlled environment.
- [x] Hosted-runner evidence recorded with workflow URL, HEAD SHA, artifact names, and hashes.
- [x] Legacy v1 SQLite migration test preserves tasks, events, approvals, and checkpoints (`TestLegacyV1DatabaseMigratesWithoutLosingTasksEventsApprovalsOrCheckpoints`).
- [x] Approval grant survives supervisor restart and is forwarded in the exact resumable checkpoint (`TestApprovalCheckpointSurvivesSupervisorRestartAndResumesExactGrant`).

## Clean physical machine checklist (required for Windows 10/11 acceptance claim)

Run on genuine clean Windows 10 x64 and Windows 11 x64 machines/VM images with no Go, Python, Node.js, npm, CRANE repository, or developer dependencies:

- [ ] Install from a verified release directory/source; confirm manifest/SHA256SUMS and signature state before install.
- [ ] `agent --version` reports expected version/build metadata.
- [ ] `agent doctor` reports packaged Python runtime, SQLite, and required modules available.
- [ ] Basic task submission, status, and result retrieval succeed.
- [ ] Supervisor start/stop behavior is clean; no orphaned processes after exit.
- [ ] Authenticated loopback API is available locally; no unexpected external listeners/data transmission observed.
- [ ] Task data persists across process restart in `%LOCALAPPDATA%\CRANE`.
- [ ] Update to a newer verified release preserves data/task history.
- [ ] Rollback restores previous version and preserves data/task history.
- [ ] Failed/unhealthy update leaves no inconsistent active install.
- [ ] Uninstall preserves data by default; removes install/PATH entries.
- [ ] Uninstall with `-RemoveData -Confirm REMOVE-CRANE-DATA` removes data only after explicit confirmation.
- [ ] PowerShell 5.1 and PowerShell 7.x both validated.
- [ ] OS build, PowerShell version, artifact version/hash/build commit, logs, and pass/fail evidence recorded.

## Artifact integrity / trust checklist

- [x] Release manifest records version, commit, OS/arch, Go/Python versions, build date, files, sizes, SHA-256 hashes, and signature state.
- [x] `SHA256SUMS.txt` generated and cross-checked by installer.
- [x] Installer validates file hashes/sizes and rejects tampered manifest/checksum/unsupported metadata.
- [x] Installer rejects unverifiable authentic signature metadata; unsigned artifacts are explicitly marked `none`/`unverified`.
- [x] CycloneDX 1.5 SBOM generated with a populated dependency inventory and authenticated by manifest + `SHA256SUMS.txt`.
- [x] Installer and website bootstrap require the `sbom` artifact kind; packaged install fails closed when it is absent.
- [ ] Authenticode signature generated with legitimate certificate/credentials (external blocker).
- [ ] Authenticode/signature verification validated on clean Windows (external blocker; requires legitimate certificate).

## Distribution / reproducibility checklist

- [ ] Controlled official release host/domain with verified ownership (external blocker).
- [ ] Official install command documented only after controlled host/domain exists.
- [x] Remote `http(s)` release source is explicit opt-in (`CRANE_ALLOW_REMOTE_RELEASE_SOURCE=1`); no silent remote fallback.
- [x] Same-host deterministic rebuild check script exists (`scripts/test-release-reproducibility.ps1`).
- [x] Same-host reproducibility evidence recorded for current release candidate inputs.
- [ ] Cross-environment reproducibility: `windows-2022` vs `windows-2025` comparison job added; first result pending on the next CI run.

## Evidence log template

Record each run with:

- Date/time (UTC and local), operator, machine/runner type
- OS build, PowerShell version
- HEAD commit, CI run URL
- Artifact version(s), manifest SHA-256, payload SHA-256, SHA256SUMS SHA-256
- Commands run, pass/fail, logs/screenshots/output paths
- Whether evidence is clean GitHub-hosted runner, dev-host isolated harness, or clean physical Windows 10/11 machine

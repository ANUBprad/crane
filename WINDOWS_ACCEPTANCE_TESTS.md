# CRANE — Windows Acceptance Tests

This checklist tracks Windows release-readiness evidence. It distinguishes clean GitHub-hosted runner evidence from clean physical Windows 10/11 x64 acceptance. Do not treat a development machine, source checkout, or historical local evidence as clean-machine acceptance.

## Current status (HEAD `256de3c3354e49c5878dd4c058397b1867d7a9ca`)

- GitHub-hosted `windows-2025` CI: **PASS** for build, formatting/vet, Go/Python tests, release packaging, and packaged install/`agent --version`/`agent doctor`/basic task smoke. Run: [45040349046](https://github.com/ANUBprad/crane/actions/runs/45040349046).
- `scripts/acceptance-windows.ps1`: implemented; covers install, version/doctor smoke, basic task/result, upgrade, rollback, uninstall/data preservation, PATH, and integrity/failure cases. It requires prebuilt `0.1.0`-`0.1.2` release directories, uses isolated `LOCALAPPDATA`, and temporarily modifies/restores the real user PATH for the final PATH check. Run sequentially on a machine where that PATH mutation is acceptable.
- `scripts/test-release-reproducibility.ps1`: implemented; compares two independent builds with identical declared inputs for byte-for-byte ZIP/manifest/SHA256SUMS equality. Same-host evidence only unless cross-environment inputs are controlled.
- Clean physical Windows 10/11 x64 acceptance: **not performed**.
- Authenticode signing/publisher authenticity verification: **not available** (no legitimate certificate/credentials; artifacts are unsigned, integrity-only).
- Controlled official release host/domain: **not verified/published**.

## Clean GitHub-hosted runner checklist

- [x] Windows runner builds Go binary and packages a release artifact (`windows-ci.yml`).
- [x] Go formatting, `go vet`, `go test ./...`, Python compile/tests run on `windows-2025`.
- [x] Packaged install smoke: `agent --version`, `agent doctor`, basic task submission on runner.
- [ ] Full `scripts/acceptance-windows.ps1` run on clean GitHub-hosted runner with recorded artifact versions/hashes/commit.
- [ ] `scripts/test-release-reproducibility.ps1` run with recorded inputs/hashes on runner or controlled environment.
- [ ] Hosted-runner evidence recorded with workflow URL, HEAD SHA, artifact names, and hashes.

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
- [ ] Authenticode signature generated with legitimate certificate/credentials (external blocker).
- [ ] Authenticode/signature verification validated on clean Windows (external blocker; requires legitimate certificate).

## Distribution / reproducibility checklist

- [ ] Controlled official release host/domain with verified ownership (external blocker).
- [ ] Official install command documented only after controlled host/domain exists.
- [x] Remote `http(s)` release source is explicit opt-in (`CRANE_ALLOW_REMOTE_RELEASE_SOURCE=1`); no silent remote fallback.
- [x] Same-host deterministic rebuild check script exists (`scripts/test-release-reproducibility.ps1`).
- [ ] Same-host reproducibility evidence recorded for current release candidate inputs.
- [ ] Cross-environment reproducibility evidence recorded, or nondeterminism documented.

## Evidence log template

Record each run with:

- Date/time (UTC and local), operator, machine/runner type
- OS build, PowerShell version
- HEAD commit, CI run URL
- Artifact version(s), manifest SHA-256, payload SHA-256, SHA256SUMS SHA-256
- Commands run, pass/fail, logs/screenshots/output paths
- Whether evidence is clean GitHub-hosted runner, dev-host isolated harness, or clean physical Windows 10/11 machine

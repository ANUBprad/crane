# CRANE — Windows Distribution and Installation

## Goal

Install CRANE on Windows 10/11 x64 with PowerShell, then invoke `agent` from any directory. End users must not need Go, Python, Node.js, Docker, Redis, or a source checkout.

This is a per-user PowerShell bootstrapper, not an MSI or graphical installer.

## Current status

**As of HEAD `af0b4a18f6da37a1a293315a273b71f681400ec4` (Windows CI run [45238765482](https://github.com/ANUBprad/crane/actions/runs/45238765482), success, completed `2026-11-23T18:36:42Z`).**

Implemented:

- `scripts/build-release.ps1` builds versioned Windows x64 release directories with tracked app staging, a private Python embeddable runtime, pinned hashed dependencies, deterministic ZIP/manifest/SHA256SUMS generation, and explicit `signature.scheme=none`/`signature.status=unverified` metadata.
- `scripts/install.ps1` performs verified Install/Update/Rollback from a local directory or an explicitly allowed `http(s)` source. Remote sources require `CRANE_ALLOW_REMOTE_RELEASE_SOURCE=1`; local sources and the website bootstrap use a local verified directory. It validates manifest/SHA256SUMS, file hashes, platform metadata, and signature state before staging/switching; it rejects unverifiable authentic signatures. Updates stage and switch version pointers, then health-check the new version and restore the previous pointer on failure.
- `scripts/uninstall.ps1` runs detached, removes install/PATH entries, preserves `%LOCALAPPDATA%\CRANE` data by default, and removes data only with `-RemoveData -Confirm REMOVE-CRANE-DATA`.
- `scripts/acceptance-windows.ps1` provides an isolated lifecycle harness (install, version/doctor smoke, basic task/result, upgrade, rollback, uninstall/data preservation, PATH, integrity/failure cases). It mutates process LOCALAPPDATA and, for one check, the real user PATH with restore in `finally`; run it only on a machine where that is acceptable.
- `scripts/test-release-reproducibility.ps1` compares two independent builds with identical declared inputs and checks byte-for-byte ZIP/manifest/SHA256SUMS equality.

Validated by current repository evidence:

- GitHub-hosted `windows-2025` CI builds, formats/vets, runs Go/Python tests, packages a release, and smoke-tests packaged install/`agent --version`/`agent doctor`/basic task submission.
- The current run built versioned `0.1.0`–`0.1.2` releases, verified all six `0.1.2-repro` outputs byte-for-byte identical (ZIP SHA-256 `a84f26e0b6c9f615f865d40e9378aa7a44b8f5b2ec20b7d943f17778d375f3b8`), and ran the full isolated acceptance harness with `ACCEPTANCE RESULT: ALL ISOLATED CHECKS PASSED` (install, version/doctor, task/result, update, rollback, uninstall/data preservation, PATH, and integrity/failure cases).
- Artifact [`crane-windows-ci-47`](https://github.com/ANUBprad/crane/actions/runs/45238765482/artifacts/14851690816) is 150,044,577 bytes; uploaded ZIP SHA-256 `b0015df306e3e7be87671e920e76e39b8703a60eeb2df808bfb5e2a7822c361e`.

Not validated / external blockers:

- **Clean physical Windows 10/11 x64 acceptance:** not performed on this dev host. GitHub-hosted runner evidence is not a substitute for clean physical Windows 10/11 acceptance.
- **Authenticode / publisher authenticity:** no legitimate code-signing certificate/credentials are available in this environment. Artifacts are unsigned (`signature.scheme=none`, `signature.status=unverified`). SHA-256/checksum verification provides artifact-integrity evidence, not publisher authenticity.
- **Controlled official release host/domain:** no controlled official release host/domain is verified. Do not publish or claim an official install URL. Remote `http(s)` install sources are opt-in only and must be operator-controlled.
- **Cross-environment reproducibility:** `scripts/test-release-reproducibility.ps1` can provide same-host/same-input evidence; cross-environment/toolchain reproducibility is not proven while relevant environment differences remain uncontrolled.

## Public website and bootstrap installer

The project website is deployed from `website/` using the GitHub Pages workflow:
`https://anubprad.github.io/crane/`.

After a stable GitHub Release exists, Windows users can bootstrap the installer from PowerShell:

```powershell
irm https://anubprad.github.io/crane/install.ps1 | iex
```

The bootstrap retrieves the latest stable release from the GitHub Releases API, downloads the manifest, checksum list, payload and lifecycle scripts into a temporary directory, checks the release/platform metadata, then calls the existing local-directory installer. The installer verifies SHA-256 values before activation. The bootstrap refuses missing assets, unexpected download hosts, mismatched platform/version metadata and unexpected signature metadata. Temporary download files are removed after the attempt.

The publishing workflow is triggered by pushing a semantic-version tag such as `v0.1.0`. It sets up the pinned Go and Python toolchains, builds the Windows x64 package on a Windows GitHub Actions runner, installs that exact package into an isolated temporary root, runs version/doctor/task/result/uninstall smoke checks, and only then attaches the versioned package and metadata to the GitHub Release. Do not publish a tag until its source commit is intended for release.

**Trust limitation:** artifacts remain unsigned until legitimate Authenticode signing credentials are configured. SHA-256 checks detect corruption/tampering relative to the downloaded manifest; they do not authenticate the publisher. The first public release also requires the GitHub Pages deployment to complete successfully and the public URL to be verified.

## Release source

For controlled/manual packaging, the release source can still be supplied explicitly as a local directory or a controlled HTTPS base URL. Remote sources are rejected unless remote use is explicitly enabled for that command.

```powershell
.\scripts\install.ps1 -Action Install -ReleaseSource .\build\release\0.1.0
# or, for an operator-controlled remote source only:
$env:CRANE_ALLOW_REMOTE_RELEASE_SOURCE = '1'
$env:CRANE_RELEASE_SOURCE = 'https://<your-controlled-release-host>/crane'
```

`-ReleaseSource`/`CRANE_RELEASE_SOURCE` accepts either an existing local directory or an `http(s)` base URL. A remote URL requires `CRANE_ALLOW_REMOTE_RELEASE_SOURCE=1`; without it the installer fails before any download. For a remote source, the installer fetches `release-manifest.json`, `SHA256SUMS.txt`, the payload, and the lifecycle scripts from that source. The website bootstrap downloads assets first and invokes the installer with the local temporary directory, so it does not need the remote opt-in.

## Build a release

```powershell
.\scripts\build-release.ps1 -Version 0.1.0
.\scripts\build-release.ps1 -Version 0.1.1
.\scripts\build-release.ps1 -Version 0.1.2
```

For reproducible/evidence builds, pass explicit `-Commit`, `-BuildDate`, `-PythonVersion`, `-GoVersion`, and `-PythonEmbedZip` so metadata does not depend on the current checkout state. `build-release.ps1` uses tracked CRANE app files only (`git ls-files`), excludes `_init__.py`, installs pinned dependencies with `--no-compile`, removes `__pycache__`/`.pyc`/`.pyo`/`*.dist-info/RECORD` and package `bin`/`Scripts` directories, stages in a deterministic temporary path, normalizes timestamps, and writes deterministic ZIP/manifest/SHA256SUMS artifacts. Use `scripts/test-release-reproducibility.ps1` to verify byte-for-byte identical outputs for identical inputs.

Each release directory contains:

```text
build/release/<version>/
  crane-windows-x64-<version>.zip   # agent.exe + private Python + CRANE app
  release-manifest.json             # schema/version/platform/build metadata + artifact hashes
  SHA256SUMS.txt                    # independent checksum list
  install.ps1
  uninstall.ps1
  python-requirements.lock.txt
```

The manifest records `schema_version`, `version`, `channel`, `os`, `arch`, `python_version`, `go_version`, `commit`, `build_date`, `signature`, and `artifacts`. Generated artifacts live under ignored `build/` and are not committed.

## Installed layout

Executables and runtime are separated from user data.

```text
%LOCALAPPDATA%\Programs\CRANE\
  bin\
    agent.cmd                       # stable launcher
    install.ps1
    uninstall.ps1
  versions\
    <version>\
      agent.exe
      install.json
      runtime\python\               # private CPython 3.12.7
      runtime\app\                  # CRANE Python application and pinned dependencies
  current.txt                       # active version pointer
  previous.txt                      # rollback version pointer
  staging\                          # transient update/rollback work

%LOCALAPPDATA%\CRANE\
  config\
  data\crane.db
  auth\token.json                   # DPAPI-protected local API credential
  logs\
  mcp\
  tmp\
  cache\
```

The installer adds `%LOCALAPPDATA%\Programs\CRANE\bin` to the user PATH. `CRANE_SKIP_PATH_UPDATE=1` and `-SkipPathUpdate` are test-only switches used by the isolated harness; production installation updates PATH by default.

## Commands

```powershell
agent                               # start/attach the local supervisor and API
agent run "do the task"
agent status <task-id>
agent result <task-id>
agent cancel <task-id>
agent doctor
agent update -source <dir-or-url> [-version <version>]
agent uninstall
agent uninstall --remove-data --confirm REMOVE-CRANE-DATA
```

PowerShell equivalents:

```powershell
& "$env:LOCALAPPDATA\Programs\CRANE\bin\install.ps1" -Action Update -ReleaseSource <dir-or-url> -Version 0.1.1
& "$env:LOCALAPPDATA\Programs\CRANE\bin\install.ps1" -Action Rollback
& "$env:LOCALAPPDATA\Programs\CRANE\bin\uninstall.ps1"
```

## Verification performed by the installer

1. Validates Windows x64 and Windows 10+.
2. Reads the manifest and requires schema version 1, Windows x64 metadata, complete build metadata, and exact unsigned/unverified signature state.
3. Cross-checks `SHA256SUMS.txt` against the manifest.
4. Verifies each artifact's size and SHA-256 before use.
5. Extracts the payload with path-traversal and reparse-point checks.
6. Checks `install.json` against the manifest and runs `agent.exe --version`.
7. Runs packaged Python `--health` and imports LangChain, LangGraph, MCP adapters, Ollama, and `jsonschema` from the private runtime.
8. Stages the payload, switches version pointers, and then runs packaged runtime health checks and `agent doctor`.
9. On a failed update after the pointer switch, restores the previous active version.
10. Refuses downgrades and rejects remote release sources unless `CRANE_ALLOW_REMOTE_RELEASE_SOURCE=1` is set.

## Update, rollback, and uninstall

- Update stages a verified payload into `staging\`, switches `current.txt`/`previous.txt`, preserves `%LOCALAPPDATA%\CRANE`, and keeps the previous version directory.
- Rollback restores the previous binaries without changing user data and keeps the newer version available for forward recovery.
- Uninstall stops CRANE-managed processes, removes `bin\`, `versions\`, `staging\`, and version pointers, and removes only CRANE's PATH entry. User data is preserved by default.
- Data removal requires both `-RemoveData`/`--remove-data` and the exact confirmation token `REMOVE-CRANE-DATA`.
- Detached execution lets the invoking CLI exit before installed binaries are deleted.

## Acceptance evidence

Run:

```powershell
.\scripts\acceptance-windows.ps1 -ReleaseRoot .\build\release
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\acceptance-windows.ps1 -ReleaseRoot .\build\release
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\acceptance-windows.ps1 -ReleaseRoot .\build\release -MinimalEnvironment
```

The harness uses a fake `LOCALAPPDATA`, local releases `0.1.0`–`0.1.2`, and writes `acceptance.log` under `%TEMP%\crane-acceptance-<id>\`. It temporarily changes the real user PATH for the final PATH test and restores it in `finally`; do not run concurrently.

Current clean-runner evidence should be recorded from GitHub-hosted Windows CI runs (workflow, run URL, HEAD SHA, artifact version/hash metadata). Historical dev-host evidence from earlier commits is not current-release proof and should not be treated as clean-machine acceptance.

This is **not** clean physical Windows 10/11 x64 acceptance and does not prove Authenticode publisher authenticity or an official controlled host.

## Clean-machine validation procedure

Run on genuine clean Windows 10 x64 and Windows 11 x64 machines/VMs with no Go, Python, Node.js, npm, CRANE repository, or development dependencies:

1. Obtain a verified release directory from a controlled source. Confirm `release-manifest.json` and `SHA256SUMS.txt` match the artifact hashes and record signature state (`signature.scheme=none`, `signature.status=unverified` means integrity checks only).
2. Install: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Action Install -ReleaseSource <dir-or-url>`.
3. From a fresh terminal, run `agent --version`, `agent doctor`, `agent run "clean machine acceptance"`, `agent status <id>`, and `agent result <id>`.
4. Validate supervisor start/stop, authenticated loopback API availability, packaged Python/SQLite operation, task persistence across restart, and PowerShell 7.x behavior.
5. Validate update to a newer verified release, rollback, failed update rollback, default uninstall (data preserved), and explicit confirmed data removal (`REMOVE-CRANE-DATA`).
6. Record OS build, PowerShell versions, artifact versions/hashes/build commits, logs, and failures. Do not claim release readiness if any required step cannot be completed.

## Hosting readiness (configurable, not official)

- A controlled host must serve versioned, immutable artifacts and matching metadata: payload ZIP, `release-manifest.json`, `SHA256SUMS.txt`, `install.ps1`, `uninstall.ps1`, and `python-requirements.lock.txt`.
- Use `https` with verified host ownership, TLS, and access control for release publication/update paths. Do not invent or document an official CRANE domain.
- Installer/update must fail safely on missing/tampered/unsupported metadata and must never fall back to unverified downloads.
- Because artifacts are unsigned, the host/control plane must not be presented as providing Authenticode trust.

## Release requirements still pending

- Legitimate Authenticode signing certificate/credentials and verification enforced in release validation. Without them, CRANE can claim checksum/integrity validation only, not publisher authenticity.
- Controlled official release host/domain with verified ownership and a documented install command.
- Clean physical Windows 10/11 x64 acceptance (PowerShell 5.1 and PowerShell 7.x) with recorded evidence.
- Cross-environment reproducibility evidence, or documented bounded nondeterminism for uncontrolled environment differences.
- Packaged-install approval persistence/recovery and packaged restart-recovery evidence.
- SBOM generation and database migration compatibility testing across packaged upgrades.

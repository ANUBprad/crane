# CRANE — Windows Distribution and Installation

## Goal

Install CRANE on Windows 10/11 x64 with PowerShell, then invoke `agent` from any directory. End users must not need Go, Python, Node.js, Docker, Redis, or a source checkout.

This is a per-user PowerShell bootstrapper, not an MSI or graphical installer.

## Current status

Implemented and validated on this Windows 11 Home Single Language 26H2 x64 development host (`10.0.26300.9974`, Go/Python/Node/npm installed). Rebuilt unsigned release artifacts `0.1.0`–`0.1.2` were produced from commit `1953963d21398940370787eb247c6c0a04e76487` (build date `2026-11-21T00:00:56+05:30`, Windows x64, Go `go1.26.5`, Python `3.12.7`) and are available under ignored `build/release/<version>/`.

Validated in isolated harness runs using fake `LOCALAPPDATA`:

- `scripts/build-release.ps1` builds versioned release trees with tracked app staging, `--no-compile`, packaged runtime cleanup/normalization, and no `crane/mcp_runtime/_init__.py` in payloads.
- `scripts/install.ps1` performs verified Install, Update, and Rollback; `scripts/uninstall.ps1` performs detached, data-preserving uninstall.
- `scripts/acceptance-windows.ps1` passed under Windows PowerShell 5.1 (`build\evidence\acceptance-ps51-1953963.log`).
- `scripts/acceptance-windows.ps1` passed under PowerShell 7.7.0 (`build\evidence\acceptance-pwsh7-1953963.log`), resolving the earlier `Get-FileHash`/`agent update` lifecycle failure.
- `scripts/acceptance-windows.ps1 -MinimalEnvironment` passed under PowerShell 7.7.0 (`build\evidence\acceptance-minimal-pwsh7-1953963.log`), proving the packaged lifecycle completed with `go`, `python`, `node`, and `npm` unavailable on `PATH`.
- Isolated checks covered install/PATH/version/doctor/supervisor start-stop/packaged Python/SQLite/authenticated loopback API/real task execution and persisted result, update, rollback, failed-update rollback, default uninstall preservation, explicit confirmed data removal, and tampered signature/hash/unsupported metadata rejection.

Not implemented or not yet validated:

- No official download domain or controlled hosted release channel is defined. Release source remains explicit (`-ReleaseSource` or `CRANE_RELEASE_SOURCE`, local path or `http(s)` only); do not publish to an uncontrolled host or claim an official URL.
- No code-signing certificate is available. Manifests explicitly declare `signature.scheme = "none"` and `signature.status = "unverified"`. The installer accepts only that explicit unsigned state; it does not claim Authenticode/signature verification.
- Genuine clean Windows 10 x64 and Windows 11 x64 machines without Go/Python/Node/npm/CRANE source are unavailable for validation on this host.
- Deterministic release packaging is now validated for identical declared inputs on this host: `scripts/test-release-reproducibility.ps1` builds the same release twice in separate temporary directories and verifies byte-for-byte identical ZIP/manifest/SHA256SUMS outputs. Prior identical `0.1.2` rebuilds on this host produced differing payload/manifest/SHA256SUMS hashes (`build\evidence\repro-0.1.2-1953963.json`) before the packaging fix; cross-environment/toolchain reproducibility remains to be validated.
- Packaged-install approval persistence/recovery and packaged restart-recovery beyond harness/task-history checks are not separately exercised by `acceptance-windows.ps1`; source/test evidence exists (`build\evidence\go-test-approval-recovery-1953963.log`) and should not be treated as clean-machine packaged proof.

## Release source

The release source is always explicit; no URL is invented.

```powershell
.\scripts\install.ps1 -Action Install -ReleaseSource .\build\release\0.1.0
# or
$env:CRANE_RELEASE_SOURCE = 'https://<your-controlled-release-host>/crane'
```

`-ReleaseSource`/`CRANE_RELEASE_SOURCE` accepts either an existing local directory or an `http(s)` base URL. The installer fetches `release-manifest.json`, `SHA256SUMS.txt`, the payload, and the lifecycle scripts from that source.

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
8. Switches version pointers only after staging and health checks succeed, then runs `agent doctor`.
9. On a failed update after the pointer switch, restores the previous active version.
10. Refuses downgrades.

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

Latest verified results for rebuilt `1953963` artifacts:

- Windows PowerShell 5.1: **all isolated checks passed** (`build\evidence\acceptance-ps51-1953963.log`).
- PowerShell 7.7.0: **all isolated checks passed** (`build\evidence\acceptance-pwsh7-1953963.log`).
- PowerShell 7.7.0 with `-MinimalEnvironment`: **all isolated checks passed**; `go`/`python`/`node`/`npm` were absent from `PATH` and lifecycle completed (`build\evidence\acceptance-minimal-pwsh7-1953963.log`).
- Real Ollama + MCP end-to-end acceptance (source/runtime e2e script, `llama3.2:3b`): **PASS** (`build\evidence\ollama-mcp-e2e-1953963.log`).
- Go approval/recovery/restart policy tests: **PASS** (`build\evidence\go-test-approval-recovery-1953963.log`).

This is **not** clean-machine acceptance and does not prove clean Windows 10/11 behavior, signed artifacts, or an official controlled host.

## Clean-machine validation procedure (unverified on this project)

Run on genuine clean Windows 10 x64 and Windows 11 x64 VMs/images with no Go, Python, Node.js, npm, CRANE repository, or development dependencies:

1. Copy a verified local release directory (or use a controlled `http(s)` release host) and confirm `release-manifest.json`/`SHA256SUMS.txt` state, including explicit unsigned/unverified signature metadata.
2. Install: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Action Install -ReleaseSource <dir-or-url>`.
3. From a fresh terminal, run `agent --version`, `agent doctor`, `agent run "clean machine acceptance"`, `agent status <id>`, and `agent result <id>`.
4. Validate supervisor start/stop behavior, authenticated loopback API availability, packaged Python/SQLite operation, task persistence across CLI/process restart, approval persistence/recovery, MCP stdio behavior, and PowerShell 7.x with the same steps.
5. Validate update to a newer verified release, rollback, failed update rollback, default uninstall (data preserved), and explicit confirmed data removal (`REMOVE-CRANE-DATA`).
6. Record OS build, PowerShell versions, artifact hashes, logs, and any failures. Do not claim release readiness if any step cannot be completed.

## Hosting readiness (configurable, not official)

- A controlled host must serve versioned, immutable artifacts and matching metadata: payload ZIP, `release-manifest.json`, `SHA256SUMS.txt`, `install.ps1`, `uninstall.ps1`, and `python-requirements.lock.txt`.
- Use `https` with verified host ownership, TLS, and access control for release publication/update paths. Do not invent or document an official CRANE domain.
- Installer/update must fail safely on missing/tampered/unsupported metadata and must never fall back to unverified downloads.
- Because artifacts are unsigned, the host/control plane must not be presented as providing Authenticode trust.

## Release requirements still pending

- Signing certificate and Authenticode-signed executable/manifest (or an equivalent verifiable signing scheme) with verification enforced in release validation.
- Official, controlled release host and documented install command with verified ownership.
- Clean Windows 10/11 x64 acceptance (PowerShell 5.1 and PowerShell 7.x).
- Reproducible release builds or documented, bounded non-determinism.
- Packaged-install approval persistence/recovery and packaged restart-recovery evidence.
- SBOM generation and database migration compatibility testing across packaged upgrades.

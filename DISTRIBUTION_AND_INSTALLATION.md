# CRANE — Windows Distribution and Installation

## Goal

Install CRANE on Windows 10/11 x64 with PowerShell, then invoke `agent` from any directory. End users must not need Go, Python, Node.js, Docker, Redis, or a source checkout.

This is a per-user PowerShell bootstrapper, not an MSI or graphical installer.

## Current status

Implemented and validated in an isolated Windows x64 development environment:

- `scripts/build-release.ps1` builds a versioned release tree under `build/release/<version>/`.
- `scripts/install.ps1` performs verified Install, Update, and Rollback.
- `scripts/uninstall.ps1` performs detached, data-preserving uninstall.
- `scripts/acceptance-windows.ps1` runs the isolated lifecycle acceptance suite.
- `requirements.lock.txt` pins every production Python dependency with a SHA-256 hash.

Not implemented or not yet validated:

- No official download domain or hosted release channel is defined.
- No code-signing certificate is available. Manifests explicitly declare `signature.scheme = "none"` and `signature.status = "unverified"`. The installer accepts only that explicit unsigned state; it does not claim signature verification.
- Clean Windows 10 x64 and Windows 11 x64 machines have not been tested.
- PowerShell 7 has not been separately validated; Windows PowerShell 5.1 was used.

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
.\scripts\acceptance-windows.ps1
```

The harness uses a fake `LOCALAPPDATA`, generated local releases `0.1.0`, `0.1.1`, and `0.1.2`, and writes `acceptance.log` under `%TEMP%\crane-acceptance-<id>\`. The latest run passed all isolated checks, including install, doctor, task submission/status/result, update, rollback, default and confirmed uninstall, tampered checksum/manifest rejection, unsupported platform rejection, failed-update recovery, and exact user PATH add/remove/restore.

This is not a clean-machine acceptance. Actual Windows 10 and Windows 11 x64 validation remains outstanding.

## Release requirements still pending

- Signing certificate and signed manifest/executable.
- Official, controlled release host and documented install command.
- Clean Windows 10/11 x64 acceptance.
- PowerShell 7 acceptance.
- SBOM generation and database migration compatibility testing.

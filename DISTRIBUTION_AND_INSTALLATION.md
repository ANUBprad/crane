# CRANE — Windows Distribution and Installation

## Goal

Install CRANE with a PowerShell bootstrap command, then invoke `agent` from any directory. End users must not need to clone the source repository or install Go, Python, Redis or Docker.

This is a CLI bootstrapper, not a graphical installer or MSI wizard.

## Intended user experience

The future install command is expected to resemble:

```powershell
irm https://<CRANE-DISTRIBUTION-DOMAIN>/install.ps1 | iex
```

**This URL is a placeholder, not a working endpoint.** Do not publish it as an install command until the official domain, TLS, bootstrap script, release artifacts and signature verification exist. The production bootstrap should download and verify a signed installer rather than execute unverified content piped from a network response.

Then:

```powershell
agent
agent run "..."
```

## Initial support matrix

- Windows 10 and Windows 11.
- x64 first.
- PowerShell 5.1 and PowerShell 7 should be tested.
- Per-user installation is the default and should not require administrator rights.
- ARM64 is deferred until runtime/dependency compatibility is validated.

## Distribution contents

Release package:
- standalone Go CLI/supervisor executable (`agent.exe`);
- packaged Python runtime;
- CRANE Python application and pinned dependencies;
- LangChain and LangGraph dependencies;
- MCP SDK/adapters;
- SQLite integration;
- required native libraries/runtime assets.

Do not bundle large LLM weights. Ollama is optional and can be installed separately; model weights are downloaded separately by the user.

## Bootstrap responsibilities

1. Validate supported Windows version and architecture.
2. Resolve stable release metadata from the official endpoint.
3. Verify metadata signature and artifact checksums.
4. Install into a per-user application directory.
5. Create/update a stable launcher and PATH entry.
6. Preserve user configuration and data.
7. Fail safely and clean up incomplete installations.
8. Be idempotent.

## Update and rollback

- `agent update` checks a signed release manifest.
- Download and verify before switching versions.
- Use an atomic or recoverable version switch.
- Preserve database, configuration, secrets and artifacts.
- Keep a known-good version for recovery.
- Test database migration compatibility.
- Do not silently downgrade or discard user data.

## Uninstall

- Remove CRANE binaries/runtime components.
- Remove PATH changes made by CRANE.
- Stop CRANE-managed background processes.
- Preserve user data by default.
- Require a separate explicit option to delete data.
- Never delete unrelated files/directories.

## Proposed directory layout

```text
%LOCALAPPDATA%\\Programs\\CRANE\\
  bin/
  versions/
  current/

%LOCALAPPDATA%\\CRANE\\
  config/
  data/
  logs/
  artifacts/
  downloads/
  backups/
```

Separate executable/runtime files from user data so updates and uninstall do not remove task history/configuration.

## Release requirements

- Reproducible or traceable build process.
- Versioned artifacts.
- Signed manifest and checksums.
- Windows executable signing when available.
- Dependency inventory/SBOM.
- Clean-machine install test.
- Upgrade, rollback and uninstall tests.
- Clearly document that the bootstrap requires internet unless an offline package is provided.

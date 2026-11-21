# CRANE — Windows Acceptance Tests

A Linux build is not proof of Windows readiness. This checklist distinguishes behavior verified in isolated Windows x64 harness runs on this development host from requirements that still need genuine clean supported machines.

## Evidence

Host: Windows 11 Home Single Language 26H2 x64 (`10.0.26300.9974`) with Go/Python/Node/npm installed. Rebuilt unsigned artifacts `0.1.0`–`0.1.2` from commit `1953963d21398940370787eb247c6c0a04e76487` (`build/release/*/release-manifest.json`, build date `2026-11-21T00:00:56+05:30`, Windows x64, Go `go1.26.5`, Python `3.12.7`, signature `none`/`unverified`). Manifest/SHA256SUMS checksums matched computed artifact hashes.

`scripts/acceptance-windows.ps1` uses local releases, a fake `LOCALAPPDATA`, and writes `%TEMP%\crane-acceptance-<id>\acceptance.log`. The final PATH test temporarily changes the real user PATH and restores it in `finally`; runs must be sequential.

Verified isolated results for rebuilt `1953963` artifacts:

- **Windows PowerShell 5.1:** all isolated checks passed — `build\evidence\acceptance-ps51-1953963.log`.
- **PowerShell 7.7.0:** all isolated checks passed — `build\evidence\acceptance-pwsh7-1953963.log`.
- **PowerShell 7.7.0, `-MinimalEnvironment`:** all isolated checks passed with `go`/`python`/`node`/`npm` absent from `PATH` — `build\evidence\acceptance-minimal-pwsh7-1953963.log`.
- **Real Ollama + MCP e2e (`llama3.2:3b`, source/runtime script):** PASS — `build\evidence\ollama-mcp-e2e-1953963.log`.
- **Go approval/recovery/restart policy tests:** PASS — `build\evidence\go-test-approval-recovery-1953963.log` (`TestMCPSubmissionContextAndToolResultPersistence`, `TestStartupRecoveryPolicies`, cancel/resume/waiting-approval cases).
- **Release reproducibility:** Deterministic packaging and `scripts/test-release-reproducibility.ps1` added; two independent builds with identical declared inputs produced byte-for-byte identical ZIP/manifest/SHA256SUMS outputs on this host (PASS). Prior identical `0.1.2` rebuilds differed as recorded in `build\evidence\repro-0.1.2-1953963.json`; cross-environment/toolchain reproducibility remains to be validated.

These results are isolated-harness evidence, not clean Windows 10/11 x64 acceptance, signed-release validation, or controlled-host publication proof.

## Installation and CLI

- [x] Isolated Windows x64 install with verified local release source (PowerShell 5.1, PowerShell 7.7.0).
- [x] `agent --version`, `agent run`, `agent status`, `agent result`, `agent doctor`, `agent update`, `agent uninstall` invoked from the installed launcher in isolated harnesses.
- [x] Install creates `bin\`, `versions\`, `staging\`, `current.txt`, and all `%LOCALAPPDATA%\CRANE` data directories.
- [x] Private CPython 3.12.7 reports healthy and imports all pinned production packages.
- [x] Per-user install requires no administrator rights in the isolated harness.
- [x] Unsupported OS/architecture metadata is rejected.
- [x] Installer PATH entry is added and removed exactly in a controlled test.
- [x] PowerShell 7.7.0 isolated install/lifecycle path tested (including prior `Get-FileHash`/update fix verification).
- [x] Packaged lifecycle completed in minimal-PATH isolated harness without `go`/`python`/`node`/`npm` on `PATH` (`-MinimalEnvironment`).
- [ ] Clean Windows 10 x64 install (no Go/Python/Node/npm/CRANE repo/dev dependencies).
- [ ] Clean Windows 11 x64 install (no Go/Python/Node/npm/CRANE repo/dev dependencies).
- [ ] `agent` resolves from a genuinely fresh terminal after PATH propagation.
- [ ] Unsupported physical architecture fails safely on real ARM64/x86 hardware.
- [ ] Interrupted install leaves no broken PATH or partial active version.

## Runtime and task lifecycle

- [x] Submit task and receive a durable task ID.
- [x] Query status and result from a new CLI process.
- [x] Task history survives update and rollback in the isolated packaged harness.
- [x] Live MCP controller is reachable after task execution.
- [x] Packaged `agent doctor` reports active release, Python, SQLite, MCP modules, and real overall status.
- [x] Failed update after pointer switch restores the previous active version and binaries.
- [x] Approval persistence/recovery and restart recovery policies validated by Go tests (`build\evidence\go-test-approval-recovery-1953963.log`); packaged-install-specific restart/approval recovery remains unverified.
- [ ] Task continues after terminal/client disconnect in the packaged installation.
- [ ] Cancel a running task in the packaged installation.
- [ ] Runtime restart recovers eligible queued/running tasks in the packaged installation (explicit packaged evidence).
- [ ] Uncertain external side effects are not blindly replayed in the packaged installation.
- [ ] Concurrent tasks obey configured limits in the packaged installation.

## MCP

- [x] MCP modules and supervisor controller health verified in the packaged runtime.
- [x] Prior Windows source-tree validation connected to real local stdio and Streamable HTTP MCP servers and enforced exact approval fingerprints.
- [x] Real Ollama + MCP tool flow (stdio test server, discovery/invoke, blocked consequential action, exact approval grant, task tool call, persisted invocation) passed in the source/runtime e2e script with `llama3.2:3b` (`build\evidence\ollama-mcp-e2e-1953963.log`); packaged-install MCP revalidation remains separate.
- [ ] MCP lifecycle revalidated from the packaged installation against real local servers.
- [ ] Reconnect behavior, malformed schemas, and timeout isolation revalidated from the packaged installation.

## Agent orchestration and offline behavior

- [x] Prior Windows validation executed a real `llama3.2:3b` Ollama tool call through LangGraph and SQLite with no fallback.
- [x] Packaged runtime default local mode executes a real bounded workflow and persists the result in isolated harnesses.
- [x] Real Ollama + MCP task execution validated in source/runtime e2e with `llama3.2:3b` (`build\evidence\ollama-mcp-e2e-1953963.log`).
- [ ] Packaged Ollama path validated with a local model from the installed release.
- [ ] Network-disconnected execution validated on a clean machine.
- [ ] Hosted fallback absence revalidated from the packaged installation.

## Permissions and security

- [x] Tampered manifest signature metadata is rejected.
- [x] Tampered payload checksum is rejected.
- [x] Unsupported platform metadata is rejected.
- [x] Uninstall preserves user data by default.
- [x] Explicit data removal requires `REMOVE-CRANE-DATA` and removes only CRANE-managed data.
- [x] Local API credential state survives update and rollback.
- [ ] Tampered executable with a valid-looking manifest is rejected (requires Authenticode signing and verification).
- [ ] Unauthorized API caller and traversal inputs revalidated from the packaged installation.

## Maintenance

- [x] Update preserves data, credentials, logs, MCP config, temp/cache, and task history.
- [x] Previous version is retained for rollback.
- [x] Rollback preserves current user data and retains the newer version for forward recovery.
- [x] Uninstall removes binaries, runtime, version pointers, and PATH entry.
- [x] Confirmed uninstall removes CRANE-managed data.
- [x] Release artifact integrity metadata (manifest/SHA256SUMS) internally consistent for rebuilt `0.1.0`–`0.1.2`; payloads contain no sampled secrets/credentials/user data/model weights and no `crane/mcp_runtime/_init__.py`.
- [x] Deterministic release builds verified for identical declared inputs on this host via `scripts/test-release-reproducibility.ps1` (ZIP/manifest/SHA256SUMS byte-identical); prior differing `0.1.2` rebuilds recorded in `build\evidence\repro-0.1.2-1953963.json`. Cross-environment/toolchain reproducibility remains to be validated.
- [ ] Database migration compatibility tested across a real version upgrade.
- [ ] Signing and artifact provenance validated with a real certificate and controlled release host.

## Resource and stability

- [ ] Idle CPU and memory measured.
- [ ] Repeated task submission process/handle leak check.
- [ ] Long-running task memory bounded.
- [ ] Large tool output bounded/streamed safely.
- [ ] SQLite concurrent access and disk-full/permission-denied scenarios tested.

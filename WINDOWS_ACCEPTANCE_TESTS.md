# CRANE — Windows Acceptance Tests

A Linux build is not proof of Windows readiness. This checklist distinguishes behavior verified in an isolated Windows x64 development environment from requirements that still need clean supported machines.

## Evidence

`scripts/acceptance-windows.ps1` generates local releases `0.1.0`, `0.1.1`, and `0.1.2` under ignored `build/release/`, uses a fake `LOCALAPPDATA`, and writes `%TEMP%\crane-acceptance-<id>\acceptance.log`.

Latest run: **all isolated checks passed**. It exercised the packaged CLI, private Python runtime, durable task lifecycle, update/rollback/uninstall, and failure/rollback paths without modifying the real CRANE data directory. The final PATH check temporarily changes the real user PATH, verifies exact add/remove behavior, and restores the original value in a `finally` block.

## Installation and CLI

- [x] Isolated Windows x64 install with verified local release source.
- [x] `agent --version`, `agent run`, `agent status`, `agent result`, `agent doctor`, `agent update`, `agent uninstall` invoked from the installed launcher.
- [x] Install creates `bin\`, `versions\`, `staging\`, `current.txt`, and all `%LOCALAPPDATA%\CRANE` data directories.
- [x] Private CPython 3.12.7 reports healthy and imports all pinned production packages.
- [x] Per-user install requires no administrator rights in the isolated harness.
- [x] Unsupported OS/architecture metadata is rejected.
- [x] Installer PATH entry is added and removed exactly in a controlled test.
- [ ] Clean Windows 10 x64 install.
- [ ] Clean Windows 11 x64 install.
- [ ] PowerShell 7 install path tested (Windows PowerShell 5.1 was used).
- [ ] `agent` resolves from a genuinely fresh terminal after PATH propagation.
- [ ] Unsupported physical architecture fails safely on real ARM64/x86 hardware.
- [ ] Interrupted install leaves no broken PATH or partial active version.

## Runtime and task lifecycle

- [x] Submit task and receive a durable task ID.
- [x] Query status and result from a new CLI process.
- [x] Task history survives update and rollback.
- [x] Live MCP controller is reachable after task execution.
- [x] Packaged `agent doctor` reports active release, Python, SQLite, MCP modules, and real overall status.
- [x] Failed update after pointer switch restores the previous active version and binaries.
- [ ] Task continues after terminal/client disconnect in the packaged installation.
- [ ] Cancel a running task in the packaged installation.
- [ ] Runtime restart recovers eligible queued/running tasks in the packaged installation.
- [ ] Uncertain external side effects are not blindly replayed in the packaged installation.
- [ ] Concurrent tasks obey configured limits in the packaged installation.

## MCP

- [x] MCP modules and supervisor controller health verified in the packaged runtime.
- [x] Prior Windows source-tree validation connected to real local stdio and Streamable HTTP MCP servers and enforced exact approval fingerprints.
- [ ] MCP lifecycle revalidated from the packaged installation against real local servers.
- [ ] Reconnect behavior, malformed schemas, and timeout isolation revalidated from the packaged installation.

## Agent orchestration and offline behavior

- [x] Prior Windows validation executed a real `llama3.2:3b` Ollama tool call through LangGraph and SQLite with no fallback.
- [x] Packaged runtime default local mode executes a real bounded workflow and persists the result.
- [ ] Packaged Ollama path validated with a local model.
- [ ] Network-disconnected execution validated on a clean machine.
- [ ] Hosted fallback absence revalidated from the packaged installation.

## Permissions and security

- [x] Tampered manifest signature metadata is rejected.
- [x] Tampered payload checksum is rejected.
- [x] Unsupported platform metadata is rejected.
- [x] Uninstall preserves user data by default.
- [x] Explicit data removal requires `REMOVE-CRANE-DATA` and removes only CRANE-managed data.
- [x] Local API credential state survives update and rollback.
- [ ] Tampered executable with a valid-looking manifest is rejected (requires signing).
- [ ] Unauthorized API caller and traversal inputs revalidated from the packaged installation.

## Maintenance

- [x] Update preserves data, credentials, logs, MCP config, temp/cache, and task history.
- [x] Previous version is retained for rollback.
- [x] Rollback preserves current user data and retains the newer version for forward recovery.
- [x] Uninstall removes binaries, runtime, version pointers, and PATH entry.
- [x] Confirmed uninstall removes CRANE-managed data.
- [ ] Database migration compatibility tested across a real version upgrade.
- [ ] Signing and artifact provenance validated with a real certificate.

## Resource and stability

- [ ] Idle CPU and memory measured.
- [ ] Repeated task submission process/handle leak check.
- [ ] Long-running task memory bounded.
- [ ] Large tool output bounded/streamed safely.
- [ ] SQLite concurrent access and disk-full/permission-denied scenarios tested.

# CRANE — Windows Acceptance Tests

A Linux build is not proof of Windows readiness. Execute this checklist on actual supported Windows environments.

## Installation and CLI

- [ ] Clean Windows 10 x64 install using the published bootstrap.
- [ ] Clean Windows 11 x64 install using the published bootstrap.
- [ ] PowerShell 5.1 install path tested.
- [ ] PowerShell 7 install path tested.
- [ ] No Go or Python installation required.
- [ ] `agent` resolves from a fresh terminal.
- [ ] CLI works from unrelated working directories.
- [ ] Per-user install succeeds without administrator rights.
- [ ] Unsupported OS/architecture fails safely with a useful message.
- [ ] Interrupted install leaves no broken PATH or partial active version.

## Runtime and task lifecycle

- [ ] Submit task and receive a task ID.
- [ ] Task continues after terminal/client disconnect.
- [ ] Query status from a new terminal.
- [ ] Retrieve result after completion.
- [ ] Cancel a running task.
- [ ] Runtime restart recovers eligible queued/running tasks.
- [ ] Uncertain external side effects are not blindly replayed.
- [ ] Concurrent tasks obey configured limits.
- [ ] Shutdown cleans up CRANE-owned child processes.

## MCP

- [ ] Connect to a local stdio MCP server.
- [ ] Connect to a Streamable HTTP MCP server.
- [ ] Discover tools and schemas dynamically.
- [ ] Invoke a read-only tool.
- [ ] Invoke a mutating tool only after policy approval where required.
- [ ] Timeout/failure of one server does not block other servers.
- [ ] Reconnect behavior is bounded.
- [ ] Malformed schemas/responses produce actionable errors.
- [ ] Server configuration changes take effect without reinstalling CRANE.

## Agent orchestration

- [ ] LangChain provider integration works with configured provider.
- [ ] LangGraph executes a multi-step workflow.
- [ ] Workflow state/checkpoints persist.
- [ ] Retry limits and maximum graph steps are enforced.
- [ ] Cancellation propagates to active work.
- [ ] Provider failure is reported without silent substitution.

## Offline and privacy

- [ ] Local model workflow succeeds with network disconnected.
- [ ] Local MCP workflow succeeds offline.
- [ ] Local task data remains local in local-only mode.
- [ ] Hosted provider fallback does not occur when local provider fails.
- [ ] Remote dependency failure is reported accurately.
- [ ] Logs and diagnostics redact secrets.

## Permissions and security

- [ ] Read-only action follows low-friction policy.
- [ ] Destructive action requests approval by default.
- [ ] Denied action does not execute.
- [ ] Approval survives runtime restart.
- [ ] Altered action arguments invalidate prior approval.
- [ ] Local API is not exposed to LAN by default.
- [ ] Unauthorized API caller is rejected.
- [ ] Unsafe executable paths and path traversal are rejected.
- [ ] Tampered release artifact is rejected.

## Maintenance

- [ ] `agent doctor` reports useful diagnostics.
- [ ] Update preserves user data.
- [ ] Failed update can recover prior version.
- [ ] Uninstall stops managed processes and removes binaries.
- [ ] Uninstall preserves user data by default.
- [ ] Explicit data removal affects only CRANE-managed data.
- [ ] Database migrations are tested across upgrades.

## Resource and stability

- [ ] Idle CPU and memory are measured and acceptable.
- [ ] Repeated task submission does not leak processes/handles.
- [ ] Long-running task memory remains bounded.
- [ ] Large tool output is bounded or streamed safely.
- [ ] SQLite concurrent access/locking behavior is tested.
- [ ] Disk-full and permission-denied scenarios fail safely.

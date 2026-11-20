# CRANE — Technical Specification

## Technology choices

| Area | Technology | Responsibility |
|---|---|---|
| CLI/supervisor | Go | Executable, lifecycle, process control, API |
| Agent runtime | Python | Agent execution |
| Model/tool abstraction | LangChain | Providers, tool interfaces, structured outputs |
| Workflow orchestration | LangGraph | Stateful graph execution, routing, retries, checkpoints |
| Tool protocol | MCP | Dynamic local/remote tool integration |
| Persistence | SQLite | Local durable state |
| Local model runtime | Ollama initially | Optional local inference |
| Distribution | Go executable + packaged Python runtime/dependencies | Standalone Windows CLI |

Use maintained SDKs/adapters where practical, including the Python MCP SDK and LangChain MCP adapters. Pin and audit dependencies. Do not implement MCP protocol behavior from scratch unless a documented gap requires it.

## Runtime process model

- `agent.exe` is the user-facing entry point.
- The Go supervisor handles CLI commands and manages background execution.
- Python is packaged as an application runtime; system Python is not a user prerequisite.
- Runtime ownership, health checks, shutdown and stale-process handling must be explicit.
- Prefer per-user operation without administrator privileges.

Implemented development behavior: `agent` and `agent serve` start the supervisor and the fixed `127.0.0.1:8787` API. Health uses a bounded `python -m crane.python_runtime --health` probe, with `CRANE_PYTHON` and `CRANE_PYTHON_HOME` overrides for development. Packaged Python remains a target, not a current deliverable.

## MCP behavior

Initial transports:
- **stdio:** start and supervise a configured local MCP server process.
- **Streamable HTTP:** connect to a configured HTTP endpoint.

Requirements:
- Dynamic add/update/remove/enable/disable.
- Connection and request timeouts.
- Tool discovery and schema validation.
- Namespacing to prevent tool-name collisions.
- Per-server health and failure isolation.
- Bounded reconnection and safe shutdown.
- Secret references rather than secrets in logs/config exports.
- Explicit trust level and tool permissions.
- No trust escalation based on tool descriptions or server metadata.

## Agent workflow

LangGraph is the workflow engine; LangChain provides model and tool abstractions.

Keep the initial graph focused:
1. Accept and normalize task.
2. Load policy and authorized tools.
3. Plan/select the next bounded action.
4. Request approval when policy requires it.
5. Execute an authorized tool or produce a response.
6. Evaluate and continue/finish under explicit limits.
7. Persist final result and execution summary.

Required controls: maximum graph steps, maximum wall-clock execution time, per-tool timeout, bounded retry count/backoff, maximum concurrent tasks, cancellation propagation, context/token budgets where supported, checkpoint boundaries and handling for ambiguous or failed tool results.

Do not promise deterministic outcomes from LLM planning. Deterministic guarantees belong in policy enforcement, lifecycle, budgets, persistence and protocol validation.

## Provider and data-egress policy

- Local providers are preferred for offline/private workflows.
- Hosted providers are optional and must be explicitly configured.
- Never silently fall back from local inference to a hosted provider.
- Enforce configured data-egress policy before sending user content to hosted providers.
- Source credentials from secure local storage or environment variables.
- Report provider unavailability clearly.

## Persistence

SQLite is the default. Use transactions for lifecycle transitions and versioned migrations.

Minimum logical records: tasks, task events, checkpoint references, MCP server configuration, approvals, artifacts and schema migrations.

Store large artifacts in CRANE-managed files when appropriate. Validate managed paths against traversal and unintended external locations.

## Desktop client boundary

The optional desktop application is located at `apps/desktop` and uses Electron + React/TypeScript. Electron main-process privileges are isolated from the renderer. Context isolation, sandboxing and web security are enabled; Node integration is disabled. The preload bridge exposes only `getAppInfo`, `getHealth`, `getTasks` and `getTask`. The renderer cannot read SQLite, access arbitrary filesystem paths, launch shell commands, receive the installation token, or issue arbitrary HTTP requests.

The main process reads the protected token through the trusted `agent api-token` command, caches it in memory, retries once with a fresh token after `401`, and calls only the fixed loopback API. The desktop client reports real health, persisted tasks, task details, and explicit disconnected/recovering/error states; it never invents runtime status. Task submission, streaming, MCP servers and approvals remain disabled because the backend API does not expose them. Do not add a cloud relay or telemetry path.

## Local API and IPC

Implemented v1 behavior:

- The API binds only to `127.0.0.1:8787`. Host configuration and all-interface binding are rejected.
- Requests require an `Authorization: Bearer <token>` header. The 256-bit token is stored on Windows with DPAPI and verified in constant time.
- Protected token state is reloaded on every request, so CLI rotation or revocation affects a running server immediately.
- Only `GET` is accepted. Query strings, unexpected request bodies, transfer-encoded bodies, malformed task IDs and bodies over 1 MiB are rejected.
- Responses are JSON with `Content-Type: application/json` and `X-Content-Type-Options: nosniff`. Success uses `{"data":...}`; errors use `{"error":{"code":"...","message":"..."}}`.
- Error codes include `unauthorized`, `bad_request`, `method_not_allowed`, `not_found`, `request_too_large` and `persistence_unavailable`.
- Shutdown is bounded and clean. HTTP liveness alone never reports readiness.

Implemented endpoints:

| Method | Path | Result |
|---|---|---|
| `GET` | `/v1/health` | API version, supervisor state, separate Python runtime probe, persistence health and overall readiness |
| `GET` | `/v1/tasks` | Real persisted task records and count |
| `GET` | `/v1/tasks/{id}` | One real persisted task record |

Raw task input is deliberately omitted from task responses. Task submission, cancellation, event streaming, MCP management and approval operations are not implemented. Do not treat CORS as access control.

## CLI contract (implemented and proposed)

Implemented development commands:

```text
agent
agent serve
agent --version
agent api-token [--rotate | --revoke]
agent task create --id <id> --input <text>
```

The original proposed contract below remains planned:

```text
agent
agent run <task>
agent status <task-id>
agent result <task-id>
agent cancel <task-id>
agent logs <task-id> [--follow] [--verbose]
agent mcp list
agent doctor
agent update
agent uninstall
```

These proposed command names/flags remain provisional.

## Configuration layout (proposed)

Current local paths:

```text
%LOCALAPPDATA%\\CRANE\\
  auth\\
    token.json       # DPAPI-protected token blob and revoked token hashes
  crane.db           # SQLite task state
```

The broader `config/`, `logs/`, `artifacts/`, `runtimes/`, `downloads/` and `backups/` layout remains proposed. Keep executable/runtime files separate from user data. The installation token uses Windows DPAPI; never store raw secrets in ordinary config files.

## Error model

Errors should carry a stable code, component, safe task/server/tool context, retryability, concise user-facing message and diagnostic reference. Do not expose secrets or unsanitized stack traces in normal output.

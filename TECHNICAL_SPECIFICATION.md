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

## Local API and IPC

- Local API is for programmatic integration, not public remote access.
- Bind to loopback or use local IPC by default.
- Require local authentication for API access.
- Validate request schemas, sizes and caller permissions.
- Version the API and test compatibility.
- Do not treat CORS as access control.

## CLI contract (proposed)

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

Command names/flags are provisional until implemented.

## Configuration layout (proposed)

```text
%LOCALAPPDATA%\\CRANE\\
  config/
  data/
    crane.db
  logs/
  artifacts/
  runtimes/
  downloads/
  backups/
```

Keep executable/runtime files separate from user data. Use Windows-protected secret storage or an appropriate OS secret store; never store raw secrets in ordinary config files.

## Error model

Errors should carry a stable code, component, safe task/server/tool context, retryability, concise user-facing message and diagnostic reference. Do not expose secrets or unsanitized stack traces in normal output.

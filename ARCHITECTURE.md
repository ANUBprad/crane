# CRANE — Architecture

## System overview

CRANE has a local Go supervisor and a packaged Python agent runtime. Go owns the product process, task lifecycle and machine-level responsibilities. Python owns agent execution using LangChain, LangGraph and MCP integrations.

```text
CLI (agent) / Local API
          |
          v
     Go Supervisor
  - command handling
  - task lifecycle
  - local IPC/API
  - concurrency and scheduling
  - process supervision
  - policy coordination
          |
          | versioned local protocol
          v
   Python Agent Runtime
  - LangChain integrations
  - LangGraph workflows
  - planning and routing
  - checkpoint interaction
  - tool selection and execution
          |
          +-----------------------+
          |                       |
          v                       v
      MCP Manager            Model Provider Layer
  - server registry          - local providers
  - stdio transport          - optional hosted APIs
  - Streamable HTTP          - explicit egress policy
  - discovery/schema
  - invocation
          |                       |
          v                       v
  Local/Remote MCP tools      Local/Remote LLMs

Shared local state: SQLite + structured log/artifact files
```

## Component ownership

### Go supervisor
- Owns the user-facing `agent` executable and CLI.
- Starts, monitors and recovers the packaged Python runtime.
- Owns task admission, identifiers, lifecycle, cancellation and concurrency limits.
- Provides local API/IPC with restrictive defaults.
- Coordinates approval requests and policy outcomes.
- Owns diagnostics and install/update coordination.
- Does not duplicate LangGraph planning logic.

### Python agent runtime
- Executes workflows using LangChain and LangGraph.
- Receives versioned task envelopes from Go.
- Maintains explicit workflow state and checkpoint boundaries.
- Invokes MCP tools only through authorized integration paths.
- Emits structured events and results to the supervisor.
- Does not independently own global task identity or installation state.

### MCP registry and manager
- Stores server definitions and transport settings.
- Supports runtime registration, updates, enable/disable and removal.
- Supervises local stdio server processes and Streamable HTTP connections.
- Discovers tools and schemas dynamically.
- Applies connection/request timeouts and bounded reconnect behavior.
- Isolates failures by server.
- Never assumes a tool is safe because an MCP server exposes it.

### Workflow engine
- Uses LangGraph for stateful multi-step workflows.
- Uses bounded planning/execution loops, conditional routing, retry policy and approval nodes.
- Persists checkpoints at explicit boundaries.
- Resumes only when checkpoint and side-effect semantics permit safe continuation.
- Avoids multi-agent complexity unless a concrete workflow needs it.

### Persistence
- SQLite is the default local database.
- Store task metadata, lifecycle events, approval records and checkpoint/artifact references.
- Keep large logs and artifacts in managed files when appropriate.
- Never store secrets as plaintext in ordinary task/configuration tables.
- Version and test all database migrations.

## Task lifecycle

Initial states:

```text
QUEUED -> STARTING -> RUNNING -> SUCCEEDED
                         |       FAILED
                         |       CANCELLED
                         |       WAITING_APPROVAL
                         |              |
                         +--------------+-> RUNNING
```

Every transition must be validated and durably persisted before being reported as committed.

A task record should include a stable ID, timestamps, input reference, state, workflow/checkpoint reference, approval state, concise result/failure summary, log/artifact references and schema/runtime version metadata.

## Recovery semantics

- On startup, the supervisor reconciles non-terminal tasks with runtime/checkpoint state.
- Safely resumable work can be resumed or re-queued.
- Work with uncertain external side effects must not be blindly replayed.
- Record idempotency metadata when supported by a remote tool.
- Do not claim exactly-once execution unless the underlying operation can guarantee it.
- Make recovery decisions inspectable through task diagnostics.

## Go/Python boundary

Use a versioned local protocol. Select a Windows-compatible IPC mechanism (such as named pipes) and use a narrowly bound loopback fallback only if necessary. Do not expose the runtime publicly by default.

Initial message families: `SubmitTask`, `TaskAccepted`, `TaskEvent`, `ApprovalRequired`, `ApprovalDecision`, `TaskCompleted`, `TaskFailed`, `CancelTask` and `HealthCheck`.

Validate messages at the boundary. Do not allow arbitrary executable commands through IPC.

## Configuration and extensions

- Use a predictable per-user application-data directory, not the current working directory.
- Validate config on load and return actionable errors.
- Keep secrets separate from ordinary configuration.
- Treat MCP servers as extensions. Core must not hardcode vendors such as GitHub, browsers, databases or Kubernetes.
- Keep OS-specific behavior behind narrow adapters.

## Observability

- Write structured local logs with task/component correlation IDs.
- Keep normal CLI output concise.
- Offer explicit verbose/trace inspection.
- Redact tokens, authorization headers, secrets and sensitive tool arguments.
- Do not require a dashboard for observability.

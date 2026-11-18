# CRANE — Implementation Plan

This is a dependency-ordered delivery guide, not a mandate to create artificial phases or perform broad rewrites. Implement the next useful slice, validate it, and keep scope tight.

## Delivery order

### Windows runtime foundation
- Establish repository layout and engineering instructions.
- Implement Go CLI entry point and supervisor.
- Package and launch Python runtime without system Python dependency.
- Define/test versioned Go/Python local protocol.
- Establish SQLite schema and migrations.
- Prove per-user install layout and process lifecycle on Windows.

**Exit condition:** clean Windows machine installs and invokes the packaged CLI; Go supervisor starts, health-checks and stops packaged Python runtime.

### Durable task execution
- Implement task submission, IDs, state transitions and persistence.
- Implement status, result, cancellation and logs commands.
- Support background execution independent of invoking terminal.
- Add startup reconciliation and safe recovery.
- Add concurrency and execution budgets.

**Exit condition:** tasks survive caller disconnect and eligible tasks recover after restart without unsafe replay.

### MCP connectivity
- Dynamic server registry/configuration.
- stdio lifecycle and tool discovery.
- Streamable HTTP lifecycle and tool discovery.
- Schema validation, timeouts, bounded reconnect and server isolation.
- Per-server/tool permission metadata.

**Exit condition:** local and remote MCP tools can be discovered and invoked with bounded failure behavior.

### LangChain and LangGraph
- Integrate LangChain model/provider abstraction.
- Integrate maintained LangChain MCP adapters where suitable.
- Build initial bounded LangGraph workflow.
- Add checkpointing, retry/routing and cancellation.
- Integrate local Ollama provider.
- Enforce provider and data-egress policy.

**Exit condition:** a multi-step task uses authorized MCP tools through LangGraph, persists state and runs locally in offline mode.

### Permissions and operational hardening
- Action classification and policy evaluation.
- Durable approval/denial workflow.
- Secret handling and redaction.
- Structured logs and `agent doctor`.
- Validate local API security if included in first release.

**Exit condition:** risky operations cannot bypass policy; diagnostics are useful without leaking secrets.

### Release readiness
- Signed release manifest and bootstrapper.
- Update, rollback and uninstall.
- Full Windows acceptance suite on real Windows environments.
- Measure idle resource use and long-running stability.
- Publish only after release gates pass.

**Exit condition:** Windows release meets `WINDOWS_ACCEPTANCE_TESTS.md`.

## Platform expansion

Start macOS/Linux only after Windows passes acceptance. Keep core portable, then add OS adapters, packaging and tests.

## Deferred

- Dashboard/UI.
- Hosted multi-tenant control plane.
- Mandatory Redis/PostgreSQL/Docker.
- Bundled model weights.
- TypeScript SDK until local API contract is stable.
- Advanced multi-agent coordination until justified by a concrete workflow.

# CRANE — Product Requirements

**Status:** Product baseline  
**Initial platform:** Windows 10/11 x64  
**Primary interface:** CLI; local API for programmatic use

## Product definition

CRANE is a general-purpose, headless MCP execution and orchestration runtime. A user submits a task; CRANE coordinates authorized tools, executes a bounded workflow, persists its state, handles failures and returns a concise result.

CRANE is not a RAG application, paper-retrieval service, research chatbot, ordinary chatbot or dashboard-first workflow product.

## Problem

Agent prototypes can demonstrate tool calling but leave operational concerns to the user: server lifecycle, state durability, restart recovery, permissions, privacy, offline operation, distribution and diagnostics. CRANE provides this execution layer.

## Primary user jobs

1. Delegate repository, terminal, API, database or infrastructure work to an agent.
2. Compose workflows across MCP servers without bespoke integration glue for every tool.
3. Run private workflows using local models, local MCP servers and local storage.
4. Submit tasks and retrieve status/results through CLI or a local API.

## Core user journeys

### Install and first use
- Run the official PowerShell bootstrap once it is published.
- Install a verified standalone runtime in the user's application directory.
- Invoke `agent` from any terminal and working directory.
- Do not require users to install Go, Python, Redis or Docker.
- Treat Ollama as optional; do not bundle large model weights in the base runtime.

### Submit and retrieve work
- Submit through CLI or local API and receive a task identifier.
- Continue execution after the invoking client/terminal disconnects.
- Query task state and retrieve the result later.
- Keep detailed logs separate from concise normal output.

### MCP tools
- Configure local or remote MCP servers dynamically.
- Connect using stdio or Streamable HTTP.
- Discover tools/schemas and invoke only tools allowed by policy.
- Isolate server failures; one failed server must not take unrelated integrations down.

### Offline operation
- Execute local workflows with no internet when required local dependencies are available.
- Never silently fall back to a hosted model.
- Clearly identify operations requiring unavailable remote services.

### Approval
- Avoid approval prompts for harmless read-only actions.
- Require policy evaluation and, by default, explicit approval for destructive, sensitive, privilege-changing or externally consequential actions.
- Persist pending approvals and resume safely after a decision.

## Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-01 | CLI task submission | Must |
| FR-02 | Durable local task state | Must |
| FR-03 | Continue after caller disconnect | Must |
| FR-04 | Safe recovery after runtime restart | Must |
| FR-05 | Task status, result and cancellation commands | Must |
| FR-06 | Dynamic MCP server configuration and lifecycle | Must |
| FR-07 | MCP stdio transport | Must |
| FR-08 | MCP Streamable HTTP transport | Must |
| FR-09 | Dynamic tool/schema discovery and validation | Must |
| FR-10 | Bounded tool invocation, timeout and structured errors | Must |
| FR-11 | LangChain model/provider and tool integration | Must |
| FR-12 | LangGraph workflow orchestration and checkpointing | Must |
| FR-13 | Optional local LLM support, initially Ollama | Must |
| FR-14 | No silent hosted fallback or unauthorized data egress | Must |
| FR-15 | Policy checks and approvals for risky operations | Must |
| FR-16 | Concise results with on-demand execution detail | Must |
| FR-17 | Runtime diagnostics | Must |
| FR-18 | Verified update and safe uninstall | Must |
| FR-19 | Local API for task submission/status/results | Should |
| FR-20 | Scheduling and recurring tasks | Later |
| FR-21 | TypeScript SDK | Later |
| FR-22 | macOS and Linux support | Later |

## Non-functional requirements

- **Reliability:** state transitions are persisted and recovery decisions explicit.
- **Security:** least privilege, local-only defaults, secret redaction and secure IPC/API boundaries.
- **Privacy:** local context remains local unless an explicitly authorized provider/operation requires otherwise.
- **Resilience:** bounded retries/timeouts; MCP server and task failure isolation.
- **Performance:** low idle footprint, bounded concurrency and no runaway agent loops.
- **Maintainability:** focused components and versioned Go/Python interfaces.
- **Usability:** concise output, actionable errors and no mandatory GUI.
- **Portability:** keep OS-specific behavior behind narrow adapters.

## Out of scope for v1

- Dashboard, admin console or visual analytics.
- Hosted multi-tenant control plane.
- Mandatory cloud LLM.
- Mandatory Redis, PostgreSQL, Docker or Kubernetes for a single machine.
- Bundled model weights.
- Unreviewed high-impact actions.
- macOS/Linux release before Windows acceptance.
- Rust rewrite without measured need.

## Release success criteria

A clean supported Windows machine must install and invoke CRANE without preinstalled Go/Python; run offline workflows; connect to stdio and Streamable HTTP MCP servers; continue and safely recover background tasks; enforce policy; update/rollback; and uninstall without deleting user data by default.

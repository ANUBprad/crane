# CRANE — Project Tracker

This tracker reports repository evidence, not intended design. A source file being present does not mean its feature is integrated or production-verified.

**Status legend:** Done (verified) · Implemented (not fully verified) · Partial · Not started · Blocked · Deferred

## Current implementation status

| Work item | Status | Evidence / remaining work |
|---|---|---|
| Product and engineering documentation baseline | Done | Root-level product, architecture, security, technical, distribution, and contributor documents |
| Go CLI entry point | Partial | `cmd/agent/main.go` exists; version flag works by design, but task commands and real supervisor wiring remain incomplete |
| Go supervisor package | Implemented (not fully verified) | `supervisor/supervisor.go` contains process/protocol coordination; full CLI integration and subprocess behavior need validation |
| Versioned Go/Python protocol | Implemented (not fully verified) | `python_protocol/protocol.go` defines message types and validation; bidirectional behavioral coverage is incomplete |
| SQLite persistence | Implemented (not fully verified) | `persistence/persistence.go` contains SQLite store and task/checkpoint methods; recovery behavior needs integration tests |
| Python runtime | Partial | Runtime and CLI modules exist; reproducible dependencies and packaged runtime are absent |
| MCP stdio transport | Implemented (not fully verified) | Transport code exists; test against a real local MCP server |
| MCP Streamable HTTP transport | Implemented (not fully verified) | Transport code exists; test against a real HTTP MCP server |
| MCP registry and tool discovery | Implemented (not fully verified) | Manager, discovery, and LangChain adapter modules exist; lifecycle and failure isolation need tests |
| LangChain tool integration | Implemented (not fully verified) | MCP-to-LangChain adapter code exists; complete provider configuration is outstanding |
| LangGraph workflow engine | Implemented (not fully verified) | `workflow_engine.py` and typed `workflow_state.py` exist; verify real task execution and failure paths |
| Durable checkpoint persistence | Partial / unverified | Task-state protocol and SQLite methods exist; prove write, process termination, restore, and resume in a real subprocess test |
| Approval workflow | Partial / unverified | Workflow state and policy classification exist; prove exact-action approval binding, persistence, denial, and restart behavior |
| Cancellation and side-effect safety | Partial / unverified | State fields and workflow handling exist; verify cancellation propagation and no unsafe replay under interruption |
| Ollama local provider | Not started | Local model provider integration and configuration remain |
| No-cloud-fallback / data-egress enforcement | Not verified | Product/security requirement; demonstrate provider-level enforcement before claiming complete |
| Structured logs and diagnostics | Partial | Basic logging exists; redaction, correlation, and user-facing diagnostics need completion |
| Windows runtime packaging | Not started | No bundled Python runtime or release package |
| Windows bootstrap installer | Not started | No official distribution endpoint or signed artifacts |
| Update, rollback, and uninstall | Not started | Requires release/installer implementation |
| Windows acceptance suite | Not verified | Must run on actual Windows with full subprocess and packaging scenarios |
| Windows release candidate | Not started | Depends on acceptance, security, packaging, and signing |
| macOS/Linux support | Deferred | Begins after Windows acceptance |

## Immediate engineering blockers

1. Wire `cmd/agent/main.go` to the actual Go supervisor and implement the real CLI task lifecycle.
2. Verify imports and package paths after moving Go packages to the repository root. The supervisor test currently references the former nested path and must be corrected.
3. Establish a pinned Python dependency manifest and reproducible installation/test workflow.
4. Run a real Go-to-Python subprocess protocol test, including stdout/stderr separation and startup handshake.
5. Prove checkpoint persistence and process-restart restoration against the same SQLite database.
6. Exercise MCP stdio and Streamable HTTP with actual local test servers.
7. Prove approval, cancellation, timeouts, and uncertain side-effect recovery.
8. Integrate and test Ollama only after the runtime execution path is reliable.

## Evidence policy

- Mark a row **Done** only when the behavior is tested at the appropriate boundary.
- Unit tests do not substitute for process-level or Windows acceptance tests.
- A manually invoked Python module does not prove the Go supervisor launches and controls it correctly.
- A checkpoint serializer does not prove durable recovery.
- Never claim exactly-once execution for non-idempotent external tools.
- Record exact test commands and environment when a status changes.

## Recent repository updates

- Initial Go, Python, persistence, MCP, and LangGraph source modules are present.
- Repository Go packages were moved to the root-level `cmd/`, `supervisor/`, `persistence/`, and `python_protocol/` directories.
- The root README and architecture documentation now distinguish design intent from implementation evidence.
- `OPENCODE.md` was removed from Git tracking at the user's request; `AGENTS.md` remains the tracked repository instruction file.

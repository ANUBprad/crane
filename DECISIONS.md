# CRANE — Architecture Decision Records

This file records current decisions and rationale. Revisit only when new evidence or a concrete requirement justifies a change.

## ADR-001 — Headless MCP runtime
**Decision:** CRANE is a general-purpose MCP execution/orchestration runtime, not a RAG app, research chatbot or dashboard product.  
**Rationale:** Its value is dependable execution across software tools.  
**Consequences:** CLI/API first; no dashboard requirement.

## ADR-002 — Offline-first, online-capable
**Decision:** Local workflows must function without internet when local dependencies are present. Remote providers and MCP servers are optional.  
**Rationale:** Privacy, availability and user control.  
**Consequences:** No silent cloud fallback or unauthorized data egress.

## ADR-003 — Windows first
**Decision:** Fully implement and validate Windows before macOS/Linux.  
**Rationale:** Focus packaging and runtime validation on one target.  
**Consequences:** Windows acceptance tests are release blockers; core remains portable.

## ADR-004 — Go supervisor plus Python agent runtime
**Decision:** Go owns CLI, lifecycle and process supervision; Python owns agent execution.  
**Rationale:** Standalone Go CLI with Python's agent ecosystem.  
**Consequences:** A versioned local protocol and packaged Python runtime are required.

## ADR-005 — LangChain and LangGraph are mandatory
**Decision:** LangChain provides model/tool abstractions; LangGraph provides stateful workflow orchestration.  
**Rationale:** Explicit project technology requirement.  
**Consequences:** Keep their integration central and testable; do not replace with an ad hoc agent loop.

## ADR-006 — MCP is the extensibility boundary
**Decision:** Integrate tools through MCP, initially stdio and Streamable HTTP.  
**Rationale:** Dynamic tool connectivity without hardcoding each vendor.  
**Consequences:** Lifecycle, discovery, schema validation and trust policy are first-class.

## ADR-007 — SQLite local default
**Decision:** Use SQLite for single-machine persistence.  
**Rationale:** Durable local storage without mandatory infrastructure.  
**Consequences:** Migrations, concurrency and recovery semantics must be tested.

## ADR-008 — Standalone CLI distribution
**Decision:** Install via PowerShell bootstrap; no source checkout or manual Go/Python prerequisite. No GUI installer/MSI in v1.  
**Rationale:** OpenCode/Gemini CLI-style experience.  
**Consequences:** Bundle Go executable, Python runtime/dependencies; verify release artifacts; preserve user data during update/uninstall.

## ADR-009 — Quiet by default
**Decision:** Show concise outcomes and intervention requests; retain detailed execution logs for on-demand inspection.  
**Rationale:** Background automation should not spam the user.  
**Consequences:** Structured logs and explicit status/result commands are required.

## ADR-010 — Avoid premature infrastructure
**Decision:** Redis, PostgreSQL, Docker, Kubernetes, Rust and a dashboard are not mandatory for v1.  
**Rationale:** Keep a single-machine product simple to install and operate.  
**Consequences:** Add infrastructure only for a demonstrated requirement.

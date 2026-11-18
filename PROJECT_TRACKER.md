# CRANE — Project Tracker

Update statuses only when work has actually been completed and verified.

**Status legend:** Not started · In progress · Blocked · Done

| Work item | Status | Evidence / notes |
|---|---|---|
| Establish repository documentation baseline | In progress | Initial documents being committed |
| Go CLI and supervisor | Not started | |
| Packaged Python runtime | Not started | No system Python prerequisite |
| Versioned Go/Python protocol | Not started | |
| SQLite persistence and migrations | Not started | |
| Durable task lifecycle | Not started | |
| Background execution and restart recovery | Not started | |
| MCP stdio support | Not started | |
| MCP Streamable HTTP support | Not started | |
| Dynamic MCP registry and discovery | Not started | |
| LangChain provider integration | Not started | |
| LangGraph workflow integration | Not started | |
| Ollama local provider | Not started | Model weights separate |
| Offline/no-cloud-fallback enforcement | Not started | |
| Permission and approval engine | Not started | |
| Structured logs and diagnostics | Not started | |
| Windows bootstrap installer | Not started | Official domain not yet available |
| Update, rollback and uninstall | Not started | |
| Windows acceptance suite | Not started | Must run on actual Windows |
| Windows release candidate | Not started | |
| macOS/Linux support | Deferred | Begins after Windows acceptance |

## Current blockers/dependencies

1. Implementation repository is initialized; runtime source is not yet implemented.
2. Official distribution domain and release hosting do not yet exist.
3. Windows validation environment and release-signing arrangements are needed before public release.

## Update log

- Created initial product/engineering baseline.
- OpenCode designated as primary development agent.

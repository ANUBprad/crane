# CRANE

**CRANE** is a headless, offline-first MCP execution and orchestration runtime. It runs durable, multi-step tasks through local and remote software tools exposed over the Model Context Protocol (MCP).

CRANE is a runtime, not a chatbot or dashboard product. It runs quietly by default, continues work after the caller disconnects, and returns concise outcomes. Detailed execution records remain available on request or when intervention is needed.

## Product principles

- **Headless:** CLI and local API first; no mandatory dashboard.
- **Offline-first:** local workflows work without internet when their local model, MCP servers and data are available.
- **Online-capable:** remote MCP servers and hosted LLMs are optional integrations.
- **Private by default:** local data is not sent to hosted providers without explicit authorization.
- **Durable:** tasks and checkpoints survive client disconnection and recover safely after runtime restart.
- **Quiet:** report completion, failure or required intervention; do not stream constant progress by default.
- **Windows first:** validate Windows before expanding to macOS or Linux.
- **Simple installation:** users install a standalone CLI; Go and Python toolchains are bundled rather than user prerequisites.

## Intended installation experience

The eventual bootstrap command is expected to follow this shape:

```powershell
irm https://<CRANE-DISTRIBUTION-DOMAIN>/install.ps1 | iex
```

The domain is a placeholder; **there is no published or working CRANE installer URL yet**. The production bootstrap must verify signed release metadata and artifacts before installing them.

After installation, the intended command is `agent`:

```powershell
agent
agent run "Summarize the issues in my local repository"
agent status <task-id>
agent result <task-id>
agent doctor
```

The executable name is provisional pending an availability check.

## Technology direction

- Go: CLI, supervisor, task lifecycle, process coordination and local API.
- Python: agent execution service.
- LangChain: model/provider and tool abstractions.
- LangGraph: stateful workflow execution, routing, retries and checkpoints.
- MCP: dynamic local and remote tool integration (stdio and Streamable HTTP).
- SQLite: default local persistence.
- Ollama: optional local model runtime; model weights are installed separately.

## Platform

The first release targets Windows 10/11 x64. macOS and Linux are deferred until Windows installation, execution, recovery, security and update behavior pass the acceptance criteria.

## Project status

This repository currently contains the product and engineering baseline. It does **not** yet contain an implemented runtime, working CLI, installer or production integrations. See [PROJECT_TRACKER.md](PROJECT_TRACKER.md) for implementation status.

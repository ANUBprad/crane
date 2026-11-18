# Contributing to CRANE

CRANE is being developed as a Windows-first, headless MCP execution runtime. Contributions should preserve its offline-first, privacy-conscious and durable-execution design.

## Before changing code

1. Read `DEVELOPMENT_RULES.md`, `ARCHITECTURE.md` and the relevant product requirements.
2. Read repository-local `AGENTS.md` or `opencode.md` instructions when present.
3. Check the current branch and working tree before editing.
4. Keep changes focused. Avoid speculative frameworks, unnecessary dependencies and unrelated refactors.

## Engineering expectations

- Go owns the CLI, supervisor and task lifecycle.
- Python owns agent execution.
- LangChain and LangGraph are required parts of the agent architecture.
- MCP is the tool integration boundary.
- SQLite is the default local persistence layer.
- Do not introduce mandatory Redis, PostgreSQL, Docker or a dashboard for single-machine use.
- Never silently send local data to a hosted model or service.
- Keep retries, loops, concurrency and execution time bounded.

## Validation

Run the tests and checks relevant to the changed behavior. Windows-specific behavior must be validated on Windows; Linux CI alone is not sufficient evidence for Windows support.

Document commands that are planned but not implemented as planned. Never claim a feature or platform is supported without evidence.

## Changes and commits

Use focused commits with descriptive messages. Stage only intended files. Do not commit secrets, generated artifacts, local databases, model weights or build output.

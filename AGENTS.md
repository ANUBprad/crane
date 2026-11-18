# Repository Instructions

These instructions apply to all coding agents working in CRANE.

## First
- Read `DEVELOPMENT_RULES.md` before implementation.
- Read `ARCHITECTURE.md` and the relevant section of `PRODUCT_REQUIREMENTS.md` for the requested change.
- Read `opencode.md` if it exists.
- Inspect the current Git branch, status and relevant files before editing.

## Scope
- Implement the requested feature or fix directly. Do not introduce artificial phases or expand into unrelated project exploration.
- Prefer minimal, production-quality diffs and established libraries.
- Do not add a dashboard, unnecessary infrastructure or speculative abstractions.
- Keep the documented product decisions intact unless the user explicitly changes them.

## Required architecture
- Go: CLI, supervisor, process lifecycle and durable task coordination.
- Python: agent execution.
- LangChain: model/tool integration.
- LangGraph: stateful agent workflow orchestration.
- MCP: external tool/server protocol.
- SQLite: default local persistence.
- Windows is the first supported target. Keep the core portable.

## Safety and privacy
- No silent cloud fallback or unauthorized data egress.
- Treat tool calls as security-sensitive operations.
- Enforce policy and approval for destructive, sensitive or externally consequential actions.
- Redact secrets from logs, errors and diagnostics.

## Validation and Git
- Test each meaningful change, including relevant failure paths.
- Validate Windows behavior on actual Windows.
- Inspect the final diff; stage only intended paths.
- Commit meaningful completed changes; do not create no-op commits.
- Never force-push or rewrite shared history without explicit authorization.

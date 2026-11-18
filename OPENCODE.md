# OpenCode Development Workflow

OpenCode is the primary coding agent for CRANE. Use this file together with `AGENTS.md` and `DEVELOPMENT_RULES.md`.

## Before every task
1. Read `AGENTS.md`, `DEVELOPMENT_RULES.md`, and this file.
2. Read the relevant product/architecture document for the requested change.
3. Inspect `git status --short --branch`, current branch, recent commits and the relevant code.
4. Do not overwrite or discard existing user changes.

## How to work
- Work on `main` unless explicitly instructed otherwise.
- Address the requested feature or blocker directly. Do not invent phases, perform broad audits, or redesign unrelated components.
- Make the smallest complete, production-quality change.
- Avoid unnecessary dependencies, abstractions, generated scaffolding and speculative features.
- Preserve the decisions recorded in `DECISIONS.md`.
- Go owns CLI/supervisor/task lifecycle; Python owns agent execution.
- LangChain and LangGraph are required core technologies.
- MCP is the tool integration boundary; SQLite is the local persistence default.
- Windows is the first release target. Keep core logic portable.

## Validation
- Run focused tests and relevant formatting/type/lint checks for each meaningful change.
- Add or update regression tests for changed behavior.
- Do not claim tests passed unless they were executed and passed.
- Windows-specific behavior must be tested on Windows; do not infer Windows readiness from Linux CI.
- Inspect the final diff for correctness, scope, secrets and generated files.

## Git
- Stage only the intended paths; never blindly stage the entire working tree.
- Commit meaningful completed changes with descriptive messages.
- Push completed changes to `origin main` when remote access is available.
- Do not create no-op commits, inflate commit counts, force-push or rewrite shared history.
- If a push fails, report the exact blocker rather than claiming it was published.

## Reporting
At the end of each task, report:
- what changed;
- tests/checks actually run and their results;
- commit hash and push status;
- known remaining blocker, if any.

Do not claim a feature, integration, installer or platform is implemented until the code and its tests demonstrate it.

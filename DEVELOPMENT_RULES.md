# CRANE — Engineering Rules

These rules apply to implementation and maintenance work.

## Working discipline

- Read `AGENTS.md`, `OPENCODE.md` and this file before coding. Follow their requirements.
- Work directly on `main` unless the user or explicit repository instruction says otherwise.
- Inspect Git state and relevant code before editing; do not perform broad exploratory rewrites.
- Fix the requested blocker or implement the requested capability. Do not invent phases or expand scope unnecessarily.
- Prefer the smallest correct production-quality change.
- Follow YAGNI: no speculative abstractions, unnecessary dependencies, duplicate implementations or dead code.
- Reuse established libraries and project conventions.
- Keep platform-specific behavior behind narrow adapters.

## Architecture constraints

- Go owns CLI, supervisor and task lifecycle.
- Python owns agent execution.
- LangChain and LangGraph are required core technologies.
- MCP is the external tool boundary.
- SQLite is the default local persistence engine.
- Redis, PostgreSQL, Docker and Kubernetes are not mandatory for single-machine operation.
- No dashboard/frontend is required for core functionality.
- Never silently send local user data to hosted services.
- Do not add Rust unless profiling demonstrates a concrete need.

## Code quality

- Validate external inputs and process boundaries.
- Use typed, versioned messages between Go and Python.
- Bound retries, loops, concurrency, output sizes and execution time.
- Make task state transitions explicit and durable.
- Treat external side effects as potentially non-idempotent.
- Redact secrets from logs and errors.
- Keep user-facing failures actionable without leaking sensitive implementation details.

## Testing

For every meaningful change:
1. Run the narrowest relevant tests.
2. Run formatting, linting and type checks for changed code.
3. Run broader tests when component boundaries are crossed.
4. Test Windows-specific behavior on Windows; Linux success is not a substitute.
5. Investigate failures. Do not claim tests passed if they were skipped or not run.

Test observable behavior, failure paths and regressions, not merely implementation details.

## Git discipline

- Commit meaningful completed code, docs and test changes.
- Push each meaningful completed change promptly when remote access and authorization are available.
- Do not batch unrelated completed work or create no-op commits.
- Stage only intended files; never blindly run `git add .`.
- Never force-push or rewrite shared history without explicit authorization.
- Verify final commit, remote synchronization and working tree state.
- Report honestly when credentials, network or permissions prevent a push.

## Documentation

- Keep docs concise, accurate and synchronized with actual behavior.
- Mark planned commands/features as planned until implemented.
- Do not claim distribution URL, release, platform support or security property without validation.

<div align="center">
  <img src="apps/desktop/public/crane-mark-dark.webp" alt="CRANE" width="320" />
</div>

<p align="center"><strong>CRANE</strong> is a headless, offline-first execution runtime for durable, tool-using tasks.</p>

CRANE runs as a local supervisor: it accepts tasks through a CLI or authenticated loopback API, executes bounded workflows through a Python/LangGraph runtime, invokes MCP tools under policy control, persists state in SQLite, and returns concise results. It is designed for local-first operation, not as a chatbot, RAG product, or dashboard.

> **Project status:** working vertical slice with active hardening. Core task admission, Python/LangGraph execution, MCP stdio/Streamable HTTP tool integration, local Ollama execution, SQLite persistence, authenticated loopback API/SSE, approval gating, and Windows packaging scripts are implemented and exercised by tests and CI. CRANE is **not** production-ready: clean physical-machine Windows 10/11 x64 acceptance, Authenticode signing and publisher-authenticity verification, a controlled official release host/domain, and cross-environment reproducibility proof remain pending or external blockers.

## What CRANE does

CRANE is built to coordinate work that needs more than a single model response:

- Accept a task, plan a bounded next action, and execute it through authorized tools.
- Keep execution headless and local-first: CLI and loopback API first, no mandatory UI.
- Use MCP as the external tool boundary instead of embedding ad hoc integrations into the core runtime.
- Persist task lifecycle, events, checkpoints, approvals, and invocation records in SQLite.
- Continue or recover safely after interruption where the persisted state makes recovery unambiguous.
- Require explicit authorization before consequential actions.

## Architecture and responsibilities

CRANE separates process control from agent execution:

| Component | Responsibility |
|---|---|
| **Go CLI / supervisor** | Owns the `agent` executable, process lifecycle, task admission/cancellation, recovery decisions, local API, and coordination of durable state. |
| **Python execution service** | Runs agent execution as a supervised runtime. It receives versioned protocol messages, executes workflows, and emits state/events/results. |
| **LangChain** | Model/tool abstraction layer. MCP tools are adapted through LangChain interfaces; provider integration is explicit. |
| **LangGraph** | Stateful workflow orchestration. The workflow executes bounded graph steps, checkpoints, tool routing, and result/failure transitions. |
| **MCP** | External tool/server protocol boundary. CRANE supports local stdio and remote Streamable HTTP MCP servers through the MCP runtime. |
| **SQLite** | Default local persistence engine for tasks, events, checkpoints, MCP configuration/discovery/invocations, and approval records. |

The optional desktop app is a separate client; it is not part of the execution core. See [ARCHITECTURE.md](ARCHITECTURE.md) and [TECHNICAL_SPECIFICATION.md](TECHNICAL_SPECIFICATION.md) for details.

## Model providers: local Ollama and explicit hosted services

CRANE's intended offline path is local model execution. Local Ollama support is implemented as an explicit provider choice (`CRANE_MODEL_PROVIDER=ollama`, `CRANE_MODEL`, `OLLAMA_HOST` as configured). CRANE does not silently fall back from local execution to a hosted model.

Hosted model providers or remote MCP endpoints are optional integrations. They must be explicitly configured and authorized; they are not the default execution path, and their use changes the data-egress boundary. See [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md).

## Project website and public installer

The product website is deployed from `website/` through GitHub Pages:
[CRANE website](https://anubprad.github.io/crane/).

Once a stable GitHub Release has been published, install the latest Windows x64 build from PowerShell:

```powershell
irm https://anubprad.github.io/crane/install.ps1 | iex
```

The bootstrap downloads the release assets from this repository, checks the release metadata, and invokes the existing checksum-verifying installer. Review [website/install.ps1](website/install.ps1) before running it. Releases are currently unsigned: SHA-256 verifies artifact integrity, not publisher authenticity. The command will fail safely until a stable release exists. A tagged release (`vX.Y.Z`) triggers the Windows packaging and GitHub Release workflow.

## Windows installation and CLI commands

Windows 10/11 x64 is the first supported target. Packaged installation uses PowerShell scripts and a private per-user runtime layout; end users do not need Go, Python, Node.js, Docker, Redis, or a source checkout.

### Release build (contributors / controlled packaging)

```powershell
.\scripts\build-release.ps1 -Version <version> -Commit <sha> -BuildDate <iso8601> -PythonVersion 3.12.7 -GoVersion <go-version> -PythonEmbedZip <path>
```

Builds are deterministic for identical declared inputs on the same host as validated by `scripts/test-release-reproducibility.ps1`. Cross-environment reproducibility is not yet proven.

### Install / update / rollback / uninstall

Release source must be explicit (local directory or controlled `http(s)` source). Remote sources require `CRANE_ALLOW_REMOTE_RELEASE_SOURCE=1`; otherwise installation fails before any download. Do not use unverified download sources.

```powershell
.\scripts\install.ps1 -Action Install -ReleaseSource <dir-or-url> [-Version <version>]
.\scripts\install.ps1 -Action Update  -ReleaseSource <dir-or-url> [-Version <version>]
.\scripts\install.ps1 -Action Rollback
.\scripts\uninstall.ps1
.\scripts\uninstall.ps1 -RemoveData -Confirm REMOVE-CRANE-DATA
```

The installer verifies manifest/SHA-256 metadata, rejects tampered/unsupported artifacts, stages payloads safely, and preserves user data by default. Current manifests are explicitly unsigned/unverified (`signature.scheme=none`, `signature.status=unverified`); this is checksum/metadata verification, not Authenticode signing.

### CLI commands

The packaged `agent` launcher exposes the user-facing command set:

```powershell
agent                               # Start supervisor and local API (default)
agent serve                         # Start supervisor and local API
agent run "task"                    # Submit a task and print its ID
agent status <task-id>              # Report persisted task status
agent result <task-id>              # Print completed task result
agent cancel <task-id>              # Request task cancellation
agent doctor                        # Report runtime/installation health
agent update -source <dir-or-url> [-version <version>]
agent uninstall [--remove-data --confirm REMOVE-CRANE-DATA]
```

Development/source commands also include:

```powershell
go run ./cmd/agent --version
go run ./cmd/agent api-token
go run ./cmd/agent api-token --rotate
go run ./cmd/agent api-token --revoke
go run ./cmd/agent task create --id <id> --input "..."
```

`api-token` is a trusted local integration boundary: tokens are printed to stdout only, never passed as URL/query args, and rotation/revocation take effect immediately because protected token state is reloaded per request. See [DISTRIBUTION_AND_INSTALLATION.md](DISTRIBUTION_AND_INSTALLATION.md).

## MCP server configuration and tool execution

MCP is the boundary for external tools. CRANE manages MCP servers through its Go supervisor and authenticated loopback API (`/v1/mcp/*` routes for status, server CRUD/connect/disconnect/discover, tools, invocations, approvals, and invoke decisions). The desktop client does not yet provide MCP management screens.

Configuration supports stdio and Streamable HTTP transports. Stdio server commands are constrained by explicit allowlist/policy (`CRANE_MCP_ALLOWED_COMMANDS` and runtime validation), and environment values are treated as sensitive: secret-like keys are rejected by policy and secret values must not be exposed through status/invocation/approval APIs or logs.

Tool execution is policy-gated:

- Read-only/allowed tools may run without approval when policy permits.
- Consequential/destructive/externally impactful tools require explicit exact-action approval.
- Approvals are bound to the exact action fingerprint; a changed action requires new approval.
- Denial prevents invocation. Pending approvals persist across restart until decided.
- MCP invocations/results are recorded as durable records; ambiguous non-idempotent external effects must not be blindly replayed.

## Task lifecycle, persistence, recovery, and approvals

Task state is persisted in SQLite. The durable lifecycle includes queued/running/succeeded/failed/cancelled and approval-waiting states as represented by the runtime/API. CRANE persists lifecycle events, checkpoints, terminal results, MCP tool results, and approval records.

Recovery behavior is safety-first:

- Startup recovery inspects persisted task/checkpoint state and resumes only where safe.
- Ambiguous in-flight or cancellation-unconfirmed work must not be reported as clean success.
- Pending approvals survive supervisor restart and remain pending until explicitly approved/denied.
- Approval decisions are exact-action grants; no broad or implicit authorization.
- External side effects are treated as potentially non-idempotent. CRANE does not claim exactly-once external execution.

SSE task events are persisted and replayable by cursor. Clients should use cursor replay after reconnect/restart rather than assuming in-memory delivery.

## Security and permission boundaries

CRANE treats tool execution as a security-sensitive boundary:

- The local API binds to loopback (`127.0.0.1:8787`) and requires bearer-token authentication.
- Model output and tool descriptions are untrusted; they cannot grant permissions or bypass policy.
- Consequential actions require explicit approval by default policy.
- Secrets/credentials must be redacted from logs, errors, diagnostics, and API responses.
- Stdio command execution and MCP environment configuration are validated/constrained.
- No silent cloud fallback or unauthorized data egress.

See [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md) for the full security model.

## Desktop workspace (optional)

The Electron + React desktop workspace under [apps/desktop](apps/desktop/README.md) is an optional client for observing health/tasks/events and submitting/cancelling tasks through the restricted preload/main-process bridge. It does not replace the CLI, does not own task lifecycle, and must not bypass Go-owned policy or access SQLite/tokens directly.

## Development and testing

### Prerequisites

- Go toolchain matching [go.mod](go.mod)
- Python 3.12.x for runtime development/packaging parity
- Node/npm for desktop workspace checks
- Windows for Windows packaging/install/lifecycle validation

### Common checks

```powershell
gofmt -l .
go build ./...
go vet ./...
go test ./...
python -m compileall -q crane
python -m unittest discover -s crane -p "test_*.py" -v
python -m crane.python_runtime --health
```

Desktop (`apps/desktop`):

```powershell
npm ci
npm run typecheck
npm run build
```

Packaging/release validation:

```powershell
.\scripts\build-release.ps1 -Version <version> -Commit <sha> -BuildDate <iso8601> -PythonVersion 3.12.7 -GoVersion <go-version> -PythonEmbedZip <path>
.\scripts\test-release-reproducibility.ps1
.\scripts\acceptance-windows.ps1 -ReleaseRoot .\build\release
```

Run tests relevant to the behavior you changed. Do not claim a test passed unless it was executed in the current environment.

## Current release limitations

The following are intentionally **not** claimed as complete:

- **Clean-machine Windows acceptance:** no verified clean Windows 10/11 x64 acceptance on machines without dev toolchains/repo dependencies.
- **Authenticode/code signing:** manifests/artifacts are currently unsigned/unverified; checksum validation is not code-signature verification.
- **Official release host:** no verified controlled official download domain/hosted release channel. Release source must be explicitly provided and controlled.
- **Cross-environment reproducibility:** identical-input deterministic outputs are validated on this host; cross-environment/toolchain reproducibility is not proven.
- **Packaged upgrade/rollback evidence:** best-effort packaged upgrade/rollback checks exist, but same-commit true upgrade/rollback proof may be environment-limited; report limitations explicitly.
- **Production readiness:** external side-effect ambiguity, timeout/concurrency saturation, packaged non-idempotent replay prevention, and full clean-machine security acceptance remain active validation areas.

## Documentation

| Document | Purpose |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | System boundaries, component ownership, and flow |
| [PRODUCT_REQUIREMENTS.md](PRODUCT_REQUIREMENTS.md) | Product scope and requirements |
| [TECHNICAL_SPECIFICATION.md](TECHNICAL_SPECIFICATION.md) | Technical contracts and runtime constraints |
| [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md) | Trust boundaries, approvals, secrets, egress |
| [DISTRIBUTION_AND_INSTALLATION.md](DISTRIBUTION_AND_INSTALLATION.md) | Windows packaging/install/update/rollback/uninstall |
| [WINDOWS_ACCEPTANCE_TESTS.md](WINDOWS_ACCEPTANCE_TESTS.md) | Windows acceptance criteria |
| [PROJECT_TRACKER.md](PROJECT_TRACKER.md) | Evidence-based status and blockers |
| [DEVELOPMENT_RULES.md](DEVELOPMENT_RULES.md) | Engineering and validation rules |
| [DECISIONS.md](DECISIONS.md) | Recorded decisions |

## Contributing

Read [AGENTS.md](AGENTS.md), [DEVELOPMENT_RULES.md](DEVELOPMENT_RULES.md), and the relevant architecture/product docs before changing code. Keep changes focused, preserve user data, validate behavior you changed, and report tests/limitations honestly.

## License

[LICENSE](LICENSE)

# CRANE — Security and Privacy

## Security posture

CRANE can invoke tools that modify files, execute commands, access services or affect external systems. Tool execution is a security boundary, not a harmless chatbot feature.

## Trust boundaries

- User/CLI/API input
- Electron renderer and preload bridge
- Electron main-process runtime client
- Go supervisor
- Python agent runtime
- LLM provider (local or hosted)
- MCP server process or remote endpoint
- Tool implementation and external systems
- Local database, logs, artifacts and secrets

Validate and govern data crossing each boundary.

## Permission classes

- **Read-only:** inspect/list/search.
- **Local mutation:** create/edit/delete local resources.
- **Execution:** run commands, scripts or code.
- **External mutation:** send messages, create tickets, publish content or modify cloud resources.
- **Sensitive access:** access credentials, private records or protected data.
- **Privilege/security changes:** alter permissions, identities, policies or security controls.

Destructive, sensitive, privilege-changing and externally consequential actions require approval by default unless the user has explicitly configured a narrower trusted policy. Harmless read-only operations should not trigger needless prompts.

## Approval requirements

- Show the exact action, target and relevant arguments before approval.
- Bind approval to the specific pending action and expire it.
- A changed action requires new approval.
- Record decision, actor and timestamp.
- Pending approval must survive runtime restart.
- Denial prevents execution.
- Never interpret model-generated text as user approval.

## Secrets

- Never commit credentials, tokens, private keys or secret-bearing endpoints.
- Never log secrets or full authorization headers.
- Prefer OS-backed secret storage on Windows.
- Support environment-variable injection for automation.
- Redact sensitive values from errors, traces and diagnostics.
- Omit secrets from config export by default.

The local API token is 256-bit random. On Windows it is protected with DPAPI and application entropy in `%LOCALAPPDATA%\CRANE\auth\token.json` (or `~/.crane/auth/token.json` off Windows). The trusted `agent api-token` command prints it only on stdout for local integrations; it is never passed as an argument, placed in a URL or browser storage, or exposed through the desktop renderer. API verification is constant time, revoked token hashes are retained permanently, and protected state is reloaded on every request so rotation or revocation takes effect immediately.

## Network and data egress

- The Go local API binds only to `127.0.0.1:8787`; host configuration and all-interface binding are rejected.
- Every API request requires bearer-token authentication; unauthenticated requests receive a uniform `unauthorized` error.
- Remote MCP endpoints and hosted LLMs must be explicitly configured.
- No automatic cloud fallback from local inference.
- Enforce explicit provider/data policy before hosted inference.
- Distinguish local and remote tool servers.
- Do not silently upload logs, prompts, files or artifacts for telemetry.

## Process and filesystem safety

- Validate executable paths and arguments for stdio servers.
- Do not build shell commands by concatenating untrusted strings.
- Use safe process APIs and explicit argument arrays.
- Restrict CRANE-managed paths; defend against path traversal and reparse-point surprises.
- Clean up CRANE-owned children without killing unrelated processes.
- Apply resource limits where feasible.
- The desktop renderer has no token, arbitrary HTTP, filesystem, SQLite or shell capability; its preload bridge exposes only health, app-info, task read/submit/cancel and stream subscription methods.
- The Electron main process validates task IDs, task input bounds, event cursors and response shapes before returning data to the renderer, and owns stream lifecycle and cleanup.
- Task lifecycle decisions remain in Go: admission happens before execution, duplicate IDs are rejected, cancellation must be confirmed, and event-stream disconnects never cancel work.
- Python execution is explicit: the default local provider returns deterministic workflow facts only, Ollama requires explicit configuration, and unsupported or unavailable providers fail without fallback.

## Supply chain and updates

- Release source must be explicit and controlled (local directory or verified `https` host). Do not define or claim an official CRANE domain until host ownership, TLS, and access control are verified.
- Verify release metadata and SHA-256 checksums before installation or update; reject tampered, incomplete, or unsupported artifacts. Current manifests are explicitly `signature.scheme = "none"` / `signature.status = "unverified"` and the installer accepts only that explicit unsigned state—it is **not** Authenticode/code-signing verification.
- Never fall back to unverified downloads or execute an unverified downloaded script/binary.
- Keep dependencies pinned and generate an SBOM for releases.
- Sign Windows executables/manifests with a real certificate when release infrastructure supports it and verify signatures in release validation; checksum verification must not be described as code signing.

Current release-readiness evidence is limited to repository scripts/tests and GitHub-hosted Windows CI on the current HEAD. Release manifests/SHA256SUMS provide integrity checks; artifacts remain unsigned (`none`/`unverified`) without Authenticode publisher authenticity. No legitimate signing certificate, controlled official host/domain, clean physical Windows 10/11 x64 acceptance, or cross-environment reproducibility proof is available.

## Logs and diagnostics

- Store logs locally by default.
- Redact secrets and sensitive arguments.
- Make retention configurable.
- No telemetry by default.
- Future telemetry must be opt-in, documented and minimized.
- Diagnostic bundles are user-initiated and must be checked for sensitive data before sharing.

## Security acceptance tests

- Unauthorized API access is rejected.
- Local API is not reachable from the network by default.
- Secrets do not appear in logs, errors or exports.
- Approval cannot be bypassed by model output or altered arguments.
- Denied actions never execute.
- Tool descriptions cannot change policy.
- Path traversal and unsafe executable configuration are rejected.
- Tampered installer artifacts are rejected.
- Local-only mode makes no hosted provider requests.

Automated checks cover token lifecycle behavior, constant-time verification inputs, loopback address validation, unauthenticated API rejection, immediate rotation/revocation, request validation, API-safe task reads, component-specific health reporting, duplicate admission rejection, explicit provider failure, confirmed cancellation, SSE cursor replay, restart recovery policy, and approval persistence/recovery cases. Release scripts also include manifest/SHA256/signature-state validation and integrity/failure harness cases. Clean physical-machine packaged security acceptance, Authenticode validation, and controlled-host verification remain unverified.

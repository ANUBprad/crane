# CRANE Desktop

An optional desktop workspace for the CRANE runtime. It is deliberately separate from the Go/Python execution core.

## Stack

- Electron main process for the desktop window, token lifecycle and loopback API client.
- React + TypeScript + Vite for the renderer.
- Context-isolated preload bridge; Node integration is disabled in the renderer.
- No Rust/Tauri dependency.

## Development

Requires Node.js 20 or newer and a Go toolchain for the local API binary.

```powershell
cd apps/desktop
npm install
npm run dev
```

`npm run build` first builds `../../agent.exe` and then produces the renderer assets. To see live data while developing, run the Go supervisor separately:

```powershell
cd ../..
go run ./cmd/agent serve
```

The desktop client connects only to the fixed `127.0.0.1:8787` loopback API.

## Windows packaging

```powershell
npm run desktop:build
```

The NSIS target is configured for per-user installation and packages the Go `agent.exe` as an extra resource. Uninstall is configured not to delete application data. Installer packaging and signed releases are not yet validated or available.

## Runtime integration status

The current client reads real health, persisted tasks and task details, submits and cancels tasks, and subscribes to live persisted events from the authenticated loopback API. The Electron main process retrieves the protected installation token through the trusted `agent api-token` command, caches it in memory, and owns all HTTP and stream lifecycle calls. The preload exposes only `getAppInfo`, `getHealth`, `getTasks`, `getTask`, `submitTask`, `cancelTask` and `subscribeTaskEvents` to the renderer.

The UI shows real supervisor, Python runtime, persistence and readiness state, real persisted tasks, submission/cancellation outcomes, and a reconnecting live event feed backed by server-side event IDs. It reports disconnected, recovering and error states explicitly and does not fabricate health, tasks, events or status. MCP server data and approvals remain unavailable because the current API does not expose them. Closing the desktop window does not stop supervisor-owned work.

## Security notes

- `contextIsolation`, `sandbox`, and `webSecurity` are enabled.
- `nodeIntegration` is disabled.
- The preload exposes only narrow, validated runtime methods; the token and arbitrary HTTP access never enter the renderer.
- The installation token never enters the renderer, URLs, browser storage, arbitrary IPC methods or CLI arguments.
- The main process validates task IDs and response shapes and retries once with a fresh token after `401`.
- New-window navigation is denied; only HTTPS links are handed to the OS browser.
- Development server binds to loopback only.
- No telemetry or hosted service calls are made by the desktop shell.

## Current limitations

- Task submission and cancellation are available only when the supervisor is connected and ready.
- Live event streaming reconnects from the last persisted event ID; a disconnect never cancels the task.
- No MCP server or approval data yet; those sections are explicitly labeled unavailable.
- The Go supervisor must be running separately for live data.
- Installer packaging has not been built or tested on Windows.
- No signed installer or release artifacts are available yet.

## Validation

```powershell
npm run typecheck
npm run build
```

The Windows vertical E2E check also drove the real renderer through task submission and live event display; exact results are recorded in the root [PROJECT_TRACKER.md](../../PROJECT_TRACKER.md).

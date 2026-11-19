# CRANE Desktop

An optional desktop workspace for the CRANE runtime. It is deliberately separate from the Go/Python execution core.

## Stack

- Electron main process for the desktop window and lifecycle.
- React + TypeScript + Vite for the renderer.
- Context-isolated preload bridge; Node integration is disabled in the renderer.
- No Rust/Tauri dependency.

## Development

Requires Node.js 20 or newer.

```powershell
cd apps/desktop
npm install
npm run dev
```

## Windows packaging

```powershell
npm run desktop:build
```

The NSIS target is configured for per-user installation. Uninstall is configured not to delete application data.

## Runtime integration status

This is the first desktop shell, not a claim that the runtime API already exists. The current Go CLI/supervisor does not expose the local authenticated API required for task submission, status streaming, MCP management, approvals, or result retrieval. The UI therefore reports the runtime as disconnected, does not fabricate tasks or component health, and keeps task submission disabled. The preload bridge exposes only desktop application metadata.

The next integration point is a versioned, authenticated loopback API owned by the Go supervisor. The desktop client must not connect directly to SQLite or launch untrusted shell commands. Browser/renderer code must never receive arbitrary Node or filesystem access.

## Security notes

- `contextIsolation`, `sandbox`, and `webSecurity` are enabled.
- `nodeIntegration` is disabled.
- The preload exposes a narrow, read-only application metadata method.
- New-window navigation is denied; only HTTPS links are handed to the OS browser.
- Development server binds to loopback only.
- No telemetry or hosted service calls are made by the desktop shell.

## Current limitations

- No Go runtime API integration yet.
- No live execution streaming or approval actions yet.
- Installer packaging has not been built or tested on Windows.
- No signed installer/release artifacts are available yet.
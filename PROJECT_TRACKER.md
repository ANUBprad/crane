# CRANE — Project Tracker

This tracker reports repository evidence, not intended design. A source file being present does not mean its feature is integrated or production-verified.

**Status legend:** Done (verified) · Implemented (not fully verified) · Partial · Not started · Blocked · Deferred

## Current implementation status

| Work item | Status | Evidence / remaining work |
|---|---|---|
| Product and engineering documentation baseline | Done | Root-level product, architecture, security, technical, distribution, and contributor documents |
| Go CLI entry point | Done (verified) | `agent serve`, `--version`, token lifecycle and `task create` are wired; the full task command set remains planned |
| Go supervisor package | Done (verified) | Durable admission, bounded subprocess execution, cancellation confirmation, event hub and startup recovery are tested on Windows |
| Versioned Go/Python protocol | Done (verified) | Bounded NDJSON handshake, submission, cancellation, state, event, result and error paths are covered by subprocess tests |
| SQLite persistence | Done (verified) | Task records, events, checkpoints, terminal results and API-safe reads are tested; approval records and migrations are absent |
| Python runtime | Implemented (not fully verified) | Real LangGraph child execution works; reproducible dependencies and packaged runtime are absent |
| MCP stdio transport | Implemented (not fully verified) | Transport code exists; test against a real local MCP server |
| MCP Streamable HTTP transport | Implemented (not fully verified) | Transport code exists; test against a real HTTP MCP server |
| MCP registry and tool discovery | Implemented (not fully verified) | Manager, discovery, and LangChain adapter modules exist; not connected to the task API or graph nodes |
| LangChain tool integration | Partial | Explicit local/Ollama provider policy exists; MCP adapter code is present but live server behavior and tool invocation are unverified |
| LangGraph workflow engine | Done (verified) | A real `StateGraph` executes admitted tasks with bounded steps/time and emits persisted checkpoints/events |
| Durable checkpoint persistence | Done (verified) | Checkpoint write and restart recovery policies are covered by supervisor tests; approval recovery is not implemented |
| Approval workflow | Partial | State fields and policy classification exist; exact-action persistence, denial and restart behavior are not exposed by the API/UI |
| Cancellation and side-effect safety | Partial | Confirmed cancellation, cancellation-before-registration race and protocol ack are tested on Windows; real MCP side-effect replay remains unverified |
| Ollama local provider | Done (verified) | `CRANE_MODEL_PROVIDER=ollama` uses pinned `langchain-ollama==1.1.0`/`ChatOllama`; real `llama3.2:3b` inference, config, preflight, diagnostics, invalid-model failure and LangGraph/SQLite persistence are verified on Windows. No fallback is permitted |
| No-cloud-fallback / data-egress enforcement | Done (verified) | Default local mode never invents model output; unsupported/unavailable providers fail explicitly with no fallback |
| Structured logs and diagnostics | Partial | Basic logging, bounded stderr and `agent doctor` provider diagnostics exist; redaction and correlation need completion |
| Windows runtime packaging | Not started | No bundled Python runtime or release package |
| Windows bootstrap installer | Not started | No official distribution endpoint or signed artifacts |
| Update, rollback, and uninstall | Not started | Requires release/installer implementation |
| Windows acceptance suite | Partial | Full supervisor/API/Python/desktop vertical E2E passed on Windows; release acceptance and packaging scenarios remain |
| Windows release candidate | Not started | Depends on acceptance, security, packaging, and signing |
| macOS/Linux support | Deferred | Begins after Windows acceptance |

## Immediate engineering blockers

1. Exercise MCP stdio and Streamable HTTP with actual local test servers and connect discovered tools to the graph through policy.
2. Implement and verify exact-action approval persistence, denial, and restart recovery.
3. Verify timeout, concurrency saturation, and side-effect ambiguity with real external tools.
4. Keep validating additional local models and packaged-runtime behavior. A real end-to-end `llama3.2:3b` task has passed.
5. Expand `requirements.txt` into a reproducible Python environment and packaged runtime; the Ollama integration dependencies are pinned but the complete runtime is not.
6. Complete Windows release acceptance, packaging, signing, update, rollback and uninstall.

## Validation evidence (Windows)

Environment: Microsoft Windows 11 Home Single Language, `go version go1.26.5 windows/amd64`, `node v24.22.2`, `Python 3.12.7`, Electron 33.2.1 from `apps/desktop/package.json`. Commands were run from `C:\projects\crane` unless noted.

```powershell
gofmt -l cmd crane localapi persistence python_protocol supervisor
go build ./...
go vet ./...
go test ./... -count=3
python -m compileall -q crane
python -m unittest discover -s crane/python_runtime -p 'test_*.py'
python -m crane.python_runtime --health
go run ./cmd/agent doctor
```

Results:

- `gofmt`, `go build ./...`, `go vet ./...` and `python -m compileall -q crane`: no output, exit 0.
- `go test ./... -count=3`: passed all packages; `cmd/agent 11.065s`, `localapi 2.432s`, `localauth 0.105s`, `persistence 0.424s`, `supervisor 32.098s`, `python_protocol` has no test files.
- Python provider/workflow tests: `Ran 23 tests ... OK`, covering configuration (including bounded `num_ctx`), preflight, initialization, mocked `ChatOllama` success/failure, LangGraph/SQLite persistence and diagnostics without task data.
- Python health: `{"protocol": "crane-agent/0.1.0", "status": "healthy", "version": "0.1.0", "python_version": "3.12.7"}`.
- Real Ollama validation: Ollama `0.40.16` is installed and reachable at `http://127.0.0.1:11434`; `agent doctor` reports `llama3.2:3b` available and the provider initializable. An authenticated API task completed through Go → Python → LangGraph → `ChatOllama`; the persisted result contained generated text with `execution_mode=langchain_ollama`, `provider=ollama`, `model=llama3.2:3b`. Lifecycle events and authenticated SSE replay included `provider_result` and `task_finished=succeeded`. A deliberately invalid model failed with `model_not_found` and no fallback.
- Cancellation race regression: `go test ./supervisor -run 'TestCancelImmediatelyAfterAdmissionStaysCancelled|TestCancelTaskConfirmsPythonOutcome' -count=3 -v` passed all six runs.

Desktop checks from `apps/desktop`:

```powershell
npm run typecheck
npm run build
```

Results:

- `npm run typecheck`: `tsc --noEmit`, exit 0.
- `npm run build`: prebuild `go build -o ../../agent.exe ../../cmd/agent`; Vite transformed 1579 modules and emitted production CSS/JS; exit 0.

Full Windows E2E harness (external working file, not part of the repository):

```powershell
& 'C:\Users\ANUBHAV\AppData\Local\Temp\opencode\crane-e2e.ps1'
```

Result: `E2E RESULT: PASS`, including `unauthenticated /v1/health rejected with 401 unauthorized`, `supervisor=True runtime=True persistence=True ready=True detail='Python 3.12.7'`, `admitted e2e-win-task with status queued`, duplicate ID `409 conflict`, `e2e-cancel-task reached confirmed status cancelled`, `e2e-win-task succeeded with execution_mode=deterministic_local model_output=null`, SSE replay `events=14 last_id=30 task_finished=succeeded`, cursor replay with no duplicate events, raw input omitted from task reads, real renderer submission of `e2e-desktop-task`, screenshot saved to `C:\Users\ANUBHAV\AppData\Local\Temp\opencode\crane-e2e\renderer.jpg`, and supervisor still ready after Electron closed. Renderer text confirmed `e2e-cancel-task` displayed as `CANCELLED`.

## Evidence policy

- Mark a row **Done** only when the behavior is tested at the appropriate boundary.
- Unit tests do not substitute for process-level or Windows acceptance tests.
- A manually invoked Python module does not prove the Go supervisor launches and controls it correctly.
- A checkpoint serializer does not prove durable recovery.
- Never claim exactly-once execution for non-idempotent external tools.
- Record exact test commands and environment when a status changes.

## Recent repository updates

- Initial Go, Python, persistence, MCP, and LangGraph source modules are present.
- Repository Go packages were moved to the root-level `cmd/`, `supervisor/`, `persistence/`, and `python_protocol/` directories.
- The root README and architecture documentation now distinguish design intent from implementation evidence.
- `OPENCODE.md` was removed from Git tracking at the user's request; `AGENTS.md` remains the tracked repository instruction file.
- The supervisor now owns durable admission, bounded Python task execution, cancellation confirmation, event streaming and explicit restart recovery.
- The local API exposes task submission, cancellation and cursor-replayable authenticated SSE; Electron main/preload/renderer use that path without exposing the token.
- The default Python provider reports deterministic workflow facts only; Ollama is explicit and fails without fallback when unavailable.
- The end-to-end Windows harness passed with real supervisor, Python, API, SSE and Electron renderer checks.

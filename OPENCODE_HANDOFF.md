# OpenCode handoff — Windows security, recovery and live acceptance

**Repository:** `ANUBprad/crane`  
**Branch:** `main`  
**Starting HEAD:** `b5c13d774782771b7e5e4dd6585d91e8a7fca6dc`  
**Latest Windows CI:** [Run 36](https://github.com/ANUBprad/crane/actions/runs/45192610227)  
**CI run ID:** `45192610227`  
**Run commit:** `b5c13d774782771b7e5e4dd6585d91e8a7fca6dc`

## Your assignment

Take over the remaining CRANE acceptance work. Do not stop after writing an analysis or plan: diagnose the current failure, fix it, run the relevant tests, and continue through the remaining Windows/Ollama acceptance scenarios that are safe to execute on this machine. Produce a final evidence-based acceptance report.

Work directly on `main`, in small focused commits. Fetch and inspect the current remote HEAD before editing; do not overwrite work another agent/user may have pushed since this handoff was created. Do not force-push. Stage only intended files. Keep the product CLI-first; do not add a dashboard or start unrelated refactors.

## Mandatory repository instructions

Read these before implementation:
- `AGENTS.md`
- `DEVELOPMENT_RULES.md`
- `ARCHITECTURE.md`
- Relevant sections of `PRODUCT_REQUIREMENTS.md`

The repository instructs coding agents to read `OPENCODE.md`, but that file was not present on `main` when this handoff was prepared. Check again; do not invent its contents.

## Latest CI analysis — exact evidence

Run 36 completed with **failure**:
- Windows runner: `windows-2025` / Windows Server 2025 x64
- Go setup: passed
- Python 3.12.7 setup: passed
- Locked Python dependency installation: passed
- Go formatting: passed
- `go vet ./...`: passed
- `go test ./... -count=1`: passed
- `python -m compileall -q crane`: passed
- Python unittest discovery: **failed**
- The Python suite ran **58 tests; 1 failure**
- Release build, packaged install smoke, reproducibility and Windows acceptance harness were skipped because the Python test step failed.

Failing test:
`python_runtime.test_generation.GenerationSafetyTests.test_validation_output_is_bounded_and_git_write_commands_are_rejected`

Failure location:
`crane/python_runtime/test_generation.py:183`

Assertion:
`self.assertEqual(result["status"], "succeeded")`

Actual:
`result["status"] == "failed"`

The CI log confirms the assertion but does not print the full `result` object, so the underlying command error is **not yet known**. Do not guess. Reproduce it on Windows, expose the returned `error`, `exit_code`, `stdout` and `stderr` in the failing assertion/log, identify the cause, then fix the implementation or test correctly. In particular, verify executable resolution for `python` on Windows and the bounded pipe-reader behavior; do not simply weaken/remove the assertion.

Full run and logs:
- Run: https://github.com/ANUBprad/crane/actions/runs/45192610227
- Job ID: `137754620268`
- Workflow file: `.github/workflows/windows-ci.yml`

Earlier run 35 also failed on Go formatting in `localapi/server_test.go`; the formatting correction is included in run 36, where formatting and all Go checks passed. Earlier run 33 failed formatting in `cmd/agent/stream_test.go`; also corrected before run 36.

## Workstream A — close Windows filesystem security gaps

Audit the current changes in:
- `crane/python_runtime/generation.py`
- `crane/python_runtime/workflow_engine.py`
- `crane/python_runtime/test_generation.py`

Review behavior rather than trusting comments. Include Windows-specific tests for:
- Drive-relative, drive-absolute, UNC and extended/device paths.
- Alternate data streams, reserved device names, trailing dots/spaces and traversal.
- Dangling symlinks, directory junctions, mount/reparse points and reparse points under staging.
- Workspace root and staging-root validation.
- Forged or tampered stage metadata and safe cleanup.
- Destination replacement/creation races and exact-path overwrite approval.
- Repair approval pause/resume without silently widening authority.
- Validation-command working directory, executable resolution, timeout and bounded stdout/stderr.
- Environment and subprocess exposure: document what is and is not sandboxed.

Do not claim complete race-free Windows filesystem isolation unless the implementation actually establishes it. Be explicit about residual TOCTOU and OS sandbox limitations. Add focused tests for any gaps found.

## Workstream B — event recovery and terminal rendering

Audit and run:
- `supervisor/event_hub_test.go`
- `localapi/server_test.go`
- `cmd/agent/stream_test.go`
- Existing persistence/event tests.

Verify:
- No event loss at replay/subscribe/publish boundaries.
- Sequence order and duplicate handling after reconnect.
- Last-Event-ID resumes after a real previously-seen event.
- Expired cursors return the documented recoverable task snapshot.
- Terminal task events are not lost when the task completes during reconnect.
- Assistant snapshots recover output when deltas were missed.
- Text mode keeps assistant output and progress diagnostics on the intended streams.
- JSONL stays valid, deduplicated and free of ANSI decoration.
- Non-TTY output and terminal resize behavior work.
- Race tests are deterministic and meaningful; avoid tests that pass without forcing the interleaving they claim to cover.

Fix every blocker found and run Go tests with race detection where supported.

## Workstream C — live Windows/Ollama acceptance on the user's machine

This work must use the actual local Windows host and the local Ollama endpoint/model. Do not label GitHub-hosted CI as a live Ollama test.

First inspect the machine safely:
- Confirm current repository path and current `main` HEAD.
- Check current CRANE supervisor/listener state before starting another instance. Do not kill unrelated processes or assume old PIDs are still valid.
- Check whether Ollama is reachable at `http://127.0.0.1:11434`.
- Check whether `llama3.2:3b` is installed. If absent, report it; do not silently download models.
- Configure `CRANE_MODEL_PROVIDER=ollama` and `CRANE_MODEL=llama3.2:3b` in the supervisor's environment before starting it. Setting these variables only in a separate CLI client process does not reconfigure an already-running supervisor.
- Use a temporary workspace and preserve the user's real project/data.

Run and capture evidence for:
1. `agent doctor`: Ollama endpoint, model availability, Python, SQLite, MCP and provider health.
2. A simple live model task; verify the persisted task result contains actual Ollama output rather than deterministic-local output.
3. A structured FastAPI + SQLite CRUD project-generation task in a temporary workspace. Verify expected files exist and validation runs.
4. A controlled, repairable validation failure. Verify CRANE proposes/applies only the approved repair paths, re-runs validation and reports the actual outcome.
5. Streaming while the model is generating, not just after task completion.
6. Disconnect/reconnect to a running task; verify cursor/snapshot recovery and no duplicate or lost assistant output.
7. Cancellation and failure behavior.
8. Terminal rendering in PowerShell: progress, assistant text, JSONL/non-TTY output, and resize where feasible. Test Windows PowerShell 5.1 and PowerShell 7 if both are installed.

Record exact commands, task IDs, model/provider, timestamps, results, and relevant output. Do not claim a scenario passed without executing it.

**Important safety boundary:** `scripts/acceptance-windows.ps1` and `scripts/acceptance-windows.ps1 -MinimalEnvironment` exercise installation/update/rollback/uninstall and temporarily modify the User PATH. Do not run those on the user's normal development machine unless the user has explicitly accepted that risk. Use a disposable VM for clean install/uninstall/PATH acceptance. The clean Windows 10/11 self-hosted runner workflow is manual and requires dedicated runners; a developer workstation is not clean-machine evidence.

## Workstream D — full CI and acceptance report

After fixes:
1. Run the narrowest affected tests first.
2. Run formatting, Go vet, Go tests, Python compile and the full Python suite.
3. Ensure run 36's Python failure is resolved.
4. Ensure the complete Windows CI pipeline reaches release build and packaged smoke steps; report any release/repro/acceptance steps that remain skipped.
5. Execute the live local Windows/Ollama tests above if this machine allows them.
6. Commit and push meaningful fixes to `main`; verify remote HEAD after every push.

Final report must separate:
- GitHub-hosted Windows CI evidence.
- Local developer-machine Windows/Ollama evidence.
- Clean physical/VM Windows 10/11 evidence.
- Tests skipped or blocked and why.
- Residual security limitations.

For each pass/fail include the CI run URL or exact command/output evidence, commit SHA, OS/PowerShell version, model/provider and task IDs where applicable. Do not turn partial evidence into a blanket “Windows accepted” claim.

## Current implementation state to audit

The current branch has recent commits:
- `74871f9` — initial workspace path/staging hardening and stream tests.
- `fa68846` — exact-path overwrite approval changes and additional tests.
- `1521f37` — SSE resume/expired-cursor regression tests.
- `b5c13d7` — Go formatting correction for API tests.

These are context, not proof that the code is correct. Inspect the actual current files and git diff before continuing.

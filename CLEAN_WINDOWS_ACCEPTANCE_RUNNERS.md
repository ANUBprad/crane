# CRANE — Clean Windows 10/11 acceptance runners

This workflow provides an explicit path to genuine Windows 10 and Windows 11 VM acceptance. It is manual-only and runs only when dispatched from \`main\`.

## Required isolated VMs

Prepare two dedicated, clean Windows x64 VMs:

| VM | Required GitHub Actions runner labels |
|---|---|
| Windows 10 x64 | \`self-hosted\`, \`windows\`, \`x64\`, \`crane-clean-win10\` |
| Windows 11 x64 | \`self-hosted\`, \`windows\`, \`x64\`, \`crane-clean-win11\` |

Install only what is needed to register and operate the GitHub Actions runner, plus PowerShell 7. Windows PowerShell 5.1 is built into supported Windows installations. Do not install Go, Python, Node.js, npm, Docker, or the CRANE development toolchain on these VMs.

The workflow builds the test release packages on a separate GitHub-hosted \`windows-2025\` runner, transfers those packages as a short-lived workflow artifact, then runs the real packaged lifecycle harness on the selected clean VM. The acceptance harness temporarily uses an isolated \`LOCALAPPDATA\` and modifies/restores the runner service account's User PATH for its PATH test. Use dedicated disposable VMs with no unrelated user PATH edits or workloads.

## Registering runners

1. Create a clean Windows 10 x64 VM and a clean Windows 11 x64 VM.
2. In GitHub, open **ANUBprad/crane → Settings → Actions → Runners → New self-hosted runner**.
3. Follow GitHub's on-screen Windows runner registration instructions on each VM. Use the labels shown above, one target label per machine.
4. Run the runner as a dedicated account on the disposable VM. Do not use the runner for pull requests or untrusted repositories/workflows.
5. Install PowerShell 7 on both VMs and confirm \`pwsh\` is available to the runner service account. Do not install Go/Python/Node.js.
6. Keep both VMs patched and dedicated to this repository's manual acceptance workflow.

A self-hosted runner executes repository workflow code on the VM. Keep it isolated and do not use these machines for sensitive personal or production workloads.

## Running acceptance

1. Confirm the intended runner is online in GitHub Settings.
2. Open **Actions → Clean Windows 10/11 acceptance → Run workflow**.
3. Select \`win10\` or \`win11\`, and dispatch from \`main\`.
4. The workflow builds three packaged acceptance versions on GitHub-hosted Windows, then runs \`scripts/acceptance-windows.ps1 -MinimalEnvironment\` under both Windows PowerShell 5.1 and PowerShell 7 on the selected VM.
5. Download the \`crane-physical-acceptance-<target>-<run-id>\` artifact. It contains machine metadata and acceptance logs.

The workflow checks the actual Windows build number and architecture before running. Windows 10 build numbers must be below 22000; Windows 11 builds must be 22000 or higher.

## Evidence boundaries

- A passing run is evidence for the selected VM, its recorded OS build, and both tested PowerShell versions.
- Run separately for Windows 10 and Windows 11. One successful target does not validate the other.
- The harness uses version-distinct packages built from the same workflow commit to exercise installation/update/rollback mechanics. It is not proof of a database migration between two independently developed product releases.
- This workflow does not validate Authenticode publisher identity or a public release host.
- GitHub-hosted \`windows-2025\` CI remains the normal regression gate. These self-hosted jobs are manual clean-machine acceptance only.

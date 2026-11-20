[CmdletBinding()]
param(
    [string]$ReleaseRoot = (Join-Path (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path 'build\release')
)

# Isolated Windows acceptance harness. It temporarily changes the process
# LOCALAPPDATA and, for the final PATH check, the real user PATH; the original
# PATH is restored in a finally block. Do not run on a machine with unsaved PATH edits.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
$testRoot = Join-Path $env:TEMP "crane-acceptance-$suffix"
$logPath = Join-Path $testRoot 'acceptance.log'
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null

function Write-Result([string]$message) {
    Write-Host $message
    Add-Content -LiteralPath $logPath -Value $message
}

function Assert-Step([bool]$condition, [string]$message) {
    if (-not $condition) {
        Write-Result "FAIL: $message"
        throw "Acceptance failure: $message"
    }
    Write-Result "PASS: $message"
}

function Invoke-Native([string]$file, [string[]]$arguments) {
    $output = & $file @arguments 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "$file $($arguments -join ' ') failed with exit ${code}: $($output -join [Environment]::NewLine)"
    }
    return [pscustomobject]@{ Output = ($output -join [Environment]::NewLine); Code = $code }
}

try {
    $release010 = Join-Path $ReleaseRoot '0.1.0'
    $release011 = Join-Path $ReleaseRoot '0.1.1'
    Assert-Step (Test-Path -LiteralPath (Join-Path $release010 'release-manifest.json')) 'Release 0.1.0 manifest exists'
    Assert-Step (Test-Path -LiteralPath (Join-Path $release011 'release-manifest.json')) 'Release 0.1.1 manifest exists'

    $fakeLocal = Join-Path $testRoot 'AppData\Local'
    New-Item -ItemType Directory -Path $fakeLocal -Force | Out-Null
    $env:LOCALAPPDATA = $fakeLocal
    $env:CRANE_SKIP_PATH_UPDATE = '1'
    $env:CRANE_RELEASE_SOURCE = $release010
    $installRoot = Join-Path $fakeLocal 'Programs\CRANE'
    $agentCmd = Join-Path $installRoot 'bin\agent.cmd'
    $dataRoot = Join-Path $fakeLocal 'CRANE'

    Write-Result "Test root: $testRoot"
    Write-Result "Fake LOCALAPPDATA: $fakeLocal"

    & (Join-Path $release010 'install.ps1') -Action Install -ReleaseSource $release010 -InstallRoot $installRoot
    Assert-Step (Test-Path -LiteralPath $agentCmd) 'Fresh install created bin\\agent.cmd'
    Assert-Step ((Get-Content -LiteralPath (Join-Path $installRoot 'current.txt')).Trim() -eq '0.1.0') 'Fresh install active version is 0.1.0'
    foreach ($name in @('config', 'data', 'auth', 'logs', 'mcp', 'tmp', 'cache')) {
        Assert-Step (Test-Path -LiteralPath (Join-Path $dataRoot $name) -PathType Container) "Install created data directory $name"
    }

    $versionResult = Invoke-Native $agentCmd @('--version')
    Assert-Step ($versionResult.Output -match '0\.1\.0') 'Installed agent reports version 0.1.0'

    $doctor = Invoke-Native $agentCmd @('doctor')
    foreach ($line in @('Active release: yes', 'Python runtime available: yes', 'SQLite available: yes', 'MCP modules available: yes', 'Installed release: 0.1.0')) {
        Assert-Step ($doctor.Output -match [regex]::Escape($line)) "Doctor reports $line"
    }
    Assert-Step ($doctor.Output -match 'Overall: (ok|degraded)') 'Doctor returns a real overall status'

    $run = Invoke-Native $agentCmd @('run', 'isolated acceptance task')
    Assert-Step ($run.Output -match 'task\s+(task-[^\s]+)\s+queued') 'agent run returned a durable task ID'
    $taskId = $Matches[1]

    $statusOutput = ''
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $status = Invoke-Native $agentCmd @('status', $taskId)
            $statusOutput = $status.Output
            if ($statusOutput -match 'Status: succeeded') { break }
        }
        catch { }
        Start-Sleep -Milliseconds 500
    }
    Assert-Step ($statusOutput -match 'Status: succeeded') 'Task reached succeeded status from a new CLI process'
    $result = Invoke-Native $agentCmd @('result', $taskId)
    Assert-Step ($result.Output -match 'execution_mode') 'agent result returned the persisted real result'

    $doctorLive = Invoke-Native $agentCmd @('doctor')
    Assert-Step ($doctorLive.Output -match 'MCP supervisor reachable: yes') 'Doctor reports live MCP controller after task execution'

    $sentinel = Join-Path $dataRoot 'data\acceptance-sentinel.txt'
    Set-Content -LiteralPath $sentinel -Value 'preserve-me'
    $tokenPath = Join-Path $dataRoot 'auth\token.json'
    Assert-Step (Test-Path -LiteralPath $tokenPath) 'Supervisor created the local API token state'

    $update = Invoke-Native $agentCmd @('update', '-source', $release011, '-version', '0.1.1')
    Assert-Step ($update.Output -match 'Updated CRANE to 0\.1\.1') 'agent update completed'
    Assert-Step ((Get-Content -LiteralPath (Join-Path $installRoot 'current.txt')).Trim() -eq '0.1.1') 'Update switched the active version to 0.1.1'
    Assert-Step ((Get-Content -LiteralPath (Join-Path $installRoot 'previous.txt')).Trim() -eq '0.1.0') 'Update recorded 0.1.0 for rollback'
    Assert-Step (Test-Path -LiteralPath (Join-Path $installRoot 'versions\0.1.0\agent.exe')) 'Update kept the previous binaries'
    Assert-Step ((Get-Content -LiteralPath $sentinel) -eq 'preserve-me') 'Update preserved user data'
    Assert-Step (Test-Path -LiteralPath $tokenPath) 'Update preserved local API credential state'

    $updatedVersion = Invoke-Native $agentCmd @('--version')
    Assert-Step ($updatedVersion.Output -match '0\.1\.1') 'Installed agent reports version 0.1.1 after update'
    $updatedStatus = Invoke-Native $agentCmd @('status', $taskId)
    Assert-Step ($updatedStatus.Output -match 'Status: succeeded') 'Task history survived the update'

    & (Join-Path $installRoot 'bin\install.ps1') -Action Rollback
    Assert-Step ((Get-Content -LiteralPath (Join-Path $installRoot 'current.txt')).Trim() -eq '0.1.0') 'Rollback restored active version 0.1.0'
    Assert-Step ((Get-Content -LiteralPath (Join-Path $installRoot 'previous.txt')).Trim() -eq '0.1.1') 'Rollback retained 0.1.1 for forward recovery'
    Assert-Step ((Get-Content -LiteralPath $sentinel) -eq 'preserve-me') 'Rollback preserved user data'
    $rolledBackVersion = Invoke-Native $agentCmd @('--version')
    Assert-Step ($rolledBackVersion.Output -match '0\.1\.0') 'Installed agent reports version 0.1.0 after rollback'
    $rolledBackStatus = Invoke-Native $agentCmd @('status', $taskId)
    Assert-Step ($rolledBackStatus.Output -match 'Status: succeeded') 'Task history survived rollback'

    $uninstall = Invoke-Native $agentCmd @('uninstall')
    Assert-Step ($uninstall.Output -match 'uninstall started') 'agent uninstall started a detached lifecycle process'
    for ($attempt = 0; $attempt -lt 80 -and (Test-Path -LiteralPath $installRoot); $attempt++) {
        Start-Sleep -Milliseconds 250
    }
    Assert-Step (-not (Test-Path -LiteralPath $installRoot)) 'Uninstall removed installed binaries'
    Assert-Step ((Get-Content -LiteralPath $sentinel) -eq 'preserve-me') 'Uninstall preserved user data by default'

    & (Join-Path $release010 'install.ps1') -Action Install -ReleaseSource $release010 -InstallRoot $installRoot
    $agentCmd = Join-Path $installRoot 'bin\agent.cmd'
    $removeData = Invoke-Native $agentCmd @('uninstall', '--remove-data', '--confirm', 'REMOVE-CRANE-DATA')
    Assert-Step ($removeData.Output -match 'uninstall started') 'agent uninstall accepted explicit confirmed data removal'
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        if (-not (Test-Path -LiteralPath $installRoot) -and -not (Test-Path -LiteralPath $sentinel)) { break }
        Start-Sleep -Milliseconds 250
    }
    Assert-Step (-not (Test-Path -LiteralPath $installRoot)) 'Confirmed uninstall removed installed binaries'
    Assert-Step (-not (Test-Path -LiteralPath $sentinel)) 'Confirmed uninstall removed CRANE-managed data'

    $tamperedRelease = Join-Path $testRoot 'tampered-release'
    Copy-Item -LiteralPath $release010 -Destination $tamperedRelease -Recurse -Force
    $manifestPath = Join-Path $tamperedRelease 'release-manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $manifest.signature.scheme = 'x509'
    $manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $manifestPath
    $tamperedFailed = $false
    try {
        & (Join-Path $tamperedRelease 'install.ps1') -Action Install -ReleaseSource $tamperedRelease -InstallRoot (Join-Path $testRoot 'Programs\Tampered') 2>&1 | Out-Null
    }
    catch { $tamperedFailed = $true }
    Assert-Step $tamperedFailed 'Installer rejected unsupported signature metadata'

    $hashTampered = Join-Path $testRoot 'hash-tampered-release'
    Copy-Item -LiteralPath $release010 -Destination $hashTampered -Recurse -Force
    $payload = Get-ChildItem -LiteralPath $hashTampered -Filter 'crane-windows-x64-*.zip' -File | Select-Object -First 1
    $bytes = [IO.File]::ReadAllBytes($payload.FullName)
    $bytes[0] = $bytes[0] -bxor 0xFF
    [IO.File]::WriteAllBytes($payload.FullName, $bytes)
    $hashFailed = $false
    try {
        & (Join-Path $hashTampered 'install.ps1') -Action Install -ReleaseSource $hashTampered -InstallRoot (Join-Path $testRoot 'Programs\HashTampered') 2>&1 | Out-Null
    }
    catch { $hashFailed = $true }
    Assert-Step $hashFailed 'Installer rejected a tampered payload checksum'

    $release012 = Join-Path $ReleaseRoot '0.1.2'
    Assert-Step (Test-Path -LiteralPath (Join-Path $release012 'release-manifest.json')) 'Release 0.1.2 manifest exists'

    $unsupportedRelease = Join-Path $testRoot 'unsupported-release'
    Copy-Item -LiteralPath $release010 -Destination $unsupportedRelease -Recurse -Force
    $manifestPath = Join-Path $unsupportedRelease 'release-manifest.json'
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $manifest.os = 'linux'
    $manifest.arch = 'arm64'
    $manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $manifestPath
    $unsupportedFailed = $false
    try {
        & (Join-Path $unsupportedRelease 'install.ps1') -Action Install -ReleaseSource $unsupportedRelease -InstallRoot (Join-Path $testRoot 'Programs\Unsupported') 2>&1 | Out-Null
    }
    catch { $unsupportedFailed = $true }
    Assert-Step $unsupportedFailed 'Installer rejected unsupported OS/architecture metadata'

    $savedLocal = $env:LOCALAPPDATA
    $rollbackDataLocal = Join-Path $testRoot 'rollback-local'
    $rollbackLocal = Join-Path $rollbackDataLocal 'Local'
    New-Item -ItemType Directory -Path $rollbackLocal -Force | Out-Null
    $env:LOCALAPPDATA = $rollbackLocal
    $rollbackRoot = Join-Path $rollbackLocal 'Programs\CRANE'
    try {
        & (Join-Path $release011 'install.ps1') -Action Install -ReleaseSource $release011 -InstallRoot $rollbackRoot
        $rollbackAgent = Join-Path $rollbackRoot 'bin\agent.cmd'
        $badDb = Join-Path $rollbackLocal 'CRANE\data\crane.db'
        if (Test-Path -LiteralPath $badDb) { Remove-Item -LiteralPath $badDb -Recurse -Force }
        New-Item -ItemType Directory -Path $badDb -Force | Out-Null
        $updateFailed = $false
        try {
            Invoke-Native $rollbackAgent @('update', '-source', $release012, '-version', '0.1.2') | Out-Null
        }
        catch { $updateFailed = $true }
        Assert-Step $updateFailed 'Update with an unhealthy target failed closed'
        Assert-Step ((Get-Content -LiteralPath (Join-Path $rollbackRoot 'current.txt')).Trim() -eq '0.1.1') 'Failed update restored the previous active version'
        Assert-Step (-not (Test-Path -LiteralPath (Join-Path $rollbackRoot 'previous.txt'))) 'Failed update cleared the temporary rollback pointer'
        Assert-Step (Test-Path -LiteralPath (Join-Path $rollbackRoot 'versions\0.1.1\agent.exe')) 'Failed update kept the previous binaries'
    }
    finally { $env:LOCALAPPDATA = $savedLocal }

    $originalUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $originalProcessPath = $env:Path
    $pathTestRoot = Join-Path $testRoot 'Programs\PathTest'
    $pathEntry = Join-Path $pathTestRoot 'bin'
    try {
        Remove-Item Env:\CRANE_SKIP_PATH_UPDATE -ErrorAction SilentlyContinue
        [Environment]::SetEnvironmentVariable('Path', 'C:\Windows\System32', 'User')
        $env:Path = 'C:\Windows\System32'
        & (Join-Path $release010 'install.ps1') -Action Install -ReleaseSource $release010 -InstallRoot $pathTestRoot
        $pathAfterInstall = [Environment]::GetEnvironmentVariable('Path', 'User')
        Assert-Step ($pathAfterInstall -like "*$pathEntry*") 'Installer added the CRANE bin directory to the user PATH'
        & (Join-Path $release010 'uninstall.ps1') -InstallRoot $pathTestRoot
        $pathAfterUninstall = [Environment]::GetEnvironmentVariable('Path', 'User')
        Assert-Step ($pathAfterUninstall -ceq 'C:\Windows\System32') 'Uninstall restored the user PATH exactly'
        Assert-Step (-not (Test-Path -LiteralPath $pathTestRoot)) 'PATH test uninstall removed installed binaries'
    }
    finally {
        [Environment]::SetEnvironmentVariable('Path', $originalUserPath, 'User')
        $env:Path = $originalProcessPath
        $env:CRANE_SKIP_PATH_UPDATE = '1'
    }

    Write-Result 'ACCEPTANCE RESULT: ALL ISOLATED CHECKS PASSED'
    Write-Result "Evidence log: $logPath"
}
catch {
    Write-Result "ACCEPTANCE RESULT: FAILED"
    Write-Result $_.ScriptStackTrace
    Write-Result $_.Exception.Message
    exit 1
}

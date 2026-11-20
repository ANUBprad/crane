[CmdletBinding()]
param(
    [ValidateSet('Install', 'Update', 'Rollback')]
    [string]$Action = 'Install',
    [string]$ReleaseSource = $env:CRANE_RELEASE_SOURCE,
    [string]$Version = '',
    [string]$InstallRoot = $env:CRANE_INSTALL_ROOT,
    [switch]$SkipPathUpdate
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:CRANE_SKIP_PATH_UPDATE -eq '1') { $SkipPathUpdate = $true }

function Write-Step([string]$message) {
    Write-Host "CRANE install: $message"
}

function Write-Warn([string]$message) {
    Write-Warning "CRANE install: $message"
}

function Get-Field([object]$object, [string]$name) {
    if ($null -eq $object) { return $null }
    $property = $object.PSObject.Properties[$name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Test-ValidVersion([string]$value) {
    if ([string]::IsNullOrWhiteSpace($value)) { return $false }
    if ($value.Length -gt 100) { return $false }
    return [regex]::IsMatch($value, '^[A-Za-z0-9][A-Za-z0-9._-]*$')
}

function Assert-WindowsX64 {
    if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
        throw 'CRANE supports Windows x64 only. This host or PowerShell process is not Windows x64.'
    }
    if ([Environment]::OSVersion.Version.Major -lt 10) {
        throw 'CRANE requires Windows 10 or later.'
    }
}

function Resolve-InstallRoot([string]$value) {
    if ([string]::IsNullOrWhiteSpace($value)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw 'LOCALAPPDATA is not set; cannot resolve the per-user installation root.'
        }
        $value = Join-Path $env:LOCALAPPDATA 'Programs\CRANE'
    }
    $full = [IO.Path]::GetFullPath($value).TrimEnd('\', '/')
    if ([string]::IsNullOrWhiteSpace($full)) { throw 'Invalid installation root.' }
    $root = [IO.Path]::GetPathRoot($full)
    if ($full -eq $root -or $full -eq $root.TrimEnd('\', '/')) {
        throw 'Refusing to use a drive root as the installation root.'
    }
    return $full
}

function Ensure-Directories([string]$root) {
    foreach ($path in @(
        $root,
        (Join-Path $root 'bin'),
        (Join-Path $root 'versions'),
        (Join-Path $root 'staging')
    )) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }

    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA is not set; cannot create the per-user data directories.'
    }
    $dataRoot = Join-Path $env:LOCALAPPDATA 'CRANE'
    foreach ($name in @('config', 'data', 'auth', 'logs', 'mcp', 'tmp', 'cache')) {
        New-Item -ItemType Directory -Path (Join-Path $dataRoot $name) -Force | Out-Null
    }
}

function Get-Sha256([string]$path) {
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function New-WorkDirectory {
    param(
        [string]$Prefix,
        [string]$Parent = ''
    )
    if ([string]::IsNullOrWhiteSpace($Parent)) { $Parent = [IO.Path]::GetTempPath() }
    $path = Join-Path $Parent ("$Prefix-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    return $path
}

function Remove-DirectoryIfPresent([string]$path) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

function Get-VersionPointer([string]$root, [string]$name) {
    $path = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return '' }
    $value = (Get-Content -LiteralPath $path -Raw).Trim()
    if (-not (Test-ValidVersion $value)) { return '' }
    return $value
}

function Set-VersionPointer([string]$root, [string]$name, [string]$version) {
    if (-not (Test-ValidVersion $version)) { throw "Refusing to write an invalid version pointer: $version" }
    $path = Join-Path $root $name
    $temporary = "$path.tmp"
    [IO.File]::WriteAllText($temporary, $version + "`n", [Text.Encoding]::ASCII)
    Move-Item -LiteralPath $temporary -Destination $path -Force
}

function Remove-VersionPointer([string]$root, [string]$name) {
    $path = Join-Path $root $name
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
}

function Compare-Version([string]$left, [string]$right) {
    $leftBase = ($left -split '-', 2)[0]
    $rightBase = ($right -split '-', 2)[0]
    $leftParts = @($leftBase.Split('.') | ForEach-Object {
        $value = 0
        if (-not [int]::TryParse($_, [ref]$value)) { throw "Version '$left' is not numeric and cannot be compared." }
        $value
    })
    $rightParts = @($rightBase.Split('.') | ForEach-Object {
        $value = 0
        if (-not [int]::TryParse($_, [ref]$value)) { throw "Version '$right' is not numeric and cannot be compared." }
        $value
    })
    $length = [Math]::Max($leftParts.Count, $rightParts.Count)
    for ($index = 0; $index -lt $length; $index++) {
        $leftValue = if ($index -lt $leftParts.Count) { [int]$leftParts[$index] } else { 0 }
        $rightValue = if ($index -lt $rightParts.Count) { [int]$rightParts[$index] } else { 0 }
        if ($leftValue -gt $rightValue) { return 1 }
        if ($leftValue -lt $rightValue) { return -1 }
    }
    return 0
}

function Normalize-PathForCompare([string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return '' }
    $trimmed = $path.Trim().TrimEnd('"')
    $expanded = [Environment]::ExpandEnvironmentVariables($trimmed)
    try {
        return [IO.Path]::GetFullPath($expanded).TrimEnd('\', '/')
    }
    catch {
        return $expanded.TrimEnd('\', '/')
    }
}

function Get-UserPath {
    $value = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($null -eq $value) { return '' }
    return $value
}

function Set-UserPath([string]$value) {
    [Environment]::SetEnvironmentVariable('Path', $value, 'User')
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if ([string]::IsNullOrWhiteSpace($machinePath)) { $env:Path = $value } else { $env:Path = "$machinePath;$value" }
}

function Add-PathEntry([string]$entry) {
    $current = Get-UserPath
    $parts = @($current -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
    $target = Normalize-PathForCompare $entry
    foreach ($part in $parts) {
        if ((Normalize-PathForCompare $part) -ieq $target) { return }
    }
    $parts += $entry
    Set-UserPath ($parts -join ';')
    Write-Step "Added $entry to the user PATH."
}

function Remove-PathEntries([string[]]$entries) {
    $current = Get-UserPath
    $parts = @($current -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
    $targets = @($entries | ForEach-Object { Normalize-PathForCompare $_ })
    $kept = @()
    foreach ($part in $parts) {
        $normalized = Normalize-PathForCompare $part
        $match = $false
        foreach ($target in $targets) {
            if ($normalized -ieq $target) { $match = $true; break }
        }
        if (-not $match) { $kept += $part }
    }
    if ($kept.Count -ne $parts.Count) {
        Set-UserPath ($kept -join ';')
        Write-Step 'Removed CRANE entries from the user PATH.'
    }
}

function Get-AncestorProcessIds {
    $ids = @()
    $current = [int]$PID
    while ($current -gt 0 -and $ids -notcontains $current) {
        $ids += $current
        $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $current" -ErrorAction SilentlyContinue
        if ($null -eq $process) { break }
        $current = [int]$process.ParentProcessId
    }
    return $ids
}

function Stop-CraneProcesses([string]$root) {
    $ancestors = @(Get-AncestorProcessIds)
    $rootPrefix = (Normalize-PathForCompare $root) + '\'
    $processes = Get-CimInstance -ClassName Win32_Process -Filter "Name = 'agent.exe' OR Name = 'python.exe'" -ErrorAction SilentlyContinue
    foreach ($process in $processes) {
        $processId = [int]$process.ProcessId
        if ($ancestors -contains $processId) { continue }
        if ([string]::IsNullOrWhiteSpace($process.ExecutablePath)) { continue }
        $executable = Normalize-PathForCompare $process.ExecutablePath
        if (-not $executable.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) { continue }
        try {
            Stop-Process -Id $processId -Force -ErrorAction Stop
            Write-Step "Stopped CRANE process $processId."
        }
        catch {
            Write-Warn "Could not stop CRANE process ${processId}: $($_.Exception.Message)"
        }
    }
    Start-Sleep -Milliseconds 500
}

function ConvertTo-ProcessArgument([string]$value) {
    if ($null -eq $value) { return '""' }
    return '"' + $value.Replace('"', '\"') + '"'
}

function Invoke-CommandChecked {
    param(
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [hashtable]$EnvironmentVariables = @{},
        [string]$WorkingDirectory = '',
        [int]$TimeoutMs = 180000
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (@($Arguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) { $startInfo.WorkingDirectory = $WorkingDirectory }
    foreach ($key in $EnvironmentVariables.Keys) {
        $startInfo.EnvironmentVariables[$key] = [string]$EnvironmentVariables[$key]
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try { [void]$process.Start() } catch { throw "Could not start $FilePath : $($_.Exception.Message)" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($TimeoutMs)) {
        try { $process.Kill() } catch { }
        throw "$FilePath did not finish within $TimeoutMs ms."
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Stdout = $stdout; Stderr = $stderr }
}

function Assert-CommandSuccess([object]$result, [string]$label) {
    if ($result.ExitCode -eq 0) { return }
    $detail = $result.Stderr.Trim()
    if ([string]::IsNullOrWhiteSpace($detail)) { $detail = $result.Stdout.Trim() }
    if ($detail.Length -gt 2000) { $detail = $detail.Substring(0, 2000) }
    throw "$label failed (exit $($result.ExitCode)): $detail"
}

function Assert-SafeArtifactName([string]$name) {
    if ([string]::IsNullOrWhiteSpace($name)) { throw 'An artifact name is empty.' }
    if ($name -ne [IO.Path]::GetFileName($name)) { throw "Unsafe artifact name: $name" }
    if ($name -match '[\r\n:]') { throw "Unsafe artifact name: $name" }
    if ($name -in @('.', '..')) { throw "Unsafe artifact name: $name" }
}

function Save-RemoteFile([string]$source, [string]$name, [string]$destination) {
    $url = $source.TrimEnd('/') + '/' + [uri]::EscapeDataString($name)
    Invoke-WebRequest -Uri $url -OutFile $destination -UseBasicParsing -TimeoutSec 120
}

function Resolve-SourcePath([string]$source, [string]$name, [string]$localSource) {
    if ($localSource) {
        $path = [IO.Path]::GetFullPath((Join-Path $localSource $name))
        $prefix = (Normalize-PathForCompare $localSource) + '\'
        if (-not (Normalize-PathForCompare $path).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Artifact path escapes the release source: $name"
        }
        return $path
    }
    $path = [IO.Path]::GetFullPath((Join-Path (New-WorkDirectory 'crane-download') $name))
    Save-RemoteFile $source $name $path
    return $path
}

function Get-ManifestArtifact([object]$manifest, [string]$kind) {
    $matches = @(@($manifest.artifacts) | Where-Object { (Get-Field $_ 'kind') -eq $kind })
    if ($matches.Count -ne 1) { throw "The release manifest must contain exactly one $kind artifact." }
    return $matches[0]
}

function Assert-Artifact([object]$artifact, [string]$path) {
    $name = Get-Field $artifact 'name'
    $expectedHash = ([string](Get-Field $artifact 'sha256')).ToLowerInvariant()
    $expectedSize = [int64](Get-Field $artifact 'size')
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Artifact is missing: $name" }
    $actualSize = (Get-Item -LiteralPath $path).Length
    if ($actualSize -ne $expectedSize) { throw "Artifact size mismatch for $name." }
    if ((Get-Sha256 $path) -ne $expectedHash) { throw "Artifact SHA-256 mismatch for $name." }
}

function Read-Manifest([string]$path) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'release-manifest.json is missing from the release source.' }
    try { $manifest = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } catch { throw "release-manifest.json is invalid: $($_.Exception.Message)" }
    if ((Get-Field $manifest 'schema_version') -ne 1) { throw 'Unsupported release manifest schema.' }
    $manifestVersion = [string](Get-Field $manifest 'version')
    if (-not (Test-ValidVersion $manifestVersion)) { throw 'The release manifest version is invalid.' }
    if ((Get-Field $manifest 'os') -ne 'windows' -or (Get-Field $manifest 'arch') -ne 'x64') {
        throw 'The release manifest does not describe a Windows x64 build.'
    }
    foreach ($field in @('python_version', 'go_version', 'commit', 'build_date')) {
        if ([string]::IsNullOrWhiteSpace([string](Get-Field $manifest $field))) { throw "The release manifest is missing $field." }
    }
    $signature = Get-Field $manifest 'signature'
    if ((Get-Field $signature 'scheme') -ne 'none' -or (Get-Field $signature 'status') -ne 'unverified') {
        throw 'This installer only accepts artifacts that explicitly declare no signature; it cannot verify the declared scheme.'
    }
    $artifacts = @(Get-Field $manifest 'artifacts')
    if ($artifacts.Count -lt 4) { throw 'The release manifest artifact list is incomplete.' }
    $seen = @{}
    foreach ($artifact in $artifacts) {
        $name = [string](Get-Field $artifact 'name')
        Assert-SafeArtifactName $name
        $kind = [string](Get-Field $artifact 'kind')
        if ($kind -notin @('payload', 'installer', 'uninstaller', 'dependency_lock')) { throw "Unknown artifact kind: $kind" }
        if ($seen.ContainsKey($kind)) { throw "Duplicate artifact kind: $kind" }
        $seen[$kind] = $true
        $hash = [string](Get-Field $artifact 'sha256')
        if ($hash -notmatch '^[0-9a-fA-F]{64}$') { throw "Invalid SHA-256 for artifact $name." }
        $size = [string](Get-Field $artifact 'size')
        if ($size -notmatch '^\d+$' -or [int64]$size -le 0) { throw "Invalid size for artifact $name." }
    }
    foreach ($kind in @('payload', 'installer', 'uninstaller', 'dependency_lock')) {
        if (-not $seen.ContainsKey($kind)) { throw "The release manifest is missing the $kind artifact." }
    }
    return $manifest
}

function Assert-Checksums([object]$manifest, [string]$path) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'SHA256SUMS.txt is missing from the release source.' }
    $entries = @{}
    foreach ($line in (Get-Content -LiteralPath $path)) {
        if ($line -match '^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$') {
            $name = $Matches[2]
            if ($entries.ContainsKey($name)) { throw "Duplicate checksum entry: $name" }
            $entries[$name] = $Matches[1].ToLowerInvariant()
        }
    }
    foreach ($artifact in $manifest.artifacts) {
        $name = [string](Get-Field $artifact 'name')
        if (-not $entries.ContainsKey($name)) { throw "SHA256SUMS.txt has no entry for $name." }
        if ($entries[$name] -ne ([string](Get-Field $artifact 'sha256')).ToLowerInvariant()) {
            throw "SHA256SUMS.txt disagrees with the manifest for $name."
        }
    }
}

function Expand-VerifiedPayload([string]$payloadPath, [string]$targetDirectory) {
    New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($payloadPath)
    try {
        $prefix = (Normalize-PathForCompare $targetDirectory) + '\'
        foreach ($entry in $archive.Entries) {
            $name = $entry.FullName.Replace('/', '\')
            if ([string]::IsNullOrWhiteSpace($name)) { throw 'The payload contains an empty entry name.' }
            if ($name.Contains(':') -or [IO.Path]::IsPathRooted($name)) { throw "Unsafe payload entry: $name" }
            $segments = @($name.Split('\'))
            if ($segments -contains '..') { throw "Unsafe payload entry: $name" }
            $destination = [IO.Path]::GetFullPath((Join-Path $targetDirectory $name))
            if (-not (Normalize-PathForCompare $destination).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Payload entry escapes the staging directory: $name"
            }
            if ($name.EndsWith('\')) {
                New-Item -ItemType Directory -Path $destination -Force | Out-Null
                continue
            }
            $parent = [IO.Path]::GetDirectoryName($destination)
            if (-not [string]::IsNullOrWhiteSpace($parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
            $sourceStream = $entry.Open()
            $targetStream = [IO.File]::Create($destination)
            try { $sourceStream.CopyTo($targetStream) } finally { $sourceStream.Dispose(); $targetStream.Dispose() }
        }
    }
    finally { $archive.Dispose() }
}

function Assert-StagedPayload([object]$manifest, [string]$stagedDirectory) {
    $version = [string](Get-Field $manifest 'version')
    foreach ($relative in @(
        'agent.exe',
        'install.json',
        'runtime\python\python.exe',
        'runtime\app\crane\python_runtime\__main__.py',
        'runtime\app\crane\mcp_runtime\__init__.py'
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $stagedDirectory $relative) -PathType Leaf)) {
            throw "The verified payload is missing $relative."
        }
    }

    $installInfoPath = Join-Path $stagedDirectory 'install.json'
    try { $installInfo = Get-Content -LiteralPath $installInfoPath -Raw | ConvertFrom-Json } catch { throw "install.json is invalid: $($_.Exception.Message)" }
    foreach ($field in @('version', 'os', 'arch', 'commit')) {
        if (-not (Get-Field $installInfo $field)) { throw "install.json is missing $field." }
    }
    if ((Get-Field $installInfo 'version') -ne $version) { throw 'install.json version does not match the release manifest.' }
    if ((Get-Field $installInfo 'os') -ne 'windows' -or (Get-Field $installInfo 'arch') -ne 'x64') {
        throw 'install.json does not describe a Windows x64 build.'
    }
    if ((Get-Field $installInfo 'commit') -ne (Get-Field $manifest 'commit')) { throw 'install.json commit does not match the release manifest.' }

    $agentExe = Join-Path $stagedDirectory 'agent.exe'
    $versionResult = Invoke-CommandChecked -FilePath $agentExe -Arguments @('--version') -TimeoutMs 30000
    Assert-CommandSuccess $versionResult 'agent.exe --version'
    if ($versionResult.Stdout -notmatch [regex]::Escape($version)) { throw 'agent.exe reported an unexpected version.' }

    $pythonExe = Join-Path $stagedDirectory 'runtime\python\python.exe'
    $appDirectory = Join-Path $stagedDirectory 'runtime\app'
    $isolatedEnvironment = @{
        PYTHONPATH        = ''
        CRANE_PYTHON      = ''
        CRANE_PYTHON_HOME = ''
    }
    $health = Invoke-CommandChecked -FilePath $pythonExe -Arguments @('-m', 'crane.python_runtime', '--health') -EnvironmentVariables $isolatedEnvironment -WorkingDirectory $appDirectory -TimeoutMs 60000
    Assert-CommandSuccess $health 'packaged Python health check'
    try { $healthJson = $health.Stdout | ConvertFrom-Json } catch { throw 'Packaged Python returned invalid health JSON.' }
    if ((Get-Field $healthJson 'status') -ne 'healthy') { throw 'Packaged Python health check did not report healthy.' }

    $importCode = 'import langchain_core, langgraph, langchain_mcp_adapters, langchain_ollama, ollama, mcp, jsonschema, sqlite3; print(1)'
    $imports = Invoke-CommandChecked -FilePath $pythonExe -Arguments @('-c', $importCode) -EnvironmentVariables $isolatedEnvironment -WorkingDirectory $appDirectory -TimeoutMs 120000
    Assert-CommandSuccess $imports 'packaged Python import check'
    if ($imports.Stdout.Trim() -ne '1') { throw 'Packaged Python dependency imports did not pass.' }
}

function Resolve-Release([string]$source, [string]$localSource, [string]$workDirectory) {
    if (-not $localSource -and $source -notmatch '^https?://') { throw "-ReleaseSource must be an existing directory or an http(s) URL." }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $manifestPath = Resolve-SourcePath $source 'release-manifest.json' $localSource
    $manifest = Read-Manifest $manifestPath
    $checksumsPath = Resolve-SourcePath $source 'SHA256SUMS.txt' $localSource
    Assert-Checksums $manifest $checksumsPath

    $artifactPaths = @{}
    foreach ($artifact in $manifest.artifacts) {
        $name = [string](Get-Field $artifact 'name')
        $path = Resolve-SourcePath $source $name $localSource
        Assert-Artifact $artifact $path
        $artifactPaths[(Get-Field $artifact 'kind')] = $path
    }

    $payloadDirectory = Join-Path $workDirectory 'payload'
    Expand-VerifiedPayload $artifactPaths['payload'] $payloadDirectory
    Assert-StagedPayload $manifest $payloadDirectory

    return [pscustomobject]@{
        Manifest         = $manifest
        ArtifactPaths    = $artifactPaths
        PayloadDirectory = $payloadDirectory
    }
}

function Test-VersionInstallation([string]$root, [string]$version) {
    $versionDirectory = Join-Path $root "versions\$version"
    $agentExe = Join-Path $versionDirectory 'agent.exe'
    $installInfoPath = Join-Path $versionDirectory 'install.json'
    if (-not (Test-Path -LiteralPath $agentExe -PathType Leaf)) { return $false }
    if (-not (Test-Path -LiteralPath $installInfoPath -PathType Leaf)) { return $false }
    if (-not (Test-Path -LiteralPath (Join-Path $versionDirectory 'runtime\python\python.exe') -PathType Leaf)) { return $false }
    try { $info = Get-Content -LiteralPath $installInfoPath -Raw | ConvertFrom-Json } catch { return $false }
    if ((Get-Field $info 'version') -ne $version) { return $false }
    $result = Invoke-CommandChecked -FilePath $agentExe -Arguments @('--version') -TimeoutMs 30000
    if ($result.ExitCode -ne 0 -or $result.Stdout -notmatch [regex]::Escape($version)) { return $false }
    return $true
}

function Assert-InstallationHealth([string]$root, [string]$expectedVersion) {
    $agentExe = Join-Path $root "versions\$expectedVersion\agent.exe"
    $result = Invoke-CommandChecked -FilePath $agentExe -Arguments @('doctor') -EnvironmentVariables @{
        CRANE_INSTALL_ROOT = $root
    } -TimeoutMs 180000
    Assert-CommandSuccess $result 'agent doctor'
    if ($result.Stdout -notmatch [regex]::Escape($expectedVersion)) {
        throw 'agent doctor did not report the expected installed version.'
    }
}

function Write-Launcher([string]$root) {
    $content = @'
@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "CRANE_INSTALL_ROOT=%%~fI"
set "CRANE_VERSION="
for /f "usebackq delims=" %%V in ("%CRANE_INSTALL_ROOT%\current.txt") do set "CRANE_VERSION=%%V"
if not defined CRANE_VERSION (
  echo CRANE: active version pointer is missing or invalid 1>&2
  exit /b 1
)
"%CRANE_INSTALL_ROOT%\versions\%CRANE_VERSION%\agent.exe" %*
exit /b %ERRORLEVEL%
'@
    [IO.File]::WriteAllText((Join-Path $root 'bin\agent.cmd'), $content + "`r`n", [Text.Encoding]::ASCII)
}

function Write-LifecycleScripts([string]$root, [object]$artifactPaths) {
    Copy-Item -LiteralPath $artifactPaths['installer'] -Destination (Join-Path $root 'bin\install.ps1') -Force
    Copy-Item -LiteralPath $artifactPaths['uninstaller'] -Destination (Join-Path $root 'bin\uninstall.ps1') -Force
    Write-Launcher $root
}

function Install-VersionDirectory([string]$root, [string]$version, [string]$stagedDirectory, [string[]]$protectedVersions) {
    $target = Join-Path $root "versions\$version"
    if (Test-Path -LiteralPath $target) {
        if ($protectedVersions -contains $version) { throw "Version $version is currently referenced by a version pointer and cannot be replaced." }
        Remove-DirectoryIfPresent $target
    }
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    Copy-Item -Path (Join-Path $stagedDirectory '*') -Destination $target -Recurse -Force
    return $target
}

function Remove-VersionDirectory([string]$root, [string]$version) {
    Remove-DirectoryIfPresent (Join-Path $root "versions\$version")
}

function Get-RequestedVersion([object]$manifest) {
    $manifestVersion = [string](Get-Field $manifest 'version')
    if (-not [string]::IsNullOrWhiteSpace($Version) -and $Version -ne $manifestVersion) {
        throw "Requested version $Version does not match release manifest version $manifestVersion."
    }
    return $manifestVersion
}

function Invoke-Install {
    param([string]$root)

    if ([string]::IsNullOrWhiteSpace($ReleaseSource)) {
        if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot) -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'release-manifest.json'))) {
            $ReleaseSource = $PSScriptRoot
        }
        else {
            throw 'Install requires -ReleaseSource or CRANE_RELEASE_SOURCE.'
        }
    }
    $localSource = $ReleaseSource -notmatch '^https?://'
    if ($localSource) {
        if (-not (Test-Path -LiteralPath $ReleaseSource -PathType Container)) { throw "Release source directory does not exist: $ReleaseSource" }
        $ReleaseSource = [IO.Path]::GetFullPath($ReleaseSource).TrimEnd('\', '/')
    }

    $active = Get-VersionPointer $root 'current.txt'
    $workDirectory = New-WorkDirectory 'crane-install' (Join-Path $root 'staging')
    try {
        $release = Resolve-Release $ReleaseSource $localSource $workDirectory
        $targetVersion = Get-RequestedVersion $release.Manifest

        if ($active -eq $targetVersion) {
            if (-not (Test-VersionInstallation $root $targetVersion)) { throw "Active version $targetVersion is incomplete." }
            Write-LifecycleScripts $root $release.ArtifactPaths
            if (-not $SkipPathUpdate) { Add-PathEntry (Join-Path $root 'bin') }
            Assert-InstallationHealth $root $targetVersion
            Write-Step "CRANE $targetVersion is already installed and healthy."
            return
        }
        if (-not [string]::IsNullOrWhiteSpace($active)) {
            throw "CRANE $active is already installed. Use -Action Update to change versions."
        }

        Write-Step "Installing CRANE $targetVersion."
        $installedDirectory = Install-VersionDirectory $root $targetVersion $release.PayloadDirectory @()
        try {
            Stop-CraneProcesses $root
            Set-VersionPointer $root 'current.txt' $targetVersion
            Remove-VersionPointer $root 'previous.txt'
            Assert-InstallationHealth $root $targetVersion
            Write-LifecycleScripts $root $release.ArtifactPaths
            if (-not $SkipPathUpdate) { Add-PathEntry (Join-Path $root 'bin') }
        }
        catch {
            Stop-CraneProcesses $root
            Remove-VersionPointer $root 'current.txt'
            Remove-VersionPointer $root 'previous.txt'
            Remove-VersionDirectory $root $targetVersion
            throw
        }
        Write-Step "Installed CRANE $targetVersion."
    }
    finally { Remove-DirectoryIfPresent $workDirectory }
}

function Invoke-Update {
    param([string]$root)

    $active = Get-VersionPointer $root 'current.txt'
    if ([string]::IsNullOrWhiteSpace($active)) { throw 'No active CRANE installation was found. Use -Action Install.' }
    if ([string]::IsNullOrWhiteSpace($ReleaseSource)) {
        if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot) -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'release-manifest.json'))) {
            $ReleaseSource = $PSScriptRoot
        }
        else {
            throw 'Update requires -ReleaseSource or CRANE_RELEASE_SOURCE.'
        }
    }
    $localSource = $ReleaseSource -notmatch '^https?://'
    if ($localSource) {
        if (-not (Test-Path -LiteralPath $ReleaseSource -PathType Container)) { throw "Release source directory does not exist: $ReleaseSource" }
        $ReleaseSource = [IO.Path]::GetFullPath($ReleaseSource).TrimEnd('\', '/')
    }

    $previous = Get-VersionPointer $root 'previous.txt'
    $workDirectory = New-WorkDirectory 'crane-update' (Join-Path $root 'staging')
    $currentWasSwitched = $false
    try {
        $release = Resolve-Release $ReleaseSource $localSource $workDirectory
        $targetVersion = Get-RequestedVersion $release.Manifest
        if ($targetVersion -eq $active) {
            if (-not (Test-VersionInstallation $root $active)) { throw "Active version $active is incomplete." }
            Write-LifecycleScripts $root $release.ArtifactPaths
            if (-not $SkipPathUpdate) { Add-PathEntry (Join-Path $root 'bin') }
            Assert-InstallationHealth $root $active
            Write-Step "CRANE $active is already installed and healthy."
            return
        }
        $comparison = Compare-Version $targetVersion $active
        if ($comparison -lt 0) { throw "Refusing to downgrade from $active to $targetVersion. Use -Action Rollback to restore the previous version." }
        if ($comparison -eq 0) { throw "Version $targetVersion is not newer than active version $active." }

        Write-Step "Updating CRANE $active to $targetVersion."
        Install-VersionDirectory $root $targetVersion $release.PayloadDirectory @($active, $previous)
        Stop-CraneProcesses $root
        Set-VersionPointer $root 'previous.txt' $active
        Set-VersionPointer $root 'current.txt' $targetVersion
        $currentWasSwitched = $true
        Assert-InstallationHealth $root $targetVersion
        Write-LifecycleScripts $root $release.ArtifactPaths
        if (-not $SkipPathUpdate) { Add-PathEntry (Join-Path $root 'bin') }
    }
    catch {
        if ($currentWasSwitched) {
            Stop-CraneProcesses $root
            Set-VersionPointer $root 'current.txt' $active
            if ([string]::IsNullOrWhiteSpace($previous)) { Remove-VersionPointer $root 'previous.txt' } else { Set-VersionPointer $root 'previous.txt' $previous }
            Write-Warn "The update failed. Restored active version $active."
        }
        throw
    }
    finally { Remove-DirectoryIfPresent $workDirectory }
    Write-Step "Updated CRANE to $targetVersion. Previous version $active is available for rollback."
}

function Invoke-Rollback {
    param([string]$root)

    $active = Get-VersionPointer $root 'current.txt'
    $previous = Get-VersionPointer $root 'previous.txt'
    if ([string]::IsNullOrWhiteSpace($active)) { throw 'No active CRANE installation was found.' }
    if ([string]::IsNullOrWhiteSpace($previous)) { throw 'No previous version is available for rollback.' }
    if ($active -eq $previous) { throw 'The previous version pointer is the same as the active version.' }
    if (-not (Test-VersionInstallation $root $previous)) { throw "Previous version $previous is incomplete and cannot be restored." }

    $restoredForward = ''
    if (Test-VersionInstallation $root $active) { $restoredForward = $active }
    $currentWasSwitched = $false
    try {
        Write-Step "Rolling back CRANE $active to $previous."
        Stop-CraneProcesses $root
        Set-VersionPointer $root 'current.txt' $previous
        $currentWasSwitched = $true
        if ([string]::IsNullOrWhiteSpace($restoredForward)) { Remove-VersionPointer $root 'previous.txt' } else { Set-VersionPointer $root 'previous.txt' $restoredForward }
        Assert-InstallationHealth $root $previous
        $agentCmd = Join-Path $root 'bin\agent.cmd'
        if (Test-Path -LiteralPath $agentCmd -PathType Leaf) { Write-Launcher $root }
        if (-not $SkipPathUpdate) { Add-PathEntry (Join-Path $root 'bin') }
    }
    catch {
        if ($currentWasSwitched) {
            Stop-CraneProcesses $root
            Set-VersionPointer $root 'current.txt' $active
            Set-VersionPointer $root 'previous.txt' $previous
            Write-Warn "Rollback failed. Restored active version $active."
        }
        throw
    }
    Write-Step "Rolled back CRANE to $previous. User data was not changed."
}

Assert-WindowsX64
$root = Resolve-InstallRoot $InstallRoot
Ensure-Directories $root
Write-Step "Installation root: $root"

switch ($Action) {
    'Install'  { Invoke-Install $root }
    'Update'   { Invoke-Update $root }
    'Rollback' { Invoke-Rollback $root }
}

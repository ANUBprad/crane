[CmdletBinding()]
param(
    [switch]$RemoveData,
    [string]$Confirm = '',
    [string]$InstallRoot = $env:CRANE_INSTALL_ROOT,
    [switch]$SkipPathUpdate,
    [switch]$InternalCopy
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step([string]$message) {
    Write-Host "CRANE uninstall: $message"
}

function Write-Warn([string]$message) {
    Write-Warning "CRANE uninstall: $message"
}

if (-not $InternalCopy -and -not [string]::IsNullOrWhiteSpace($PSCommandPath) -and (Test-Path -LiteralPath $PSCommandPath -PathType Leaf)) {
    $copy = Join-Path ([IO.Path]::GetTempPath()) ("crane-uninstall-" + [guid]::NewGuid().ToString('N') + ".ps1")
    Copy-Item -LiteralPath $PSCommandPath -Destination $copy -Force
    $forward = @()
    if ($RemoveData) { $forward += '-RemoveData' }
    if (-not [string]::IsNullOrWhiteSpace($Confirm)) { $forward += @('-Confirm', $Confirm) }
    if (-not [string]::IsNullOrWhiteSpace($InstallRoot)) { $forward += @('-InstallRoot', $InstallRoot) }
    if ($SkipPathUpdate) { $forward += '-SkipPathUpdate' }
    try {
        & $copy @forward -InternalCopy
        $exitCode = $LASTEXITCODE
    }
    finally {
        Remove-Item -LiteralPath $copy -Force -ErrorAction SilentlyContinue
    }
    if ($null -eq $exitCode) { $exitCode = 0 }
    exit $exitCode
}

function Assert-Windows {
    if ($env:OS -ne 'Windows_NT') { throw 'CRANE uninstall supports Windows only.' }
}

function Resolve-InstallRoot([string]$value) {
    if ([string]::IsNullOrWhiteSpace($value)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw 'LOCALAPPDATA is not set; cannot resolve the default installation root.'
        }
        $value = Join-Path $env:LOCALAPPDATA 'Programs\CRANE'
    }
    $full = [IO.Path]::GetFullPath($value).TrimEnd('\', '/')
    $root = [IO.Path]::GetPathRoot($full)
    if ($full -eq $root -or $full -eq $root.TrimEnd('\', '/')) {
        throw 'Refusing to uninstall from a drive root.'
    }
    return $full
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

function Assert-NoReparsePoints([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    $rootItem = Get-Item -LiteralPath $path -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to remove a reparse point: $path"
    }
    $links = @(Get-ChildItem -LiteralPath $path -Recurse -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue)
    if ($links.Count -gt 0) {
        throw "Refusing to remove $path because it contains a reparse point."
    }
}

function Remove-KnownDirectory([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    Assert-NoReparsePoints $path
    Remove-Item -LiteralPath $path -Recurse -Force
}

function Remove-KnownFile([string]$path) {
    if (Test-Path -LiteralPath $path -PathType Leaf) { Remove-Item -LiteralPath $path -Force }
}

function Remove-EmptyDirectoryIfPossible([string]$path) {
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { return }
    Assert-NoReparsePoints $path
    if (@(Get-ChildItem -LiteralPath $path -Force).Count -eq 0) {
        Remove-Item -LiteralPath $path -Force
    }
}

Assert-Windows
if ($RemoveData -and $Confirm -cne 'REMOVE-CRANE-DATA') {
    throw 'Removing CRANE user data requires -Confirm REMOVE-CRANE-DATA.'
}

$root = Resolve-InstallRoot $InstallRoot
Write-Step "Installation root: $root"

if (Test-Path -LiteralPath $root -PathType Container) {
    Assert-NoReparsePoints $root
    Stop-CraneProcesses $root
    foreach ($name in @('bin', 'versions', 'staging')) {
        Remove-KnownDirectory (Join-Path $root $name)
    }
    foreach ($name in @('current.txt', 'previous.txt', 'install.receipt.json')) {
        Remove-KnownFile (Join-Path $root $name)
    }
    Remove-EmptyDirectoryIfPossible $root
    Write-Step 'Removed CRANE binaries, runtime files, and version pointers.'
}
else {
    Write-Step 'No CRANE installation directory was found.'
}

$pathEntries = @((Join-Path $root 'bin'))
if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    $pathEntries += (Join-Path $env:LOCALAPPDATA 'Programs\CRANE\bin')
}
if (-not $SkipPathUpdate) { Remove-PathEntries $pathEntries }

if ($RemoveData) {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA is not set; cannot locate CRANE user data.'
    }
    $dataRoot = Join-Path $env:LOCALAPPDATA 'CRANE'
    if (Test-Path -LiteralPath $dataRoot -PathType Container) {
        Assert-NoReparsePoints $dataRoot
        foreach ($name in @('config', 'data', 'auth', 'logs', 'mcp', 'tmp', 'cache')) {
            Remove-KnownDirectory (Join-Path $dataRoot $name)
        }
        Remove-EmptyDirectoryIfPossible $dataRoot
        Write-Step "Removed CRANE-managed user data under $dataRoot."
    }
    else {
        Write-Step 'No CRANE user data directory was found.'
    }
}
elseif (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Write-Step "User data was preserved under $(Join-Path $env:LOCALAPPDATA 'CRANE')."
}

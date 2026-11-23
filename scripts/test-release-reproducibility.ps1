[CmdletBinding()]
param(
    [string]$Version = '0.1.2',
    [string]$Commit = '',
    [string]$BuildDate = '',
    [string]$PythonVersion = '3.12.7',
    [string]$GoVersion = '',
    [string]$PythonEmbedZip = '',
    [string]$OutputRoot = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step([string]$message) {
    Write-Host "CRANE repro: $message"
}

function Fail([string]$message) {
    Write-Error "CRANE repro: $message"
    exit 1
}

# ZipArchiveEntry.Crc32 is not available in Windows PowerShell 5.1. SHA-256
# over the decompressed entry content provides a stronger equality check.
function Get-ZipEntryInfo([string]$zipPath) {
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $entries = @()
        foreach ($entry in $zip.Entries) {
            $ms = New-Object System.IO.MemoryStream
            $stream = $entry.Open()
            try {
                $stream.CopyTo($ms)
            }
            finally {
                $stream.Dispose()
            }
            $bytes = $ms.ToArray()
            $hash = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
            $entries += [ordered]@{
                name = $entry.FullName
                length = [long]$entry.Length
                compressedLength = [long]$entry.CompressedLength
                lastWriteTimeUtc = $entry.LastWriteTime.ToUniversalTime().ToString('o')
                sha256 = $hash
            }
        }
        return $entries
    }
    finally {
        $zip.Dispose()
    }
}

function Compare-Zips([string]$zipA, [string]$zipB) {
    $entriesA = @(Get-ZipEntryInfo $zipA)
    $entriesB = @(Get-ZipEntryInfo $zipB)
    $namesA = @($entriesA | ForEach-Object { $_.name })
    $namesB = @($entriesB | ForEach-Object { $_.name })
    $count = [Math]::Max($namesA.Count, $namesB.Count)
    for ($i = 0; $i -lt $count; $i++) {
        $na = if ($i -lt $namesA.Count) { $namesA[$i] } else { $null }
        $nb = if ($i -lt $namesB.Count) { $namesB[$i] } else { $null }
        if ($na -ne $nb) {
            return "ZIP entry order/name differs at index ${i}: A='$na' B='$nb'"
        }
    }
    $mapB = @{}
    foreach ($e in $entriesB) { $mapB[$e.name] = $e }
    foreach ($ea in $entriesA) {
        $eb = $mapB[$ea.name]
        if ($null -eq $eb) {
            return "ZIP entry missing in second build: $($ea.name)"
        }
        if ($ea.sha256 -ne $eb.sha256 -or $ea.length -ne $eb.length -or $ea.compressedLength -ne $eb.compressedLength -or $ea.lastWriteTimeUtc -ne $eb.lastWriteTimeUtc) {
            return "First differing ZIP entry: $($ea.name)"
        }
    }
    return $null
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$buildScript = Join-Path $repoRoot 'scripts\build-release.ps1'
if (-not (Test-Path -LiteralPath $buildScript)) { Fail "Build script not found: $buildScript" }

if (-not $Commit) {
    $Commit = (& git -C $repoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Commit)) { Fail 'Could not resolve commit.' }
}
if (-not $BuildDate) {
    $BuildDate = (& git -C $repoRoot show -s --format=%cI $Commit).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($BuildDate)) { Fail "Could not resolve build date for commit $Commit." }
}
if (-not $GoVersion) {
    $rawGoVersion = (& go version).Trim()
    $GoVersion = ($rawGoVersion -split '\s+')[2]
    if (-not $GoVersion) { Fail 'Could not resolve Go version.' }
}
if (-not $PythonEmbedZip) {
    $PythonEmbedZip = Join-Path $repoRoot 'build\release\staging' ("python-{0}-embed-amd64.zip" -f $PythonVersion)
}
if (-not (Test-Path -LiteralPath $PythonEmbedZip)) { Fail "Python embeddable archive not found: $PythonEmbedZip" }

$tempBase = if ($OutputRoot) { [System.IO.Path]::GetFullPath($OutputRoot) } else { Join-Path ([System.IO.Path]::GetTempPath()) ("crane-release-repro-{0}" -f [guid]::NewGuid().ToString('N')) }
$createdTempBase = $false
if (-not (Test-Path -LiteralPath $tempBase)) {
    New-Item -ItemType Directory -Path $tempBase -Force | Out-Null
    $createdTempBase = $true
}
$buildA = Join-Path $tempBase 'build-a'
$buildB = Join-Path $tempBase 'build-b'
$releaseA = Join-Path $buildA $Version
$releaseB = Join-Path $buildB $Version

Write-Step "Inputs: version=$Version commit=$Commit build_date=$BuildDate python=$PythonVersion go=$GoVersion embed=$PythonEmbedZip"
Write-Step "Temporary output root: $tempBase"

$script:exitCode = 0
try {
    New-Item -ItemType Directory -Path $buildA -Force | Out-Null
    New-Item -ItemType Directory -Path $buildB -Force | Out-Null

    Write-Step 'Building release A'
    & pwsh -NoProfile -ExecutionPolicy Bypass -File $buildScript -Version $Version -OutputRoot $buildA -PythonEmbedZip $PythonEmbedZip -Commit $Commit -BuildDate $BuildDate -PythonVersion $PythonVersion -GoVersion $GoVersion
    if ($LASTEXITCODE -ne 0) { Fail "Build A failed with exit code $LASTEXITCODE" }

    Write-Step 'Building release B'
    & pwsh -NoProfile -ExecutionPolicy Bypass -File $buildScript -Version $Version -OutputRoot $buildB -PythonEmbedZip $PythonEmbedZip -Commit $Commit -BuildDate $BuildDate -PythonVersion $PythonVersion -GoVersion $GoVersion
    if ($LASTEXITCODE -ne 0) { Fail "Build B failed with exit code $LASTEXITCODE" }

    if (-not (Test-Path -LiteralPath $releaseA)) { Fail "Release directory missing: $releaseA" }
    if (-not (Test-Path -LiteralPath $releaseB)) { Fail "Release directory missing: $releaseB" }

    $filesA = @(Get-ChildItem -LiteralPath $releaseA -File | Sort-Object Name)
    $filesB = @(Get-ChildItem -LiteralPath $releaseB -File | Sort-Object Name)
    $namesA = @($filesA | ForEach-Object { $_.Name })
    $namesB = @($filesB | ForEach-Object { $_.Name })
    if (($namesA -join '|') -ne (($namesB -join '|'))) {
        Write-Host "CRANE repro: release artifact list differs"
        Write-Host "CRANE repro: A: $($namesA -join ', ')"
        Write-Host "CRANE repro: B: $($namesB -join ', ')"
        $script:exitCode = 1
    }

    $allNames = @($namesA + $namesB | Sort-Object -Unique)
    $firstDiff = $null
    foreach ($name in $allNames) {
        $pathA = Join-Path $releaseA $name
        $pathB = Join-Path $releaseB $name
        if (-not (Test-Path -LiteralPath $pathA) -or -not (Test-Path -LiteralPath $pathB)) {
            $firstDiff = "Missing artifact: $name"
            break
        }
        $hashA = (Get-FileHash -LiteralPath $pathA -Algorithm SHA256).Hash.ToLowerInvariant()
        $hashB = (Get-FileHash -LiteralPath $pathB -Algorithm SHA256).Hash.ToLowerInvariant()
        $sizeA = (Get-Item -LiteralPath $pathA).Length
        $sizeB = (Get-Item -LiteralPath $pathB).Length
        $eq = ($hashA -eq $hashB -and $sizeA -eq $sizeB)
        Write-Host ("CRANE repro: {0} equal={1} sha256_a={2} sha256_b={3} size_a={4} size_b={5}" -f $name, $eq, $hashA, $hashB, $sizeA, $sizeB)
        if (-not $eq -and $null -eq $firstDiff) {
            if ($name -like '*.zip') {
                $zipDiff = Compare-Zips $pathA $pathB
                if ($zipDiff) {
                    $firstDiff = "$name -> $zipDiff"
                }
                else {
                    $firstDiff = "Artifact bytes differ but ZIP entries compared equal: $name"
                }
            }
            else {
                $firstDiff = "Artifact differs: $name"
            }
        }
    }

    if ($script:exitCode -ne 0 -or $null -ne $firstDiff) {
        if ($null -ne $firstDiff) {
            Write-Host "CRANE repro: FIRST DIFFERENCE: $firstDiff"
        }
        Write-Host 'CRANE repro: RESULT=FAIL'
        $script:exitCode = 1
    }
    else {
        Write-Host 'CRANE repro: RESULT=PASS (complete release outputs are byte-for-byte identical)'
        $script:exitCode = 0
    }
}
finally {
    if ($createdTempBase -and (Test-Path -LiteralPath $tempBase)) {
        Remove-Item -LiteralPath $tempBase -Recurse -Force -ErrorAction SilentlyContinue
        Write-Step "Cleaned temporary directory: $tempBase"
    }
    elseif (-not $createdTempBase) {
        # If caller provided OutputRoot, clean only build-a/build-b created by this run.
        if (Test-Path -LiteralPath $buildA) { Remove-Item -LiteralPath $buildA -Recurse -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $buildB) { Remove-Item -LiteralPath $buildB -Recurse -Force -ErrorAction SilentlyContinue }
        Write-Step "Cleaned temporary build directories under: $tempBase"
    }
}

exit $script:exitCode

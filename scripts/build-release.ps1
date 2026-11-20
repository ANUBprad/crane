[CmdletBinding()]
param(
    [string]$Version = '0.1.0',
    [string]$OutputRoot = (Join-Path $PSScriptRoot '..\build\release'),
    [string]$PythonVersion = '3.12.7',
    [string]$PythonEmbedZip = '',
    [string]$Commit = '',
    [string]$BuildDate = '',
    [string]$GoVersion = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Step([string]$message) {
    Write-Host "CRANE build: $message"
}

function Get-RepoRoot {
    return (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
}

function Get-Sha256([string]$path) {
    return (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-IsWindowsX64 {
    if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitProcess -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
        throw 'CRANE releases must be built on Windows x64.'
    }
}

function ConvertTo-JsonFile([object]$value, [string]$path) {
    $json = $value | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($path, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
}

function Remove-Directory([string]$path) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

Assert-IsWindowsX64
$repoRoot = Get-RepoRoot
if ([System.IO.Path]::IsPathRooted($OutputRoot)) {
    $outputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
}
else {
    $outputRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputRoot))
}
$releaseDir = Join-Path $outputRoot $Version
$stagingRoot = Join-Path $outputRoot 'staging'
$stage = Join-Path $stagingRoot ("build-{0}-{1}" -f $Version, [guid]::NewGuid().ToString('N'))
Remove-Directory $releaseDir
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
New-Item -ItemType Directory -Path $stage -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stage 'runtime') -Force | Out-Null

if (-not $Commit) {
    $Commit = (& git -C $repoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the git commit.' }
}
if (-not $BuildDate) {
    $BuildDate = (& git -C $repoRoot show -s --format=%cI $Commit).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($BuildDate)) {
        throw "Could not read the build date for commit $Commit."
    }
}
$buildDate = $BuildDate
if (-not $GoVersion) {
    $rawGoVersion = (& go version).Trim()
    $GoVersion = ($rawGoVersion -split '\s+')[2]
    if (-not $GoVersion) { throw 'Could not read the Go version.' }
}
if (-not $PythonEmbedZip) {
    $PythonEmbedZip = Join-Path $stagingRoot ("python-{0}-embed-amd64.zip" -f $PythonVersion)
    if (-not (Test-Path -LiteralPath $PythonEmbedZip)) {
        Write-Step "Downloading CPython $PythonVersion embeddable runtime"
        $url = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $PythonEmbedZip -UseBasicParsing
    }
}
if (-not (Test-Path -LiteralPath $PythonEmbedZip)) { throw "Python embeddable archive not found: $PythonEmbedZip" }

Write-Step 'Building agent.exe'
$ldflags = "-s -w -X main.version=$Version -X main.buildCommit=$Commit -X main.buildDate=$buildDate -X main.goVersion=$($GoVersion -replace '\s+', ' ')"
Push-Location $repoRoot
try {
    & go build -trimpath "-ldflags=$ldflags" -o (Join-Path $stage 'agent.exe') ./cmd/agent
    if ($LASTEXITCODE -ne 0) { throw 'go build failed.' }
}
finally { Pop-Location }

Write-Step 'Preparing private Python runtime'
$pythonDir = Join-Path $stage 'runtime\python'
$sitePackages = Join-Path $pythonDir 'Lib\site-packages'
New-Item -ItemType Directory -Path $pythonDir -Force | Out-Null
Expand-Archive -LiteralPath $PythonEmbedZip -DestinationPath $pythonDir -Force
New-Item -ItemType Directory -Path $sitePackages -Force | Out-Null
$pyMajorMinor = ($PythonVersion -split '\.')[0] + ($PythonVersion -split '\.')[1]
$pthPath = Join-Path $pythonDir "python$pyMajorMinor._pth"
@("python$pyMajorMinor.zip", '.', '..\app', 'Lib\site-packages', 'import site') | Set-Content -LiteralPath $pthPath -Encoding ASCII

Write-Step 'Installing pinned production Python dependencies'
& python -m pip install --disable-pip-version-check --no-deps --no-compile --only-binary=:all: --require-hashes --target $sitePackages -r (Join-Path $repoRoot 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Packaged Python dependency installation failed.' }

Write-Step 'Copying CRANE Python application'
$appDir = Join-Path $stage 'runtime\app\crane'
New-Item -ItemType Directory -Path $appDir -Force | Out-Null
$trackedAppFiles = & git -C $repoRoot ls-files -- crane/python_runtime crane/mcp_runtime
if ($LASTEXITCODE -ne 0) { throw 'Could not enumerate tracked CRANE Python application files.' }
foreach ($relative in $trackedAppFiles) {
    $normalized = $relative -replace '/', '\'
    $name = [IO.Path]::GetFileName($normalized)
    if ($name -eq '_init__.py') { continue }
    $source = Join-Path $repoRoot $normalized
    $destination = Join-Path $appDir ($normalized.Substring('crane\'.Length))
    $destinationDir = [IO.Path]::GetDirectoryName($destination)
    if (-not (Test-Path -LiteralPath $destinationDir)) {
        New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $source -Destination $destination -Force
}
Get-ChildItem -LiteralPath $appDir -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $appDir -Recurse -File -Filter 'test_*.py' | Remove-Item -Force
Get-ChildItem -LiteralPath $appDir -Recurse -File -Filter '*.pyc' | Remove-Item -Force
Get-ChildItem -LiteralPath $appDir -Recurse -File -Filter '_init__.py' | Remove-Item -Force

$installInfo = [ordered]@{
    schema_version = 1
    version        = $Version
    commit         = $Commit
    build_date     = $buildDate
    os             = 'windows'
    arch           = 'x64'
    python_version = $PythonVersion
    go_version     = $GoVersion
    signature      = 'unverified'
}
ConvertTo-JsonFile $installInfo (Join-Path $stage 'install.json')

Write-Step 'Validating standalone runtime'
$pythonExe = Join-Path $pythonDir 'python.exe'
$savedPythonPath = $env:PYTHONPATH
$savedCranePython = $env:CRANE_PYTHON
$savedCraneHome = $env:CRANE_PYTHON_HOME
try {
    $env:PYTHONPATH = $null
    $env:CRANE_PYTHON = $null
    $env:CRANE_PYTHON_HOME = $null
    $healthText = & $pythonExe -m crane.python_runtime --health
    if ($LASTEXITCODE -ne 0) { throw 'Packaged Python health check failed.' }
    $health = $healthText | ConvertFrom-Json
    if ($health.status -ne 'healthy') { throw 'Packaged Python health response was not healthy.' }

    $importCode = 'import langchain_core, langgraph, langchain_mcp_adapters, langchain_ollama, ollama, mcp, jsonschema, sqlite3; print(1)'
    $importResult = & $pythonExe -c $importCode
    if ($LASTEXITCODE -ne 0 -or $importResult.Trim() -ne '1') { throw 'Packaged Python dependency import check failed.' }

    $versionText = & (Join-Path $stage 'agent.exe') --version
    if ($LASTEXITCODE -ne 0 -or $versionText -notmatch [regex]::Escape($Version)) {
        throw 'agent.exe version validation failed.'
    }
}
finally {
    $env:PYTHONPATH = $savedPythonPath
    $env:CRANE_PYTHON = $savedCranePython
    $env:CRANE_PYTHON_HOME = $savedCraneHome
}

Write-Step 'Creating release artifacts'
Get-ChildItem -LiteralPath $stage -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
Get-ChildItem -LiteralPath $stage -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.Extension -in '.pyc', '.pyo' } | Remove-Item -Force
$fixedTimestamp = [datetime]'1980-01-01T00:00:00'
Get-ChildItem -LiteralPath $stage -Recurse -Force | ForEach-Object {
    $_.CreationTimeUtc = $fixedTimestamp
    $_.LastWriteTimeUtc = $fixedTimestamp
    $_.LastAccessTimeUtc = $fixedTimestamp
}
Copy-Item -LiteralPath (Join-Path $repoRoot 'scripts\install.ps1') -Destination (Join-Path $releaseDir 'install.ps1') -Force
Copy-Item -LiteralPath (Join-Path $repoRoot 'scripts\uninstall.ps1') -Destination (Join-Path $releaseDir 'uninstall.ps1') -Force
Copy-Item -LiteralPath (Join-Path $repoRoot 'requirements.lock.txt') -Destination (Join-Path $releaseDir 'python-requirements.lock.txt') -Force
$payloadName = "crane-windows-x64-$Version.zip"
$payloadPath = Join-Path $releaseDir $payloadName
if (Test-Path -LiteralPath $payloadPath) { Remove-Item -LiteralPath $payloadPath -Force }
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $payloadPath -CompressionLevel Optimal

$artifacts = @(
    [ordered]@{ name = $payloadName; kind = 'payload'; sha256 = (Get-Sha256 $payloadPath); size = (Get-Item $payloadPath).Length },
    [ordered]@{ name = 'install.ps1'; kind = 'installer'; sha256 = (Get-Sha256 (Join-Path $releaseDir 'install.ps1')); size = (Get-Item (Join-Path $releaseDir 'install.ps1')).Length },
    [ordered]@{ name = 'uninstall.ps1'; kind = 'uninstaller'; sha256 = (Get-Sha256 (Join-Path $releaseDir 'uninstall.ps1')); size = (Get-Item (Join-Path $releaseDir 'uninstall.ps1')).Length },
    [ordered]@{ name = 'python-requirements.lock.txt'; kind = 'dependency_lock'; sha256 = (Get-Sha256 (Join-Path $releaseDir 'python-requirements.lock.txt')); size = (Get-Item (Join-Path $releaseDir 'python-requirements.lock.txt')).Length }
)
$manifest = [ordered]@{
    schema_version = 1
    version        = $Version
    channel        = 'stable'
    os             = 'windows'
    arch           = 'x64'
    python_version = $PythonVersion
    go_version     = $GoVersion
    commit         = $Commit
    build_date     = $buildDate
    signature      = [ordered]@{ scheme = 'none'; status = 'unverified'; note = 'No production code-signing certificate is available.' }
    artifacts      = $artifacts
}
ConvertTo-JsonFile $manifest (Join-Path $releaseDir 'release-manifest.json')
$sums = foreach ($artifact in $artifacts) { "{0}  {1}" -f $artifact.sha256, $artifact.name }
$sums | Set-Content -LiteralPath (Join-Path $releaseDir 'SHA256SUMS.txt') -Encoding ASCII

Write-Step "Release written to $releaseDir"
Get-ChildItem -LiteralPath $releaseDir -File | Select-Object Name, Length

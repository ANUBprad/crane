# CRANE public bootstrap installer.
# Usage: irm https://anubprad.github.io/crane/install.ps1 | iex
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$owner = 'ANUBprad'
$repo = 'crane'
$api = "https://api.github.com/repos/$owner/$repo/releases/latest"
$headers = @{ 'Accept' = 'application/vnd.github+json'; 'User-Agent' = 'CRANE-Bootstrap' }
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("crane-install-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

try {
    if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) {
        throw 'CRANE currently supports Windows 10/11 x64 only.'
    }

    Write-Host 'CRANE: checking the latest published release...'
    $release = Invoke-RestMethod -Uri $api -Headers $headers -Method Get
    if (-not $release.tag_name -or $release.draft -or $release.prerelease) {
        throw 'No stable CRANE release is currently available.'
    }

    $required = @(
        'release-manifest.json',
        'SHA256SUMS.txt',
        'install.ps1',
        'uninstall.ps1',
        'python-requirements.lock.txt'
    )
    $assets = @{}
    foreach ($asset in $release.assets) {
        if ($asset.name -and $asset.browser_download_url) { $assets[$asset.name] = $asset.browser_download_url }
    }
    foreach ($name in $required) {
        if (-not $assets.ContainsKey($name)) { throw "Release $($release.tag_name) is incomplete: missing $name." }
    }
    $payload = @($release.assets | Where-Object { $_.name -match '^crane-windows-x64-.+\.zip$' })
    if ($payload.Count -ne 1) { throw 'Release must contain exactly one Windows x64 runtime archive.' }
    $assets[$payload[0].name] = $payload[0].browser_download_url

    foreach ($name in ($required + $payload[0].name)) {
        $download = [Uri]$assets[$name]
        if ($download.Scheme -ne 'https' -or $download.Host -ne 'github.com') {
            throw "Refusing an unexpected release asset URL for $name."
        }
        Write-Host "CRANE: downloading $name"
        Invoke-WebRequest -Uri $download.AbsoluteUri -OutFile (Join-Path $tempRoot $name) -UseBasicParsing
    }

    $manifest = Get-Content -LiteralPath (Join-Path $tempRoot 'release-manifest.json') -Raw | ConvertFrom-Json
    if ($manifest.version -ne ($release.tag_name -replace '^v', '') -or $manifest.os -ne 'windows' -or $manifest.arch -ne 'x64') {
        throw 'Release metadata does not match the selected GitHub release or Windows x64 platform.'
    }
    if ($manifest.signature.scheme -ne 'none' -or $manifest.signature.status -ne 'unverified') {
        throw 'Unexpected release signature metadata. Refusing to continue.'
    }

    # Verify the downloaded installer and payload before executing any release code.
    $manifestArtifacts = @{}
    foreach ($item in $manifest.artifacts) {
        if (-not $item.name -or $item.sha256 -notmatch '^[0-9a-fA-F]{64}    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "CRANE installer exited with code $LASTEXITCODE." }
    Write-Host ''
    Write-Host 'CRANE installation finished. Open a new PowerShell window and run: agent doctor' -ForegroundColor Green
}
catch {
    Write-Error ("CRANE installation failed: " + $_.Exception.Message)
    throw
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
) {
            throw 'Release manifest contains an invalid artifact entry.'
        }
        $manifestArtifacts[$item.name] = $item.sha256.ToLowerInvariant()
    }
    $checksumEntries = @{}
    foreach ($line in (Get-Content -LiteralPath (Join-Path $tempRoot 'SHA256SUMS.txt'))) {
        if ($line -match '^([0-9a-fA-F]{64})\s+\*?(.+)    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "CRANE installer exited with code $LASTEXITCODE." }
    Write-Host ''
    Write-Host 'CRANE installation finished. Open a new PowerShell window and run: agent doctor' -ForegroundColor Green
}
catch {
    Write-Error ("CRANE installation failed: " + $_.Exception.Message)
    throw
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
) {
            $checksumEntries[$Matches[2].Trim()] = $Matches[1].ToLowerInvariant()
        }
    }
    foreach ($name in $manifestArtifacts.Keys) {
        if (-not $checksumEntries.ContainsKey($name) -or $checksumEntries[$name] -ne $manifestArtifacts[$name]) {
            throw "SHA256SUMS does not match the release manifest for $name."
        }
        $path = Join-Path $tempRoot $name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required release artifact was not downloaded: $name."
        }
        $actualHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $manifestArtifacts[$name]) {
            throw "SHA-256 verification failed for $name."
        }
    }

    Write-Warning 'This release is not Authenticode-signed. SHA-256 checks verify integrity, not publisher authenticity.'
    $installer = Join-Path $tempRoot 'install.ps1'
    Unblock-File -LiteralPath $installer
    & $installer -Action Install -ReleaseSource $tempRoot
    if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "CRANE installer exited with code $LASTEXITCODE." }
    Write-Host ''
    Write-Host 'CRANE installation finished. Open a new PowerShell window and run: agent doctor' -ForegroundColor Green
}
catch {
    Write-Error ("CRANE installation failed: " + $_.Exception.Message)
    throw
}
finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

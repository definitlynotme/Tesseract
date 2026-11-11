[CmdletBinding()]
param(
    [string] $OutputDir = "build\third-party-sources"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path

function Resolve-RepoPath([string] $Path) {
    if ([System.IO.Path]::IsPathRooted($Path)) {
        return [System.IO.Path]::GetFullPath($Path)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $Path))
}

function Assert-UnderRepo([string] $Path) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $prefix = $RepoRoot.TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    ) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to write third-party sources outside the repository: $fullPath"
    }
    return $fullPath
}

function Assert-Hash([string] $Path, [string] $ExpectedSha256) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required third-party source is missing: $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "Third-party source SHA-256 mismatch for $Path`: expected $ExpectedSha256, got $actual"
    }
}

function Receive-PinnedFile(
    [string] $Uri,
    [string] $Destination,
    [string] $ExpectedSha256
) {
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        Assert-Hash $Destination $ExpectedSha256
        return
    }
    $temporary = "$Destination.download"
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    try {
        Invoke-WebRequest -Uri $Uri -OutFile $temporary -Headers @{
            "User-Agent" = "Tesseract-release-source-bundle"
        }
        Assert-Hash $temporary $ExpectedSha256
        Move-Item -LiteralPath $temporary -Destination $Destination
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

$destinationRoot = Assert-UnderRepo (Resolve-RepoPath $OutputDir)
New-Item -ItemType Directory -Path $destinationRoot -Force | Out-Null
$downloads = @(
    @{
        Name = "mihomo-v1.19.27-source.zip"
        Uri = "https://github.com/MetaCubeX/mihomo/archive/refs/tags/v1.19.27.zip"
        Sha256 = "1ceb58df46e52900f9f4c9bcb4e6bad19a5fb138ed131b60dca793845893d709"
    },
    @{
        Name = "pyside-setup-6.11.0-source.tar.xz"
        Uri = "https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.0-src/pyside-setup-everywhere-src-6.11.0.tar.xz"
        Sha256 = "48d5c44d7c3ed861055d5491486e6a220ef5006573cae01a5fae3fb69d786336"
    },
    @{
        Name = "qtbase-6.11.0-source.tar.xz"
        Uri = "https://download.qt.io/official_releases/qt/6.11/6.11.0/submodules/qtbase-everywhere-src-6.11.0.tar.xz"
        Sha256 = "231ad85979864d914dc9568a1b71c91d6cf20d7b2021d059103bf0eb51cb755e"
    },
    @{
        Name = "qtsvg-6.11.0-source.tar.xz"
        Uri = "https://download.qt.io/official_releases/qt/6.11/6.11.0/submodules/qtsvg-everywhere-src-6.11.0.tar.xz"
        Sha256 = "dfa8d653be07087d9407ed4a4ebae847f8953e0b7abd829f089803ab652a30e6"
    }
)

foreach ($download in $downloads) {
    Receive-PinnedFile `
        -Uri $download.Uri `
        -Destination (Join-Path $destinationRoot $download.Name) `
        -ExpectedSha256 $download.Sha256
}

$pythonExecutable = (Get-Command "python" -ErrorAction Stop).Source
$pythonLicenseProbe = @'
import json
import platform
import sys
from pathlib import Path

print(json.dumps({
    'version': platform.python_version(),
    'license_path': str(Path(sys.base_prefix) / 'LICENSE.txt'),
}, separators=(',', ':')))
'@
$pythonLicenseEvidence = [string] (& $pythonExecutable -c $pythonLicenseProbe)
if ($LASTEXITCODE -ne 0 -or -not $pythonLicenseEvidence.Trim()) {
    throw "Unable to resolve the CPython Windows license material."
}
$pythonLicensePayload = $pythonLicenseEvidence | ConvertFrom-Json
if ([string] $pythonLicensePayload.version -cne "3.12.7") {
    throw (
        "CPython Windows license version mismatch: expected 3.12.7, got " +
        "$($pythonLicensePayload.version)."
    )
}
$pythonLicenseSource = [string] $pythonLicensePayload.license_path
$pythonLicenseDestination = Join-Path `
    $destinationRoot `
    "CPython-3.12.7-WINDOWS-LICENSE-AND-NOTICES.txt"
$pythonLicenseSha256 = "e502c6b880ff58d614901495a9009c136539cd0b1e2a2abb8fc00b934c203419"
Assert-Hash $pythonLicenseSource $pythonLicenseSha256
Copy-Item -LiteralPath $pythonLicenseSource -Destination $pythonLicenseDestination -Force
Assert-Hash $pythonLicenseDestination $pythonLicenseSha256

$wintunLicenseSource = Resolve-RepoPath "packaging\licenses\WINTUN-PREBUILT-LICENSE.txt"
$wintunLicenseDestination = Join-Path $destinationRoot "WINTUN-PREBUILT-LICENSE.txt"
$wintunLicenseSha256 = "9aaf948856ce8845a762121306039ef09d0eeb4d9e4f4c355647d4081e818087"
Assert-Hash $wintunLicenseSource $wintunLicenseSha256
Copy-Item -LiteralPath $wintunLicenseSource -Destination $wintunLicenseDestination -Force
Assert-Hash $wintunLicenseDestination $wintunLicenseSha256

Write-Host "Pinned third-party source materials are ready at $destinationRoot"

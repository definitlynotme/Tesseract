[CmdletBinding()]
param(
    [string] $Version = "1.19.34",
    [ValidateSet("v1", "v2", "v3")]
    [string] $CpuLevel = "v1",
    [string] $DownloadUrl = "",
    [string] $MihomoArchiveSha256 = "703e325bfeee582f874b06d054eaae55ab7c194645d1835411685aafa85da5ea",
    [string] $MihomoBinarySha256 = "513ec525d782e29e00dd92b25f25623b3811a9b2fc5c51d95e1190bcc7fa0d3c",
    [string] $OutputPath = "bin\mihomo.exe",
    [string] $WintunOutputPath = "bin\wintun.dll",
    [string] $WintunVersion = "0.14.1",
    [string] $WintunArchiveSha256 = "07c256185d6ee3652e09fa55c0b673e2624b565e02c4b9091c79ca7d2f24ef51",
    [string] $WintunBinarySha256 = "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce",
    [string] $WintunSignerThumbprint = "df98e075a012ed8c86fbcf14854b8f9555cb3d45",
    [switch] $SkipWintun,
    [switch] $Force
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
    $repoPrefix = $RepoRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($repoPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to write outside repository: $fullPath"
    }
    return $fullPath
}

$destination = Assert-UnderRepo (Resolve-RepoPath $OutputPath)
if ((Test-Path -LiteralPath $destination) -and -not $Force) {
    Write-Host "Mihomo already exists at $destination"
    return
}

$tag = ""
$assetUrl = $DownloadUrl
if (-not $assetUrl) {
    if (-not $Version) {
        throw "A pinned Mihomo version is required when DownloadUrl is not supplied."
    }
    $tag = if ($Version.StartsWith("v")) { $Version } else { "v$Version" }
    if ($tag -notmatch '^v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$') {
        throw "Mihomo version must be a canonical stable vX.Y.Z tag: $tag"
    }
    $exactName = "mihomo-windows-amd64-$CpuLevel-$tag.zip"
    $assetUrl = "https://github.com/MetaCubeX/mihomo/releases/download/$tag/$exactName"
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("tesseract-mihomo-" + [System.Guid]::NewGuid().ToString("N"))
$archivePath = Join-Path $tempRoot "mihomo.zip"
$extractPath = Join-Path $tempRoot "extract"

try {
    New-Item -ItemType Directory -Path $extractPath | Out-Null
    Write-Host "Downloading Mihomo from $assetUrl"
    Invoke-WebRequest -Uri $assetUrl -OutFile $archivePath -Headers @{ "User-Agent" = "Tesseract-build" }
    $archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($archiveHash -ne $MihomoArchiveSha256.ToLowerInvariant()) {
        throw "Mihomo archive SHA-256 mismatch: expected $MihomoArchiveSha256, got $archiveHash"
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractPath -Force

    $mihomoExe = Get-ChildItem -LiteralPath $extractPath -Recurse -File -Filter "*.exe" |
        Where-Object { $_.Name -match "^mihomo.*\.exe$" } |
        Select-Object -First 1
    if (-not $mihomoExe) {
        throw "Downloaded archive does not contain a Mihomo executable."
    }
    $mihomoHash = (Get-FileHash -LiteralPath $mihomoExe.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($mihomoHash -ne $MihomoBinarySha256.ToLowerInvariant()) {
        throw "Mihomo executable SHA-256 mismatch: expected $MihomoBinarySha256, got $mihomoHash"
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $mihomoExe.FullName -Destination $destination -Force
    Write-Host "Mihomo written to $destination"
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        $tempFull = [System.IO.Path]::GetFullPath($tempRoot)
        $systemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if ($tempFull.StartsWith($systemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force
        }
    }
}

# ---------------- WinTun driver (required for TUN mode) ----------------

if ($SkipWintun) {
    Write-Host "Skipping WinTun download (--SkipWintun)."
    return
}

$wintunDestination = Assert-UnderRepo (Resolve-RepoPath $WintunOutputPath)
if ((Test-Path -LiteralPath $wintunDestination) -and -not $Force) {
    Write-Host "WinTun already exists at $wintunDestination"
    return
}

$wintunUrl = "https://www.wintun.net/builds/wintun-$WintunVersion.zip"
$wintunTempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("tesseract-wintun-" + [System.Guid]::NewGuid().ToString("N"))
$wintunArchive = Join-Path $wintunTempRoot "wintun.zip"
$wintunExtract = Join-Path $wintunTempRoot "extract"

try {
    New-Item -ItemType Directory -Path $wintunExtract | Out-Null
    Write-Host "Downloading WinTun $WintunVersion from $wintunUrl"
    Invoke-WebRequest -Uri $wintunUrl -OutFile $wintunArchive -Headers @{ "User-Agent" = "Tesseract-build" }
    $wintunArchiveHash = (Get-FileHash -LiteralPath $wintunArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($wintunArchiveHash -ne $WintunArchiveSha256.ToLowerInvariant()) {
        throw "WinTun archive SHA-256 mismatch: expected $WintunArchiveSha256, got $wintunArchiveHash"
    }
    Expand-Archive -LiteralPath $wintunArchive -DestinationPath $wintunExtract -Force

    $wintunDll = Get-ChildItem -LiteralPath $wintunExtract -Recurse -File -Filter "wintun.dll" |
        Where-Object { $_.FullName -match "amd64" } |
        Select-Object -First 1
    if (-not $wintunDll) {
        $wintunDll = Get-ChildItem -LiteralPath $wintunExtract -Recurse -File -Filter "wintun.dll" | Select-Object -First 1
    }
    if (-not $wintunDll) {
        throw "WinTun archive does not contain wintun.dll"
    }
    $wintunHash = (Get-FileHash -LiteralPath $wintunDll.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($wintunHash -ne $WintunBinarySha256.ToLowerInvariant()) {
        throw "WinTun DLL SHA-256 mismatch: expected $WintunBinarySha256, got $wintunHash"
    }
    $wintunSignature = Get-AuthenticodeSignature -LiteralPath $wintunDll.FullName
    $actualSignerThumbprint = [string] $wintunSignature.SignerCertificate.Thumbprint
    if (
        $wintunSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
        $actualSignerThumbprint.ToLowerInvariant() -ne $WintunSignerThumbprint.ToLowerInvariant()
    ) {
        throw (
            "WinTun upstream Authenticode verification failed: " +
            "status=$($wintunSignature.Status), signer=$actualSignerThumbprint"
        )
    }

    New-Item -ItemType Directory -Path (Split-Path -Parent $wintunDestination) -Force | Out-Null
    Copy-Item -LiteralPath $wintunDll.FullName -Destination $wintunDestination -Force
    Write-Host "WinTun written to $wintunDestination"
}
finally {
    if (Test-Path -LiteralPath $wintunTempRoot) {
        $tempFull = [System.IO.Path]::GetFullPath($wintunTempRoot)
        $systemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if ($tempFull.StartsWith($systemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $wintunTempRoot -Recurse -Force
        }
    }
}

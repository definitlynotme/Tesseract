[CmdletBinding()]
param(
    # Pinned description of the bootstrapper (file name, source URL, SHA-256, signer).
    [string] $ManifestPath = "",
    # Directory the verified bootstrapper is placed in (ignored by git).
    [string] $OutputDir = "",
    # Import an already-downloaded copy instead of downloading (offline builds).
    [string] $LocalFile = "",
    # Download into a fresh staging directory, verify the Authenticode signer, print
    # the values to pin, and stop. The manifest and the output directory are untouched.
    [switch] $Repin,
    [switch] $Force
)

# Fetches Microsoft's WebView2 Evergreen Bootstrapper for the Shell-flavor installer
# (packaging/windows/Tesseract-shell.iss). The binary is never committed and is never
# executed by this script: it is only hashed and its Authenticode signature is read.

$ErrorActionPreference = "Stop"

if (-not $ManifestPath) { $ManifestPath = Join-Path $PSScriptRoot "webview2-bootstrapper.manifest.json" }
if (-not $OutputDir) { $OutputDir = Join-Path $PSScriptRoot "cache" }
$IssPath = Join-Path (Split-Path -Parent $PSScriptRoot) "Tesseract-shell.iss"

$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$fileName = [string] $manifest.file_name
$sourceUrl = [string] $manifest.source_url
$expectedSha256 = ([string] $manifest.sha256).ToLowerInvariant()
$expectedSigner = [string] $manifest.signer_subject
$expectedRoot = [string] $manifest.signer_root_subject

if ($fileName -notmatch '^[A-Za-z0-9_.-]+\.exe$') { throw "Manifest file_name is not a plain .exe name: $fileName" }
if ($sourceUrl -notmatch '^https://go\.microsoft\.com/fwlink/') { throw "Manifest source_url must be a Microsoft fwlink: $sourceUrl" }
if (-not $Repin -and $expectedSha256 -notmatch '^[0-9a-f]{64}$') { throw "Manifest sha256 is not pinned." }
if (-not $expectedSigner) { throw "Manifest signer_subject is empty." }

function Get-Sha256([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# Reads the Authenticode signature (does not run the file) and requires a valid
# signature whose leaf subject and chain root match the manifest.
function Assert-MicrosoftSignature([string] $Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Authenticode status is $($signature.Status) for $Path"
    }
    $leaf = $signature.SignerCertificate
    if ($leaf.Subject -ne $expectedSigner) {
        throw "Unexpected signer subject: $($leaf.Subject)"
    }
    $chain = New-Object System.Security.Cryptography.X509Certificates.X509Chain
    $chain.ChainPolicy.RevocationMode = [System.Security.Cryptography.X509Certificates.X509RevocationMode]::NoCheck
    [void] $chain.Build($leaf)
    $root = $chain.ChainElements[$chain.ChainElements.Count - 1].Certificate
    if ($expectedRoot -and $root.Subject -ne $expectedRoot) {
        throw "Unexpected signing chain root: $($root.Subject)"
    }
    $timestamp = $null
    if ($signature.TimeStamperCertificate) { $timestamp = $signature.TimeStamperCertificate.Subject }
    return [ordered] @{
        status = [string] $signature.Status
        signer_subject = $leaf.Subject
        signer_thumbprint = $leaf.Thumbprint.ToLowerInvariant()
        signer_issuer = $leaf.Issuer
        signer_root_subject = $root.Subject
        timestamper_subject = $timestamp
    }
}

# The .iss pins the same hash for its compile-time check; both must agree.
function Assert-IssPin {
    if (-not (Test-Path -LiteralPath $IssPath)) { return }
    $text = Get-Content -LiteralPath $IssPath -Raw -Encoding UTF8
    $match = [regex]::Match($text, '#define\s+WebView2BootstrapperSha256\s+"([0-9a-fA-F]{64})"')
    if (-not $match.Success) { throw "Tesseract-shell.iss does not pin WebView2BootstrapperSha256." }
    if ($match.Groups[1].Value.ToLowerInvariant() -ne $expectedSha256) {
        throw "Tesseract-shell.iss pins $($match.Groups[1].Value), manifest pins $expectedSha256."
    }
}

function Describe([string] $Path) {
    $item = Get-Item -LiteralPath $Path
    $info = [ordered] @{
        file_name = $fileName
        source_url = $sourceUrl
        sha256 = Get-Sha256 $Path
        size_bytes = $item.Length
        file_version = $item.VersionInfo.FileVersion
        product_version = $item.VersionInfo.ProductVersion
    }
    foreach ($entry in (Assert-MicrosoftSignature $Path).GetEnumerator()) { $info[$entry.Key] = $entry.Value }
    return $info
}

$destination = Join-Path $OutputDir $fileName

if ($Repin) {
    $staging = Join-Path $OutputDir ("repin-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $staging | Out-Null
    $stagedFile = Join-Path $staging $fileName
    Write-Host "Downloading $sourceUrl"
    Invoke-WebRequest -Uri $sourceUrl -OutFile $stagedFile -UseBasicParsing -Headers @{ "User-Agent" = "Tesseract-build" }
    $info = Describe $stagedFile
    $info["downloaded_utc"] = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $info["staged_path"] = $stagedFile
    Write-Host "Verified signer; values to pin (manifest and Tesseract-shell.iss):"
    $info | ConvertTo-Json
    # Side by side with the manifest for manual review: a new thumbprint is normal
    # when Microsoft renews its certificate; a new issuer or root needs a closer look.
    Write-Host ""
    Write-Host "Manifest (pinned) vs this download:"
    foreach ($field in @("sha256", "file_version", "signer_subject", "signer_thumbprint", "signer_issuer", "signer_root_subject")) {
        $pinned = [string] $manifest.$field
        $observed = [string] $info[$field]
        $mark = if ($pinned -eq $observed) { "same" } else { "CHANGED" }
        Write-Host ("{0,-21} [{1}]" -f $field, $mark)
        Write-Host ("  manifest: {0}" -f $pinned)
        Write-Host ("  download: {0}" -f $observed)
    }
    Write-Host ""
    Write-Host "Staged copy kept for review in $staging; delete it by hand (cache\repin-*) once reviewed."
    return
}

Assert-IssPin

if ((Test-Path -LiteralPath $destination) -and -not $Force) {
    if ((Get-Sha256 $destination) -eq $expectedSha256) {
        [void] (Assert-MicrosoftSignature $destination)
        Write-Host "WebView2 bootstrapper already verified at $destination"
        return
    }
    throw "Existing $destination does not match the pinned SHA-256; delete it or pass -Force."
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$staging = Join-Path $OutputDir ("download-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $staging | Out-Null
$stagedFile = Join-Path $staging $fileName
try {
    if ($LocalFile) {
        Copy-Item -LiteralPath $LocalFile -Destination $stagedFile
    }
    else {
        Write-Host "Downloading $sourceUrl"
        Invoke-WebRequest -Uri $sourceUrl -OutFile $stagedFile -UseBasicParsing -Headers @{ "User-Agent" = "Tesseract-build" }
    }
    $actual = Get-Sha256 $stagedFile
    if ($actual -ne $expectedSha256) {
        throw ("WebView2 bootstrapper SHA-256 mismatch: expected $expectedSha256, got $actual. " +
            "Microsoft may have published a new bootstrapper; re-pin with -Repin after review.")
    }
    [void] (Assert-MicrosoftSignature $stagedFile)
    Move-Item -LiteralPath $stagedFile -Destination $destination -Force
    Write-Host "WebView2 bootstrapper verified and written to $destination"
}
finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}

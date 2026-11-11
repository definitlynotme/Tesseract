[CmdletBinding()]
param(
    [string] $Version = "",
    [string] $ExpectedGitCommit = "",
    [string] $Repository = "",
    [string] $WorkflowRunId = "",
    [switch] $SkipDependencyInstall,
    [switch] $SkipTests,
    [switch] $SkipInstaller,
    [switch] $SkipPackagedSmoke,
    [switch] $SkipMihomoDownload,
    [switch] $RequireSigning,
    # Local builds without an elevated shell: compile the installer but do not
    # install it into the isolated validation directory. Official builds refuse it.
    [switch] $SkipInstallerAcceptance,
    # Rebuild the web UI, shell and sidecar from scratch (.build/p6-*). Official builds always do.
    [switch] $CleanShellBuild
)

# Release flavor: Tauri shell (Tesseract.exe) + web UI + PyInstaller sidecar
# (tesseract-runtime.exe). The retired Qt flavor is gone.

$ErrorActionPreference = "Stop"

function Resolve-RepoPath([string] $Path) {
    return [System.IO.Path]::GetFullPath((Join-Path $RepoRoot $Path))
}

function Assert-UnderRepo([string] $Path) {
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $repoPrefix = $RepoRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($repoPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to operate outside repository: $fullPath"
    }
    return $fullPath
}

function Get-Sha256([string] $Path) {
    return (
        Get-FileHash -LiteralPath $Path -Algorithm SHA256
    ).Hash.ToLowerInvariant()
}

function Write-GeneratedVersionInfo(
    [string] $DisplayVersion,
    [int[]] $VersionTuple,
    [string] $RepositoryUrl,
    [string] $OutputPath
) {
    if ($VersionTuple.Count -ne 4) {
        throw "Windows version metadata requires exactly four numeric components."
    }
    $safeOutputPath = Assert-UnderRepo $OutputPath
    $contents = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($($VersionTuple[0]), $($VersionTuple[1]), $($VersionTuple[2]), $($VersionTuple[3])),
    prodvers=($($VersionTuple[0]), $($VersionTuple[1]), $($VersionTuple[2]), $($VersionTuple[3])),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        "040904B0",
        [
          StringStruct("CompanyName", "Tesseract contributors"),
          StringStruct("FileDescription", "Tesseract \u2014 Layered proxy manager for Windows"),
          StringStruct("FileVersion", "$DisplayVersion"),
          StringStruct("InternalName", "Tesseract"),
          StringStruct("LegalCopyright", "Copyright \u00a9 2026 Tesseract contributors"),
          StringStruct("LegalTrademarks", ""),
          StringStruct("OriginalFilename", "Tesseract.exe"),
          StringStruct("ProductName", "Tesseract"),
          StringStruct("ProductVersion", "$DisplayVersion"),
          StringStruct("Comments", "$RepositoryUrl"),
        ],
      )
    ]),
    VarFileInfo([VarStruct("Translation", [1033, 1200])]),
  ],
)
"@
    [System.IO.File]::WriteAllText(
        $safeOutputPath,
        $contents.TrimStart() + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )
}

function Sync-ThirdPartySourceInputs(
    [string] $ManifestPath,
    [string] $SourceRoot,
    [string[]] $SkipComponentNames = @()
) {
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Third-party source manifest is missing: $ManifestPath"
    }
    $manifest = Get-Content -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or -not $manifest.components) {
        throw "Third-party source manifest must be a non-empty schema-version-1 document."
    }
    $safeSourceRoot = Assert-UnderRepo $SourceRoot
    New-Item -ItemType Directory -Path $safeSourceRoot -Force | Out-Null
    $seenNames = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($component in @($manifest.components)) {
        $name = [string] $component.name
        $relativePath = [string] $component.source_path
        $expectedHash = ([string] $component.sha256).ToLowerInvariant()
        $sourceUrl = [string] $component.source_url
        if (-not $name -or -not $seenNames.Add($name)) {
            throw "Third-party source manifest contains an empty or duplicate component name."
        }
        if (
            -not $relativePath -or
            [System.IO.Path]::GetFileName($relativePath) -ne $relativePath -or
            $relativePath -in @(".", "..")
        ) {
            throw "Third-party source path must be a single safe file name: $relativePath"
        }
        if ($expectedHash -notmatch "^[0-9a-f]{64}$") {
            throw "Third-party source hash is unresolved for $name."
        }
        if ($SkipComponentNames -contains $name) {
            # Qt-era components (pyside-setup, qtbase, qtsvg): the shell bundle drops them.
            Write-Host "Skipping $name (not shipped by the shell flavor)."
            continue
        }
        $destination =Assert-UnderRepo (Join-Path $safeSourceRoot $relativePath)
        if (
            (Test-Path -LiteralPath $destination -PathType Leaf) -and
            (Get-Sha256 $destination) -eq $expectedHash
        ) {
            Write-Host "Using hash-verified cached source material for $name."
            continue
        }

        $temporary = Assert-UnderRepo "$destination.download"
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        try {
            if ($name -eq "wintun-license") {
                $reviewedLicense = Resolve-RepoPath "packaging\licenses\WINTUN-PREBUILT-LICENSE.txt"
                Copy-Item -LiteralPath $reviewedLicense -Destination $temporary
            }
            elseif ($name -eq "cpython-windows-license") {
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
                $pythonLicenseEvidence = [string] (
                    & $ReleasePython -c $pythonLicenseProbe
                )
                if ($LASTEXITCODE -ne 0 -or -not $pythonLicenseEvidence.Trim()) {
                    throw "Unable to resolve the CPython Windows license material."
                }
                $pythonLicensePayload = $pythonLicenseEvidence | ConvertFrom-Json
                if ([string] $pythonLicensePayload.version -cne [string] $component.version) {
                    throw (
                        "CPython Windows license version mismatch: expected " +
                        "$($component.version), got $($pythonLicensePayload.version)."
                    )
                }
                $reviewedLicense = [string] $pythonLicensePayload.license_path
                if (-not (Test-Path -LiteralPath $reviewedLicense -PathType Leaf)) {
                    throw "CPython Windows license is unavailable: $reviewedLicense"
                }
                Copy-Item -LiteralPath $reviewedLicense -Destination $temporary
            }
            else {
                $uri = [Uri] $sourceUrl
                if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne "https" -or $uri.Fragment) {
                    throw "Third-party source URL must be an absolute fragment-free HTTPS URL for $name."
                }
                Invoke-WebRequest -Uri $uri.AbsoluteUri -OutFile $temporary
            }
            if (-not (Test-Path -LiteralPath $temporary -PathType Leaf)) {
                throw "Third-party source download did not create a file for $name."
            }
            $actualHash = Get-Sha256 $temporary
            if ($actualHash -ne $expectedHash) {
                throw (
                    "Third-party source hash mismatch for $name`: " +
                    "expected $expectedHash, got $actualHash"
                )
            }
            Move-Item -LiteralPath $temporary -Destination $destination -Force
        }
        finally {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Find-Iscc {
    $candidates = @()
    if ($env:ISCC_PATH) {
        $candidates += $env:ISCC_PATH
    }
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
    }
    if ($env:ProgramFiles) {
        $candidates += (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    }
    $command = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
    if ($command) {
        $candidates += $command.Source
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Get-InnoSetupVersion([string] $CompilerPath) {
    if (-not $CompilerPath) {
        return $null
    }
    $resolvedCompiler = (Resolve-Path -LiteralPath $CompilerPath).Path
    $registrationPaths = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1",
        "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1"
    )
    $registeredVersions = @()
    foreach ($registrationPath in $registrationPaths) {
        $registration = Get-ItemProperty -LiteralPath $registrationPath -ErrorAction SilentlyContinue
        if (-not $registration) {
            continue
        }
        $displayVersion = ([string] $registration.DisplayVersion).Trim()
        $installLocation = ([string] $registration.InstallLocation).Trim().TrimEnd("\", "/")
        if (-not $displayVersion -or -not $installLocation) {
            continue
        }
        $registeredCompiler = Join-Path $installLocation "ISCC.exe"
        if (-not (Test-Path -LiteralPath $registeredCompiler -PathType Leaf)) {
            continue
        }
        $resolvedRegisteredCompiler = (Resolve-Path -LiteralPath $registeredCompiler).Path
        if (-not $resolvedRegisteredCompiler.Equals(
            $resolvedCompiler,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            continue
        }
        if ($displayVersion -notmatch "^[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?$") {
            throw "Registered Inno Setup version is malformed: $displayVersion"
        }
        $registeredVersions += $displayVersion
    }
    $uniqueVersions = @($registeredVersions | Sort-Object -Unique)
    if ($uniqueVersions.Count -ne 1) {
        throw (
            "Unable to resolve exactly one registered Inno Setup version for " +
            "$resolvedCompiler."
        )
    }
    $resolvedVersion = [string] $uniqueVersions[0]
    if (
        $env:INNO_SETUP_VERSION -and
        $resolvedVersion -cne ([string] $env:INNO_SETUP_VERSION).Trim()
    ) {
        throw (
            "Registered Inno Setup version does not match INNO_SETUP_VERSION: " +
            "expected $env:INNO_SETUP_VERSION, got $resolvedVersion."
        )
    }
    return $resolvedVersion
}

function Find-SignTool {
    $command = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $kitsRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    if (Test-Path -LiteralPath $kitsRoot) {
        $candidate = Get-ChildItem -LiteralPath $kitsRoot -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\signtool\.exe$" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) {
            return $candidate.FullName
        }
    }
    return $null
}

function Sign-Artifact([string] $ArtifactPath) {
    if (-not $env:SIGN_CERT_PATH) {
        Write-Host "Signing skipped for $ArtifactPath because SIGN_CERT_PATH is not set."
        return
    }
    if (-not (Test-Path -LiteralPath $env:SIGN_CERT_PATH)) {
        throw "SIGN_CERT_PATH does not exist: $env:SIGN_CERT_PATH"
    }
    $signtool = Find-SignTool
    if (-not $signtool) {
        throw "signtool.exe was not found. Install Windows SDK or add signtool.exe to PATH."
    }
    $args = @(
        "sign",
        "/fd", "SHA256",
        "/td", "SHA256",
        "/tr", "http://timestamp.digicert.com",
        "/f", $env:SIGN_CERT_PATH
    )
    if ($env:SIGN_CERT_PASSWORD) {
        $args += @("/p", $env:SIGN_CERT_PASSWORD)
    }
    $args += $ArtifactPath
    & $signtool @args
    if ($LASTEXITCODE -ne 0) {
        throw "signtool failed for $ArtifactPath"
    }
    & $signtool "verify" "/pa" "/v" $ArtifactPath
    if ($LASTEXITCODE -ne 0) {
        throw "Authenticode verification failed for $ArtifactPath"
    }
}

function Wait-FileUnlocked([string] $Path, [int] $TimeoutSeconds = 20) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $stream = [System.IO.File]::Open(
                $Path,
                [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::Read,
                [System.IO.FileShare]::Read
            )
            $stream.Dispose()
            return
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    throw "Timed out waiting for file lock to clear: $Path"
}

function Invoke-WithRetry([scriptblock] $Action, [string] $Description, [int] $Attempts = 5, [int] $DelayMilliseconds = 500) {
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            & $Action
            return
        }
        catch {
            if ($attempt -eq $Attempts) {
                throw
            }
            Write-Host "$Description failed on attempt $attempt/$Attempts; retrying..."
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
}

function Invoke-Native([string] $Description, [string] $FilePath, [string[]] $Arguments) {
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Assert-ReleaseCandidateIdentity(
    [string] $PythonPath,
    [string] $IdentityToolPath,
    [string] $CandidateDirectory,
    [string] $ManifestPath,
    [string] $Description
) {
    Invoke-Native $Description $PythonPath @(
        $IdentityToolPath,
        "verify-dir",
        "--directory", $CandidateDirectory,
        "--manifest", $ManifestPath
    )
}

function Invoke-HiddenProcessWithTimeout(
    [string] $Description,
    [string] $FilePath,
    [string] $Arguments,
    [int] $TimeoutMilliseconds
) {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $Arguments
    $startInfo.WorkingDirectory = $RepoRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        [void] $process.Start()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            $process.Kill()
            $process.WaitForExit()
            throw "$Description timed out."
        }
        if ($process.ExitCode -ne 0) {
            throw "$Description failed with exit code $($process.ExitCode)."
        }
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-ExactSmokeProcess(
    [string] $Description,
    [string] $FilePath,
    [string] $WorkingDirectory,
    [string] $Arguments,
    [string] $ExpectedMarker,
    [string] $OutputPath,
    [string] $ErrorPath,
    [string] $ConfigDirectory,
    [string] $DataDirectory,
    [int] $TimeoutMilliseconds
) {
    Remove-Item -LiteralPath $OutputPath, $ErrorPath -Force -ErrorAction SilentlyContinue
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = $Arguments
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables["TESSERACT_CONFIG_DIR"] = $ConfigDirectory
    $startInfo.EnvironmentVariables["TESSERACT_DATA_DIR"] = $DataDirectory
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $stdout = ""
    $stderr = ""
    $exitCode = $null
    $timedOut = $false
    try {
        [void] $process.Start()
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            $timedOut = $true
            $process.Kill()
            $process.WaitForExit()
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        $exitCode = $process.ExitCode
    }
    finally {
        $process.Dispose()
    }
    [System.IO.File]::WriteAllText(
        $OutputPath,
        $stdout,
        [System.Text.UTF8Encoding]::new($false)
    )
    [System.IO.File]::WriteAllText(
        $ErrorPath,
        $stderr,
        [System.Text.UTF8Encoding]::new($false)
    )
    if ($timedOut) {
        throw "$Description timed out after $TimeoutMilliseconds milliseconds."
    }
    $normalizedStdout = $stdout.Replace("`r`n", "`n").Replace("`r", "`n")
    $expectedStdout = $ExpectedMarker + "`n"
    if ($exitCode -ne 0 -or $normalizedStdout -cne $expectedStdout) {
        if ($stderr) {
            Write-Error $stderr
        }
        throw (
            "$Description failed or did not emit exactly one expected marker: " +
            $ExpectedMarker
        )
    }
}

function Wait-PathsAbsent(
    [string[]] $Paths,
    [string] $Description,
    [int] $TimeoutSeconds = 30
) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $remaining = @($Paths | Where-Object { Test-Path -LiteralPath $_ })
        if ($remaining.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "$Description remained after $TimeoutSeconds seconds: $($remaining -join ', ')"
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

$BuildMutexHasher = [System.Security.Cryptography.SHA256]::Create()
try {
    $BuildMutexId = (
        [System.BitConverter]::ToString(
            $BuildMutexHasher.ComputeHash(
                [System.Text.Encoding]::UTF8.GetBytes(
                    $RepoRoot.ToLowerInvariant()
                )
            )
        )
    ).Replace("-", "").Substring(0, 24)
}
finally {
    $BuildMutexHasher.Dispose()
}
$BuildMutexName = "Local\TesseractWindowsBuild_$BuildMutexId"
$BuildMutexCreated = $false
$BuildMutex = [System.Threading.Mutex]::new(
    $true,
    $BuildMutexName,
    [ref] $BuildMutexCreated
)
if (-not $BuildMutexCreated) {
    $BuildMutex.Dispose()
    throw (
        "Another Windows release build is already running for this " +
        "repository. Refusing to modify .venv, build, dist, or release."
    )
}

try {
$BootstrapPython = (Get-Command "python" -ErrorAction Stop).Source
if (-not $Version) {
    $Version = (& $BootstrapPython -c "from app_info import APP_VERSION; print(APP_VERSION)").Trim()
}
if (-not $Version) {
    throw "Unable to determine release version."
}
$VersionContractPython = @'
import json
import sys

from app_info import release_version_contract

print(json.dumps(release_version_contract(sys.argv[1]), separators=(',', ':')))
'@
$VersionContractJson = [string] (
    & $BootstrapPython -c $VersionContractPython $Version
)
if ($LASTEXITCODE -ne 0 -or -not $VersionContractJson.Trim()) {
    throw "Unable to resolve the canonical release-version contract: $Version"
}
try {
    $VersionContract = $VersionContractJson | ConvertFrom-Json
}
catch {
    throw "Release-version contract did not return valid JSON: $Version"
}
if (
    [string] $VersionContract.version -cne $Version -or
    @($VersionContract.windows_version_tuple).Count -ne 4 -or
    [string]::IsNullOrWhiteSpace(
        [string] $VersionContract.windows_version_numeric
    )
) {
    throw "Release-version contract is incomplete or non-canonical: $Version"
}
$VersionTuple = @(
    $VersionContract.windows_version_tuple |
        ForEach-Object { [int] $_ }
)
$VersionNumeric = [string] $VersionContract.windows_version_numeric
if (($VersionTuple -join ".") -cne $VersionNumeric) {
    throw "Release-version contract numeric forms disagree: $Version"
}

if ($env:OS -ne "Windows_NT") {
    throw "This build script only supports Windows."
}

$OfficialFieldCount = @(
    $ExpectedGitCommit,
    $Repository,
    $WorkflowRunId
).Where({ -not [string]::IsNullOrWhiteSpace($_) }).Count
if ($OfficialFieldCount -notin @(0, 3)) {
    throw (
        "ExpectedGitCommit, Repository, and WorkflowRunId must either all " +
        "be supplied for an official build or all be omitted."
    )
}
$OfficialRelease = $OfficialFieldCount -eq 3
$ActualGitCommit = (git rev-parse HEAD).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $ActualGitCommit -notmatch "^[0-9a-f]{40}$") {
    throw "Unable to resolve the exact Git commit for the release build."
}
$TrackedStatus = @(git status --porcelain --untracked-files=no)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to determine tracked working-tree state."
}
$InitialDirty = [bool] ($TrackedStatus -join "")
$SourceVersionContractPython = @'
import json

from app_info import DEFAULT_RELEASE_VERSION_CONTRACT

print(json.dumps(DEFAULT_RELEASE_VERSION_CONTRACT, separators=(',', ':')))
'@
$SourceVersionContractJson = [string] (
    & $BootstrapPython -c $SourceVersionContractPython
)
if ($LASTEXITCODE -ne 0 -or -not $SourceVersionContractJson.Trim()) {
    throw "Unable to resolve DEFAULT_RELEASE_VERSION_CONTRACT from app_info.py."
}
try {
    $SourceVersionContract = $SourceVersionContractJson | ConvertFrom-Json
}
catch {
    throw "DEFAULT_RELEASE_VERSION_CONTRACT did not return valid JSON."
}
$SourceVersion = [string] $SourceVersionContract.version
$SourceVersionNumeric = [string] $SourceVersionContract.windows_version_numeric
if (-not $SourceVersion -or -not $SourceVersionNumeric) {
    throw "DEFAULT_RELEASE_VERSION_CONTRACT is incomplete."
}
$AppRepositoryIdentityPython = @'
import json
import app_info

print(json.dumps({
    'repository_url': app_info.APP_REPOSITORY_URL,
    'releases_url': app_info.APP_RELEASES_URL,
    'update_api_url': app_info.APP_UPDATE_API_URL,
}, separators=(',', ':')))
'@
$AppRepositoryIdentityJson = [string] (
    & $BootstrapPython -c $AppRepositoryIdentityPython
)
if ($LASTEXITCODE -ne 0 -or -not $AppRepositoryIdentityJson.Trim()) {
    throw "Unable to resolve the embedded repository/update identity from app_info.py."
}
$AppRepositoryIdentity = $AppRepositoryIdentityJson | ConvertFrom-Json
$ConfiguredRepositoryUrl = [string] $AppRepositoryIdentity.repository_url
$ConfiguredReleasesUrl = [string] $AppRepositoryIdentity.releases_url
$ConfiguredUpdateApiUrl = [string] $AppRepositoryIdentity.update_api_url
if ($ConfiguredRepositoryUrl -notmatch "^https://github\.com/(?<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$") {
    throw "APP_REPOSITORY_URL is not a canonical GitHub repository URL."
}
$ConfiguredRepositorySlug = [string] $Matches.slug

if ($OfficialRelease) {
    if ($ExpectedGitCommit -notmatch "^[0-9a-fA-F]{40}$") {
        throw "ExpectedGitCommit must be a complete 40-character Git commit."
    }
    if ($Repository -notmatch "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$") {
        throw "Repository must be an owner/name GitHub slug."
    }
    if ($WorkflowRunId -notmatch "^[1-9]\d*$") {
        throw "WorkflowRunId must be a positive GitHub Actions run ID."
    }
    if ($ActualGitCommit -ne $ExpectedGitCommit.ToLowerInvariant()) {
        throw "Checked-out commit does not match ExpectedGitCommit."
    }
    if ($InitialDirty) {
        throw "Official release builds require a clean tracked working tree."
    }
    if (
        $SourceVersion -cne $Version -or
        $SourceVersionNumeric -cne $VersionNumeric
    ) {
        throw (
            "Official release version does not match the source SemVer/Windows " +
            "version contract."
        )
    }
    $ExpectedRepositoryUrl = "https://github.com/$Repository"
    $ExpectedReleasesUrl = "$ExpectedRepositoryUrl/releases"
    $ExpectedUpdateApiUrl = "https://api.github.com/repos/$Repository/releases/latest"
    if (
        $ConfiguredRepositorySlug -cne $Repository -or
        $ConfiguredRepositoryUrl -cne $ExpectedRepositoryUrl -or
        $ConfiguredReleasesUrl -cne $ExpectedReleasesUrl -or
        $ConfiguredUpdateApiUrl -cne $ExpectedUpdateApiUrl
    ) {
        throw (
            "Embedded repository, release, and updater identities must exactly " +
            "match the official Repository parameter."
        )
    }
    if (
        $env:GITHUB_ACTIONS -ne "true" -or
        $env:GITHUB_EVENT_NAME -ne "push" -or
        $env:GITHUB_REF_TYPE -ne "tag" -or
        $env:GITHUB_REF_NAME -ne "v$Version" -or
        $env:GITHUB_REPOSITORY -ne $Repository -or
        $env:GITHUB_RUN_ID -ne $WorkflowRunId
    ) {
        throw "Official release parameters do not match the GitHub Actions tag context."
    }
    if (
        $SkipDependencyInstall -or
        $SkipTests -or
        $SkipInstaller -or
        $SkipPackagedSmoke -or
        $SkipMihomoDownload -or
        $RequireSigning -or
        $SkipInstallerAcceptance
    ) {
        throw "Official releases forbid every partial-build and Authenticode switch."
    }
    if ($env:SIGN_CERT_PATH -or $env:SIGN_CERT_PASSWORD) {
        throw "Anonymous official releases forbid Authenticode signing material."
    }
    if ($env:MIHOMO_DOWNLOAD_URL) {
        throw "Official releases forbid custom Mihomo download URLs."
    }
}

$EffectiveRepository = if ($OfficialRelease) {
    $Repository
}
else {
    $ConfiguredRepositorySlug
}
$RepositoryUrl = "https://github.com/$EffectiveRepository"
$BuildStartedUtc = [DateTime]::UtcNow.ToString("o")
$ReleaseVenvDir = Resolve-RepoPath ".venv"
$ReleasePython = Join-Path $ReleaseVenvDir "Scripts\python.exe"
$BuildDir = Resolve-RepoPath "build"
# Release candidates are assembled in dist-release, never in dist (the
# day-to-day build that "refresh dist" delivers and the user runs).
$DistDir = Resolve-RepoPath "dist-release"
$ReleaseDir = Resolve-RepoPath "release"
$DistAppDir = Join-Path $DistDir "Tesseract"
$ExePath = Join-Path $DistAppDir "Tesseract.exe"
$PortableZip = Join-Path $ReleaseDir "Tesseract-$Version-win-x64-portable.zip"
$InstallerPath = Join-Path $ReleaseDir "Tesseract-Setup-$Version-win-x64.exe"
$HashFile = Join-Path $ReleaseDir "SHA256SUMS.txt"
$SbomFile = Join-Path $ReleaseDir "sbom.cdx.json"
$ReleaseMetadataFile = Join-Path $ReleaseDir "release-metadata.json"
$ThirdPartySourcesZip = Join-Path $ReleaseDir "Tesseract-$Version-third-party-sources.zip"
$ThirdPartySourceManifest = Resolve-RepoPath "packaging\third_party_sources.template.json"
$ThirdPartySourceRoot = Resolve-RepoPath ".tmp\release-third-party-sources"
$ThirdPartyLicenseDir = Join-Path $BuildDir "third-party-licenses"
$BuildVersionFile = Resolve-RepoPath "app_version.txt"
$GeneratedVersionInfoFile = Join-Path $BuildDir "version_info.generated.txt"
$CandidateIdentityTool = Resolve-RepoPath "scripts\release_candidate_identity.py"
$CandidateManifestFile = Join-Path $BuildDir "release-candidate-identity.json"
$InstallerValidationRoot = Join-Path $BuildDir "installer-validation"
$InstallerValidationDir = Join-Path $InstallerValidationRoot "Tesseract"
$InstallerValidationLog = Join-Path $InstallerValidationRoot "install.log"
$TesseractUninstallKeyName = "{7D0F8A35-2A48-44A8-A75A-1A2F8631BA72}_is1"
$TesseractUninstallRegistryPaths = @(
    "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$TesseractUninstallKeyName",
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$TesseractUninstallKeyName",
    "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\$TesseractUninstallKeyName"
)
$PreviousVersionInfoPath = [System.Environment]::GetEnvironmentVariable(
    "TESSERACT_VERSION_INFO_PATH",
    "Process"
)
# app_version.txt is a generated, untracked input (the local shell refresh
# reads it too): the build writes the release version and restores it after.
$PreviousBuildVersionBytes = if (Test-Path -LiteralPath $BuildVersionFile -PathType Leaf) {
    [System.IO.File]::ReadAllBytes($BuildVersionFile)
}
else {
    $null
}

if (-not $SkipDependencyInstall) {
    $safeReleaseVenvDir = Assert-UnderRepo $ReleaseVenvDir
    if (Test-Path -LiteralPath $safeReleaseVenvDir) {
        Remove-Item -LiteralPath $safeReleaseVenvDir -Recurse -Force
    }
    Invoke-Native "release virtual environment creation" $BootstrapPython @(
        "-m", "venv", $safeReleaseVenvDir
    )
    Invoke-Native "locked dependency install" $ReleasePython @(
        "-m", "pip", "install",
        "--disable-pip-version-check",
        "--no-deps",
        "--require-hashes",
        "--only-binary=:all:",
        "-r", "requirements-windows.lock.txt"
    )
    # Web UI packages from ui/package-lock.json (integrity-checked, no install
    # scripts) and Rust crates from shell/Cargo.lock; the shell build itself
    # then runs fully offline (scripts/build_shell_preview.py).
    Invoke-Native "locked web UI dependency install" "npm" @(
        "--prefix", "ui", "ci", "--ignore-scripts", "--no-audit", "--no-fund"
    )
    Invoke-Native "locked Rust crate fetch" "cargo" @(
        "fetch", "--locked", "--manifest-path", "shell\Cargo.toml"
    )
}
elseif (-not (Test-Path -LiteralPath $ReleasePython)) {
    Write-Warning (
        "-SkipDependencyInstall is using the current Python environment because " +
        ".venv\Scripts\python.exe does not exist. This is partial release verification."
    )
    $ReleasePython = $BootstrapPython
}

# Complete the release-environment and external runtime preflight before
# deleting previously usable artifacts.
Invoke-Native "locked Python/build dependency verification" $ReleasePython @(
    "scripts\verify_release_environment.py",
    "--lock", "requirements-windows.lock.txt",
    "--requirements", "requirements.txt", "requirements-build.txt"
)

$TrustedUpdateKeyIdsPython = @'
import json
import app_info
from app_update_signing import decode_public_key, public_key_id

keys = (
    getattr(app_info, 'APP_UPDATE_SIGNING_PUBLIC_KEY', ''),
    *tuple(getattr(app_info, 'APP_UPDATE_SIGNING_PUBLIC_KEYS', ())),
)
key_ids = sorted(
    {public_key_id(decode_public_key(key)) for key in keys if key}
)
print(json.dumps(key_ids, separators=(',', ':')))
'@
$TrustedUpdateKeyIdsJson = [string] (
    & $ReleasePython -c $TrustedUpdateKeyIdsPython
)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to validate the embedded update-signing public-key ring."
}
$TrustedUpdateKeyIdsJson = $TrustedUpdateKeyIdsJson.Trim()
$TrustedUpdateKeyIds = @(
    $TrustedUpdateKeyIdsJson |
        ConvertFrom-Json |
        ForEach-Object { [string] $_ }
)
if (@($TrustedUpdateKeyIds | Where-Object { $_ -notmatch "^sha256:[0-9a-f]{64}$" }).Count) {
    throw "Embedded update-signing key IDs are malformed."
}
if ($OfficialRelease -and $TrustedUpdateKeyIds.Count -lt 1) {
    throw "Official releases require at least one valid embedded update-signing public key."
}

if (-not $SkipMihomoDownload) {
    $downloadArgs = @{
        OutputPath = (Resolve-RepoPath "bin\mihomo.exe")
    }
    if ($OfficialRelease) {
        # An official build must not silently reuse an unverified checkout
        # binary or let download_mihomo.ps1 return before refreshing WinTun.
        $downloadArgs.Force = $true
    }
    if ($env:MIHOMO_VERSION) {
        $downloadArgs.Version = $env:MIHOMO_VERSION
    }
    if ($env:MIHOMO_WINDOWS_AMD64_LEVEL) {
        $downloadArgs.CpuLevel = $env:MIHOMO_WINDOWS_AMD64_LEVEL
    }
    if ($env:MIHOMO_DOWNLOAD_URL) {
        $downloadArgs.DownloadUrl = $env:MIHOMO_DOWNLOAD_URL
    }
    & (Resolve-RepoPath "scripts\download_mihomo.ps1") @downloadArgs
}

$MihomoPath = Resolve-RepoPath "bin\mihomo.exe"
$WintunPath = Resolve-RepoPath "bin\wintun.dll"
if (-not (Test-Path -LiteralPath $MihomoPath -PathType Leaf)) {
    throw "Pinned Mihomo binary is missing: $MihomoPath"
}
if (-not (Test-Path -LiteralPath $WintunPath -PathType Leaf)) {
    throw "Pinned WinTun runtime is missing: $WintunPath"
}
$ExpectedMihomoSha256 = "842fa17493a82c97148e76e3c523f5058e3cf386fba611cab86b6681e75f2f77"
$ExpectedWintunSha256 = "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce"
if ((Get-Sha256 $MihomoPath) -ne $ExpectedMihomoSha256) {
    throw "Pinned Mihomo binary SHA-256 does not match the reviewed release input."
}
if ((Get-Sha256 $WintunPath) -ne $ExpectedWintunSha256) {
    throw "Pinned WinTun binary SHA-256 does not match the reviewed release input."
}
$MihomoVersionText = (& $MihomoPath -v | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $MihomoVersionText -notmatch "\bv?1\.19\.27\b") {
    throw "Expected Mihomo 1.19.27, got: $MihomoVersionText"
}

$IsccPath = $null
if (-not $SkipInstaller) {
    $IsccPath = Find-Iscc
    if (-not $IsccPath) {
        throw "Inno Setup ISCC.exe was not found. Install Inno Setup 6 or set ISCC_PATH."
    }
    if (-not $SkipInstallerAcceptance) {
        if (-not (Test-IsAdministrator)) {
            throw (
                "Installer acceptance requires an elevated Windows build process " +
                "(local builds may pass -SkipInstallerAcceptance)."
            )
        }
        $ExistingTesseractInstallKeys = @(
            $TesseractUninstallRegistryPaths |
                Where-Object { Test-Path -LiteralPath $_ }
        )
        if ($ExistingTesseractInstallKeys.Count -ne 0) {
            throw (
                "Installer acceptance refuses to replace an existing Tesseract " +
                "installation: $($ExistingTesseractInstallKeys -join ', ')"
            )
        }
    }
}

# Shell toolchain: Node.js (web UI), Rust (Tauri shell) and the two pinned
# license tools. Nothing here installs a compiler; CI sets them up first.
$ShellToolBin = Resolve-RepoPath ".build\t-p6-02-tools\bin"
if (Test-Path -LiteralPath $ShellToolBin -PathType Container) {
    $env:PATH = "$ShellToolBin;$env:PATH"
}
$ShellToolVersions = [ordered]@{}
foreach ($Tool in @("node", "npm", "cargo", "rustc", "cargo-about", "cargo-deny")) {
    $Command = Get-Command $Tool -ErrorAction SilentlyContinue
    if (-not $Command) {
        throw "$Tool is not on PATH. The shell release build needs Node.js, Rust, cargo-about 0.9.3 and cargo-deny 0.20.2."
    }
    $ShellToolVersions[$Tool] = ((& $Command.Source --version) | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $ShellToolVersions[$Tool]) {
        throw "Unable to read the $Tool version."
    }
}
if ($ShellToolVersions["cargo-about"] -notmatch "\b0\.9\.3\b") {
    throw "cargo-about 0.9.3 is required, got: $($ShellToolVersions['cargo-about'])"
}
if ($ShellToolVersions["cargo-deny"] -notmatch "\b0\.20\.2\b") {
    throw "cargo-deny 0.20.2 is required, got: $($ShellToolVersions['cargo-deny'])"
}
if ($RequireSigning -and -not $env:SIGN_CERT_PATH) {
    throw "-RequireSigning was specified but SIGN_CERT_PATH is not set."
}
if ($env:SIGN_CERT_PATH) {
    if (-not (Test-Path -LiteralPath $env:SIGN_CERT_PATH -PathType Leaf)) {
        throw "SIGN_CERT_PATH does not exist: $env:SIGN_CERT_PATH"
    }
    if (-not (Find-SignTool)) {
        throw "signtool.exe was not found. Install Windows SDK or add signtool.exe to PATH."
    }
}

# Fetch every redistributable source/license input while the previous build
# artifacts are still intact. Each input is pinned by the checked-in manifest
# and is hash-verified before it enters the persistent repository cache.
Sync-ThirdPartySourceInputs $ThirdPartySourceManifest $ThirdPartySourceRoot @(
    "pyside-shiboken", "qtbase", "qtsvg"
)

foreach ($dir in @($BuildDir, $DistDir, $ReleaseDir)) {
    $safeDir = Assert-UnderRepo $dir
    if (Test-Path -LiteralPath $dir) {
        Remove-Item -LiteralPath $safeDir -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $BuildDir | Out-Null
New-Item -ItemType Directory -Path $ReleaseDir | Out-Null

try {
Write-GeneratedVersionInfo `
    -DisplayVersion $Version `
    -VersionTuple $VersionTuple `
    -RepositoryUrl $RepositoryUrl `
    -OutputPath $GeneratedVersionInfoFile
[System.Environment]::SetEnvironmentVariable(
    "TESSERACT_VERSION_INFO_PATH",
    $GeneratedVersionInfoFile,
    "Process"
)
$ExpectedBuildVersionText = $Version + "`n"
[System.IO.File]::WriteAllText(
    $BuildVersionFile,
    $ExpectedBuildVersionText,
    [System.Text.Encoding]::ASCII
)
$WrittenBuildVersionText = [System.Text.Encoding]::ASCII.GetString(
    [System.IO.File]::ReadAllBytes($BuildVersionFile)
)
if ($WrittenBuildVersionText -cne $ExpectedBuildVersionText) {
    throw "app_version.txt did not round-trip the source release version."
}

# Shell legal inputs (T-P6-02/T-P6-04): the exact Rust crate graph, cargo-deny
# policy check and cargo-about inventory, all offline against the crates
# fetched from shell/Cargo.lock. The vendored sources feed the MPL-2.0 archive.
$ShellLegalInputsDir = Join-Path $BuildDir "shell-legal-inputs"
$ShellVendorDir = Join-Path $BuildDir "shell-vendor"
$ShellRustSourceDir = Join-Path $BuildDir "shell-mpl-sources"
Invoke-Native "shell crate graph, policy and license inventory" $ReleasePython @(
    "-B", "scripts\shell_release_licenses.py", "cargo-inputs",
    "--output-dir", $ShellLegalInputsDir,
    "--vendor-dir", $ShellVendorDir
)
$ShellLicenseArguments = @(
    "-B", "scripts\collect_release_licenses.py",
    "--flavor", "shell",
    "--lock", "requirements-windows.lock.txt",
    "--runtime-requirements", "requirements.txt",
    "--build-requirements", "requirements-build.txt",
    "--license-dir", $ThirdPartyLicenseDir,
    "--sbom", $SbomFile,
    "--mihomo-version", "1.19.27",
    "--app-version", $Version,
    "--cargo-about-json", (Join-Path $ShellLegalInputsDir "cargo-about.json"),
    "--cargo-metadata-json", (Join-Path $ShellLegalInputsDir "cargo-metadata.json"),
    "--cargo-tree", (Join-Path $ShellLegalInputsDir "cargo-tree.txt"),
    "--cargo-tree-linked", (Join-Path $ShellLegalInputsDir "cargo-tree-linked.txt")
)
# Preliminary pass: tesseract-runtime.spec bundles build\third-party-licenses
# and release\sbom.cdx.json into the sidecar. The post-freeze pass below is
# authoritative.
Invoke-Native "third-party license and SBOM collection" $ReleasePython $ShellLicenseArguments
Invoke-Native "MPL-2.0 crate source archive" $ReleasePython @(
    "-B", "scripts\shell_release_licenses.py", "mpl-sources",
    "--cargo-about-json", (Join-Path $ShellLegalInputsDir "cargo-about.json"),
    "--cargo-metadata-json", (Join-Path $ShellLegalInputsDir "cargo-metadata.json"),
    "--cargo-tree", (Join-Path $ShellLegalInputsDir "cargo-tree.txt"),
    "--cargo-tree-linked", (Join-Path $ShellLegalInputsDir "cargo-tree-linked.txt"),
    "--vendor-dir", $ShellVendorDir,
    "--output-dir", $ShellRustSourceDir
)
Invoke-Native "third-party source bundle" $ReleasePython @(
    "-B", "scripts\shell_release_licenses.py", "source-bundle",
    "--manifest", $ThirdPartySourceManifest,
    "--source-root", $ThirdPartySourceRoot,
    "--rust-source-dir", $ShellRustSourceDir,
    "--project-license", (Resolve-RepoPath "LICENSE"),
    "--third-party-notices", (Resolve-RepoPath "THIRD_PARTY_NOTICES.shell.md"),
    "--binary-mapping", (Resolve-RepoPath "packaging\THIRD_PARTY_BUILD_AND_BINARY_MAPPING.md"),
    "--output", $ThirdPartySourcesZip
)

# Compile owned source roots only. Historical audit artifacts intentionally
# contain ACL-restricted temporary directories and are not application source.
Invoke-Native "compileall root modules" $ReleasePython @("-m", "compileall", "-q", "-l", ".")
Invoke-Native "compileall scripts/tests" $ReleasePython @("-m", "compileall", "-q", "scripts", "tests")

# Source smoke: the sidecar's static import closure (runtime_main.pyw plus
# every function-level import) resolves inside the repository and is Qt-free.
$SourceSmokeStatus = "not_run"
Invoke-Native "source smoke test" $ReleasePython @(
    "-B", "-c",
    "import sidecar_import_graph as g; graph = g.sidecar_graph(); tainted = g.qt_tainted_modules(graph); " +
    "assert not tainted, sorted(tainted); print('Tesseract sidecar source smoke passed:', len(graph['modules']), 'modules')"
)
$SourceSmokeStatus = "passed"

$FullTestStatus = if ($SkipTests) { "skipped-partial" } else { "not_run" }
$MihomoConfigStatus = if ($SkipTests) { "skipped-partial" } else { "not_run" }
if (-not $SkipTests) {
    Invoke-Native "Mihomo configuration validation" $ReleasePython @(
        "-m", "pytest", "-q", "tests/test_mihomo_generated_config.py"
    )
    $MihomoConfigStatus = "passed"

    # Keep release verification scoped to the source test suite.
    Invoke-Native "pytest" $ReleasePython @("-m", "pytest", "-q", "tests")
    $FullTestStatus = "passed"
}

# Web UI (ui/build.mjs) + Tauri shell (cargo --release --locked --offline,
# feature "distribution") + PyInstaller sidecar (tesseract-runtime.spec),
# assembled and gated by scripts/build_shell_preview.py. Its side files
# (preview-build.json, ...) land next to the candidate directory, not in it.
$ShellBuildEvidence = Join-Path $BuildDir "shell-build"
$ShellBuildArguments = @(
    "-B", "scripts\build_shell_preview.py", "build",
    "--out", $DistAppDir,
    "--evidence", $ShellBuildEvidence
)
if ($OfficialRelease -or $CleanShellBuild) {
    $ShellBuildArguments += "--clean"
}
# Rust panic locations carry source paths: keep the build machine's profile,
# repository and crate-registry paths out of Tesseract.exe (rustc applies the
# last matching remap). CARGO_ENCODED_RUSTFLAGS replaces shell\.cargo\config.toml's
# rustflags, so the static CRT flag is repeated here. 0x1F separates the flags.
$ShellCargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE ".cargo" }
$PreviousEncodedRustFlags = $env:CARGO_ENCODED_RUSTFLAGS
$env:CARGO_ENCODED_RUSTFLAGS = @(
    "-C", "target-feature=+crt-static",
    "--remap-path-prefix=$($env:USERPROFILE)=/home",
    "--remap-path-prefix=$($RepoRoot.TrimEnd('\'))=/src",
    "--remap-path-prefix=$ShellCargoHome=/cargo"
) -join ([char] 0x1F)
try {
    Invoke-Native "shell, web UI and sidecar build" $ReleasePython $ShellBuildArguments
}
finally {
    $env:CARGO_ENCODED_RUSTFLAGS = $PreviousEncodedRustFlags
}

$RuntimeExePath = Join-Path $DistAppDir "tesseract-runtime.exe"
foreach ($BuiltExe in @($ExePath, $RuntimeExePath)) {
    if (-not (Test-Path -LiteralPath $BuiltExe -PathType Leaf)) {
        throw "The shell build did not produce $BuiltExe"
    }
    if ($env:USERPROFILE -and (Select-String -LiteralPath $BuiltExe -SimpleMatch -Pattern $env:USERPROFILE -Quiet)) {
        throw "$BuiltExe carries the build machine's profile path."
    }
}

Sign-Artifact $ExePath
Sign-Artifact $RuntimeExePath

# The preliminary legal/SBOM pass is a PyInstaller input. This second pass is
# authoritative: it inventories and hashes the actual Tesseract.exe, sidecar
# EXE/DLL/PYD payload, rejects unmapped or denied modules, and synchronizes
# portable-root legal materials from that final inventory.
Invoke-Native "frozen distribution license and SBOM audit" $ReleasePython (
    $ShellLicenseArguments + @("--dist-dir", $DistAppDir)
)

# PyInstaller copied the preliminary legal inputs into its resource directory.
# Replace those copies with the authoritative post-freeze inventory as well.
$InternalResourceDir = Assert-UnderRepo (Join-Path $DistAppDir "_internal")
if (-not (Test-Path -LiteralPath $InternalResourceDir -PathType Container)) {
    throw "PyInstaller resource directory is missing: $InternalResourceDir"
}
foreach ($LegalFile in @("LICENSE", "THIRD_PARTY_NOTICES.md")) {
    $RootLegalFile = Join-Path $DistAppDir $LegalFile
    if (-not (Test-Path -LiteralPath $RootLegalFile -PathType Leaf)) {
        throw "The license audit did not place $LegalFile in the candidate root."
    }
    Copy-Item -LiteralPath $RootLegalFile -Destination $InternalResourceDir -Force
}
Copy-Item -LiteralPath $SbomFile -Destination $InternalResourceDir -Force
$InternalThirdPartyLicenseDir = Assert-UnderRepo (
    Join-Path $InternalResourceDir "third-party-licenses"
)
if (Test-Path -LiteralPath $InternalThirdPartyLicenseDir) {
    Remove-Item -LiteralPath $InternalThirdPartyLicenseDir -Recurse -Force
}
Copy-Item -LiteralPath $ThirdPartyLicenseDir -Destination $InternalThirdPartyLicenseDir -Recurse -Force
# tier3_build_id.json (root and _internal) was finalized by tesseract-runtime.spec
# for tesseract-runtime.exe and is left untouched.

# This is the only release-candidate freeze point.  Every byte that the
# portable archive or installer may distribute is now present.  The identity
# manifest deliberately lives in build/, outside the candidate tree, so it
# cannot hash itself and no later qualification result needs to rewrite the
# candidate.
$PackagedReleaseMetadataPath = Join-Path $DistAppDir "release-metadata.json"
if (Test-Path -LiteralPath $PackagedReleaseMetadataPath) {
    throw (
        "release-metadata.json must remain external to the immutable release " +
        "candidate to avoid a qualification-result cycle."
    )
}
Invoke-Native "release candidate identity freeze" $ReleasePython @(
    $CandidateIdentityTool,
    "create",
    "--directory", $DistAppDir,
    "--manifest", $CandidateManifestFile
)
Assert-ReleaseCandidateIdentity `
    -PythonPath $ReleasePython `
    -IdentityToolPath $CandidateIdentityTool `
    -CandidateDirectory $DistAppDir `
    -ManifestPath $CandidateManifestFile `
    -Description "frozen release candidate verification"
$CandidateIdentity = Get-Content -Raw -LiteralPath $CandidateManifestFile |
    ConvertFrom-Json
if (
    $CandidateIdentity.schema_version -ne 1 -or
    $CandidateIdentity.algorithm -cne "SHA-256" -or
    $CandidateIdentity.archive_root -cne "Tesseract" -or
    [string] $CandidateIdentity.tree_sha256 -notmatch "^[0-9a-f]{64}$" -or
    [string] $CandidateIdentity.tesseract_exe_sha256 -notmatch "^[0-9a-f]{64}$" -or
    [int] $CandidateIdentity.file_count -lt 1
) {
    throw "Frozen release candidate identity manifest is incomplete or malformed."
}
$EmbeddedReleaseMetadataEntries = @(
    $CandidateIdentity.files |
        Where-Object {
            [System.IO.Path]::GetFileName(
                ([string] $_.path).Replace("/", "\")
            ) -ieq "release-metadata.json"
        }
)
if ($EmbeddedReleaseMetadataEntries.Count -ne 0) {
    throw "Frozen release candidate must not embed mutable qualification metadata."
}
$CandidateManifestSha256 = Get-Sha256 $CandidateManifestFile
$FrozenCandidateDir = $DistAppDir
$FrozenCandidateExe = Join-Path $FrozenCandidateDir "Tesseract.exe"
$CandidateDirectoryStatus = "passed"
$CandidateShellSmokeStatus = if ($SkipPackagedSmoke) {
    "skipped-partial"
}
else {
    "not_run"
}

if (-not $SkipPackagedSmoke) {
    # Isolated custom scope only (never the default scope or a live profile):
    # the frozen sidecar's 16-helper validator smoke, then one cold start of the
    # shell with that sidecar, host proxy/firewall state compared before/after.
    Invoke-Native "packaged shell and 16-helper smoke" $ReleasePython @(
        "-B", "scripts\build_shell_preview.py", "smoke",
        "--runs", "1",
        "--label", "release",
        "--app", $FrozenCandidateDir,
        "--evidence", (Join-Path $BuildDir "packaged-smoke")
    )
    Wait-FileUnlocked $FrozenCandidateExe
    Wait-FileUnlocked (Join-Path $FrozenCandidateDir "tesseract-runtime.exe")
    $CandidateShellSmokeStatus = "passed"
    Assert-ReleaseCandidateIdentity `
        -PythonPath $ReleasePython `
        -IdentityToolPath $CandidateIdentityTool `
        -CandidateDirectory $FrozenCandidateDir `
        -ManifestPath $CandidateManifestFile `
        -Description "post-packaged-smoke candidate verification"
}

$FinalGitCommit = (git rev-parse HEAD).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $FinalGitCommit -notmatch "^[0-9a-f]{40}$") {
    throw "Unable to re-resolve the Git commit after the release build."
}
$FinalTrackedStatus = @(git status --porcelain --untracked-files=no)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to recheck tracked working-tree state after the release build."
}
$FinalDirty = [bool] ($FinalTrackedStatus -join "")
if ($OfficialRelease -and ($FinalGitCommit -ne $ActualGitCommit -or $FinalDirty)) {
    throw "Official release build changed HEAD or the tracked working tree."
}

$ReleaseMode = if ($SkipInstaller) { "portable-only" } else { "portable-and-installer" }
$SigningMode = if ($OfficialRelease) {
    "unsigned-by-policy"
}
elseif ($env:SIGN_CERT_PATH) {
    "signed-local"
}
else {
    "unsigned-local"
}
$PackagedSmokeStatus = if ($SkipPackagedSmoke) { "skipped-partial" } else { "passed" }
$HelperSmokeStatus = if ($SkipPackagedSmoke) { "skipped-partial" } else { "passed" }
$ReleasePythonVersion = (
    & $ReleasePython -c "import platform; print(platform.python_version())"
).Trim()
if ($LASTEXITCODE -ne 0 -or -not $ReleasePythonVersion) {
    throw "Unable to record the release Python version."
}
$PyInstallerVersion = (
    & $ReleasePython -c "import PyInstaller; print(PyInstaller.__version__)"
).Trim()
if ($LASTEXITCODE -ne 0 -or -not $PyInstallerVersion) {
    throw "Unable to record the PyInstaller version."
}
$InnoSetupVersion = Get-InnoSetupVersion $IsccPath
if ($SkipPackagedSmoke) {
    Write-Warning (
        "Packaged smoke was skipped. Artifacts are only partially verified " +
        "and must not be reported as a verified release."
    )
}
if ($SkipInstaller) {
    Write-Host "Release mode: portable-only (no installer was built)."
}
if (-not $env:SIGN_CERT_PATH) {
    if ($OfficialRelease) {
        Write-Warning "Official Windows artifacts are Authenticode unsigned by project policy."
    }
    else {
        Write-Warning "Release mode is unsigned because SIGN_CERT_PATH is not set."
    }
}

$PortableIdentityStatus = "not_run"
Invoke-Native "portable candidate archive creation" $ReleasePython @(
    $CandidateIdentityTool,
    "create-zip",
    "--directory", $FrozenCandidateDir,
    "--manifest", $CandidateManifestFile,
    "--zip", $PortableZip
)
if (
    -not (Test-Path -LiteralPath $PortableZip -PathType Leaf) -or
    (Get-Item -LiteralPath $PortableZip).Length -le 0
) {
    throw "Portable archive was not created or is empty: $PortableZip"
}
Invoke-Native "portable candidate identity verification" $ReleasePython @(
    $CandidateIdentityTool,
    "verify-zip",
    "--zip", $PortableZip,
    "--manifest", $CandidateManifestFile
)
Assert-ReleaseCandidateIdentity `
    -PythonPath $ReleasePython `
    -IdentityToolPath $CandidateIdentityTool `
    -CandidateDirectory $FrozenCandidateDir `
    -ManifestPath $CandidateManifestFile `
    -Description "post-portable-verification candidate verification"
$PortableIdentityStatus = "passed"

$InstallerIdentityStatus = if ($SkipInstaller) { "skipped-partial" } else { "not_run" }
$InstalledSmokeStatus = if ($SkipInstaller) { "skipped-partial" } else { "not_run" }
$InstalledShellScopeStatus = if ($SkipInstaller -or $SkipInstallerAcceptance) { "skipped-partial" } else { "not_run" }
if ($SkipInstallerAcceptance) {
    $InstallerIdentityStatus = "skipped-partial"
    $InstalledSmokeStatus = "skipped-partial"
}
if (-not $SkipInstaller) {
    # The WebView2 Evergreen bootstrapper the installer embeds: downloaded from
    # the Microsoft fwlink in the manifest, hash- and signer-verified (cached).
    & (Resolve-RepoPath "packaging\windows\webview2\fetch_webview2_bootstrapper.ps1")
    if (-not $?) {
        throw "WebView2 bootstrapper fetch/verification failed."
    }
    & $IsccPath "/DAppVersion=$Version" "/DAppVersionNumeric=$VersionNumeric" "/DSourceDir=$FrozenCandidateDir" "/DOutputDir=$ReleaseDir" "packaging\windows\Tesseract-shell.iss"
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed."
    }
    if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
        throw "Installer was not created: $InstallerPath"
    }
    Assert-ReleaseCandidateIdentity `
        -PythonPath $ReleasePython `
        -IdentityToolPath $CandidateIdentityTool `
        -CandidateDirectory $FrozenCandidateDir `
        -ManifestPath $CandidateManifestFile `
        -Description "post-installer-compilation candidate verification"
    Sign-Artifact $InstallerPath

    # Tesseract Setup (shell/setup, 11-05): what users download is a small window around the
    # Inno package just compiled and signed. It embeds that package and runs it silently and
    # elevated, showing its progress. Any command-line switch (the in-app updater, /VERYSILENT
    # deployments, the acceptance install below) or a machine without WebView2 runs the
    # embedded package unchanged, so the asset keeps its name and every silent path its
    # behaviour. The inner package stays in build\, outside the release staging.
    $SetupWindowDir = Join-Path $BuildDir "setup-window"
    New-Item -ItemType Directory -Force -Path $SetupWindowDir | Out-Null
    $SetupInnerPath = Join-Path $SetupWindowDir "Tesseract-Setup-$Version-win-x64.inner.exe"
    Move-Item -LiteralPath $InstallerPath -Destination $SetupInnerPath -Force
    $SetupInstalledBytes = (Get-ChildItem -LiteralPath $FrozenCandidateDir -Recurse -File | Measure-Object -Property Length -Sum).Sum
    $env:TESSERACT_SETUP_PAYLOAD = $SetupInnerPath
    $env:TESSERACT_SETUP_INSTALLED_BYTES = [string] $SetupInstalledBytes
    # Panic locations carry source paths; keep the build machine's user folder out of the
    # published binary (the crates registry, the repository, then anything else under the
    # profile; rustc applies the last matching remap). Separated by 0x1F, so spaces are safe.
    $SetupCargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { Join-Path $env:USERPROFILE ".cargo" }
    # CARGO_ENCODED_RUSTFLAGS replaces every config rustflags, so the static CRT is set
    # here too: the setup window must start on a PC without the VC++ redistributable.
    $env:CARGO_ENCODED_RUSTFLAGS = @(
        "-C", "target-feature=+crt-static",
        "--remap-path-prefix=$($env:USERPROFILE)=/home",
        "--remap-path-prefix=$((Resolve-RepoPath '.').TrimEnd('\'))=/src",
        "--remap-path-prefix=$SetupCargoHome=/cargo"
    ) -join ([char] 0x1F)
    try {
        Invoke-Native "setup window build" "cargo" @("build", "--release", "--locked", "--offline", "--manifest-path", (Resolve-RepoPath "shell\setup\Cargo.toml"))
    } finally {
        Remove-Item Env:TESSERACT_SETUP_PAYLOAD, Env:TESSERACT_SETUP_INSTALLED_BYTES, Env:CARGO_ENCODED_RUSTFLAGS -ErrorAction SilentlyContinue
    }
    if ($env:USERPROFILE -and (Select-String -LiteralPath (Resolve-RepoPath "shell\setup\target\release\tesseract-setup.exe") -SimpleMatch -Pattern $env:USERPROFILE -Quiet)) {
        throw "The setup window still carries the build machine's profile path."
    }
    $SetupWindowExe = Resolve-RepoPath "shell\setup\target\release\tesseract-setup.exe"
    # The published installer is this program: it must not need the VC++ runtime DLLs.
    Invoke-Native "setup window static runtime check" $ReleasePython @(
        "-B", "-c",
        ("import sys; sys.path.insert(0, 'scripts'); import build_shell_preview as b; from pathlib import Path; " +
         "f = b.pe_facts(Path(sys.argv[1])); " +
         "bad = [d for d in f['imports'] + f['delay_imports'] if d.lower().startswith(('vcruntime', 'msvcp', 'concrt', 'api-ms-win-crt-'))]; " +
         "sys.exit('setup window links the VC++ runtime dynamically: ' + ', '.join(bad)) if bad else print('setup window imports: ' + ', '.join(sorted(set(f['imports']))))"),
        $SetupWindowExe
    )
    if (-not (Test-Path -LiteralPath $SetupWindowExe -PathType Leaf) -or
        (Get-Item -LiteralPath $SetupWindowExe).Length -le (Get-Item -LiteralPath $SetupInnerPath).Length) {
        throw "The setup window was not built around the installer: $SetupWindowExe"
    }
    Copy-Item -LiteralPath $SetupWindowExe -Destination $InstallerPath -Force
    Sign-Artifact $InstallerPath
    Assert-ReleaseCandidateIdentity `
        -PythonPath $ReleasePython `
        -IdentityToolPath $CandidateIdentityTool `
        -CandidateDirectory $FrozenCandidateDir `
        -ManifestPath $CandidateManifestFile `
        -Description "pre-installation candidate verification"

    if (-not $SkipInstallerAcceptance) {
    if (Test-Path -LiteralPath $InstallerValidationRoot) {
        throw "Installer validation root unexpectedly exists: $InstallerValidationRoot"
    }
    New-Item -ItemType Directory -Path $InstallerValidationRoot | Out-Null
    $InstallerValidationArguments = @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/NOICONS",
        "/DIR=`"$InstallerValidationDir`"",
        "/LOG=`"$InstallerValidationLog`""
    ) -join " "
    try {
        Invoke-HiddenProcessWithTimeout `
            -Description "isolated installer acceptance" `
            -FilePath $InstallerPath `
            -Arguments $InstallerValidationArguments `
            -TimeoutMilliseconds 180000

        $InstalledRegistryPaths = @(
            $TesseractUninstallRegistryPaths |
                Where-Object { Test-Path -LiteralPath $_ }
        )
        if ($InstalledRegistryPaths.Count -ne 1) {
            throw (
                "Isolated installer acceptance expected exactly one uninstall " +
                "registration, found $($InstalledRegistryPaths.Count)."
            )
        }
        $InstalledRegistration = Get-ItemProperty -LiteralPath $InstalledRegistryPaths[0]
        $RegisteredInstallLocation = [System.IO.Path]::GetFullPath(
            ([string] $InstalledRegistration.InstallLocation).Trim().TrimEnd("\", "/")
        )
        if (-not $RegisteredInstallLocation.Equals(
            [System.IO.Path]::GetFullPath($InstallerValidationDir),
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            throw (
                "Installer registered an unexpected isolated location: " +
                $RegisteredInstallLocation
            )
        }
        if ([string] $InstalledRegistration.DisplayVersion -cne $Version) {
            throw "Installer registered an unexpected version."
        }
        Invoke-Native "pre-smoke installed candidate identity verification" $ReleasePython @(
            $CandidateIdentityTool,
            "verify-installed",
            "--directory", $InstallerValidationDir,
            "--manifest", $CandidateManifestFile
        )
        $InstalledCandidateExe = Join-Path $InstallerValidationDir "Tesseract.exe"
        $InstalledRuntimeExe = Join-Path $InstallerValidationDir "tesseract-runtime.exe"
        $InstalledSmokeProfile = Join-Path $InstallerValidationRoot "installed-smoke-profile"
        # The sidecar never creates its data directory for a helper smoke.
        New-Item -ItemType Directory -Force -Path (Join-Path $InstalledSmokeProfile "data") | Out-Null
        Invoke-ExactSmokeProcess `
            -Description "installed sidecar helper smoke" `
            -FilePath $InstalledRuntimeExe `
            -WorkingDirectory $InstallerValidationDir `
            -Arguments "--validator-helper-smoke 4" `
            -ExpectedMarker "Tesseract validator helper smoke test passed" `
            -OutputPath (Join-Path $InstallerValidationRoot "installed-smoke.out") `
            -ErrorPath (Join-Path $InstallerValidationRoot "installed-smoke.err") `
            -ConfigDirectory (Join-Path $InstalledSmokeProfile "config") `
            -DataDirectory (Join-Path $InstalledSmokeProfile "data") `
            -TimeoutMilliseconds 120000
        Wait-FileUnlocked $InstalledRuntimeExe
        $InstalledSmokeStatus = "passed"
        Invoke-Native "post-smoke installed candidate identity verification" $ReleasePython @(
            $CandidateIdentityTool,
            "verify-installed",
            "--directory", $InstallerValidationDir,
            "--manifest", $CandidateManifestFile
        )

        # The installed shell decides its instance scope correctly: only
        # --print-scope / --version run, under fake environments (no lock, no
        # profile, no window), exactly as the build gate checks the candidate.
        Invoke-Native "installed shell scope check" $ReleasePython @(
            "-B", "-c",
            "import sys; from pathlib import Path; sys.path.insert(0, 'scripts'); import build_shell_preview as b; " +
            "b.gate_print_scope(Path(sys.argv[1]), b.release_contract()); print('Tesseract installed shell scope check passed')",
            $InstallerValidationDir
        )
        Wait-FileUnlocked $InstalledCandidateExe
        $InstalledShellScopeStatus = "passed"
        Invoke-Native "installed candidate identity verification" $ReleasePython @(
            $CandidateIdentityTool,
            "verify-installed",
            "--directory", $InstallerValidationDir,
            "--manifest", $CandidateManifestFile
        )
        $InstallerIdentityStatus = "passed"
        Assert-ReleaseCandidateIdentity `
            -PythonPath $ReleasePython `
            -IdentityToolPath $CandidateIdentityTool `
            -CandidateDirectory $FrozenCandidateDir `
            -ManifestPath $CandidateManifestFile `
            -Description "post-installed-tree-verification candidate verification"
    }
    finally {
        $ValidationUninstaller = Join-Path $InstallerValidationDir "unins000.exe"
        if (Test-Path -LiteralPath $ValidationUninstaller -PathType Leaf) {
            Invoke-HiddenProcessWithTimeout `
                -Description "isolated installer acceptance cleanup" `
                -FilePath $ValidationUninstaller `
                -Arguments "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART" `
                -TimeoutMilliseconds 180000
            Wait-PathsAbsent `
                -Paths @($ValidationUninstaller) `
                -Description "Installer acceptance uninstaller"
        }
        Wait-PathsAbsent `
            -Paths $TesseractUninstallRegistryPaths `
            -Description "Installer acceptance uninstall registration"
        $RemainingInstallKeys = @(
            $TesseractUninstallRegistryPaths |
                Where-Object { Test-Path -LiteralPath $_ }
        )
        if ($RemainingInstallKeys.Count -ne 0) {
            throw (
                "Installer acceptance cleanup left uninstall registration: " +
                ($RemainingInstallKeys -join ", ")
            )
        }
        if (Test-Path -LiteralPath $InstallerValidationDir) {
            $RemainingInstalledFiles = @(
                Get-ChildItem -LiteralPath $InstallerValidationDir -File -Recurse -Force
            )
            if ($RemainingInstalledFiles.Count -ne 0) {
                throw (
                    "Installer acceptance cleanup left installed files: " +
                    ($RemainingInstalledFiles.FullName -join ", ")
                )
            }
            Remove-Item -LiteralPath $InstallerValidationDir -Recurse -Force
        }
    }
    }
    Assert-ReleaseCandidateIdentity `
        -PythonPath $ReleasePython `
        -IdentityToolPath $CandidateIdentityTool `
        -CandidateDirectory $FrozenCandidateDir `
        -ManifestPath $CandidateManifestFile `
        -Description "post-installer-acceptance candidate verification"
}

# Qualification results are external release evidence.  Writing this document
# into the candidate would mutate the bytes that the preceding tests approved.
$MetadataTag = if ($OfficialRelease) { $env:GITHUB_REF_NAME } else { $null }
$MetadataGitRef = if ($OfficialRelease) { $env:GITHUB_REF } else { $null }
$MetadataWorkflowRunId = if ($OfficialRelease) { $WorkflowRunId } else { $null }
$ReleaseMetadata = [ordered]@{
    schema_version = 2
    version = $Version
    tag = $MetadataTag
    repository = $EffectiveRepository
    git_commit = $FinalGitCommit
    git_ref = $MetadataGitRef
    dirty = $FinalDirty
    workflow_run_id = $MetadataWorkflowRunId
    build_time_utc = $BuildStartedUtc
    python = $ReleasePythonVersion
    flavor = "shell"
    mihomo = "1.19.27"
    wintun = "0.14.1"
    artifact_mode = $ReleaseMode
    authenticode = $SigningMode
    release_signature = "pending"
    key_id = $null
    trusted_update_key_ids = [string[]] $TrustedUpdateKeyIds
    packaged_smoke = $PackagedSmokeStatus
    candidate_identity = [ordered]@{
        schema_version = [int] $CandidateIdentity.schema_version
        manifest_sha256 = $CandidateManifestSha256
        tree_sha256 = [string] $CandidateIdentity.tree_sha256
        tesseract_exe_sha256 = [string] $CandidateIdentity.tesseract_exe_sha256
        file_count = [int] $CandidateIdentity.file_count
    }
    toolchain = [ordered]@{
        python = $ReleasePythonVersion
        pyinstaller = $PyInstallerVersion
        inno_setup = $InnoSetupVersion
        node = [string] $ShellToolVersions["node"]
        npm = [string] $ShellToolVersions["npm"]
        cargo = [string] $ShellToolVersions["cargo"]
        rustc = [string] $ShellToolVersions["rustc"]
        cargo_about = [string] $ShellToolVersions["cargo-about"]
        cargo_deny = [string] $ShellToolVersions["cargo-deny"]
        windows_version_numeric = $VersionNumeric
    }
    tests = [ordered]@{
        source_smoke = $SourceSmokeStatus
        pytest = $FullTestStatus
        mihomo_config = $MihomoConfigStatus
        packaged_smoke = $PackagedSmokeStatus
        helper_smoke = $HelperSmokeStatus
        candidate_shell_smoke = $CandidateShellSmokeStatus
        candidate_directory = $CandidateDirectoryStatus
        portable_zip = $PortableIdentityStatus
        installed_smoke = $InstalledSmokeStatus
        installed_shell_scope = $InstalledShellScopeStatus
        installer_tree = $InstallerIdentityStatus
        public_release_audit = "pending"
    }
}
$ReleaseMetadataJson = ($ReleaseMetadata | ConvertTo-Json -Depth 6) + "`n"
[System.IO.File]::WriteAllText(
    $ReleaseMetadataFile,
    $ReleaseMetadataJson,
    [System.Text.UTF8Encoding]::new($false)
)
Assert-ReleaseCandidateIdentity `
    -PythonPath $ReleasePython `
    -IdentityToolPath $CandidateIdentityTool `
    -CandidateDirectory $FrozenCandidateDir `
    -ManifestPath $CandidateManifestFile `
    -Description "post-metadata candidate verification"

$HashArtifactPaths = @(
    $PortableZip,
    $SbomFile,
    $ReleaseMetadataFile,
    $ThirdPartySourcesZip
)
if (-not $SkipInstaller) {
    $HashArtifactPaths += $InstallerPath
}
$HashArtifacts = @(
    $HashArtifactPaths |
        ForEach-Object {
            if (-not (Test-Path -LiteralPath $_ -PathType Leaf)) {
                throw "Required release payload is missing: $_"
            }
            $item = Get-Item -LiteralPath $_
            if ($item.Length -le 0) {
                throw "Required release payload is empty: $_"
            }
            $item
        } |
        Sort-Object Name
)
if (@($HashArtifacts.Name | Select-Object -Unique).Count -ne $HashArtifacts.Count) {
    throw "Release payload file names must be unique."
}
if ($OfficialRelease -and $HashArtifacts.Count -ne 5) {
    throw "Official releases must contain exactly five unsigned manifest payloads."
}
$ExpectedPreManifestFiles = @($HashArtifacts.Name | Sort-Object)
$ActualPreManifestFiles = @(
    Get-ChildItem -LiteralPath $ReleaseDir -File |
        ForEach-Object Name |
        Sort-Object
)
if (Compare-Object -ReferenceObject $ExpectedPreManifestFiles -DifferenceObject $ActualPreManifestFiles) {
    throw "Release staging contains a missing or unexpected file before manifest creation."
}
$hashLines = @(
    $HashArtifacts | ForEach-Object {
        $hash = Get-Sha256 $_.FullName
        "$hash  $($_.Name)"
    }
)
$ExpectedHashText = ($hashLines -join "`n") + "`n"
[System.IO.File]::WriteAllText(
    $HashFile,
    $ExpectedHashText,
    [System.Text.UTF8Encoding]::new($false)
)
$HashBytes = [System.IO.File]::ReadAllBytes($HashFile)
if (
    $HashBytes.Length -ge 3 -and
    $HashBytes[0] -eq 0xef -and
    $HashBytes[1] -eq 0xbb -and
    $HashBytes[2] -eq 0xbf
) {
    throw "SHA256SUMS.txt must be UTF-8 without a BOM."
}
$StrictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
$WrittenHashText = $StrictUtf8.GetString($HashBytes)
if ($WrittenHashText.Contains("`r") -or $WrittenHashText -ne $ExpectedHashText) {
    throw "SHA256SUMS.txt must use exact UTF-8 LF bytes with one trailing newline."
}
$WrittenHashLines = @(
    $WrittenHashText.Split("`n", [System.StringSplitOptions]::RemoveEmptyEntries)
)
if (
    $WrittenHashLines.Count -ne $hashLines.Count -or
    (Compare-Object -ReferenceObject $hashLines -DifferenceObject $WrittenHashLines)
) {
    throw "SHA256SUMS.txt did not round-trip the computed release hashes."
}
foreach ($Artifact in $HashArtifacts) {
    $ActualHash = Get-Sha256 $Artifact.FullName
    $ExpectedLine = "$ActualHash  $($Artifact.Name)"
    if ($WrittenHashLines -notcontains $ExpectedLine) {
        throw "SHA256SUMS.txt does not match $($Artifact.Name)."
    }
}
Assert-ReleaseCandidateIdentity `
    -PythonPath $ReleasePython `
    -IdentityToolPath $CandidateIdentityTool `
    -CandidateDirectory $FrozenCandidateDir `
    -ManifestPath $CandidateManifestFile `
    -Description "final release candidate verification"

Write-Host (
    "Release artifacts written to $ReleaseDir " +
    "(mode=$ReleaseMode, signing=$SigningMode, packaged_smoke=$PackagedSmokeStatus)"
)
}
finally {
    [System.Environment]::SetEnvironmentVariable(
        "TESSERACT_VERSION_INFO_PATH",
        $PreviousVersionInfoPath,
        "Process"
    )
    if ($null -ne $PreviousBuildVersionBytes) {
        [System.IO.File]::WriteAllBytes($BuildVersionFile, $PreviousBuildVersionBytes)
    }
    else {
        Remove-Item -LiteralPath $BuildVersionFile -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $GeneratedVersionInfoFile -Force -ErrorAction SilentlyContinue
}
}
finally {
    try {
        $BuildMutex.ReleaseMutex()
    }
    finally {
        $BuildMutex.Dispose()
    }
}

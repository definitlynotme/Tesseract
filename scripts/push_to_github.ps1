[CmdletBinding()]
param(
    # Also create and push the signed release tag v<version> from app_info.py
    # (starts the "Release Windows" workflow). Without it only main is pushed.
    [switch] $Release,
    # The public repository: a separate local clone whose history contains only
    # "Tesseract contributors" commits signed with $SshSigningKey. Created on
    # the first run by scripts\bootstrap_public_repository.py.
    [string] $PublicDir = "",
    # GitHub remote of the public repository (default: this repository's origin).
    [string] $RemoteUrl = "",
    [string] $GitHubLogin = "definitlynotme",
    [string] $GitHubId = "283048617",
    # Ed25519 SSH key registered on GitHub as a *signing* key.
    [string] $SshSigningKey = "",
    # Private deny list for the anonymity/secret audit (one value per line,
    # outside the repository; the same list as the PUBLIC_RELEASE_PRIVATE_DENYLIST
    # secret of the release environment).
    [string] $DenyFile = "",
    # Prepare and audit the public commit, but do not push anything.
    [switch] $NoPush
)

# One-step publish (RELEASING.md). This private working repository is never
# pushed: its history carries personal identities. The non-ignored file tree
# is copied into the public repository, audited, committed and pushed from
# there. Never force-pushes.

$ErrorActionPreference = "Stop"
# git prints paths as UTF-8; read them as such (non-ASCII file names).
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$RepoRoot =(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot

function Invoke-Git([string] $Directory, [string[]] $Arguments) {
    $output = & git -C $Directory @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed in $Directory (exit $LASTEXITCODE)."
    }
    return $output
}

if (-not $PublicDir) { $PublicDir = Join-Path (Split-Path -Parent $RepoRoot) "Tessract-public" }
if (-not $SshSigningKey) { $SshSigningKey = Join-Path $env:USERPROFILE ".ssh\id_ed25519" }
if (-not $DenyFile) { $DenyFile = Join-Path $env:USERPROFILE ".tesseract-release\public-denylist.txt" }
if (-not $RemoteUrl) {
    # The official repository is the one the app's update check reads
    # (app_info.APP_REPOSITORY_URL); this private checkout may have no origin.
    $python0 = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python0 -PathType Leaf)) { $python0 = "python" }
    $RemoteUrl = ((& $python0 -B -c "import app_info; print(app_info.APP_REPOSITORY_URL + '.git')") | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $RemoteUrl) { throw "Unable to read APP_REPOSITORY_URL from app_info.py." }
}
foreach ($required in @($SshSigningKey, $DenyFile)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Missing $required (see RELEASING.md)."
    }
}
$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}
$email = "$GitHubId+$GitHubLogin@users.noreply.github.com"

$tracked = @(Invoke-Git $RepoRoot @("status", "--porcelain", "--untracked-files=no"))
if ($tracked.Count -ne 0) {
    throw "The private repository has uncommitted tracked changes; commit them first."
}
$sourceCommit = ((Invoke-Git $RepoRoot @("rev-parse", "--short", "HEAD")) | Out-String).Trim()
$version = ((& $python -B -c "from app_info import DEFAULT_APP_VERSION; print(DEFAULT_APP_VERSION)") | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Unable to read the version from app_info.py." }

# The bootstrap signs with no terminal attached (it can never ask for a
# passphrase), so the SSH key must be held by an agent that Git's own
# ssh-keygen reaches: Git for Windows' ssh-agent. The passphrase is typed once
# here; the agent this script starts is stopped again in the finally block.
$gitRoot = Split-Path (Split-Path (Split-Path (((& git --exec-path) | Out-String).Trim() -replace '/', '\')))
$sshBin = Join-Path $gitRoot "usr\bin"
$sshAgent = Join-Path $sshBin "ssh-agent.exe"
$sshAdd = Join-Path $sshBin "ssh-add.exe"
$sshKeygen = Join-Path $sshBin "ssh-keygen.exe"
foreach ($tool in @($sshAgent, $sshAdd, $sshKeygen)) {
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw "Git for Windows' $(Split-Path -Leaf $tool) is missing ($tool)." }
}
function Get-AgentState([string] $Fingerprint) {
    # 0 = key loaded, 1 = agent without the key, 2 = no reachable agent
    $ErrorActionPreference = "Continue"
    $listing = & $sshAdd -l 2>$null
    if ($LASTEXITCODE -eq 2) { return 2 }
    if (($listing -join "`n") -match [regex]::Escape($Fingerprint)) { return 0 }
    return 1
}
$keyFingerprint = (((& $sshKeygen -lf "$SshSigningKey.pub") | Out-String).Trim() -split '\s+')[1]
if (-not $keyFingerprint -or -not $keyFingerprint.StartsWith("SHA256:")) { throw "Cannot read the fingerprint of $SshSigningKey.pub." }
$originalAgentSocket = $env:SSH_AUTH_SOCK
$originalAgentPid = $env:SSH_AGENT_PID
$startedAgent = $false
$agentState = Get-AgentState $keyFingerprint
if ($agentState -eq 2) {
    $agentOutput = ((& $sshAgent -s) | Out-String)
    $socket = [regex]::Match($agentOutput, 'SSH_AUTH_SOCK=([^;]+);').Groups[1].Value
    $agentPid = [regex]::Match($agentOutput, 'SSH_AGENT_PID=(\d+);').Groups[1].Value
    if (-not $socket -or -not $agentPid) { throw "Could not start Git's ssh-agent." }
    $env:SSH_AUTH_SOCK = $socket
    $env:SSH_AGENT_PID = $agentPid
    $startedAgent = $true
}

try {
if ($agentState -ne 0) {
    Write-Host "Enter the passphrase of $SshSigningKey once; it signs the public commit and the tag."
    & $sshAdd $SshSigningKey
    if ($LASTEXITCODE -ne 0 -or (Get-AgentState $keyFingerprint) -ne 0) {
        throw "ssh-add did not load $SshSigningKey; the public commit cannot be signed."
    }
}

$publicGit = Join-Path $PublicDir ".git"
if (-not (Test-Path -LiteralPath $publicGit)) {
    # First publication: one SSH-signed root commit of the non-ignored tree,
    # audited before and after the commit (scripts\bootstrap_public_repository.py).
    New-Item -ItemType Directory -Force -Path $PublicDir | Out-Null
    & $python -B scripts\bootstrap_public_repository.py `
        --source-repo $RepoRoot `
        --target-dir $PublicDir `
        --github-id $GitHubId `
        --github-login $GitHubLogin `
        --ssh-signing-key $SshSigningKey `
        --deny-file $DenyFile
    if ($LASTEXITCODE -ne 0) { throw "Public repository bootstrap failed (see the code above)." }
}
else {
    # Later publications: replace the public tree with the current non-ignored
    # tree and add one signed commit (the repository is configured with the
    # project identity, commit.gpgsign and tag.gpgsign by the bootstrap).
    $listing = & git -C $RepoRoot ls-files -c -o --exclude-standard -z
    if ($LASTEXITCODE -ne 0) { throw "Unable to list the source tree." }
    $files = @(($listing -join "") -split "`0" | Where-Object { $_ })
    Get-ChildItem -LiteralPath $PublicDir -Force |
        Where-Object { $_.Name -ne ".git" } |
        Remove-Item -Recurse -Force
    foreach ($relative in $files) {
        $source = Join-Path $RepoRoot $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
        $destination = Join-Path $PublicDir $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
    Invoke-Git $PublicDir @("add", "--all") | Out-Null
    & git -C $PublicDir diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        & $python -B scripts\audit_public_release.py --repo-root $PublicDir --deny-file $DenyFile
        if ($LASTEXITCODE -ne 0) {
            Invoke-Git $PublicDir @("reset", "--quiet") | Out-Null
            throw "Anonymity/secret audit failed; nothing was committed."
        }
        Invoke-Git $PublicDir @("commit", "--quiet", "-S", "-m", "Tesseract $version") | Out-Null
        Write-Host "Committed Tesseract $version (from private $sourceCommit) to $PublicDir."
    }
    else {
        Write-Host "Public tree already matches the source tree."
    }
}

& $python -B scripts\audit_public_release.py --repo-root $PublicDir --deny-file $DenyFile
if ($LASTEXITCODE -ne 0) { throw "Anonymity/secret audit of the public repository failed." }

if ($Release) {
    $tag = "v$version"
    & git -C $PublicDir rev-parse -q --verify "refs/tags/$tag" | Out-Null
    if ($LASTEXITCODE -eq 0) {
        throw "Tag $tag already exists; bump APP_RELEASE_BASE_VERSION in app_info.py for a new release."
    }
    Invoke-Git $PublicDir @("tag", "-s", $tag, "-m", "Tesseract $version") | Out-Null
    Write-Host "Created signed tag $tag."
}

if ($NoPush) {
    Write-Host "Prepared $PublicDir; nothing pushed (-NoPush)."
    return
}

# Fails early (and without prompting) when GitHub credentials are missing or
# expired: run `gh auth login` and `gh auth setup-git`, then retry.
$env:GIT_TERMINAL_PROMPT = "0"
& git -C $PublicDir ls-remote --heads $RemoteUrl | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Cannot reach $RemoteUrl with the stored credentials. Run 'gh auth login' and 'gh auth setup-git', then retry."
}
Invoke-Git $PublicDir @("push", $RemoteUrl, "main:main") | Out-Null
Write-Host "Pushed main to $RemoteUrl."
if ($Release) {
    Invoke-Git $PublicDir @("push", $RemoteUrl, "refs/tags/v${version}:refs/tags/v${version}") | Out-Null
    Write-Host "Pushed v$version; the Release Windows workflow builds and publishes it."
}
}
finally {
    if ($startedAgent) {
        # Stop the agent this script started (it holds the unlocked key) and
        # give the caller its original agent settings back.
        $ErrorActionPreference = "Continue"
        & $sshAgent -k 2>$null | Out-Null
        if ($originalAgentSocket) { $env:SSH_AUTH_SOCK = $originalAgentSocket } else { Remove-Item Env:SSH_AUTH_SOCK -ErrorAction SilentlyContinue }
        if ($originalAgentPid) { $env:SSH_AGENT_PID = $originalAgentPid } else { Remove-Item Env:SSH_AGENT_PID -ErrorAction SilentlyContinue }
    }
}

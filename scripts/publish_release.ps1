[CmdletBinding()]
param(
    # owner/name on GitHub. Default: APP_REPOSITORY_URL in app_info.py (the
    # address the app's update check reads), so the two can never disagree.
    [string] $Repository = "",
    # Encrypted Ed25519 update-signing key (PKCS#8 PEM). Never inside the repository.
    [string] $SigningKey = "",
    # Private deny list for the anonymity/secret audit (see RELEASING.md).
    [string] $DenyFile = "",
    # Ed25519 SSH key used to sign the public commits and the release tag.
    [string] $SshSigningKey = "",
    # Pause the signing job until you approve it on github.com.
    [switch] $RequireApproval,
    # Check everything and prepare the public commit locally; change nothing on GitHub.
    [switch] $DryRun,
    # Do not wait for the release workflow and the live update check.
    [switch] $NoWatch,
    # The version tag is already on GitHub but its build failed and no release
    # (not even a draft) uses it: delete it there and in the public clone, and
    # tag a new commit of the current source instead. Never touches a release.
    [switch] $MoveUnreleasedTag
)

# One-click public release (RELEASING.md):
#   1. read-only checks: committed tree, GitHub login and scopes, SSH signing
#      key, update-signing key, repository state, tag not yet used;
#   2. one confirmation;
#   3. set up what is missing (repository, `release` environment, secrets),
#      then scripts\push_to_github.ps1 -Release (audit, signed commit, signed tag, push);
#   4. watch the "Release Windows" workflow, then download the published
#      release the way the app does and verify its signature.
# Safe to re-run: every step checks before it writes, and nothing is force-pushed.

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
Set-Location $RepoRoot
$PasswordEnv = "TESSERACT_RELEASE_SIGNING_PASSWORD"

function Write-Step([string] $Text) { Write-Host ""; Write-Host "== $Text" -ForegroundColor Cyan }
function Write-Ok([string] $Text) { Write-Host "   ok  $Text" -ForegroundColor Green }
function Write-Todo([string] $Text) { Write-Host "   ->  $Text" -ForegroundColor Yellow }
function Stop-Publish([string] $Text) { Write-Host ""; Write-Host "STOP: $Text" -ForegroundColor Red; exit 1 }

function ConvertTo-ArgumentString([string[]] $Arguments) {
    $quoted = foreach ($argument in $Arguments) {
        if ($argument -eq "") { '""' }
        elseif ($argument -notmatch '[\s"]') { $argument }
        else { '"' + (($argument -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"' }
    }
    return ($quoted -join " ")
}

# Runs a tool with separate stdout/stderr and optional exact stdin bytes (no
# trailing newline, so secrets are stored byte-for-byte).
function Invoke-ToolOnce([string] $Exe, [string[]] $Arguments, [object] $InputText = $null) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = (Get-Command $Exe -ErrorAction Stop).Source
    $info.Arguments = ConvertTo-ArgumentString $Arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardInput = ($null -ne $InputText)
    $info.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $info.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    # .NET Framework opens a redirected stdin with Console.InputEncoding and
    # writes its preamble at once: with the console on code page 65001 that is a
    # UTF-8 BOM in front of every body. GitHub then rejects JSON ("Problems
    # parsing JSON") and a secret would be stored with three extra bytes. Start
    # the child with a BOM-less UTF-8 input encoding and restore the console's.
    $restoreInputEncoding = $null
    if ($null -ne $InputText) {
        try {
            if ([Console]::InputEncoding.GetPreamble().Length -gt 0) {
                $restoreInputEncoding = [Console]::InputEncoding
                [Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
            }
        }
        catch { $restoreInputEncoding = $null }
    }
    try { $process = [System.Diagnostics.Process]::Start($info) }
    finally {
        if ($null -ne $restoreInputEncoding) {
            try { [Console]::InputEncoding = $restoreInputEncoding } catch { }
        }
    }
    if ($null -ne $InputText -and $process.StandardInput.Encoding.GetPreamble().Length -gt 0) {
        # The preamble already went out: stop the child before it gets any data.
        try { $process.Kill() } catch { }
        throw "Cannot pass exact bytes to $Exe (its stdin would start with a byte-order mark)."
    }
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if ($null -ne $InputText) {
        $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes([string] $InputText)
        $process.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
        $process.StandardInput.Close()
    }
    $process.WaitForExit()
    return [pscustomobject]@{ Code = $process.ExitCode; Out = $stdout.Result.Trim(); Err = $stderr.Result.Trim() }
}

# Connections to GitHub from this machine drop now and then (gh: `EOF`). A call
# is repeated only when its failure looks like the network, never when GitHub
# answered with an error; every write below is safe to repeat or re-checked.
$TransientNetworkPattern = '(?i)\bEOF\b|connection (was )?reset|forcibly closed|wsarecv|wsasend|i/o timeout|' +
    'handshake timeout|timeout awaiting|deadline exceeded|HTTP 50[234]|returned error: 50[234]|connectex|' +
    'no such host|could not resolve host|recv failure|ssl_error_syscall|schannel|failed to connect|early eof|' +
    'rpc failed|timed out|remote end closed|temporarily unavailable'
function Invoke-Tool([string] $Exe, [string[]] $Arguments, [object] $InputText = $null) {
    for ($attempt = 1; ; $attempt++) {
        $result = Invoke-ToolOnce $Exe $Arguments $InputText
        if ($result.Code -eq 0 -or $attempt -ge 6) { return $result }
        if ("$($result.Err)`n$($result.Out)" -notmatch $TransientNetworkPattern) { return $result }
        Write-Host "   ..  the connection to GitHub dropped; retrying ($attempt of 5)" -ForegroundColor DarkGray
        Start-Sleep -Seconds ([int][Math]::Min(30, [Math]::Pow(2, $attempt)))
    }
}

function Confirm-Yes([string] $Question) {
    $answer = Read-Host "$Question [y/N]"
    return ($answer -match '^(y|yes)$')
}

function Read-Secret([string] $Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

foreach ($tool in @("git", "gh")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Stop-Publish "$tool is not installed or not on PATH." }
}
$python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { $python = (Get-Command python -ErrorAction Stop).Source }
if (-not $SigningKey) { $SigningKey = Join-Path $env:USERPROFILE ".tesseract-release\update-signing-key.pem" }
if (-not $DenyFile) { $DenyFile = Join-Path $env:USERPROFILE ".tesseract-release\public-denylist.txt" }
if (-not $SshSigningKey) { $SshSigningKey = Join-Path $env:USERPROFILE ".ssh\id_ed25519" }
$PublicDir = Join-Path (Split-Path -Parent $RepoRoot) "Tessract-public"

$appInfo = Invoke-Tool $python @("-B", "-c", "import app_info; print(app_info.DEFAULT_APP_VERSION); print(app_info.APP_REPOSITORY_URL)")
if ($appInfo.Code -ne 0) { Stop-Publish "Cannot read app_info.py: $($appInfo.Err)" }
$version, $repositoryUrl = $appInfo.Out -split "\r?\n"
if (-not $Repository) { $Repository = ($repositoryUrl -replace '^https://github\.com/', '').TrimEnd('/') }
if ($Repository -notmatch '^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$') { Stop-Publish "Repository '$Repository' is not owner/name." }
$owner = $Repository.Split("/")[0]
$tag = "v$version"
$remoteUrl = "https://github.com/$Repository.git"
$actions = New-Object System.Collections.Generic.List[string]

Write-Host "Tesseract $version  ->  https://github.com/$Repository  (public)" -ForegroundColor White
if ($DryRun) { Write-Host "Dry run: nothing on GitHub is changed and nothing is pushed." }

# ---------------------------------------------------------------- 1. checks
Write-Step "Source tree"
$dirty = Invoke-Tool "git" @("status", "--porcelain", "--untracked-files=no")
if ($dirty.Out) { Stop-Publish "Uncommitted changes in tracked files; commit them first:`n$($dirty.Out)" }
$untracked = Invoke-Tool "git" @("ls-files", "-o", "--exclude-standard")
if ($untracked.Out) {
    Write-Todo "These untracked files are not ignored, so they would be published:"
    $untracked.Out -split "\r?\n" | ForEach-Object { Write-Host "       $_" }
    if (-not (Confirm-Yes "Publish them too?")) { Stop-Publish "Commit, delete or ignore them, then run again." }
}
$head = (Invoke-Tool "git" @("rev-parse", "--short", "HEAD")).Out
Write-Ok "committed (HEAD $head, version $version)"
if (-not (Test-Path -LiteralPath $DenyFile -PathType Leaf)) { Stop-Publish "Missing private deny list $DenyFile (RELEASING.md, one-time setup 3)." }
Write-Ok "private deny list present"

Write-Step "GitHub login"
# gh can hold several accounts; only the active one's token is used. Ask the
# API who that is instead of parsing `gh auth status` (which lists them all).
function Get-GitHubUser {
    $who = Invoke-Tool "gh" @("api", "user", "--jq", '.login + " " + (.id|tostring)')
    if ($who.Code -eq 0 -and $who.Out) { return @($who.Out -split " ") }
    return $null
}
$current = Get-GitHubUser
if ($current -and $current[0] -ne $owner) {
    # The owner's account may already be stored next to the other one.
    $switched = Invoke-Tool "gh" @("auth", "switch", "-h", "github.com", "-u", $owner)
    if ($switched.Code -eq 0) { $current = Get-GitHubUser }
}
if (-not $current -or $current[0] -ne $owner) {
    if ($current) { Write-Todo "gh is logged in as $($current[0]), but the release goes to $owner." }
    else { Write-Todo "gh is not logged in (or the token expired)." }
    Write-Host "       The login page opens in your default browser, which must be signed in to GitHub as ${owner}."
    Write-Host "       If it shows another account, sign out there first (or open the link in a private window)."
    Read-Host "   Press Enter to log in as $owner" | Out-Null
    & gh auth login -h github.com -p https -w -s "workflow,admin:ssh_signing_key"
    if ($LASTEXITCODE -ne 0) { Stop-Publish "gh auth login failed." }
    $current = Get-GitHubUser
    if (-not $current -or $current[0] -ne $owner) {
        Stop-Publish "Still not logged in as $owner. Sign the browser in to $owner, then run again."
    }
}
$login, $userId = $current
# `workflow` is needed to push .github/workflows; the other to check/add the SSH signing key.
$headers = Invoke-Tool "gh" @("api", "-i", "user")
$scopeLine = ($headers.Out -split "\r?\n" | Where-Object { $_ -match '^X-Oauth-Scopes:' } | Select-Object -First 1)
$granted = @(($scopeLine -replace '^X-Oauth-Scopes:\s*', '') -split ',\s*' | Where-Object { $_ })
$missingScopes = @("workflow", "admin:ssh_signing_key") | Where-Object { $granted -notcontains $_ }
if ($missingScopes) {
    Write-Todo "Adding token scopes: $($missingScopes -join ', ')"
    & gh auth refresh -h github.com -s ($missingScopes -join ",")
    if ($LASTEXITCODE -ne 0) { Stop-Publish "gh auth refresh failed." }
}
& gh auth setup-git | Out-Null
Write-Ok "logged in as $login"

Write-Step "SSH signing key (commits and tag must show Verified)"
foreach ($path in @($SshSigningKey, "$SshSigningKey.pub")) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { Stop-Publish "Missing $path (RELEASING.md, one-time setup 2)." }
}
$sshPublic = ((Get-Content -LiteralPath "$SshSigningKey.pub" -Raw).Trim() -split '\s+')[0..1] -join " "
if (-not $sshPublic.StartsWith("ssh-ed25519 ")) { Stop-Publish "$SshSigningKey is not an Ed25519 key." }
$signingKeys = Invoke-Tool "gh" @("api", "user/ssh_signing_keys", "--paginate", "--jq", ".[].key")
if ($signingKeys.Code -ne 0) { Stop-Publish "Cannot list SSH signing keys: $($signingKeys.Err)" }
$addSshKey = -not (($signingKeys.Out -split "\r?\n") -contains $sshPublic)
if ($addSshKey) { Write-Todo "register $SshSigningKey.pub on GitHub as a signing key"; $actions.Add("Register the SSH public key as a GitHub signing key") }
else { Write-Ok "registered on GitHub" }

Write-Step "Repository"
$repoName = $Repository.Split("/")[1]
$repoState = Invoke-Tool "gh" @("api", "repos/$Repository", "--jq", '.full_name + " " + (.private|tostring)')
$createRepo = $false; $makePublic = $false; $archiveRepo = $null; $repoExists = $false; $tagOnGitHub = $false
if ($repoState.Code -ne 0) {
    if ($repoState.Err -notmatch "404|Not Found") { Stop-Publish "Cannot read $Repository`: $($repoState.Err)" }
}
else {
    $fullName, $isPrivate = $repoState.Out -split " "
    # After a rename GitHub redirects the old name to the renamed repository
    # until a new repository takes the name, so a redirect means the name is free.
    $repoExists = ($fullName -eq $Repository)
    if (-not $repoExists) { Write-Ok "the name is free ($Repository only redirects to $fullName)" }
}
if (-not $repoExists) {
    $createRepo = $true
    Write-Todo "create public repository $Repository"
    $actions.Add("Create the public repository $Repository")
}
else {
    $remoteHeads = Invoke-Tool "git" @("ls-remote", "--heads", "--tags", $remoteUrl)
    if ($remoteHeads.Code -ne 0) { Stop-Publish "Cannot read the branches and tags of $remoteUrl`: $($remoteHeads.Err)" }
    $isEmpty = -not $remoteHeads.Out
    if ($isPrivate -eq "true" -and -not $isEmpty) {
        # Never made public: older private history may carry personal identities.
        # Keep it as a private archive under a new name and start a fresh public one.
        $archiveRepo = "$repoName-private-$(Get-Date -Format yyyyMMdd)"
        $archiveState = Invoke-Tool "gh" @("api", "repos/$owner/$archiveRepo", "--jq", ".full_name")
        if ($archiveState.Code -eq 0 -and $archiveState.Out -eq "$owner/$archiveRepo") {
            Stop-Publish "$Repository is private with content and $owner/$archiveRepo exists too; rename one of them on github.com, then run again."
        }
        Write-Todo "$Repository is private and already has content, so it is never made public"
        Write-Host "       It is renamed to $owner/$archiveRepo (stays private, nothing is deleted),"
        Write-Host "       and a new public $Repository is created in its place."
        $createRepo = $true
        $actions.Add("Rename the private $Repository to $owner/$archiveRepo (kept private, nothing deleted)")
        $actions.Add("Create the public repository $Repository")
    }
    elseif ($isPrivate -eq "true") {
        $makePublic = $true
        Write-Todo "make the empty private repository public"
        $actions.Add("Make the empty repository $Repository public")
    }
    else { Write-Ok "public" }
    if (-not $archiveRepo) {
        if (-not $isEmpty -and -not (Test-Path -LiteralPath (Join-Path $PublicDir ".git"))) {
            Stop-Publish ("$Repository already has history, but the local public clone $PublicDir is missing. " +
                "Clone it there first (git clone $remoteUrl `"$PublicDir`"), then run again.")
        }
        if ($remoteHeads.Out -match [regex]::Escape("refs/tags/$tag")) { $tagOnGitHub = $true }
    }
}
# A remote of this private checkout that names the public repository would
# publish the private history with one `git push`; point it elsewhere.
$publicRemotePattern = '(?i)github\.com[/:]' + [regex]::Escape($Repository) + '(\.git)?/?$'
$hazardRemotes = @()
foreach ($remoteName in @((Invoke-Tool "git" @("remote")).Out -split "\r?\n" | Where-Object { $_ })) {
    $remoteTarget = (Invoke-Tool "git" @("remote", "get-url", $remoteName)).Out
    if ($remoteTarget -match $publicRemotePattern) { $hazardRemotes += $remoteName }
}
if ($hazardRemotes) {
    if ($archiveRepo) {
        Write-Todo "remote $($hazardRemotes -join ', ') of this checkout will follow the archive"
        $actions.Add("Point remote $($hazardRemotes -join ', ') of this private checkout at $owner/$archiveRepo")
    }
    else {
        Write-Todo "remote $($hazardRemotes -join ', ') of this checkout points at the public repository"
        $actions.Add("Remove remote $($hazardRemotes -join ', ') from this private checkout (it names the public repository)")
    }
}
# An earlier run that got this far is picked up where it stopped:
#   "watch"  - the tag is on GitHub: wait for (or re-run) its release workflow;
#   "verify" - the release is already published: only the live update check is left.
$resumeMode = $null
$moveTag = $false
# Releases that use the tag, drafts included (the list shows drafts to the owner).
function Get-TagReleaseCount {
    $listed = Invoke-Tool "gh" @("api", "repos/$Repository/releases", "--paginate",
        "--jq", "[.[] | select(.tag_name == `"$tag`")] | length")
    if ($listed.Code -ne 0) { return -1 }
    return (@($listed.Out -split "\r?\n" | Where-Object { $_ } | ForEach-Object { [int] $_ }) | Measure-Object -Sum).Sum
}
if ($tagOnGitHub) {
    $published = Invoke-Tool "gh" @("api", "repos/$Repository/releases/tags/$tag", "--jq", ".draft|tostring")
    if ($published.Code -eq 0 -and $published.Out -eq "false") {
        $resumeMode = "verify"
        Write-Ok "$tag is already published; only the live update check is left"
        if ($MoveUnreleasedTag) { Stop-Publish "$tag is published; -MoveUnreleasedTag never moves a released tag." }
    }
    elseif ($MoveUnreleasedTag) {
        $tagReleases = Get-TagReleaseCount
        if ($tagReleases -ne 0) {
            Stop-Publish "Cannot confirm that no release (or draft) uses $tag (found: $tagReleases); nothing is moved."
        }
        $moveTag = $true
        Write-Todo "$tag is on GitHub without any release; it is moved to a new commit of the current source"
    }
    else {
        $resumeMode = "watch"
        Write-Ok "$tag is already on GitHub; this run picks up its release workflow (pass -MoveUnreleasedTag to tag new source instead)"
    }
}
elseif (Test-Path -LiteralPath (Join-Path $PublicDir ".git")) {
    $localTag = Invoke-Tool "git" @("-C", $PublicDir, "rev-parse", "-q", "--verify", "refs/tags/$tag")
    if ($localTag.Code -eq 0) {
        Write-Todo "the signed tag $tag of an earlier run is not on GitHub yet; it is pushed if it still matches the public commit"
    }
    else { Write-Ok "tag $tag is unused" }
}
else { Write-Ok "tag $tag is unused" }

if ($resumeMode) {
    # Nothing is set up or pushed again; the update-signing key is not needed.
    $actions.Clear()
    if ($resumeMode -eq "watch") { $actions.Add("Wait for the release workflow of $tag (re-run failed jobs if you agree), then check the live update") }
    else { $actions.Add("Check the live update of the published $tag") }
}
else {
Write-Step "Update-signing key (the app installs only releases signed with it)"
$latestRelease = Invoke-Tool "gh" @("api", "repos/$Repository/releases/latest", "--jq", ".tag_name")
$hasPublishedRelease = ($latestRelease.Code -eq 0 -and $latestRelease.Out)
# Releases of a repository that is only a redirect or about to be archived
# leave with it; installed copies read the public repository only.
if (-not $repoExists -or $archiveRepo) { $hasPublishedRelease = $false }
$generateKey = $false
if (Test-Path -LiteralPath $SigningKey -PathType Leaf) {
    $password = Read-Secret "Password of $SigningKey"
    [Environment]::SetEnvironmentVariable($PasswordEnv, $password, "Process")
    $check = Invoke-Tool $python @("-B", "scripts\publish_release_helper.py", "check-key", $SigningKey)
    if ($check.Code -ne 0) { Stop-Publish "$($check.Out) $($check.Err)" }
    Write-Ok $check.Out
}
else {
    if ($hasPublishedRelease) {
        Stop-Publish ("No update-signing key at $SigningKey, and $($latestRelease.Out) is already published. Installed copies " +
            "trust only the old key: restore that key file (or pass -SigningKey) instead of making a new one.")
    }
    Write-Todo "no update-signing key at $SigningKey"
    Write-Host "       Nothing is published yet, so a new key can replace the one in app_info.py safely."
    $generateKey = $true
    $actions.Add("Generate a new update-signing key, put its public key into app_info.py and commit that")
}

if ($moveTag) { $actions.Add("Delete the unreleased tag $tag on GitHub and in $PublicDir (no release uses it)") }
$actions.Add("Set up the 'release' environment (tags v* only$(if ($RequireApproval) { ', your approval' })) and its 3 secrets")
$actions.Add("Audit, commit and sign the public tree in $PublicDir, tag $tag, push main and $tag")
}

# ---------------------------------------------------------------- 2. confirm
Write-Step "Plan"
$actions | ForEach-Object { Write-Host "   - $_" }
if ($DryRun -and $resumeMode) {
    Write-Host ""
    Write-Host "Dry run finished: an earlier run already pushed $tag. Run again without -DryRun to continue it." -ForegroundColor Green
    exit 0
}
if ($DryRun) {
    Write-Host ""
    Write-Host "Dry run: preparing and auditing the public commit locally (no tag, no push)..."
    if ($generateKey) { Write-Todo "the real run makes the signing key first; this preview still uses the old app_info.py" }
    & (Join-Path $PSScriptRoot "push_to_github.ps1") -NoPush -RemoteUrl $remoteUrl -GitHubLogin $login -GitHubId $userId `
        -SshSigningKey $SshSigningKey -DenyFile $DenyFile -PublicDir $PublicDir
    Write-Host ""
    Write-Host "Dry run finished: checks passed and the public tree passed the audit. Run again without -DryRun to publish." -ForegroundColor Green
    exit 0
}
if (-not $resumeMode) {
    Write-Host ""
    if (-not (Confirm-Yes "Publish Tesseract $version publicly at https://github.com/$Repository ?")) { Stop-Publish "Cancelled; nothing was changed." }
}

# ---------------------------------------------------------------- 3. set up and push
# (skipped when an earlier run already pushed the tag)
if (-not $resumeMode) {
if ($generateKey) {
    Write-Step "New update-signing key"
    Write-Host "   Choose a strong password. Keep the key file and the password backed up:"
    Write-Host "   without them no future release can update installed copies."
    $first = Read-Secret "New key password"
    $second = Read-Secret "Repeat the password"
    if (-not $first -or $first -ne $second) { Stop-Publish "The passwords are empty or differ." }
    $password = $first
    [Environment]::SetEnvironmentVariable($PasswordEnv, $password, "Process")
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SigningKey) | Out-Null
    $publicKeyFile = "$SigningKey.public.txt"
    $made = Invoke-Tool $python @("-B", "scripts\generate_release_signing_key.py", "--private-key", $SigningKey, "--public-key", $publicKeyFile, "--password-env", $PasswordEnv)
    if ($made.Code -ne 0) { Stop-Publish "Key generation failed: $($made.Out) $($made.Err)" }
    $installed = Invoke-Tool $python @("-B", "scripts\publish_release_helper.py", "install-public-key", $publicKeyFile)
    if ($installed.Code -ne 0) { Stop-Publish $installed.Out }
    $check = Invoke-Tool $python @("-B", "scripts\publish_release_helper.py", "check-key", $SigningKey)
    if ($check.Code -ne 0) { Stop-Publish "$($check.Out) $($check.Err)" }
    & git add -- app_info.py
    & git commit --quiet -m "Trust the update-signing key of the first public release" -- app_info.py
    if ($LASTEXITCODE -ne 0) { Stop-Publish "Committing app_info.py failed." }
    Write-Ok "$($check.Out) Key file: $SigningKey"
}

if ($addSshKey) {
    Write-Step "Register SSH signing key"
    $added = Invoke-Tool "gh" @("ssh-key", "add", "$SshSigningKey.pub", "--type", "signing", "--title", "Tesseract release signing")
    if ($added.Code -ne 0) {
        Stop-Publish ("gh ssh-key add failed: $($added.Err) If GitHub says the key is already in use, remove it from the other " +
            "account (Settings -> SSH and GPG keys) or pass -SshSigningKey with another Ed25519 key, then run again.")
    }
    Write-Ok "registered $SshSigningKey.pub as a signing key"
}

if ($archiveRepo) {
    Write-Step "Archive the old private repository"
    $renamed = Invoke-Tool "gh" @("repo", "rename", $archiveRepo, "--repo", $Repository, "--yes")
    if ($renamed.Code -ne 0) { Stop-Publish "Renaming $Repository to $archiveRepo failed: $($renamed.Err) Run again to retry." }
    Write-Ok "$Repository is now $owner/$archiveRepo (private)"
}
foreach ($remoteName in $hazardRemotes) {
    if ($archiveRepo) {
        & git remote set-url $remoteName "https://github.com/$owner/$archiveRepo.git"
        if ($LASTEXITCODE -ne 0) { Stop-Publish "Could not repoint remote $remoteName; fix it before publishing." }
        Write-Ok "remote $remoteName -> $owner/$archiveRepo"
    }
    else {
        & git remote remove $remoteName
        if ($LASTEXITCODE -ne 0) { Stop-Publish "Could not remove remote $remoteName; remove it before publishing." }
        Write-Ok "removed remote $remoteName"
    }
}

if ($createRepo) {
    Write-Step "Create repository"
    $created = Invoke-Tool "gh" @("repo", "create", $Repository, "--public", "--description", "Tesseract - a Windows proxy client")
    if ($created.Code -ne 0) { Stop-Publish "gh repo create failed: $($created.Err) Run again: an existing repository is detected." }
    Write-Ok "created https://github.com/$Repository (public)"
}
if ($makePublic) {
    $edited = Invoke-Tool "gh" @("repo", "edit", $Repository, "--visibility", "public", "--accept-visibility-change-consequences")
    if ($edited.Code -ne 0) { Stop-Publish "gh repo edit --visibility public failed: $($edited.Err)" }
    Write-Ok "$Repository is public"
}

Write-Step "Release environment and secrets"
$environment = @{
    deployment_branch_policy = @{ protected_branches = $false; custom_branch_policies = $true }
}
if ($RequireApproval) {
    $environment.reviewers = @(@{ type = "User"; id = [long] $userId })
    $environment.prevent_self_review = $false
}
$put = Invoke-Tool "gh" @("api", "-X", "PUT", "repos/$Repository/environments/release", "--input", "-") ($environment | ConvertTo-Json -Depth 5 -Compress)
if ($put.Code -ne 0) { Stop-Publish "Creating the release environment failed: $($put.Err)" }
$policies = Invoke-Tool "gh" @("api", "repos/$Repository/environments/release/deployment-branch-policies", "--jq", '.branch_policies[] | .type + ":" + .name')
if (-not (($policies.Out -split "\r?\n") -contains "tag:v*")) {
    $policy = Invoke-Tool "gh" @("api", "-X", "POST", "repos/$Repository/environments/release/deployment-branch-policies", "--input", "-") '{"name":"v*","type":"tag"}'
    if ($policy.Code -ne 0) {
        # A dropped connection may hide a request that did go through: look again.
        $policies = Invoke-Tool "gh" @("api", "repos/$Repository/environments/release/deployment-branch-policies", "--jq", '.branch_policies[] | .type + ":" + .name')
        if (-not (($policies.Out -split "\r?\n") -contains "tag:v*")) {
            Stop-Publish "Restricting the release environment to v* tags failed: $($policy.Err)"
        }
    }
}
Write-Ok "environment 'release' (tags v* only$(if ($RequireApproval) { ', needs your approval' }))"
$secrets = [ordered]@{
    TESSERACT_UPDATE_PRIVATE_KEY_B64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($SigningKey))
    TESSERACT_UPDATE_KEY_PASSWORD    = $password
    PUBLIC_RELEASE_PRIVATE_DENYLIST  = [IO.File]::ReadAllText($DenyFile)
}
foreach ($name in $secrets.Keys) {
    $set = Invoke-Tool "gh" @("secret", "set", $name, "--env", "release", "--repo", $Repository) $secrets[$name]
    if ($set.Code -ne 0) { Stop-Publish "Setting secret $name failed: $($set.Err)" }
    Write-Ok "secret $name"
}
$secrets = $null

if ($moveTag) {
    Write-Step "Move the unreleased tag $tag"
    # Look again right before deleting: still no release or draft uses the tag.
    if ((Get-TagReleaseCount) -ne 0) { Stop-Publish "A release (or draft) for $tag appeared meanwhile; nothing is deleted." }
    $deleted = Invoke-Tool "git" @("-C", $PublicDir, "push", $remoteUrl, ":refs/tags/$tag")
    $remoteTagsNow = Invoke-Tool "git" @("ls-remote", "--tags", $remoteUrl)
    if ($remoteTagsNow.Code -ne 0) { Stop-Publish "Cannot read the tags of $remoteUrl`: $($remoteTagsNow.Err)" }
    # A dropped connection can hide a delete that went through: the tag list decides.
    if ($remoteTagsNow.Out -match ("\srefs/tags/" + [regex]::Escape($tag) + "(\^\{\})?(\r?\n|$)")) {
        Stop-Publish "Deleting $tag on GitHub failed: $($deleted.Err)"
    }
    $localTagNow = Invoke-Tool "git" @("-C", $PublicDir, "rev-parse", "-q", "--verify", "refs/tags/$tag")
    if ($localTagNow.Code -eq 0) {
        $removedLocal = Invoke-Tool "git" @("-C", $PublicDir, "tag", "-d", $tag)
        if ($removedLocal.Code -ne 0) { Stop-Publish "Deleting the local tag $tag failed: $($removedLocal.Err)" }
    }
    Write-Ok "$tag removed on GitHub and in $PublicDir; the new public commit gets it"
}

Write-Step "Publish source and tag"
& (Join-Path $PSScriptRoot "push_to_github.ps1") -Release -RemoteUrl $remoteUrl -GitHubLogin $login -GitHubId $userId `
    -SshSigningKey $SshSigningKey -DenyFile $DenyFile -PublicDir $PublicDir
}

# ---------------------------------------------------------------- 4. watch and verify
[Environment]::SetEnvironmentVariable($PasswordEnv, $null, "Process")
$password = $null
if ($NoWatch -and $resumeMode -ne "verify") {
    Write-Host "Pushed. Follow the build at https://github.com/$Repository/actions (run this script again to pick it up)." -ForegroundColor Green
    exit 0
}

# Polls the run instead of `gh run watch`, which ends with a failure code when
# the connection drops and would make a running build look failed.
function Wait-ReleaseRun([string] $RunId) {
    $lastStatus = ""; $lastNote = Get-Date; $misses = 0
    while ($true) {
        $view = Invoke-Tool "gh" @("run", "view", $RunId, "--repo", $Repository, "--json", "status,conclusion",
            "--jq", '.status + " " + .conclusion')
        if ($view.Code -ne 0) {
            $misses++
            if ($misses -ge 20) { return "unknown" }
        }
        else {
            $misses = 0
            $parts = @($view.Out -split " ")
            $status = $parts[0]
            if ($status -eq "completed") { if ($parts.Count -gt 1) { return $parts[1] } else { return "" } }
            if ($status -ne $lastStatus -or ((Get-Date) - $lastNote).TotalMinutes -ge 10) {
                Write-Host ("   {0}  {1}" -f (Get-Date -Format "HH:mm"), $status)
                $lastStatus = $status; $lastNote = Get-Date
            }
        }
        Start-Sleep -Seconds 60
    }
}

if ($resumeMode -ne "verify") {
    Write-Step "Release workflow"
    # Follow the run of the commit the tag names now: a tag of the same name
    # that was pushed before may have left a failed run behind.
    $remoteTags = Invoke-Tool "git" @("ls-remote", "--tags", $remoteUrl)
    $tagLine = @($remoteTags.Out -split "\r?\n" | Where-Object { $_ -match ("\srefs/tags/" + [regex]::Escape($tag) + "\^\{\}$") }) | Select-Object -First 1
    $tagSha = if ($tagLine) { ($tagLine -split "\s+")[0] } else { $null }
    if (-not $tagSha) { Stop-Publish "Cannot read $tag on GitHub: $($remoteTags.Err)" }
    $runId = $null
    for ($attempt = 0; $attempt -lt 36 -and -not $runId; $attempt++) {
        if ($attempt -gt 0) { Start-Sleep -Seconds 5 }
        $runs = Invoke-Tool "gh" @("run", "list", "--repo", $Repository, "--workflow", "release-windows.yml", "--limit", "20",
            "--json", "databaseId,headBranch,headSha",
            "--jq", ".[] | select(.headBranch == `"$tag`" and .headSha == `"$tagSha`") | .databaseId")
        if ($runs.Code -eq 0 -and $runs.Out) { $runId = ($runs.Out -split "\r?\n")[0] }
    }
    if (-not $runId) { Stop-Publish "No release workflow run for $tag appeared; see https://github.com/$Repository/actions and run this script again." }
    Write-Host "   https://github.com/$Repository/actions/runs/$runId  (about 40-60 minutes; this window can stay open)"
    if ($RequireApproval) { Write-Todo "approve the 'release' deployment on that page when the build finishes" }
    $conclusion = Wait-ReleaseRun $runId
    for ($rerun = 1; $conclusion -ne "success" -and $conclusion -ne "unknown" -and $rerun -le 2; $rerun++) {
        # A few tests drive a real Mihomo and can miss a timing window on a busy
        # runner; re-running only the failed jobs is the normal remedy. Nothing is
        # published until the last job succeeds.
        Write-Todo "the release workflow ended with '$conclusion': https://github.com/$Repository/actions/runs/$runId"
        if (-not (Confirm-Yes "Re-run the failed jobs (attempt $rerun of 2)?")) { break }
        $again = Invoke-Tool "gh" @("run", "rerun", $runId, "--repo", $Repository, "--failed")
        if ($again.Code -ne 0) { Stop-Publish "gh run rerun failed: $($again.Err)" }
        Start-Sleep -Seconds 20
        $conclusion = Wait-ReleaseRun $runId
    }
    if ($conclusion -eq "unknown") {
        Stop-Publish ("GitHub could not be reached for a long time; the build keeps running there " +
            "(https://github.com/$Repository/actions/runs/$runId). Run this script again later: it picks the run up.")
    }
    if ($conclusion -ne "success") {
        Stop-Publish "The release workflow ended with '$conclusion'; open the run above. Nothing is published until it succeeds."
    }
}

Write-Step "In-app update check against the live release"
$verified = Invoke-Tool $python @("-B", "scripts\publish_release_helper.py", "verify-live")
$verified.Out -split "\r?\n" | Where-Object { $_ } | ForEach-Object { Write-Host "   $_" }
if ($verified.Code -ne 0) { Stop-Publish "The release exists but the app could not verify it: $($verified.Out) $($verified.Err)" }
Write-Host ""
Write-Host "Published: https://github.com/$Repository/releases/tag/$tag" -ForegroundColor Green

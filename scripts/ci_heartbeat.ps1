[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Context,
    [Parameter(Mandatory = $true)] [string] $ProgressFile,
    [int] $IntervalSeconds = 15
)

# Release workflow only. A GitHub-hosted runner that stops communicating is
# declared lost 45 minutes later and keeps no log at all (actions/runner#4632).
# This posts a commit status every $IntervalSeconds with the step's age and what
# it is doing (the build stage or test named in $ProgressFile), so a silent
# runner shows within a minute and its last status names where it was. The step
# writes "$ProgressFile.done" (success or failure) to end it. Nothing here can
# change the step's own result: every failure to post is ignored.

$ErrorActionPreference = "Continue"
$repository = $env:GITHUB_REPOSITORY
$commit = $env:GITHUB_SHA
if (-not $repository -or -not $commit -or -not $env:GH_TOKEN) { exit 0 }
$doneFile = "$ProgressFile.done"
$started = Get-Date

function Send-Status([string] $State, [string] $Text) {
    $text = ($Text -replace '\s+', ' ').Trim()
    if ($text.Length -gt 140) { $text = $text.Substring(0, 137) + "..." }
    try {
        & gh api --method POST "repos/$repository/statuses/$commit" -f "state=$State" -f "context=$Context" -f "description=$text" *> $null
    }
    catch { }
}

function Read-Text([string] $Path) {
    try { if (Test-Path -LiteralPath $Path) { return [System.IO.File]::ReadAllText($Path).Trim() } } catch { }
    return ""
}

# Machine state for each status: memory in use, process count, TCP connections and
# how many of them are TIME_WAIT, so the last status before a silent runner shows it.
function Read-Machine {
    $parts = @()
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $parts += "m{0}%" -f [int] (100 - 100 * $os.FreePhysicalMemory / $os.TotalVisibleMemorySize)
    } catch { }
    try { $parts += "p{0}" -f @(Get-Process -ErrorAction Stop).Count } catch { }
    try {
        $tcp = @(Get-NetTCPConnection -ErrorAction Stop)
        $parts += "t{0}w{1}" -f $tcp.Count, @($tcp | Where-Object { $_.State -eq "TimeWait" }).Count
    } catch { }
    return ($parts -join " ")
}

while ($true) {
    $minutes = [int] [Math]::Floor(((Get-Date) - $started).TotalMinutes)
    if (Test-Path -LiteralPath $doneFile) {
        $result = Read-Text $doneFile
        $state = if ($result -eq "success") { "success" } else { "failure" }
        Send-Status $state "after $minutes min: $result"
        exit 0
    }
    $doing = Read-Text $ProgressFile
    if (-not $doing) { $doing = "starting" }
    Send-Status "pending" "$minutes min $(Read-Machine): $doing"
    for ($second = 0; $second -lt $IntervalSeconds -and -not (Test-Path -LiteralPath $doneFile); $second++) {
        Start-Sleep -Seconds 1
    }
}

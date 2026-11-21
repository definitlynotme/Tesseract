[CmdletBinding()]
param(
    [int] $StepProcessId = 0,
    [string] $SummaryFile = ""
)

# Release workflow only: test-windows runs this just before its tests. The first
# attempts of the 0.1.2, 0.1.3 and 0.1.4 test jobs lost their runner a few minutes into
# pytest, and a lost runner keeps no log (scripts/ci_heartbeat.ps1). Up to 0.1.4 the
# core-runner tests handed made-up PIDs to a KILL_ON_JOB_CLOSE Windows job, which kills
# whatever real process holds such a PID. Windows ignores a PID's low two bits, so
# 101-102 reached 100, and 1000-1017 reached 1000, 1004, 1008, 1012 and 1016.
#
# This records who holds those PIDs on a fresh runner, where the runner agent and the
# test step run, and the step's privileges, so the next lost attempt can be attributed.
# The report goes to the log. One summary line goes to $SummaryFile, which the heartbeat
# posts as a commit status, because the log of a lost runner is gone.
#
# The step starts this as a process of its own: .NET's process APIs (Get-Process)
# enable SeDebugPrivilege in the calling process's token where the token holds it, and
# the tests must not inherit a privilege the probe turned on. Nothing here fails the
# step: every error is reported and ignored, and the exit code is always 0.

$ErrorActionPreference = "Continue"
$probePids = @(100, 1000, 1004, 1008, 1012, 1016)

function Get-Holder([int] $Id = 0, [string] $Name = "") {
    # -IncludeUserName needs an elevated token; without one, the processes alone.
    $arguments = @{ ErrorAction = "Stop" }
    if ($Name) { $arguments.Name = $Name } else { $arguments.Id = $Id }
    try { return @(Get-Process @arguments -IncludeUserName) } catch { }
    try { return @(Get-Process @arguments) } catch { }
    return @()
}

function Get-ShortName([string] $Name) {
    # One word of at most 14 characters ("Memory Compression" has a space).
    $Name = $Name -replace '\s', '_'
    if ($Name.Length -le 14) { return $Name }
    return $Name.Substring(0, 14)
}

try {
    # 1. The privileges first, before this process uses .NET's process APIs: whoami.exe
    #    gets a copy of this process's token, which is the step's token as inherited.
    $debug = "unknown"
    "== whoami /priv (the test step's token, as this process inherited it)"
    try {
        $lines = @(& whoami.exe /priv 2>&1 | ForEach-Object { [string] $_ })
        $lines | ForEach-Object { "  $_" }
        $line = $lines | Where-Object { $_ -match '^\s*SeDebugPrivilege\s' } | Select-Object -First 1
        if ($line) { $debug = ($line.Trim() -split '\s+')[-1] } else { $debug = "absent" }
    }
    catch { "  whoami /priv failed: $_" }

    # 2. One table of every process, for parents and command lines (WMI, no .NET process API).
    $table = @{}
    try {
        foreach ($item in @(Get-CimInstance Win32_Process -OperationTimeoutSec 60 -ErrorAction Stop)) {
            $table[[int] $item.ProcessId] = $item
        }
    }
    catch { "  The process table is unavailable: $_" }

    "== Who holds the PIDs the core-runner tests used to put in a kill-on-close job"
    $holders = @()
    foreach ($id in $probePids) {
        $process = @(Get-Holder -Id $id) | Select-Object -First 1
        if ($null -eq $process) {
            "  {0,5}  none" -f $id
            $holders += "$id=-"
            continue
        }
        $item = $table[$id]
        "  {0,5}  {1}  session {2}  user {3}  parent {4}  {5}" -f $id, $process.ProcessName,
            $process.SessionId, $process.UserName, $item.ParentProcessId, $item.CommandLine
        $holders += "$id=$(Get-ShortName $process.ProcessName)"
    }

    "== The runner agent and the test step"
    $agent = @()
    foreach ($entry in @(@("Runner.Listener", "L"), @("Runner.Worker", "W"))) {
        $found = @(Get-Holder -Name $entry[0])
        if (-not $found.Count) {
            "  {0}: none" -f $entry[0]
            $agent += "$($entry[1])=-"
            continue
        }
        foreach ($process in $found) {
            "  {0}: pid {1}  session {2}  user {3}" -f $entry[0], $process.Id, $process.SessionId, $process.UserName
        }
        $agent += "$($entry[1])=" + (($found | ForEach-Object { "$($_.Id)/s$($_.SessionId)" }) -join ",")
    }
    $step = $null
    if ($StepProcessId -gt 0) { $step = @(Get-Holder -Id $StepProcessId) | Select-Object -First 1 }
    $stepSession = "?"
    if ($null -ne $step) {
        $stepSession = $step.SessionId
        "  Test step: {0} pid {1}  session {2}  user {3}" -f $step.ProcessName, $step.Id, $step.SessionId, $step.UserName
    }
    else { "  Test step: pid $StepProcessId not found" }
    $chain = @()
    $cursor = $StepProcessId
    for ($depth = 0; $depth -lt 10 -and $table.ContainsKey($cursor); $depth++) {
        $item = $table[$cursor]
        $chain += "{0} {1} (session {2})" -f $cursor, $item.Name, $item.SessionId
        $parent = [int] $item.ParentProcessId
        if ($parent -le 0 -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
    "  Test step ancestry: " + ($chain -join " <- ")

    $summary = "debug=$debug $($agent -join ' ') step=$StepProcessId/s$stepSession | $($holders -join ' ')"
    "== Summary, posted by the heartbeat as a commit status: $summary"
    if ($SummaryFile) {
        try { [System.IO.File]::WriteAllText($SummaryFile, $summary) }
        catch { "  Could not write ${SummaryFile}: $_" }
    }
}
catch { "Runner probe failed: $_" }
exit 0

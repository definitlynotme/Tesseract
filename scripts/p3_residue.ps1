# T-P3-H2 (C3-8): dot-sourced by the p3 smokes (shell, tray, exit; host and notify for the waits). When this scope's WebView2 is
# still alive after the 20 s grace (decision P3-06 item 6), Save-ResidueTree writes residue-tree.json:
# the process tree of what is left (pid, parent, WebView2 --type, creation time, whether the parent is
# the shell or still alive), the shell's own exit facts from shell.log, and - after watching up to
# $ObserveSeconds more, never killing - when each one went away. It returns the class:
#   shell_residue       the shell itself is still running, or it exited without finishing its WebView
#                       shutdown (no webview_browser_exit_wait line in shell.log): the shell's leftover;
#   webview2_slow_exit  the shell finished (exit wait logged, exited) and every leftover WebView2 went
#                       away by itself during the observation: the WebView2 runtime's own slow exit;
#   webview2_stuck      as above, but still alive after the observation.
# process_watch.py classifies the kernel job the same way (residue_class in process-inventory.json),
# including non-WebView2 members the script cannot see. The smoke still fails either way.
function Save-ResidueTree {
    param($Left, $Shell, [string] $ShellLog, [string] $OutDir, [int] $ObserveSeconds = 20)
    $Now = [DateTime]::UtcNow
    $ShellFacts = [ordered]@{ pid = $null; exited = $false; exit_code = $null; exit_time = $null }
    $From = $Now
    if ($Shell) {
        try {
            $Shell.Refresh(); $ShellFacts.pid = $Shell.Id; $ShellFacts.exited = $Shell.HasExited
            if ($Shell.HasExited) { $ShellFacts.exit_code = $Shell.ExitCode; $From = $Shell.ExitTime.ToUniversalTime(); $ShellFacts.exit_time = $From.ToString("o") }
        } catch { }
    }
    $Lines = @(); if ($ShellLog -and (Test-Path -LiteralPath $ShellLog)) { $Lines = @(Get-Content -LiteralPath $ShellLog) }
    $ExitWait = ($Lines | Where-Object { $_ -match ' webview_browser_exit_wait ' } | Select-Object -Last 1)
    $ShellExit = ($Lines | Where-Object { $_ -match ' shell_exit_\d+$' } | Select-Object -Last 1)
    $ShellFacts.exit_wait_line = if ($ExitWait) { ([string]$ExitWait) -replace '^\d+ ','' } else { $null }
    $ShellFacts.shell_exit_line = if ($ShellExit) { ([string]$ShellExit) -replace '^\d+ ','' } else { $null }
    $All = @{}; foreach ($P in @(Get-CimInstance Win32_Process)) { $All[[int]$P.ProcessId] = $P }
    $Nodes = @(foreach ($P in @($Left)) {
        $Line = [string]$P.CommandLine
        $Type = [regex]::Match($Line, '--type=(\S+)')
        $Parent = $All[[int]$P.ParentProcessId]
        # A parent PID counts only when that process is older than the child (not a reused PID).
        $ParentAlive = [bool]($Parent -and $Parent.CreationDate -le $P.CreationDate)
        [ordered]@{ pid = [int]$P.ProcessId; parent_pid = [int]$P.ParentProcessId
            webview_type = if ($Type.Success) { $Type.Groups[1].Value.Trim('"') } else { "browser" }
            created = $P.CreationDate.ToUniversalTime().ToString("o")
            parent_is_shell = ($ShellFacts.pid -and [int]$P.ParentProcessId -eq [int]$ShellFacts.pid)
            parent_alive = $ParentAlive; parent_name = if ($ParentAlive) { [string]$Parent.Name } else { $null }
            exit_after_shell_s = $null }
    })
    # Watch (never kill) until every leftover is gone or the observation ends.
    $End = [DateTime]::UtcNow.AddSeconds($ObserveSeconds)
    do {
        $Pending = 0
        foreach ($Node in $Nodes) {
            if ($null -ne $Node.exit_after_shell_s) { continue }
            $P = Get-Process -Id $Node.pid -ErrorAction SilentlyContinue
            $Same = $false
            if ($P) { try { $Same = [Math]::Abs(($P.StartTime.ToUniversalTime() - [DateTime]::Parse($Node.created, $null, [Globalization.DateTimeStyles]::RoundtripKind)).TotalSeconds) -lt 1 } catch { $Same = $true } }
            if ($Same) { $Pending++ } else { $Node.exit_after_shell_s = [Math]::Round(([DateTime]::UtcNow - $From).TotalSeconds, 1) }
        }
        if ($Pending -eq 0) { break }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $End)
    $ShellDone = $ShellFacts.exited -and $ShellFacts.exit_wait_line
    $Class = if (-not $ShellDone) { "shell_residue" } elseif ($Pending -eq 0) { "webview2_slow_exit" } else { "webview2_stuck" }
    $Result = [ordered]@{ residue_class = $Class; detected_after_shell_s = [Math]::Round(($Now - $From).TotalSeconds, 1)
        observe_s = $ObserveSeconds; still_alive_after_observe = $Pending; shell = $ShellFacts; processes = $Nodes }
    if ($OutDir) {
        New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
        [IO.File]::WriteAllText((Join-Path $OutDir "residue-tree.json"), ($Result | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
    }
    return $Class
}
# T-P3-H2 fix 1: a failed process_watch.py audit (residue 10 s after the root exit for other
# processes, 20 s for this scope's WebView2) keeps watching the residue for RESIDUE_OBSERVE (20 s)
# more before it exits, so the callers wait 30 s + the observation + a margin for it.
$InventoryWaitMs = 30000 + 1000 * 20 + 5000
# The message when the audit still has not exited: a residue the audit already reported (its result
# is written at once, residue_class "observing" until the observation ends) is named as such.
function Inventory-TimeoutMessage([string] $Inventory, [string] $Suffix = "") {
    try {
        if ($Inventory -and (Test-Path -LiteralPath $Inventory)) {
            $Found = Get-Content -LiteralPath $Inventory -Raw | ConvertFrom-Json
            if ($Found.residue_class) { return "owned process residual detected (residue_class=$($Found.residue_class), audit still running)$Suffix" }
        }
    } catch { }
    return "process inventory timeout$Suffix"
}
# T-P3-H2 fix 3 (decision wave 6, T-P3-H2 item 5): the "late shell". A launch that gave up waiting
# for root.json ("suspended job launch" timeout on a loaded machine) leaves its watcher running,
# and the watcher may still start the shell afterwards - which then sat on the user's desktop in its
# native pre-hello box (reviewA1 exit, reviewA3 single_instance). Every smoke's cleanup that never
# learned its shell asks Resolve-LateShell (root.json, pid + creation time, never a reused pid) and
# ends such a shell with Close-LateShell: close its native boxes, ask the tray to quit (or close the
# main window), and only then stop the scoped test shell itself. late-shell.json records the steps.
if (-not ("P3Late" -as [type])) {
Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class P3Late {
    delegate bool EnumProc(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc f, IntPtr l);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr h, uint m, IntPtr w, IntPtr l);
    static string ClassOf(IntPtr h) { var s = new StringBuilder(256); GetClassNameW(h, s, 256); return s.ToString(); }
    public static IntPtr Find(string cls, uint pid) {
        IntPtr found = IntPtr.Zero;
        EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p); if (p == pid && ClassOf(h) == cls) { found = h; return false; } return true; }, IntPtr.Zero);
        return found;
    }
    // WM_CLOSE every visible native box (#32770) of pid; returns how many were asked.
    public static int CloseDialogs(uint pid) {
        int n = 0;
        EnumWindows((h, l) => { uint p; GetWindowThreadProcessId(h, out p); if (p == pid && IsWindowVisible(h) && ClassOf(h) == "#32770" && PostMessageW(h, 0x10, IntPtr.Zero, IntPtr.Zero)) n++; return true; }, IntPtr.Zero);
        return n;
    }
}
"@
}
function Resolve-LateShell([string] $RootFile, $Watcher, [int] $Seconds = 30) {
    if ($Watcher) {
        $Until = [DateTime]::UtcNow.AddSeconds($Seconds)
        while (-not (Test-Path -LiteralPath $RootFile) -and -not $Watcher.HasExited -and [DateTime]::UtcNow -lt $Until) { Start-Sleep -Milliseconds 200 }
    }
    if (-not (Test-Path -LiteralPath $RootFile)) { return $null }
    try {
        $Root = Get-Content -LiteralPath $RootFile -Raw | ConvertFrom-Json
        $Late = Get-Process -Id ([int]$Root.pid) -ErrorAction Stop
        if ([Math]::Abs($Late.StartTime.ToFileTimeUtc() - [long]$Root.created_filetime) -lt 10000000) { $null = $Late.Handle; return $Late }
    } catch { }
    return $null
}
function Close-LateShell($Shell, [string] $OutDir) {
    $Steps = @("late_root_pid=$($Shell.Id)")
    for ($I = 0; $I -lt 3 -and -not $Shell.HasExited; $I++) {
        $Closed = [P3Late]::CloseDialogs([uint32]$Shell.Id)
        if ($Closed -eq 0) { break }
        $Steps += "dialog_wm_close=$Closed"; Start-Sleep -Milliseconds 500
    }
    if (-not $Shell.HasExited) {
        $Tray = [P3Late]::Find("TesseractShellTray", [uint32]$Shell.Id)
        if ($Tray -ne [IntPtr]::Zero) { [void][P3Late]::PostMessageW($Tray, 0x111, [IntPtr]2, [IntPtr]::Zero); $Steps += "tray_quit" }
        elseif ($Shell.CloseMainWindow()) { $Steps += "close_main_window" }
    }
    if (-not $Shell.WaitForExit(30000)) {
        # A box may have opened meanwhile (e.g. the hello timeout's); close it once more first.
        $Closed = [P3Late]::CloseDialogs([uint32]$Shell.Id)
        if ($Closed) { $Steps += "dialog_wm_close_late=$Closed" }
        if (-not $Shell.WaitForExit(5000)) { Stop-Process -Id $Shell.Id -Force -ErrorAction SilentlyContinue; $Steps += "stop_scoped_shell"; [void]$Shell.WaitForExit(5000) }
    }
    $Steps += "exited=" + $Shell.HasExited
    if ($OutDir) {
        try {
            New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
            [IO.File]::WriteAllText((Join-Path $OutDir "late-shell.json"), ([ordered]@{ pid = $Shell.Id; steps = $Steps } | ConvertTo-Json -Depth 3), (New-Object Text.UTF8Encoding $false))
        } catch { }
    }
    return ,$Steps
}

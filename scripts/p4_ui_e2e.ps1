[CmdletBinding()]
# T-P4-11 end-to-end flows of the P4 page in the debug Tauri shell (P4 architecture section 12).
#
#   ./scripts/p4_ui_e2e.ps1                                    # stand-in sidecar, every flow
#   ./scripts/p4_ui_e2e.ps1 -RealSidecar -Flows connect,commands,language
#   ./scripts/p4_ui_e2e.ps1 -SkipBuild -Flows notify,quit
#
# Build (unless -SkipBuild): scripts/shell_copy_ui.ps1 (the current ui/ app bundle into
# shell/app/ui), then `cargo build --offline` of the debug shell with the job count and priority of
# the work-order CPU convention (evidence/CPU_*.txt: cargo_jobs, cargo_priority; mode=timing forces
# -j 2 and BelowNormal; no convention file: -j 1); start and end UTC go to
# evidence/T-P4-11/cpu-windows.txt.
#
# Every flow gets its own sandbox under %TEMP% and its own custom scope: TESSERACT_INSTANCE_SCOPE,
# TESSERACT_CONFIG_DIR, TESSERACT_DATA_DIR and TEMP/TMP all point into the sandbox (Assert-Scope
# refuses to launch when one is missing, empty, the default scope or outside the sandbox, or when a
# directory is the live %APPDATA%\Tesseract / %LOCALAPPDATA%\Tesseract). The default scope's mutex
# and event names are never used. Each launch of a flow (ui/test/e2e/run.mjs --list) starts the
# debug shell under shell/app/process_watch.py with TESSERACT_SHELL_CDP_PORT (a free loopback port)
# and then runs ui/test/e2e/run.mjs --flow <name> --launch <n> (playwright-core connectOverCDP).
# After each launch: tray Quit (WM_COMMAND 2 to TesseractShellTray; the page answers
# host:confirmQuit itself), shell exit code 0, this scope's WebView2 gone within 20 s of the shell's
# exit (decision P3-06 item 6), process_watch inventory with no active process (shell, sidecar and
# every descendant), the stand-in sidecar's own pids gone. A failure is cleaned up in the order of
# p3_exit_smoke.ps1 (close this shell's native boxes, resume a suspended sidecar - this script never
# suspends one -, tray Quit, and only then stop this scope's test shell); every step goes into the
# flow's result.json.
#
# Sidecars:
#   default        --dev-sidecar-script shell/app/src/p4_ui_fixture.py (scripted stand-in; per-launch
#                  options in <data>/p4-ui-fixture/options.json)
#   -RealSidecar   the source sidecar (runtime_main.pyw from .venv) on the T-P2-33 hardened sandbox:
#                  tests/test_bridge_p2_33.py prepare (every listener on a free high port, Tier3,
#                  sources, subscriptions, system proxy, kill switch, alerts, GeoData updates, DNS
#                  and the automatic exit lookup off; sources.yaml with its migration markers;
#                  APPDATA / LOCALAPPDATA inside the sandbox), no HTTP(S)/ALL proxy variables in the
#                  launch environment (APPDATA / LOCALAPPDATA are redirected into the sandbox in
#                  both modes, defence in depth), the HKCU Internet Settings proxy values and the Tesseract /
#                  ProxyManager-KillSwitch firewall rule names read before and after (any difference
#                  fails), and the sandbox read back after the run (readback_problems must be empty).
#                  Only the flows that do not take over the network run (run.mjs --list --real).
#
# Desktop side effects (run it while the desktop is idle): the notify and navigate flows show real
# Windows notification balloons, and notify brings the test window to the foreground
# (native.py: AttachThreadInput, SetForegroundWindow, SwitchToThisWindow) - keys typed meanwhile
# may land in the test window. No key is ever injected into another window.
#
# Every launch's node run.mjs has an overall limit (-FlowTimeoutSec, default 300): past it the node
# process tree is ended and the launch goes to the failure cleanup. A failed launch also records,
# after the cleanup, the process_watch inventory (passed / active_processes) and the count of this
# scope's WebView2 processes; any residue is flagged in result.json (residue_after_cleanup).
#
# Evidence: .build/<work orders>/evidence/T-P4-11/<scope>/ per flow (result.json, launch-<n>.json,
# screenshots, shell.log copies, process inventories; never raw frames) and
# evidence/T-P4-11/run-<stamp>-<mode>.json for the whole run. A flow that passed, with every
# evidence copy written, has its sandbox removed afterwards (Remove-OwnSandbox: only a directory
# New-Sandbox made in this run, directly under the %TEMP% read at start, named
# tesseract-p4e2e-<flow>-<12 hex>; anything else is refused); a failed flow keeps it, and
# -KeepSandbox keeps every sandbox. This run's bytecode cache directory goes the same way at the
# end. An error while copying evidence (e.g. a full disk) fails that launch / flow (evidence_error)
# and the run goes on with the next flow; the run summary is always written.
# ASCII only (Windows PowerShell 5.1 reads scripts without a BOM as ANSI).
param([switch] $SkipBuild, [switch] $RealSidecar, [string[]] $Flows = @(), [string] $Label = "", [int] $FlowTimeoutSec = 300, [switch] $KeepSandbox)
$ErrorActionPreference = "Stop"
$Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
# (%TEMP% as it is before any flow redirects TEMP into a sandbox: sandboxes live directly under it)
$TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
# (the sandboxes this run made: Remove-OwnSandbox removes nothing else)
$OwnBoxes = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
. (Join-Path $PSScriptRoot "p3_residue.ps1")
$Flows = @($Flows | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
$Python = Join-Path $Repo ".venv/Scripts/python.exe"
$Node = (Get-Command node -ErrorAction Stop).Source
$RunMjs = Join-Path $Repo "ui/test/e2e/run.mjs"
$ShellExe = Join-Path $Repo "shell/target/debug/tesseract-shell.exe"
$Fixture = Join-Path $Repo "shell/app/src/p4_ui_fixture.py"
$Utf8 = New-Object Text.UTF8Encoding $false
# The work-order directory name is not ASCII: find it by the ticket file.
$Orders = Get-ChildItem -LiteralPath (Join-Path $Repo ".build") -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "T-P4-11.md") } | Select-Object -First 1
if (-not $Orders) { throw "work-order directory not found" }
$EvidenceRoot = Join-Path $Orders.FullName "evidence/T-P4-11"
New-Item -ItemType Directory -Force -Path $EvidenceRoot | Out-Null
$Mode = if ($RealSidecar) { "real" } else { "fixture" }
$Stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
$RunTag = if ($Label) { $Label } else { $Stamp + "-" + $Mode }
# The live profile locations this script must never use (read before anything is redirected).
$LiveDirs = @()
foreach ($Base in @($env:APPDATA, $env:LOCALAPPDATA)) { if ($Base) { $LiveDirs += [IO.Path]::GetFullPath((Join-Path $Base "Tesseract")).TrimEnd('\') } }

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class P4E2e {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindowExW(IntPtr parent, IntPtr after, string cls, string title);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
    public static IntPtr Find(string cls, uint pid) {
        IntPtr after = IntPtr.Zero;
        while (true) {
            after = FindWindowExW(IntPtr.Zero, after, cls, null);
            if (after == IntPtr.Zero) return IntPtr.Zero;
            uint owner; GetWindowThreadProcessId(after, out owner);
            if (owner == pid) return after;
        }
    }
}
'@

function Write-Json([string] $Path, $Value) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12), $Utf8)
}
function Wait-Until([scriptblock] $Condition, [string] $What, [int] $Seconds = 15) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while (-not (& $Condition)) {
        if ([DateTime]::UtcNow -gt $Deadline) { throw "timeout: $What" }
        Start-Sleep -Milliseconds 100
    }
}
function Free-Port { $L = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0); $L.Start(); $P = $L.LocalEndpoint.Port; $L.Stop(); return $P }
function Under([string] $Path, [string] $Root) {
    $P = [IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'; $R = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    return $P.StartsWith($R, [StringComparison]::OrdinalIgnoreCase)
}

# --- build -----------------------------------------------------------------------------------
function Cpu-Convention {
    $Conv = @{ mode = "normal"; cargo_jobs = "1"; cargo_priority = "BelowNormal"; file = $null }
    $File = Get-ChildItem -LiteralPath (Join-Path $Orders.FullName "evidence") -Filter "CPU_*.txt" -File -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($File) {
        $Conv.file = $File.Name
        foreach ($Line in (Get-Content -LiteralPath $File.FullName -Encoding UTF8)) {
            $M = [regex]::Match($Line, '^\s*(mode|cargo_jobs|cargo_priority)\s*=\s*(\S+)')
            if ($M.Success) { $Conv[$M.Groups[1].Value] = $M.Groups[2].Value }
        }
    }
    if ($Conv.mode -eq "timing") { $Conv.cargo_jobs = "2"; $Conv.cargo_priority = "BelowNormal" }
    return $Conv
}
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot "shell_copy_ui.ps1")
    $Conv = Cpu-Convention
    $CpuLog = Join-Path $EvidenceRoot "cpu-windows.txt"
    [IO.File]::AppendAllText($CpuLog, ("T-P4-11 cargo build --offline -j {0} priority={1} mode={2} start {3}`r`n" -f $Conv.cargo_jobs, $Conv.cargo_priority, $Conv.mode, [DateTime]::UtcNow.ToString("o")), $Utf8)
    $Cargo = Start-Process -FilePath "cargo" -ArgumentList @("build","--offline","-j",$Conv.cargo_jobs) -WorkingDirectory (Join-Path $Repo "shell") -NoNewWindow -PassThru
    $null = $Cargo.Handle
    try { $Cargo.PriorityClass = [Diagnostics.ProcessPriorityClass]$Conv.cargo_priority } catch { }
    $Cargo.WaitForExit()
    [IO.File]::AppendAllText($CpuLog, ("T-P4-11 cargo build end {0} exit={1}`r`n" -f [DateTime]::UtcNow.ToString("o"), $Cargo.ExitCode), $Utf8)
    if ($Cargo.ExitCode -ne 0) { throw "cargo build failed (exit $($Cargo.ExitCode))" }
}
if (-not (Test-Path -LiteralPath $ShellExe)) { throw "debug shell missing: build it first (or drop -SkipBuild)" }

# --- the flow plan -----------------------------------------------------------------------------
$ListArgs = @($RunMjs, "--list"); if ($RealSidecar) { $ListArgs += "--real" }
$Plan = (& $Node @ListArgs) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "run.mjs --list failed" }
if (-not $Flows.Count) { $Flows = @($Plan | ForEach-Object { $_.name }) }
foreach ($F in $Flows) { if (-not @($Plan | Where-Object { $_.name -eq $F }).Count) { throw "unknown flow: $F" } }

# --- bytecode warm-up for the real sidecar (as p3_shell_smoke.ps1: never the source tree) --------
$PyCache = Join-Path $TempRoot ("tesseract-p4e2e-pycache-" + [Guid]::NewGuid().ToString("N"))
[void]$OwnBoxes.Add([IO.Path]::GetFullPath($PyCache).TrimEnd('\'))
New-Item -ItemType Directory -Force -Path $PyCache | Out-Null
if ($RealSidecar) {
    $SavedWarm = @{ PYTHONPYCACHEPREFIX = $env:PYTHONPYCACHEPREFIX; PYTHONDONTWRITEBYTECODE = $env:PYTHONDONTWRITEBYTECODE }
    $env:PYTHONPYCACHEPREFIX = $PyCache; Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
    Push-Location $Repo; try { & $Python -c "import bridge_main, desktop_runtime" } finally { Pop-Location }
    foreach ($Key in @($SavedWarm.Keys)) { [Environment]::SetEnvironmentVariable($Key,$SavedWarm[$Key],"Process") }
}

# --- host facts (real sidecar): HKCU proxy values and firewall rule names, read only ---------------
function Host-Facts {
    $Facts = [ordered]@{}
    $Values = Get-ItemProperty -LiteralPath "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    foreach ($Name in @("ProxyEnable","ProxyServer","ProxyOverride","AutoConfigURL")) {
        $P = $Values.PSObject.Properties[$Name]; $Facts[$Name] = if ($null -eq $P) { $null } else { [string]$P.Value }
    }
    try {
        $Rules = @(Get-NetFirewallRule -ErrorAction Stop | Where-Object { $_.Name -like "*Tesseract*" -or $_.DisplayName -like "*Tesseract*" -or $_.Name -like "ProxyManager-KillSwitch*" -or $_.DisplayName -like "ProxyManager-KillSwitch*" })
        $Facts.firewall = (@($Rules | ForEach-Object { [string]$_.Name }) | Sort-Object) -join ";"
        $Facts.firewall_error = $null
    } catch { $Facts.firewall = $null; $Facts.firewall_error = $_.Exception.Message }
    return $Facts
}

# --- sandbox and scope -------------------------------------------------------------------------
$ScopeNames = @("TESSERACT_INSTANCE_SCOPE","TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR","TEMP","TMP","APPDATA","LOCALAPPDATA",
    "TESSERACT_SHELL_CDP_PORT","TESSERACT_ISOLATED_AUDIT_MANIFEST","TESSERACT_REDUCED_MOTION","TESSERACT_SHELL_DEV_READY_TIMEOUT_MS",
    "TESSERACT_SHELL_DEV_WEBVIEW2_MISSING","TESSERACT_SHELL_DEV_RECREATE_FAIL","TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED","TESSERACT_SHELL_DEV_MARK_QUITTING",
    "PYTHONDONTWRITEBYTECODE","PYTHONPYCACHEPREFIX","PYTHONPATH","HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY","http_proxy","https_proxy","all_proxy","no_proxy")
$SavedEnv = @{}; foreach ($Name in $ScopeNames) { $SavedEnv[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
function Restore-Env { foreach ($Name in $ScopeNames) { [Environment]::SetEnvironmentVariable($Name,$SavedEnv[$Name],"Process") } }

function New-Sandbox([string] $Flow) {
    $Box = Join-Path $TempRoot ("tesseract-p4e2e-" + $Flow + "-" + [Guid]::NewGuid().ToString("N").Substring(0,12))
    [void]$OwnBoxes.Add([IO.Path]::GetFullPath($Box).TrimEnd('\'))
    $S = [ordered]@{ box = $Box; config = (Join-Path $Box "config"); data = (Join-Path $Box "data"); temp = (Join-Path $Box "temp")
        scope = ("p4e2e-" + $Flow + "-" + [Guid]::NewGuid().ToString("N").Substring(0,10)); appdata = $null; localappdata = $null }
    if ($RealSidecar) {
        $Out = & $Python -B (Join-Path $Repo "tests/test_bridge_p2_33.py") prepare --sandbox $Box
        if ($LASTEXITCODE -ne 0) { throw "hardened sandbox preparation failed" }
        $S.appdata = Join-Path $Box "appdata"; $S.localappdata = Join-Path $Box "localappdata"
    } else {
        # (defence in depth: the stand-in and the shell use none of them, yet APPDATA / LOCALAPPDATA
        # point into the sandbox as in the real-sidecar mode)
        $S.appdata = Join-Path $Box "appdata"; $S.localappdata = Join-Path $Box "localappdata"
        foreach ($Dir in @($S.config,$S.data,$S.temp,$S.appdata,$S.localappdata)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
        # The stand-in sidecar reads no configuration; the isolated example one (Tier3, sources, system
        # proxy, alerts and kill switch off; sources.yaml with its migration markers) is there anyway.
        & $Python -B -c @'
import sys, pathlib, yaml
repo, config = map(pathlib.Path, sys.argv[1:])
c = yaml.safe_load((repo/'config.example.yaml').read_text('utf-8'))
c['tiers']['tier3']['enabled'] = False
for t in ('tier1','tier2'):
    c['tiers'][t]['nodes'] = []
    c['tiers'][t]['subscription_urls'] = []
c['client']['system_proxy']['enabled'] = False
c['alerts']['enabled'] = False
if isinstance(c.get('killswitch'), dict): c['killswitch']['enabled'] = False
(config/'config.yaml').write_bytes(yaml.safe_dump(c,allow_unicode=True).encode('utf-8'))
(config/'sources.yaml').write_bytes(b'migrations:\n  tier3_source_portfolio_defaults_v1: true\n  tier3_source_portfolio_defaults_v2: true\n  tier3_source_default_delivery_v1: true\ngithub_repos: []\ntelegram_channels: []\nsubscription_urls: []\nlocal_files: []\n')
'@ $Repo $S.config
        if ($LASTEXITCODE -ne 0) { throw "isolated config setup failed" }
    }
    return $S
}
function Set-ScopeEnvironment($S, [int] $Port, $Extra) {
    foreach ($Name in $ScopeNames) { if ($Name -notin @("APPDATA","LOCALAPPDATA")) { Remove-Item "Env:$Name" -ErrorAction SilentlyContinue } }
    $env:TESSERACT_INSTANCE_SCOPE = $S.scope; $env:TESSERACT_CONFIG_DIR = $S.config; $env:TESSERACT_DATA_DIR = $S.data
    $env:TEMP = $S.temp; $env:TMP = $S.temp; $env:TESSERACT_SHELL_CDP_PORT = "$Port"
    $env:PYTHONDONTWRITEBYTECODE = "1"; $env:PYTHONPYCACHEPREFIX = $PyCache
    $env:APPDATA = $S.appdata; $env:LOCALAPPDATA = $S.localappdata
    if ($Extra) { foreach ($P in $Extra.PSObject.Properties) { [Environment]::SetEnvironmentVariable($P.Name, [string]$P.Value, "Process") } }
}
# Refuses a launch that is not fully inside this flow's custom scope.
function Assert-Scope($S) {
    foreach ($Name in @("TESSERACT_INSTANCE_SCOPE","TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR","TEMP")) {
        $V = [Environment]::GetEnvironmentVariable($Name,"Process")
        if (-not $V) { throw "refusing to launch: $Name is not set" }
    }
    if ($env:TESSERACT_INSTANCE_SCOPE -ne $S.scope -or $env:TESSERACT_INSTANCE_SCOPE -match '^(default)?$') { throw "refusing to launch: scope '$($env:TESSERACT_INSTANCE_SCOPE)' is not this flow's custom scope" }
    foreach ($Dir in @($env:TESSERACT_CONFIG_DIR, $env:TESSERACT_DATA_DIR, $env:TEMP, $env:TMP)) {
        if (-not (Under $Dir $S.box)) { throw "refusing to launch: $Dir is outside the sandbox $($S.box)" }
        foreach ($Live in $LiveDirs) { if ((Under $Dir $Live) -or (Under $Live $Dir)) { throw "refusing to launch: $Dir overlaps the live profile" } }
    }
    if (-not $env:APPDATA -or -not $env:LOCALAPPDATA -or -not (Under $env:APPDATA $S.box) -or -not (Under $env:LOCALAPPDATA $S.box)) { throw "refusing to launch: APPDATA/LOCALAPPDATA are not redirected into the sandbox" }
}

# --- one launch ----------------------------------------------------------------------------------
function Scoped-WebViews([string] $Data) {
    $Roots = @("WebView2","WebView2-admin") | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $Data $_)).TrimEnd('\') + '\' }
    return @(Get-CimInstance Win32_Process -Filter "name = 'msedgewebview2.exe'" | Where-Object {
        $Line = [string]$_.CommandLine
        $M = [regex]::Match($Line, '--user-data-dir=(?:"([^"]*)"|(\S+))')
        if (-not $M.Success) { return $Line.IndexOf($Data, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
        $Dir = if ($M.Groups[1].Success) { $M.Groups[1].Value } else { $M.Groups[2].Value }
        if ($Dir.StartsWith('\\?\UNC\')) { $Dir = '\\' + $Dir.Substring(8) } elseif ($Dir.StartsWith('\\?\')) { $Dir = $Dir.Substring(4) }
        $Full = [IO.Path]::GetFullPath($Dir).TrimEnd('\') + '\'
        @($Roots | Where-Object { $Full.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
    })
}
function Wait-ScopedWebViewsGone([string] $Data, $Shell) {
    $From = [DateTime]::UtcNow
    if ($Shell) { try { $Shell.Refresh(); if ($Shell.HasExited) { $From = $Shell.ExitTime.ToUniversalTime() } } catch { } }
    $End = $From.AddSeconds(20)
    do { $Left = @(Scoped-WebViews $Data); if ($Left.Count -eq 0) { break }; Start-Sleep -Milliseconds 200 } while ([DateTime]::UtcNow -lt $End)
    return ,$Left
}
function Tray-Quit([int] $ShellPid) {
    $Tray = [P4E2e]::Find("TesseractShellTray", [uint32]$ShellPid)
    if ($Tray -eq [IntPtr]::Zero) { return $false }
    return [P4E2e]::PostMessageW($Tray, 0x111, [IntPtr]2, [IntPtr]::Zero)
}
function Fixture-Pids([string] $Data) {
    $State = Join-Path $Data "p4-ui-fixture"
    if (-not (Test-Path -LiteralPath $State)) { return @() }
    return @(Get-ChildItem -LiteralPath $State -Filter "launch-*" -File | ForEach-Object { [int](Get-Content -LiteralPath $_.FullName -Raw) })
}
# The cleanup order of p3_exit_smoke.ps1, every step recorded.
function Cleanup-Launch($L) {
    $Steps = @()
    if (-not $L.shell) {
        $Late = Resolve-LateShell (Join-Path $L.dir "root.json") $L.watcher
        if ($Late) { $Steps += Close-LateShell $Late $L.dir; $L.shell = $Late }
        else { $Steps += "no_shell" }
    }
    if ($L.shell -and -not $L.shell.HasExited) {
        $Closed = [P3Late]::CloseDialogs([uint32]$L.shell.Id); $Steps += "dialogs_closed=$Closed"
        if ($Closed) { Start-Sleep -Milliseconds 500 }
        $Steps += "resume_sidecar=not_suspended"
        if (-not $L.shell.HasExited) { $Steps += ("tray_quit=" + (Tray-Quit $L.shell.Id)) }
        if (-not $L.shell.WaitForExit(30000)) {
            $Closed = [P3Late]::CloseDialogs([uint32]$L.shell.Id); if ($Closed) { $Steps += "dialogs_closed_late=$Closed" }
            if (-not $L.shell.WaitForExit(5000)) { Stop-Process -Id $L.shell.Id -Force -ErrorAction SilentlyContinue; $Steps += "stop_scoped_shell"; [void]$L.shell.WaitForExit(5000) }
        }
        $Steps += "shell_exited=" + $L.shell.HasExited
    }
    if ($L.watcher -and -not $L.watcher.HasExited) { [void]$L.watcher.WaitForExit($InventoryWaitMs); $Steps += "watcher_exited=" + $L.watcher.HasExited }
    return ,$Steps
}
# After the failure cleanup: what is left (process_watch inventory, this scope's WebView2, the
# stand-in's pids). verdict: none | found | unknown (the inventory is missing or unreadable).
function Residue-AfterCleanup($L, [string] $Data) {
    $R = [ordered]@{ verdict = "unknown"; inventory = $null; scoped_webviews = $null; fixture_pids_alive = $null }
    $InvFile = Join-Path $L.dir "process-inventory.json"
    $Inv = $null
    if (Test-Path -LiteralPath $InvFile) {
        try { $Inv = Get-Content -LiteralPath $InvFile -Raw | ConvertFrom-Json; $R.inventory = [ordered]@{ passed = $Inv.passed; active_processes = $Inv.active_processes; total_processes = $Inv.total_processes } }
        catch { $R.inventory = "unreadable" }
    } else { $R.inventory = "missing" }
    # (a fresh 20 s window from now: the cleanup may have spent the one after the shell's exit)
    $Left = Wait-ScopedWebViewsGone $Data $null     # (returns ,$array: no @() around it)
    $R.scoped_webviews = $Left.Count
    if ($Left.Count) { $R.scoped_webview_list = @($Left | ForEach-Object { $T = [regex]::Match([string]$_.CommandLine, '--type=(\S+)'); "{0} {1} started {2}" -f $_.ProcessId, $(if ($T.Success) { $T.Groups[1].Value } else { "browser" }), [string]$_.CreationDate }) }
    $R.fixture_pids_alive = @(Fixture-Pids $Data | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }).Count
    if ($R.scoped_webviews -gt 0 -or $R.fixture_pids_alive -gt 0 -or ($Inv -and (-not $Inv.passed -or $Inv.active_processes -ne 0))) { $R.verdict = "found" }
    elseif ($Inv) { $R.verdict = "none" }
    return $R
}
# Ends a process and its descendants (the node flow runner and whatever it spawned); -> the pids.
function Stop-Tree([int] $RootPid) {
    $All = @(Get-CimInstance Win32_Process -Property ProcessId,ParentProcessId,CreationDate)
    $Root = @($All | Where-Object { $_.ProcessId -eq $RootPid })[0]
    if (-not $Root) { return "" }
    # (children only if started after the root: a recycled parent pid never pulls in a stranger)
    $Tree = @($RootPid); $I = 0
    while ($I -lt $Tree.Count) { $P = $Tree[$I]; $Tree += @($All | Where-Object { $_.ParentProcessId -eq $P -and $_.ProcessId -ne $P -and $_.CreationDate -ge $Root.CreationDate } | ForEach-Object { [int]$_.ProcessId }); $I++ }
    [array]::Reverse($Tree)
    foreach ($P in $Tree) { Stop-Process -Id $P -Force -ErrorAction SilentlyContinue }
    return ($Tree -join ",")
}

function Invoke-Launch($S, $Flow, [int] $Index, $Spec, [string] $Evidence) {
    $L = [ordered]@{ launch = $Index; args = @($Spec.args | Where-Object { $_ }); env = $Spec.env; dir = (Join-Path $S.box ("launch-" + $Index)); shell = $null; watcher = $null
        passed = $false; error = $null; node = $null; checks = [ordered]@{}; cleanup = @(); evidence_error = @() }
    New-Item -ItemType Directory -Force -Path $L.dir | Out-Null
    $Log = Join-Path $S.data "logs/shell.log"
    $FixtureState = Join-Path $S.data "p4-ui-fixture"
    try {
        if (-not $RealSidecar) {
            New-Item -ItemType Directory -Force -Path $FixtureState | Out-Null
            $Options = if ($Spec.options) { $Spec.options } else { @{} }
            [IO.File]::WriteAllText((Join-Path $FixtureState "options.json"), ($Options | ConvertTo-Json -Depth 6 -Compress), $Utf8)
        }
        $ReadyBefore = @(Get-ChildItem -LiteralPath $FixtureState -Filter "ready-*" -File -ErrorAction SilentlyContinue).Count
        $LogLinesBefore = if (Test-Path -LiteralPath $Log) { @(Get-Content -LiteralPath $Log).Count } else { 0 }
        $Port = Free-Port
        Set-ScopeEnvironment $S $Port $Spec.env
        if ($RealSidecar) { foreach ($Name in @("HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY","http_proxy","https_proxy","all_proxy","no_proxy","PYTHONPATH")) { Remove-Item "Env:$Name" -ErrorAction SilentlyContinue } }
        Assert-Scope $S
        $Arguments = @()
        if (-not $RealSidecar) { $Arguments += @("--dev-sidecar-script", ('"' + $Fixture + '"')) }
        $Arguments += @($Spec.args | Where-Object { $_ })
        $Watch = @("-B",('"'+(Join-Path $Repo "shell/app/process_watch.py")+'"'),"--output",('"'+(Join-Path $L.dir "process-inventory.json")+'"'),"--ready",('"'+(Join-Path $L.dir "root.json")+'"'),"--",('"'+$ShellExe+'"')) + $Arguments
        $L.watcher = Start-Process -FilePath $Python -ArgumentList $Watch -WorkingDirectory $Repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $L.dir "watch-stdout.log") -RedirectStandardError (Join-Path $L.dir "watch-stderr.log")
        $null = $L.watcher.Handle
        Wait-Until { (Test-Path -LiteralPath (Join-Path $L.dir "root.json")) -or $L.watcher.HasExited } "suspended job launch" 30
        if (-not (Test-Path -LiteralPath (Join-Path $L.dir "root.json"))) { throw "suspended job launch failed" }
        $ShellPid = [int]((Get-Content -LiteralPath (Join-Path $L.dir "root.json") -Raw | ConvertFrom-Json).pid)
        $L.shell = Get-Process -Id $ShellPid; $null = $L.shell.Handle
        $L.checks.shell_pid = $ShellPid; $L.checks.cdp_port = $Port; $L.checks.scope = $S.scope
        $NewLines = { if (Test-Path -LiteralPath $Log) { @(Get-Content -LiteralPath $Log | Select-Object -Skip $LogLinesBefore) } else { @() } }
        Wait-Until { $N = & $NewLines; if (@($N | Where-Object { $_ -match ' (sidecar_start_failed|hello_timeout|sidecar_exit_before_hello)' }).Count) { throw "sidecar failed before hello" }
            @($N | Where-Object { $_ -match ' (shell_window_ready|autostart_start )' }).Count -ge 1 -and @($N | Where-Object { $_ -match ' sidecar_ready ' }).Count -ge 1 } "shell window + sidecar hello (window-first launch, 11-05)" 60
        if (-not $RealSidecar) { Wait-Until { @(Get-ChildItem -LiteralPath $FixtureState -Filter "ready-*" -File -ErrorAction SilentlyContinue).Count -gt $ReadyBefore } "stand-in sidecar ready" 30 }
        # The flow itself (playwright-core over CDP).
        $NodeArgs = @($RunMjs, "--flow", $Flow, "--launch", "$Index", "--port", "$Port", "--pid", "$ShellPid", "--data", $S.data, "--evidence", $Evidence, "--python", $Python)
        if ($RealSidecar) { $NodeArgs += "--real" }
        # (a child process with its output in files: an overall limit, and native stderr never turns
        # into PowerShell errors under Stop)
        $NodeStdout = Join-Path $L.dir "node-stdout.log"; $NodeStderr = Join-Path $L.dir "node-stderr.log"
        $NodeProc = Start-Process -FilePath $Node -ArgumentList @($NodeArgs | ForEach-Object { '"' + $_ + '"' }) -WorkingDirectory $Repo -NoNewWindow -PassThru -RedirectStandardOutput $NodeStdout -RedirectStandardError $NodeStderr
        $null = $NodeProc.Handle
        $NodeTimedOut = -not $NodeProc.WaitForExit($FlowTimeoutSec * 1000)
        if ($NodeTimedOut) { $L.checks.node_timeout = ("killed after {0} s: {1}" -f $FlowTimeoutSec, (Stop-Tree $NodeProc.Id)); [void]$NodeProc.WaitForExit(10000) }
        $NodeOut = @(@(Get-Content -LiteralPath $NodeStdout -Encoding UTF8 -ErrorAction SilentlyContinue) + @(Get-Content -LiteralPath $NodeStderr -Encoding UTF8 -ErrorAction SilentlyContinue) | ForEach-Object { [string]$_ })
        [IO.File]::WriteAllLines((Join-Path $Evidence ("launch-" + $Index + "-node.log")), [string[]]@($NodeOut | Where-Object { $_ -notlike "E2E_RESULT *" }), $Utf8)
        if ($NodeTimedOut) { throw "flow ${Flow} launch ${Index}: node run.mjs exceeded $FlowTimeoutSec s (ended; the page may be stuck)" }
        $ResultFile = Join-Path $Evidence ("launch-" + $Index + ".json")
        if (-not (Test-Path -LiteralPath $ResultFile)) { throw "flow wrote no result (node output: $(@($NodeOut) -join ' | '))" }
        $L.node = Get-Content -LiteralPath $ResultFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $L.node.passed) { throw "flow ${Flow} launch ${Index}: $($L.node.error)" }
        # The end of every launch: tray Quit, exit 0, nothing left.
        if (-not $L.shell.HasExited) {
            if (-not (Tray-Quit $ShellPid)) { throw "tray window not found for the quit" }
            if (-not $L.shell.WaitForExit(30000)) { throw "shell did not exit within 30 s of the tray Quit" }
        }
        $L.shell.Refresh(); $L.checks.shell_exit_code = $L.shell.ExitCode
        if ($L.shell.ExitCode -ne 0) { throw "shell exit code $($L.shell.ExitCode)" }
        $Left = Wait-ScopedWebViewsGone $S.data $L.shell
        $L.checks.scoped_webviews_after_exit = $Left.Count
        if ($Left.Count) { $L.checks.residue_class = Save-ResidueTree $Left $L.shell $Log $L.dir; throw "scoped WebView processes remain ($($L.checks.residue_class))" }
        if (-not $L.watcher.WaitForExit($InventoryWaitMs)) { throw (Inventory-TimeoutMessage (Join-Path $L.dir "process-inventory.json")) }
        $Inventory = Get-Content -LiteralPath (Join-Path $L.dir "process-inventory.json") -Raw | ConvertFrom-Json
        $L.checks.inventory = [ordered]@{ passed = $Inventory.passed; active_processes = $Inventory.active_processes; total_processes = $Inventory.total_processes
            kernel_membership_complete = $Inventory.kernel_membership_complete; webview_grace_used = $Inventory.webview_grace_used }
        if (-not $Inventory.passed -or $Inventory.active_processes -ne 0) { throw "owned process residual detected" }
        $Alive = @(Fixture-Pids $S.data | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        $L.checks.fixture_pids_alive = $Alive.Count
        if ($Alive.Count) { throw "stand-in sidecar process still alive: $($Alive -join ',')" }
        $L.passed = $true
    } catch {
        $L.error = $_.Exception.Message
        $L.cleanup = Cleanup-Launch $L
        try { $L.residue_after_cleanup = Residue-AfterCleanup $L $S.data }
        catch { $L.residue_after_cleanup = [ordered]@{ verdict = "unknown"; error = $_.Exception.Message } }
        $L.cleanup += ("residue=" + $L.residue_after_cleanup.verdict)
    } finally {
        Restore-Env
        # (each copy on its own: one IO error, e.g. a full disk, fails this launch, never the run)
        foreach ($File in @("process-inventory.json","root.json","residue-tree.json","late-shell.json")) {
            try { $From = Join-Path $L.dir $File; if (Test-Path -LiteralPath $From) { Copy-Item -LiteralPath $From -Destination (Join-Path $Evidence ("launch-" + $Index + "-" + $File)) -Force } }
            catch { $L.evidence_error += ($File + ": " + $_.Exception.Message) }
        }
        if ($L.evidence_error.Count) {
            $L.passed = $false
            if (-not $L.error) { $L.error = "evidence copy failed: " + ($L.evidence_error -join "; ") }
        }
        $L.shell = $null; $L.watcher = $null
    }
    return $L
}

# Removes a directory this run made (New-Sandbox, or this run's bytecode cache) and nothing else:
# it must be in $OwnBoxes, sit directly under $TempRoot, be named tesseract-p4e2e-<flow>-<12 hex>
# (or tesseract-p4e2e-pycache-<32 hex>) and not be a reparse point. [IO.Directory]::Delete removes a
# link inside without following it. -> "removed" | "absent" | "refused: ..." | "failed: ..."
function Remove-OwnSandbox([string] $Box) {
    try {
        $Full = [IO.Path]::GetFullPath($Box).TrimEnd('\')
        $Name = [IO.Path]::GetFileName($Full)
        if (-not $OwnBoxes.Contains($Full)) { return "refused: not made by this run" }
        if (-not [string]::Equals([IO.Path]::GetDirectoryName($Full), $TempRoot, [StringComparison]::OrdinalIgnoreCase)) { return "refused: not directly under $TempRoot" }
        if ($Name -cnotmatch '^tesseract-p4e2e-([A-Za-z0-9_]+-[0-9a-f]{12}|pycache-[0-9a-f]{32})$') { return "refused: name $Name" }
        if (-not (Test-Path -LiteralPath $Full -PathType Container)) { return "absent" }
        if (([IO.File]::GetAttributes($Full) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return "refused: reparse point" }
        for ($Try = 1; ; $Try++) {
            try { [IO.Directory]::Delete($Full, $true); return "removed" }
            catch { if ($Try -ge 3) { return "failed: " + $_.Exception.Message }; Start-Sleep -Milliseconds 1000 }
        }
    } catch { return "failed: " + $_.Exception.Message }
}

# --- the run ------------------------------------------------------------------------------------
$Summary = [ordered]@{ run = $RunTag; mode = $Mode; started_at = [DateTime]::UtcNow.ToString("o"); flows = [ordered]@{} }
$HostBefore = $null
if ($RealSidecar) {
    $HostBefore = Host-Facts
    if ($HostBefore.firewall_error) { throw "refusing to run without a firewall baseline: $($HostBefore.firewall_error)" }
    $Summary.host_before = $HostBefore
}
$AnyFailed = $false
$AllKnown = @(); $AllResidue = @(); $AllEvidenceErrors = @()
foreach ($Flow in $Flows) {
    $Entry = @($Plan | Where-Object { $_.name -eq $Flow })[0]
    if ($RealSidecar -and -not $Entry.real) {
        $Summary.flows[$Flow] = [ordered]@{ passed = $null; skipped = $true; reason = $Entry.skip_real }
        Write-Output ("SKIP {0} (real sidecar): {1}" -f $Flow, $Entry.skip_real)
        continue
    }
    # (one flow's failure, a full disk included, never ends the run: the next flow runs and the run
    # summary is written)
    $S = $null; $Evidence = $null
    $Result = [ordered]@{ flow = $Flow; mode = $Mode; run = $RunTag; scope = $null; sandbox = $null; passed = $false; error = $null; launches = @(); evidence_error = @(); sandbox_cleanup = $null }
    $Started = [DateTime]::UtcNow
    try {
        $S = New-Sandbox $Flow
        $Result.scope = $S.scope; $Result.sandbox = $S.box
        $Evidence = Join-Path $EvidenceRoot $S.scope
        New-Item -ItemType Directory -Force -Path $Evidence | Out-Null
        $I = 0
        foreach ($Spec in @($Entry.launches)) {
            $L = Invoke-Launch $S $Flow $I $Spec $Evidence
            $Result.launches += $L
            if (-not $L.passed) { throw $L.error }
            $I++
        }
        if ($RealSidecar) {
            $Readback = & $Python -B (Join-Path $Repo "tests/test_bridge_p2_33.py") readback --sandbox $S.box
            $Result.readback = (@($Readback) | Select-Object -Last 1) | ConvertFrom-Json
            if ($LASTEXITCODE -ne 0 -or @($Result.readback.problems).Count) { throw "sandbox read-back: $(@($Result.readback.problems) -join '; ')" }
            $After = Host-Facts; $Result.host_after = $After
            foreach ($Key in @("ProxyEnable","ProxyServer","ProxyOverride","AutoConfigURL","firewall","firewall_error")) {
                if ([string]$After[$Key] -ne [string]$HostBefore[$Key]) { throw "host fact $Key changed: '$($HostBefore[$Key])' -> '$($After[$Key])'" }
            }
        }
        $Result.passed = $true
    } catch {
        $Result.error = $_.Exception.Message
    } finally {
        Restore-Env
        $Result.seconds = [Math]::Round(([DateTime]::UtcNow - $Started).TotalSeconds, 1)
        if ($S -and $Evidence) {
            # (each copy on its own; an IO error fails the flow and is recorded, never thrown)
            try { $ShellLog = Join-Path $S.data "logs/shell.log"; if (Test-Path -LiteralPath $ShellLog) { Copy-Item -LiteralPath $ShellLog -Destination (Join-Path $Evidence "shell.log") -Force } }
            catch { $Result.evidence_error += ("shell.log: " + $_.Exception.Message) }
            foreach ($Old in @(Get-ChildItem -LiteralPath (Join-Path $S.data "logs") -Filter "shell.log.*" -File -ErrorAction SilentlyContinue)) {
                try { Copy-Item -LiteralPath $Old.FullName -Destination (Join-Path $Evidence $Old.Name) -Force }
                catch { $Result.evidence_error += ($Old.Name + ": " + $_.Exception.Message) }
            }
        }
        if ($Result.evidence_error.Count -and $Result.passed) { $Result.passed = $false; $Result.error = "evidence copy failed: " + ($Result.evidence_error -join "; ") }
        # The sandbox goes only when the flow passed and its evidence is written; a failed flow keeps it.
        $RemoveBox = $S -and $Result.passed -and -not $KeepSandbox
        $Result.sandbox_cleanup = if (-not $S) { "none" } elseif ($KeepSandbox) { "kept (-KeepSandbox)" } elseif (-not $Result.passed) { "kept (failed)" } else { "pending" }
        if ($Evidence) {
            try { Write-Json (Join-Path $Evidence "result.json") $Result }
            catch { $Result.evidence_error += ("result.json: " + $_.Exception.Message); $Result.passed = $false; $Result.error = "evidence: result.json not written: " + $_.Exception.Message; $RemoveBox = $false; $Result.sandbox_cleanup = "kept (failed)" }
        }
        if ($RemoveBox) {
            $Result.sandbox_cleanup = Remove-OwnSandbox $S.box
            try { Write-Json (Join-Path $Evidence "result.json") $Result } catch { $Result.evidence_error += ("result.json rewrite: " + $_.Exception.Message) }
        }
    }
    # Checks a flow marks as another ticket's unfinished work (driver.mjs known()): listed, never hidden.
    $Known = @($Result.launches | ForEach-Object { if ($_.node) { @($_.node.known_fail) } } | Where-Object { $_ } | ForEach-Object { "$($_.id) ($($_.owner))" })
    $Fixed = @($Result.launches | ForEach-Object { if ($_.node) { @($_.node.known_fixed) } } | Where-Object { $_ } | ForEach-Object { "$($_.id) ($($_.owner))" })
    $Residue = @($Result.launches | Where-Object { $_.residue_after_cleanup } | ForEach-Object { "launch $($_.launch): $($_.residue_after_cleanup.verdict)" })
    $EvidenceErrors = @(@($Result.evidence_error) + @($Result.launches | ForEach-Object { @($_.evidence_error) | ForEach-Object { "launch: $_" } }) | Where-Object { $_ })
    $Summary.flows[$Flow] = [ordered]@{ passed = $Result.passed; error = $Result.error; scope = $Result.scope; evidence = $Evidence; seconds = $Result.seconds; known_fail = $Known; known_fixed = $Fixed
        residue_after_cleanup = $Residue; evidence_error = $EvidenceErrors; sandbox = $Result.sandbox; sandbox_cleanup = $Result.sandbox_cleanup }
    foreach ($K in $Known) { $AllKnown += ($Flow + ": " + $K) }
    foreach ($R in $Residue) { if ($R -notlike "*: none") { $AllResidue += ($Flow + " " + $R) } }
    foreach ($E in $EvidenceErrors) { $AllEvidenceErrors += ($Flow + ": " + $E) }
    $Note = ""; if ($Known.Count) { $Note += " KNOWN_FAIL: " + ($Known -join ", ") }; if ($Fixed.Count) { $Note += " KNOWN_FIXED: " + ($Fixed -join ", ") }
    if ($Result.passed) { Write-Output ("PASS {0} ({1} s){3} {2} [sandbox {4}]" -f $Flow, $Result.seconds, $Evidence, $Note, $Result.sandbox_cleanup) }
    else { $AnyFailed = $true; Write-Output ("FAIL {0}: {1} ({2}) [sandbox {3}: {4}]" -f $Flow, $Result.error, $Evidence, $Result.sandbox_cleanup, $Result.sandbox) }
}
$Summary.pycache_cleanup = if ($KeepSandbox) { "kept (-KeepSandbox)" } else { Remove-OwnSandbox $PyCache }
$Summary.ended_at = [DateTime]::UtcNow.ToString("o")
$Summary.passed = -not $AnyFailed
# (the checks recorded as another ticket's unfinished work, at the top of the run, never hidden in
# a flow entry; residue left after a failure cleanup and evidence copy errors likewise)
$Summary.known_fail_total = $AllKnown.Count
$Summary.known_fail = $AllKnown
$Summary.residue_after_cleanup = $AllResidue
$Summary.evidence_error = $AllEvidenceErrors
$RunFile = Join-Path $EvidenceRoot ("run-" + $RunTag + ".json")
try { Write-Json $RunFile $Summary }
catch { $AnyFailed = $true; Write-Output ("RUN SUMMARY NOT WRITTEN ({0}): {1}" -f $RunFile, $_.Exception.Message); Write-Output ($Summary | ConvertTo-Json -Depth 6 -Compress) }
if ($AllResidue.Count) { Write-Output ("RESIDUE after failure cleanup: " + ($AllResidue -join "; ")) }
if ($AllEvidenceErrors.Count) { Write-Output ("EVIDENCE ERRORS: " + ($AllEvidenceErrors -join "; ")) }
if ($AnyFailed) { throw "T-P4-11 end-to-end: some flows failed (run-$RunTag.json)" }
if ($AllKnown.Count) { Write-Output ("T-P4-11 end-to-end passed with {0} known_fail ({1}; run-{2}.json): {3}" -f $AllKnown.Count, $Mode, $RunTag, ($AllKnown -join "; ")) }
else { Write-Output "T-P4-11 end-to-end passed ($Mode; run-$RunTag.json)" }

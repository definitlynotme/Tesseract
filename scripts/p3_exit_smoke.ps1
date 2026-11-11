[CmdletBinding()]
param([string[]] $Scenarios = @("tray-quit","sidecar-kill","session-end","quit-no-bye","quit-stall-force","prehello"))
# T-P3-04 exit smoke, custom scope only (P3-02 guard):
#  tray-quit     tray Quit -> page host:confirmQuit answered with 0 -> app.quit -> bye; the shell
#                exits with the sidecar's exit code and leaves no process behind.
#  sidecar-kill  the sidecar is killed -> stopped-state relaunch, new epoch, host:banner on the
#                dev page, shell.log records; then tray Quit (page silent -> 3 s timeout) exits 0.
#  session-end   a helper (this script) sends WM_QUERYENDSESSION + WM_ENDSESSION(logoff) to the main
#                window -> app.sessionEnd is sent and answered; both processes exit.
#  quit-no-bye   (fix round 1, M1) the sidecar is suspended, tray Quit, then the script terminates it
#                with code 3 before any bye: the shell process itself must exit 3, not 0.
#  quit-stall-force  (S8) the sidecar is suspended, tray Quit; after 20 s without progress the
#                visible, topmost stall dialog appears; its "force quit" button (TDM_CLICK_BUTTON)
#                terminates the sidecar and the shell process exits 1.
#  prehello      (S3) a sidecar that exits before hello (whoami.exe) under a hidden (SW_HIDE) start:
#                the pre-hello dialog must still be visible; WM_CLOSE ends the shell with 1.
# A failed scenario is cleaned up: dialogs of the scoped shell get WM_CLOSE, a suspended sidecar is
# resumed, the tray Quit is sent, and only then the scoped test shell is stopped (result.json
# records which step ended it). A scenario that failed before hello is retried once.
# Real logoff acceptance belongs to P7. The caller builds incrementally under the ticket's CPU
# rules; this script never builds.
$ErrorActionPreference = "Stop"
foreach ($Name in @("TESSERACT_INSTANCE_SCOPE","TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR")) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($Name,"Process"))) {
        throw "custom_scope_required: missing $Name"
    }
}
if ($env:TESSERACT_ISOLATED_AUDIT_MANIFEST) { throw "custom_scope_required: audit is not allowed" }
$TempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
foreach ($Name in @("TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR")) {
    $Resolved = (Resolve-Path -LiteralPath ([Environment]::GetEnvironmentVariable($Name,"Process"))).Path
    if (-not $Resolved.StartsWith($TempRoot,[StringComparison]::OrdinalIgnoreCase)) { throw "custom_scope_required: $Name must be under the temporary directory" }
}
$Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
. (Join-Path $PSScriptRoot "p3_residue.ps1")
$Python = Join-Path $Repo ".venv/Scripts/python.exe"
$Exe = Join-Path $Repo "shell/target/debug/tesseract-shell.exe"
if (-not (Test-Path -LiteralPath $Exe)) { throw "debug shell missing; build it first" }
# The work-order directory name is not ASCII; locate it by the ticket file instead of spelling it here.
$Orders = Get-ChildItem -LiteralPath (Join-Path $Repo ".build") -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "T-P3-04.md") } | Select-Object -First 1
if (-not $Orders) { throw "work-order directory not found" }
$BaseConfig = $env:TESSERACT_CONFIG_DIR; $BaseData = $env:TESSERACT_DATA_DIR

Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Net;
using System.Net.WebSockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
public static class P3Exit {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindowExW(IntPtr parent, IntPtr after, string cls, string title);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern IntPtr SendMessageTimeoutW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l, uint flags, uint ms, out IntPtr result);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll", EntryPoint="GetWindowLongPtrW")] static extern IntPtr GetWindowLongPtrW(IntPtr hwnd, int index);
    [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("ntdll.dll")] static extern int NtSuspendProcess(IntPtr process);
    [DllImport("ntdll.dll")] static extern int NtResumeProcess(IntPtr process);
    public static bool Topmost(IntPtr hwnd) { return (GetWindowLongPtrW(hwnd, -20).ToInt64() & 0x8) != 0; }
    // PROCESS_SUSPEND_RESUME | PROCESS_TERMINATE; only ever called on the scoped test sidecar.
    static bool OnProcess(uint pid, Func<IntPtr,bool> action) {
        IntPtr h = OpenProcess(0x0800 | 0x0001, false, pid);
        if (h == IntPtr.Zero) return false;
        try { return action(h); } finally { CloseHandle(h); }
    }
    public static bool Suspend(uint pid) { return OnProcess(pid, h => NtSuspendProcess(h) >= 0); }
    public static bool Resume(uint pid) { return OnProcess(pid, h => NtResumeProcess(h) >= 0); }
    public static bool Terminate(uint pid, uint code) { return OnProcess(pid, h => TerminateProcess(h, code)); }
    public static IntPtr Find(string cls, string title, uint pid) {
        IntPtr after = IntPtr.Zero;
        while (true) {
            after = FindWindowExW(IntPtr.Zero, after, cls, title);
            if (after == IntPtr.Zero) return IntPtr.Zero;
            uint owner; GetWindowThreadProcessId(after, out owner);
            if (owner == pid) return after;
        }
    }
    // SMTO_ABORTIFHUNG; returns the window procedure's result, or -1 on timeout/failure.
    public static long Send(IntPtr hwnd, uint msg, long w, long l, uint ms) {
        IntPtr result;
        if (SendMessageTimeoutW(hwnd, msg, new IntPtr(w), new IntPtr(l), 2, ms, out result) == IntPtr.Zero) return -1;
        return result.ToInt64();
    }
    public static string Http(string url) {
        HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url); request.Proxy = null; request.Timeout = 3000;
        using (WebResponse response = request.GetResponse()) using (StreamReader reader = new StreamReader(response.GetResponseStream())) return reader.ReadToEnd();
    }
    // One fixed Runtime.evaluate; expressions come from this script only (no quotes or backslashes).
    public static string Evaluate(string socketUrl, string expression) {
        using (ClientWebSocket ws = new ClientWebSocket()) {
            ws.Options.Proxy = null;
            CancellationTokenSource cts = new CancellationTokenSource(8000);
            ws.ConnectAsync(new Uri(socketUrl), cts.Token).Wait();
            string body = "{\"id\":1,\"method\":\"Runtime.evaluate\",\"params\":{\"expression\":\"" + expression + "\",\"returnByValue\":true}}";
            ws.SendAsync(new ArraySegment<byte>(Encoding.UTF8.GetBytes(body)), WebSocketMessageType.Text, true, cts.Token).Wait();
            byte[] buffer = new byte[65536];
            while (true) {
                StringBuilder text = new StringBuilder(); WebSocketReceiveResult part;
                do { part = ws.ReceiveAsync(new ArraySegment<byte>(buffer), cts.Token).Result; text.Append(Encoding.UTF8.GetString(buffer, 0, part.Count)); } while (!part.EndOfMessage);
                string message = text.ToString();
                if (message.StartsWith("{\"id\":1,")) return message;
            }
        }
    }
}
'@

$WM_COMMAND = 0x111; $CmdQuit = 2; $WM_QUERYENDSESSION = 0x11; $WM_ENDSESSION = 0x16; $ENDSESSION_LOGOFF = 0x80000000L
$WM_CLOSE = 0x10; $TDM_CLICK_BUTTON = 0x466; $ID_FORCE = 101
function Wait-Until([scriptblock] $Condition, [string] $Label, [int] $Seconds = 15) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while (-not (& $Condition)) {
        if ([DateTime]::UtcNow -gt $Deadline) { throw "timeout: $Label" }
        Start-Sleep -Milliseconds 100
    }
}
function Log-Lines { if (Test-Path -LiteralPath $script:ShellLog) { return @(Get-Content -LiteralPath $script:ShellLog) } else { return @() } }
# A shell.log line "<ms> <code>[ k=v ...]" whose text after the timestamp starts with $Prefix.
function Log-Has([string] $Prefix, [int] $Count = 1) {
    return @(Log-Lines | Where-Object { ($_ -replace '^\d+ ','').StartsWith($Prefix) }).Count -ge $Count
}
function Log-Value([string] $Pattern) {
    foreach ($Line in (Log-Lines)) { $M = [regex]::Match($Line, $Pattern); if ($M.Success) { return $M.Groups[1].Value } }
    return $null
}
# Residue check (decision P3-06 item 6, T-P3-H): this scope's msedgewebview2 processes - their
# --user-data-dir lies under <data>\WebView2[-admin] (or, without that switch, the command line
# names <data>) - get 20 s after the shell exits; process_watch.py applies the same rule to the
# kernel job and fails any other descendant at once (10 s audit window, no grace).
$ResidueGraceSeconds = 20
function Scoped-WebViews([string] $Data) {
    $Roots = @("WebView2","WebView2-admin") | ForEach-Object { [IO.Path]::GetFullPath((Join-Path $Data $_)).TrimEnd('\') + '\' }
    return @(Get-CimInstance Win32_Process -Filter "name = 'msedgewebview2.exe'" | Where-Object {
        $Line = [string]$_.CommandLine
        $M = [regex]::Match($Line, '--user-data-dir=(?:"([^"]*)"|(\S+))')
        # Case-insensitive like the --user-data-dir rule (T-P3-H fix 1); an unreadable command line
        # (empty) is not counted here - process_watch decides by the job membership.
        if (-not $M.Success) { return $Line.IndexOf($Data, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
        $Dir = if ($M.Groups[1].Success) { $M.Groups[1].Value } else { $M.Groups[2].Value }
        # WebView2 passes the extended form (--user-data-dir=\\?\C:\...): drop the prefix first.
        if ($Dir.StartsWith('\\?\UNC\')) { $Dir = '\\' + $Dir.Substring(8) } elseif ($Dir.StartsWith('\\?\')) { $Dir = $Dir.Substring(4) }
        $Full = [IO.Path]::GetFullPath($Dir).TrimEnd('\') + '\'
        @($Roots | Where-Object { $Full.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
    })
}
# The 20 s count from the shell's exit, like process_watch (T-P3-H fix 1, S5).
function Wait-ScopedWebViewsGone([string] $Data, $Shell) {
    $From = [DateTime]::UtcNow
    if ($Shell) { try { $Shell.Refresh(); if ($Shell.HasExited) { $From = $Shell.ExitTime.ToUniversalTime() } } catch { } }
    $End = $From.AddSeconds($ResidueGraceSeconds)
    do { $Left = @(Scoped-WebViews $Data); if ($Left.Count -eq 0) { break }; Start-Sleep -Milliseconds 200 } while ([DateTime]::UtcNow -lt $End)
    return ,$Left
}
# TESSERACT_P3_EVIDENCE_ROOT (T-P3-H): a later ticket re-running this smoke keeps its evidence in its own directory.
$EvidenceRoot = if ($env:TESSERACT_P3_EVIDENCE_ROOT) { $env:TESSERACT_P3_EVIDENCE_ROOT } else { Join-Path $Orders.FullName "evidence/T-P3-04" }
function Page-Socket {
    try { $Pages = [P3Exit]::Http("http://127.0.0.1:$script:Port/json/list") | ConvertFrom-Json } catch { return $null }
    $Page = @($Pages | Where-Object { $_.type -eq "page" -and $_.url -like "*dev-rt.html*" }) | Select-Object -First 1
    if ($Page) { return $Page.webSocketDebuggerUrl } else { return $null }
}
function Page-Eval([string] $Expression) {
    $Socket = Page-Socket
    if (-not $Socket) { return $null }
    try { $Reply = [P3Exit]::Evaluate($Socket, $Expression) | ConvertFrom-Json } catch { return $null }
    return $Reply.result.result.value
}
function Sidecar-Pids([int] $ShellPid) {
    return @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ShellPid" | Where-Object { $_.Name -like "python*.exe" -or $_.Name -like "tesseract-runtime*.exe" } | ForEach-Object { [int]$_.ProcessId })
}
function New-Scenario([string] $Name) {
    $Tag = "p3-04-$Name-" + [Guid]::NewGuid().ToString("N").Substring(0,12)
    $S = [ordered]@{ name = $Name; tag = $Tag
        evidence = (Join-Path $EvidenceRoot ("exit-" + $Tag))
        config = (Join-Path $BaseConfig $Tag); data = (Join-Path $BaseData $Tag) }
    $S.temp = Join-Path $S.data "temp"
    foreach ($Dir in @($S.evidence,$S.config,$S.data,$S.temp)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
    # Only the example configuration is read; no live credentials, proxy or firewall changes.
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
    return $S
}
function Launch-Shell($S, [string[]] $Arguments) {
    $env:TESSERACT_CONFIG_DIR = $S.config; $env:TESSERACT_DATA_DIR = $S.data; $env:TESSERACT_INSTANCE_SCOPE = $S.tag
    $env:TEMP = $S.temp; $env:TMP = $S.temp; $env:PYTHONDONTWRITEBYTECODE = "1"
    # Read-only use of the warm bytecode cache (the sidecar runs with -B and never writes it).
    $env:PYTHONPYCACHEPREFIX = $script:PyCache
    $Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $Listener.Start(); $script:Port = $Listener.LocalEndpoint.Port; $Listener.Stop()
    $env:TESSERACT_SHELL_CDP_PORT = "$script:Port"
    $script:ShellLog = Join-Path $S.data "logs/shell.log"
    $WatchArguments = @("-B",('"'+(Join-Path $Repo "shell/app/process_watch.py")+'"'),"--output",('"'+(Join-Path $S.evidence "process-inventory.json")+'"'),"--ready",('"'+(Join-Path $S.evidence "root.json")+'"'),"--",('"'+$Exe+'"')) + $Arguments
    $S.watcher = Start-Process -FilePath $Python -ArgumentList $WatchArguments -WorkingDirectory $Repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $S.evidence "watch-stdout.log") -RedirectStandardError (Join-Path $S.evidence "watch-stderr.log")
    $S.watcher_handle = $S.watcher.Handle
    Wait-Until { (Test-Path -LiteralPath (Join-Path $S.evidence "root.json")) -or $S.watcher.HasExited } "suspended job launch"
    $Root = Get-Content -LiteralPath (Join-Path $S.evidence "root.json") -Raw | ConvertFrom-Json
    $S.pid = [int]$Root.pid
    $S.shell = Get-Process -Id $S.pid; $S.shell_handle = $S.shell.Handle
}
function Start-Shell($S, [string[]] $Arguments) {
    Launch-Shell $S $Arguments
    # Fail fast when the start fails before hello (the pre-hello dialog would block otherwise).
    Wait-Until { if (Log-Has "sidecar_start_failed") { throw "sidecar_start_failed before hello" }; (Log-Has "shell_window_ready") -and (Log-Has "sidecar_ready") } "shell window + sidecar hello (window-first launch, 11-05)" 60
    Wait-Until { Log-Has "tray_icon_added" } "tray icon added" 10
    $S.tray = [P3Exit]::Find("TesseractShellTray", [NullString]::Value, [uint32]$S.pid)
    $S.main = [P3Exit]::Find([NullString]::Value, "Tesseract Shell (P3)", [uint32]$S.pid)
    if ($S.tray -eq [IntPtr]::Zero -or $S.main -eq [IntPtr]::Zero) { throw "tray or main window not found" }
    $Sidecars = Sidecar-Pids $S.pid
    if ($Sidecars.Count -ne 1) { throw "expected one sidecar child, found $($Sidecars.Count)" }
    $S.checks.sidecar_pid = $Sidecars[0]
    # T-P3-H: the residue identification (--user-data-dir under <data>\WebView2) finds the live WebView2.
    $S.checks.scoped_webviews_while_running = @(Scoped-WebViews $S.data).Count
    if ($S.checks.scoped_webviews_while_running -lt 1) { throw "scoped WebView2 identification found no live process" }
}
function Connect-Page($S) {
    Wait-Until { (Page-Eval "typeof window.devSubscribe") -eq "function" } "dev page script" 30
    [void](Page-Eval "(window.devSubscribe(), true)")
    Wait-Until { (Page-Eval "window.devFrames.some(f => f.t === 'hello')") -eq $true } "page subscribed" 15
}
# $Expected: the shell's own process exit code when it is not simply the sidecar's (forced quit).
function Finish-Shell($S, $Expected = $null) {
    if (-not $S.shell.WaitForExit(30000)) { throw "shell did not exit" }
    $S.shell.Refresh(); $S.checks.shell_exit_code = $S.shell.ExitCode
    # The shell exits with the exit code of the sidecar process it was quitting.
    $Last = @(Log-Lines | Where-Object { $_ -match ' sidecar_exit_(\d+)$' } | ForEach-Object { [int64]([regex]::Match($_,' sidecar_exit_(\d+)$').Groups[1].Value) })
    if ($null -ne $Expected) {
        if ([int64]$S.checks.shell_exit_code -ne [int64]$Expected) { throw "shell exit $($S.checks.shell_exit_code) != expected $Expected" }
    } else {
        if ($Last.Count -eq 0) { throw "sidecar exit code not logged" }
        $S.checks.sidecar_exit_code = $Last[-1]
        if ([int64]$S.checks.shell_exit_code -ne $S.checks.sidecar_exit_code) { throw "shell exit $($S.checks.shell_exit_code) != sidecar exit $($S.checks.sidecar_exit_code)" }
    }
    # The process exit code and the logged decision agree (M1: they used to differ, 0 vs N).
    if (-not (Log-Has "shell_exit_$($S.checks.shell_exit_code)")) { throw "shell_exit not logged" }
    if (-not (Log-Has "quit_exit_$($S.checks.shell_exit_code)") -and -not (Log-Has "sidecar_prehello_dialog")) { throw "quit_exit_$($S.checks.shell_exit_code) not logged" }
    foreach ($SidecarPid in @($S.checks.sidecar_pid, $S.checks.second_sidecar_pid)) {
        if ($SidecarPid -and (Get-Process -Id $SidecarPid -ErrorAction SilentlyContinue)) { throw "sidecar $SidecarPid still running" }
    }
    $Remaining = Wait-ScopedWebViewsGone $S.data $S.shell
    # T-P3-H2 (C3-8): residue-tree.json tells the shell's leftover from WebView2's own slow exit.
    if ($Remaining.Count -gt 0) { $S.checks.residue_class = Save-ResidueTree $Remaining $S.shell $script:ShellLog $S.evidence; throw "scoped WebView processes remain ($($S.checks.residue_class))" }
    if (-not $S.watcher.WaitForExit($InventoryWaitMs)) { throw (Inventory-TimeoutMessage (Join-Path $S.evidence "process-inventory.json")) }
    $S.watcher.Refresh()
    $Inventory = Get-Content -LiteralPath (Join-Path $S.evidence "process-inventory.json") -Raw | ConvertFrom-Json
    # process_watch's own `passed` means "root exited 0"; residue is judged by the job accounting.
    # process_watch exits 1 whenever the root exit code is not 0; only that combination is accepted.
    $WatchOk = ($S.watcher.ExitCode -eq 0) -or ($S.watcher.ExitCode -eq 1 -and [int64]$Inventory.root_exit_code -ne 0)
    if (-not $WatchOk -or -not $Inventory.kernel_membership_complete -or $Inventory.active_processes -ne 0) { throw "owned process residual detected" }
    if ([int64]$Inventory.root_exit_code -ne [int64]$S.checks.shell_exit_code) { throw "kernel-observed exit $($Inventory.root_exit_code) != $($S.checks.shell_exit_code)" }
    $S.checks.kernel_root_exit_code = $Inventory.root_exit_code
    $S.checks.no_residue = $true
}
function Expect-Log($S, [string[]] $Prefixes) {
    foreach ($Prefix in $Prefixes) { if (-not (Log-Has $Prefix)) { throw "shell.log lacks: $Prefix" } }
    $S.checks.log_codes = @($Prefixes)
}

function Run-TrayQuit($S) {
    Start-Shell $S @("--dev-rt"); Connect-Page $S
    [void][P3Exit]::PostMessageW($S.tray, $WM_COMMAND, [IntPtr]$CmdQuit, [IntPtr]::Zero)
    Wait-Until { [int](Page-Eval "(window.devFrames.find(f => f.t === 'host:confirmQuit') || {token: 0}).token") -gt 0 } "host:confirmQuit on the page" 10
    $Token = [int](Page-Eval "window.devFrames.find(f => f.t === 'host:confirmQuit').token")
    $S.checks.confirm_token = $Token
    [void](Page-Eval "(window.__TAURI__.core.invoke('host_confirm_quit', {token: $Token, unsaved: 0}), true)")
    Finish-Shell $S
    Expect-Log $S @("quit_requested_tray","quit_confirm_asked","quit_confirm_reply_0","quit_send_tray","quit_sent reason=user","quit_bye_received","bye exit_code=","quit_exit_")
    if (Log-Has "quit_confirm_timeout") { throw "the page answer did not end the confirmation" }
}
function Run-SidecarKill($S) {
    Start-Shell $S @("--dev-rt"); Connect-Page $S
    $First = Page-Eval "window.devFrames.filter(f => f.t === 'hello').map(f => f.epoch).join(',')"
    Stop-Process -Id $S.checks.sidecar_pid -Force
    Wait-Until { (Log-Has "sidecar_crashed exit_code=") -and (Log-Has "sidecar_ready pid=" 2) } "stopped-state relaunch" 30
    $Second = Sidecar-Pids $S.pid
    if ($Second.Count -ne 1 -or $Second[0] -eq $S.checks.sidecar_pid) { throw "no new sidecar process" }
    $S.checks.second_sidecar_pid = $Second[0]
    Wait-Until { (Page-Eval "window.devFrames.filter(f => f.t === 'host:banner' && f.state === 'restarted').length") -ge 1 } "restarted banner" 10
    $Epochs = @((Page-Eval "window.devFrames.filter(f => f.t === 'hello').map(f => f.epoch).join(',')") -split ',')
    $S.checks.epochs = $Epochs
    if ($Epochs.Count -lt 2 -or $Epochs[0] -ne $First -or $Epochs[-1] -eq $Epochs[0]) { throw "epoch did not change: $($Epochs -join ',')" }
    $NewEpoch = $Epochs[-1]
    if ((Page-Eval "window.devFrames.some(f => f.t === 'rt:reset' && f.epoch === '$NewEpoch')") -ne $true) { throw "no rt:reset for the new epoch" }
    $S.checks.banners = Page-Eval "window.devFrames.filter(f => f.t === 'host:banner').map(f => f.kind + ':' + f.state + ':' + f.restarts).join(',')"
    if ($S.checks.banners -notlike "*sidecar_crashed:restarting:1*" -or $S.checks.banners -notlike "*sidecar_crashed:restarted:1*") { throw "banners: $($S.checks.banners)" }
    # The relaunched sidecar is stopped: no service job was started for it.
    if ((Page-Eval "window.devFrames.some(f => f.topic === 'jobs' && f.epoch === '$NewEpoch' && f.data.name === 'service.start')") -eq $true) { throw "service started after relaunch" }
    # Quit again; this time the page stays silent, so the 3 s confirmation window expires.
    [void][P3Exit]::PostMessageW($S.tray, $WM_COMMAND, [IntPtr]$CmdQuit, [IntPtr]::Zero)
    Finish-Shell $S
    if ($S.checks.shell_exit_code -ne 0) { throw "exit after relaunch should be 0" }
    Expect-Log $S @("sidecar_crashed exit_code=","sidecar_ready pid=","quit_requested_tray","quit_confirm_asked","quit_confirm_timeout","quit_sent reason=user")
}
function Run-SessionEnd($S) {
    Start-Shell $S @()
    $S.checks.query_result = [P3Exit]::Send($S.main, $WM_QUERYENDSESSION, 0, $ENDSESSION_LOGOFF, 10000)
    if ($S.checks.query_result -ne 1) { throw "WM_QUERYENDSESSION must allow the session end: $($S.checks.query_result)" }
    $Started = [DateTime]::UtcNow
    $S.checks.end_result = [P3Exit]::Send($S.main, $WM_ENDSESSION, 1, $ENDSESSION_LOGOFF, 10000)
    $S.checks.endsession_ms = [int]([DateTime]::UtcNow - $Started).TotalMilliseconds
    if ($S.checks.end_result -ne 0) { throw "WM_ENDSESSION handler failed: $($S.checks.end_result)" }
    Finish-Shell $S
    Expect-Log $S @("session_end_query reason=logoff","session_end_block_reason_created","session_end_requested reason=logoff budget_ms=4000","session_end_sent reason=logoff budget_ms=4000",
        "session_end_reply sent=true reply=ok writes_revoked=true","session_end_result sent=true reply=ok writes_revoked=true","session_end_block_reason_destroyed","bye exit_code=")
    if (-not (Log-Has "session_end_result" ) -or (Log-Value 'session_end_result .* timeout=(\w+)') -ne "false") { throw "session end timed out" }
    if (Log-Has "quit_sent") { throw "a session end must not send app.quit" }
    $S.checks.recovery = Log-Value 'session_end_result .*(proxy=\S+ firewall=\S+)'
}
# A top-level dialog (#32770) of the scoped shell process, or zero.
function Shell-Dialog($S) { return [P3Exit]::Find("#32770", [NullString]::Value, [uint32]$S.pid) }
function Wait-Dialog($S, [int] $Seconds) {
    Wait-Until { $script:Dialog = Shell-Dialog $S; ($script:Dialog -ne [IntPtr]::Zero) -and [P3Exit]::IsWindowVisible($script:Dialog) } "visible dialog" $Seconds
    $S.checks.dialog_visible = [P3Exit]::IsWindowVisible($script:Dialog)
    try { Wait-Until { [P3Exit]::Topmost($script:Dialog) } "topmost dialog" 2 } catch { }
    $S.checks.dialog_topmost = [P3Exit]::Topmost($script:Dialog)
    if (-not $S.checks.dialog_topmost) { throw "dialog is not topmost" }
    return $script:Dialog
}
function Suspend-Sidecar($S) {
    if (-not [P3Exit]::Suspend([uint32]$S.checks.sidecar_pid)) { throw "could not suspend the test sidecar" }
    $S.suspended = $S.checks.sidecar_pid; $S.checks.sidecar_suspended = $true
}
function Run-QuitNoBye($S) {
    # No dev page: no confirmation round, app.quit goes out at once.
    Start-Shell $S @()
    Suspend-Sidecar $S
    [void][P3Exit]::PostMessageW($S.tray, $WM_COMMAND, [IntPtr]$CmdQuit, [IntPtr]::Zero)
    Wait-Until { Log-Has "quit_sent reason=user" } "quit_sent" 10
    if (-not [P3Exit]::Terminate([uint32]$S.checks.sidecar_pid, 3)) { throw "could not terminate the test sidecar" }
    $S.suspended = $null
    Finish-Shell $S 3
    Expect-Log $S @("quit_sent reason=user","sidecar_exit_3","bye_missing","quit_exit_without_bye","quit_exit_3","shell_exit_3")
    if (Log-Has "quit_bye_received") { throw "the terminated sidecar cannot have said bye" }
}
function Run-QuitStallForce($S) {
    Start-Shell $S @()
    Suspend-Sidecar $S
    [void][P3Exit]::PostMessageW($S.tray, $WM_COMMAND, [IntPtr]$CmdQuit, [IntPtr]::Zero)
    Wait-Until { Log-Has "quit_sent reason=user" } "quit_sent" 10
    $Sent = [DateTime]::UtcNow
    Wait-Until { Log-Has "quit_stall_prompt" } "20 s stall prompt" 30
    $S.checks.stall_prompt_after_s = [math]::Round(([DateTime]::UtcNow - $Sent).TotalSeconds, 1)
    if ($S.checks.stall_prompt_after_s -lt 19) { throw "stall prompt came too early: $($S.checks.stall_prompt_after_s) s" }
    $Dialog = Wait-Dialog $S 5
    # The task dialog's own button message: "force quit (network settings may not be restored)".
    [void][P3Exit]::Send($Dialog, $TDM_CLICK_BUTTON, $ID_FORCE, 0, 5000)
    Finish-Shell $S 1
    $S.suspended = $null
    Expect-Log $S @("quit_stall_prompt","quit_forced_by_user","sidecar_force_killed","quit_exit_1","shell_exit_1")
}
function Run-PreHello($S) {
    # whoami.exe never speaks the bridge protocol: the start fails before hello. process_watch
    # starts the shell with STARTUPINFO SW_HIDE, the case that used to hide the dialog.
    New-Item -ItemType Directory -Force -Path (Join-Path $S.data "logs") | Out-Null
    [IO.File]::WriteAllText((Join-Path $S.data "logs/sidecar-stderr.log"), "boot`r`nyaml error at line 3: password: hunter2-smoke`r`n", (New-Object Text.UTF8Encoding $false))
    $script:ShellLog = Join-Path $S.data "logs/shell.log"
    Launch-Shell $S @("--sidecar", (Join-Path $env:SystemRoot "System32\whoami.exe"))
    Wait-Until { Log-Has "sidecar_prehello_dialog" } "pre-hello dialog logged" 30
    $Dialog = Wait-Dialog $S 5
    $S.checks.prehello_kind = Log-Value 'sidecar_prehello_dialog kind=(\w+)'
    if ($S.checks.prehello_kind -ne "exited") { throw "wording kind: $($S.checks.prehello_kind)" }
    [void][P3Exit]::PostMessageW($Dialog, $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero)
    Finish-Shell $S 1
    Expect-Log $S @("sidecar_start_failed","sidecar_prehello_dialog kind=exited","sidecar_prehello_dialog_closed","shell_exit_1")
    if (@(Log-Lines | Where-Object { $_.Contains("hunter2") }).Count -gt 0) { throw "stderr text reached shell.log" }
}
# Failure cleanup for one scoped test shell: close its dialogs, let a suspended sidecar run again,
# ask the tray to quit, and stop only the scoped test shell process if it still will not exit.
function Clean-Up($S) {
    $Steps = @()
    if (-not $S.shell) {
        # T-P3-H2 fix 1: the launch gave up ("timeout: suspended job launch") before root.json
        # appeared, but the watcher went on and started the shell, which then sat in its pre-hello
        # box on the desktop (reviewA1 sidecar-kill). Pick it up from root.json (pid + creation time).
        $RootFile = Join-Path $S.evidence "root.json"
        if ($S.watcher) {
            $Until = [DateTime]::UtcNow.AddSeconds(30)
            while (-not (Test-Path -LiteralPath $RootFile) -and -not $S.watcher.HasExited -and [DateTime]::UtcNow -lt $Until) { Start-Sleep -Milliseconds 200 }
        }
        if (Test-Path -LiteralPath $RootFile) {
            try {
                $Root = Get-Content -LiteralPath $RootFile -Raw | ConvertFrom-Json
                $Late = Get-Process -Id ([int]$Root.pid) -ErrorAction Stop
                if ([Math]::Abs($Late.StartTime.ToFileTimeUtc() - [long]$Root.created_filetime) -lt 10000000) {
                    $S.pid = [int]$Root.pid; $S.shell = $Late; $S.shell_handle = $Late.Handle; $Steps += "late_root_pid=$($S.pid)"
                }
            } catch { }
        }
    }
    if (-not $S.shell -or $S.shell.HasExited) { return $Steps }
    if ($S.suspended) { $Steps += "resume_sidecar=" + [P3Exit]::Resume([uint32]$S.suspended) }
    for ($i = 0; $i -lt 3; $i++) {
        $D = Shell-Dialog $S
        if ($D -eq [IntPtr]::Zero) { break }
        [void][P3Exit]::PostMessageW($D, $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero); $Steps += "dialog_wm_close"; Start-Sleep -Milliseconds 300
    }
    if ($S.tray -and $S.tray -ne [IntPtr]::Zero -and -not $S.shell.HasExited) {
        [void][P3Exit]::PostMessageW($S.tray, $WM_COMMAND, [IntPtr]$CmdQuit, [IntPtr]::Zero); $Steps += "tray_quit"
    }
    if (-not $S.shell.WaitForExit(30000)) {
        # A later stall dialog may have opened meanwhile; close it once more before stopping.
        $D = Shell-Dialog $S
        if ($D -ne [IntPtr]::Zero) { [void][P3Exit]::PostMessageW($D, $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero); $Steps += "dialog_wm_close_late" }
        if (-not $S.shell.WaitForExit(5000)) { Stop-Process -Id $S.pid -Force -ErrorAction SilentlyContinue; $Steps += "stop_scoped_shell" }
    }
    $Steps += "exited=" + $S.shell.HasExited
    return $Steps
}

$Names = @("TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR","TESSERACT_INSTANCE_SCOPE","TESSERACT_ISOLATED_AUDIT_MANIFEST",
    "TESSERACT_SHELL_CDP_PORT","TESSERACT_SHELL_DEV_MARK_QUITTING","PYTHONPATH","PYTHONDONTWRITEBYTECODE","PYTHONPYCACHEPREFIX","TEMP","TMP")
$Previous = @{}
foreach ($Name in $Names) { $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
# Warm-up (review: cold-start hello_timeout). The sidecar runs with -B, so with no bytecode cache
# every launch compiles desktop_runtime.py (~2 MB) again; under load that alone passes the 10 s
# hello window. One import here writes the bytecode into a temporary PYTHONPYCACHEPREFIX (never
# into the source tree); every scenario's sidecar then reads it.
$script:PyCache = Join-Path $BaseData "pycache-warm"
New-Item -ItemType Directory -Force -Path $script:PyCache | Out-Null
$env:PYTHONPYCACHEPREFIX = $script:PyCache; Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
$WarmStart = [DateTime]::UtcNow
Push-Location $Repo; try { & $Python -c "import bridge_main, desktop_runtime" } finally { Pop-Location }
$WarmCode = $LASTEXITCODE
foreach ($Var in $Names) { [Environment]::SetEnvironmentVariable($Var,$Previous[$Var],"Process") }
Write-Output ("warm-up import rc={0} {1:N1} s (bytecode cache {2})" -f $WarmCode, ([DateTime]::UtcNow - $WarmStart).TotalSeconds, $script:PyCache)
$Results = @(); $AllPassed = $true
$Queue = [Collections.Generic.Queue[object]]::new()
foreach ($Name in $Scenarios) { $Queue.Enqueue(@($Name, 1)) }
while ($Queue.Count -gt 0) {
    $Item = $Queue.Dequeue(); $Name = $Item[0]; $Attempt = $Item[1]
    $S = New-Scenario $Name; $S.checks = [ordered]@{}; $Passed = $false; $Failure = $null; $Cleanup = @()
    try {
        Remove-Item Env:TESSERACT_ISOLATED_AUDIT_MANIFEST -ErrorAction SilentlyContinue
        Remove-Item Env:TESSERACT_SHELL_DEV_MARK_QUITTING -ErrorAction SilentlyContinue
        switch ($Name) {
            "tray-quit" { Run-TrayQuit $S }
            "sidecar-kill" { Run-SidecarKill $S }
            "session-end" { Run-SessionEnd $S }
            "quit-no-bye" { Run-QuitNoBye $S }
            "quit-stall-force" { Run-QuitStallForce $S }
            "prehello" { Run-PreHello $S }
            default { throw "unknown scenario $Name" }
        }
        $Passed = $true
    } catch {
        $Failure = $_.Exception.Message
    } finally {
        # On failure: dialogs closed, tray quit (the real exit path), the scoped test shell
        # stopped only as the last step; the sidecar's own runtime/network owner is never killed.
        if (-not $Passed) { $Cleanup = @(Clean-Up $S) }
        if ($S.watcher -and -not $S.watcher.HasExited) { [void]$S.watcher.WaitForExit($InventoryWaitMs) }
        foreach ($Var in $Names) { [Environment]::SetEnvironmentVariable($Var,$Previous[$Var],"Process") }
        if ($script:ShellLog -and (Test-Path -LiteralPath $script:ShellLog)) { Copy-Item -LiteralPath $script:ShellLog -Destination (Join-Path $S.evidence "shell.log") }
        $Result = [ordered]@{ scenario = $Name; attempt = $Attempt; passed = $Passed; error = $Failure; cleanup = $Cleanup; scope = $S.tag; data = $S.data; checks = $S.checks }
        [IO.File]::WriteAllText((Join-Path $S.evidence "result.json"), ($Result | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
        $Results += $Result
        $PreHelloFailure = $script:ShellLog -and ((Log-Has "hello_timeout") -or (Log-Has "sidecar_start_failed")) -and $Name -ne "prehello"
        $script:ShellLog = $null
    }
    # A cold machine can miss the 10 s hello window once (review S3); retry such a start once.
    if (-not $Passed -and $PreHelloFailure -and $Attempt -eq 1) {
        Write-Output "retrying $Name after a failure before hello: $Failure (evidence: $($S.evidence))"
        $Queue.Enqueue(@($Name, 2)); continue
    }
    if (-not $Passed) { $AllPassed = $false; Write-Output "FAILED $Name : $Failure (evidence: $($S.evidence))" }
    else { Write-Output "passed $Name (evidence: $($S.evidence))" }
}
if (-not $AllPassed) { throw "P3-04 exit smoke failed" }
Write-Output "P3-04 exit smoke passed: $($Scenarios -join ', ')"

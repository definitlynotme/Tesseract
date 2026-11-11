[CmdletBinding()]
param([string[]] $Scenarios = @("main","install-failure","autostart-tray","autostart-window"))
# T-P3-06 host commands / save grant / update hand-off / autostart smoke, custom scope only (P3-02
# guard). The debug shell runs the scripted stand-in sidecar shell/app/src/host_fixture.py
# (`--dev-sidecar-script`, debug builds only). Scenarios (each in its own scope):
#  main             host.openFolder opens exactly the mapped scope folders config, data, logs and
#                   <data>/exports, nothing outside the scope (Explorer windows are
#                   checked and closed) and refuses anything else; host.openUrl refuses non-whitelisted
#                   links and shows the native question for an allowed one (closed = not opened, no
#                   browser starts); host.saveFile: the native save dialog is cancelled (no grant), then
#                   confirmed with a typed path (shell.saveGrant registered; the fixture writes through
#                   the duplicated handle; the page only holds the ref); host.reveal refuses the ref
#                   until the export job reported it, then selects the file in Explorer; the update
#                   hand-off with a wrong sha256 is declined (shell keeps running), with the right one
#                   it is accepted, app.quit{reason:update} follows, and the fake installer (a small
#                   program compiled here that writes a marker file) starts with the args; shell exit 0.
#  install-failure  a process left running under the (debug) install root makes the install fail after
#                   the residual wait: the window is restored from the tray, the sidecar is relaunched
#                   stopped and the new epoch receives update.installResult{launched:false}.
#  autostart-tray   --autostart with ui.start_in_tray_on_autostart=true: the window stays hidden (tray).
#  autostart-window --autostart with the setting false: the window shows.
# Evidence: the work-order directory evidence/T-P3-06/host-<run>/. The caller builds; this script
# never builds.
$ErrorActionPreference = "Stop"
# T-P3-H2 fix 1: $InventoryWaitMs and Inventory-TimeoutMessage (process_watch observes a residue 20 s).
. (Join-Path $PSScriptRoot "p3_residue.ps1")
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
$Python = Join-Path $Repo ".venv/Scripts/python.exe"
$Exe = Join-Path $Repo "shell/target/debug/tesseract-shell.exe"
$Fixture = Join-Path $Repo "shell/app/src/host_fixture.py"
if (-not (Test-Path -LiteralPath $Exe)) { throw "debug shell missing; build it first" }
# The work-order directory name is not ASCII; locate it by the ticket file instead of spelling it here.
$Orders = Get-ChildItem -LiteralPath (Join-Path $Repo ".build") -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "T-P3-06.md") } | Select-Object -First 1
if (-not $Orders) { throw "work-order directory not found" }
$BaseConfig = $env:TESSERACT_CONFIG_DIR; $BaseData = $env:TESSERACT_DATA_DIR
$Run = "host-" + [Guid]::NewGuid().ToString("N").Substring(0,12)
# TESSERACT_P3_EVIDENCE_ROOT (T-P3-H): a later ticket re-running this smoke keeps its evidence in its own directory.
$EvidenceRoot = if ($env:TESSERACT_P3_EVIDENCE_ROOT) { $env:TESSERACT_P3_EVIDENCE_ROOT } else { Join-Path $Orders.FullName "evidence/T-P3-06" }
$Evidence = Join-Path $EvidenceRoot $Run
New-Item -ItemType Directory -Force -Path $Evidence | Out-Null

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.WebSockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
public static class P3Host {
    delegate bool EnumProc(IntPtr hwnd, IntPtr l);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindowExW(IntPtr parent, IntPtr after, string cls, string title);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr SendMessageW(IntPtr hwnd, uint msg, IntPtr w, string l);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumProc proc, IntPtr l);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetClassNameW(IntPtr hwnd, StringBuilder name, int max);
    [DllImport("user32.dll")] static extern IntPtr GetParent(IntPtr hwnd);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern uint GetLongPathNameW(string shortPath, StringBuilder longPath, uint size);
    public static string LongPath(string path) { StringBuilder s = new StringBuilder(1024); uint n = GetLongPathNameW(path, s, 1024); return n > 0 && n < 1024 ? s.ToString() : path; }
    static string Class(IntPtr hwnd) { StringBuilder s = new StringBuilder(256); GetClassNameW(hwnd, s, 256); return s.ToString(); }
    public static IntPtr Find(string cls, string title, uint pid) {
        IntPtr after = IntPtr.Zero;
        while (true) {
            after = FindWindowExW(IntPtr.Zero, after, cls, title);
            if (after == IntPtr.Zero) return IntPtr.Zero;
            uint owner; GetWindowThreadProcessId(after, out owner);
            if (owner == pid) return after;
        }
    }
    // A visible top-level window of `cls` owned by `pid` (dialogs of the scoped shell).
    public static IntPtr FindVisible(string cls, uint pid) {
        IntPtr after = IntPtr.Zero;
        while (true) {
            after = FindWindowExW(IntPtr.Zero, after, cls, null);
            if (after == IntPtr.Zero) return IntPtr.Zero;
            uint owner; GetWindowThreadProcessId(after, out owner);
            if (owner == pid && IsWindowVisible(after)) return after;
        }
    }
    // The file-name box of the common save dialog: an Edit inside a ComboBox.
    public static IntPtr FileNameEdit(IntPtr dialog) {
        IntPtr found = IntPtr.Zero; IntPtr fallback = IntPtr.Zero;
        EnumChildWindows(dialog, (h, l) => {
            if (Class(h) == "Edit" && IsWindowVisible(h)) {
                if (Class(GetParent(h)) == "ComboBox") { found = h; return false; }
                if (fallback == IntPtr.Zero) fallback = h;
            }
            return true;
        }, IntPtr.Zero);
        return found != IntPtr.Zero ? found : fallback;
    }
    public static void SetText(IntPtr hwnd, string text) { SendMessageW(hwnd, 0x000C, IntPtr.Zero, text); }
    public static string Http(string url) {
        HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url); request.Proxy = null; request.Timeout = 3000;
        using (WebResponse response = request.GetResponse()) using (StreamReader reader = new StreamReader(response.GetResponseStream())) return reader.ReadToEnd();
    }
    // One fixed Runtime.evaluate; expressions come from this script only (no double quotes or backslashes).
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

$WM_COMMAND = 0x111; $CmdQuit = 2; $WM_CLOSE = 0x10; $IDOK = 1; $IDCANCEL = 2
$Utf8 = New-Object Text.UTF8Encoding $false
function Wait-Until([scriptblock] $Condition, [string] $Label, [int] $Seconds = 15) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while (-not (& $Condition)) {
        if ([DateTime]::UtcNow -gt $Deadline) { throw "timeout: $Label" }
        Start-Sleep -Milliseconds 100
    }
}
function Log-Lines { if (Test-Path -LiteralPath $script:C.log) { return @(Get-Content -LiteralPath $script:C.log) } else { return @() } }
function Log-Has([string] $Prefix) { return @(Log-Lines | Where-Object { ($_ -replace '^\d+ ','').StartsWith($Prefix) }).Count -ge 1 }
function Page-Socket {
    try { $Pages = [P3Host]::Http("http://127.0.0.1:$($script:C.port)/json/list") | ConvertFrom-Json } catch { return $null }
    $Page = @($Pages | Where-Object { $_.type -eq "page" -and $_.url -like "*dev-rt.html*" }) | Select-Object -First 1
    if ($Page) { return $Page.webSocketDebuggerUrl } else { return $null }
}
function Page-Eval([string] $Expression) {
    $Socket = Page-Socket
    if (-not $Socket) { return $null }
    try { $Reply = [P3Host]::Evaluate($Socket, $Expression) | ConvertFrom-Json } catch { return $null }
    return $Reply.result.result.value
}
function Page-Count([string] $Filter) { return [int](Page-Eval "window.devFrames.filter(f => $Filter).length") }
$script:Req = 0
# `rt` with a host command from the dev page; returns the req_id whose res arrives on the Channel.
function Rt([string] $Cmd, [string] $ArgsJs) {
    $script:Req++; $Id = "p306-$($script:Req)"
    $Key = "__p306_" + $script:Req
    [void](Page-Eval "(window.$Key = null, window.__TAURI__.core.invoke('rt', {req_id: '$Id', cmd: '$Cmd', args: $ArgsJs, owner: 'dev-rt'}).then(r => window.$Key = 'ok:' + JSON.stringify(r), e => window.$Key = 'err:' + String(e)), true)")
    Wait-Until { $null -ne (Page-Eval "window.$Key") } "invoke $Cmd" 10
    $Queued = Page-Eval "window.$Key"
    if ($Queued -notlike 'ok:*"queued":true*') { throw "rt $Cmd not queued: $Queued" }
    return $Id
}
function Res([string] $Id, [int] $Seconds = 15) {
    Wait-Until { (Page-Count "f.t === 'res' && f.req_id === '$Id'") -ge 1 } "res $Id" $Seconds
    return (Page-Eval "JSON.stringify(window.devFrames.find(f => f.t === 'res' && f.req_id === '$Id'))") | ConvertFrom-Json
}
function Expect-Error([string] $Cmd, [string] $ArgsJs, [string] $Code) {
    $R = Res (Rt $Cmd $ArgsJs)
    if ($R.ok -or $R.error.code -ne $Code) { throw "$Cmd $ArgsJs expected $Code, got $($R | ConvertTo-Json -Compress -Depth 5)" }
    return "$Code/$($R.error.reason_code)"
}
function Fixture-Frames([int] $Launch = 1) {
    $Path = Join-Path $script:C.state "frames-$Launch.jsonl"
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    return @(Get-Content -LiteralPath $Path | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
}
function Sent([string] $Cmd, [int] $Launch = 1) { return @(Fixture-Frames $Launch | Where-Object { $_.cmd -eq $Cmd }) }
function Push-Event($Spec) {
    $script:C.push++
    $Dir = Join-Path $script:C.state "push"; New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $Name = "{0:D3}" -f $script:C.push
    $Tmp = Join-Path $Dir "$Name.tmp"
    [IO.File]::WriteAllText($Tmp, ($Spec | ConvertTo-Json -Depth 6 -Compress), $Utf8)
    Move-Item -LiteralPath $Tmp -Destination (Join-Path $Dir "$Name.json")
    Wait-Until { Test-Path -LiteralPath (Join-Path $Dir "$Name.sent") } "fixture sent $Name" 10
}
function Long([string] $Path) { return [P3Host]::LongPath([IO.Path]::GetFullPath($Path)).TrimEnd('\') }
function Explorer-Windows {
    $Shell = New-Object -ComObject Shell.Application
    return @($Shell.Windows() | ForEach-Object { try { if ($_.FullName -like '*explorer.exe') { [pscustomobject]@{ path = [string]$_.Document.Folder.Self.Path; window = $_ } } } catch { } })
}
function Explorer-Paths { return @(Explorer-Windows | ForEach-Object { $_.path }) }
function Close-Explorer([string[]] $Paths) {
    foreach ($W in (Explorer-Windows)) { if ($Paths -contains $W.path) { try { $W.window.Quit() } catch { } } }
}
function Visible-Dialog { return [P3Host]::FindVisible("#32770", [uint32]$script:C.pid) }
function Build-FakeInstaller([string] $Out) {
    $Csc = Join-Path $env:WINDIR "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if (-not (Test-Path -LiteralPath $Csc)) { throw "csc.exe (.NET Framework 4) not found" }
    $Source = Join-Path (Split-Path $Out) "fake_installer.cs"
    # A stand-in "installer": writes its args and own path to $env:TESSERACT_FAKE_INSTALLER_MARKER;
    # /HOLD keeps it running (a residual process for the install-failure scenario).
    [IO.File]::WriteAllText($Source, @'
using System; using System.IO; using System.Threading;
class FakeInstaller { static int Main(string[] a) {
    if (Array.IndexOf(a, "/HOLD") >= 0) { Thread.Sleep(180000); return 0; }
    string marker = Environment.GetEnvironmentVariable("TESSERACT_FAKE_INSTALLER_MARKER");
    if (!String.IsNullOrEmpty(marker)) File.WriteAllText(marker, String.Join(" ", a) + "\n" + System.Reflection.Assembly.GetExecutingAssembly().Location);
    return 0; } }
'@, $Utf8)
    & $Csc /nologo /target:winexe ("/out:" + $Out) $Source | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Out)) { throw "fake installer build failed" }
}
function Handoff([string] $Id, [string] $Sha) {
    return [ordered]@{ topic = "update.prepareExit"; kind = "edge"; data = [ordered]@{ handoff_id = $Id; receipt_ref = "receipt-smoke"; version = "9.9.9"; verified = $true
        expires_at_ms = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + 60000); installer_path = $script:C.installer; sha256 = $Sha
        args = @("/SILENT","/SUPPRESSMSGBOXES","/NORESTART") } }
}

$Names = @("TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR","TESSERACT_INSTANCE_SCOPE","TESSERACT_ISOLATED_AUDIT_MANIFEST","TESSERACT_SHELL_CDP_PORT","PYTHONDONTWRITEBYTECODE","TEMP","TMP",
    "TESSERACT_SHELL_DEV_INSTALL_ROOT","TESSERACT_FAKE_INSTALLER_MARKER")
$Previous = @{}
foreach ($Name in $Names) { $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
$FakeInstaller = Join-Path $Evidence "Tesseract-Setup-9.9.9-win-x64.exe"

function Start-Shell([string] $Name, [string[]] $Extra, [scriptblock] $Prepare) {
    $Tag = "p3-06-$Name-" + [Guid]::NewGuid().ToString("N").Substring(0,10)
    $C = [ordered]@{ name = $Name; tag = $Tag; evidence = (Join-Path $Evidence $Name); config = [IO.Path]::GetFullPath((Join-Path $BaseConfig $Tag)); data = [IO.Path]::GetFullPath((Join-Path $BaseData $Tag)); push = 0; checks = [ordered]@{} }
    $C.temp = Join-Path $C.data "temp"; $C.state = Join-Path $C.data "host-fixture"; $C.log = Join-Path $C.data "logs/shell.log"
    $C.root = Join-Path $C.data "install-root"; $C.marker = Join-Path $C.data "installer-marker.txt"
    foreach ($Dir in @($C.evidence,$C.config,$C.data,$C.temp,$C.state,$C.root)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
    $script:C = $C
    if ($Prepare) { & $Prepare }
    $env:TESSERACT_CONFIG_DIR = $C.config; $env:TESSERACT_DATA_DIR = $C.data; $env:TESSERACT_INSTANCE_SCOPE = $C.tag
    $env:TEMP = $C.temp; $env:TMP = $C.temp; $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:TESSERACT_SHELL_DEV_INSTALL_ROOT = $C.root; $env:TESSERACT_FAKE_INSTALLER_MARKER = $C.marker
    Remove-Item Env:TESSERACT_ISOLATED_AUDIT_MANIFEST -ErrorAction SilentlyContinue
    $Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $Listener.Start(); $C.port = $Listener.LocalEndpoint.Port; $Listener.Stop()
    $env:TESSERACT_SHELL_CDP_PORT = "$($C.port)"
    $WatchArguments = @("-B",('"'+(Join-Path $Repo "shell/app/process_watch.py")+'"'),"--output",('"'+(Join-Path $C.evidence "process-inventory.json")+'"'),"--ready",('"'+(Join-Path $C.evidence "root.json")+'"'),"--",('"'+$Exe+'"'),"--dev-rt","--dev-sidecar-script",('"'+$Fixture+'"')) + $Extra
    $C.watcher = Start-Process -FilePath $Python -ArgumentList $WatchArguments -WorkingDirectory $Repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $C.evidence "watch-stdout.log") -RedirectStandardError (Join-Path $C.evidence "watch-stderr.log")
    $C.watcher_handle = $C.watcher.Handle
    Wait-Until { (Test-Path -LiteralPath (Join-Path $C.evidence "root.json")) -or $C.watcher.HasExited } "suspended job launch"
    $C.pid = [int]((Get-Content -LiteralPath (Join-Path $C.evidence "root.json") -Raw | ConvertFrom-Json).pid)
    $C.shell = Get-Process -Id $C.pid; $C.shell_handle = $C.shell.Handle
    Wait-Until { if (Log-Has "sidecar_start_failed") { throw "sidecar_start_failed before hello" }; (Log-Has "shell_window_ready") -and (Log-Has "sidecar_ready") } "shell window + sidecar hello (window-first launch, 11-05)" 60
    Wait-Until { Log-Has "tray_icon_added" } "tray icon added" 10
    Wait-Until { Test-Path -LiteralPath (Join-Path $C.state "ready-1") } "fixture ready" 10
    $C.tray = [P3Host]::Find("TesseractShellTray", [NullString]::Value, [uint32]$C.pid)
    $C.main = [P3Host]::Find([NullString]::Value, "Tesseract Shell (P3)", [uint32]$C.pid)
    if ($C.tray -eq [IntPtr]::Zero -or $C.main -eq [IntPtr]::Zero) { throw "tray or main window not found" }
    return $C
}
function Subscribe-Page {
    Wait-Until { (Page-Eval "typeof window.devSubscribe") -eq "function" } "dev page script" 30
    [void](Page-Eval "(window.devSubscribe(), true)")
    Wait-Until { (Page-Count "f.t === 'hello'") -ge 1 } "page subscribed" 15
    $script:C.subscribed = $true
}
function Quit-Tray {
    [void][P3Host]::PostMessageW($script:C.tray, $WM_COMMAND, [IntPtr]$CmdQuit, [IntPtr]::Zero)
    if ($script:C.subscribed) {
        Wait-Until { (Page-Count "f.t === 'host:confirmQuit'") -ge 1 } "host:confirmQuit" 10
        $Token = [int](Page-Eval "window.devFrames.filter(f => f.t === 'host:confirmQuit').pop().token")
        [void](Page-Eval "(window.__TAURI__.core.invoke('host_confirm_quit', {token: $Token, unsaved: 0}), true)")
    }
}
function Finish-Shell([int] $Expected = 0) {
    $C = $script:C
    if (-not $C.shell.WaitForExit(30000)) { throw "shell did not exit" }
    $C.shell.Refresh(); $C.checks.shell_exit_code = $C.shell.ExitCode
    if ($C.checks.shell_exit_code -ne $Expected) { throw "shell exit $($C.checks.shell_exit_code), expected $Expected" }
    if (-not $C.watcher.WaitForExit($InventoryWaitMs)) { throw (Inventory-TimeoutMessage (Join-Path $C.evidence "process-inventory.json")) }
    $Inventory = Get-Content -LiteralPath (Join-Path $C.evidence "process-inventory.json") -Raw | ConvertFrom-Json
    if (-not $Inventory.kernel_membership_complete) { throw "process inventory incomplete" }
    # Decision P3-06 item 6 (T-P3-H): process_watch itself gives this scope's msedgewebview2 (job
    # member, --user-data-dir under <data>\WebView2) 20 s after the shell exits and fails any other
    # descendant at once; the former local 10 + 30 s tolerance is gone, so all p3 smokes share one rule.
    if ($Inventory.active_processes -ne 0) { throw "owned process residual detected: $(@($Inventory.residual | ForEach-Object { [IO.Path]::GetFileName($_.image) }) -join ',') after $($Inventory.residual_after_root_s) s" }
    if ($Inventory.webview_grace_used) { $C.checks.webview_late_exit_s = $Inventory.last_exit_after_root_s }
    $C.checks.no_residue = $true
}
function Cleanup-Shell {
    $C = $script:C; $Steps = @()
    if (-not $C) { return $Steps }
    # T-P3-H2 fix 3: a launch that gave up never learned its shell; end one the watcher started late.
    if (-not $C.shell) { $Late = Resolve-LateShell (Join-Path $C.evidence "root.json") $C.watcher; if ($Late) { $Steps += Close-LateShell $Late $C.evidence } }
    if ($C.shell -and -not $C.shell.HasExited) {
        $Dialog = Visible-Dialog
        if ($Dialog -ne [IntPtr]::Zero) { [void][P3Host]::PostMessageW($Dialog, $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero); $Steps += "dialog_closed"; Start-Sleep -Milliseconds 500 }
        if ($C.tray -and $C.tray -ne [IntPtr]::Zero) { try { Quit-Tray; $Steps += "tray_quit" } catch { $Steps += "tray_quit_unanswered" } }
        if (-not $C.shell.WaitForExit(30000)) { Stop-Process -Id $C.pid -Force -ErrorAction SilentlyContinue; $Steps += "stop_scoped_shell" }
        $Steps += "exited=" + $C.shell.HasExited
    }
    if ($C.watcher -and -not $C.watcher.HasExited) { [void]$C.watcher.WaitForExit($InventoryWaitMs) }
    if ($C.holder -and -not $C.holder.HasExited) { Stop-Process -Id $C.holder.Id -Force -ErrorAction SilentlyContinue; $Steps += "residual_stopped" }
    if (Test-Path -LiteralPath $C.log) { Copy-Item -LiteralPath $C.log -Destination (Join-Path $C.evidence "shell.log") }
    foreach ($File in @(Get-ChildItem -LiteralPath $C.state -Filter "*.jsonl" -ErrorAction SilentlyContinue)) { Copy-Item -LiteralPath $File.FullName -Destination (Join-Path $C.evidence ("fixture-" + $File.Name)) }
    return $Steps
}

function Scenario-Main {
    $C = Start-Shell "main" @() $null
    Subscribe-Page
    # 1. host.openFolder: exactly the mapped folders; anything else refused.
    $Before = Explorer-Paths
    # All four kinds (review S3): each is a directory of this scope; exports is <data>/exports.
    $Expected = @{ logs = (Long (Join-Path $C.data "logs")); config = (Long $C.config); data = (Long $C.data); exports = ((Long $C.data) + "\exports") }
    foreach ($Kind in @("logs","config","data","exports")) {
        $R = Res (Rt "host.openFolder" "{kind: '$Kind'}")
        if (-not $R.ok -or -not $R.data.opened) { throw "openFolder $Kind failed: $($R | ConvertTo-Json -Compress -Depth 5)" }
        Wait-Until { (Explorer-Paths) -contains $Expected[$Kind] } "explorer at $Kind" 15
    }
    $C.checks.open_folder_refused = @(
        (Expect-Error "host.openFolder" "{kind: '../logs'}" "not_allowed"),
        (Expect-Error "host.openFolder" "{kind: 'C:/Windows'}" "not_allowed"),
        (Expect-Error "host.openFolder" "{kind: 'logs', path: 'C:/'}" "not_allowed"),
        (Expect-Error "host.openFolder" "{}" "not_allowed"))
    Start-Sleep -Milliseconds 1500
    $New = @(Explorer-Paths | Where-Object { $Before -notcontains $_ })
    $C.checks.open_folder_windows = $New
    foreach ($Path in $New) { if (@($Expected.Values) -notcontains $Path) { throw "an Explorer window outside the mapped folders opened: $Path" } }
    Close-Explorer @($Expected.Values)
    # 2. host.openUrl: refused links never ask; an allowed one asks natively (closed = not opened).
    $C.checks.open_url_refused = @(
        (Expect-Error "host.openUrl" "{url: 'http://github.com/'}" "not_allowed"),
        (Expect-Error "host.openUrl" "{url: 'javascript:alert(1)'}" "not_allowed"),
        (Expect-Error "host.openUrl" "{url: 'https://github.com.evil.example/'}" "not_allowed"),
        (Expect-Error "host.openUrl" ("{url: 'https://user" + "@github.com/'}") "not_allowed"),
        (Expect-Error "host.openUrl" "{url: 'https://github.com:8443/'}" "not_allowed"),
        (Expect-Error "host.openUrl" "{url: 'https://example.com/'}" "not_allowed"))
    if ((Visible-Dialog) -ne [IntPtr]::Zero) { throw "a refused link opened a dialog" }
    $Id = Rt "host.openUrl" "{url: 'https://github.com/definitlynotme/Tessract/releases'}"
    Wait-Until { (Visible-Dialog) -ne [IntPtr]::Zero } "open-url question" 10
    [void][P3Host]::PostMessageW((Visible-Dialog), $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero)
    $R = Res $Id
    if (-not $R.ok -or $R.data.opened -ne $false) { throw "declined link: $($R | ConvertTo-Json -Compress -Depth 5)" }
    if (-not (Log-Has "host_open_url host=github.com result=declined")) { throw "declined link not logged by host" }
    $C.checks.open_url_declined = "github.com"
    # 3. host.saveFile: cancel (no grant), then confirm a typed path (grant registered, ref only).
    $Id = Rt "host.saveFile" "{purpose: 'filtered', format: 'json'}"
    Wait-Until { (Visible-Dialog) -ne [IntPtr]::Zero } "save dialog (cancel)" 15
    Start-Sleep -Milliseconds 500
    [void][P3Host]::PostMessageW((Visible-Dialog), $WM_COMMAND, [IntPtr]$IDCANCEL, [IntPtr]::Zero)
    $R = Res $Id
    if (-not $R.ok -or $R.data.cancelled -ne $true -or $null -ne $R.data.ref) { throw "cancelled save: $($R | ConvertTo-Json -Compress -Depth 5)" }
    if (@(Sent "shell.saveGrant").Count -ne 0) { throw "a cancelled dialog registered a grant" }
    $C.checks.save_cancel = "cancelled"
    $ExportDir = Join-Path $C.data "export"; New-Item -ItemType Directory -Force -Path $ExportDir | Out-Null
    $Target = Join-Path $ExportDir "smoke-bundle.zip"
    $Id = Rt "host.saveFile" "{purpose: 'bundle', format: 'zip'}"
    Wait-Until { (Visible-Dialog) -ne [IntPtr]::Zero } "save dialog (confirm)" 15
    $Dialog = Visible-Dialog
    Wait-Until { [P3Host]::FileNameEdit($Dialog) -ne [IntPtr]::Zero } "file name box" 10
    Start-Sleep -Milliseconds 500
    [P3Host]::SetText([P3Host]::FileNameEdit($Dialog), $Target)
    [void][P3Host]::PostMessageW($Dialog, $WM_COMMAND, [IntPtr]$IDOK, [IntPtr]::Zero)
    $R = Res $Id 20
    if (-not $R.ok -or $R.data.cancelled -ne $false -or -not $R.data.ref) { throw "confirmed save: $($R | ConvertTo-Json -Compress -Depth 5)" }
    $Ref = [string]$R.data.ref
    if (($R | ConvertTo-Json -Compress -Depth 5) -like "*smoke-bundle*") { throw "the page saw the path" }
    $Grant = Get-Content -LiteralPath (Join-Path $C.state "grants.jsonl") | Select-Object -First 1 | ConvertFrom-Json
    if ((Long $Grant.path) -ne (Long $Target) -or $Grant.written -ne 20 -or $Grant.file_type -ne 1 -or $Grant.args.ref -ne $Ref) { throw "grant mismatch: $($Grant | ConvertTo-Json -Compress -Depth 5)" }
    if ([IO.File]::ReadAllText($Target) -ne "host-fixture-export`n") { throw "the fixture did not write through the granted handle" }
    $GrantReq = @(Sent "shell.saveGrant")[0]
    # T-P2-31b: the shell sends saveGrant under its own envelope owner and names the page owner in args.for_owner.
    if ($GrantReq.from -ne "shell" -or $GrantReq.owner -ne "shell" -or $GrantReq.args.for_owner -ne "dev-rt" -or $GrantReq.args.purpose -ne "bundle" -or $GrantReq.args.format -ne "zip") { throw "saveGrant envelope: $($GrantReq | ConvertTo-Json -Compress -Depth 5)" }
    $C.checks.save_confirm = [ordered]@{ ref = $Ref; handle_id = $GrantReq.args.handle_id; written = $Grant.written }
    # 4. host.reveal: only after the export result named the ref.
    $C.checks.reveal_before_export = Expect-Error "host.reveal" "{ref: '$Ref'}" "not_allowed"
    Push-Event ([ordered]@{ topic = "jobs"; kind = "edge"; data = [ordered]@{ op = "export-smoke"; name = "logs.exportBundle"; state = "done"; owner = "dev-rt"; started_at_ms = 1
        result = [ordered]@{ ref = $Ref; size = 20; files = @() } } })
    Wait-Until { (Page-Count "f.topic === 'jobs' && f.data.op === 'export-smoke'") -ge 1 } "export job on the page" 10
    $R = Res (Rt "host.reveal" "{ref: '$Ref'}")
    if (-not $R.ok -or -not $R.data.revealed) { throw "reveal: $($R | ConvertTo-Json -Compress -Depth 5)" }
    $ExportLong = Long $ExportDir
    Wait-Until { (Explorer-Paths) -contains $ExportLong } "explorer selecting the export" 15
    Close-Explorer @($ExportLong)
    $C.checks.reveal_unknown = Expect-Error "host.reveal" "{ref: 'save-ffffffffffffffffffffffffffffffff'}" "not_allowed"
    # 5. Update hand-off: wrong sha256 declined; right one accepted, app.quit{update}, installer runs.
    $UpdateDir = Join-Path $C.data "updates"; New-Item -ItemType Directory -Force -Path $UpdateDir | Out-Null
    $C.installer = Join-Path $UpdateDir "Tesseract-Setup-9.9.9-win-x64.exe"
    Copy-Item -LiteralPath $FakeInstaller -Destination $C.installer
    $Sha = (Get-FileHash -LiteralPath $C.installer -Algorithm SHA256).Hash.ToLowerInvariant()
    Push-Event (Handoff "update-handoff-bad" ("0" * 64))
    Wait-Until { @(Sent "update.exitResponse").Count -ge 1 } "declined exitResponse" 15
    $Decline = @(Sent "update.exitResponse")[0].args
    if ($Decline.handoff_id -ne "update-handoff-bad" -or $Decline.accepted -ne $false -or $Decline.reason_code -ne "hash_mismatch") { throw "mismatch not declined: $($Decline | ConvertTo-Json -Compress)" }
    Start-Sleep -Milliseconds 500
    if ($C.shell.HasExited -or @(Sent "app.quit").Count -ne 0) { throw "a declined hand-off quit the shell" }
    $C.checks.handoff_mismatch = "declined:hash_mismatch"
    Push-Event (Handoff "update-handoff-ok" $Sha)
    Wait-Until { @(Sent "app.quit").Count -ge 1 } "app.quit for the update" 20
    $Accept = @(Sent "update.exitResponse")[1].args
    if ($Accept.handoff_id -ne "update-handoff-ok" -or $Accept.accepted -ne $true) { throw "hand-off not accepted: $($Accept | ConvertTo-Json -Compress)" }
    if (@(Sent "app.quit")[0].args.reason -ne "update") { throw "app.quit reason is not update" }
    Finish-Shell 0
    Wait-Until { Test-Path -LiteralPath $C.marker } "fake installer marker" 15
    $Marker = @(Get-Content -LiteralPath $C.marker)
    if ($Marker[0] -ne "/SILENT /SUPPRESSMSGBOXES /NORESTART" -or (Long $Marker[1]) -ne (Long $C.installer)) { throw "installer marker: $($Marker -join ' | ')" }
    foreach ($Code in @("update_exit_response handoff=update-handoff-ok accepted=true","update_quit_requested","quit_sent reason=update","update_installer_launched handoff=update-handoff-ok version=9.9.9")) {
        if (-not (Log-Has $Code)) { throw "shell.log lacks: $Code" }
    }
    $C.checks.handoff_launched = [string]$Marker[0]  # plain text, not the PSObject with provider notes
}
function Scenario-InstallFailure {
    $C = Start-Shell "install-failure" @() {
        # A process left under the install root (a copy of the stand-in, /HOLD) blocks the launch.
        Copy-Item -LiteralPath $FakeInstaller -Destination (Join-Path $script:C.root "residual.exe")
        $script:C.holder = Start-Process -FilePath (Join-Path $script:C.root "residual.exe") -ArgumentList "/HOLD" -PassThru
    }
    Subscribe-Page
    [void][P3Host]::PostMessageW($C.main, $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero)
    Wait-Until { Log-Has "window_closed_to_tray" } "closed to tray" 10
    $UpdateDir = Join-Path $C.data "updates"; New-Item -ItemType Directory -Force -Path $UpdateDir | Out-Null
    $C.installer = Join-Path $UpdateDir "Tesseract-Setup-9.9.9-win-x64.exe"
    Copy-Item -LiteralPath $FakeInstaller -Destination $C.installer
    Push-Event (Handoff "update-handoff-residual" (Get-FileHash -LiteralPath $C.installer -Algorithm SHA256).Hash.ToLowerInvariant())
    Wait-Until { @(Sent "app.quit").Count -ge 1 } "app.quit for the update" 20
    Wait-Until { Log-Has "update_install_failed handoff=update-handoff-residual version=9.9.9 reason=residual_processes" } "residual failure" 40
    Wait-Until { Test-Path -LiteralPath (Join-Path $C.state "ready-2") } "stopped-state relaunch" 30
    Wait-Until { @(Sent "update.installResult" 2).Count -ge 1 } "installResult to the new epoch" 15
    $Result = @(Sent "update.installResult" 2)[0].args
    if ($Result.receipt_ref -ne "receipt-smoke" -or $Result.launched -ne $false -or $Result.reason_code -ne "residual_processes") { throw "installResult: $($Result | ConvertTo-Json -Compress)" }
    if (@(Fixture-Frames 2 | Where-Object { $_.cmd -eq "service.start" }).Count -ne 0) { throw "the relaunched sidecar was started" }
    Wait-Until { [P3Host]::IsWindowVisible($C.main) } "window restored" 10
    if (-not (Log-Has "update_window_restored")) { throw "window restore not logged" }
    if (Test-Path -LiteralPath $C.marker) { throw "the installer ran despite the residual process" }
    $C.checks.install_failure = "residual_processes -> installResult(launched=false), window restored, relaunched stopped"
    Stop-Process -Id $C.holder.Id -Force
    # Back to Running: closing hides to the tray again, then tray Quit exits 0.
    [void][P3Host]::PostMessageW($C.main, $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero)
    Wait-Until { @(Log-Lines | Where-Object { $_ -like "* window_closed_to_tray" }).Count -ge 2 } "closed to tray again" 10
    $C.checks.close_to_tray_restored = $true
    Quit-Tray
    Finish-Shell 0
}
function Scenario-Autostart([bool] $Tray) {
    $Name = if ($Tray) { "autostart-tray" } else { "autostart-window" }
    $C = Start-Shell $Name @("--autostart") { if ($Tray) { [IO.File]::WriteAllText((Join-Path $script:C.state "tray"), "1") } }
    $Expected = if ($Tray) { "autostart_start setting=true window=tray" } else { "autostart_start setting=false window=shown" }
    Wait-Until { Log-Has $Expected } $Expected 15
    if (@(Sent "config.snapshot").Count -ne 1) { throw "config.snapshot not queried once" }
    Start-Sleep -Milliseconds 500
    $Visible = [P3Host]::IsWindowVisible($C.main)
    if ($Visible -eq $Tray) { throw "window visible=$Visible with tray setting $Tray" }
    $C.checks.window_visible = $Visible
    Quit-Tray
    Finish-Shell 0
}

$Results = [ordered]@{}; $Failure = $null
try {
    Build-FakeInstaller $FakeInstaller
    foreach ($Scenario in $Scenarios) {
        $script:C = $null; $Steps = @(); $Passed = $false; $Error1 = $null
        try {
            switch ($Scenario) {
                "main" { Scenario-Main }
                "install-failure" { Scenario-InstallFailure }
                "autostart-tray" { Scenario-Autostart $true }
                "autostart-window" { Scenario-Autostart $false }
                default { throw "unknown scenario $Scenario" }
            }
            $Passed = $true
        } catch { $Error1 = $_.Exception.Message } finally {
            $Steps = Cleanup-Shell
            foreach ($Var in $Names) { [Environment]::SetEnvironmentVariable($Var,$Previous[$Var],"Process") }
        }
        $Results[$Scenario] = [ordered]@{ passed = $Passed; error = $Error1; cleanup = $Steps; scope = $script:C.tag; checks = $script:C.checks }
        if (-not $Passed -and -not $Failure) { $Failure = "${Scenario}: $Error1" }
    }
} catch {
    if (-not $Failure) { $Failure = $_.Exception.Message }
} finally {
    foreach ($Var in $Names) { [Environment]::SetEnvironmentVariable($Var,$Previous[$Var],"Process") }
    [IO.File]::WriteAllText((Join-Path $Evidence "result.json"), ([ordered]@{ passed = (-not $Failure); error = $Failure; scenarios = $Results } | ConvertTo-Json -Depth 8), $Utf8)
}
if ($Failure) { throw "P3-06 host smoke failed: $Failure (evidence: $Evidence)" }
Write-Output "P3-06 host smoke passed (evidence: $Evidence)"

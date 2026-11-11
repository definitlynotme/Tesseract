[CmdletBinding()]
param()
# T-P3-03 tray smoke: tray icon present, close hides to tray, simulated tray click restores,
# simulated TaskbarCreated re-adds, debug balloon entry submits, the live right-click menu
# shows both localized items, tray Quit exits shell + sidecar.
# The caller builds incrementally under the ticket's CPU rules; this script never builds.
$ErrorActionPreference = "Stop"
# Refuse before any process/Win32 access. Caller must explicitly opt in (P3-02 guard).
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
$Orders = Get-ChildItem -LiteralPath (Join-Path $Repo ".build") -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "T-P3-03.md") } | Select-Object -First 1
if (-not $Orders) { throw "work-order directory not found" }
$Tag = "p3-03-" + [Guid]::NewGuid().ToString("N")
# TESSERACT_P3_EVIDENCE_ROOT (T-P3-H): a later ticket re-running this smoke keeps its evidence in its own directory.
$EvidenceRoot = if ($env:TESSERACT_P3_EVIDENCE_ROOT) { $env:TESSERACT_P3_EVIDENCE_ROOT } else { Join-Path $Orders.FullName "evidence/T-P3-03" }
$Evidence = Join-Path $EvidenceRoot ("tray-" + $Tag)
$Config = Join-Path $env:TESSERACT_CONFIG_DIR $Tag
$Data = Join-Path $env:TESSERACT_DATA_DIR $Tag
$Temp = Join-Path $Data "temp"
foreach ($Dir in @($Evidence,$Config,$Data,$Temp)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
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
'@ $Repo $Config
if ($LASTEXITCODE -ne 0) { throw "isolated config setup failed" }

Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Net;
using System.Net.WebSockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
public static class P3Tray {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindowExW(IntPtr parent, IntPtr after, string cls, string title);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern IntPtr SendMessageTimeoutW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l, uint flags, uint ms, out IntPtr result);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern uint RegisterWindowMessageW(string name);
    [StructLayout(LayoutKind.Sequential)] public struct Point { public int X, Y; }
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out Point point);
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [StructLayout(LayoutKind.Sequential)] struct NotifyIconIdentifier { public uint cbSize; public IntPtr hWnd; public uint uID; public Guid guidItem; }
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("shell32.dll")] static extern int Shell_NotifyIconGetRect(ref NotifyIconIdentifier id, out Rect rect);
    public static IntPtr Find(string cls, string title, uint pid) {
        IntPtr after = IntPtr.Zero;
        while (true) {
            after = FindWindowExW(IntPtr.Zero, after, cls, title);
            if (after == IntPtr.Zero) return IntPtr.Zero;
            uint owner; GetWindowThreadProcessId(after, out owner);
            if (owner == pid) return after;
        }
    }
    // S_OK means Explorer currently holds an icon for (hwnd, uID).
    public static int IconRect(IntPtr hwnd, uint id) {
        NotifyIconIdentifier n = new NotifyIconIdentifier(); n.cbSize = (uint)Marshal.SizeOf(typeof(NotifyIconIdentifier)); n.hWnd = hwnd; n.uID = id;
        Rect r; return Shell_NotifyIconGetRect(ref n, out r);
    }
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetMenuStringW(IntPtr menu, uint item, StringBuilder text, int max, uint flags);
    [DllImport("user32.dll")] static extern int GetMenuItemCount(IntPtr menu);
    // The visible popup menu (#32768) owned by the shell process, if any.
    public static IntPtr VisibleMenu(uint pid) {
        IntPtr after = IntPtr.Zero;
        while (true) {
            after = FindWindowExW(IntPtr.Zero, after, "#32768", null);
            if (after == IntPtr.Zero) return IntPtr.Zero;
            uint owner; GetWindowThreadProcessId(after, out owner);
            if (owner == pid && IsWindowVisible(after)) return after;
        }
    }
    // MN_GETHMENU, then the item count and the strings of command ids 1 (show) and 2 (quit).
    public static string[] MenuTexts(IntPtr menuWindow) {
        IntPtr menu = new IntPtr(Send(menuWindow, 0x01E1, 0, 0));
        if (menu == IntPtr.Zero || menu.ToInt64() == -1) return null;
        string[] texts = new string[3]; texts[0] = GetMenuItemCount(menu).ToString();
        for (uint id = 1; id <= 2; id++) { StringBuilder s = new StringBuilder(256); GetMenuStringW(menu, id, s, 256, 0); texts[id] = s.ToString(); }
        return texts;
    }
    public static long Send(IntPtr hwnd, uint msg, long w, long l) {
        IntPtr result;
        if (SendMessageTimeoutW(hwnd, msg, new IntPtr(w), new IntPtr(l), 2, 10000, out result) == IntPtr.Zero) return -1;
        return result.ToInt64();
    }
    public static string Http(string url) {
        HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url); request.Proxy = null; request.Timeout = 3000;
        using (WebResponse response = request.GetResponse()) using (StreamReader reader = new StreamReader(response.GetResponseStream())) return reader.ReadToEnd();
    }
    // One fixed Runtime.evaluate; the expression comes from this script only.
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

$WM_CLOSE = 0x10; $WM_COMMAND = 0x111; $WM_APP = 0x8000
$TrayCallback = $WM_APP + 1; $DevBalloon = $WM_APP + 2; $NIN_SELECT = 0x400; $CmdQuit = 2
$ShellLog = Join-Path $Data "logs/shell.log"
$Checks = [ordered]@{}
function Wait-Until([scriptblock] $Condition, [string] $Label, [int] $Seconds = 15) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while (-not (& $Condition)) {
        if ([DateTime]::UtcNow -gt $Deadline) { throw "timeout: $Label" }
        Start-Sleep -Milliseconds 100
    }
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
function Log-Has([string] $Code, [int] $Count = 1) {
    if (-not (Test-Path -LiteralPath $ShellLog)) { return $false }
    return @((Get-Content -LiteralPath $ShellLog) | Where-Object { $_ -match (" " + [regex]::Escape($Code) + "$") }).Count -ge $Count
}
function Log-Time([string] $Code) {
    $Line = (Get-Content -LiteralPath $ShellLog) | Where-Object { $_ -match (" " + [regex]::Escape($Code) + "$") } | Select-Object -First 1
    return [int64](($Line -split " ")[0])
}
function Page-Visibility {
    $Pages = $null
    try { $Pages = [P3Tray]::Http("http://127.0.0.1:$Port/json/list") | ConvertFrom-Json } catch { return "" }
    $Page = @($Pages | Where-Object { $_.type -eq "page" }) | Select-Object -First 1
    if (-not $Page) { return "" }
    try { $Reply = [P3Tray]::Evaluate($Page.webSocketDebuggerUrl, "document.visibilityState") | ConvertFrom-Json } catch { return "" }
    return [string]$Reply.result.result.value
}
function Sidecar-Pids([int] $ShellPid) {
    return @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ShellPid" | Where-Object { $_.Name -like "python*.exe" -or $_.Name -like "tesseract-runtime*.exe" } | ForEach-Object { [int]$_.ProcessId })
}

$Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
$Listener.Start(); $Port = $Listener.LocalEndpoint.Port; $Listener.Stop()
$Names = @("TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR","TESSERACT_INSTANCE_SCOPE","TESSERACT_ISOLATED_AUDIT_MANIFEST",
    "TESSERACT_SHELL_CDP_PORT","TESSERACT_SHELL_DEV_MARK_QUITTING","PYTHONPATH","PYTHONDONTWRITEBYTECODE","PYTHONPYCACHEPREFIX","TEMP","TMP")
$Previous = @{}
foreach ($Name in $Names) { $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
$ShellProcess = $null; $Watcher = $null; $TrayHwnd = [IntPtr]::Zero; $Passed = $false; $Failure = $null
try {
    $env:TESSERACT_CONFIG_DIR = $Config; $env:TESSERACT_DATA_DIR = $Data; $env:TESSERACT_INSTANCE_SCOPE = $Tag
    Remove-Item Env:TESSERACT_ISOLATED_AUDIT_MANIFEST -ErrorAction SilentlyContinue
    Remove-Item Env:TESSERACT_SHELL_DEV_MARK_QUITTING -ErrorAction SilentlyContinue
    $env:TESSERACT_SHELL_CDP_PORT = "$Port"; $env:TEMP = $Temp; $env:TMP = $Temp
    # Bytecode warm-up as in p3_exit_smoke.ps1 (T-P3-H fix 1, S7): the real sidecar runs with -B, so
    # without a cache every start compiles desktop_runtime.py (~2 MB) again, which alone can pass the
    # shell's 10 s hello window on a loaded machine. The cache lives in this scope's temporary data
    # directory (never the source tree); the sidecar only reads it. The hello timeout is unchanged.
    $env:PYTHONPYCACHEPREFIX = Join-Path $Data "pycache-warm"
    Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
    $WarmStart = [DateTime]::UtcNow
    Push-Location $Repo; try { & $Python -c "import bridge_main, desktop_runtime" } finally { Pop-Location }
    $Checks.warmup_rc = $LASTEXITCODE; $Checks.warmup_s = [Math]::Round(([DateTime]::UtcNow - $WarmStart).TotalSeconds, 1)
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $WatchArguments = @("-B",('"'+(Join-Path $Repo "shell/app/process_watch.py")+'"'),"--output",('"'+(Join-Path $Evidence "process-inventory.json")+'"'),"--ready",('"'+(Join-Path $Evidence "root.json")+'"'),"--",('"'+$Exe+'"'))
    $Watcher = Start-Process -FilePath $Python -ArgumentList $WatchArguments -WorkingDirectory $Repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Evidence "watch-stdout.log") -RedirectStandardError (Join-Path $Evidence "watch-stderr.log")
    $WatcherHandle = $Watcher.Handle
    Wait-Until { (Test-Path -LiteralPath (Join-Path $Evidence "root.json")) -or $Watcher.HasExited } "suspended job launch"
    $Root = Get-Content -LiteralPath (Join-Path $Evidence "root.json") -Raw | ConvertFrom-Json
    $ShellPid = [int]$Root.pid
    $ShellProcess = Get-Process -Id $ShellPid; $ShellHandle = $ShellProcess.Handle

    # 1. Tray icon exists, added at least 250 ms after the first window show.
    Wait-Until { Log-Has "shell_window_ready" } "shell window" 60
    Wait-Until { Log-Has "tray_icon_added" } "tray icon added" 10
    $Checks.tray_delay_ms = (Log-Time "tray_icon_added") - (Log-Time "window_shown")
    if ($Checks.tray_delay_ms -lt 250 -or $Checks.tray_delay_ms -gt 5000) { throw "tray delay out of range: $($Checks.tray_delay_ms)" }
    $TrayHwnd = [P3Tray]::Find("TesseractShellTray", [NullString]::Value, [uint32]$ShellPid)
    $MainHwnd = [P3Tray]::Find([NullString]::Value, "Tesseract Shell (P3)", [uint32]$ShellPid)
    if ($TrayHwnd -eq [IntPtr]::Zero -or $MainHwnd -eq [IntPtr]::Zero) { throw "tray or main window not found" }
    $Checks.icon_rect_hresult = [P3Tray]::IconRect($TrayHwnd, 1)
    if ($Checks.icon_rect_hresult -ne 0) { throw "tray icon not registered with Explorer: $($Checks.icon_rect_hresult)" }
    $Sidecars = Sidecar-Pids $ShellPid
    if ($Sidecars.Count -ne 1) { throw "expected one sidecar child, found $($Sidecars.Count)" }
    $Checks.sidecar_pid = $Sidecars[0]
    Wait-Until { (Page-Visibility) -eq "visible" } "page visible at start" 30
    # T-P3-H: the residue identification (--user-data-dir under <data>\WebView2) finds the live WebView2.
    $Checks.scoped_webviews_while_running = @(Scoped-WebViews $Data).Count
    if ($Checks.scoped_webviews_while_running -lt 1) { throw "scoped WebView2 identification found no live process" }

    # 2. Closing the window hides it; shell and sidecar keep running; the page is hidden.
    [void][P3Tray]::PostMessageW($MainHwnd, $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero)
    Wait-Until { (Log-Has "window_closed_to_tray") -and (Log-Has "window_hidden") } "close to tray"
    Start-Sleep -Milliseconds 1500
    $ShellProcess.Refresh()
    if ($ShellProcess.HasExited) { throw "shell exited on window close" }
    if ([P3Tray]::IsWindowVisible($MainHwnd)) { throw "window still visible after close" }
    if (-not (Get-Process -Id $Checks.sidecar_pid -ErrorAction SilentlyContinue)) { throw "sidecar exited on window close" }
    if (Log-Has "quit_requested_window_without_tray") { throw "close took the quit path although the tray exists" }
    Wait-Until { (Page-Visibility) -eq "hidden" } "page visibilityState hidden"
    $Checks.close_hides_process_alive_page_hidden = $true

    # 3. Simulated tray left click (v4 NIN_SELECT callback) restores through the P3-02 restore path.
    [void][P3Tray]::PostMessageW($TrayHwnd, $TrayCallback, [IntPtr]::Zero, [IntPtr](65536 + $NIN_SELECT))
    Wait-Until { [P3Tray]::IsWindowVisible($MainHwnd) -and -not [P3Tray]::IsIconic($MainHwnd) } "restore from tray click"
    Wait-Until { (Log-Has "tray_restore_requested") -and (Log-Has "restore_event_received") -and (Log-Has "window_shown" 2) } "restore log codes"
    Wait-Until { (Page-Visibility) -eq "visible" } "page visible after restore"
    $Checks.tray_click_restores = $true

    # 4. Simulated Explorer restart message re-registers the icon.
    $TaskbarCreated = [P3Tray]::RegisterWindowMessageW("TaskbarCreated")
    [void][P3Tray]::Send($TrayHwnd, $TaskbarCreated, 0, 0)
    Wait-Until { Log-Has "tray_icon_readded" } "taskbar created re-add"
    if ([P3Tray]::IconRect($TrayHwnd, 1) -ne 0) { throw "icon missing after TaskbarCreated" }
    $Checks.taskbar_created_readds = $true

    # 5. Debug-build balloon entry reports submission to Windows.
    $Checks.dev_balloon_result = [P3Tray]::Send($TrayHwnd, $DevBalloon, 0, 0)
    if ($Checks.dev_balloon_result -ne 1) { throw "dev balloon entry failed: $($Checks.dev_balloon_result)" }
    Wait-Until { Log-Has "balloon_submitted" } "balloon log"

    # 6a. The real right-click menu: a v4 WM_CONTEXTMENU callback runs CreatePopupMenu /
    # AppendMenuW / TrackPopupMenuEx; read the two item texts back from the live #32768 window.
    $NativeText = Get-Content -LiteralPath (Join-Path $Repo "shell/app/src/native_text.rs") -Encoding UTF8 -Raw
    $Locales = @([regex]::Matches(([regex]::Match($NativeText, 'LOCALES:\[&str;9\]=\[([^\]]*)\]').Groups[1].Value), '"([^"]*)"') | ForEach-Object { $_.Groups[1].Value })
    $ShowTexts = @([regex]::Matches(([regex]::Match($NativeText, 'id:"tray_show",text:\[([^\]]*)\]').Groups[1].Value), '"([^"]*)"') | ForEach-Object { $_.Groups[1].Value })
    $QuitTexts = @([regex]::Matches(([regex]::Match($NativeText, 'id:"tray_quit",text:\[([^\]]*)\]').Groups[1].Value), '"([^"]*)"') | ForEach-Object { $_.Groups[1].Value })
    if ($Locales.Count -ne 9 -or $ShowTexts.Count -ne 9 -or $QuitTexts.Count -ne 9) { throw "native_text menu table not parsed" }
    $WM_CONTEXTMENU = 0x7B; $WM_KEYDOWN = 0x100; $VK_ESCAPE = 0x1B; $WM_CANCELMODE = 0x1F
    # T-P3-H fix 1: open the menu at the primary-screen corner farthest from the real cursor, so a
    # mouse release by someone using the desktop cannot land on an item (the shell logs the cursor
    # and button state in tray_menu_shown either way).
    # Physical pixels like the shell (it reads the posted point as physical): DPI-aware from here on.
    [void][P3Tray]::SetProcessDPIAware()
    $Cursor = New-Object P3Tray+Point; [void][P3Tray]::GetCursorPos([ref]$Cursor)
    $ScreenW = [P3Tray]::GetSystemMetrics(0); $ScreenH = [P3Tray]::GetSystemMetrics(1)
    $Anchor = @(@(200,200), @(($ScreenW - 300),200), @(200,($ScreenH - 100)), @(($ScreenW - 300),($ScreenH - 100))) |
        Sort-Object { -(([double]$_[0] - $Cursor.X) * ([double]$_[0] - $Cursor.X) + ([double]$_[1] - $Cursor.Y) * ([double]$_[1] - $Cursor.Y)) } | Select-Object -First 1
    $Checks.menu_anchor = "$($Anchor[0]),$($Anchor[1])"; $Checks.cursor_at_menu = "$($Cursor.X),$($Cursor.Y)"
    [void][P3Tray]::PostMessageW($TrayHwnd, $TrayCallback, [IntPtr]($Anchor[0] + $Anchor[1] * 65536), [IntPtr](65536 + $WM_CONTEXTMENU))
    Wait-Until { [P3Tray]::VisibleMenu([uint32]$ShellPid) -ne [IntPtr]::Zero } "tray context menu shown"
    $MenuWindow = [P3Tray]::VisibleMenu([uint32]$ShellPid)
    $MenuTexts = [P3Tray]::MenuTexts($MenuWindow)
    if (-not $MenuTexts) { throw "MN_GETHMENU failed" }
    $Checks.menu_item_count = [int]$MenuTexts[0]
    $Checks.menu_show_text = $MenuTexts[1]; $Checks.menu_quit_text = $MenuTexts[2]
    $Index = [Array]::IndexOf([string[]]$ShowTexts, [string]$MenuTexts[1])
    if ($Checks.menu_item_count -ne 3) { throw "menu should hold show, separator, quit: $($Checks.menu_item_count)" }
    if ($Index -lt 0 -or $QuitTexts[$Index] -ne $MenuTexts[2]) { throw "menu texts are not one locale's tray_show/tray_quit pair" }
    $Checks.menu_locale = $Locales[$Index]
    [void][P3Tray]::PostMessageW($MenuWindow, $WM_KEYDOWN, [IntPtr]$VK_ESCAPE, [IntPtr]1)
    $Closed = $false; $Deadline = [DateTime]::UtcNow.AddSeconds(5)
    while ([DateTime]::UtcNow -lt $Deadline) { if ([P3Tray]::VisibleMenu([uint32]$ShellPid) -eq [IntPtr]::Zero) { $Closed = $true; break }; Start-Sleep -Milliseconds 100 }
    $Checks.menu_closed_by = "escape"
    if (-not $Closed) {
        # The menu belongs to the tray window; DefWindowProc(WM_CANCELMODE) ends menu mode.
        [void][P3Tray]::Send($TrayHwnd, $WM_CANCELMODE, 0, 0); $Checks.menu_closed_by = "cancelmode"
        Wait-Until { [P3Tray]::VisibleMenu([uint32]$ShellPid) -eq [IntPtr]::Zero } "tray context menu closed" 5
    }
    $ShellProcess.Refresh(); if ($ShellProcess.HasExited) { throw "shell exited after closing the menu" }
    if (Log-Has "quit_requested_tray") { throw "the open menu was used to quit (see tray_menu_shown in shell.log: a real mouse release?)" }
    $Checks.tray_menu_runtime = $true

    # 6. Tray Quit (the menu's WM_COMMAND) exits shell and sidecar without residue.
    [void][P3Tray]::PostMessageW($TrayHwnd, $WM_COMMAND, [IntPtr]$CmdQuit, [IntPtr]::Zero)
    if (-not $ShellProcess.WaitForExit(30000)) { throw "shell did not exit after tray quit" }
    $ShellProcess.Refresh(); $Checks.shell_exit_code = $ShellProcess.ExitCode
    if ($Checks.shell_exit_code -ne 0) { throw "shell exit code $($Checks.shell_exit_code)" }
    if (-not (Log-Has "quit_requested_tray")) { throw "tray quit not logged" }
    if (Get-Process -Id $Checks.sidecar_pid -ErrorAction SilentlyContinue) { throw "sidecar still running" }
    $Checks.icon_removed_hresult = [P3Tray]::IconRect($TrayHwnd, 1)
    if ($Checks.icon_removed_hresult -eq 0) { throw "tray icon left behind" }
    $Remaining = Wait-ScopedWebViewsGone $Data $ShellProcess
    # T-P3-H2 (C3-8): residue-tree.json tells the shell's leftover from WebView2's own slow exit.
    if ($Remaining.Count -gt 0) { $Checks.residue_class = Save-ResidueTree $Remaining $ShellProcess $ShellLog $Evidence; throw "scoped WebView processes remain ($($Checks.residue_class))" }
    if (-not $Watcher.WaitForExit($InventoryWaitMs)) { throw (Inventory-TimeoutMessage (Join-Path $Evidence "process-inventory.json")) }
    $Watcher.Refresh()
    $Inventory = Get-Content -LiteralPath (Join-Path $Evidence "process-inventory.json") -Raw | ConvertFrom-Json
    if ($Watcher.ExitCode -ne 0 -or -not $Inventory.passed -or $Inventory.active_processes -ne 0) { throw "owned process residual detected" }
    if (-not @($Inventory.records | Where-Object { $_.pid -eq $Checks.sidecar_pid -and $_.identity_available }).Count) { throw "sidecar creation identity not observed" }
    $Checks.tray_quit_no_residue = $true
    $Passed = $true
} catch {
    $Failure = $_.Exception.Message
} finally {
    # T-P3-H2 fix 3: a launch that gave up never learned its shell; end one the watcher started late.
    if (-not $ShellProcess) { $Late = Resolve-LateShell (Join-Path $Evidence "root.json") $Watcher; if ($Late) { $Checks.late_shell = Close-LateShell $Late $Evidence } }
    # On automation failure request the tray quit (the real exit path); never kill runtime/network owners.
    if ($ShellProcess -and -not $ShellProcess.HasExited) {
        if ($TrayHwnd -ne [IntPtr]::Zero) { [void][P3Tray]::PostMessageW($TrayHwnd, $WM_COMMAND, [IntPtr]$CmdQuit, [IntPtr]::Zero) }
        else { [void]$ShellProcess.CloseMainWindow() }
        [void]$ShellProcess.WaitForExit(30000)
    }
    if ($Watcher -and -not $Watcher.HasExited) { [void]$Watcher.WaitForExit($InventoryWaitMs) }
    foreach ($Name in $Names) { [Environment]::SetEnvironmentVariable($Name,$Previous[$Name],"Process") }
    if (Test-Path -LiteralPath $ShellLog) { Copy-Item -LiteralPath $ShellLog -Destination (Join-Path $Evidence "shell.log") }
    $Result = [ordered]@{ passed = $Passed; error = $Failure; scope = $Tag; data = $Data; checks = $Checks }
    [IO.File]::WriteAllText((Join-Path $Evidence "result.json"), ($Result | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))
}
if (-not $Passed) { throw "P3-03 tray smoke failed: $Failure (evidence: $Evidence)" }
Write-Output "P3-03 tray smoke passed; evidence: $Evidence"

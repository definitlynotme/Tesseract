[CmdletBinding()]
param()
# T-P3-05 notify / presence / language smoke, custom scope only (P3-02 guard). The debug shell
# runs the scripted stand-in sidecar shell/app/src/notify_fixture.py (`--dev-sidecar-script`,
# debug builds only): it declares alerts.desktop / ui.language and the shell commands
# alerts.desktopDelivered / ui.presence, records every frame it receives, and sends each JSON file
# this script drops into <data>/notify-fixture/push. The script checks:
#  - hello.lang and a later ui.language reach the page as host:language; shell.log records them;
#  - ui.presence: full value first, the page id from host_set_page (whitelist), hidden after the
#    window closes to the tray, visible again after opening the notice restores it;
#  - host:visibility false/true on the page around close-to-tray and restore;
#  - alerts.desktop while hidden -> outside the window (10-30 notice redesign, notify.rs): the
#    notice card window (route=card), or the tray balloon when the card window cannot show it
#    (route=windows) -> delivered/windows receipt; opening it (notice_open from the card page: its
#    body or its View button; NIN_BALLOONUSERCLICK posted to the tray window for the balloon)
#    restores, and host:navigate reaches the page only after the host:visibility{true} of the
#    restored window;
#  - while Windows says the user is busy (SHQueryUserNotificationState: full screen, presentation,
#    quiet time) an unforced notice is held (route=held, no receipt) and shown once the user is
#    free. That is the machine's state, not a contract error: a forced preview (never held) then
#    stands in for the hidden-window notice, every other check still runs, and the smoke ends with
#    an "environment:" failure naming the state (result.json: checks.environment);
#  - in front: a page that has not called host_notify_ready still gets its notice outside the
#    window (card, balloon, or held while busy); after it, in-app host:notify,
#    host_notify_rendered -> delivered/in_app; a silent page -> dropped/none after 3 s. If Windows
#    refuses this test the foreground (focus-steal rules), the route must still match the probed
#    state, and result.json records foreground=false;
#  - desktop-test-* always tries Windows; a repeated id gets no second receipt; the page cannot
#    send alerts.desktopDelivered itself (rt -> not_allowed) nor confirm an unknown id;
#  - tray Quit exits 0 with no owned process left.
# Evidence: the work-order directory evidence/T-P3-05/notify-<scope>/. The caller builds; this
# script never builds.
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
$Fixture = Join-Path $Repo "shell/app/src/notify_fixture.py"
if (-not (Test-Path -LiteralPath $Exe)) { throw "debug shell missing; build it first" }
# The work-order directory name is not ASCII; locate it by the ticket file instead of spelling it here.
$Orders = Get-ChildItem -LiteralPath (Join-Path $Repo ".build") -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "T-P3-05.md") } | Select-Object -First 1
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
public static class P3Notify {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindowExW(IntPtr parent, IntPtr after, string cls, string title);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("shell32.dll")] static extern int SHQueryUserNotificationState(out int state);
    // QUNS_*: 1 not present, 2 busy, 3 full-screen D3D, 4 presentation, 5 accepts notifications,
    // 6 quiet time, 7 app; -1 when it cannot be asked. 2, 3, 4 and 6 hold cards (notice.rs busy_state).
    public static int UserState() { int state; return SHQueryUserNotificationState(out state) == 0 ? state : -1; }
    public static IntPtr Find(string cls, string title, uint pid) {
        IntPtr after = IntPtr.Zero;
        while (true) {
            after = FindWindowExW(IntPtr.Zero, after, cls, title);
            if (after == IntPtr.Zero) return IntPtr.Zero;
            uint owner; GetWindowThreadProcessId(after, out owner);
            if (owner == pid) return after;
        }
    }
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

$WM_COMMAND = 0x111; $CmdQuit = 2; $WM_CLOSE = 0x10; $TrayCallback = 0x8001; $NIN_BALLOONUSERCLICK = 0x405
function Wait-Until([scriptblock] $Condition, [string] $Label, [int] $Seconds = 15) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while (-not (& $Condition)) {
        if ([DateTime]::UtcNow -gt $Deadline) { throw "timeout: $Label" }
        Start-Sleep -Milliseconds 100
    }
}
function Log-Lines { if (Test-Path -LiteralPath $script:ShellLog) { return @(Get-Content -LiteralPath $script:ShellLog) } else { return @() } }
function Log-Has([string] $Prefix, [int] $Count = 1) {
    return @(Log-Lines | Where-Object { ($_ -replace '^\d+ ','').StartsWith($Prefix) }).Count -ge $Count
}
function Log-Count([string] $Prefix) { return @(Log-Lines | Where-Object { ($_ -replace '^\d+ ','').StartsWith($Prefix) }).Count }
# The CDP target of one of the shell's pages: the dev page (main window) or notice.html (the card window).
function Page-Socket([string] $Page = "dev-rt.html") {
    try { $Pages = [P3Notify]::Http("http://127.0.0.1:$script:Port/json/list") | ConvertFrom-Json } catch { return $null }
    $Target = @($Pages | Where-Object { $_.type -eq "page" -and $_.url -like "*$Page*" }) | Select-Object -First 1
    if ($Target) { return $Target.webSocketDebuggerUrl } else { return $null }
}
function Page-Eval([string] $Expression, [string] $Page = "dev-rt.html") {
    $Socket = Page-Socket $Page
    if (-not $Socket) { return $null }
    try { $Reply = [P3Notify]::Evaluate($Socket, $Expression) | ConvertFrom-Json } catch { return $null }
    return $Reply.result.result.value
}
# A Tauri invoke from the page; returns "ok:<json>" or "err:<message>".
function Page-Invoke([string] $Command, [string] $ArgsJs, [string] $Page = "dev-rt.html") {
    $Key = "__p305_" + [Guid]::NewGuid().ToString("N").Substring(0,8)
    [void](Page-Eval "(window.$Key = null, window.__TAURI__.core.invoke('$Command', $ArgsJs).then(r => window.$Key = 'ok:' + JSON.stringify(r === undefined ? null : r), e => window.$Key = 'err:' + String(e)), true)" $Page)
    Wait-Until { $null -ne (Page-Eval "window.$Key" $Page) } "invoke $Command" 10
    return Page-Eval "window.$Key" $Page
}
function Page-Count([string] $Filter) { return [int](Page-Eval "window.devFrames.filter(f => $Filter).length") }
function Fixture-Frames {
    $Path = Join-Path $script:State "frames-1.jsonl"
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    return @(Get-Content -LiteralPath $Path | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
}
function Receipts([string] $Id) { return @(Fixture-Frames | Where-Object { $_.cmd -eq "alerts.desktopDelivered" -and $_.args.delivery_id -eq $Id }) }
function Presences { return @(Fixture-Frames | Where-Object { $_.cmd -eq "ui.presence" } | ForEach-Object { $_.args }) }
function Last-Presence { $All = @(Presences); if ($All.Count -gt 0) { return $All[-1] } else { return $null } }
$script:Push = 0
function Push-Event($Spec) {
    $script:Push++
    $Dir = Join-Path $script:State "push"; New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $Name = "{0:D3}" -f $script:Push
    $Tmp = Join-Path $Dir "$Name.tmp"
    [IO.File]::WriteAllText($Tmp, ($Spec | ConvertTo-Json -Depth 6 -Compress), (New-Object Text.UTF8Encoding $false))
    Move-Item -LiteralPath $Tmp -Destination (Join-Path $Dir "$Name.json")
    Wait-Until { Test-Path -LiteralPath (Join-Path $Dir "$Name.sent") } "fixture sent $Name" 10
}
function Desktop([string] $Id, [string] $Detail) {
    return [ordered]@{ topic = "alerts.desktop"; kind = "edge"; data = [ordered]@{ delivery_id = $Id; event_ids = @("smoke"); severity = "info"
        title = "Tesseract smoke"; detail = $Detail; members = @("smoke"); target = "alerts" } }
}
function Expect-Receipt([string] $Id, [string] $Outcome, [string] $Surface, [int] $Seconds = 10) {
    Wait-Until { @(Receipts $Id).Count -ge 1 } "receipt $Id" $Seconds
    $R = @(Receipts $Id)[0].args
    if ($R.outcome -ne $Outcome -or $R.surface -ne $Surface) { throw "receipt $Id is $($R.outcome)/$($R.surface), expected $Outcome/$Surface" }
    if (-not (Log-Has "notify_receipt_sent delivery=$Id outcome=$Outcome surface=$Surface")) { throw "shell.log lacks the receipt of $Id" }
    return "$Outcome/$Surface"
}
# The latest notify_route line of a delivery: a held notice gets a second one once it is released,
# and a card's line is written when the card is on screen (or its balloon fallback was submitted).
function Route-Of([string] $Id) {
    $Last = $null
    foreach ($Line in (Log-Lines)) { $M = [regex]::Match($Line, " notify_route delivery=$([regex]::Escape($Id)) route=(\w+) force=(\w+) visible=(\w+) focused=(\w+)"); if ($M.Success) { $Last = $M } }
    return $Last
}
# The route of a notice that is not shown in the page: card or windows once it is on screen (the
# card window builds and loads its page first; its balloon fallback can take ~12 s), or held while
# Windows says the user is busy. The shell asks again every 5 s whether the user is free; a held
# notice gets $script:HoldWait seconds to come out (shorter once this run has seen the user busy).
$script:HoldWait = 25
function Wait-Shown([string] $Id) {
    Wait-Until { $null -ne (Route-Of $Id) } "route of $Id" 20
    try { Wait-Until { (Route-Of $Id).Groups[1].Value -ne "held" } "release of $Id" $script:HoldWait } catch { }
    return (Route-Of $Id)
}
# Windows says the user is busy (a held notice): recorded once; the smoke ends with "environment:".
function Note-Busy([string] $Id) {
    $script:HoldWait = 6
    if (-not $S.checks.environment) {
        $S.checks.environment = "Windows reports the user busy (SHQueryUserNotificationState=$([P3Notify]::UserState())): $Id was held, not shown"
    }
}

$Names = @("TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR","TESSERACT_INSTANCE_SCOPE","TESSERACT_ISOLATED_AUDIT_MANIFEST","TESSERACT_SHELL_CDP_PORT","PYTHONDONTWRITEBYTECODE","TEMP","TMP")
$Previous = @{}
foreach ($Name in $Names) { $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
$Tag = "p3-05-notify-" + [Guid]::NewGuid().ToString("N").Substring(0,12)
# TESSERACT_P3_EVIDENCE_ROOT (T-P3-H): a later ticket re-running this smoke keeps its evidence in its own directory.
$EvidenceRoot = if ($env:TESSERACT_P3_EVIDENCE_ROOT) { $env:TESSERACT_P3_EVIDENCE_ROOT } else { Join-Path $Orders.FullName "evidence/T-P3-05" }
$S = [ordered]@{ tag = $Tag; evidence = (Join-Path $EvidenceRoot ("notify-" + $Tag)); config = (Join-Path $BaseConfig $Tag); data = (Join-Path $BaseData $Tag); checks = [ordered]@{} }
$S.temp = Join-Path $S.data "temp"
foreach ($Dir in @($S.evidence,$S.config,$S.data,$S.temp)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
$script:State = Join-Path $S.data "notify-fixture"
$script:ShellLog = Join-Path $S.data "logs/shell.log"
$Passed = $false; $Failure = $null; $Cleanup = @()
try {
    $env:TESSERACT_CONFIG_DIR = $S.config; $env:TESSERACT_DATA_DIR = $S.data; $env:TESSERACT_INSTANCE_SCOPE = $S.tag
    $env:TEMP = $S.temp; $env:TMP = $S.temp; $env:PYTHONDONTWRITEBYTECODE = "1"
    Remove-Item Env:TESSERACT_ISOLATED_AUDIT_MANIFEST -ErrorAction SilentlyContinue
    $Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
    $Listener.Start(); $script:Port = $Listener.LocalEndpoint.Port; $Listener.Stop()
    $env:TESSERACT_SHELL_CDP_PORT = "$script:Port"
    $WatchArguments = @("-B",('"'+(Join-Path $Repo "shell/app/process_watch.py")+'"'),"--output",('"'+(Join-Path $S.evidence "process-inventory.json")+'"'),"--ready",('"'+(Join-Path $S.evidence "root.json")+'"'),"--",('"'+$Exe+'"'),"--dev-rt","--dev-sidecar-script",('"'+$Fixture+'"'))
    $S.watcher = Start-Process -FilePath $Python -ArgumentList $WatchArguments -WorkingDirectory $Repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $S.evidence "watch-stdout.log") -RedirectStandardError (Join-Path $S.evidence "watch-stderr.log")
    $S.watcher_handle = $S.watcher.Handle
    Wait-Until { (Test-Path -LiteralPath (Join-Path $S.evidence "root.json")) -or $S.watcher.HasExited } "suspended job launch"
    $S.pid = [int]((Get-Content -LiteralPath (Join-Path $S.evidence "root.json") -Raw | ConvertFrom-Json).pid)
    $S.shell = Get-Process -Id $S.pid; $S.shell_handle = $S.shell.Handle
    Wait-Until { if (Log-Has "sidecar_start_failed") { throw "sidecar_start_failed before hello" }; (Log-Has "shell_window_ready") -and (Log-Has "sidecar_ready") } "shell window + sidecar hello (window-first launch, 11-05)" 60
    Wait-Until { Log-Has "tray_icon_added" } "tray icon added" 10
    Wait-Until { Test-Path -LiteralPath (Join-Path $script:State "ready") } "fixture ready" 10
    $S.tray = [P3Notify]::Find("TesseractShellTray", [NullString]::Value, [uint32]$S.pid)
    $S.main = [P3Notify]::Find([NullString]::Value, "Tesseract Shell (P3)", [uint32]$S.pid)
    if ($S.tray -eq [IntPtr]::Zero -or $S.main -eq [IntPtr]::Zero) { throw "tray or main window not found" }

    # 1. Page and language from hello.
    Wait-Until { (Page-Eval "typeof window.devSubscribe") -eq "function" } "dev page script" 30
    [void](Page-Eval "(window.devSubscribe(), true)")
    Wait-Until { (Page-Count "f.t === 'hello'") -ge 1 } "page subscribed" 15
    Wait-Until { (Page-Count "f.t === 'host:language' && f.language === 'zh_CN'") -ge 1 } "host:language from hello" 10
    if (-not (Log-Has "language_applied lang=zh_CN source=hello")) { throw "hello language not logged" }
    $Subscribe = @(Fixture-Frames | Where-Object { $_.t -eq "subscribe" })[0]
    $S.checks.subscribed_topics = @($Subscribe.topics)
    if (@($Subscribe.topics) -notcontains "alerts.desktop" -or @($Subscribe.topics) -notcontains "ui.language") { throw "shell topics not subscribed" }

    # 2. Presence: full value, then the page id (whitelist only).
    Wait-Until { @(Presences).Count -ge 1 } "first presence" 10
    $S.checks.first_presence = Last-Presence
    if (-not $S.checks.first_presence.visible) { throw "the shown window must report visible=true" }
    $S.checks.set_page_logs = Page-Invoke "host_set_page" "{page: 'logs'}"
    if ($S.checks.set_page_logs -ne "ok:null") { throw "host_set_page logs: $($S.checks.set_page_logs)" }
    Wait-Until { (Last-Presence).page -eq "logs" } "presence page=logs" 10
    $S.checks.set_page_bogus = Page-Invoke "host_set_page" "{page: 'update'}"
    if ($S.checks.set_page_bogus -notlike "err:*invalid_page*") { throw "a non-whitelisted page id was accepted: $($S.checks.set_page_bogus)" }

    # 3. The page cannot receipt by itself.
    $S.checks.page_receipt = Page-Invoke "rt" "{req_id: 'p305-forged', cmd: 'alerts.desktopDelivered', args: {delivery_id: 'x', outcome: 'delivered', surface: 'in_app'}, owner: 'dev-rt'}"
    if ($S.checks.page_receipt -notlike "err:*not_allowed*") { throw "page receipt was not refused: $($S.checks.page_receipt)" }
    $S.checks.unknown_render = Page-Invoke "host_notify_rendered" "{delivery_id: 'desktop-never-sent'}"
    if ($S.checks.unknown_render -notlike "err:*unknown_delivery*") { throw "an unknown delivery was confirmed: $($S.checks.unknown_render)" }

    # 4. Close to tray: host:visibility false and presence hidden; a notice goes outside the window:
    #    the card window (route=card) or its tray-balloon fallback (route=windows), delivered/windows.
    [void][P3Notify]::PostMessageW($S.main, $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero)
    Wait-Until { Log-Has "window_closed_to_tray" } "closed to tray" 10
    Wait-Until { (Page-Count "f.t === 'host:visibility' && f.visible === false") -ge 1 } "host:visibility false" 10
    Wait-Until { -not (Last-Presence).visible } "presence hidden" 10
    Push-Event (Desktop "desktop-smoke-bg" "T-P3-05 notify smoke (background)")
    $Route = Wait-Shown "desktop-smoke-bg"
    $S.checks.background_route = $Route.Groups[1].Value
    if ($Route.Groups[3].Value -ne "false") { throw "background route not logged as hidden: $($Route.Value)" }
    $Shown = "desktop-smoke-bg"
    $S.checks.background_held = $S.checks.background_route -eq "held"
    if ($S.checks.background_held) {
        # Windows says the user is busy: held, and a held notice is not delivered (no receipt).
        Note-Busy "desktop-smoke-bg"
        if (@(Receipts "desktop-smoke-bg").Count -ne 0) { throw "a held notice was receipted" }
        # A forced preview is never held: it stands in for the hidden-window notice.
        $Shown = "desktop-test-smoke-bg"
        Push-Event (Desktop $Shown "T-P3-05 notify smoke (background, forced)")
        $Route = Wait-Shown $Shown
        if ($Route.Groups[2].Value -ne "true" -or $Route.Groups[3].Value -ne "false") { throw "forced background route: $($Route.Value)" }
    }
    $Surface = $Route.Groups[1].Value
    if (@("card","windows") -notcontains $Surface) { throw "background notice routed $Surface, expected card or windows" }
    $S.checks.background_surface = $Surface
    $S.checks.background = Expect-Receipt $Shown "delivered" "windows"

    # 5. Open it: the card's body or its View button (notice_open from the card page), or the
    #    balloon (the card window's fallback): restore, then host:navigate{page: target}.
    if ($Surface -eq "card") {
        Wait-Until { $null -ne (Page-Socket "notice.html") } "card page target" 10
        $S.checks.card_open = Page-Invoke "notice_open" "{delivery_id: '$Shown'}" "notice.html"
        if ($S.checks.card_open -ne "ok:null") { throw "notice_open: $($S.checks.card_open)" }
        Wait-Until { Log-Has "notice_opened target=alerts" } "card opened" 10
    } else {
        [void][P3Notify]::PostMessageW($S.tray, $TrayCallback, [IntPtr]::Zero, [IntPtr]$NIN_BALLOONUSERCLICK)
    }
    Wait-Until { Log-Has "tray_balloon_navigate page=alerts" } "$Surface click navigate" 10
    Wait-Until { (Page-Count "f.t === 'host:navigate' && f.page === 'alerts'") -ge 1 } "host:navigate on the page" 10
    Wait-Until { [P3Notify]::IsWindowVisible($S.main) } "window restored" 10
    Wait-Until { (Page-Count "f.t === 'host:visibility' && f.visible === true") -ge 2 } "host:visibility true again" 10
    Wait-Until { (Last-Presence).visible } "presence visible again" 10
    # Order on the page: hidden (close to tray) -> visible again -> navigate.
    $S.checks.navigate_after_visible = [bool](Page-Eval "(() => { const a = window.devFrames; const h = a.map(f => f.t === 'host:visibility' && f.visible === false).lastIndexOf(true); const v = a.findIndex((f, i) => i > h && f.t === 'host:visibility' && f.visible === true); const n = a.findIndex((f, i) => i > h && f.t === 'host:navigate'); return h >= 0 && v > h && n > v; })()")
    if (-not $S.checks.navigate_after_visible) { throw "host:navigate reached the page before the restored window was visible" }
    if (-not (Log-Has "tray_balloon_navigate page=alerts visible=true")) { throw "navigate not logged as sent to a visible window" }
    $S.checks.click = "${Surface}:restored+navigate:alerts"

    # 6. In front: in-app, page confirmation; a silent page is dropped after 3 s.
    if ([P3Notify]::GetForegroundWindow() -ne $S.main) { [void][P3Notify]::SetForegroundWindow($S.main); Start-Sleep -Milliseconds 300 }
    $S.checks.foreground = ([P3Notify]::GetForegroundWindow() -eq $S.main)
    Wait-Until { [bool](Last-Presence).focused -eq $S.checks.foreground } "presence focused=$($S.checks.foreground)" 10
    if ($S.checks.foreground) {
        # The page has not declared host_notify_ready yet: even in front, not in the page: the
        # card (or its balloon), held while Windows says the user is busy.
        Push-Event (Desktop "desktop-smoke-unready" "T-P3-05 notify smoke (page not ready)")
        $Route = Wait-Shown "desktop-smoke-unready"
        $S.checks.unready_route = $Route.Groups[1].Value
        if (@("card","windows","held") -notcontains $S.checks.unready_route) { throw "an undeclared page's notice was routed $($S.checks.unready_route)" }
        if (-not (Log-Has "notify_route delivery=desktop-smoke-unready route=$($S.checks.unready_route) force=false visible=true focused=true severity=info target=alerts page=unready")) { throw "an undeclared page was not routed outside the page: $($Route.Value)" }
        if ($S.checks.unready_route -eq "held") { Note-Busy "desktop-smoke-unready" } else { $S.checks.unready = Expect-Receipt "desktop-smoke-unready" "delivered" "windows" }
        if ((Page-Count "f.t === 'host:notify'") -ne 0) { throw "an undeclared page got host:notify" }
    }
    $S.checks.notify_ready = Page-Invoke "host_notify_ready" "{}"
    if ($S.checks.notify_ready -ne "ok:null") { throw "host_notify_ready: $($S.checks.notify_ready)" }
    Wait-Until { Log-Has "notify_page_ready page=" } "notify_page_ready logged" 10
    # The front step needs the window in front when it is routed, as the silent step below: regain
    # the foreground first (a held notice before it waits seconds, and the user may click elsewhere
    # meanwhile); a route with focused=false means another window took it: outside the page is then
    # the right answer, and one retry follows with a new id.
    $FrontTries = @()
    foreach ($Try in 1..2) {
        $FrontId = if ($Try -eq 1) { "desktop-smoke-fg" } else { "desktop-smoke-fg-retry" }
        if ($S.checks.foreground) {
            if ([P3Notify]::GetForegroundWindow() -ne $S.main) { [void][P3Notify]::SetForegroundWindow($S.main); Start-Sleep -Milliseconds 300 }
            try { Wait-Until { [bool](Last-Presence).focused } "presence focused before the front step" 5 } catch { }
        }
        Push-Event (Desktop $FrontId "T-P3-05 notify smoke (front)")
        $Route = Wait-Shown $FrontId
        $FrontTries += "$FrontId route=$($Route.Groups[1].Value) focused=$($Route.Groups[4].Value)"
        if (-not $S.checks.foreground -or $Route.Groups[4].Value -eq "true") { break }
        if (@("card","windows","held") -notcontains $Route.Groups[1].Value) { throw "not in front, yet route $($Route.Groups[1].Value)" }
        if ($Route.Groups[1].Value -eq "held") { Note-Busy $FrontId } else { [void](Expect-Receipt $FrontId "delivered" "windows") }
    }
    $S.checks.front_tries = $FrontTries
    $S.checks.front_route = $Route.Groups[1].Value
    if ($S.checks.foreground) {
        if ($Route.Groups[4].Value -ne "true") { throw "environment: the foreground was taken before the front step was routed, twice ($($FrontTries -join '; '))" }
        if ($S.checks.front_route -ne "in_app") { throw "in front with a page: route $($S.checks.front_route)" }
        Wait-Until { (Page-Count "f.t === 'host:notify' && f.delivery_id === '$FrontId'") -ge 1 } "host:notify on the page" 5
        $S.checks.rendered = Page-Invoke "host_notify_rendered" "{delivery_id: '$FrontId'}"
        if ($S.checks.rendered -ne "ok:null") { throw "render confirmation refused: $($S.checks.rendered)" }
        $S.checks.front = Expect-Receipt $FrontId "delivered" "in_app"
        $S.checks.second_render = Page-Invoke "host_notify_rendered" "{delivery_id: '$FrontId'}"
        if ($S.checks.second_render -notlike "err:*already_receipted*") { throw "a second confirmation was accepted" }
        # The silent step needs the window in front when it is routed (T-P3-H fix 1, S4). Regain the
        # foreground first; if the route still says focused=false, another window took the
        # foreground (outside the page is then the right answer: the card, or a hold while Windows
        # says the user is busy): retry once with a new id, and if it happens again, fail as an
        # environment problem rather than a contract error.
        $SilentTries = @()
        foreach ($Try in 1..2) {
            $SilentId = if ($Try -eq 1) { "desktop-smoke-silent" } else { "desktop-smoke-silent-retry" }
            if ([P3Notify]::GetForegroundWindow() -ne $S.main) { [void][P3Notify]::SetForegroundWindow($S.main); Start-Sleep -Milliseconds 300 }
            try { Wait-Until { [bool](Last-Presence).focused } "presence focused before the silent step" 5 } catch { }
            Push-Event (Desktop $SilentId "T-P3-05 notify smoke (silent page)")
            $SilentRoute = Wait-Shown $SilentId
            $SilentTries += "$SilentId route=$($SilentRoute.Groups[1].Value) focused=$($SilentRoute.Groups[4].Value)"
            if ($SilentRoute.Groups[4].Value -eq "true") {
                if ($SilentRoute.Groups[1].Value -ne "in_app") { throw "in front with a ready page: route $($SilentRoute.Groups[1].Value)" }
                $S.checks.silent = Expect-Receipt $SilentId "dropped" "none" 10; break
            }
            if ($SilentRoute.Groups[1].Value -eq "held") { Note-Busy $SilentId } else { [void](Expect-Receipt $SilentId "delivered" "windows") }
        }
        $S.checks.silent_tries = $SilentTries
        if (-not $S.checks.silent) { throw "environment: the foreground was taken before the silent step was routed, twice ($($SilentTries -join '; '))" }
    } else {
        # Windows kept the foreground elsewhere: the route must follow the probed state (outside the
        # page: the card or its balloon, held while Windows says the user is busy).
        if (@("card","windows","held") -notcontains $S.checks.front_route -or $Route.Groups[4].Value -ne "false") { throw "not in front, yet route $($S.checks.front_route)" }
        if ($S.checks.front_route -eq "held") { Note-Busy $FrontId } else { $S.checks.front = Expect-Receipt $FrontId "delivered" "windows" }
        $S.checks.in_app_note = "foreground refused by Windows; the in-app branch is covered by cargo test shell_topics_route_notices_report_presence_and_switch_language"
    }

    # 7. A forced test always tries Windows (never held, never in the page); a repeated id gets no
    #    second receipt (a held one counts as known too).
    Push-Event (Desktop "desktop-test-smoke" "T-P3-05 notify smoke (forced test)")
    $Route = Wait-Shown "desktop-test-smoke"
    if (@("card","windows") -notcontains $Route.Groups[1].Value -or $Route.Groups[2].Value -ne "true") { throw "forced test routed: $($Route.Value)" }
    $S.checks.forced = Expect-Receipt "desktop-test-smoke" "delivered" "windows"
    Push-Event (Desktop "desktop-smoke-bg" "T-P3-05 notify smoke (repeat)")
    Wait-Until { Log-Has "notify_route delivery=desktop-smoke-bg route=duplicate" } "duplicate logged" 10
    Start-Sleep -Milliseconds 500
    $S.checks.repeat_receipts = @(Receipts "desktop-smoke-bg").Count
    # Shown at step 4: exactly its one receipt. Held there: none, or one if the shell released it
    # meanwhile (the user became free); never two.
    $Fewest = if ($S.checks.background_held) { 0 } else { 1 }
    if ($S.checks.repeat_receipts -lt $Fewest -or $S.checks.repeat_receipts -gt 1) { throw "desktop-smoke-bg receipted $($S.checks.repeat_receipts) times" }

    # 8. ui.language: native text and the page follow it.
    Push-Event ([ordered]@{ topic = "ui.language"; kind = "edge"; data = [ordered]@{ language = "ja_JP"; config_rev = 2 } })
    Wait-Until { Log-Has "language_applied lang=ja_JP source=topic" } "language logged" 10
    Wait-Until { (Page-Count "f.t === 'host:language' && f.language === 'ja_JP'") -ge 1 } "host:language ja_JP" 10
    $S.checks.language = "ja_JP"
    if ((Page-Count "f.topic === 'alerts.desktop' || f.topic === 'ui.language'") -ne 0) { throw "a shell topic reached the page" }
    # hello.lang and the first ui.language snapshot carry the same zh_CN: the page hears it once.
    $S.checks.zh_language_frames = Page-Count "f.t === 'host:language' && f.language === 'zh_CN'"
    if ($S.checks.zh_language_frames -ne 1) { throw "host:language zh_CN reached the page $($S.checks.zh_language_frames) times" }
    if ((Page-Count "f.t === 'res' && f.id > 1") -ne 0) { throw "a reply to a shell report reached the page" }
    $S.checks.receipts = @(Fixture-Frames | Where-Object { $_.cmd -eq "alerts.desktopDelivered" } | ForEach-Object { "$($_.args.delivery_id)=$($_.args.outcome)/$($_.args.surface)" })
    $S.checks.presences = @(Presences | ForEach-Object { "visible=$($_.visible) focused=$($_.focused) page=$($_.page)" })
    [IO.File]::WriteAllText((Join-Path $S.evidence "page-frames.json"), [string](Page-Eval "JSON.stringify(window.devFrames.filter(f => f.t.startsWith('host:')))"), (New-Object Text.UTF8Encoding $false))

    # 9. Tray Quit; the dev page answers the confirmation with 0.
    [void][P3Notify]::PostMessageW($S.tray, $WM_COMMAND, [IntPtr]$CmdQuit, [IntPtr]::Zero)
    Wait-Until { (Page-Count "f.t === 'host:confirmQuit'") -ge 1 } "host:confirmQuit" 10
    $Token = [int](Page-Eval "window.devFrames.find(f => f.t === 'host:confirmQuit').token")
    [void](Page-Eval "(window.__TAURI__.core.invoke('host_confirm_quit', {token: $Token, unsaved: 0}), true)")
    if (-not $S.shell.WaitForExit(30000)) { throw "shell did not exit" }
    $S.shell.Refresh(); $S.checks.shell_exit_code = $S.shell.ExitCode
    if ($S.checks.shell_exit_code -ne 0) { throw "shell exit $($S.checks.shell_exit_code)" }
    if (-not $S.watcher.WaitForExit($InventoryWaitMs)) { throw (Inventory-TimeoutMessage (Join-Path $S.evidence "process-inventory.json")) }
    $Inventory = Get-Content -LiteralPath (Join-Path $S.evidence "process-inventory.json") -Raw | ConvertFrom-Json
    if (-not $Inventory.kernel_membership_complete -or $Inventory.active_processes -ne 0 -or [int64]$Inventory.root_exit_code -ne 0) { throw "owned process residual detected" }
    # process_watch gives this scope's msedgewebview2 20 s after the shell exits (decision P3-06 item 6, T-P3-H).
    if ($Inventory.webview_grace_used) { $S.checks.webview_late_exit_s = $Inventory.last_exit_after_root_s }
    $S.checks.no_residue = $true
    # 11-18: the stand-in refuses what the real sidecar refuses (shell/app/src/fixture_contract.py)
    $Refused = Join-Path $script:State "contract-violations.jsonl"
    if (Test-Path -LiteralPath $Refused) { throw "the real sidecar refuses frames the shell sent: $((Get-Content -LiteralPath $Refused -TotalCount 5) -join ' | ')" }
    # Every contract check above passed; an unforced notice outside the page was never shown,
    # because Windows said the user was busy: not a pass on this machine state.
    if ($S.checks.environment) { throw "environment: $($S.checks.environment); every other check passed" }
    $Passed = $true
} catch {
    $Failure = $_.Exception.Message
} finally {
    # T-P3-H2 fix 3: a launch that gave up never learned its shell; end one the watcher started late.
    if (-not $S.shell) { $Late = Resolve-LateShell (Join-Path $S.evidence "root.json") $S.watcher; if ($Late) { $Cleanup += Close-LateShell $Late $S.evidence } }
    if (-not $Passed -and $S.shell -and -not $S.shell.HasExited) {
        if ($S.tray -and $S.tray -ne [IntPtr]::Zero) { [void][P3Notify]::PostMessageW($S.tray, $WM_COMMAND, [IntPtr]$CmdQuit, [IntPtr]::Zero); $Cleanup += "tray_quit" }
        try {
            $Token = [int](Page-Eval "(window.devFrames.filter(f => f.t === 'host:confirmQuit').pop() || {token: 0}).token")
            if ($Token -gt 0) { [void](Page-Eval "(window.__TAURI__.core.invoke('host_confirm_quit', {token: $Token, unsaved: 0}), true)"); $Cleanup += "confirm_0" }
        } catch { }
        if (-not $S.shell.WaitForExit(30000)) { Stop-Process -Id $S.pid -Force -ErrorAction SilentlyContinue; $Cleanup += "stop_scoped_shell" }
        $Cleanup += "exited=" + $S.shell.HasExited
    }
    if ($S.watcher -and -not $S.watcher.HasExited) { [void]$S.watcher.WaitForExit($InventoryWaitMs) }
    foreach ($Var in $Names) { [Environment]::SetEnvironmentVariable($Var,$Previous[$Var],"Process") }
    if (Test-Path -LiteralPath $script:ShellLog) { Copy-Item -LiteralPath $script:ShellLog -Destination (Join-Path $S.evidence "shell.log") }
    $Frames = Join-Path $script:State "frames-1.jsonl"
    if (Test-Path -LiteralPath $Frames) { Copy-Item -LiteralPath $Frames -Destination (Join-Path $S.evidence "fixture-frames.jsonl") }
    $Result = [ordered]@{ passed = $Passed; error = $Failure; cleanup = $Cleanup; scope = $S.tag; data = $S.data; checks = $S.checks }
    [IO.File]::WriteAllText((Join-Path $S.evidence "result.json"), ($Result | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding $false))
}
if (-not $Passed) { throw "P3-05 notify smoke failed: $Failure (evidence: $($S.evidence))" }
Write-Output "P3-05 notify smoke passed (foreground=$($S.checks.foreground); evidence: $($S.evidence))"

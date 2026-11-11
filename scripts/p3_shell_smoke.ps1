[CmdletBinding()]
# T-P3-07 adds two scenarios after the original one ("main"):
#   webview - DWM cloak until host_ready (DWMWA_CLOAKED), V2 render/rAF measurements, renderer crash
#             (CDP Page.crash) -> reload + cache replay, browser-process kill -> window recreated,
#             BrowserProcessExited wait at exit;
#   missing - simulated missing WebView2 runtime (debug env) -> native box, exit 1, no sidecar;
#   lost    - browser-process kill while every recreation fails (debug env) -> 4 attempts with
#             0.5/1/2 s backoff, native box with one button, then the normal quit (app.quit, bye);
#   lostbuilt - the same, but every attempt first builds the real window and then fails, so the
#             destroy + label wait before each retry runs (fix round 1, B-S2).
# T-P4-C3 adds "c3" (the built app page on the notify stand-in sidecar: host:caps, rt:replayed and
# the first live tier.transition, rt_resync, reduced motion, D20 tray icon states) and checks the
# host:caps / rt:replayed replay frames in "webview" (real sidecar, dev page).
# -RequireTierRecover (c3 only): the recovery after the page reload must be exactly one live rt:tier
# recover (integration, after rebuilding shell/app/ui); without it only liveness is checked (T-P4-H3:
# no guess from the bundle's source).
# -ForceExitWaitTimeout (main only): the debug shell ignores BrowserProcessExited, so the exit wait
# must run into its 12 s cap and the scenario must still pass (fix round 1, M1).
# Residue checks give this scope's msedgewebview2 processes 20 s (decision P3-06 item 6); process_watch.py
# applies the same rule to the kernel job (T-P3-H). Exit-wait timeouts are read as numbers (12-15 s).
# Results stay in each %TEMP% sandbox; -EvidenceDir copies result.json and shell.log there.
# T-P3-H2 (31b-17): the renderer crash in "webview" no longer fires Page.crash blind. The target is
# confirmed (one dev-rt page target, its id), the command's outcome is read back (the target's
# Inspector.targetCrashed / detach / socket close, or a refusal reply), and before any re-send the
# renderer is checked: still answering -> Page.crash once more on a fresh session to the same target;
# alive but silent -> its crash handling (minidump) is still running, keep waiting (the lab measured
# 10-30 s on a throttled machine); gone without the shell's render_exited -> fail (a shell finding,
# not masked). Only after a 60 s budget: end this scope's single renderer process (same
# render_exited path in the shell, so the reload is still exercised) - the scenario then fails at its
# end all the same (checks.page_crash_fallback; fix 1). checks.page_crash records every attempt and the timings.
# T-P3-H2 (C3-8): residue failures write residue-tree.json (Save-ResidueTree, p3_residue.ps1).
param([string] $Sidecar = "", [switch] $SkipBuild, [string[]] $Scenarios = @("main","webview","missing","lost","lostbuilt","c3"), [string] $EvidenceDir = "",
    [switch] $ForceExitWaitTimeout, [switch] $RequireTierRecover)
$ErrorActionPreference = "Stop"
$Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
. (Join-Path $PSScriptRoot "p3_residue.ps1")
# `powershell -File ... -Scenarios a,b` passes one string; accept both forms.
$Scenarios = @($Scenarios | ForEach-Object { $_ -split "," } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
foreach ($Scenario in $Scenarios) { if (@("main","webview","missing","lost","lostbuilt","c3") -notcontains $Scenario) { throw "unknown scenario: $Scenario" } }
if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot "shell_copy_ui.ps1")
    Push-Location -LiteralPath (Join-Path $Repo "shell")
    try { & cargo build --offline -j 1; if ($LASTEXITCODE -ne 0) { throw "cargo build failed" } }
    finally { Pop-Location }
}
# --- T-P3-07 helpers (shared by the webview and missing scenarios) ---
$Python = Join-Path $Repo ".venv/Scripts/python.exe"
# Bytecode warm-up as in p3_exit_smoke.ps1 (T-P3-H fix 1, S7): the real sidecar runs with -B, so
# without a cache every start compiles desktop_runtime.py (~2 MB) again, which alone can pass the
# shell's 10 s hello window on a loaded machine. One import writes the bytecode into a temporary
# PYTHONPYCACHEPREFIX (never the source tree); every scenario's sidecar only reads it. The shell's
# hello timeout is unchanged.
$PyCache = Join-Path ([IO.Path]::GetTempPath()) ("tesseract-p3-pycache-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $PyCache | Out-Null
$SavedWarm = @{ PYTHONPYCACHEPREFIX = $env:PYTHONPYCACHEPREFIX; PYTHONDONTWRITEBYTECODE = $env:PYTHONDONTWRITEBYTECODE }
$env:PYTHONPYCACHEPREFIX = $PyCache; Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
$WarmStart = [DateTime]::UtcNow
Push-Location $Repo; try { & $Python -c "import bridge_main, desktop_runtime" } finally { Pop-Location }
Write-Output ("warm-up import rc={0} {1:N1} s (bytecode cache {2})" -f $LASTEXITCODE, ([DateTime]::UtcNow - $WarmStart).TotalSeconds, $PyCache)
foreach ($Key in @($SavedWarm.Keys)) { [Environment]::SetEnvironmentVariable($Key,$SavedWarm[$Key],"Process") }
$ShellExe = Join-Path $Repo "shell/target/debug/tesseract-shell.exe"
if (-not (Test-Path -LiteralPath $ShellExe)) { throw "debug shell missing; build it first" }
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Net;
using System.Net.WebSockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
public static class P3Web {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindowExW(IntPtr parent, IntPtr after, string cls, string title);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int cmd);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern int GetWindowLongW(IntPtr hwnd, int index);
    [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out int value, int size);
    public static IntPtr Find(string cls, string title, uint pid) {
        IntPtr after = IntPtr.Zero;
        while (true) {
            after = FindWindowExW(IntPtr.Zero, after, cls, title);
            if (after == IntPtr.Zero) return IntPtr.Zero;
            uint owner; GetWindowThreadProcessId(after, out owner);
            if (owner == pid) return after;
        }
    }
    // DWMWA_CLOAKED (14): 0 = shown, >0 = cloaked (1 = by the app), -1 = query failed.
    public static int Cloaked(IntPtr hwnd) { int value; return DwmGetWindowAttribute(hwnd, 14, out value, 4) == 0 ? value : -1; }
    public static bool Topmost(IntPtr hwnd) { return (GetWindowLongW(hwnd, -20) & 0x8) != 0; }
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool SystemParametersInfoW(uint action, uint param, out uint value, uint ini);
    // SPI_GETFOREGROUNDLOCKTIMEOUT (0x2000): recorded with a topmost check, -1 = query failed.
    public static long ForegroundLockTimeout() { uint value; return SystemParametersInfoW(0x2000, 0, out value, 0) ? (long)value : -1L; }
    public static string Http(string url) {
        HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url); request.Proxy = null; request.Timeout = 3000;
        using (WebResponse response = request.GetResponse()) using (StreamReader reader = new StreamReader(response.GetResponseStream())) return reader.ReadToEnd();
    }
}
// One CDP session on the debug-only loopback port; commands come from this script only.
public sealed class P3Cdp : IDisposable {
    readonly ClientWebSocket ws = new ClientWebSocket(); int next = 0;
    static CancellationToken After(int ms) { return new CancellationTokenSource(ms).Token; }
    public P3Cdp(string url) { ws.Options.Proxy = null; ws.ConnectAsync(new Uri(url), After(8000)).Wait(); }
    void Send(int id, string method, string parameters, CancellationToken token) {
        string body = "{\"id\":" + id + ",\"method\":\"" + method + "\",\"params\":" + parameters + "}";
        ws.SendAsync(new ArraySegment<byte>(Encoding.UTF8.GetBytes(body)), WebSocketMessageType.Text, true, token).Wait();
    }
    // The reply to one command (events in between are skipped).
    public string Call(string method, string parameters, int ms) {
        int id = ++next; CancellationToken token = After(ms); Send(id, method, parameters, token);
        byte[] buffer = new byte[65536]; string prefix = "{\"id\":" + id + ",";
        while (true) {
            MemoryStream message = new MemoryStream(); WebSocketReceiveResult part;
            do {
                part = ws.ReceiveAsync(new ArraySegment<byte>(buffer), token).Result;
                if (part.MessageType == WebSocketMessageType.Close) throw new IOException("cdp_closed");
                message.Write(buffer, 0, part.Count);
            } while (!part.EndOfMessage);
            string text = Encoding.UTF8.GetString(message.ToArray());
            if (text.StartsWith(prefix)) return text;
        }
    }
    // A command whose target may die before it answers (Page.crash).
    public void Fire(string method, string parameters) { Send(++next, method, parameters, After(3000)); }
    // T-P3-H2: Page.crash with its outcome read back for up to `ms`: "target_crashed" (the target's
    // Inspector.targetCrashed event), "detached:<message>", "closed" (the browser closed the session),
    // "reply:<message>" (answered, i.e. refused - e.g. a pending navigation), "no_reply" (nothing
    // within `ms`), "socket_error:<type>". The session is unusable afterwards either way.
    public string Crash(int ms) {
        int id = ++next; Send(id, "Page.crash", "{}", After(3000));
        string prefix = "{\"id\":" + id + ","; byte[] buffer = new byte[65536]; CancellationToken token = After(ms);
        try {
            while (true) {
                MemoryStream message = new MemoryStream(); WebSocketReceiveResult part;
                do {
                    part = ws.ReceiveAsync(new ArraySegment<byte>(buffer), token).Result;
                    if (part.MessageType == WebSocketMessageType.Close) return "closed";
                    message.Write(buffer, 0, part.Count);
                } while (!part.EndOfMessage);
                string text = Encoding.UTF8.GetString(message.ToArray());
                if (text.StartsWith(prefix)) return "reply:" + Short(text);
                if (text.Contains("\"Inspector.targetCrashed\"")) return "target_crashed";
                if (text.Contains("\"Inspector.detached\"")) return "detached:" + Short(text);
            }
        } catch (AggregateException e) {
            if (token.IsCancellationRequested) return "no_reply";
            return "socket_error:" + e.InnerException.GetType().Name;
        } catch (Exception e) { return "socket_error:" + e.GetType().Name; }
    }
    static string Short(string text) { return text.Length > 300 ? text.Substring(0, 300) : text; }
    public void Dispose() { try { ws.Abort(); } catch { } ws.Dispose(); }
}
'@
function Save-SmokeEvidence([string] $Box, [string] $Name) {
    $Target = Join-Path $EvidenceDir ("shell-" + $Name + "-" + (Split-Path -Leaf $Box))
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    foreach ($File in @("result.json","summary.json","process-inventory.json","residue-tree.json","late-shell.json","cdp.log","cdp-errors.log")) {
        $From = Join-Path $Box $File; if (Test-Path -LiteralPath $From) { Copy-Item -LiteralPath $From -Destination $Target }
    }
    $Log = Join-Path $Box "data/logs/shell.log"; if (Test-Path -LiteralPath $Log) { Copy-Item -LiteralPath $Log -Destination (Join-Path $Target "shell.log") }
    Write-Output "evidence copied: $Target"
}
# A fresh %TEMP% sandbox with the isolated example configuration (Tier3, sources, system proxy,
# alerts and kill switch off), like the main scenario.
function New-SmokeSandbox([string] $Name) {
    $Box = Join-Path ([IO.Path]::GetTempPath()) ("tesseract-p3-" + $Name + "-" + [Guid]::NewGuid().ToString("N"))
    $Paths = @{ box = $Box; config = (Join-Path $Box "config"); data = (Join-Path $Box "data"); temp = (Join-Path $Box "temp") }
    foreach ($Dir in @($Paths.config,$Paths.data,$Paths.temp)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
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
'@ $Repo $Paths.config
    if ($LASTEXITCODE -ne 0) { throw "isolated config setup failed" }
    return $Paths
}
function Wait-Until([scriptblock] $Condition, [string] $Label, [int] $Seconds = 15) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while (-not (& $Condition)) {
        if ([DateTime]::UtcNow -gt $Deadline) { throw "timeout: $Label" }
        Start-Sleep -Milliseconds 100
    }
}
function Log-Count([string] $Log, [string] $Pattern) {
    if (-not (Test-Path -LiteralPath $Log)) { return 0 }
    return @((Get-Content -LiteralPath $Log) | Where-Object { $_ -match $Pattern }).Count
}
function Free-Port { $L = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0); $L.Start(); $P = $L.LocalEndpoint.Port; $L.Stop(); return $P }
function Start-WatchedShell($Box, [string[]] $Arguments) {
    $WatchArguments = @("-B",('"'+(Join-Path $Repo "shell/app/process_watch.py")+'"'),"--output",('"'+(Join-Path $Box.box "process-inventory.json")+'"'),"--ready",('"'+(Join-Path $Box.box "root.json")+'"'),"--",('"'+$ShellExe+'"')) + $Arguments
    $Watcher = Start-Process -FilePath $Python -ArgumentList $WatchArguments -WorkingDirectory $Repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Box.box "stdout.log") -RedirectStandardError (Join-Path $Box.box "stderr.log")
    $null = $Watcher.Handle
    # T-P3-H2 fix 3: known to the scenario's cleanup even when the launch below gives up, so a shell
    # the watcher starts late is still found (Resolve-LateShell) and ended (Close-LateShell).
    $script:Watcher = $Watcher
    Wait-Until { (Test-Path -LiteralPath (Join-Path $Box.box "root.json")) -or $Watcher.HasExited } "suspended job launch"
    if (-not (Test-Path -LiteralPath (Join-Path $Box.box "root.json"))) { throw "suspended job launch failed: $($Box.box)" }
    return $Watcher
}
function Cdp-Eval($Cdp, [string] $Expression, [int] $Ms = 8000) {
    $Parameters = @{ expression = $Expression; awaitPromise = $true; returnByValue = $true } | ConvertTo-Json -Compress
    $Reply = $Cdp.Call("Runtime.evaluate", $Parameters, $Ms) | ConvertFrom-Json
    if ($Reply.error -or $Reply.result.exceptionDetails) { throw "evaluate failed: $Expression" }
    return $Reply.result.result.value
}
# Connects to the dev-rt page target (a reloaded or recreated WebView included) once its script runs.
function Cdp-Connect([int] $Port, [int] $Seconds = 30) {
    $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
    while ([DateTime]::UtcNow -lt $Deadline) {
        $Cdp = $null
        try {
            $Pages = [P3Web]::Http("http://127.0.0.1:$Port/json/list") | ConvertFrom-Json
            $Page = @($Pages | Where-Object { $_.type -eq "page" -and $_.url -like "*dev-rt.html*" }) | Select-Object -First 1
            if ($Page) {
                $Cdp = New-Object P3Cdp $Page.webSocketDebuggerUrl
                # T-P3-H2: the target this session talks to, and how many dev-rt page targets exist.
                $script:CdpTarget = [ordered]@{ id = [string]$Page.id; url = [string]$Page.url; type = [string]$Page.type
                    dev_rt_page_targets = @($Pages | Where-Object { $_.type -eq "page" -and $_.url -like "*dev-rt.html*" }).Count }
                if ((Cdp-Eval $Cdp "typeof window.devSubscribe === 'function'") -eq $true) { return $Cdp }
                $Cdp.Dispose()
            }
        } catch { if ($Cdp) { $Cdp.Dispose() } }
        Start-Sleep -Milliseconds 200
    }
    throw "dev-rt CDP target not ready"
}
# V2: requestAnimationFrame callbacks within 1 s, and what the page believes about its visibility.
$RafProbe = "new Promise(r=>{let n=0,done=false;const s=performance.now();const end=t=>{if(!done){done=true;r({frames:n,visibility:document.visibilityState,hidden:document.hidden,timer_fallback:t})}};const f=()=>{n++;if(performance.now()-s<1000)requestAnimationFrame(f);else end(false)};requestAnimationFrame(f);setTimeout(()=>end(true),2500)})"
function Subscribe-Replay($Cdp) {
    [void](Cdp-Eval $Cdp "window.devSubscribe().then(()=>true)")
    Wait-Until { (Cdp-Eval $Cdp "window.devFrames.some(f=>f.t==='hello') && window.devFrames.some(f=>f.topic==='service.state')") -eq $true } "hello and service.state replay" 20
    return (Cdp-Eval $Cdp "(()=>{const h=window.devFrames.find(f=>f.t==='hello');return {epoch:h.epoch,runtime_pid:h.runtime_pid,topics:[...new Set(window.devFrames.filter(f=>f.topic).map(f=>f.topic))].sort()}})()")
}
# T-P3-H2 (31b-17): this scope's renderer processes (pid -> creation time) and whether one is alive.
function Scoped-Renderers([string] $Data) {
    return @(Scoped-WebViews $Data | Where-Object { [string]$_.CommandLine -match '--type=renderer' -and [string]$_.CommandLine -notmatch '--extension-process' } |
        ForEach-Object { [ordered]@{ pid = [int]$_.ProcessId; created = $_.CreationDate.ToUniversalTime().ToString("o") } })
}
function Renderer-Alive($Renderer) {
    $P = Get-Process -Id $Renderer.pid -ErrorAction SilentlyContinue
    if (-not $P) { return $false }
    # Same process, not a reused PID: creation times agree (CIM and .NET round differently).
    $Created = [DateTime]::Parse($Renderer.created, $null, [Globalization.DateTimeStyles]::RoundtripKind)
    try { return [Math]::Abs(($P.StartTime.ToUniversalTime() - $Created).TotalSeconds) -lt 1 } catch { return $true }
}
# Whether the dev-rt page still answers on a fresh session to the same target (3 s at most).
function Page-Answers([int] $Port, [string] $TargetId) {
    try {
        $Pages = [P3Web]::Http("http://127.0.0.1:$Port/json/list") | ConvertFrom-Json
        $Page = @($Pages | Where-Object { [string]$_.id -eq $TargetId }) | Select-Object -First 1
        if (-not $Page) { return [ordered]@{ answers = $false; target_listed = $false } }
        $Probe = New-Object P3Cdp $Page.webSocketDebuggerUrl
        try { return [ordered]@{ answers = ((Cdp-Eval $Probe "1+1" 3000) -eq 2); target_listed = $true } } finally { $Probe.Dispose() }
    } catch { return [ordered]@{ answers = $false; target_listed = $null; error = $_.Exception.Message } }
}
# The renderer crash of the webview scenario, with every attempt recorded in $Checks.page_crash.
# Returns the method that took effect; throws when none did (evidence in the attempts).
# Lab finding (evidence/T-P3-H2/crash-lab): Page.crash is acted on, but the renderer then sits in its
# crash handling (minidump) and exits - so WebView2 reports render_exited - only 10-30 s later on a
# loaded or throttled machine; a re-send cannot speed that up. So: wait while the renderer is alive
# but no longer answers (crash in progress), re-send only to a page that still answers, and end the
# renderer only after $CrashBudgetSeconds (the lab saw up to 41 s from Page.crash to render_exited).
$CrashBudgetSeconds = 60
function Invoke-RendererCrash($Cdp, [int] $Port, [string] $Data, [string] $Log, $Checks) {
    $Crashed = { (Log-Count $Log ' webview_process_failed kind=render_exited remedy=reload$') -ge 1 }
    $Target = $script:CdpTarget
    $Renderers = @(Scoped-Renderers $Data)
    $Record = [ordered]@{ target = $Target; renderers_before = $Renderers; attempts = @(); method = $null
        budget_s = $CrashBudgetSeconds; renderer_gone_after_crash_ms = $null; render_exited_after_crash_ms = $null }
    $Checks.page_crash = $Record
    if ($Target.dev_rt_page_targets -ne 1) { throw "crash target not unique: $($Target.dev_rt_page_targets) dev-rt page targets" }
    $CrashAt = [DateTime]::UtcNow
    $Attempt = {
        param([string] $Method, [string] $Outcome, [double] $WaitSeconds)
        $Start = [DateTime]::UtcNow
        while ($true) {
            # When the renderer itself went away (vs when the shell logged it): where the time goes.
            if ($null -eq $Record.renderer_gone_after_crash_ms -and @($Renderers | Where-Object { Renderer-Alive $_ }).Count -eq 0) {
                $Record.renderer_gone_after_crash_ms = [int]([DateTime]::UtcNow - $CrashAt).TotalMilliseconds
            }
            if (& $Crashed) { break }
            if (([DateTime]::UtcNow - $Start).TotalSeconds -ge $WaitSeconds) { break }
            Start-Sleep -Milliseconds 100
        }
        $Seen = [bool](& $Crashed)
        # Fix 1 (SHOULD-8): the loop breaks as soon as the shell logged render_exited, before its
        # next look at the renderer; look once more so the timing is not lost (lab2: always null).
        if ($null -eq $Record.renderer_gone_after_crash_ms -and @($Renderers | Where-Object { Renderer-Alive $_ }).Count -eq 0) {
            $Record.renderer_gone_after_crash_ms = [int]([DateTime]::UtcNow - $CrashAt).TotalMilliseconds
            $Record.renderer_gone_measured_after_loop = $true
        }
        if ($Seen -and $null -eq $Record.render_exited_after_crash_ms) { $Record.render_exited_after_crash_ms = [int]([DateTime]::UtcNow - $CrashAt).TotalMilliseconds }
        $Entry = [ordered]@{ method = $Method; outcome = $Outcome; at = $Start.ToString("o"); render_exited_logged = $Seen
            waited_ms = [int]([DateTime]::UtcNow - $Start).TotalMilliseconds; renderers_alive = @($Renderers | Where-Object { Renderer-Alive $_ } | ForEach-Object { $_.pid }) }
        $Record.attempts = @($Record.attempts) + @($Entry)
        if ($Seen) { $Record.method = $Method }
        return $Seen
    }
    $Left = { [Math]::Max(0.0, $CrashBudgetSeconds - ([DateTime]::UtcNow - $CrashAt).TotalSeconds) }
    # 1. Page.crash on the open session; the outcome (event, close, refusal, nothing) is read back.
    try { [void]$Cdp.Call("Inspector.enable", "{}", 3000) } catch { }
    $Outcome = $Cdp.Crash(3000); $Cdp.Dispose()
    if (& $Attempt "page_crash" $Outcome 8) { return $Record.method }
    # 2. Not seen yet: look at the renderer before anything is sent again (within the budget).
    $Resent = $false; $Round = 0
    while ((& $Left) -gt 0) {
        $Round++
        $State = Page-Answers $Port $Target.id
        $Alive = @($Renderers | Where-Object { Renderer-Alive $_ })
        $Record["state_$Round"] = [ordered]@{ after_crash_ms = [int]([DateTime]::UtcNow - $CrashAt).TotalMilliseconds; page = $State; renderers_alive = @($Alive | ForEach-Object { $_.pid }) }
        if (& $Crashed) { [void](& $Attempt "seen_while_checking" "render_exited" 0); return $Record.method }
        if ($State.answers) {
            # Still the same live page: the command was lost or refused. Once more on a fresh session.
            if ($Resent) { break }
            $Resent = $true
            $Pages = [P3Web]::Http("http://127.0.0.1:$Port/json/list") | ConvertFrom-Json
            $Page = @($Pages | Where-Object { [string]$_.id -eq $Target.id }) | Select-Object -First 1
            $Fresh = New-Object P3Cdp $Page.webSocketDebuggerUrl
            try { [void]$Fresh.Call("Inspector.enable", "{}", 3000) } catch { }
            $Outcome = $Fresh.Crash(3000); $Fresh.Dispose()
            if (& $Attempt "page_crash_fresh_session" $Outcome ([Math]::Min(8, (& $Left)))) { return $Record.method }
        } elseif ($Alive.Count -gt 0) {
            # The renderer exists but no longer answers: its crash handling is still running.
            if (& $Attempt "wait_crash_in_progress" "renderer_alive_not_answering" ([Math]::Min(10, (& $Left)))) { return $Record.method }
        } else {
            # Renderer gone: give the shell's ProcessFailed a moment, else it is the shell's miss.
            if (& $Attempt "wait_after_renderer_exit" "renderer_gone" 5) { return $Record.method }
            throw "renderer exited but the shell logged no render_exited (not masked)"
        }
    }
    # 3. Last resort after the budget: end this scope's single renderer (WebView2 reports the same
    # render_exited for it). Fix 1: the webview scenario still fails at its end when this was needed.
    $Alive = @($Renderers | Where-Object { Renderer-Alive $_ })
    if ($Alive.Count -ne 1) { throw "Page.crash not acted on and the renderer is ambiguous ($($Alive.Count) alive)" }
    try { Stop-Process -Id $Alive[0].pid -Force -ErrorAction Stop; $Outcome = "terminated pid=$($Alive[0].pid)" } catch { $Outcome = "terminate_failed: " + $_.Exception.Message }
    if (& $Attempt "renderer_terminate" $Outcome 10) { return $Record.method }
    throw "render process failure not observed after every method"
}
# This scope's WebView2 (T-P3-H): msedgewebview2.exe whose --user-data-dir lies under
# <data>\WebView2[-admin] (single_instance::profile); without that switch, a command line naming <data>.
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
# The logged exit wait (T-P3-07, section 7.9): "exited ms=N", or the 12 s cap "timeout ms=N" with N read as a
# number - the wait loop may overrun the cap by a message dispatch under load (T-P3-07 recheck S2).
function Exit-WaitOk([string] $Line, [bool] $AllowExited, [bool] $AllowTimeout) {
    $M = [regex]::Match($Line, 'webview_browser_exit_wait result=(exited|timeout) ms=(\d+)$')
    if (-not $M.Success) { return $false }
    $Ms = [int64]$M.Groups[2].Value
    if ($M.Groups[1].Value -eq "exited") { return $AllowExited }
    return $AllowTimeout -and $Ms -ge 12000 -and $Ms -le 15000
}
# Native boxes: TDN_CREATED makes them topmost and a 200 ms TDN_TIMER re-applies it, so the check
# waits up to 3 s (T-P4-01 S8) and records the foreground-lock facts next to the result.
function Wait-Topmost([IntPtr] $Hwnd, $Checks) {
    $Start = [DateTime]::UtcNow
    while (-not [P3Web]::Topmost($Hwnd) -and ([DateTime]::UtcNow - $Start).TotalMilliseconds -lt 3000) { Start-Sleep -Milliseconds 50 }
    $Checks.dialog_topmost = [P3Web]::Topmost($Hwnd)
    $Checks.dialog_topmost_wait_ms = [int]([DateTime]::UtcNow - $Start).TotalMilliseconds
    $Checks.foreground_is_dialog = [P3Web]::GetForegroundWindow() -eq $Hwnd
    $Checks.foreground_lock_timeout_ms = [P3Web]::ForegroundLockTimeout()
}
$SmokeNames = @("TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR","TESSERACT_INSTANCE_SCOPE","TESSERACT_ISOLATED_AUDIT_MANIFEST","TESSERACT_SHELL_CDP_PORT",
    "TESSERACT_SHELL_DEV_READY_TIMEOUT_MS","TESSERACT_SHELL_DEV_WEBVIEW2_MISSING","TESSERACT_SHELL_DEV_RECREATE_FAIL","TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED","TESSERACT_REDUCED_MOTION","PYTHONPATH","PYTHONDONTWRITEBYTECODE","PYTHONPYCACHEPREFIX","TEMP","TMP")
function Set-SmokeEnvironment($Box, [hashtable] $Extra) {
    $env:TESSERACT_CONFIG_DIR = $Box.config; $env:TESSERACT_DATA_DIR = $Box.data
    $env:TESSERACT_INSTANCE_SCOPE = "p3-07-" + [Guid]::NewGuid().ToString("N")
    foreach ($Name in @("TESSERACT_ISOLATED_AUDIT_MANIFEST","TESSERACT_SHELL_CDP_PORT","TESSERACT_SHELL_DEV_READY_TIMEOUT_MS","TESSERACT_SHELL_DEV_WEBVIEW2_MISSING","TESSERACT_SHELL_DEV_RECREATE_FAIL","TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED","TESSERACT_REDUCED_MOTION")) {
        Remove-Item "Env:$Name" -ErrorAction SilentlyContinue
    }
    $env:PYTHONDONTWRITEBYTECODE = "1"; $env:PYTHONPYCACHEPREFIX = $PyCache; $env:TEMP = $Box.temp; $env:TMP = $Box.temp
    foreach ($Key in $Extra.Keys) { [Environment]::SetEnvironmentVariable($Key,[string]$Extra[$Key],"Process") }
}
# Residue check: this scope's msedgewebview2 processes get 20 s (decision P3-06 item 6; the browser
# may still be shutting down when the exit wait reached its 12 s cap).
$ResidueGraceSeconds = 20
# The 20 s count from the shell's exit, like process_watch (T-P3-H fix 1, S5).
function Wait-ScopedWebViewsGone([string] $Data, $Shell) {
    $From = [DateTime]::UtcNow
    if ($Shell) { try { $Shell.Refresh(); if ($Shell.HasExited) { $From = $Shell.ExitTime.ToUniversalTime() } } catch { } }
    $End = $From.AddSeconds($ResidueGraceSeconds)
    do { $Left = @(Scoped-WebViews $Data); if ($Left.Count -eq 0) { break }; Start-Sleep -Milliseconds 200 } while ([DateTime]::UtcNow -lt $End)
    return ,$Left
}
# Never kill runtime/network owners: leave through the tray Quit command (or the window) on failure.
function Stop-SmokeShell($ShellProcess, [int] $ShellPid) {
    if (-not $ShellProcess -or $ShellProcess.HasExited) { return }
    $Tray = [P3Web]::Find("TesseractShellTray", [NullString]::Value, [uint32]$ShellPid)
    if ($Tray -ne [IntPtr]::Zero) { [void][P3Web]::PostMessageW($Tray, 0x111, [IntPtr]2, [IntPtr]::Zero) } else { [void]$ShellProcess.CloseMainWindow() }
    [void]$ShellProcess.WaitForExit(30000)
}
if ($Scenarios -contains "main") {
$Sandbox = Join-Path ([IO.Path]::GetTempPath()) ("tesseract-p3-" + [Guid]::NewGuid().ToString("N"))
$Config = Join-Path $Sandbox "config"
$Data = Join-Path $Sandbox "data"
$Temp = Join-Path $Sandbox "temp"
foreach ($Dir in @($Config,$Data,$Temp)) { New-Item -ItemType Directory -Path $Dir -Force | Out-Null }
$Python = Join-Path $Repo ".venv/Scripts/python.exe"
# Only the example configuration is read; no live credentials or network capture.
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
$Listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
$Listener.Start(); $Port = $Listener.LocalEndpoint.Port; $Listener.Stop()
$Names = @("TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR","TESSERACT_INSTANCE_SCOPE","TESSERACT_ISOLATED_AUDIT_MANIFEST",
    "TESSERACT_SHELL_CDP_PORT","TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED","PYTHONPATH","PYTHONDONTWRITEBYTECODE","PYTHONPYCACHEPREFIX","TEMP","TMP")
$Previous = @{}
foreach ($Name in $Names) { $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
$ShellProcess = $null
$NodeProcess = $null
$Watcher = $null

try {
    $env:TESSERACT_CONFIG_DIR = $Config; $env:TESSERACT_DATA_DIR = $Data
    $env:TESSERACT_INSTANCE_SCOPE = "p3-shell-smoke"
    Remove-Item Env:TESSERACT_ISOLATED_AUDIT_MANIFEST -ErrorAction SilentlyContinue
    $env:TESSERACT_SHELL_CDP_PORT = "$Port"; $env:PYTHONDONTWRITEBYTECODE = "1"; $env:PYTHONPYCACHEPREFIX = $PyCache
    if ($ForceExitWaitTimeout) { $env:TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED = "1" } else { Remove-Item Env:TESSERACT_SHELL_DEV_EXIT_WAIT_IGNORE_EXITED -ErrorAction SilentlyContinue }
    $env:TEMP = $Temp; $env:TMP = $Temp
    $Arguments = @("--dev-rt")
    if ($Sidecar) { $Arguments += @("--sidecar",('"' + [IO.Path]::GetFullPath($Sidecar) + '"')) }
    $WatchArguments = @("-B",('"'+(Join-Path $Repo "shell/app/process_watch.py")+'"'),"--output",('"'+(Join-Path $Sandbox "process-inventory.json")+'"'),"--ready",('"'+(Join-Path $Sandbox "root.json")+'"'),"--",('"'+(Join-Path $Repo "shell/target/debug/tesseract-shell.exe")+'"')) + $Arguments
    $Watcher = Start-Process -FilePath $Python -ArgumentList $WatchArguments -WorkingDirectory $Repo -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Sandbox "stdout.log") -RedirectStandardError (Join-Path $Sandbox "stderr.log")
    $WatcherHandle=$Watcher.Handle
    $Deadline=[DateTime]::UtcNow.AddSeconds(15)
    while (-not (Test-Path -LiteralPath (Join-Path $Sandbox "root.json"))) {
        if ($Watcher.HasExited -or [DateTime]::UtcNow -gt $Deadline) {throw "suspended job launch failed: $Sandbox"}
        Start-Sleep -Milliseconds 100
    }
    $Root=Get-Content -LiteralPath (Join-Path $Sandbox "root.json") -Raw | ConvertFrom-Json
    $ShellProcess=Get-Process -Id $Root.pid
    $ProcessHandle=$ShellProcess.Handle
    $NodeProcess = Start-Process -FilePath (Get-Command node).Source -ArgumentList @(('"'+(Join-Path $Repo "shell/app/smoke_cdp.mjs")+'"'),$Port,('"'+(Join-Path $Sandbox "summary.json")+'"')) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Sandbox "cdp.log") -RedirectStandardError (Join-Path $Sandbox "cdp-errors.log")
    $NodeHandle=$NodeProcess.Handle
    $Deadline=[DateTime]::UtcNow.AddSeconds(60)
    do {
        if ([DateTime]::UtcNow -gt $Deadline) { throw "CDP timeout: $Sandbox" }
        Start-Sleep -Milliseconds 100
        $NodeProcess.Refresh()
    } while (-not $NodeProcess.HasExited)
    $NodeProcess.WaitForExit()
    if ($NodeProcess.ExitCode -ne 0) { throw "WebView CDP assertions failed: $Sandbox" }
    # 30 s like the other scenarios: after quit_exit_0 the shell may wait up to EXIT_WAIT (12 s) for
    # BrowserProcessExited before it exits (fix round 1, M1).
    if (-not $ShellProcess.WaitForExit(30000)) { throw "shell exit timeout: $Sandbox" }
    $ShellProcess.Refresh()
    if ($ShellProcess.ExitCode -ne 0) { throw "shell exit failed: $Sandbox" }
    $Summary = Get-Content -LiteralPath (Join-Path $Sandbox "summary.json") -Raw | ConvertFrom-Json
    $Placement = Get-Content -LiteralPath (Join-Path $Data "window-placement.json") -Raw | ConvertFrom-Json
    if ($Placement.scale -le 0 -or $Placement.width / $Placement.scale -lt 1060 -or $Placement.height / $Placement.scale -lt 700) { throw "invalid persisted window placement" }
    if (Get-Process -Id $Summary.runtime_pid -ErrorAction SilentlyContinue) { throw "sidecar still running" }
    $Remaining = Wait-ScopedWebViewsGone $Data $ShellProcess
    if ($Remaining.Count -gt 0) { $Class = Save-ResidueTree $Remaining $ShellProcess (Join-Path $Data "logs/shell.log") $Sandbox; throw "scoped WebView processes remain ($Class)" }
    if (-not $Watcher.WaitForExit($InventoryWaitMs)) {throw (Inventory-TimeoutMessage (Join-Path $Sandbox "process-inventory.json") ": $Sandbox")}
    $Watcher.Refresh()
    $Inventory=Get-Content -LiteralPath (Join-Path $Sandbox "process-inventory.json") -Raw | ConvertFrom-Json
    if ($Watcher.ExitCode -ne 0 -or -not $Inventory.passed -or $Inventory.active_processes -ne 0) {throw "owned process residual detected: $Sandbox"}
    if (-not @($Inventory.records | Where-Object {$_.pid -eq $Summary.runtime_pid -and $_.identity_available}).Count) {throw "runtime creation identity not observed"}
    # T-P3-07: AUMID, runtime check, the dev page never sends host_ready -> revealed at the 1.5 s
    # timeout (logged), and the exit waited for BrowserProcessExited.
    $MainLog = @(Get-Content -LiteralPath (Join-Path $Data "logs/shell.log"))
    # -ForceExitWaitTimeout: only the timeout branch may appear (the cap was reached and logged).
    $ExitWaitLine = ($MainLog | Where-Object { $_ -match ' webview_browser_exit_wait ' } | Select-Object -Last 1)
    if (-not (Exit-WaitOk ([string]$ExitWaitLine) (-not $ForceExitWaitTimeout) $true)) { throw "exit wait not bounded or not logged ($ExitWaitLine): $Sandbox" }
    foreach ($Pattern in @(' aumid_set ok=true$',' webview_runtime version=[0-9.]+$',' window_reveal_timeout waited_ms=\d+ ok=true$')) {
        if (-not @($MainLog | Where-Object { $_ -match $Pattern }).Count) { throw "shell.log lacks $Pattern : $Sandbox" }
    }
    $Waited = [int](([regex]::Match(($MainLog | Where-Object { $_ -match ' window_reveal_timeout ' } | Select-Object -First 1), 'waited_ms=(\d+)')).Groups[1].Value)
    if ($Waited -lt 1400 -or $Waited -gt 4000) { throw "reveal timeout out of range: $Waited ms" }
    if (@($MainLog | Where-Object { $_ -match ' window_revealed ' }).Count) { throw "main scenario revealed by host_ready although the page never sent it" }
    $EvidenceCopy = @{ scenario = "main"; passed = $true; sandbox = $Sandbox; reveal_timeout_waited_ms = $Waited; force_exit_wait_timeout = [bool]$ForceExitWaitTimeout
        exit_wait = (($MainLog | Where-Object { $_ -match ' webview_browser_exit_wait ' } | Select-Object -Last 1) -replace '^\d+ ','') }
    [IO.File]::WriteAllText((Join-Path $Sandbox "result.json"), ($EvidenceCopy | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding $false))
    Write-Output "P3 smoke passed; shell, sidecar and scoped WebView processes exited. Evidence: $Sandbox"
} catch {
    # T-P3-H2 fix 1: a failed main scenario leaves a result.json too (the error, where it was raised).
    $MainFailure = [ordered]@{ scenario = "main"; passed = $false; sandbox = $Sandbox; error = $_.Exception.Message
        error_where = $_.InvocationInfo.PositionMessage }
    [IO.File]::WriteAllText((Join-Path $Sandbox "result.json"), ($MainFailure | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding $false))
    throw
} finally {
    # On automation failure request the actual window close; never kill runtime/network owners.
    if ($ShellProcess -and -not $ShellProcess.HasExited) { [void] $ShellProcess.CloseMainWindow(); [void] $ShellProcess.WaitForExit(30000) }
    # T-P3-H2 fix 3: a launch that gave up never learned its shell; end one the watcher started late.
    if (-not $ShellProcess) { $Late = Resolve-LateShell (Join-Path $Sandbox "root.json") $Watcher; if ($Late) { [void](Close-LateShell $Late $Sandbox) } }
    foreach ($Name in $Names) { [Environment]::SetEnvironmentVariable($Name,$Previous[$Name],"Process") }
    # T-P3-H2 fix 1: copied on failure as well (shell.log, process-inventory.json, residue-tree.json).
    if ($EvidenceDir) { Save-SmokeEvidence $Sandbox "main" }
}
}

# --- T-P3-07 scenario "webview" -------------------------------------------------------------
# Cloak until host_ready (asserted with DWMWA_CLOAKED), V2 measurements, renderer crash -> reload
# and cache replay, browser-process kill -> recreated window, exit waits for BrowserProcessExited.
# The debug-only ready timeout is stretched to 30 s so the cloaked phase can be observed.
if ($Scenarios -contains "webview") {
    $Box = New-SmokeSandbox "webview"; $Port = Free-Port; $Log = Join-Path $Box.data "logs/shell.log"
    $Checks = [ordered]@{}; $Passed = $false; $Failure = $null; $ShellProcess = $null; $ShellPid = 0; $Watcher = $null; $Cdp = $null
    $Saved = @{}; foreach ($Name in $SmokeNames) { $Saved[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
    try {
        Set-SmokeEnvironment $Box @{ TESSERACT_SHELL_CDP_PORT = $Port; TESSERACT_SHELL_DEV_READY_TIMEOUT_MS = 30000 }
        $Watcher = Start-WatchedShell $Box @("--dev-rt")
        $ShellPid = [int]((Get-Content -LiteralPath (Join-Path $Box.box "root.json") -Raw | ConvertFrom-Json).pid)
        $ShellProcess = Get-Process -Id $ShellPid; $null = $ShellProcess.Handle
        Wait-Until { (Log-Count $Log ' shell_window_ready$') -ge 1 } "shell window" 60
        $Hwnd = [P3Web]::Find([NullString]::Value, "Tesseract Shell (P3)", [uint32]$ShellPid)
        if ($Hwnd -eq [IntPtr]::Zero) { throw "main window not found" }

        # 1. Cloaked: Win32-visible (WebView2 keeps rendering) but DWM does not show it.
        $Checks.start_is_window_visible = [P3Web]::IsWindowVisible($Hwnd)
        $Checks.start_dwm_cloaked = [P3Web]::Cloaked($Hwnd)
        if (-not $Checks.start_is_window_visible -or $Checks.start_dwm_cloaked -le 0) { throw "window not cloaked at start: visible=$($Checks.start_is_window_visible) cloaked=$($Checks.start_dwm_cloaked)" }
        $Cdp = Cdp-Connect $Port
        $Checks.v2_cloaked = Cdp-Eval $Cdp $RafProbe
        # The page builds its DOM from real state while cloaked (here: the dev page's replay).
        $First = Subscribe-Replay $Cdp
        $Checks.epoch = $First.epoch; $Checks.runtime_pid = $First.runtime_pid
        # T-P4-C3: the replay opens with rt:reset, host:caps, hello; rt:replayed ends it (or the
        # new epoch's seed, when the page connected before the seed was complete).
        $ReplayProbe = "(()=>{const t=window.devFrames.map(f=>f.t);const c=window.devFrames.find(f=>f.t==='host:caps')||{invokes:[],events:[]};return {order:t.slice(0,3),replayed:t.indexOf('rt:replayed'),hello:t.indexOf('hello'),resync:c.invokes.includes('rt_resync'),marker:c.events.includes('rt:replayed')}})()"
        Wait-Until { (Cdp-Eval $Cdp "window.devFrames.some(f=>f.t==='rt:replayed')") -eq $true } "rt:replayed" 30
        $Checks.c3_replay = Cdp-Eval $Cdp $ReplayProbe
        if ((@($Checks.c3_replay.order) -join ",") -ne "rt:reset,host:caps,hello" -or $Checks.c3_replay.replayed -le $Checks.c3_replay.hello -or -not $Checks.c3_replay.resync -or -not $Checks.c3_replay.marker) { throw "replay frames: $($Checks.c3_replay | ConvertTo-Json -Compress)" }
        Wait-Until { (Log-Count $Log ' rt_seed_done ') -ge 1 } "seed end" 30
        $Checks.c3_seed_line = ((Get-Content -LiteralPath $Log) | Where-Object { $_ -match ' rt_seed_done ' } | Select-Object -First 1) -replace '^\d+ ',''
        $Checks.cloaked_before_ready = [P3Web]::Cloaked($Hwnd)
        if ($Checks.cloaked_before_ready -le 0 -or (Log-Count $Log ' window_reveal') -ne 0) { throw "revealed before host_ready" }
        [void](Cdp-Eval $Cdp "window.__TAURI__.core.invoke('host_ready').then(()=>true)")
        Wait-Until { [P3Web]::Cloaked($Hwnd) -eq 0 } "uncloaked after host_ready" 5
        Wait-Until { (Log-Count $Log ' window_revealed reason=ready waited_ms=\d+ ok=true$') -eq 1 } "window_revealed log"
        $Checks.after_ready_dwm_cloaked = [P3Web]::Cloaked($Hwnd)
        $Checks.v2_visible = Cdp-Eval $Cdp $RafProbe

        # 2. V2 plain hide: SetWindowPos(SWP_HIDEWINDOW) bypasses WM_SHOWWINDOW, so the controller
        # stays IsVisible=true; then ShowWindow(SW_HIDE) (what Tauri hide() calls), where the
        # T-P3-03 subclass sets IsVisible=false. Measured, not asserted (report V2).
        $Hidden = Log-Count $Log ' window_hidden$'
        [void][P3Web]::SetWindowPos($Hwnd, [IntPtr]::Zero, 0, 0, 0, 0, 0x80 -bor 0x1 -bor 0x2 -bor 0x4 -bor 0x10)
        Wait-Until { -not [P3Web]::IsWindowVisible($Hwnd) } "SetWindowPos hide"
        $Checks.v2_setwindowpos_hidden = Cdp-Eval $Cdp $RafProbe
        $Checks.v2_setwindowpos_sent_wm_showwindow = (Log-Count $Log ' window_hidden$') -gt $Hidden
        [void][P3Web]::SetWindowPos($Hwnd, [IntPtr]::Zero, 0, 0, 0, 0, 0x40 -bor 0x1 -bor 0x2 -bor 0x4 -bor 0x10)
        Wait-Until { [P3Web]::IsWindowVisible($Hwnd) } "SetWindowPos show"
        [void][P3Web]::ShowWindow($Hwnd, 0)
        Wait-Until { -not [P3Web]::IsWindowVisible($Hwnd) } "ShowWindow hide"
        $Checks.v2_showwindow_hidden = Cdp-Eval $Cdp $RafProbe
        [void][P3Web]::ShowWindow($Hwnd, 5)
        Wait-Until { [P3Web]::IsWindowVisible($Hwnd) -and (Cdp-Eval $Cdp "document.visibilityState") -eq "visible" } "ShowWindow show"
        if ($Checks.v2_cloaked.frames -le 0) { throw "no animation frames while cloaked" }

        # 3. Renderer crash (debug-only CDP Page.crash): reload, the fresh page replays the shell
        # cache of the same epoch; the sidecar is untouched.
        # T-P3-H2 (31b-17): confirmed target, outcome read back, renderer checked before a re-send,
        # renderer termination as the last resort (Invoke-RendererCrash; attempts in checks.page_crash).
        $CrashCdp = $Cdp; $Cdp = $null
        $Checks.page_crash_method = Invoke-RendererCrash $CrashCdp $Port $Box.data $Log $Checks
        # Fix 1 (SHOULD-1): "resent" only when a second crash action really went out; the
        # renderer_terminate fallback keeps the reload path under test but the scenario still fails
        # at the end (Page.crash did not take effect within the budget: not green).
        $Checks.page_crash_resent = $Checks.page_crash_method -in @("page_crash_fresh_session","renderer_terminate")
        $Checks.page_crash_fallback = $Checks.page_crash_method -eq "renderer_terminate"
        Wait-Until { (Log-Count $Log ' webview_reload kind=render_exited ok=true$') -ge 1 } "reload" 10
        $Cdp = Cdp-Connect $Port
        $Checks.reload_fresh_page = (Cdp-Eval $Cdp "window.devFrames.length") -eq 0
        $Reloaded = Subscribe-Replay $Cdp
        $Checks.reload_replay_topics = $Reloaded.topics
        if (-not $Checks.reload_fresh_page -or $Reloaded.epoch -ne $First.epoch -or $Reloaded.runtime_pid -ne $First.runtime_pid) { throw "reload did not restore the same session" }
        if (-not (Get-Process -Id $First.runtime_pid -ErrorAction SilentlyContinue)) { throw "sidecar gone after renderer crash" }
        $Checks.reload_same_epoch = $true

        # 4. Browser-process loss: kill this scope's msedgewebview2 browser process; the shell
        # destroys the dead window, builds a new one (cloaked again until host_ready) and the new
        # page replays the same epoch.
        $Browser = @(Scoped-WebViews $Box.data | Where-Object { $_.CommandLine -notmatch '--type=' })
        if ($Browser.Count -ne 1) { throw "expected one scoped browser process, found $($Browser.Count)" }
        $Checks.killed_browser_pid = [int]$Browser[0].ProcessId
        $Cdp.Dispose(); $Cdp = $null
        Stop-Process -Id $Checks.killed_browser_pid -Force
        Wait-Until { (Log-Count $Log ' webview_process_failed kind=browser_exited remedy=recreate$') -ge 1 } "browser process failure" 15
        Wait-Until { (Log-Count $Log ' webview_recreated attempt=\d+$') -ge 1 } "window recreated" 20
        $Checks.recreated_line = ((Get-Content -LiteralPath $Log) | Where-Object { $_ -match ' webview_recreated ' } | Select-Object -Last 1) -replace '^\d+ ',''
        $NewHwnd = [P3Web]::Find([NullString]::Value, "Tesseract Shell (P3)", [uint32]$ShellPid)
        if ($NewHwnd -eq [IntPtr]::Zero -or $NewHwnd -eq $Hwnd -or [P3Web]::IsWindow($Hwnd)) { throw "main window not replaced" }
        $Checks.recreated_dwm_cloaked = [P3Web]::Cloaked($NewHwnd)
        if ($Checks.recreated_dwm_cloaked -le 0) { throw "recreated window not cloaked before host_ready" }
        $Cdp = Cdp-Connect $Port
        $Recreated = Subscribe-Replay $Cdp
        if ($Recreated.epoch -ne $First.epoch -or $Recreated.runtime_pid -ne $First.runtime_pid) { throw "recreated page did not replay the same session" }
        [void](Cdp-Eval $Cdp "window.__TAURI__.core.invoke('host_ready').then(()=>true)")
        Wait-Until { [P3Web]::Cloaked($NewHwnd) -eq 0 -and [P3Web]::IsWindowVisible($NewHwnd) } "recreated window revealed" 5
        $Checks.recreated_same_epoch_revealed = $true

        # 5. Quit through the dev page: exit 0 after waiting for BrowserProcessExited, no residue.
        try { [void](Cdp-Eval $Cdp "window.__TAURI__.core.invoke('dev_quit').then(()=>true)" 3000) } catch { }
        $Cdp.Dispose(); $Cdp = $null
        if (-not $ShellProcess.WaitForExit(30000)) { throw "shell did not exit" }
        $ShellProcess.Refresh(); $Checks.shell_exit_code = $ShellProcess.ExitCode
        if ($Checks.shell_exit_code -ne 0) { throw "shell exit code $($Checks.shell_exit_code)" }
        $Checks.exit_wait_line = ((Get-Content -LiteralPath $Log) | Where-Object { $_ -match ' webview_browser_exit_wait ' } | Select-Object -Last 1) -replace '^\d+ ',''
        # Bounded wait (§7.9): BrowserProcessExited seen, or the 12 s cap reached (the browser's own
        # shutdown time varies from 0.2 s to over 20 s on the development machine; report T-P3-07).
        if (-not (Exit-WaitOk ([string]$Checks.exit_wait_line) $true $true)) { throw "exit wait not bounded or not logged: $($Checks.exit_wait_line)" }
        if (Get-Process -Id $First.runtime_pid -ErrorAction SilentlyContinue) { throw "sidecar still running" }
        $Remaining = Wait-ScopedWebViewsGone $Box.data $ShellProcess
        $Checks.scoped_webviews_after_exit = $Remaining.Count
        if ($Remaining.Count -gt 0) { $Checks.residue_class = Save-ResidueTree $Remaining $ShellProcess $Log $Box.box; throw "scoped WebView processes remain ($($Checks.residue_class))" }
        if (-not $Watcher.WaitForExit($InventoryWaitMs)) { throw (Inventory-TimeoutMessage (Join-Path $Box.box "process-inventory.json")) }
        $Inventory = Get-Content -LiteralPath (Join-Path $Box.box "process-inventory.json") -Raw | ConvertFrom-Json
        if (-not $Inventory.passed -or $Inventory.active_processes -ne 0) { throw "owned process residual detected" }
        $Checks.inventory_total_processes = $Inventory.total_processes
        if ($Checks.page_crash_fallback) { throw "Page.crash not acted on within $($CrashBudgetSeconds) s: only the renderer_terminate fallback crashed the renderer (rest of the scenario passed; not green)" }
        $Passed = $true
    } catch {
        $Failure = $_.Exception.Message
    } finally {
        if ($Cdp) { $Cdp.Dispose() }
        if (-not $ShellProcess) { $Late = Resolve-LateShell (Join-Path $Box.box "root.json") $Watcher; if ($Late) { $Checks.late_shell = Close-LateShell $Late $Box.box } }
        Stop-SmokeShell $ShellProcess $ShellPid
        if ($Watcher -and -not $Watcher.HasExited) { [void]$Watcher.WaitForExit($InventoryWaitMs) }
        foreach ($Name in $SmokeNames) { [Environment]::SetEnvironmentVariable($Name,$Saved[$Name],"Process") }
        $Result = [ordered]@{ scenario = "webview"; passed = $Passed; error = $Failure; sandbox = $Box.box; checks = $Checks }
        [IO.File]::WriteAllText((Join-Path $Box.box "result.json"), ($Result | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding $false))
        if ($EvidenceDir) { Save-SmokeEvidence $Box.box "webview" }
    }
    if (-not $Passed) { throw "T-P3-07 webview scenario failed: $Failure (evidence: $($Box.box))" }
    Write-Output "T-P3-07 webview scenario passed; evidence: $($Box.box)"
}

# --- T-P3-07 scenario "missing" -------------------------------------------------------------
# Debug-only simulation of a machine without the WebView2 runtime: the native box appears (visible,
# topmost), no window and no sidecar are created, closing the box exits 1 without opening a browser.
if ($Scenarios -contains "missing") {
    $Box = New-SmokeSandbox "missing"; $Log = Join-Path $Box.data "logs/shell.log"
    $Checks = [ordered]@{}; $Passed = $false; $Failure = $null; $ShellProcess = $null; $ShellPid = 0; $Watcher = $null
    $Saved = @{}; foreach ($Name in $SmokeNames) { $Saved[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
    try {
        Set-SmokeEnvironment $Box @{ TESSERACT_SHELL_DEV_WEBVIEW2_MISSING = 1 }
        $Watcher = Start-WatchedShell $Box @()
        $ShellPid = [int]((Get-Content -LiteralPath (Join-Path $Box.box "root.json") -Raw | ConvertFrom-Json).pid)
        $ShellProcess = Get-Process -Id $ShellPid; $null = $ShellProcess.Handle
        Wait-Until { (Log-Count $Log ' webview_runtime_missing hresult=0x80070002 simulated=true$') -eq 1 } "missing runtime logged" 30
        Wait-Until { $script:Dialog = [P3Web]::Find("#32770", [NullString]::Value, [uint32]$ShellPid); $script:Dialog -ne [IntPtr]::Zero -and [P3Web]::IsWindowVisible($script:Dialog) } "missing-runtime dialog" 15
        $Checks.dialog_visible = $true
        Wait-Topmost $script:Dialog $Checks
        $Checks.main_window_absent = [P3Web]::Find([NullString]::Value, "Tesseract Shell (P3)", [uint32]$ShellPid) -eq [IntPtr]::Zero
        # The debug shell is a console-subsystem exe: Windows may give it a conhost.exe, which is not ours.
        $Checks.children_while_dialog = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ShellPid" | Where-Object { $_.Name -ne "conhost.exe" }).Count
        if (-not $Checks.dialog_topmost -or -not $Checks.main_window_absent -or $Checks.children_while_dialog -ne 0) { throw "dialog state wrong: $($Checks | ConvertTo-Json -Compress)" }
        [void][P3Web]::PostMessageW($script:Dialog, 0x10, [IntPtr]::Zero, [IntPtr]::Zero)
        if (-not $ShellProcess.WaitForExit(15000)) { throw "shell did not exit after the dialog" }
        $ShellProcess.Refresh(); $Checks.shell_exit_code = $ShellProcess.ExitCode
        if ($Checks.shell_exit_code -ne 1) { throw "exit code $($Checks.shell_exit_code), expected 1" }
        if ((Log-Count $Log ' shell_exit_1$') -ne 1 -or (Log-Count $Log ' webview_runtime_help_') -ne 0 -or (Log-Count $Log ' sidecar_') -ne 0) { throw "unexpected shell.log lines" }
        if (-not $Watcher.WaitForExit($InventoryWaitMs)) { throw (Inventory-TimeoutMessage (Join-Path $Box.box "process-inventory.json")) }
        $Inventory = Get-Content -LiteralPath (Join-Path $Box.box "process-inventory.json") -Raw | ConvertFrom-Json
        $Checks.inventory_total_processes = $Inventory.total_processes; $Checks.inventory_root_exit_code = $Inventory.root_exit_code
        $Checks.inventory_non_console_processes = @($Inventory.records | Where-Object { $_.image -notmatch '\\conhost\.exe$' }).Count
        if ($Inventory.active_processes -ne 0 -or $Inventory.root_exit_code -ne 1 -or $Checks.inventory_non_console_processes -ne 1) { throw "processes other than the shell were started" }
        $Passed = $true
    } catch {
        $Failure = $_.Exception.Message
    } finally {
        if (-not $ShellProcess) { $Late = Resolve-LateShell (Join-Path $Box.box "root.json") $Watcher; if ($Late) { $Checks.late_shell = Close-LateShell $Late $Box.box } }
        if ($ShellProcess -and -not $ShellProcess.HasExited) {
            $Left = [P3Web]::Find("#32770", [NullString]::Value, [uint32]$ShellPid)
            if ($Left -ne [IntPtr]::Zero) { [void][P3Web]::PostMessageW($Left, 0x10, [IntPtr]::Zero, [IntPtr]::Zero) }
            [void]$ShellProcess.WaitForExit(15000)
        }
        if ($Watcher -and -not $Watcher.HasExited) { [void]$Watcher.WaitForExit($InventoryWaitMs) }
        foreach ($Name in $SmokeNames) { [Environment]::SetEnvironmentVariable($Name,$Saved[$Name],"Process") }
        $Result = [ordered]@{ scenario = "missing"; passed = $Passed; error = $Failure; sandbox = $Box.box; checks = $Checks }
        [IO.File]::WriteAllText((Join-Path $Box.box "result.json"), ($Result | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding $false))
        if ($EvidenceDir) { Save-SmokeEvidence $Box.box "missing" }
    }
    if (-not $Passed) { throw "T-P3-07 missing scenario failed: $Failure (evidence: $($Box.box))" }
    Write-Output "T-P3-07 missing scenario passed; evidence: $($Box.box)"
}

# --- T-P3-07 scenario "lost" ----------------------------------------------------------------
# The browser process dies and no window can be built again (debug-only TESSERACT_SHELL_DEV_RECREATE_FAIL
# makes every recreation attempt fail): first attempt at once plus 3 retries after 0.5/1/2 s, then the
# one-button native box; closing it runs the normal quit (app.quit -> bye -> sidecar exit 0), exit 0.
# "lostbuilt" (fix round 1, B-S2): TESSERACT_SHELL_DEV_RECREATE_FAIL=after_build lets every attempt
# build the real window and fail only afterwards, so the destroy and the wait for Tauri to free the
# "main" label run before each retry; all four attempts must have built a window again.
foreach ($LostMode in @(@("lost","lostbuilt") | Where-Object { $Scenarios -contains $_ })) {
    $Built = $LostMode -eq "lostbuilt"
    $Box = New-SmokeSandbox $LostMode; $Log = Join-Path $Box.data "logs/shell.log"
    $Checks = [ordered]@{}; $Passed = $false; $Failure = $null; $ShellProcess = $null; $ShellPid = 0; $Watcher = $null
    $Saved = @{}; foreach ($Name in $SmokeNames) { $Saved[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
    try {
        Set-SmokeEnvironment $Box @{ TESSERACT_SHELL_DEV_RECREATE_FAIL = $(if ($Built) { "after_build" } else { "1" }) }
        $Watcher = Start-WatchedShell $Box @()
        $ShellPid = [int]((Get-Content -LiteralPath (Join-Path $Box.box "root.json") -Raw | ConvertFrom-Json).pid)
        $ShellProcess = Get-Process -Id $ShellPid; $null = $ShellProcess.Handle
        Wait-Until { (Log-Count $Log ' shell_window_ready$') -ge 1 -and (Log-Count $Log ' window_reveal_timeout ') -ge 1 } "shell window" 60
        $RuntimePid = [int]([regex]::Match(((Get-Content -LiteralPath $Log) | Where-Object { $_ -match ' sidecar_ready pid=\d+$' } | Select-Object -First 1), 'pid=(\d+)').Groups[1].Value)
        if (-not $RuntimePid -or -not (Get-Process -Id $RuntimePid -ErrorAction SilentlyContinue)) { throw "sidecar not running" }
        $Checks.runtime_pid = $RuntimePid
        $Browser = @(Scoped-WebViews $Box.data | Where-Object { $_.CommandLine -notmatch '--type=' })
        if ($Browser.Count -ne 1) { throw "expected one scoped browser process, found $($Browser.Count)" }
        $Checks.killed_browser_pid = [int]$Browser[0].ProcessId
        Stop-Process -Id $Checks.killed_browser_pid -Force
        Wait-Until { (Log-Count $Log ' webview_process_failed kind=browser_exited remedy=recreate$') -ge 1 } "browser process failure" 15
        Wait-Until { (Log-Count $Log ' webview_recreate_exhausted$') -eq 1 } "recreation exhausted" $(if ($Built) { 60 } else { 20 })
        # Attempt times: the first at once, then 0.5 s, 1 s and 2 s apart.
        $Attempts = @((Get-Content -LiteralPath $Log) | Where-Object { $_ -match ' webview_create_failed phase=recreate attempt=\d$' } | ForEach-Object { [int64](($_ -split ' ')[0]) })
        $Checks.recreate_attempts = $Attempts.Count
        if ($Attempts.Count -ne 4) { throw "expected 4 recreation attempts, saw $($Attempts.Count)" }
        $Checks.recreate_gaps_ms = @(1..3 | ForEach-Object { $Attempts[$_] - $Attempts[$_ - 1] })
        # lostbuilt: each gap also holds the next window's build, the destroy and the label wait.
        $Slack = if ($Built) { 12000 } else { 1500 }
        foreach ($Pair in @(@(0,500),@(1,1000),@(2,2000))) {
            $Gap = $Checks.recreate_gaps_ms[$Pair[0]]
            if ($Gap -lt $Pair[1] - 20 -or $Gap -gt $Pair[1] + $Slack) { throw "backoff gap $($Pair[0]) = $Gap ms, expected about $($Pair[1])" }
        }
        if ($Built) {
            # Every attempt (not only the first) built its window: the label was free each time.
            $Checks.attempts_built = Log-Count $Log ' dev_recreate_fail_after_build attempt=\d$'
            $Checks.label_still_registered = Log-Count $Log ' webview_label_still_registered$'
            if ($Checks.attempts_built -ne 4 -or $Checks.label_still_registered -ne 0) { throw "post-build retries: built=$($Checks.attempts_built) label_stuck=$($Checks.label_still_registered)" }
        }
        # A-S8: the session-end hook moved to the tray window before the box.
        $Checks.session_end_hook_on_tray = (Log-Count $Log ' session_end_hook_on_tray$') -eq 1
        if (-not $Checks.session_end_hook_on_tray) { throw "session-end hook not moved to the tray window" }
        Wait-Until { $script:Dialog = [P3Web]::Find("#32770", [NullString]::Value, [uint32]$ShellPid); $script:Dialog -ne [IntPtr]::Zero -and [P3Web]::IsWindowVisible($script:Dialog) } "webview-lost dialog" 15
        Wait-Topmost $script:Dialog $Checks
        $Checks.main_window_absent = [P3Web]::Find([NullString]::Value, "Tesseract Shell (P3)", [uint32]$ShellPid) -eq [IntPtr]::Zero
        $Checks.sidecar_alive_while_dialog = [bool](Get-Process -Id $RuntimePid -ErrorAction SilentlyContinue)
        $Checks.quit_before_dialog_closed = (Log-Count $Log ' quit_requested_') -ne 0
        # With no window left, Tauri asked to end the loop and the shell refused (recovering).
        $Checks.exit_prevented_logged = (Log-Count $Log ' exit_prevented_recreating$') -ge 1
        if (-not $Checks.dialog_topmost -or -not $Checks.main_window_absent -or -not $Checks.sidecar_alive_while_dialog -or $Checks.quit_before_dialog_closed -or -not $Checks.exit_prevented_logged) { throw "dialog state wrong: $($Checks | ConvertTo-Json -Compress)" }
        [void][P3Web]::PostMessageW($script:Dialog, 0x10, [IntPtr]::Zero, [IntPtr]::Zero)
        if (-not $ShellProcess.WaitForExit(40000)) { throw "shell did not exit after the dialog" }
        $ShellProcess.Refresh(); $Checks.shell_exit_code = $ShellProcess.ExitCode
        if ($Checks.shell_exit_code -ne 0) { throw "shell exit code $($Checks.shell_exit_code)" }
        $Lines = @(Get-Content -LiteralPath $Log)
        foreach ($Pattern in @(' quit_requested_webview_lost$',' quit_confirm_not_needed$',' quit_sent reason=user$',' bye exit_code=0 ',' quit_exit_0$',' shell_exit_0$')) {
            if (-not @($Lines | Where-Object { $_ -match $Pattern }).Count) { throw "shell.log lacks $Pattern" }
        }
        $Checks.exit_wait_line = (($Lines | Where-Object { $_ -match ' webview_browser_exit_wait ' } | Select-Object -Last 1) -replace '^\d+ ','')
        # lostbuilt: the last built window's browser may still be shutting down (bounded, logged).
        if (-not (Exit-WaitOk ([string]$Checks.exit_wait_line) $true ([bool]$Built))) { throw "exit wait: $($Checks.exit_wait_line)" }
        if (Get-Process -Id $RuntimePid -ErrorAction SilentlyContinue) { throw "sidecar still running" }
        $Remaining = Wait-ScopedWebViewsGone $Box.data $ShellProcess
        $Checks.scoped_webviews_after_exit = $Remaining.Count
        if ($Remaining.Count -gt 0) { $Checks.residue_class = Save-ResidueTree $Remaining $ShellProcess $Log $Box.box; throw "scoped WebView processes remain ($($Checks.residue_class))" }
        if (-not $Watcher.WaitForExit($InventoryWaitMs)) { throw (Inventory-TimeoutMessage (Join-Path $Box.box "process-inventory.json")) }
        $Inventory = Get-Content -LiteralPath (Join-Path $Box.box "process-inventory.json") -Raw | ConvertFrom-Json
        if (-not $Inventory.passed -or $Inventory.active_processes -ne 0) { throw "owned process residual detected" }
        $Checks.inventory_total_processes = $Inventory.total_processes
        $Passed = $true
    } catch {
        $Failure = $_.Exception.Message
    } finally {
        if (-not $ShellProcess) { $Late = Resolve-LateShell (Join-Path $Box.box "root.json") $Watcher; if ($Late) { $Checks.late_shell = Close-LateShell $Late $Box.box } }
        if ($ShellProcess -and -not $ShellProcess.HasExited) {
            $Left = [P3Web]::Find("#32770", [NullString]::Value, [uint32]$ShellPid)
            if ($Left -ne [IntPtr]::Zero) { [void][P3Web]::PostMessageW($Left, 0x10, [IntPtr]::Zero, [IntPtr]::Zero) }
            [void]$ShellProcess.WaitForExit(30000)
        }
        Stop-SmokeShell $ShellProcess $ShellPid
        if ($Watcher -and -not $Watcher.HasExited) { [void]$Watcher.WaitForExit($InventoryWaitMs) }
        foreach ($Name in $SmokeNames) { [Environment]::SetEnvironmentVariable($Name,$Saved[$Name],"Process") }
        $Result = [ordered]@{ scenario = $LostMode; passed = $Passed; error = $Failure; sandbox = $Box.box; checks = $Checks }
        [IO.File]::WriteAllText((Join-Path $Box.box "result.json"), ($Result | ConvertTo-Json -Depth 6), (New-Object Text.UTF8Encoding $false))
        if ($EvidenceDir) { Save-SmokeEvidence $Box.box $LostMode }
    }
    if (-not $Passed) { throw "T-P3-07 $LostMode scenario failed: $Failure (evidence: $($Box.box))" }
    Write-Output "T-P3-07 $LostMode scenario passed; evidence: $($Box.box)"
}

# --- T-P4-C3 scenario "c3" ------------------------------------------------------------------
# The built app page (index.html, T.rt) on the scripted stand-in sidecar shell/app/src/notify_fixture.py
# (debug `--dev-sidecar-script`; it sends each JSON file dropped into <data>/notify-fixture/push):
#  - host:caps reaches T.rt (hostHas('rt_resync')); the new epoch's seed ends with rt:replayed
#    (shell.log rt_seed_done);
#  - 02b section 9 item 5 / ruling 4th wave 02b-3: the first live tier.transition of the session,
#    and again the first one after a page reload, is projected as live (instant=false, rt:tier
#    fallback / recover), not swallowed as replay;
#  - rt_resync: cache answer, rate_limited within 1 s, not_allowed for jobs;
#  - TESSERACT_REDUCED_MOTION=1 -> window.__TESSERACT_MOTION__ frozen {reduced:true} (shell.log);
#  - D20: the tray icon follows pushed service.state / killswitch.state (shell.log tray_icon_state):
#    stopped, starting, running, running-ks, failed-ks, failed;
#  - tray Quit exits 0 with no owned process left.
if ($Scenarios -contains "c3") {
    $Box = New-SmokeSandbox "c3"; $Port = Free-Port; $Log = Join-Path $Box.data "logs/shell.log"
    $FixtureState = Join-Path $Box.data "notify-fixture"
    $Checks = [ordered]@{}; $Passed = $false; $Failure = $null; $ShellProcess = $null; $ShellPid = 0; $Watcher = $null; $Cdp = $null
    $Saved = @{}; foreach ($Name in $SmokeNames) { $Saved[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
    $script:PushNo = 0
    function Push-Fixture($Spec) {
        $script:PushNo++; $Name = "{0:d3}" -f $script:PushNo
        $Dir = Join-Path $FixtureState "push"; New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        [IO.File]::WriteAllText((Join-Path $Dir "$Name.tmp"), ($Spec | ConvertTo-Json -Depth 8 -Compress), (New-Object Text.UTF8Encoding $false))
        Move-Item -LiteralPath (Join-Path $Dir "$Name.tmp") -Destination (Join-Path $Dir "$Name.json")
        Wait-Until { Test-Path -LiteralPath (Join-Path $Dir "$Name.sent") } "fixture sent $Name" 10
    }
    function Service-State([string] $Phase) {
        $Live = @("collecting","core_standby","running") -contains $Phase
        return @{ topic = "service.state"; kind = "snapshot"; data = @{
            intent = @{ desired_running = $Live; stop_requested = $false; lifecycle_revision = 1; runtime_generation = 1 }
            runtime = @{ runtime_running = ($Phase -eq "running"); startup_phase = $Phase; shutdown_cleanup_pending = $false; failure_code = $(if ($Phase -eq "failed") { "core_start_failed" } else { $null }) }
            core = @{ running = ($Phase -eq "running"); pid = $null; process_generation = 1; core_generation = 1; cleanup_pending = $false }
            readiness = @{ state = "ready"; validation_active = $false; reason = ""; remaining_ms = $null; publication_revision = 1; lifecycle_revision = 1; route_epoch = 1; network_context_epoch = 1 }
            freshness = @{ sampled_at_ms = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()); snapshot_revision = 1; stale = $false; uptime_s = $null } } }
    }
    function Kill-Switch([bool] $Enabled) {
        return @{ topic = "killswitch.state"; kind = "snapshot"; data = @{ enabled = $Enabled; active = $Enabled; admin = $true; error = $null } }
    }
    function Wait-Icon([string] $Icon) { Wait-Until { (Log-Count $Log (' tray_icon_state icon=' + [regex]::Escape($Icon) + '$')) -ge 1 } "tray icon $Icon" 10 }
    function Cdp-ConnectApp([int] $Seconds = 30) {
        $Deadline = [DateTime]::UtcNow.AddSeconds($Seconds)
        while ([DateTime]::UtcNow -lt $Deadline) {
            $C = $null
            try {
                $Pages = [P3Web]::Http("http://127.0.0.1:$Port/json/list") | ConvertFrom-Json
                $Page = @($Pages | Where-Object { $_.type -eq "page" -and $_.url -like "http://tauri.localhost/*" -and $_.url -notlike "*dev-rt*" }) | Select-Object -First 1
                if ($Page) {
                    $C = New-Object P3Cdp $Page.webSocketDebuggerUrl
                    if ((Cdp-Eval $C "!!(window.T && window.T.rt && window.T.rt.state === 'live' && window.T.store.get('hostCaps'))") -eq $true) { return $C }
                    $C.Dispose()
                }
            } catch { if ($C) { $C.Dispose() } }
            Start-Sleep -Milliseconds 200
        }
        throw "app page with T.rt not live"
    }
    $Listen = "(()=>{window.__c3={tr:[],tier:[]};window.T.on('rt:transition',e=>window.__c3.tr.push(e));window.T.on('rt:tier',e=>window.__c3.tier.push(e));return true})()"
    $Seen = "JSON.stringify(window.__c3)"
    function Transition([string] $From, [string] $To) {
        return @{ topic = "tier.transition"; kind = "edge"; data = @{ from = $From; to = $To; observed_at_ms = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()); authority = "observed" } }
    }
    try {
        Set-SmokeEnvironment $Box @{ TESSERACT_SHELL_CDP_PORT = $Port; TESSERACT_REDUCED_MOTION = "1" }
        $Watcher = Start-WatchedShell $Box @("--dev-sidecar-script", ('"' + (Join-Path $Repo "shell/app/src/notify_fixture.py") + '"'))
        $ShellPid = [int]((Get-Content -LiteralPath (Join-Path $Box.box "root.json") -Raw | ConvertFrom-Json).pid)
        $ShellProcess = Get-Process -Id $ShellPid; $null = $ShellProcess.Handle
        Wait-Until { Test-Path -LiteralPath (Join-Path $FixtureState "ready") } "fixture ready" 30
        Wait-Until { (Log-Count $Log ' shell_window_ready$') -ge 1 } "shell window" 60

        # 1. Seed end, host:caps and reduced motion.
        Wait-Until { (Log-Count $Log ' rt_seed_done ') -ge 1 } "seed end" 30
        $Checks.seed_line = ((Get-Content -LiteralPath $Log) | Where-Object { $_ -match ' rt_seed_done ' } | Select-Object -First 1) -replace '^\d+ ',''
        $Cdp = Cdp-ConnectApp
        $Checks.host_caps = Cdp-Eval $Cdp "window.T.store.get('hostCaps')"
        $Checks.host_has_rt_resync = Cdp-Eval $Cdp "window.T.rt.hostHas('rt_resync')"
        if (-not $Checks.host_has_rt_resync -or @($Checks.host_caps.events) -notcontains "rt:replayed") { throw "host:caps missing rt_resync / rt:replayed" }
        $Checks.motion = Cdp-Eval $Cdp "JSON.stringify({value:window.__TESSERACT_MOTION__,frozen:Object.isFrozen(window.__TESSERACT_MOTION__)})"
        if ($Checks.motion -ne '{"value":{"reduced":true},"frozen":true}') { throw "motion flag: $($Checks.motion)" }
        if ((Log-Count $Log ' reduced_motion reduced=true$') -lt 1) { throw "shell.log lacks reduced_motion reduced=true" }

        # 2. The first live tier.transition of the session is live (not swallowed as replay).
        [void](Cdp-Eval $Cdp $Listen)
        # A tiers.summary baseline (Vanguard first, no fallback) so every page build has the context
        # it projects a transition against; it arrives live too and must not raise rt:tier.
        Push-Fixture @{ topic = "tiers.summary"; kind = "snapshot"; data = @{ carrier = "vanguard"; order = @("vanguard","sentinel","bastion","gateway"); fallback = $false; degraded = $false
            tiers = @{ vanguard = @{ enabled = $true; total = 1; usable = 1 }; sentinel = @{ enabled = $true; total = 1; usable = 1 }; bastion = @{ enabled = $false; total = 0; usable = 0 }; gateway = @{ enabled = $false; total = 0; usable = 0 } }
            pool = @{ usable = 0; total = 0 }; observed_at_ms = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) } }
        Push-Fixture (Transition "vanguard" "sentinel")
        Wait-Until { (Cdp-Eval $Cdp "window.__c3.tr.length") -ge 1 } "rt:transition on the page" 10
        Start-Sleep -Milliseconds 300
        $Checks.first_live = Cdp-Eval $Cdp $Seen
        $First = $Checks.first_live | ConvertFrom-Json
        if ($First.tr[0].instant -ne $false -or -not @($First.tier | Where-Object { $_.kind -eq "fallback" -and $_.instant -eq $false }).Count) { throw "first live transition treated as replay: $($Checks.first_live)" }

        # The tiers.summary a real sidecar sends after that switch (T-P4-H3, ninth-wave ruling): Sentinel
        # carries, Sentinel first in the order, fallback on. The page must hold it before the reload, so
        # the shell's replay after the reload brings the fallback level back (T-P4-H level reconciliation;
        # a cached summary older than the transition announces no recovery: a known edge).
        Push-Fixture @{ topic = "tiers.summary"; kind = "snapshot"; data = @{ carrier = "sentinel"; order = @("sentinel","vanguard","bastion","gateway"); fallback = $true; degraded = $false
            tiers = @{ vanguard = @{ enabled = $true; total = 1; usable = 0 }; sentinel = @{ enabled = $true; total = 1; usable = 1 }; bastion = @{ enabled = $false; total = 0; usable = 0 }; gateway = @{ enabled = $false; total = 0; usable = 0 } }
            pool = @{ usable = 0; total = 0 }; observed_at_ms = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) } }
        $SummaryHeld = "(()=>{const s=window.T.rt.topic('tiers.summary').get();return !!(s&&s.carrier==='sentinel'&&s.fallback===true&&Array.isArray(s.order)&&s.order[0]==='sentinel')})()"
        Wait-Until { (Cdp-Eval $Cdp $SummaryHeld) -eq $true } "fallback tiers.summary on the page" 10
        $Checks.fallback_summary = Cdp-Eval $Cdp "JSON.stringify((({carrier,order,fallback})=>({carrier,order,fallback}))(window.T.rt.topic('tiers.summary').get()))"

        # 3. After a page reload (fresh T.rt, full replay ending with rt:replayed) the next one is live too.
        try { [void](Cdp-Eval $Cdp "location.reload(),true" 3000) } catch { }
        $Cdp.Dispose(); $Cdp = $null
        Start-Sleep -Milliseconds 500
        $Cdp = Cdp-ConnectApp
        [void](Cdp-Eval $Cdp $Listen)
        Start-Sleep -Milliseconds 300
        $Checks.reload_replayed_transitions = Cdp-Eval $Cdp "window.__c3.tr.length"
        Push-Fixture (Transition "sentinel" "vanguard")
        Wait-Until { (Cdp-Eval $Cdp "window.__c3.tr.length") -ge 1 } "rt:transition after reload" 10
        Start-Sleep -Milliseconds 300
        $Checks.after_reload_live = Cdp-Eval $Cdp $Seen
        $After = $Checks.after_reload_live | ConvertFrom-Json
        # The shell's part is that the frame is live. With -RequireTierRecover (T-P4-H3: no guess from
        # the served bundle's source any more) the page must also announce exactly one live rt:tier
        # recover: the replayed fallback summary set the level, the live transition leaves it.
        $Checks.require_tier_recover = [bool]$RequireTierRecover
        if ($Checks.reload_replayed_transitions -ne 0 -or $After.tr[0].instant -ne $false -or @($After.tier | Where-Object { $_.instant -ne $false }).Count) { throw "transition after reload: $($Checks.after_reload_live)" }
        if ($RequireTierRecover) {
            $Recover = @($After.tier)
            if ($Recover.Count -ne 1 -or $Recover[0].kind -ne "recover" -or $Recover[0].instant -ne $false) { throw "recovery after reload (-RequireTierRecover): $($Checks.after_reload_live)" }
        }

        # 4. rt_resync from the page.
        $Resync = "(n,a)=>window.T.rt.invokeHost(n,a).then(r=>JSON.stringify(r),e=>'ERR:'+(e&&e.code))"
        $Checks.resync_cache = Cdp-Eval $Cdp "($Resync)('rt_resync',{topic:'service.state'})"
        $Checks.resync_again = Cdp-Eval $Cdp "($Resync)('rt_resync',{topic:'service.state'})"
        $Checks.resync_jobs = Cdp-Eval $Cdp "($Resync)('rt_resync',{topic:'jobs'})"
        if ($Checks.resync_cache -ne '{"source":"cache"}' -or $Checks.resync_again -ne 'ERR:rate_limited' -or $Checks.resync_jobs -ne 'ERR:not_allowed') {
            throw "rt_resync: $($Checks.resync_cache) / $($Checks.resync_again) / $($Checks.resync_jobs)"
        }

        # 5. D20 tray icon states (the fixture's first service.state is not a tray fact: unknown).
        Push-Fixture (Kill-Switch $true)
        Push-Fixture (Service-State "stopped"); Wait-Icon "stopped"
        Push-Fixture (Service-State "collecting"); Wait-Icon "starting"
        Push-Fixture (Service-State "running"); Wait-Icon "running"
        Push-Fixture (Kill-Switch $false); Wait-Icon "running-ks"
        Push-Fixture (Service-State "failed"); Wait-Icon "failed-ks"
        Push-Fixture (Kill-Switch $true); Wait-Icon "failed"
        $Checks.tray_icon_states = @((Get-Content -LiteralPath $Log) | Where-Object { $_ -match ' tray_icon_state icon=' } | ForEach-Object { ($_ -split 'icon=')[1] })
        $Checks.tray_status_icon_failed = Log-Count $Log ' tray_status_icon_failed '
        if ($Checks.tray_status_icon_failed -ne 0) { throw "a status icon failed to load" }

        # 6. Tray Quit: exit 0, nothing left.
        $Cdp.Dispose(); $Cdp = $null
        Stop-SmokeShell $ShellProcess $ShellPid
        if (-not $ShellProcess.HasExited -and -not $ShellProcess.WaitForExit(30000)) { throw "shell did not exit" }
        $ShellProcess.Refresh(); $Checks.shell_exit_code = $ShellProcess.ExitCode
        if ($Checks.shell_exit_code -ne 0) { throw "shell exit code $($Checks.shell_exit_code)" }
        $Remaining = Wait-ScopedWebViewsGone $Box.data $ShellProcess
        $Checks.scoped_webviews_after_exit = $Remaining.Count
        if ($Remaining.Count -gt 0) { $Checks.residue_class = Save-ResidueTree $Remaining $ShellProcess $Log $Box.box; throw "scoped WebView processes remain ($($Checks.residue_class))" }
        if (-not $Watcher.WaitForExit($InventoryWaitMs)) { throw (Inventory-TimeoutMessage (Join-Path $Box.box "process-inventory.json")) }
        $Inventory = Get-Content -LiteralPath (Join-Path $Box.box "process-inventory.json") -Raw | ConvertFrom-Json
        if (-not $Inventory.passed -or $Inventory.active_processes -ne 0) { throw "owned process residual detected" }
        $Checks.inventory_total_processes = $Inventory.total_processes
        $Passed = $true
    } catch {
        $Failure = $_.Exception.Message
    } finally {
        if ($Cdp) { $Cdp.Dispose() }
        if (-not $ShellProcess) { $Late = Resolve-LateShell (Join-Path $Box.box "root.json") $Watcher; if ($Late) { $Checks.late_shell = Close-LateShell $Late $Box.box } }
        Stop-SmokeShell $ShellProcess $ShellPid
        if ($Watcher -and -not $Watcher.HasExited) { [void]$Watcher.WaitForExit($InventoryWaitMs) }
        foreach ($Name in $SmokeNames) { [Environment]::SetEnvironmentVariable($Name,$Saved[$Name],"Process") }
        $Result = [ordered]@{ scenario = "c3"; passed = $Passed; error = $Failure; sandbox = $Box.box; checks = $Checks }
        [IO.File]::WriteAllText((Join-Path $Box.box "result.json"), ($Result | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding $false))
        if ($EvidenceDir) { Save-SmokeEvidence $Box.box "c3" }
    }
    if (-not $Passed) { throw "T-P4-C3 c3 scenario failed: $Failure (evidence: $($Box.box))" }
    Write-Output "T-P4-C3 c3 scenario passed; evidence: $($Box.box)"
}

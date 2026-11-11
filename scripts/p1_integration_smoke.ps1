[CmdletBinding()]
param([switch] $Source, [switch] $Rebuild, [string] $OutputDirectory = "", [switch] $SkipP1Shell, [switch] $SkipV1)

# Integration smoke on an isolated custom scope (never the live profile).
#   Stage P1 (headless_shell, Rust host): handshake, P1 topics, start/stop/quit, bye 0.
#   Stage v1 (T-P2-33, tests/test_bridge_p2_33.py smoke): hello caps = v1 set,
#     every topic subscribed, one real request for each of the 38 commands and
#     10 queries, start/stop, desktop-alert receipt round trip, quit and a
#     second epoch ending with app.sessionEnd, bye 0, no residual process.
# Both stages use a hardened sandbox written by tests/test_bridge_p2_33.py
# prepare: every listener on a free high port, DNS qualification, GeoData
# updates, Bastion, subscriptions, alert channels and the automatic exit
# lookup off, sources.yaml empty, APPDATA/LOCALAPPDATA inside the sandbox.
# Stage P1 (T-P2-H2, 33 smoke): headless_shell starts with an allowlisted
# environment only (the driver's ENV_ALLOWLIST plus the sandbox variables;
# no tokens, HTTP(S)_PROXY or PYTHON* from this shell), and after its run the
# sandbox is read back with the driver's readback_problems (sources still
# empty, nothing switched on, listeners still on the probed ports).
# The script reads HKCU Internet Settings (ProxyEnable, ProxyServer,
# ProxyOverride, AutoConfigURL) and counts firewall rules named *Tesseract*
# and ProxyManager-KillSwitch* (read only; names compared as sets) before and
# after; any difference, or a firewall query that fails, fails the run.

$ErrorActionPreference = "Stop"
$Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$Python = Join-Path $Repo ".venv/Scripts/python.exe"
$Driver = Join-Path $Repo "tests/test_bridge_p2_33.py"
$Bundle = Join-Path $Repo "dist-sidecar/tesseract-runtime"
# -Rebuild: build_runtime_sidecar.ps1 reads the CPU convention first and
# refuses to build while it says mode=timing.
if ($Rebuild) { & (Join-Path $PSScriptRoot "build_runtime_sidecar.ps1") -Python $Python }
if (-not $SkipP1Shell) {
    Push-Location -LiteralPath (Join-Path $Repo "shell")
    try {
        & cargo build --offline -j 1 -p headless_shell
        if ($LASTEXITCODE -ne 0) { throw "headless_shell build failed" }
    } finally { Pop-Location }
}
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $Repo (".build/p1-16-smoke/" + [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss-fff"))
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$SandboxRoot = Join-Path ([IO.Path]::GetTempPath()) ("tesseract-p1-16-" + [Guid]::NewGuid().ToString("N"))

function Get-HostNetworkFacts {
    $Key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    $Values = Get-ItemProperty -LiteralPath $Key
    $Facts = [ordered]@{}
    foreach ($Name in @("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL")) {
        $Property = $Values.PSObject.Properties[$Name]
        if ($null -eq $Property) { $Facts[$Name] = $null } else { $Facts[$Name] = [string]$Property.Value }
    }
    # A failed query must not read as "0 before, 0 after": record the error;
    # any error fails the run (before the start it stops the run at once).
    $Facts["FirewallError"] = $null
    try {
        $AllRules = @(Get-NetFirewallRule -ErrorAction Stop)
    } catch {
        $AllRules = @()
        $Facts["FirewallError"] = "Get-NetFirewallRule failed: $($_.Exception.Message)"
    }
    $Tesseract = @($AllRules | Where-Object {
        ($_.Name -like "*Tesseract*") -or ($_.DisplayName -like "*Tesseract*") })
    # killswitch.KILL_SWITCH_RULE_NAMES are named ProxyManager-KillSwitch-*.
    $KillSwitch = @($AllRules | Where-Object {
        ($_.Name -like "ProxyManager-KillSwitch*") -or ($_.DisplayName -like "ProxyManager-KillSwitch*") })
    $Facts["FirewallRulesTesseract"] = $Tesseract.Count
    $Facts["FirewallRulesKillSwitch"] = $KillSwitch.Count
    # Compare the name sets, not only the counts.
    $Facts["FirewallRuleNamesTesseract"] = (@($Tesseract | ForEach-Object { [string]$_.Name }) | Sort-Object) -join ";"
    $Facts["FirewallRuleNamesKillSwitch"] = (@($KillSwitch | ForEach-Object { [string]$_.Name }) | Sort-Object) -join ";"
    return $Facts
}

function Get-SmokeProcesses {
    # Processes of this smoke: the bundle's executables or anything whose
    # command line names this run's sandbox.
    $BundlePrefix = $Bundle.ToLowerInvariant()
    $SandboxPrefix = $SandboxRoot.ToLowerInvariant()
    return @(Get-CimInstance Win32_Process | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath.ToLowerInvariant().StartsWith($BundlePrefix)) -or
        ($_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($SandboxPrefix)) -or
        ($_.Name -eq "headless_shell.exe") })
}

function New-Sandbox([string] $Name, [bool] $Frozen) {
    $Directory = Join-Path $SandboxRoot $Name
    $Arguments = @("-B", $Driver, "prepare", "--sandbox", $Directory)
    if ($Frozen) { $Arguments += "--frozen" }
    $Json = & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "sandbox preparation failed" }
    return ($Json | Select-Object -Last 1 | ConvertFrom-Json)
}

$Before = Get-HostNetworkFacts
if ($Before["FirewallError"]) { throw "Refusing to run without a firewall baseline: $($Before['FirewallError'])" }
$ProcessesBefore = @(Get-SmokeProcesses | ForEach-Object { $_.ProcessId })
$Names = @("TESSERACT_CONFIG_DIR", "TESSERACT_DATA_DIR", "TESSERACT_INSTANCE_SCOPE", "TESSERACT_ISOLATED_AUDIT_MANIFEST",
           "PYTHONPATH", "TEMP", "TMP", "PYTHONDONTWRITEBYTECODE", "APPDATA", "LOCALAPPDATA")
$Previous = @{}
foreach ($Name in $Names) { $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process") }
$Failures = @()
try {
    $Runtime = Join-Path $Bundle "tesseract-runtime.exe"
    if (-not $Source -and -not (Test-Path -LiteralPath $Runtime -PathType Leaf)) { throw "Missing runtime: $Runtime" }
    if (-not $SkipP1Shell) {
        $SandboxDirectory = Join-Path $SandboxRoot "p1"
        $Sandbox = New-Sandbox "p1" (-not $Source)
        # Allowlisted child environment from the driver (ENV_ALLOWLIST plus the
        # sandbox's TESSERACT_*/TEMP/TMP/APPDATA/LOCALAPPDATA); nothing else of
        # this shell's environment reaches headless_shell or the sidecar.
        $ChildEnv = [ordered]@{}
        foreach ($Property in $Sandbox.env.PSObject.Properties) { $ChildEnv[$Property.Name] = [string]$Property.Value }
        $ChildEnv["TESSERACT_INSTANCE_SCOPE"] = "p1-integration-smoke"
        $ChildEnv["PYTHONDONTWRITEBYTECODE"] = "1"
        $ChildEnv.Remove("TESSERACT_ISOLATED_AUDIT_MANIFEST")
        $ChildEnv.Remove("PYTHONPATH")
        $Arguments = @("--exe", $Runtime, "--output", (Join-Path $OutputDirectory "frames.jsonl"))
        if ($Source) {
            # Windows venv python.exe is a redirector with a different child PID;
            # launch the interpreter itself and load the venv dependencies via PATH.
            $ChildEnv["PYTHONPATH"] = "$Repo;$(Join-Path $Repo '.venv/Lib/site-packages')"
            $Runtime = (& $Python -B -c "import sys; print(sys._base_executable)").Trim()
            # Bytecode warm-up as in the p3 smokes (T-P2-H2, C2 #10): the sidecar runs with -B, so
            # one import writes bytecode into a PYTHONPYCACHEPREFIX inside the sandbox (never the
            # source tree); the sidecar only reads it. headless_shell's hello window is unchanged.
            $PyCache = Join-Path $Sandbox.temp "pycache-warm"
            New-Item -ItemType Directory -Force -Path $PyCache | Out-Null
            # The warm-up child gets the same allowlisted environment (fix round 1), with
            # APPDATA/LOCALAPPDATA in a throwaway sandbox directory (as the driver's
            # warm_source_bytecode) and bytecode writing on; it imports the sidecar's whole
            # Qt-free closure. A timeout only leaves a partial cache.
            $WarmProfile = Join-Path $Sandbox.temp "warm-profile"
            New-Item -ItemType Directory -Force -Path $WarmProfile | Out-Null
            $WarmScript = Join-Path $Sandbox.temp "warm_bytecode.py"
            [IO.File]::WriteAllText($WarmScript, ("import importlib`nfrom sidecar_import_graph import sidecar_modules`n" +
                "for name in sidecar_modules():`n    try:`n        importlib.import_module(name)`n" +
                "    except Exception:`n        pass`n"))
            $WarmInfo = New-Object System.Diagnostics.ProcessStartInfo
            $WarmInfo.FileName = $Runtime
            $WarmInfo.Arguments = '"' + $WarmScript + '"'
            $WarmInfo.WorkingDirectory = $Repo
            $WarmInfo.UseShellExecute = $false
            $WarmInfo.CreateNoWindow = $true
            $WarmInfo.EnvironmentVariables.Clear()
            foreach ($Name in $ChildEnv.Keys) { $WarmInfo.EnvironmentVariables[$Name] = $ChildEnv[$Name] }
            $WarmInfo.EnvironmentVariables.Remove("PYTHONDONTWRITEBYTECODE")
            $WarmInfo.EnvironmentVariables["PYTHONPYCACHEPREFIX"] = $PyCache
            $WarmInfo.EnvironmentVariables["APPDATA"] = $WarmProfile
            $WarmInfo.EnvironmentVariables["LOCALAPPDATA"] = $WarmProfile
            $Warm = [System.Diagnostics.Process]::Start($WarmInfo)
            if (-not $Warm.WaitForExit(900000)) { $Warm.Kill(); $Warm.WaitForExit() }
            $ChildEnv["PYTHONPYCACHEPREFIX"] = $PyCache
            $Arguments = @("--exe", $Runtime, "--source", "--entry", (Join-Path $Repo "runtime_main.pyw"), "--output", (Join-Path $OutputDirectory "frames.jsonl"))
        }
        if (-not (Test-Path -LiteralPath $Runtime -PathType Leaf)) { throw "Missing runtime: $Runtime" }
        $Quoted = $Arguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }
        $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
        $StartInfo.FileName = Join-Path $Repo "shell/target/debug/headless_shell.exe"
        $StartInfo.Arguments = ($Quoted -join " ")
        $StartInfo.WorkingDirectory = $Repo
        $StartInfo.UseShellExecute = $false
        $StartInfo.CreateNoWindow = $true
        $StartInfo.RedirectStandardOutput = $true
        $StartInfo.RedirectStandardError = $true
        $StartInfo.EnvironmentVariables.Clear()
        foreach ($Name in $ChildEnv.Keys) { $StartInfo.EnvironmentVariables[$Name] = $ChildEnv[$Name] }
        $Process = [System.Diagnostics.Process]::Start($StartInfo)
        $StdoutTask = $Process.StandardOutput.ReadToEndAsync()
        $StderrTask = $Process.StandardError.ReadToEndAsync()
        $TimedOut = -not $Process.WaitForExit(65000)
        if ($TimedOut) {
            # Close only this harness; its pipe closure triggers sidecar shutdown.
            $Process.Kill()
        }
        $Process.WaitForExit()
        [IO.File]::WriteAllText((Join-Path $OutputDirectory "shell.stdout.log"), $StdoutTask.Result)
        [IO.File]::WriteAllText((Join-Path $OutputDirectory "shell.stderr.log"), $StderrTask.Result)
        if ($TimedOut) { throw "headless_shell external timeout" }
        if ($Process.ExitCode -ne 0) { throw "headless_shell failed: $($StderrTask.Result)" }
        foreach ($Name in $Names) { [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], "Process") }
        $env:PYTHONPATH = $Repo
        & $Python -B (Join-Path $Repo "tests/test_bridge_integration.py") (Join-Path $OutputDirectory "frames.jsonl") |
            Tee-Object -FilePath (Join-Path $OutputDirectory "summary.json")
        if ($LASTEXITCODE -ne 0) { throw "JSONL assertions failed" }
        # Sandbox read-back verdict (the driver's readback_problems).
        & $Python -B $Driver readback --sandbox $SandboxDirectory |
            Tee-Object -FilePath (Join-Path $OutputDirectory "p1.readback.json")
        if ($LASTEXITCODE -ne 0) { throw "P1 sandbox read-back failed (see p1.readback.json)" }
        Write-Output "P1 smoke passed: $OutputDirectory"
    }
    if (-not $SkipV1) {
        foreach ($Name in $Names) { [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], "Process") }
        $V1Output = Join-Path $OutputDirectory "v1"
        $Arguments = @("-B", $Driver, "smoke", "--sandbox", (Join-Path $SandboxRoot "v1"), "--output", $V1Output)
        if (-not $Source) { $Arguments += @("--exe", $Runtime) }
        & $Python @Arguments | Tee-Object -FilePath (Join-Path $OutputDirectory "v1.result.json")
        if ($LASTEXITCODE -ne 0) { $Failures += "v1 full-API scenario failed (see $V1Output/summary.json)" }
        else { Write-Output "v1 smoke passed: $V1Output" }
    }
} finally {
    foreach ($Name in $Names) { [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], "Process") }
    $Deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        $Residual = @(Get-SmokeProcesses | Where-Object { $ProcessesBefore -notcontains $_.ProcessId })
        if ($Residual.Count -eq 0) { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $Deadline)
    $After = Get-HostNetworkFacts
    $BundleBytes = $null
    if (Test-Path -LiteralPath $Bundle) {
        $BundleBytes = (Get-ChildItem -LiteralPath $Bundle -Recurse -File | Measure-Object -Property Length -Sum).Sum
    }
    $HostReport = [ordered]@{
        before = $Before; after = $After; bundle_bytes = $BundleBytes; sandbox = $SandboxRoot
        residual_processes = @($Residual | ForEach-Object { "$($_.ProcessId) $($_.Name)" })
    }
    $HostReport | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $OutputDirectory "host-facts.json") -Encoding UTF8
}
if ($After["FirewallError"]) { $Failures += "firewall rules unreadable after the run: $($After['FirewallError'])" }
foreach ($Name in @($Before.Keys)) {
    if ([string]$Before[$Name] -ne [string]$After[$Name]) { $Failures += "host fact changed: $Name '$($Before[$Name])' -> '$($After[$Name])'" }
}
if ($Residual.Count -gt 0) { $Failures += "residual processes: $($HostReport.residual_processes -join ', ')" }
if ($Failures.Count -gt 0) { throw ("Smoke failed:`n" + ($Failures -join "`n")) }
Write-Output "Host facts unchanged (HKCU Internet Settings, Tesseract firewall rules = $($After['FirewallRulesTesseract'])); no residual process; bundle bytes = $BundleBytes"

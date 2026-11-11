[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)] [string] $EvidenceBase,
    [string[]] $Rounds = @("1","2","3"),
    [string[]] $Smokes = @("shell","single_instance","tray","exit","notify","host"),
    [string] $Label = "round"
)
# T-P3-H fix 1: runs the p3 smokes for acceptance rounds, each smoke in its own custom scope
# (TESSERACT_INSTANCE_SCOPE + TESSERACT_CONFIG_DIR + TESSERACT_DATA_DIR under %TEMP%, never the
# default scope's mutexes/events), evidence under -EvidenceBase/<label><round>/<smoke> via
# TESSERACT_P3_EVIDENCE_ROOT. Before every round it records the machine load (total CPU and the
# busiest processes) in smoke-runs.txt, so a run that failed under someone else's load can be
# told apart from a regression (the real sidecar's 10 s hello window is load sensitive).
# The shell smoke runs with -SkipBuild: build the debug shell first under the ticket's CPU rules.
$ErrorActionPreference = "Continue"
$Repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
# `powershell -File ... -Rounds 1,2,3` passes one string "1,2,3": split it here.
$Rounds = @($Rounds | ForEach-Object { $_ -split "," } | Where-Object { $_ -ne "" })
$Smokes = @($Smokes | ForEach-Object { $_ -split "," } | Where-Object { $_ -ne "" })
foreach ($Smoke in $Smokes) {
    if (-not (Test-Path -LiteralPath (Join-Path $Repo ("scripts/p3_" + $Smoke + "_smoke.ps1")))) { throw "unknown smoke: $Smoke" }
}
New-Item -ItemType Directory -Force -Path $EvidenceBase | Out-Null
# Absolute, so every smoke (and its Python helpers) writes to the same place whatever its directory.
$EvidenceBase = (Resolve-Path -LiteralPath $EvidenceBase).Path
$Summary = Join-Path $EvidenceBase "smoke-runs.txt"
$Names = @("TESSERACT_INSTANCE_SCOPE","TESSERACT_CONFIG_DIR","TESSERACT_DATA_DIR","TESSERACT_P3_EVIDENCE_ROOT","TESSERACT_ISOLATED_AUDIT_MANIFEST")

# A reader (e.g. `tail -f`) may hold the summary open for a moment: retry, never lose a line.
function Add-Summary([string] $Text) {
    for ($Try = 1; $Try -le 50; $Try++) {
        try { [IO.File]::AppendAllText($Summary, $Text + "`n", (New-Object Text.UTF8Encoding $false)); return } catch { Start-Sleep -Milliseconds 200 }
    }
    Write-Warning "smoke-runs.txt not writable: $Text"
}
# Perf classes have stable (non-localized) names, unlike Get-Counter paths on a zh_CN system.
function Load-Line([string] $Tag) {
    try {
        $Cpus = [int](Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
        # Formatted counters are one short interval each: three samples, half a second apart.
        $Total = @(1..3 | ForEach-Object { if ($_ -gt 1) { Start-Sleep -Milliseconds 500 }; (Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name = '_Total'").PercentProcessorTime }) -join "/"
        $Top = @(Get-CimInstance Win32_PerfFormattedData_PerfProc_Process |
            Where-Object { $_.Name -ne "_Total" -and $_.Name -ne "Idle" -and $_.PercentProcessorTime -gt 0 } |
            Sort-Object PercentProcessorTime -Descending | Select-Object -First 5 |
            ForEach-Object { "{0}({1})={2:n0}%" -f $_.Name, $_.IDProcess, ($_.PercentProcessorTime / [Math]::Max(1,$Cpus)) })
        return "LOAD {0} at={1:o} cpu_total={2}% logical_cpus={3} top={4}" -f $Tag, [DateTime]::UtcNow, $Total, $Cpus, ($Top -join ",")
    } catch {
        return "LOAD {0} at={1:o} unavailable: {2}" -f $Tag, [DateTime]::UtcNow, $_.Exception.Message
    }
}

$Failed = 0
foreach ($Round in $Rounds) {
    $Load = Load-Line "$Label$Round"
    Add-Summary $Load
    Write-Output $Load
    foreach ($Smoke in $Smokes) {
        $Tag = "p3r-$Label$Round-$Smoke-" + [Guid]::NewGuid().ToString("N").Substring(0,8)
        $Root = Join-Path ([IO.Path]::GetTempPath()) ("tess-" + $Tag)
        $Config = Join-Path $Root "config"; $Data = Join-Path $Root "data"
        $Ev = Join-Path $EvidenceBase ("$Label$Round/$Smoke")
        foreach ($Dir in @($Config,$Data,$Ev)) { New-Item -ItemType Directory -Force -Path $Dir | Out-Null }
        $Saved = @{}; foreach ($Name in $Names) { $Saved[$Name] = [Environment]::GetEnvironmentVariable($Name,"Process") }
        $env:TESSERACT_INSTANCE_SCOPE = $Tag; $env:TESSERACT_CONFIG_DIR = $Config; $env:TESSERACT_DATA_DIR = $Data
        $env:TESSERACT_P3_EVIDENCE_ROOT = $Ev
        Remove-Item Env:TESSERACT_ISOLATED_AUDIT_MANIFEST -ErrorAction SilentlyContinue
        $Script = Join-Path $Repo ("scripts/p3_" + $Smoke + "_smoke.ps1")
        $Arguments = @("-NoProfile","-ExecutionPolicy","Bypass","-File",$Script)
        if ($Smoke -eq "shell") { $Arguments += @("-SkipBuild","-EvidenceDir",$Ev) }
        $Out = Join-Path $Ev "output.txt"
        $Start = [DateTime]::UtcNow
        & powershell.exe @Arguments *> $Out
        $Rc = $LASTEXITCODE
        $End = [DateTime]::UtcNow
        foreach ($Name in $Names) { [Environment]::SetEnvironmentVariable($Name,$Saved[$Name],"Process") }
        # Decision wave 6 (T-P3-H2): a smoke that met a locked workstation (single_instance needs a
        # real click on the desktop) writes smoke-skipped.json and exits 0: SKIP, not a failure.
        $Skipped = $null
        if ($Rc -eq 0) {
            foreach ($File in @(Get-ChildItem -LiteralPath $Ev -Recurse -Filter "smoke-skipped.json" -ErrorAction SilentlyContinue)) {
                try { $Found = Get-Content -LiteralPath $File.FullName -Raw | ConvertFrom-Json; $Skipped = $Found } catch { $Skipped = [pscustomobject]@{ reason = "unreadable_marker" } }
            }
        }
        if ($Rc -ne 0) { $Failed++ }
        # A failed smoke whose shell.log says hello_timeout most likely met a loaded machine (see LOAD).
        $HelloTimeouts = 0
        if ($Rc -ne 0 -or $Skipped) { $HelloTimeouts = @(Get-ChildItem -LiteralPath $Ev -Recurse -Filter "shell.log" -ErrorAction SilentlyContinue | Select-String -Pattern ' hello_timeout$').Count }
        $Line = "{0} {1}{2} {3} rc={4} start={5:o} end={6:o} seconds={7:n0} scope={8} hello_timeout_lines={9}" -f ($(if ($Rc -ne 0) {"FAIL"} elseif ($Skipped) {"SKIP"} else {"PASS"})), $Label, $Round, $Smoke, $Rc, $Start, $End, ($End-$Start).TotalSeconds, $Tag, $HelloTimeouts
        # T-P3-H2 fix 1: how the webview scenario's renderer crash took effect (renderer_terminate is
        # the fallback and never green), and any residue classes the evidence holds.
        $Extra = @()
        if ($Skipped) {
            $Extra += "skipped=$($Skipped.reason)"
            $NotPassed = @($Skipped.cases | Where-Object { -not $_.passed } | ForEach-Object { $_.case })
            if ($NotPassed.Count) { $Extra += "blocked_by_lock=" + ($NotPassed -join ",") }
        }
        foreach ($File in @(Get-ChildItem -LiteralPath $Ev -Recurse -Filter "result.json" -ErrorAction SilentlyContinue)) {
            try { $Found = Get-Content -LiteralPath $File.FullName -Raw | ConvertFrom-Json } catch { continue }
            if ($Found.scenario -eq "webview" -and $Found.checks.page_crash_method) {
                $Extra += "page_crash_method=$($Found.checks.page_crash_method)"
                if ($Found.checks.page_crash_method -eq "renderer_terminate") { $Extra += "PAGE_CRASH_FALLBACK" }
            }
        }
        $Classes = @(foreach ($Kind in @("process-inventory.json","residue-tree.json")) {
            foreach ($File in @(Get-ChildItem -LiteralPath $Ev -Recurse -Filter $Kind -ErrorAction SilentlyContinue)) {
                try { $Found = Get-Content -LiteralPath $File.FullName -Raw | ConvertFrom-Json; if ($Found.residue_class) { $Found.residue_class } } catch { }
            }
        })
        if ($Classes.Count) { $Extra += "residue_class=" + (($Classes | Sort-Object -Unique) -join "/") }
        if ($Extra.Count) { $Line += " " + ($Extra -join " ") }
        Add-Summary $Line
        Write-Output $Line
    }
}
if ($Failed -gt 0) { exit 1 }

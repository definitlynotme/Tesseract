[CmdletBinding()]
param(
    [ValidateSet("build", "smoke", "all")] [string] $Stage = "build",
    [switch] $Clean,
    [switch] $ReuseShell,
    [int] $Runs = 3,
    [string] $Label = "run",
    [string] $Evidence = "",
    [string] $Python = "",
    [string] $Out = "",
    [string] $App = "",
    [int] $WaitMinutes = 0
)
# Shell preview dist (T-P6-01): builds dist-shell/Tesseract (Tauri shell + PyInstaller sidecar
# + web UI, plan section 9.1-9.3), local only, never published. Only the Shell flavour: never
# calls scripts/build_windows.ps1 and never touches the Qt dist. Nothing is downloaded: cargo
# runs --offline --locked, npm and pip are not run. Private build directories only
# (.build/p6-ui, .build/p6-target, .build/p6-pyi), so the debug end-to-end runs (ui/dist,
# shell/app/ui, shell/target) are never written. The work is done by build_shell_preview.py;
# see its docstring for the stages and the gates.
# T-P6-06: the shell is the distribution build (cargo --features distribution): double-clicking
# Tesseract.exe opens the real (default) profile. The build only checks that with
# `Tesseract.exe --print-scope` under a fake environment; it never starts it there.
# -Out DIR assembles into DIR (side files next to it) instead of dist-shell/Tesseract;
# -App DIR smokes that directory; -WaitMinutes N waits (checking every 2 minutes) for programs
# running from the target to end instead of stopping at once (they are never ended).
# Shell unit tests: first `cargo build -p bridge_host --bins`, then `cargo test` (with and without --features distribution), CARGO_TARGET_DIR=.build/p6-target.
#
#   ./scripts/build_shell_preview.ps1 -Clean                  # build from a clean state
#   ./scripts/build_shell_preview.ps1 -Stage smoke -Runs 3    # isolated smoke + 3 cold starts
#   ./scripts/build_shell_preview.ps1 -Stage all -Clean
#   ./scripts/build_shell_preview.ps1 -ReuseShell             # sidecar again (e.g. after the
#        sidecar spec's input guard stopped a build because a backend file changed meanwhile)
$ErrorActionPreference = "Stop"
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $Python) { $Python = Join-Path $RepoRoot ".venv/Scripts/python.exe" }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python not found: $Python. Install build dependencies separately."
}
$Driver = Join-Path $PSScriptRoot "build_shell_preview.py"
$Common = @()
if ($Evidence) { $Common += @("--evidence", $Evidence) }
Push-Location -LiteralPath $RepoRoot
try {
    if ($Stage -in @("build", "all")) {
        $Arguments = @("-B", $Driver, "build") + $Common
        if ($Clean) { $Arguments += "--clean" }
        if ($ReuseShell) { $Arguments += "--reuse-shell" }
        if ($Out) { $Arguments += @("--out", $Out) }
        if ($WaitMinutes -gt 0) { $Arguments += @("--wait-minutes", "$WaitMinutes") }
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Shell preview build failed (exit $LASTEXITCODE)." }
    }
    if ($Stage -in @("smoke", "all")) {
        $Arguments = @("-B", $Driver, "smoke", "--runs", "$Runs", "--label", $Label) + $Common
        if ($App) { $Arguments += @("--app", $App) } elseif ($Out) { $Arguments += @("--app", $Out) }
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Shell preview smoke failed (exit $LASTEXITCODE)." }
    }
} finally {
    Pop-Location
}

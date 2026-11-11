[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
# Refuse before any process/Win32 access. Caller must explicitly opt in.
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
$env:PYTHONDONTWRITEBYTECODE = "1"
# The caller builds incrementally under the ticket's CPU rules; this script never builds.
& (Join-Path $Repo ".venv/Scripts/python.exe") -B (Join-Path $Repo "shell/app/src/p3_instance_smoke.py")
if ($LASTEXITCODE -ne 0) { throw "P3-02 single-instance smoke failed" }
